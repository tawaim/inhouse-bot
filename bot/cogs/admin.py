"""Admin commands: result reporting, channel config, rank syncing."""
from __future__ import annotations

import difflib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select, update

from bot.config import Config, ROLES, format_team_lines
from bot.db.models import Alias, GameStat, GuildConfig, Match, MatchPerformance, Player, ProposalSet, Rating, Session as InhouseSession, Signup
from bot.db.session import get_session
from bot.services.champions import resolve_champion
from bot.services.report_analysis import (
    GameResult,
    ReportState,
    build_game_proposal,
)
from bot.services.ocr import parse_scoreboard_image
from bot.services.elo import (
    DEFAULT_ELO,
    INHOUSE_ROLE,
    average_elo,
    parse_series_score,
    seed_from_past_season,
    seed_from_rank,
    update_elo_team_game,
)
from bot.services.opgg_client import OpggClient
from bot.services.riot_client import RiotAuthError, RiotClient

log = logging.getLogger(__name__)


# =============================================================================
# Manual match parsing
# =============================================================================

# Discord mention format: <@123456789> or <@!123456789> (the ! is for nicknames)
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(.+?)\s*$")

# Display + Discord ROLE names for the two teams (these keep their spaces).
# team1 -> "lo gang", team2 -> "team 10". Used by /match-channels and
# /clear-match-channels and the result embeds.
TEAM_NAMES = {1: "lo gang", 2: "team 10"}


def team_channel_name(team_name: str) -> str:
    """A team's text-channel name. Discord channel names can't contain spaces and
    are lowercased, so the channel is the hyphenated form of the role/display
    name: 'lo gang' -> 'lo-gang', 'team 10' -> 'team-10'."""
    return team_name.lower().replace(" ", "-")


async def _safe_fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    """Return a guild member from cache, falling back to an API fetch. None if
    the user isn't in the server (e.g. they left after the roster was built)."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.HTTPException:
        return None


class ManualMatchParseError(ValueError):
    """Raised when the manual match input is malformed. Message is shown to admin."""


def parse_manual_match(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Parse a strict line-based team roster.

    Expected format (case-insensitive headers, exact role names):
        TEAM 1
        TOP: <@123>
        JUNGLE: <@456>
        MID: <@789>
        BOT: <@111>
        SUPPORT: <@222>
        TEAM 2
        TOP: <@333>
        ...

    Returns (team1_dict, team2_dict) where each dict maps role -> discord_id.

    Raises ManualMatchParseError with a human-readable message on any issue.
    """
    if not text or not text.strip():
        raise ManualMatchParseError("Input is empty.")

    current_team: Optional[int] = None
    teams: dict[int, dict[str, int]] = {1: {}, 2: {}}

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        # Team header?
        upper = line.upper()
        if upper in ("TEAM 1", "TEAM1", "T1"):
            current_team = 1
            continue
        if upper in ("TEAM 2", "TEAM2", "T2"):
            current_team = 2
            continue

        # Role line
        if current_team is None:
            raise ManualMatchParseError(
                f"Line {line_num}: `{line}` — expected a TEAM header first."
            )
        m = _ROLE_LINE_RE.match(line)
        if not m:
            raise ManualMatchParseError(
                f"Line {line_num}: `{line}` — expected `ROLE: @user`."
            )

        role = m.group(1).upper()
        # Accept some shorthand
        role_aliases = {
            "TOP": "TOP",
            "JUNGLE": "JUNGLE", "JG": "JUNGLE", "JUNG": "JUNGLE",
            "MID": "MID", "MIDDLE": "MID",
            "BOT": "BOT", "BOTTOM": "BOT", "ADC": "BOT",
            "SUPPORT": "SUPPORT", "SUP": "SUPPORT", "SUPP": "SUPPORT",
        }
        if role not in role_aliases:
            raise ManualMatchParseError(
                f"Line {line_num}: unknown role `{role}`. "
                f"Use TOP / JUNGLE / MID / BOT / SUPPORT."
            )
        canonical = role_aliases[role]

        if canonical in teams[current_team]:
            raise ManualMatchParseError(
                f"Line {line_num}: {canonical} already assigned on team {current_team}."
            )

        mention_match = _MENTION_RE.search(m.group(2))
        if not mention_match:
            raise ManualMatchParseError(
                f"Line {line_num}: no Discord mention found in `{m.group(2)}`. "
                f"Use @-mentions, e.g. `TOP: @alice`."
            )
        teams[current_team][canonical] = int(mention_match.group(1))

    # Validate completeness
    for team_num in (1, 2):
        missing = [r for r in ROLES if r not in teams[team_num]]
        if missing:
            raise ManualMatchParseError(
                f"Team {team_num} is missing: {', '.join(missing)}."
            )

    # No duplicate players across teams
    t1_ids = set(teams[1].values())
    t2_ids = set(teams[2].values())
    overlap = t1_ids & t2_ids
    if overlap:
        raise ManualMatchParseError(
            f"Player(s) on both teams: {', '.join(f'<@{i}>' for i in overlap)}."
        )
    if len(t1_ids) != 5 or len(t2_ids) != 5:
        raise ManualMatchParseError(
            "Each team must have 5 distinct players (a player was listed twice)."
        )

    return teams[1], teams[2]


def format_roster_block(team1: dict[str, int], team2: dict[str, int]) -> str:
    """Render two role->discord_id team dicts as the line-based roster that
    parse_manual_match accepts. Inverse of parse_manual_match:
        parse_manual_match(format_roster_block(t1, t2)) == (t1, t2)
    Used by /match-roster to print a copy/paste-ready, editable roster.
    """
    lines = ["TEAM 1"]
    lines += [f"{r}: <@{team1[r]}>" for r in ROLES if r in team1]
    lines.append("TEAM 2")
    lines += [f"{r}: <@{team2[r]}>" for r in ROLES if r in team2]
    return "\n".join(lines)


# =============================================================================
# Modal for /manual-match input
# =============================================================================

class ManualMatchModal(discord.ui.Modal, title="Manual Match Entry"):
    roster = discord.ui.TextInput(
        label="Team roster",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
        placeholder=(
            "TEAM 1\n"
            "TOP: @user1\n"
            "JUNGLE: @user2\n"
            "MID: @user3\n"
            "BOT: @user4\n"
            "SUPPORT: @user5\n"
            "TEAM 2\n"
            "TOP: @user6\n..."
        ),
    )

    def __init__(
        self,
        session_id: Optional[int],
        edit_match_id: Optional[int] = None,
        prefill: Optional[str] = None,
        create_channels: bool = False,
        channels_builder=None,
    ):
        super().__init__()
        self.session_id = session_id
        self.edit_match_id = edit_match_id  # when set, UPDATE this match instead of creating one
        # When True, build the per-team roles + private channels right after the
        # match is saved. channels_builder is the cog's _build_match_channels.
        self.create_channels = create_channels
        self._channels_builder = channels_builder
        if prefill:
            self.roster.default = prefill   # pre-fills the text box for in-place editing

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            team1, team2 = parse_manual_match(self.roster.value)
        except ManualMatchParseError as e:
            await interaction.followup.send(f"❌ Parse error: {e}", ephemeral=True)
            return

        all_ids = list(team1.values()) + list(team2.values())

        async with get_session() as db:
            # Verify every mentioned user is linked + approved
            unlinked = []
            for pid in all_ids:
                player = await db.get(Player, pid)
                if player is None or not player.riot_puuid or player.link_status != "approved":
                    unlinked.append(pid)
            if unlinked:
                mentions = " ".join(f"<@{i}>" for i in unlinked)
                await interaction.followup.send(
                    f"❌ Not linked / not approved: {mentions}\n"
                    f"They must run `/link` (and be approved) before being used in a manual match.",
                    ephemeral=True,
                )
                return

            if self.edit_match_id is not None:
                # Edit-in-place: replace an existing match's roster.
                match = await db.get(Match, self.edit_match_id)
                if match is None:
                    await interaction.followup.send(
                        f"❌ Match {self.edit_match_id} no longer exists.", ephemeral=True
                    )
                    return
                if match.winner is not None:
                    await interaction.followup.send(
                        "❌ That match is already reported — run `/unreport` first, then edit.",
                        ephemeral=True,
                    )
                    return
                match.team1_json = json.dumps(team1)
                match.team2_json = json.dumps(team2)
                verb = "updated"
            else:
                match = Match(
                    session_id=self.session_id,
                    team1_json=json.dumps(team1),
                    team2_json=json.dumps(team2),
                    predicted_balance=None,  # n/a for manual
                )
                db.add(match)
                verb = "created"

            session = await db.get(InhouseSession, match.session_id) if match.session_id else None
            if session and session.status in ("recruiting", "closed"):
                session.status = "matched"
            await db.commit()
            await db.refresh(match)
            match_id = match.id
            session_id = match.session_id
            game_date = session.game_date if session else None
            channel_id = session.recruit_channel_id if session else None

        # Post the (new or updated) teams to the recruit channel, if there is one.
        title = "🏆 Updated Teams" if verb == "updated" else "🏆 Manual Teams"
        if game_date:
            title += f" for Thursday {game_date.strftime('%b %d')}"
        embed = discord.Embed(title=title, color=discord.Color.green())
        all_ids = list(team1.values()) + list(team2.values())
        name_map = {
            uid: (m.display_name if (m := interaction.guild.get_member(uid)) else str(uid))
            for uid in all_ids
        } if interaction.guild else None
        embed.add_field(name=f"🔵 {TEAM_NAMES[1]} (Blue)", value=format_team_lines(team1, name_map=name_map), inline=True)
        embed.add_field(name=f"🔴 {TEAM_NAMES[2]} (Red)", value=format_team_lines(team2, name_map=name_map), inline=True)
        footer = f"Match {match_id}"
        if session_id is not None:
            footer += f" · Session #{session_id}"
        footer += f" · Manual entry by {interaction.user.display_name}"
        embed.set_footer(text=footer)

        if channel_id:
            channel = interaction.client.get_channel(channel_id)
            if channel:
                await channel.send(content=f"🔒 Teams ({verb}):", embed=embed)

        posted = " Posted to recruit channel." if channel_id else ""
        msg = (
            f"✅ Match {match_id} {verb} with manual roster.{posted}\n"
            f"Report the result with `/report` or `/report-manual` when games are done."
        )
        # Optional one-shot: build the per-team roles + channels for this match.
        if self.create_channels and self._channels_builder and interaction.guild:
            channel_summary = await self._channels_builder(
                interaction.guild, interaction.guild_id, match_id
            )
            msg += f"\n\n{channel_summary}"
        await interaction.followup.send(msg, ephemeral=True)


class EditRosterView(discord.ui.View):
    """Ephemeral 'Edit teams' button attached to /match-roster. Opens the manual
    roster modal pre-filled with the current teams and updates the match in place."""

    def __init__(self, match_id: int):
        super().__init__(timeout=300)
        self.match_id = match_id

    @discord.ui.button(label="Edit teams", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with get_session() as db:
            match = await db.get(Match, self.match_id)
            if match is None:
                await interaction.response.send_message("That match no longer exists.", ephemeral=True)
                return
            if match.winner is not None:
                await interaction.response.send_message(
                    "That match is already reported — run `/unreport` first, then edit.", ephemeral=True
                )
                return
            team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}
            session_id = match.session_id
        await interaction.response.send_modal(
            ManualMatchModal(session_id, edit_match_id=self.match_id,
                             prefill=format_roster_block(team1, team2))
        )


class PickupSeriesModal(discord.ui.Modal, title="Pickup Series Result"):
    """Standalone series report — no recruitment session needed.
    Admin pastes 2 rosters + series score, bot creates a Match row with
    session_id=NULL, runs elo updates, posts confirmation.
    """
    roster = discord.ui.TextInput(
        label="Team rosters",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
        placeholder=(
            "TEAM 1\n"
            "TOP: @user1\n"
            "JUNGLE: @user2\n"
            "MID: @user3\n"
            "BOT: @user4\n"
            "SUPPORT: @user5\n"
            "TEAM 2\n"
            "TOP: @user6\n..."
        ),
    )
    series_score = discord.ui.TextInput(
        label="Series score (e.g. 2-0, 2-1)",
        style=discord.TextStyle.short,
        required=True,
        max_length=8,
        placeholder="2-1",
    )

    def __init__(self, commit_callback):
        super().__init__()
        self._commit = commit_callback

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Parse series score first
        team1_wins, team2_wins = parse_series_score(self.series_score.value)
        if team1_wins < 0:
            await interaction.followup.send(
                f'❌ Invalid series score "{self.series_score.value}". Use "2-0", "2-1", "1-2", "0-2".',
                ephemeral=True,
            )
            return

        # Parse roster
        try:
            team1, team2 = parse_manual_match(self.roster.value)
        except ManualMatchParseError as e:
            await interaction.followup.send(f"❌ Parse error: {e}", ephemeral=True)
            return

        all_ids = list(team1.values()) + list(team2.values())

        async with get_session() as db:
            # Verify everyone is linked
            unlinked = []
            for pid in all_ids:
                player = await db.get(Player, pid)
                if player is None or not player.riot_puuid or player.link_status != "approved":
                    unlinked.append(pid)
            if unlinked:
                mentions = " ".join(f"<@{i}>" for i in unlinked)
                await interaction.followup.send(
                    f"❌ Not linked / not approved: {mentions}",
                    ephemeral=True,
                )
                return

            # Create a session-less Match row
            match = Match(
                session_id=None,
                team1_json=json.dumps(team1),
                team2_json=json.dumps(team2),
                predicted_balance=None,
            )
            db.add(match)
            await db.commit()
            await db.refresh(match)
            match_id = match.id

        # Apply elo updates via the cog's commit helper
        await self._commit(match_id, team1_wins, team2_wins, None, interaction.user.id, None)

        # Post confirmation in the channel
        winner_label = TEAM_NAMES[1] if team1_wins > team2_wins else TEAM_NAMES[2]
        from bot.config import ROLE_EMOJIS
        embed = discord.Embed(
            title=f"🎮 Pickup Series Result: {team1_wins}-{team2_wins}",
            description=f"**{winner_label} wins.** Elo updated for all 10 players.",
            color=discord.Color.green(),
        )
        t1 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team1[r]}>" for r in ROLES)
        t2 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team2[r]}>" for r in ROLES)
        embed.add_field(name=f"🔵 {TEAM_NAMES[1]} (Blue)", value=t1, inline=True)
        embed.add_field(name=f"🔴 {TEAM_NAMES[2]} (Red)", value=t2, inline=True)
        embed.set_footer(text=f"Match {match_id} · Pickup by {interaction.user.display_name}")

        # Post publicly in the same channel where the command was run
        if interaction.channel:
            await interaction.channel.send(embed=embed)

        await interaction.followup.send(
            f"✅ Pickup series recorded as match {match_id}.",
            ephemeral=True,
        )


# =============================================================================
# Name resolution (/resolve-names, /set-alias) — turn informal names from a
# signup/screenshot into the <@id> block /manual-match accepts.
# =============================================================================

# A line that's just a team header ("TEAM 1", "T2") — passed through untouched.
_TEAM_HEADER_RE = re.compile(r"^(team\s*\d+|t\d+)$", re.IGNORECASE)
# Below this difflib ratio we don't even suggest a fuzzy match.
_FUZZY_CUTOFF = 0.72


def _norm_name(s: str) -> str:
    """The match key: lowercased, trimmed, whitespace-collapsed, leading @ dropped."""
    s = s.strip().lstrip("@").strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _name_variants(s: str) -> set[str]:
    """Normalized forms accepted as an exact match: the full name and the name
    with any '(...)' suffix stripped — so 'kaari (inhouse arena champ)' still
    matches a pasted 'kaari'."""
    out: set[str] = set()
    full = _norm_name(s)
    if full:
        out.add(full)
    stripped = _norm_name(re.sub(r"\(.*?\)", "", s))
    if stripped:
        out.add(stripped)
    return out


async def _build_candidate_index(
    guild: discord.Guild, db
) -> tuple[dict[str, set[int]], dict[int, str]]:
    """Build {normalized identity string -> {discord_ids}} over all linked+approved
    players (matching on Riot game name + Discord display name / username / nick),
    plus a {discord_id -> display label} map. Only linked players are candidates,
    since they're the only ones valid in /manual-match."""
    rows = (
        await db.execute(
            select(Player).where(
                Player.link_status == "approved", Player.riot_puuid.isnot(None)
            )
        )
    ).scalars().all()

    index: dict[str, set[int]] = defaultdict(set)
    labels: dict[int, str] = {}
    for p in rows:
        member = guild.get_member(p.discord_id)
        strings = [p.riot_game_name]
        if member is not None:
            strings += [member.display_name, member.name, member.global_name, member.nick]
        for s in strings:
            if not s:
                continue
            for v in _name_variants(s):
                index[v].add(p.discord_id)
        labels[p.discord_id] = (
            member.display_name if member is not None else (p.riot_game_name or str(p.discord_id))
        )
    return index, labels


async def resolve_name_block(guild: discord.Guild, raw: str) -> tuple[str, list[str], int]:
    """Rewrite a pasted block of plain names into <@id> mentions.

    Lines shaped like 'ROLE: name' keep the 'ROLE:' prefix; team headers pass
    through; any other line is treated as a bare name. Confident, unambiguous
    exact matches are saved as aliases (so they're instant next time). Returns
    (rewritten_block, notes, learned_alias_count)."""
    async with get_session() as db:
        alias_map = {
            a.alias_norm: a.discord_id
            for a in (await db.execute(select(Alias))).scalars().all()
        }
        index, labels = await _build_candidate_index(guild, db)
        keys = list(index.keys())

        out_lines: list[str] = []
        notes: list[str] = []
        learned = 0

        def label(i: int) -> str:
            return f"{labels.get(i, i)} (<@{i}>)"

        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line:
                out_lines.append("")
                continue
            if _TEAM_HEADER_RE.match(line):
                out_lines.append(line)
                continue
            m = _ROLE_LINE_RE.match(line)
            if m:
                prefix, namepart = f"{m.group(1)}: ", m.group(2)
            else:
                prefix, namepart = "", line
            name = namepart.strip()
            n = _norm_name(name)

            # 1. Known alias — exact, trusted.
            if n in alias_map:
                out_lines.append(f"{prefix}<@{alias_map[n]}>")
                continue
            # 2. Exact match on a live identity string.
            if n in index:
                ids = index[n]
                if len(ids) == 1:
                    did = next(iter(ids))
                    out_lines.append(f"{prefix}<@{did}>")
                    db.add(Alias(alias_norm=n, discord_id=did, alias=name))
                    alias_map[n] = did
                    learned += 1
                    continue
                out_lines.append(f"{prefix}⚠️ {name}")
                notes.append(
                    f"❓ **{name}** is ambiguous — matches {', '.join(label(i) for i in ids)}. "
                    f"Pick one with `/set-alias`."
                )
                continue
            # 3. Fuzzy fallback — suggested, never auto-saved.
            cand_ids: list[int] = []
            for k in difflib.get_close_matches(n, keys, n=5, cutoff=_FUZZY_CUTOFF):
                for i in index[k]:
                    if i not in cand_ids:
                        cand_ids.append(i)
            if len(cand_ids) == 1:
                did = cand_ids[0]
                out_lines.append(f"{prefix}<@{did}>  ← guess, verify")
                notes.append(
                    f"🤔 Guessed **{name}** → {label(did)} (not saved). Fix with `/set-alias` if wrong."
                )
                continue
            if len(cand_ids) > 1:
                out_lines.append(f"{prefix}⚠️ {name}")
                notes.append(
                    f"🤔 **{name}** is close to {', '.join(label(i) for i in cand_ids[:5])}. "
                    f"Disambiguate with `/set-alias`."
                )
                continue
            # 4. Nothing.
            out_lines.append(f"{prefix}❌ {name}")
            notes.append(
                f"❌ No match for **{name}** — are they `/link`'d? "
                f"Map them with `/set-alias member:@them alias:{name}`."
            )

        if learned:
            await db.commit()

    return "\n".join(out_lines), notes, learned


# --- Single-name resolution, reused for screenshot player identification ------

@dataclass
class NameMatch:
    """Result of resolving one in-game/scoreboard name to a Discord player.

    `discord_id` is set only for a CONFIDENT match (stored alias or a unique
    exact identity hit). Ambiguous, fuzzy, and no-match cases leave it None and
    surface `candidates` so the caller can offer an ephemeral picker; the chosen
    member is then saved as an alias so the name auto-resolves next time.
    """
    name: str
    discord_id: Optional[int]
    confidence: str  # "alias" | "exact" | "ambiguous" | "fuzzy" | "none"
    candidates: list[int] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.discord_id is not None


def _resolve_single_name(
    name: str,
    alias_map: dict[str, int],
    index: dict[str, set[int]],
    keys: list[str],
) -> NameMatch:
    """Resolve one name against the alias map + candidate index. Same tiers as
    resolve_name_block (alias → exact → fuzzy), but returns structured data
    instead of a rewritten line."""
    n = _norm_name(name)
    if not n:
        return NameMatch(name, None, "none", [])
    if n in alias_map:
        did = alias_map[n]
        return NameMatch(name, did, "alias", [did])
    if n in index:
        ids = list(index[n])
        if len(ids) == 1:
            return NameMatch(name, ids[0], "exact", ids)
        return NameMatch(name, None, "ambiguous", ids)
    cand_ids: list[int] = []
    for k in difflib.get_close_matches(n, keys, n=5, cutoff=_FUZZY_CUTOFF):
        for i in index[k]:
            if i not in cand_ids:
                cand_ids.append(i)
    if len(cand_ids) == 1:
        return NameMatch(name, None, "fuzzy", cand_ids)
    if len(cand_ids) > 1:
        return NameMatch(name, None, "ambiguous", cand_ids)
    return NameMatch(name, None, "none", [])


async def match_player_names(
    guild: discord.Guild, names: list[str]
) -> tuple[list[NameMatch], dict[int, str]]:
    """Resolve a list of scoreboard names to Discord players. Returns the matches
    (in input order) plus a {discord_id -> display label} map for rendering."""
    async with get_session() as db:
        alias_map = {
            a.alias_norm: a.discord_id
            for a in (await db.execute(select(Alias))).scalars().all()
        }
        index, labels = await _build_candidate_index(guild, db)
        keys = list(index.keys())
        matches = [_resolve_single_name(nm, alias_map, index, keys) for nm in names]
    return matches, labels


class ResolveNamesModal(discord.ui.Modal, title="Resolve names → mentions"):
    """Paste a list of plain names (or a full roster with role labels); returns the
    same block with each name swapped for its <@id> mention, ready for /manual-match."""

    names = discord.ui.TextInput(
        label="Names (one per line; 'ROLE: name' ok)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
        placeholder=(
            "Robo\ncarter_k\nkaari\n\n"
            "…or a full roster:\n"
            "TEAM 1\nTOP: kaari\nJUNGLE: Max\n..."
        ),
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Run this in a server.", ephemeral=True)
            return
        # Populate the member cache so we can match on Discord display names/nicks.
        try:
            if not guild.chunked:
                await guild.chunk()
        except Exception:
            log.warning("resolve-names: guild chunk failed; using cache", exc_info=True)

        block, notes, learned = await resolve_name_block(guild, self.names.value)
        msg = f"**Resolved** — paste into `/manual-match`:\n```\n{block}\n```"
        if learned:
            msg += f"\n🧠 Learned {learned} new alias(es)."
        if notes:
            msg += "\n\n" + "\n".join(notes[:12])
        await interaction.followup.send(
            msg[:1950], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


# =============================================================================
# /report — multi-screenshot, per-game confirm flow
# =============================================================================

def _report_embed(state: ReportState, labels: dict[int, str]) -> discord.Embed:
    """Render the current proposed report for the confirm message."""
    e = discord.Embed(title=f"Confirm report — Match {state.match_id}",
                      color=discord.Color.orange())
    for gi, (g, winner) in enumerate(zip(state.games, state.winners), start=1):
        lines = []
        for team in (1, 2):
            tag = "🔵" if team == 1 else "🔴"
            crown = " 🏆" if winner == team else ""
            lines.append(f"{tag} **{TEAM_NAMES[team]}**{crown}")
            for s in [x for x in g.slots if x.team == team]:
                who = labels.get(s.discord_id, f"<@{s.discord_id}>") if s.discord_id \
                    else f"⚠️ {s.name_guess or '?'}"
                mark = " 🔁" if s.is_sub else ""  # subbed-in player (not the rostered one)
                champ = s.champion or "—"
                kda = f"{s.kills}/{s.deaths}/{s.assists}" if s.kills is not None else "—"
                lines.append(f"`{s.role:7}` {who}{mark} · {champ} · {kda}")
        e.add_field(name=f"Game {gi}", value="\n".join(lines), inline=False)
    ok, problems = state.ready()
    e.set_footer(text=("⚠️ " + " · ".join(problems[:3])) if problems
                 else "Set winners, fix players/champions, then ✅ Commit.")
    return e


class _WinnerButton(discord.ui.Button):
    def __init__(self, gi: int, state: ReportState):
        super().__init__(label=f"G{gi+1}: {TEAM_NAMES[state.winners[gi]]}",
                         style=discord.ButtonStyle.secondary, row=0)
        self.gi = gi

    async def callback(self, interaction: discord.Interaction):
        self.view.state.toggle_winner(self.gi)
        await self.view.refresh(interaction)


class _SlotSelect(discord.ui.Select):
    """Pick which roster slot to change. Slots are pre-filled from the match roster,
    so this is mainly for confirming/correcting subs (listed first) — or fixing the
    rare slot OCR got wrong."""

    def __init__(self, state: ReportState):
        slots = [(gi, s) for gi, g in enumerate(state.games) for s in g.slots]
        slots.sort(key=lambda gs: (not gs[1].is_sub, gs[0], gs[1].team))  # subs first
        options = []
        for gi, s in slots[:25]:  # Discord caps a select at 25 options
            read = s.name_guess or "—"
            options.append(discord.SelectOption(
                label=f"G{gi+1} · {TEAM_NAMES[s.team]} · {s.role}"[:100],
                value=f"{gi}:{s.team}:{s.role}",
                description=(("SUB → " if s.is_sub else "read: ") + read)[:100],
            ))
        super().__init__(placeholder="Change a slot (subs listed first)…",
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        gi, team, role = self.values[0].split(":")
        self.view._sel = (int(gi), int(team), role)
        self.view.show_fix()
        await self.view.refresh(interaction)


class _SlotPlayerSelect(discord.ui.UserSelect):
    """Pick the player for the slot chosen in _SlotSelect. Saves the OCR'd name as
    an alias so that player auto-resolves next time."""

    def __init__(self, sel):
        gi, _team, role = sel
        super().__init__(placeholder=f"Set G{gi+1} {role} to…",
                         min_values=1, max_values=1, row=1)
        self.sel = sel

    async def callback(self, interaction: discord.Interaction):
        gi, team, role = self.sel
        member = self.values[0]
        v = self.view
        v.state.set_player(gi, team, role, member.id)
        v.labels[member.id] = member.display_name
        slot = v.state._slot(gi, team, role)
        if slot and slot.name_guess:
            await v.cog._learn_alias(slot.name_guess, member.id)
        v._sel = None
        v.show_fix()
        await v.refresh(interaction)


_KDA_LINE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$")


class _ChampionModal(discord.ui.Modal, title="Champions & KDA — one player per line"):
    """Edit champion + KDA per player. One paragraph box per game, 10 lines in the
    shown order (T1 top→sup, then T2 top→sup); each line is 'Champion k/d/a'."""

    def __init__(self, view: "ReportConfirmView"):
        super().__init__()
        self.view_ref = view
        self.boxes = []
        for gi, g in enumerate(view.state.games):
            lines = []
            for s in g.slots:
                kda = f"{s.kills}/{s.deaths}/{s.assists}" if s.kills is not None else ""
                lines.append(f"{s.champion or ''} {kda}".strip())
            box = discord.ui.TextInput(
                label=f"Game {gi+1} — 'Champion k/d/a' per line",
                style=discord.TextStyle.paragraph, required=False,
                default="\n".join(lines), max_length=600,
            )
            self.boxes.append(box)
            self.add_item(box)

    async def on_submit(self, interaction: discord.Interaction):
        for gi, box in enumerate(self.boxes):
            slots = self.view_ref.state.games[gi].slots
            for i, line in enumerate(box.value.splitlines()):
                if i >= len(slots) or not line.strip():
                    continue
                m = _KDA_LINE_RE.search(line)
                if m:
                    slots[i].kills = int(m.group(1))
                    slots[i].deaths = int(m.group(2))
                    slots[i].assists = int(m.group(3))
                    line = line[: m.start()].strip()
                if line.strip():
                    slots[i].champion = resolve_champion(line) or line.strip()
        await self.view_ref.refresh(interaction)


class ReportConfirmView(discord.ui.View):
    """Ephemeral confirm/correct UI for /report. Drives a pure ReportState; the
    Discord components are a thin shell over its tested transitions."""

    def __init__(self, cog, state: ReportState, labels: dict[int, str],
                 screenshot_url: Optional[str], author_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.state = state
        self.labels = labels
        self.screenshot_url = screenshot_url
        self.author_id = author_id
        self._sel = None  # (game_idx, team, role) currently being edited in fix mode
        self.show_main()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your report.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=_report_embed(self.state, self.labels), view=self)

    def show_main(self):
        self.clear_items()
        for gi in range(len(self.state.games)):
            self.add_item(_WinnerButton(gi, self.state))
        fix = discord.ui.Button(label="Subs / fix", emoji="🔁",
                                style=discord.ButtonStyle.primary, row=1)
        fix.callback = self._on_fix
        champs = discord.ui.Button(label="Champs / KDA", emoji="⚔️",
                                   style=discord.ButtonStyle.primary, row=1)
        champs.callback = self._on_champions
        commit = discord.ui.Button(label="Commit", emoji="✅",
                                   style=discord.ButtonStyle.success, row=2)
        commit.callback = self._on_commit
        cancel = discord.ui.Button(label="Cancel", emoji="❌",
                                   style=discord.ButtonStyle.danger, row=2)
        cancel.callback = self._on_cancel
        for b in (fix, champs, commit, cancel):
            self.add_item(b)

    def show_fix(self):
        self.clear_items()
        self.add_item(_SlotSelect(self.state))
        if self._sel is not None:
            self.add_item(_SlotPlayerSelect(self._sel))
        done = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, row=4)
        done.callback = self._on_done
        self.add_item(done)

    async def _on_fix(self, interaction: discord.Interaction):
        self._sel = None
        self.show_fix()
        await self.refresh(interaction)

    async def _on_done(self, interaction: discord.Interaction):
        self.show_main()
        await self.refresh(interaction)

    async def _on_champions(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ChampionModal(self))

    async def _on_cancel(self, interaction: discord.Interaction):
        for c in self.children:
            c.disabled = True
        self.stop()
        await interaction.response.edit_message(content="❌ Report cancelled.", embed=None, view=None)

    async def _on_commit(self, interaction: discord.Interaction):
        ok, problems = self.state.ready()
        if not ok:
            await interaction.response.send_message(
                "Can't commit yet:\n• " + "\n• ".join(problems), ephemeral=True)
            return
        await interaction.response.defer()
        games = self.state.to_game_results()
        await self.cog._commit_games(self.state.match_id, games, self.screenshot_url, self.author_id)
        self.stop()
        await interaction.edit_original_response(
            content=f"✅ Match {self.state.match_id} recorded — {len(games)} game(s), elo updated.",
            embed=_report_embed(self.state, self.labels), view=None,
        )


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config, riot: RiotClient, opgg: OpggClient):
        self.bot = bot
        self.config = config
        self.riot = riot
        self.opgg = opgg

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.name == self.config.admin_role_name for r in interaction.user.roles)

    @app_commands.command(name="set-channel", description="(admin) Configure which channel the bot posts to.")
    @app_commands.describe(
        purpose="Which type of channel to configure",
        channel="The channel to use",
    )
    async def set_channel(
        self,
        interaction: discord.Interaction,
        purpose: Literal["recruit", "results"],
        channel: discord.TextChannel,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        async with get_session() as db:
            cfg = await db.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                db.add(cfg)
            if purpose == "recruit":
                cfg.recruit_channel_id = channel.id
            elif purpose == "results":
                cfg.results_channel_id = channel.id
            await db.commit()
        hint = (
            " — the weekly auto-post and `/recruit-now` will post here."
            if purpose == "recruit" else ""
        )
        # Warn now if the bot can't actually post here, rather than letting the
        # weekly auto-post or /recruit-now fail later with a raw 403.
        perms = channel.permissions_for(channel.guild.me)
        missing = [
            name for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            ) if not ok
        ]
        warn = (
            f"\n⚠️ Heads up: I'm missing {', '.join(missing)} in that channel — "
            f"posting will fail until you grant it."
            if missing else ""
        )
        await interaction.response.send_message(
            f"✅ {purpose} channel set to {channel.mention}{hint}{warn}", ephemeral=True
        )

    @app_commands.command(
        name="set-match-category",
        description="(admin) Set the category where /match-channels creates per-team channels.",
    )
    @app_commands.describe(category="The category to create the team channels under")
    async def set_match_category(
        self, interaction: discord.Interaction, category: discord.CategoryChannel
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        async with get_session() as db:
            cfg = await db.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                db.add(cfg)
            cfg.match_category_id = category.id
            await db.commit()
        # Warn now if the bot can't create roles/channels, rather than failing later.
        me = interaction.guild.me
        missing = [
            name for name, ok in (
                ("Manage Channels", me.guild_permissions.manage_channels),
                ("Manage Roles", me.guild_permissions.manage_roles),
            ) if not ok
        ]
        warn = (
            f"\n⚠️ Heads up: I'm missing {', '.join(missing)} — `/match-channels` will fail "
            f"until you grant it (and my role must sit above the team roles)."
            if missing else ""
        )
        await interaction.response.send_message(
            f"✅ Match channels will be created under **{category.name}**.{warn}", ephemeral=True
        )

    @app_commands.command(
        name="resolve-names",
        description="(admin) Turn a list of plain names into a /manual-match mention block.",
    )
    async def resolve_names(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(ResolveNamesModal())

    @app_commands.command(
        name="set-alias",
        description="(admin) Teach the bot that a name maps to a Discord member.",
    )
    @app_commands.describe(
        member="The Discord member",
        alias="The name as it appears in screenshots / signups (e.g. 'Robo')",
    )
    async def set_alias(
        self, interaction: discord.Interaction, member: discord.Member, alias: str
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        norm = _norm_name(alias)
        if not norm:
            await interaction.response.send_message("❌ Alias can't be empty.", ephemeral=True)
            return
        async with get_session() as db:
            row = await db.get(Alias, norm)
            if row is None:
                db.add(Alias(alias_norm=norm, discord_id=member.id, alias=alias.strip()))
                verb = "Added"
            else:
                row.discord_id = member.id
                row.alias = alias.strip()
                verb = "Updated"
            await db.commit()
        await interaction.response.send_message(
            f"✅ {verb} alias **{alias.strip()}** → {member.mention}.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="aliases", description="(admin) List stored name aliases.")
    @app_commands.describe(member="Optionally filter to one member's aliases")
    async def aliases(
        self, interaction: discord.Interaction, member: Optional[discord.Member] = None
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        async with get_session() as db:
            q = select(Alias).order_by(Alias.alias_norm)
            if member is not None:
                q = q.where(Alias.discord_id == member.id)
            rows = (await db.execute(q)).scalars().all()
        if not rows:
            await interaction.response.send_message("No aliases stored yet.", ephemeral=True)
            return
        by_user: dict[int, list[str]] = defaultdict(list)
        for a in rows:
            by_user[a.discord_id].append(a.alias)
        lines = [f"<@{did}> — {', '.join(sorted(al))}" for did, al in by_user.items()]
        body = "**Stored aliases:**\n" + "\n".join(lines)
        await interaction.response.send_message(
            body[:1950], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="remove-alias", description="(admin) Delete a stored name alias.")
    @app_commands.describe(alias="The alias text to remove")
    async def remove_alias(self, interaction: discord.Interaction, alias: str):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        async with get_session() as db:
            row = await db.get(Alias, _norm_name(alias))
            if row is None:
                await interaction.response.send_message(
                    f"No alias **{alias.strip()}** found.", ephemeral=True
                )
                return
            await db.delete(row)
            await db.commit()
        await interaction.response.send_message(
            f"🗑️ Removed alias **{alias.strip()}**.", ephemeral=True
        )

    async def _create_team_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        name: str,
        roster: dict[str, int],
        admin_role: Optional[discord.Role],
        enemy_name: Optional[str] = None,
        enemy_roster: Optional[dict[str, int]] = None,
    ) -> str:
        """Create-or-reuse the team's role + private channel, assign the role to
        the roster's members, and post an intro. Returns a one-line summary.

        If enemy_name/enemy_roster are given, the intro also lists the opposing
        team by role so players have the matchups handy in their own channel."""
        # Role: reuse a same-named one if present, else create it.
        role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(
                name=name, mentionable=True, reason="In-house match team role"
            )
        added: list[discord.Member] = []
        missing = 0
        for pid in roster.values():
            member = await _safe_fetch_member(guild, pid)
            if member is None:
                missing += 1
                continue
            await member.add_roles(role, reason="In-house match team")
            added.append(member)

        # Private channel: only this role (+ bot + admins) can see it.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if admin_role is not None:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )
        chan_name = team_channel_name(name)  # 'lo gang' -> 'lo-gang'
        channel = discord.utils.get(category.text_channels, name=chan_name)
        if channel is None:
            channel = await guild.create_text_channel(
                chan_name, category=category, overwrites=overwrites,
                reason="In-house match team channel",
            )
        else:
            await channel.edit(overwrites=overwrites)

        mentions = " ".join(m.mention for m in added) or "_(no members found)_"
        message = f"**{name}** — your team for this match: {mentions}"
        if enemy_roster:
            enemy_block = format_team_lines(enemy_roster, emoji=True)
            enemy_label = f"enemy team ({enemy_name})" if enemy_name else "enemy team"
            message += f"\n\n__Vs. {enemy_label}:__\n{enemy_block}"
        await channel.send(message)
        miss = f", {missing} not in server" if missing else ""
        return f"• {channel.mention} ({role.mention}) — {len(added)} added{miss}"

    async def _build_match_channels(
        self, guild: discord.Guild, guild_id: int, match_id: int
    ) -> str:
        """Create-or-reuse the per-team roles + private channels for a match and
        return a human-readable summary (success, or the reason it couldn't).
        Shared by /match-channels and /manual-match's create_channels flag — does
        not send any message itself, so callers control the response."""
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                return f"❌ Match {match_id} not found — channels not created."
            cfg = await db.get(GuildConfig, guild_id)
            team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}

        category = (
            guild.get_channel(cfg.match_category_id)
            if cfg and cfg.match_category_id else None
        )
        if not isinstance(category, discord.CategoryChannel):
            return (
                "⚠️ No match category configured — run `/set-match-category`, "
                f"then `/match-channels match_id:{match_id}`."
            )

        admin_role = discord.utils.get(guild.roles, name=self.config.admin_role_name)
        try:
            lines = []
            for team_no, roster, enemy_no, enemy_roster in (
                (1, team1, 2, team2),
                (2, team2, 1, team1),
            ):
                lines.append(
                    await self._create_team_channel(
                        guild, category, TEAM_NAMES[team_no], roster, admin_role,
                        enemy_name=TEAM_NAMES[enemy_no], enemy_roster=enemy_roster,
                    )
                )
        except discord.Forbidden:
            return (
                "❌ I lack permission to create roles/channels. Grant me **Manage Roles** + "
                "**Manage Channels**, and make sure my role sits above the team roles."
            )
        return f"✅ Match {match_id} team channels ready:\n" + "\n".join(lines)

    @app_commands.command(
        name="match-channels",
        description="(admin) Create private per-team roles + channels for a match.",
    )
    @app_commands.describe(match_id="The match ID to build team channels for")
    async def match_channels(self, interaction: discord.Interaction, match_id: int):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        summary = await self._build_match_channels(
            interaction.guild, interaction.guild_id, match_id
        )
        await interaction.followup.send(summary, ephemeral=True)

    @app_commands.command(
        name="clear-match-channels",
        description="(admin) Delete the per-team channels and strip the team roles from members.",
    )
    async def clear_match_channels(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        async with get_session() as db:
            cfg = await db.get(GuildConfig, interaction.guild_id)
        category = (
            guild.get_channel(cfg.match_category_id)
            if cfg and cfg.match_category_id else None
        )

        removed: list[str] = []
        problems: list[str] = []

        # --- Phase 1: delete the two private channels (hyphenated names) ---
        for name in TEAM_NAMES.values():
            chan_name = team_channel_name(name)
            channel = None
            if isinstance(category, discord.CategoryChannel):
                channel = discord.utils.get(category.text_channels, name=chan_name)
            if channel is None:
                channel = discord.utils.get(guild.text_channels, name=chan_name)
            if channel is not None:
                try:
                    await channel.delete(reason="In-house match cleanup")
                    removed.append(f"#{chan_name}")
                except discord.Forbidden:
                    problems.append(f"can't delete #{chan_name}")

        # --- Phase 2: strip the roles (runs independently of Phase 1) ---
        # Pull the FULL member list from the API so this never depends on a warm
        # member cache — the original bug was a cold cache leaving role.members
        # empty, so channels deleted but the roles were never removed.
        members: list[discord.Member] = []
        try:
            members = [m async for m in guild.fetch_members(limit=None)]
        except Exception:
            log.warning("clear-match-channels: fetch_members failed; using cache", exc_info=True)
            members = list(guild.members)

        for name in TEAM_NAMES.values():
            # Match the spaced role name, but also fall back to the hyphenated
            # form in case an older run created the role with hyphens.
            role = (discord.utils.get(guild.roles, name=name)
                    or discord.utils.get(guild.roles, name=team_channel_name(name)))
            if role is None:
                problems.append(f"role '{name}' not found")
                continue
            # Union the API result with any cached holders, just in case.
            holders = {m for m in members if role in m.roles} | set(role.members)
            stripped = 0
            for member in holders:
                try:
                    await member.remove_roles(role, reason="In-house match cleanup")
                    stripped += 1
                except discord.Forbidden:
                    pass
            if stripped:
                removed.append(f"@{name} (cleared from {stripped})")
            elif holders:
                problems.append(f"can't remove @{name} — move my role above it")

        msg = ("🧹 Removed: " + ", ".join(removed)) if removed else "Nothing to remove."
        if problems:
            msg += "\n⚠️ " + "; ".join(problems)
        await interaction.followup.send(msg, ephemeral=True)

    async def _build_resolver(self, guild: discord.Guild):
        """Build a name->discord_id resolver over the alias index + linked players,
        plus a {discord_id -> display label} map. Only CONFIDENT matches resolve
        (alias or unique exact); fuzzy/ambiguous return None so the slot goes to the
        ephemeral picker."""
        async with get_session() as db:
            alias_map = {a.alias_norm: a.discord_id
                         for a in (await db.execute(select(Alias))).scalars().all()}
            index, labels = await _build_candidate_index(guild, db)
            keys = list(index.keys())

        def resolve(name: str) -> Optional[int]:
            return _resolve_single_name(name, alias_map, index, keys).discord_id

        return resolve, labels

    async def _learn_alias(self, name: str, discord_id: int) -> None:
        """Persist a confirmed scoreboard-name -> player mapping so it auto-resolves
        next time."""
        n = _norm_name(name)
        if not n:
            return
        async with get_session() as db:
            existing = await db.get(Alias, n)
            if existing is None:
                db.add(Alias(alias_norm=n, discord_id=discord_id, alias=name))
            elif existing.discord_id != discord_id:
                existing.discord_id = discord_id
            await db.commit()

    async def _games_from_gamestats(self, db, match) -> list:
        """Rebuild the per-game lineups (GameResult list) from a match's saved
        GameStat rows, so it can be rescored without re-uploading screenshots.
        GameStat stores role + won + champion + KDA per player per game; the team
        split comes from the won flag, oriented to the match's teams by roster
        overlap."""
        ros1 = set(int(v) for v in json.loads(match.team1_json).values())
        ros2 = set(int(v) for v in json.loads(match.team2_json).values())
        rows = (await db.execute(
            select(GameStat).where(GameStat.match_id == match.id).order_by(GameStat.game_no)
        )).scalars().all()
        by_game: dict[int, list] = defaultdict(list)
        for r in rows:
            by_game[r.game_no].append(r)
        games = []
        for gno in sorted(by_game):
            g = by_game[gno]
            win_ids = {r.discord_id for r in g if r.won}
            winners_are_t1 = len(win_ids & ros1) >= len(win_ids & ros2)
            games.append(GameResult(
                winner=1 if winners_are_t1 else 2,
                team1={r.role: r.discord_id for r in g if r.won == winners_are_t1},
                team2={r.role: r.discord_id for r in g if r.won != winners_are_t1},
                champions={r.discord_id: r.champion for r in g if r.champion},
                kdas={r.discord_id: (r.kills, r.deaths, r.assists)
                      for r in g if r.kills is not None},
            ))
        return games

    @app_commands.command(
        name="rescore-match",
        description="(admin) Recompute a reported match's elo with current settings — no re-upload.",
    )
    @app_commands.describe(match_id="The match to rescore (uses its saved per-game data)")
    async def rescore_match(self, interaction: discord.Interaction, match_id: int):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                return
            if match.winner is None:
                await interaction.followup.send(
                    f"Match {match_id} isn't reported — nothing to rescore.", ephemeral=True)
                return
            games = await self._games_from_gamestats(db, match)
            screenshot_url = match.screenshot_url
        if not games:
            await interaction.followup.send(
                f"Match {match_id} has no per-game data (reported before per-game tracking). "
                f"Re-report it with `/report` to rescore.", ephemeral=True)
            return
        # Reverse the old elo, then re-apply the same lineups with the current tuning.
        await self._revert_result(match_id)
        await self._commit_games(match_id, games, screenshot_url, interaction.user.id)
        await interaction.followup.send(
            f"✅ Rescored Match {match_id} with current elo settings ({len(games)} game(s)). "
            f"Player counts and champions/KDA preserved.", ephemeral=True)

    @app_commands.command(
        name="report",
        description="(admin) Report a match from up to 3 game screenshots.",
    )
    @app_commands.describe(
        match_id="The match ID from the teams post",
        game1="Game 1 scoreboard screenshot",
        game2="Game 2 screenshot (optional)",
        game3="Game 3 screenshot (optional)",
    )
    async def report(
        self,
        interaction: discord.Interaction,
        match_id: int,
        game1: discord.Attachment,
        game2: Optional[discord.Attachment] = None,
        game3: Optional[discord.Attachment] = None,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                return
            if match.winner is not None:
                await interaction.followup.send(
                    f"Match {match_id} already reported — /unreport first to correct it.",
                    ephemeral=True,
                )
                return
            fixed_t1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            fixed_t2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}

        guild = interaction.guild
        if guild is not None and not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                log.warning("report: guild chunk failed; matching on cache", exc_info=True)
        resolve, labels = await self._build_resolver(guild)

        attachments = [a for a in (game1, game2, game3) if a is not None]
        proposals = []
        for att in attachments:
            parsed = parse_scoreboard_image(await att.read())
            proposals.append(build_game_proposal(parsed, resolve, fixed_t1, fixed_t2))

        state = ReportState.from_proposals(match_id, proposals)
        view = ReportConfirmView(self, state, labels, game1.url, interaction.user.id)
        await interaction.followup.send(embed=_report_embed(state, labels), view=view, ephemeral=True)

    @app_commands.command(name="report-manual", description="(admin) Report best-of-3 series outcome without a screenshot.")
    @app_commands.describe(
        series_score='Series score from team1 perspective: "2-0", "2-1", "1-2", or "0-2"',
        match_id="The match ID from the teams post",
        session_id="Session ID — reports that session's latest unreported match",
        game_date="Game date YYYY-MM-DD (must be a Thursday) — alternative to session_id",
    )
    async def report_manual(
        self,
        interaction: discord.Interaction,
        series_score: str,
        match_id: Optional[int] = None,
        session_id: Optional[int] = None,
        game_date: Optional[str] = None,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return

        team1_wins, team2_wins = parse_series_score(series_score)
        if team1_wins < 0:
            await interaction.response.send_message(
                'Invalid series score. Use "2-0", "2-1", "1-2", or "0-2" (team1-team2).',
                ephemeral=True,
            )
            return

        if sum(x is not None for x in (match_id, session_id, game_date)) != 1:
            await interaction.response.send_message(
                "Provide exactly one of `match_id:`, `session_id:`, or `game_date:`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            if match_id is not None:
                match = await db.get(Match, match_id)
                if match is None:
                    await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                    return
            else:
                # Resolve a session (by id or date), then its latest unreported match.
                if session_id is not None:
                    session = await db.get(InhouseSession, session_id)
                    if session is None:
                        await interaction.followup.send(f"Session #{session_id} not found.", ephemeral=True)
                        return
                else:
                    try:
                        target_date = datetime.strptime(game_date, "%Y-%m-%d").date()
                    except ValueError:
                        await interaction.followup.send(
                            "Date must be YYYY-MM-DD format (e.g. `2026-05-14`).",
                            ephemeral=True,
                        )
                        return
                    if target_date.weekday() != 3:
                        await interaction.followup.send("Date must be a Thursday.", ephemeral=True)
                        return
                    session = (await db.execute(
                        select(InhouseSession).where(InhouseSession.game_date == target_date)
                    )).scalars().first()
                    if session is None:
                        await interaction.followup.send(f"No session for {game_date}.", ephemeral=True)
                        return
                match = (await db.execute(
                    select(Match)
                    .where(Match.session_id == session.id, Match.winner.is_(None))
                    .order_by(Match.id.desc())
                )).scalars().first()
                if match is None:
                    await interaction.followup.send(
                        f"No unreported match for Session #{session.id}.",
                        ephemeral=True,
                    )
                    return
                match_id = match.id

            if match.winner is not None:
                await interaction.followup.send(f"Match {match_id} already reported.", ephemeral=True)
                return

        await self._commit_result(match_id, team1_wins, team2_wins, None, interaction.user.id, None)
        winner_team = TEAM_NAMES[1] if team1_wins > team2_wins else TEAM_NAMES[2]
        await interaction.followup.send(
            f"✅ Match {match_id} recorded. Series {team1_wins}-{team2_wins} ({winner_team} wins). Elo updated.",
            ephemeral=True,
        )

    @app_commands.command(
        name="unreport",
        description="(admin) Undo a reported series — reverses the elo and clears the result.",
    )
    @app_commands.describe(match_id="The match ID to un-report (lets you re-report with a corrected score)")
    async def unreport(self, interaction: discord.Interaction, match_id: int):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        status, count, prev_score = await self._revert_result(match_id)
        if status == "not_found":
            await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
            return
        if status == "not_reported":
            await interaction.followup.send(
                f"Match {match_id} hasn't been reported — nothing to undo.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Un-reported match {match_id} (was {prev_score}). Reversed elo for {count} players. "
            f"You can now re-report it with the correct score.",
            ephemeral=True,
        )

    async def _revert_result(self, match_id: int) -> tuple[str, int, str]:
        """Reverse a reported series (mirror of _commit_result). Returns
        (status, players_reversed, prev_score) where status is
        'ok' | 'not_found' | 'not_reported'."""
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                return ("not_found", 0, "")
            if match.winner is None:
                return ("not_reported", 0, "")

            perfs = (await db.execute(
                select(MatchPerformance).where(MatchPerformance.match_id == match_id)
            )).scalars().all()

            # How many games each player actually played, from GameStat. Pre-per-game
            # ("legacy") matches have no GameStat rows — fall back to 1 per player,
            # matching how they were committed (one rating event per series).
            gstats = (await db.execute(
                select(GameStat).where(GameStat.match_id == match_id)
            )).scalars().all()
            games_by_pid: dict[int, int] = defaultdict(int)
            for gs in gstats:
                games_by_pid[gs.discord_id] += 1
            legacy = len(gstats) == 0

            # Reverse each player's stored (summed) deltas on their role + INHOUSE
            # ratings, and decrement games_played by the games they played. Exactly
            # restores pre-report state.
            for perf in perfs:
                cnt = 1 if legacy else games_by_pid.get(perf.discord_id, 0)
                role_rating = await db.get(Rating, (perf.discord_id, perf.role))
                if role_rating is not None:
                    role_rating.inhouse_modifier -= perf.role_elo_delta or 0
                    role_rating.games_played = max(0, role_rating.games_played - cnt)
                    role_rating.elo = role_rating.base_seed + role_rating.inhouse_modifier
                overall = await db.get(Rating, (perf.discord_id, INHOUSE_ROLE))
                if overall is not None:
                    overall.inhouse_modifier -= perf.inhouse_elo_delta or 0
                    overall.games_played = max(0, overall.games_played - cnt)
                    overall.elo = overall.base_seed + overall.inhouse_modifier
                    overall.total_balance_diff -= (match.predicted_balance or 0.0) * cnt
                await db.delete(perf)
            for gs in gstats:
                await db.delete(gs)

            prev_score = f"{match.team1_wins}-{match.team2_wins}"
            match.winner = None
            match.team1_wins = 0
            match.team2_wins = 0
            match.reported_by = None
            match.reported_at = None
            match.screenshot_url = None

            # Roll the session back from completed -> matched so it reads as un-played.
            if match.session_id is not None:
                session = await db.get(InhouseSession, match.session_id)
                if session and session.status == "completed":
                    session.status = "matched"

            await db.commit()
        return ("ok", len(perfs), prev_score)

    @app_commands.command(name="sync-ranks", description="(admin) Refresh Riot rank for all linked players. Updates base_seed only.")
    async def sync_ranks(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self._refresh_all_base_seeds()
        await interaction.followup.send(
            f"✅ Synced {result['updated']} players · "
            f"updated base_seed on {result['rows_updated']} rating rows · "
            f"{result['skipped']} manually-seeded (skipped) · "
            f"{result['errors']} errors",
            ephemeral=True,
        )

    async def _refresh_base_seed_for_player(self, db, player: Player) -> int:
        """Re-derive one player's base_seed from current Riot rank (or OP.GG past
        season), updating all their rating rows in place. The caller commits.

        Returns the number of rating rows written; 0 means no current or past
        rank was found anywhere, in which case base_seed is left as-is. Sets
        riot_last_synced regardless. Does NOT consult manual_seed — callers
        decide whether a player should be refreshed.
        """
        rank = await self.riot.get_solo_rank(player.riot_puuid)
        past = None
        if rank is None:
            past = await self.opgg.get_past_season_rank(
                player.riot_game_name, player.riot_tag_line, player.region
            )

        if rank:
            player.solo_tier = rank.tier
            player.solo_rank = rank.rank
            player.solo_lp = rank.league_points
            new_seed = seed_from_rank(rank.tier, rank.rank)
        elif past:
            new_seed = seed_from_past_season(past.tier, past.division, past.seasons_elapsed)
        else:
            new_seed = None  # no current or past rank anywhere; leave base_seed as-is

        player.riot_last_synced = datetime.utcnow()
        if new_seed is None:
            return 0

        player.last_synced_seed_elo = new_seed
        rows = 0
        for role in [*ROLES, INHOUSE_ROLE]:
            r = await db.get(Rating, (player.discord_id, role))
            if r is None:
                db.add(Rating(
                    discord_id=player.discord_id,
                    role=role,
                    elo=new_seed,
                    base_seed=new_seed,
                    inhouse_modifier=0,
                    games_played=0,
                ))
            else:
                r.base_seed = new_seed
                r.elo = r.base_seed + r.inhouse_modifier
            rows += 1
        return rows

    async def _refresh_all_base_seeds(self) -> dict:
        """Refresh base_seed for every linked player from current Riot rank.
        Used by both /sync-ranks (manual) and the Monday close job. Players with
        a manual seed (set via /set-seed) are skipped so an admin override isn't
        clobbered. Returns counts dict for logging.
        """
        async with get_session() as db:
            players = (await db.execute(
                select(Player).where(
                    Player.riot_puuid.is_not(None),
                    Player.link_status == "approved",
                )
            )).scalars().all()
            updated = 0
            errors = 0
            rows_updated = 0
            skipped = 0
            for player in players:
                if player.manual_seed:
                    skipped += 1
                    continue
                try:
                    rows_updated += await self._refresh_base_seed_for_player(db, player)
                    updated += 1
                except RiotAuthError:
                    raise
                except Exception:
                    log.exception("Failed to sync %s", player.discord_id)
                    errors += 1
            await db.commit()
        return {"updated": updated, "rows_updated": rows_updated, "errors": errors, "skipped": skipped}

    @app_commands.command(
        name="reseed-all",
        description="(admin) Refresh base_seed for everyone from current Riot rank. inhouse_modifier preserved.",
    )
    @app_commands.describe(confirm="Type 'yes' to confirm")
    async def reseed_all(self, interaction: discord.Interaction, confirm: str = ""):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        if confirm.lower() != "yes":
            await interaction.response.send_message(
                "⚠️ This refreshes base_seed for every linked player from their current Riot rank. "
                "inhouse_modifier (W/L from inhouse games) is preserved. "
                "Run again with `confirm:yes` to proceed.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self._refresh_all_base_seeds()
        except RiotAuthError:
            await interaction.followup.send("❌ Riot API key rejected.", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Reseed complete · "
            f"refreshed base_seed for {result['updated']} players "
            f"({result['rows_updated']} rating rows) · "
            f"{result['skipped']} manually-seeded (skipped) · "
            f"inhouse_modifier preserved · "
            f"{result['errors']} errors",
            ephemeral=True,
        )

    @app_commands.command(
        name="set-seed",
        description="(admin) Manually set a player's base_seed. Locks it against auto-refresh until /refresh-seed.",
    )
    @app_commands.describe(
        user="The player whose base_seed to set",
        seed="The base_seed elo value to apply to every role row",
    )
    async def set_seed(self, interaction: discord.Interaction, user: discord.Member, seed: int):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            player = await db.get(Player, user.id)
            if player is None:
                await interaction.followup.send(
                    f"{user.mention} has no player record — they need to /link first.",
                    ephemeral=True,
                )
                return
            # Apply to all role rows (creating any that are missing), keeping each
            # row's inhouse_modifier so displayed elo = manual seed + W/L.
            for role in [*ROLES, INHOUSE_ROLE]:
                r = await db.get(Rating, (user.id, role))
                if r is None:
                    db.add(Rating(
                        discord_id=user.id,
                        role=role,
                        elo=seed,
                        base_seed=seed,
                        inhouse_modifier=0,
                        games_played=0,
                    ))
                else:
                    r.base_seed = seed
                    r.elo = r.base_seed + r.inhouse_modifier
            player.manual_seed = True
            player.last_synced_seed_elo = seed
            await db.commit()
        log.info("set-seed by %s: %s base_seed -> %s (locked)", interaction.user.id, user.id, seed)
        await interaction.followup.send(
            f"🔒 Set {user.mention}'s base_seed to **{seed}** on all roles. "
            f"It's now locked against the weekly refresh, /sync-ranks, /reseed-all, and re-links. "
            f"Run `/refresh-seed user:{user.display_name}` to unlock and re-derive from rank.",
            ephemeral=True,
        )

    @app_commands.command(
        name="refresh-seed",
        description="(admin) Clear a player's manual seed and re-derive base_seed from their current Riot rank.",
    )
    @app_commands.describe(user="The player whose manual seed to clear and refresh")
    async def refresh_seed(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            player = await db.get(Player, user.id)
            if player is None:
                await interaction.followup.send(
                    f"{user.mention} has no player record — nothing to refresh.",
                    ephemeral=True,
                )
                return
            was_manual = player.manual_seed
            player.manual_seed = False
            if player.riot_puuid is None:
                # No linked Riot account to pull a rank from. Just unlock; the
                # existing base_seed stays until they link and get refreshed.
                await db.commit()
                await interaction.followup.send(
                    f"🔓 Cleared manual seed for {user.mention}, but they have no linked Riot "
                    f"account so base_seed can't be re-derived — it stays as-is until they /link.",
                    ephemeral=True,
                )
                return
            try:
                rows = await self._refresh_base_seed_for_player(db, player)
            except RiotAuthError:
                await interaction.followup.send("❌ Riot API key rejected.", ephemeral=True)
                return
            await db.commit()
        log.info("refresh-seed by %s: %s (was_manual=%s, rows=%s)", interaction.user.id, user.id, was_manual, rows)
        unlock = "🔓 Cleared manual seed for" if was_manual else "🔄 Refreshed"
        if rows:
            await interaction.followup.send(
                f"{unlock} {user.mention} and re-derived base_seed from their current rank "
                f"({rows} rating rows updated).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{unlock} {user.mention}, but no current or past rank was found — base_seed left "
                f"unchanged. They'll be picked up by the next refresh once a rank is available.",
                ephemeral=True,
            )

    @app_commands.command(
        name="clear-matches",
        description="(admin) Delete ALL matches and reset every player's inhouse elo. Destructive.",
    )
    @app_commands.describe(confirm="Type 'yes' to confirm — this cannot be undone")
    async def clear_matches(self, interaction: discord.Interaction, confirm: str = ""):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return

        # Show what will be destroyed and require explicit confirmation first.
        async with get_session() as db:
            match_count = (await db.execute(select(func.count(Match.id)))).scalar_one()
            rating_count = (await db.execute(select(func.count(Rating.discord_id)))).scalar_one()

        if confirm.lower() != "yes":
            await interaction.response.send_message(
                f"⚠️ This deletes **all {match_count} match(es)** (and their per-game stats and "
                f"pending proposals) and resets **{rating_count} rating row(s)** — "
                f"`inhouse_modifier` and `games_played` go to 0, so elo falls back to `base_seed`. "
                f"Player Riot links, base seeds, sessions, and signups are kept. "
                f"**This cannot be undone.** Run again with `confirm:yes` to proceed.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            # Delete in FK-dependency order: rows referencing matches first.
            await db.execute(delete(MatchPerformance))
            await db.execute(delete(GameStat))
            await db.execute(delete(ProposalSet))
            deleted = (await db.execute(delete(Match))).rowcount
            # Reset accumulated inhouse results; keep base_seed (rank-derived).
            reset = (await db.execute(
                update(Rating).values(
                    inhouse_modifier=0,
                    games_played=0,
                    elo=Rating.base_seed,
                    total_balance_diff=0.0,
                )
            )).rowcount
            await db.commit()

        log.info(
            "clear-matches by %s: deleted %s matches, reset %s ratings",
            interaction.user.id, deleted, reset,
        )
        await interaction.followup.send(
            f"🧹 Cleared **{deleted} match(es)** and reset **{reset} rating row(s)** "
            f"(elo back to base seed). Riot links and base seeds preserved.",
            ephemeral=True,
        )

    # ---------- internal: commit a match result + run elo update ----------

    async def _commit_result(
        self,
        match_id: int,
        team1_wins: int,
        team2_wins: int,
        screenshot_url: str | None,
        admin_id: int,
        parsed,
    ) -> None:
        """Series-score reporting (no subs): expand the series into per-game
        results over the match's fixed roster and delegate to _commit_games.

        This keeps /report-manual, pickup-series, and the Monday proposal flow
        working unchanged while elo is now applied per game. The multi-screenshot
        flow calls _commit_games directly with real per-game lineups (subs).
        """
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None or match.winner is not None:
                return  # idempotency guard (see _commit_games)
            team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}

            # Series-level OCR gives one scoreboard, not per-game KDA. Attach it to
            # the LAST game only (assume the end-of-series scoreboard) so per-game
            # KDA totals aren't multiplied across games.
            kdas: dict[int, tuple] = {}
            if parsed:
                ocr_by_riot_id = {p.riot_id: p for p in parsed.players if p.riot_id}
                for pid in list(team1.values()) + list(team2.values()):
                    player = await db.get(Player, pid)
                    if player and player.riot_game_name:
                        row = ocr_by_riot_id.get(f"{player.riot_game_name}#{player.riot_tag_line}")
                        if row:
                            kdas[pid] = (row.kills, row.deaths, row.assists)

        # team1 wins first, then team2 — order has only a minor effect on elo
        # (ratings evolve game to game) and is deterministic.
        games = [GameResult(winner=1, team1=team1, team2=team2) for _ in range(team1_wins)]
        games += [GameResult(winner=2, team1=team1, team2=team2) for _ in range(team2_wins)]
        if games and kdas:
            games[-1].kdas = kdas
        await self._commit_games(match_id, games, screenshot_url, admin_id)

    async def _commit_games(
        self,
        match_id: int,
        games: list["GameResult"],
        screenshot_url: str | None,
        admin_id: int,
    ) -> None:
        """Apply a match GAME BY GAME. Each game is its own elo event of ~±15 (see
        update_elo_team_game), so a 2-0 ≈ +30 and a 2-1 nets ≈ +15, and a sub earns
        elo only for the games they actually played. Writes per-game GameStat rows
        and one aggregate MatchPerformance per player (summed deltas) so /unreport
        reverses exactly. games_played counts ACTUAL games.
        """
        if not games:
            return
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None or match.winner is not None:
                return  # not found, or already reported (idempotency guard)

            cache: dict[tuple, Rating] = {}

            async def rget(pid: int, role: str) -> Rating:
                key = (pid, role)
                r = cache.get(key)
                if r is None:
                    r = await db.get(Rating, key)
                    if r is None:
                        r = Rating(discord_id=pid, role=role, elo=DEFAULT_ELO,
                                   base_seed=DEFAULT_ELO, inhouse_modifier=0, games_played=0)
                        db.add(r)
                    cache[key] = r
                return r

            deltas: dict[int, dict[str, int]] = defaultdict(lambda: {"role": 0, "inhouse": 0})
            played: dict[int, int] = defaultdict(int)  # discord_id -> games actually played
            first_balance: Optional[float] = None
            t1_wins = t2_wins = 0

            for gi, g in enumerate(games, start=1):
                if g.winner == 1:
                    t1_wins += 1
                else:
                    t2_wins += 1
                t1, t2 = g.team1, g.team2
                t1won = g.winner == 1

                t1_role = {role: await rget(pid, role) for role, pid in t1.items()}
                t2_role = {role: await rget(pid, role) for role, pid in t2.items()}
                t1_over = {pid: await rget(pid, INHOUSE_ROLE) for pid in t1.values()}
                t2_over = {pid: await rget(pid, INHOUSE_ROLE) for pid in t2.values()}

                # Pre-game snapshots so the two team loops stay symmetric.
                t1_ravg = average_elo([r.elo for r in t1_role.values()])
                t2_ravg = average_elo([r.elo for r in t2_role.values()])
                t1_oavg = average_elo([r.elo for r in t1_over.values()])
                t2_oavg = average_elo([r.elo for r in t2_over.values()])
                t1_rpre = {role: r.elo for role, r in t1_role.items()}
                t2_rpre = {role: r.elo for role, r in t2_role.items()}
                t1_opre = {pid: r.elo for pid, r in t1_over.items()}
                t2_opre = {pid: r.elo for pid, r in t2_over.items()}

                if first_balance is None:
                    first_balance = abs(sum(t1_rpre.values()) - sum(t2_rpre.values()))

                for role, pid in t1.items():
                    rr = t1_role[role]
                    _, rd = update_elo_team_game(
                        t1_ravg, t2_ravg, t1_rpre[role], t2_rpre.get(role, t2_ravg), won=t1won)
                    rr.inhouse_modifier += rd
                    rr.elo = rr.base_seed + rr.inhouse_modifier
                    rr.games_played += 1
                    ov = t1_over[pid]
                    _, od = update_elo_team_game(
                        t1_oavg, t2_oavg, t1_opre[pid], t2_opre.get(t2.get(role), t2_oavg), won=t1won)
                    ov.inhouse_modifier += od
                    ov.elo = ov.base_seed + ov.inhouse_modifier
                    ov.games_played += 1
                    deltas[pid]["role"] += rd
                    deltas[pid]["inhouse"] += od
                    played[pid] += 1

                for role, pid in t2.items():
                    rr = t2_role[role]
                    _, rd = update_elo_team_game(
                        t2_ravg, t1_ravg, t2_rpre[role], t1_rpre.get(role, t1_ravg), won=not t1won)
                    rr.inhouse_modifier += rd
                    rr.elo = rr.base_seed + rr.inhouse_modifier
                    rr.games_played += 1
                    ov = t2_over[pid]
                    _, od = update_elo_team_game(
                        t2_oavg, t1_oavg, t2_opre[pid], t1_opre.get(t1.get(role), t1_oavg), won=not t1won)
                    ov.inhouse_modifier += od
                    ov.elo = ov.base_seed + ov.inhouse_modifier
                    ov.games_played += 1
                    deltas[pid]["role"] += rd
                    deltas[pid]["inhouse"] += od
                    played[pid] += 1

                # Per-game stat rows (champions normalized to canonical names).
                for roster, won in ((t1, t1won), (t2, not t1won)):
                    for role, pid in roster.items():
                        k = g.kdas.get(pid, (None, None, None))
                        champ = g.champions.get(pid)
                        db.add(GameStat(
                            match_id=match.id, game_no=gi, discord_id=pid, role=role,
                            champion=(resolve_champion(champ) or champ) if champ else None,
                            kills=k[0], deaths=k[1], assists=k[2], won=won,
                        ))

            if match.predicted_balance is None:
                match.predicted_balance = float(first_balance or 0.0)
            balance = match.predicted_balance or 0.0
            series_winner = 1 if t1_wins > t2_wins else 2

            role_of: dict[int, str] = {}
            team_of: dict[int, int] = {}
            for g in games:
                for role, pid in g.team1.items():
                    role_of[pid], team_of[pid] = role, 1
                for role, pid in g.team2.items():
                    role_of[pid], team_of[pid] = role, 2

            for pid, cnt in played.items():
                ov = cache[(pid, INHOUSE_ROLE)]
                ov.total_balance_diff += balance * cnt
                db.add(MatchPerformance(
                    match_id=match.id, discord_id=pid, role=role_of[pid],
                    kills=None, deaths=None, assists=None,
                    won=(team_of[pid] == series_winner),
                    role_elo_delta=deltas[pid]["role"], inhouse_elo_delta=deltas[pid]["inhouse"],
                ))

            match.winner = series_winner
            match.team1_wins = t1_wins
            match.team2_wins = t2_wins
            match.reported_by = admin_id
            match.reported_at = datetime.utcnow()
            match.screenshot_url = screenshot_url
            if match.session_id is not None:
                session = await db.get(InhouseSession, match.session_id)
                if session and session.status == "matched":
                    session.status = "completed"

            await db.commit()

    # ========== Test/dev helpers ==========

    @app_commands.command(
        name="test-fake-signups",
        description="(admin) Fill the active session with fake players for testing matchmaking.",
    )
    @app_commands.describe(
        count="How many fake players to add (default 10)",
        clear_first="If true, wipe existing signups from this session before adding fakes",
    )
    async def test_fake_signups(
        self,
        interaction: discord.Interaction,
        count: int = 10,
        clear_first: bool = True,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        if count < 1 or count > 30:
            await interaction.response.send_message("Count must be 1-30.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        import random
        # Stable per-fake skills so /match-preview shows the same proposals on re-runs
        rng = random.Random(42)

        # Find the most recent recruiting session
        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession).where(InhouseSession.status == "recruiting")
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.followup.send(
                    "No active recruiting session. Run `/recruit-now` first.",
                    ephemeral=True,
                )
                return

            if clear_first:
                # Delete all existing signups for this session
                existing = (await db.execute(
                    select(Signup).where(Signup.session_id == session.id)
                )).scalars().all()
                for s in existing:
                    await db.delete(s)
                # Also delete the fake players we created previously, identified by
                # discord_id range below 1000 (real Discord IDs are ~18 digits)
                fake_players = (await db.execute(
                    select(Player).where(Player.discord_id < 10_000)
                )).scalars().all()
                for p in fake_players:
                    await db.delete(p)
                await db.commit()

            # Generate roles with realistic distribution: a few specialists per role,
            # plus some FILLs. We pick from this template and shuffle.
            role_templates = [
                ["TOP"],
                ["TOP"],
                ["JUNGLE"],
                ["JUNGLE"],
                ["MID"],
                ["MID"],
                ["BOT"],
                ["BOT"],
                ["SUPPORT"],
                ["SUPPORT"],
                ["FILL"],
                ["FILL"],
                ["TOP", "MID"],     # multi-role
                ["JUNGLE", "SUPPORT"],
                ["BOT"],
            ]
            rng.shuffle(role_templates)

            # Realistic skill spread: mostly mid-range with a few outliers
            tiers_pool = [
                ("SILVER", "II"), ("GOLD", "IV"), ("GOLD", "II"),
                ("PLATINUM", "IV"), ("PLATINUM", "II"), ("EMERALD", "III"),
                ("DIAMOND", "IV"), ("BRONZE", "I"), ("GOLD", "I"),
                ("PLATINUM", "I"),
            ]

            created = []
            for i in range(count):
                fake_id = 1 + i  # use 1, 2, 3, ... — won't collide with real Discord IDs
                tier, division = tiers_pool[i % len(tiers_pool)]
                seed_elo = seed_from_rank(tier, division)
                # Per-fake jitter so equal-tier players differ slightly
                jitter = int(rng.uniform(-50, 50))

                player = await db.get(Player, fake_id)
                if player is None:
                    player = Player(
                        discord_id=fake_id,
                        riot_game_name=f"FakePlayer{i+1}",
                        riot_tag_line="TEST",
                        solo_tier=tier,
                        solo_rank=division,
                        link_status="approved",
                    )
                    db.add(player)

                # Per-role ratings + INHOUSE
                for role in [*ROLES, INHOUSE_ROLE]:
                    rating = await db.get(Rating, (fake_id, role))
                    if rating is None:
                        seed_with_jitter = seed_elo + jitter
                        db.add(Rating(
                            discord_id=fake_id,
                            role=role,
                            elo=seed_with_jitter,
                            base_seed=seed_with_jitter,
                            inhouse_modifier=0,
                            games_played=0,
                        ))

                # Signup with a random role template
                roles = role_templates[i % len(role_templates)]
                db.add(Signup(
                    session_id=session.id,
                    discord_id=fake_id,
                    status="playing",
                    roles=",".join(roles),
                    signed_up_at=datetime.utcnow(),
                ))
                created.append((fake_id, tier, division, roles))

            await db.commit()

        summary = "\n".join(
            f"`{pid:>3}` **FakePlayer{pid}** · {tier} {div} · {','.join(roles)}"
            for pid, tier, div, roles in created
        )
        await interaction.followup.send(
            f"✅ Created {count} fake playing signups for session {session.id}.\n\n"
            f"{summary}\n\n"
            f"Next: run `/match-preview` to see the top-3 proposals, or wait for "
            f"the Monday 9:30 PM scheduler to fire.",
            ephemeral=True,
        )

    @app_commands.command(
        name="test-trigger-close",
        description="(admin) Manually trigger the Monday close-and-DM-options job for testing.",
    )
    async def test_trigger_close(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        from bot.cogs.recruitment import RecruitmentCog
        cog = self.bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            await interaction.followup.send("Recruitment cog not loaded.", ephemeral=True)
            return

        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession).where(InhouseSession.status == "recruiting")
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.followup.send("No active recruiting session.", ephemeral=True)
                return

        try:
            await cog.close_signups_and_match(session.id)
            await interaction.followup.send(
                f"✅ Triggered close on session {session.id}. Check your DMs for the 3 options.",
                ephemeral=True,
            )
        except Exception as e:
            log.exception("test-trigger-close failed")
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @app_commands.command(
        name="manual-match",
        description="(admin) Override the matchmaker — paste in a roster you made yourself.",
    )
    @app_commands.describe(
        session_id="Session ID (from the recruitment post footer). Takes priority over game_date.",
        game_date="Game date YYYY-MM-DD (must be a Thursday). Alternative to session_id.",
        create_channels="Also create the per-team roles + private channels for this match.",
    )
    async def manual_match(
        self,
        interaction: discord.Interaction,
        session_id: Optional[int] = None,
        game_date: Optional[str] = None,
        create_channels: bool = False,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return

        async with get_session() as db:
            if session_id is not None:
                session = await db.get(InhouseSession, session_id)
                if session is None:
                    await interaction.response.send_message(
                        f"Session #{session_id} not found.", ephemeral=True
                    )
                    return
            elif game_date:
                # Parse and validate date
                try:
                    target_date = datetime.strptime(game_date, "%Y-%m-%d").date()
                except ValueError:
                    await interaction.response.send_message(
                        "Date must be YYYY-MM-DD format (e.g. `2026-05-14`).",
                        ephemeral=True,
                    )
                    return
                if target_date.weekday() != 3:  # Thursday = 3
                    await interaction.response.send_message(
                        "Date must be a Thursday.",
                        ephemeral=True,
                    )
                    return
                session = (await db.execute(
                    select(InhouseSession).where(InhouseSession.game_date == target_date)
                )).scalars().first()
                if session is None:
                    await interaction.response.send_message(
                        f"No session for {game_date}. Run `/recruit-now game_date:{game_date}` first.",
                        ephemeral=True,
                    )
                    return
            else:
                # Fallback: soonest active session (recruiting, closed, or matched)
                session = (await db.execute(
                    select(InhouseSession)
                    .where(InhouseSession.status.in_(["recruiting", "closed", "matched"]))
                    .order_by(InhouseSession.game_date.asc())
                )).scalars().first()
                if session is None:
                    await interaction.response.send_message(
                        "No active session. Use `/recruit-now` first or specify `game_date:`.",
                        ephemeral=True,
                    )
                    return

        await interaction.response.send_modal(
            ManualMatchModal(
                session.id,
                create_channels=create_channels,
                channels_builder=self._build_match_channels,
            )
        )

    @app_commands.command(
        name="pickup-series",
        description="(admin) Report a pickup series — no recruitment session needed.",
    )
    async def pickup_series(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.send_modal(
            PickupSeriesModal(commit_callback=self._commit_result)
        )

    @app_commands.command(
        name="match-roster",
        description="(admin) Print a match roster in copy/paste format to edit and re-submit.",
    )
    @app_commands.describe(match_id="Match to export (defaults to the most recent match)")
    async def match_roster(
        self, interaction: discord.Interaction, match_id: Optional[int] = None
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        async with get_session() as db:
            if match_id is None:
                match = (await db.execute(
                    select(Match).order_by(Match.id.desc())
                )).scalars().first()
            else:
                match = await db.get(Match, match_id)
            if match is None:
                msg = "No matches exist yet." if match_id is None else f"Match {match_id} not found."
                await interaction.response.send_message(msg, ephemeral=True)
                return
            team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}
            ids = list(team1.values()) + list(team2.values())
            players = {
                p.discord_id: p for p in (await db.execute(
                    select(Player).where(Player.discord_id.in_(ids))
                )).scalars().all()
            }

        def who(team: dict[str, int]) -> str:
            parts = []
            for r in ROLES:
                if r not in team:
                    continue
                p = players.get(team[r])
                name = p.riot_game_name if p and p.riot_game_name else f"<@{team[r]}>"
                parts.append(f"{r} {name}")
            return ", ".join(parts)

        reported = match.winner is not None
        score = f" · reported {match.team1_wins}-{match.team2_wins}" if reported else ""
        edit_hint = (
            "Run `/unreport` to edit it."
            if reported
            else "Hit **Edit teams** below to change it in place, or copy the block into "
                 "`/manual-match` / `/pickup-series`."
        )
        content = (
            f"🧩 **Match {match.id}** roster{score}. {edit_hint}\n"
            f"```\n{format_roster_block(team1, team2)}\n```\n"
            f"**{TEAM_NAMES[1]}** — {who(team1)}\n"
            f"**{TEAM_NAMES[2]}** — {who(team2)}"
        )
        kwargs = dict(ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        if not reported:
            kwargs["view"] = EditRosterView(match.id)
        await interaction.response.send_message(content, **kwargs)

    @app_commands.command(
        name="matches",
        description="(admin) List recent matches with their IDs and report status.",
    )
    @app_commands.describe(session_id="Only show matches for this session (optional).")
    async def matches(self, interaction: discord.Interaction, session_id: Optional[int] = None):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            stmt = select(Match).order_by(Match.id.desc())
            if session_id is not None:
                stmt = stmt.where(Match.session_id == session_id)
            else:
                stmt = stmt.limit(20)
            rows = (await db.execute(stmt)).scalars().all()
            if not rows:
                where = f" for Session #{session_id}" if session_id is not None else ""
                await interaction.followup.send(f"No matches found{where}.", ephemeral=True)
                return
            sess_ids = {m.session_id for m in rows if m.session_id is not None}
            sessions = {}
            if sess_ids:
                sessions = {
                    s.id: s for s in (await db.execute(
                        select(InhouseSession).where(InhouseSession.id.in_(sess_ids))
                    )).scalars().all()
                }

        lines = []
        for m in rows:
            if m.session_id is not None and m.session_id in sessions:
                where = f"Thu {sessions[m.session_id].game_date.strftime('%b %d')} · Session #{m.session_id}"
            elif m.session_id is not None:
                where = f"Session #{m.session_id}"
            else:
                where = "pickup"
            status = (
                f"{m.team1_wins}-{m.team2_wins} ({TEAM_NAMES[m.winner]} won)"
                if m.winner is not None else "⏳ unreported"
            )
            lines.append(f"`#{m.id}` · {where} · {status}")

        title = (
            f"🗒️ Matches for Session #{session_id}" if session_id is not None
            else f"🗒️ Recent matches (last {len(rows)})"
        )
        chunk = title
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await interaction.followup.send(chunk, ephemeral=True)
                chunk = ""
            chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(
        name="match-elos",
        description="(admin) Show each team's INHOUSE elo for a match (what they were entering it).",
    )
    @app_commands.describe(match_id="Match ID (from /matches or the teams post).")
    async def match_elos(self, interaction: discord.Interaction, match_id: int):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                return
            team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}
            ids = list(team1.values()) + list(team2.values())
            inhouse = {
                r.discord_id: r for r in (await db.execute(
                    select(Rating).where(Rating.role == INHOUSE_ROLE, Rating.discord_id.in_(ids))
                )).scalars().all()
            }
            players = {
                p.discord_id: p for p in (await db.execute(
                    select(Player).where(Player.discord_id.in_(ids))
                )).scalars().all()
            }
            perfs = {
                p.discord_id: p for p in (await db.execute(
                    select(MatchPerformance).where(MatchPerformance.match_id == match_id)
                )).scalars().all()
            }
            # INHOUSE deltas these players earned in matches reported AFTER this one,
            # so we can subtract back to their elo *entering* this match.
            later: dict[int, int] = {}
            if match.reported_at is not None:
                rows = (await db.execute(
                    select(MatchPerformance.discord_id, MatchPerformance.inhouse_elo_delta)
                    .join(Match, Match.id == MatchPerformance.match_id)
                    .where(
                        MatchPerformance.discord_id.in_(ids),
                        Match.reported_at > match.reported_at,
                    )
                )).all()
                for did, d in rows:
                    later[did] = later.get(did, 0) + (d or 0)

        reported = match.winner is not None

        def who(did: int) -> str:
            p = players.get(did)
            return p.riot_game_name if p and p.riot_game_name else f"<@{did}>"

        def entering_elo(did: int) -> int:
            cur = inhouse[did].elo if did in inhouse else DEFAULT_ELO
            if not reported:
                return cur
            this_delta = perfs[did].inhouse_elo_delta if did in perfs else 0
            return cur - (this_delta or 0) - later.get(did, 0)

        def team_block(team: dict[str, int]) -> tuple[int, list[str]]:
            roles = [r for r in ROLES if r in team]
            lines, total = [], 0
            for r in roles:
                did = team[r]
                e = entering_elo(did)
                total += e
                dtxt = f"  ({perfs[did].inhouse_elo_delta:+d})" if (reported and did in perfs) else ""
                lines.append(f"`{e:>4}` {r:<7} {who(did)}{dtxt}")
            avg = round(total / len(roles)) if roles else DEFAULT_ELO
            return avg, lines

        a1, l1 = team_block(team1)
        a2, l2 = team_block(team2)
        label = "INHOUSE elo entering the match" if reported else "current INHOUSE elo (not yet reported)"
        head = f"📊 **Match {match_id}** — {label}"
        if reported:
            head += f" · result {match.team1_wins}-{match.team2_wins}"
        body = (
            f"{head}\n\n🔵 **{TEAM_NAMES[1]}** — avg **{a1}**\n" + "\n".join(l1)
            + f"\n\n🔴 **{TEAM_NAMES[2]}** — avg **{a2}**\n" + "\n".join(l2)
            + f"\n\nTeam elo gap: **{abs(a1 - a2)}**"
        )
        if reported and any(did in perfs for did in ids):
            body += "\n_( ± = what this match applied to each player. )_"
        await interaction.followup.send(
            body, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
