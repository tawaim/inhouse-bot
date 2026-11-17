"""The owner's team-option DM: the pick buttons, and committing the picked
option as the session's match."""
from __future__ import annotations

import json
import logging
from datetime import datetime

import discord
from sqlalchemy import select

from bot.config import format_team_lines
from bot.db.models import Match, ProposalSet, Rating, Session as InhouseSession
from bot.db.session import get_session
from bot.services.elo import DEFAULT_ELO
from bot.services.rosters import TEAM_NAMES

log = logging.getLogger(__name__)



def _team_lines(
    by_role: dict[str, int],
    emoji: bool = True,
    name_map: dict[int, str] | None = None,
    elo_map: dict[int, float] | None = None,
) -> str:
    return format_team_lines(by_role, emoji=emoji, name_map=name_map, elo_map=elo_map)


# =============================================================================
# Owner choice view (for picking among the top match options)
# =============================================================================

# How many team options the matchmaker offers the owner. Ten is the most that
# fits one DM: Discord allows 10 embeds per message (one per option).
OPTION_COUNT = 10


def option_label(index: int) -> str:
    """0 -> 'A', 1 -> 'B', ..."""
    return chr(ord("A") + index)


def option_color(index: int) -> discord.Color:
    """Embed colour for an option: the first three keep their old colours."""
    first = [discord.Color.green(), discord.Color.blue(), discord.Color.purple()]
    return first[index] if index < len(first) else discord.Color.light_grey()


class _PickButton(discord.ui.Button):
    def __init__(self, index: int):
        super().__init__(
            label=f"Pick {option_label(index)}",
            style=discord.ButtonStyle.success if index == 0 else discord.ButtonStyle.secondary,
            # a/b/c match the IDs on option DMs sent before there were ten.
            custom_id=f"proposal_pick_{option_label(index).lower()}",
            row=index // 5,
        )
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        await _handle_proposal_pick(interaction, self.index)


class ProposalChoiceView(discord.ui.View):
    """Persistent view DM'd to the owner with one button per option (A, B, ...).
    Custom IDs are stable across restarts. The handlers re-fetch state from DB.

    With no arguments it carries all OPTION_COUNT buttons, which is what the
    post-restart re-registration needs.
    """

    def __init__(self, proposal_set_id: int = 0, count: int = OPTION_COUNT):
        super().__init__(timeout=None)
        for i in range(min(count, OPTION_COUNT)):
            self.add_item(_PickButton(i))


async def _handle_proposal_pick(interaction: discord.Interaction, choice_index: int) -> None:
    """Shared handler: validate, commit chosen option as a Match, post publicly."""
    # Look up the ProposalSet by the DM message ID
    async with get_session() as db:
        stmt = select(ProposalSet).where(ProposalSet.dm_message_id == interaction.message.id)
        ps = (await db.execute(stmt)).scalar_one_or_none()

        if ps is None:
            await interaction.response.send_message(
                "Couldn't find this proposal set. The bot may have been restarted with a fresh DB.",
                ephemeral=True,
            )
            return
        if ps.chosen_index is not None:
            await interaction.response.send_message(
                f"This was already resolved (Option {chr(ord('A') + ps.chosen_index)}).",
                ephemeral=True,
            )
            return
        if ps.resolved_at is not None:
            await interaction.response.send_message(
                "These options were replaced by a newer set — use the latest DM.", ephemeral=True
            )
            return
        owning = await db.get(InhouseSession, ps.session_id)
        has_teams = (await db.execute(
            select(Match).where(Match.session_id == ps.session_id, Match.winner.is_(None))
        )).scalars().first() is not None
        if owning is None or owning.status != "matched" or has_teams:
            why = ("teams were already set for this session" if has_teams
                   else f"the session is {owning.status if owning else 'gone'}")
            await interaction.response.send_message(
                f"These options are no longer valid — {why}.", ephemeral=True
            )
            return

        proposals_data = json.loads(ps.proposals_json)
        if choice_index >= len(proposals_data):
            await interaction.response.send_message(
                f"Option {chr(ord('A') + choice_index)} doesn't exist (only {len(proposals_data)} were generated).",
                ephemeral=True,
            )
            return

        chosen = proposals_data[choice_index]
        # Persist as a Match row
        match = Match(
            session_id=ps.session_id,
            team1_json=json.dumps({k: int(v) for k, v in chosen["team1"].items()}),
            team2_json=json.dumps({k: int(v) for k, v in chosen["team2"].items()}),
            predicted_balance=chosen["balance_diff"],
        )
        db.add(match)
        await db.flush()  # assigns match.id without committing
        ps.chosen_index = choice_index
        ps.resolved_at = datetime.utcnow()
        ps.resolved_match_id = match.id
        await db.commit()  # the match and its link from the option set land together
        await db.refresh(match)

        session = await db.get(InhouseSession, ps.session_id)

        # Each player's elo in the role they were given, for the public post.
        elo_map: dict[int, float] = {}
        for team in (chosen["team1"], chosen["team2"]):
            for role, uid in team.items():
                rating = await db.get(Rating, (int(uid), role))
                elo_map[int(uid)] = rating.elo if rating else DEFAULT_ELO

    # Disable all buttons on the DM
    view = ProposalChoiceView(count=len(proposals_data))
    for child in view.children:
        child.disabled = True
    label = chr(ord("A") + choice_index)
    await interaction.response.edit_message(
        content=f"✅ Committed Option {label}. Posted to the recruit channel.",
        view=view,
    )

    # Post the chosen teams publicly
    posted = False
    channel = (
        interaction.client.get_channel(session.recruit_channel_id)
        if session and session.recruit_channel_id else None
    )
    if channel:
        embed = discord.Embed(
            title=f"🏆 Final Teams for Thursday {session.game_date.strftime('%b %d')}",
            color=discord.Color.green(),
        )
        all_ids = list(chosen["team1"].values()) + list(chosen["team2"].values())
        # The pick is clicked in a DM, so interaction.guild is None — resolve
        # display names through the recruit channel's guild instead.
        guild = getattr(channel, "guild", None)
        name_map = {
            uid: (m.display_name if guild and (m := guild.get_member(uid)) else str(uid))
            for uid in all_ids
        }
        embed.add_field(name=f"🔵 {TEAM_NAMES[1]} (Blue)", value=_team_lines(chosen["team1"], name_map=name_map, elo_map=elo_map), inline=True)
        embed.add_field(name=f"🔴 {TEAM_NAMES[2]} (Red)", value=_team_lines(chosen["team2"], name_map=name_map, elo_map=elo_map), inline=True)
        lane = chosen.get("lane_diff")
        balance_part = (
            f"Lane diff: {lane:.0f} · Team diff: {chosen['balance_diff']:.0f}"
            if lane is not None else f"Skill diff: {chosen['balance_diff']:.2f}"
        )
        embed.set_footer(
            text=f"Match {match.id} · Session #{session.id} (Option {label}) · "
                 f"{balance_part} · Game time: Thu 9:00 PM ET"
        )
        try:
            await channel.send(content="🔒 Teams finalized:", embed=embed)
            posted = True
        except discord.HTTPException:
            log.exception("Failed to post final teams for session %s", session.id)
    if not posted:
        # The DM above already said "Posted", so correct it rather than leave the
        # owner thinking teams went out.
        await interaction.followup.send(
            f"⚠️ Option {label} is saved as Match {match.id}, but I couldn't post the teams "
            f"to the recruit channel. Share them with `/match-roster match_id:{match.id}`."
        )

    await interaction.followup.send(await _build_channels_for_pick(interaction, channel, match.id))


async def _build_channels_for_pick(interaction: discord.Interaction, recruit_channel, match_id: int) -> str:
    """Build the per-team roles + private channels for a match that was just
    picked (what /match-channels does) and return the summary for the owner.
    A failure here never undoes the pick: the match is already saved."""
    retry = f"Run `/match-channels match_id:{match_id}` to build the team channels."
    # The pick is clicked in a DM, so the guild comes from the recruit channel.
    guild = getattr(recruit_channel, "guild", None)
    admin_cog = interaction.client.get_cog("AdminCog")
    if guild is None or admin_cog is None:
        return f"⚠️ I couldn't reach the server to build the team channels. {retry}"
    try:
        return await admin_cog._build_match_channels(guild, guild.id, match_id)
    except Exception:
        log.exception("Building team channels for match %s failed", match_id)
        return f"⚠️ Building the team channels failed partway. {retry}"
