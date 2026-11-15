"""The owner's team-option DM: the A/B/C buttons, and committing the picked
option as the session's match."""
from __future__ import annotations

import json
import logging
from datetime import datetime

import discord
from sqlalchemy import select

from bot.config import format_team_lines
from bot.db.models import Match, ProposalSet, Session as InhouseSession
from bot.db.session import get_session
from bot.services.rosters import TEAM_NAMES

log = logging.getLogger(__name__)



def _team_lines(
    by_role: dict[str, int],
    emoji: bool = True,
    name_map: dict[int, str] | None = None,
) -> str:
    return format_team_lines(by_role, emoji=emoji, name_map=name_map)


# =============================================================================
# Owner choice view (for picking among top-3 match options)
# =============================================================================

class ProposalChoiceView(discord.ui.View):
    """Persistent view DM'd to the owner with 3 buttons (A/B/C).
    Custom IDs are stable across restarts. The handlers re-fetch state from DB.
    """

    def __init__(self, proposal_set_id: int = 0, count: int = 3):
        super().__init__(timeout=None)
        # We always render up to 3 buttons; we'll disable extras if count<3
        # The placeholder (proposal_set_id=0) is the post-restart re-registration

    @discord.ui.button(label="Pick Option A", style=discord.ButtonStyle.success, custom_id="proposal_pick_a")
    async def pick_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_proposal_pick(interaction, 0)

    @discord.ui.button(label="Pick Option B", style=discord.ButtonStyle.primary, custom_id="proposal_pick_b")
    async def pick_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_proposal_pick(interaction, 1)

    @discord.ui.button(label="Pick Option C", style=discord.ButtonStyle.secondary, custom_id="proposal_pick_c")
    async def pick_c(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_proposal_pick(interaction, 2)


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

    # Disable all buttons on the DM
    view = ProposalChoiceView()
    for child in view.children:
        child.disabled = True
    label = chr(ord("A") + choice_index)
    await interaction.response.edit_message(
        content=f"✅ Committed Option {label}. Posted to the recruit channel.",
        view=view,
    )

    # Post the chosen teams publicly
    if session and session.recruit_channel_id:
        channel = interaction.client.get_channel(session.recruit_channel_id)
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
            embed.add_field(name=f"🔵 {TEAM_NAMES[1]} (Blue)", value=_team_lines(chosen["team1"], name_map=name_map), inline=True)
            embed.add_field(name=f"🔴 {TEAM_NAMES[2]} (Red)", value=_team_lines(chosen["team2"], name_map=name_map), inline=True)
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
                return
            except discord.HTTPException:
                log.exception("Failed to post final teams for session %s", session.id)
    # Reached only when the public post didn't happen — the DM above already said
    # "Posted", so correct it rather than leave the owner thinking teams went out.
    await interaction.followup.send(
        f"⚠️ Option {label} is saved as Match {match.id}, but I couldn't post the teams "
        f"to the recruit channel. Share them with `/match-roster match_id:{match.id}`."
    )
