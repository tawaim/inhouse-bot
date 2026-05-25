"""Admin commands: result reporting, channel config, rank syncing."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select, update

from bot.config import Config, ROLES
from bot.db.models import GuildConfig, Match, MatchPerformance, Player, ProposalSet, Rating, Session as InhouseSession, Signup
from bot.db.session import get_session
from bot.services.elo import (
    DEFAULT_ELO,
    INHOUSE_ROLE,
    average_elo,
    parse_series_score,
    seed_from_past_season,
    seed_from_rank,
    update_elo,
    update_elo_series,
)
from bot.services.ocr import parse_screenshot
from bot.services.opgg_client import OpggClient
from bot.services.riot_client import RiotAuthError, RiotClient

log = logging.getLogger(__name__)


# =============================================================================
# Manual match parsing
# =============================================================================

# Discord mention format: <@123456789> or <@!123456789> (the ! is for nicknames)
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(.+?)\s*$")


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
    ):
        super().__init__()
        self.session_id = session_id
        self.edit_match_id = edit_match_id  # when set, UPDATE this match instead of creating one
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
        from bot.config import ROLE_EMOJIS
        title = "🏆 Updated Teams" if verb == "updated" else "🏆 Manual Teams"
        if game_date:
            title += f" for Thursday {game_date.strftime('%b %d')}"
        embed = discord.Embed(title=title, color=discord.Color.green())
        t1 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team1[r]}>" for r in ROLES)
        t2 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team2[r]}>" for r in ROLES)
        embed.add_field(name="🔵 Team 1 (Blue)", value=t1, inline=True)
        embed.add_field(name="🔴 Team 2 (Red)", value=t2, inline=True)
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
        await interaction.followup.send(
            f"✅ Match {match_id} {verb} with manual roster.{posted}\n"
            f"Report the result with `/report` or `/report-manual` when games are done.",
            ephemeral=True,
        )


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
        winner_label = "Team 1" if team1_wins > team2_wins else "Team 2"
        from bot.config import ROLE_EMOJIS
        embed = discord.Embed(
            title=f"🎮 Pickup Series Result: {team1_wins}-{team2_wins}",
            description=f"**{winner_label} wins.** Elo updated for all 10 players.",
            color=discord.Color.green(),
        )
        t1 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team1[r]}>" for r in ROLES)
        t2 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team2[r]}>" for r in ROLES)
        embed.add_field(name="🔵 Team 1 (Blue)", value=t1, inline=True)
        embed.add_field(name="🔴 Team 2 (Red)", value=t2, inline=True)
        embed.set_footer(text=f"Match {match_id} · Pickup by {interaction.user.display_name}")

        # Post publicly in the same channel where the command was run
        if interaction.channel:
            await interaction.channel.send(embed=embed)

        await interaction.followup.send(
            f"✅ Pickup series recorded as match {match_id}.",
            ephemeral=True,
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

    @app_commands.command(name="report", description="(admin) Report best-of-3 series outcome with screenshot.")
    @app_commands.describe(
        match_id="The match ID from the teams post",
        series_score='Series score from team1 perspective: "2-0", "2-1", "1-2", or "0-2"',
        screenshot="End-of-game screenshot",
    )
    async def report(
        self,
        interaction: discord.Interaction,
        match_id: int,
        series_score: str,
        screenshot: discord.Attachment,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        team1_wins, team2_wins = parse_series_score(series_score)
        if team1_wins < 0:
            await interaction.response.send_message(
                'Invalid series score. Use "2-0", "2-1", "1-2", or "0-2".',
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                return
            if match.winner is not None:
                await interaction.followup.send(
                    f"Match {match_id} already reported. Use /unreport first if this is a correction.",
                    ephemeral=True,
                )
                return

        # OCR for KDA enrichment (winner is admin-specified, not OCR-determined)
        image_bytes = await screenshot.read()
        parsed = parse_screenshot(image_bytes)

        # Show admin a confirmation embed before committing
        winner_label = "team1" if team1_wins > team2_wins else "team2"
        embed = discord.Embed(
            title=f"Confirm Match {match_id} Report",
            color=discord.Color.orange(),
            description=f"**Series:** {team1_wins}-{team2_wins} ({winner_label} wins)\n**Screenshot:** {screenshot.filename}",
        )
        if parsed.notes:
            embed.add_field(name="OCR Notes", value="\n".join(parsed.notes), inline=False)
        if parsed.players:
            lines = [
                f"`{p.kills}/{p.deaths}/{p.assists}` {p.riot_id or '?'}"
                for p in parsed.players[:12]
            ]
            embed.add_field(name="Detected rows", value="\n".join(lines) or "none", inline=False)
        embed.set_footer(text=f"OCR confidence: {parsed.confidence:.0%}. Click ✅ to commit, ❌ to cancel.")

        confirm_msg = await interaction.followup.send(embed=embed)
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")

        def check(reaction: discord.Reaction, user: discord.User):
            return (
                user.id == interaction.user.id
                and reaction.message.id == confirm_msg.id
                and str(reaction.emoji) in ("✅", "❌")
            )

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", check=check, timeout=120.0)
        except Exception:
            await confirm_msg.edit(content="⏱️ Confirmation timed out.", embed=None)
            return

        if str(reaction.emoji) == "❌":
            await confirm_msg.edit(content="❌ Cancelled.", embed=None)
            return

        # Commit: update match, write performances, run elo update
        await self._commit_result(match_id, team1_wins, team2_wins, screenshot.url, interaction.user.id, parsed)
        await confirm_msg.edit(
            content=f"✅ Match {match_id} recorded. Series {team1_wins}-{team2_wins} ({winner_label} wins). Elo updated.",
            embed=None,
        )

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
        winner_team = "team1" if team1_wins > team2_wins else "team2"
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

            # Reverse each player's stored deltas on their role + INHOUSE ratings,
            # and decrement games_played. This exactly restores pre-report state.
            for perf in perfs:
                role_rating = await db.get(Rating, (perf.discord_id, perf.role))
                if role_rating is not None:
                    role_rating.inhouse_modifier -= perf.role_elo_delta or 0
                    role_rating.games_played = max(0, role_rating.games_played - 1)
                    role_rating.elo = role_rating.base_seed + role_rating.inhouse_modifier
                overall = await db.get(Rating, (perf.discord_id, INHOUSE_ROLE))
                if overall is not None:
                    overall.inhouse_modifier -= perf.inhouse_elo_delta or 0
                    overall.games_played = max(0, overall.games_played - 1)
                    overall.elo = overall.base_seed + overall.inhouse_modifier
                await db.delete(perf)

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
            f"{result['errors']} errors",
            ephemeral=True,
        )

    async def _refresh_all_base_seeds(self) -> dict:
        """Refresh base_seed for every linked player from current Riot rank.
        Used by both /sync-ranks (manual) and the Monday close job.
        Returns counts dict for logging.
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
            for player in players:
                try:
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
                    updated += 1

                    if new_seed is None:
                        continue

                    player.last_synced_seed_elo = new_seed
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
                            rows_updated += 1
                        else:
                            r.base_seed = new_seed
                            r.elo = r.base_seed + r.inhouse_modifier
                            rows_updated += 1
                except RiotAuthError:
                    raise
                except Exception:
                    log.exception("Failed to sync %s", player.discord_id)
                    errors += 1
            await db.commit()
        return {"updated": updated, "rows_updated": rows_updated, "errors": errors}

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
            f"inhouse_modifier preserved · "
            f"{result['errors']} errors",
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
            await db.execute(delete(ProposalSet))
            deleted = (await db.execute(delete(Match))).rowcount
            # Reset accumulated inhouse results; keep base_seed (rank-derived).
            reset = (await db.execute(
                update(Rating).values(
                    inhouse_modifier=0,
                    games_played=0,
                    elo=Rating.base_seed,
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
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                return
            if match.winner is not None:
                # Already reported. Idempotent guard: the /report flow checks this
                # in a separate session before a 120s reaction wait, so without
                # re-checking here a second /report (or a racing /report-manual)
                # would double-apply elo. Bail out instead.
                return
            team1: dict[str, int] = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2: dict[str, int] = {k: int(v) for k, v in json.loads(match.team2_json).items()}

            # Per-player elo deltas applied this match, so /unreport can reverse them.
            deltas: dict[int, dict[str, int]] = {}

            # Helper: get-or-create a rating row. New rows start at DEFAULT_ELO
            # base_seed with 0 modifier (someone who's never linked but appears
            # in a manual match — edge case).
            async def get_or_create_rating(pid: int, role: str) -> Rating:
                r = await db.get(Rating, (pid, role))
                if r is None:
                    r = Rating(
                        discord_id=pid, role=role,
                        elo=DEFAULT_ELO,
                        base_seed=DEFAULT_ELO,
                        inhouse_modifier=0,
                        games_played=0,
                    )
                    db.add(r)
                return r

            # Fetch all ratings we'll need: per-role for players + INHOUSE for all 10
            t1_role_ratings: dict[str, Rating] = {}
            t2_role_ratings: dict[str, Rating] = {}
            t1_overall: dict[int, Rating] = {}
            t2_overall: dict[int, Rating] = {}
            for role, pid in team1.items():
                t1_role_ratings[role] = await get_or_create_rating(pid, role)
                t1_overall[pid] = await get_or_create_rating(pid, INHOUSE_ROLE)
            for role, pid in team2.items():
                t2_role_ratings[role] = await get_or_create_rating(pid, role)
                t2_overall[pid] = await get_or_create_rating(pid, INHOUSE_ROLE)

            # Compute opposing-team averages BEFORE applying any changes,
            # so updates use pre-match values consistently. Use displayed elo
            # (base_seed + inhouse_modifier) for the matchup math.
            t1_role_avg = average_elo([r.elo for r in t1_role_ratings.values()])
            t2_role_avg = average_elo([r.elo for r in t2_role_ratings.values()])
            t1_overall_avg = average_elo([r.elo for r in t1_overall.values()])
            t2_overall_avg = average_elo([r.elo for r in t2_overall.values()])

            team1_won = team1_wins > team2_wins  # for the perf-row 'won' field

            # Update ratings: chess-elo delta is added to inhouse_modifier
            # (NOT base_seed). Then `elo` is recomputed = base_seed + modifier.
            # base_seed is rank-derived and only changed by sync.
            #
            # Series scoring: 2-0 = 1.0 actual, 2-1 = 0.667 actual, etc.
            # Each player gets ONE elo update for the entire series.
            for role, role_rating in t1_role_ratings.items():
                pid = team1[role]
                _, role_delta = update_elo_series(
                    role_rating.elo, t2_role_avg,
                    player_team_wins=team1_wins, opponent_team_wins=team2_wins,
                    games_played=role_rating.games_played,
                )
                role_rating.inhouse_modifier += role_delta
                role_rating.elo = role_rating.base_seed + role_rating.inhouse_modifier
                role_rating.games_played += 1

                overall = t1_overall[pid]
                _, overall_delta = update_elo_series(
                    overall.elo, t2_overall_avg,
                    player_team_wins=team1_wins, opponent_team_wins=team2_wins,
                    games_played=overall.games_played,
                )
                overall.inhouse_modifier += overall_delta
                overall.elo = overall.base_seed + overall.inhouse_modifier
                overall.games_played += 1
                deltas[pid] = {"role": role_delta, "inhouse": overall_delta}

            for role, role_rating in t2_role_ratings.items():
                pid = team2[role]
                _, role_delta = update_elo_series(
                    role_rating.elo, t1_role_avg,
                    player_team_wins=team2_wins, opponent_team_wins=team1_wins,
                    games_played=role_rating.games_played,
                )
                role_rating.inhouse_modifier += role_delta
                role_rating.elo = role_rating.base_seed + role_rating.inhouse_modifier
                role_rating.games_played += 1

                overall = t2_overall[pid]
                _, overall_delta = update_elo_series(
                    overall.elo, t1_overall_avg,
                    player_team_wins=team2_wins, opponent_team_wins=team1_wins,
                    games_played=overall.games_played,
                )
                overall.inhouse_modifier += overall_delta
                overall.elo = overall.base_seed + overall.inhouse_modifier
                overall.games_played += 1
                deltas[pid] = {"role": role_delta, "inhouse": overall_delta}

            # Write per-player performance rows. KDA from OCR if available.
            ocr_by_riot_id = {}
            if parsed:
                ocr_by_riot_id = {p.riot_id: p for p in parsed.players if p.riot_id}

            async def get_kda(pid: int):
                if not parsed:
                    return None, None, None
                player = await db.get(Player, pid)
                if not player or not player.riot_game_name:
                    return None, None, None
                key = f"{player.riot_game_name}#{player.riot_tag_line}"
                row = ocr_by_riot_id.get(key)
                if not row:
                    return None, None, None
                return row.kills, row.deaths, row.assists

            for role, pid in team1.items():
                k, d, a = await get_kda(pid)
                pd = deltas.get(pid, {"role": 0, "inhouse": 0})
                db.add(MatchPerformance(
                    match_id=match.id, discord_id=pid, role=role,
                    kills=k, deaths=d, assists=a, won=team1_won,
                    role_elo_delta=pd["role"], inhouse_elo_delta=pd["inhouse"],
                ))
            for role, pid in team2.items():
                k, d, a = await get_kda(pid)
                pd = deltas.get(pid, {"role": 0, "inhouse": 0})
                db.add(MatchPerformance(
                    match_id=match.id, discord_id=pid, role=role,
                    kills=k, deaths=d, assists=a, won=not team1_won,
                    role_elo_delta=pd["role"], inhouse_elo_delta=pd["inhouse"],
                ))

            match.winner = 1 if team1_wins > team2_wins else 2
            match.team1_wins = team1_wins
            match.team2_wins = team2_wins
            match.reported_by = admin_id
            match.reported_at = datetime.utcnow()
            match.screenshot_url = screenshot_url

            # If this match was tied to a session, mark the session completed.
            # Pickup matches have no session, so skip this step.
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
    )
    async def manual_match(
        self,
        interaction: discord.Interaction,
        session_id: Optional[int] = None,
        game_date: Optional[str] = None,
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

        await interaction.response.send_modal(ManualMatchModal(session.id))

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
            f"**Team 1** — {who(team1)}\n"
            f"**Team 2** — {who(team2)}"
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
                f"{m.team1_wins}-{m.team2_wins} (Team {m.winner} won)"
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
            f"{head}\n\n🔵 **Team 1** — avg **{a1}**\n" + "\n".join(l1)
            + f"\n\n🔴 **Team 2** — avg **{a2}**\n" + "\n".join(l2)
            + f"\n\nTeam elo gap: **{abs(a1 - a2)}**"
        )
        if reported and any(did in perfs for did in ids):
            body += "\n_( ± = what this match applied to each player. )_"
        await interaction.followup.send(
            body, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
