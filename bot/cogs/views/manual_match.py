"""Roster-entry modals: /manual-match (and the "Edit teams" button on
/match-roster), /pickup-series, and /resolve-names."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import discord
from sqlalchemy import select

from bot.config import ROLES, format_team_lines
from bot.db.models import Match, Player, Session as InhouseSession
from bot.db.session import get_session
from bot.services.elo import parse_series_score
from bot.services.names import resolve_name_block
from bot.services.pins import pin_current
from bot.services.ratings_lock import ratings_lock
from bot.services.rosters import (
    TEAM_NAMES,
    ManualMatchParseError,
    format_roster_block,
    parse_manual_match,
)

log = logging.getLogger(__name__)



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
                teams_msg = await channel.send(content=f"🔒 Teams ({verb}):", embed=embed)
                await pin_current(teams_msg, interaction.client.user)

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


# A best-of-3 takes well over half an hour, so the same ten players in the same
# roles being recorded again inside this window is a retry, not a second series.
PICKUP_DUPLICATE_WINDOW = timedelta(minutes=30)


async def _recent_duplicate_pickup(db, team1: dict[str, int], team2: dict[str, int]) -> Optional[Match]:
    """A reported session-less match with exactly these rosters, recorded within
    PICKUP_DUPLICATE_WINDOW — i.e. this /pickup-series is a repeat submission."""
    cutoff = datetime.utcnow() - PICKUP_DUPLICATE_WINDOW
    recent = (await db.execute(
        select(Match).where(
            Match.session_id.is_(None), Match.winner.isnot(None), Match.reported_at >= cutoff,
        ).order_by(Match.id.desc())
    )).scalars().all()
    want = ({k: int(v) for k, v in team1.items()}, {k: int(v) for k, v in team2.items()})
    for m in recent:
        try:
            have = ({k: int(v) for k, v in json.loads(m.team1_json).items()},
                    {k: int(v) for k, v in json.loads(m.team2_json).items()})
        except (ValueError, TypeError, AttributeError):
            continue
        if have == want:
            return m
    return None


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
                f'❌ Invalid series score "{self.series_score.value}". Use "2-0", "2-1", "1-2", "0-2" '
                f'(or "1-0" / "0-1" for a single game).',
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

        # Create the match AND apply its result in ONE transaction, under the
        # rating lock: a failure leaves nothing behind (no orphan unreported match),
        # and a retried/double-submitted modal sees the first one's row and stops.
        problem: Optional[str] = None
        match_id = 0
        async with ratings_lock():
            async with get_session() as db:
                # Verify everyone is linked
                unlinked = []
                for pid in all_ids:
                    player = await db.get(Player, pid)
                    if player is None or not player.riot_puuid or player.link_status != "approved":
                        unlinked.append(pid)
                dup = None if unlinked else await _recent_duplicate_pickup(db, team1, team2)
                if unlinked:
                    mentions = " ".join(f"<@{i}>" for i in unlinked)
                    problem = f"❌ Not linked / not approved: {mentions}"
                elif dup is not None:
                    problem = (
                        f"⚠️ These exact rosters were already recorded as **match {dup.id}** "
                        f"({dup.team1_wins}-{dup.team2_wins}) a few minutes ago, so this looks like "
                        f"a repeat submission — nothing was recorded. If match {dup.id} is wrong, "
                        f"`/unreport {dup.id}` and report it again."
                    )
                else:
                    # Create a session-less Match row
                    match = Match(
                        session_id=None,
                        team1_json=json.dumps(team1),
                        team2_json=json.dumps(team2),
                        predicted_balance=None,
                    )
                    db.add(match)
                    await db.flush()
                    match_id = match.id
                    # Apply elo updates via the cog's commit helper, same transaction
                    status = await self._commit(
                        match_id, team1_wins, team2_wins, None, interaction.user.id, None, db=db)
                    if status == "ok":
                        await db.commit()
                    else:
                        await db.rollback()
                        problem = f"❌ Pickup series was NOT recorded ({status})."
        if problem:
            await interaction.followup.send(
                problem, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

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
