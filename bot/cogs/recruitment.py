"""Recruitment cog: button-based RSVP with private role picker.

Flow
----
Friday 9 AM (or /recruit-now)
  -> Bot posts an embed in the recruit channel with 3 buttons:
        🎮 Playing      — opens ephemeral role picker
        ❌ Not Playing   — records them as out
        📺 Commentator   — records them as commentator
  -> The public embed shows only counts: "Playing: 7, Not Playing: 2, Commentators: 3"

Clicking 🎮 Playing
  -> Bot replies with an ephemeral message containing 6 toggle buttons
     (Top/Jungle/Mid/Bot/Support/Fill) + a Done button.
  -> Toggling a role button highlights it; clicking Done saves the choice.
  -> If the user already had roles selected, the picker pre-loads them.

Clicking ❌ Not Playing or 📺 Commentator
  -> Records the status, replies ephemerally with confirmation.
  -> They can still change their mind by clicking another button.

Monday 9 PM 30
  -> Read all signups with status='playing', run matchmaker, post teams publicly.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.config import Config, ROLE_EMOJIS, ROLES
from bot.db.models import GuildConfig, Match, MatchPerformance, Player, ProposalSet, Rating, Session as InhouseSession, Signup
from bot.db.session import get_session
from bot.services.matchmaking import MatchProposal, PlayerInput, TeamAssignment, make_match, make_top_matches
from bot.cogs.admin import format_roster_block

log = logging.getLogger(__name__)


def upcoming_recruit_thursdays(today: date, horizon_days: int = 13) -> list[date]:
    """Thursdays within `horizon_days` of `today` whose recruitment window has
    opened (recruitment posts 13 days ahead, so at any moment the next ~2
    Thursdays should already be open). Used to self-heal missed auto-posts."""
    out = []
    for ahead in range(horizon_days + 1):
        d = today + timedelta(days=ahead)
        if d.weekday() == 3:  # Thursday
            out.append(d)
    return out


# =============================================================================
# Public RSVP buttons (Playing / Not Playing / Commentator)
# =============================================================================

class RsvpView(discord.ui.View):
    """The three-button view attached to the public recruitment message.

    Persistent (timeout=None). Each button is its own action.
    Custom IDs are stable so they survive bot restarts; we re-register a
    placeholder instance in cog_load.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Playing", style=discord.ButtonStyle.success, emoji="🎮", custom_id="rsvp_playing")
    async def playing(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await _resolve_session_from_message(interaction.message.id)
        if session is None:
            await interaction.response.send_message("This recruitment is no longer active.", ephemeral=True)
            return
        if session.status != "recruiting":
            await interaction.response.send_message(
                f"Signups are closed for this session ({session.status}).", ephemeral=True
            )
            return

        # Look up existing roles so we can pre-fill the picker
        async with get_session() as db:
            existing = await db.get(Signup, (session.id, interaction.user.id))
            existing_roles = set(existing.role_list) if existing and existing.status == "playing" else set()

        view = RolePickerView(session_id=session.id, user_id=interaction.user.id, selected=existing_roles)
        await interaction.response.send_message(
            content="**Pick your role(s).** Toggle as many as you'd like, then click Done. "
                    "Choose **Fill** if you'll play whatever the team needs.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="Not Playing", style=discord.ButtonStyle.danger, emoji="❌", custom_id="rsvp_not_playing")
    async def not_playing(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _set_simple_status(interaction, "not_playing", "❌ Recorded — see you next week.")

    @discord.ui.button(label="Commentator", style=discord.ButtonStyle.secondary, emoji="📺", custom_id="rsvp_commentator")
    async def commentator(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _set_simple_status(interaction, "commentator", "📺 Logged as commentator.")


async def _set_simple_status(interaction: discord.Interaction, status: str, confirm: str) -> None:
    """Shared logic for Not Playing and Commentator buttons."""
    session = await _resolve_session_from_message(interaction.message.id)
    if session is None:
        await interaction.response.send_message("This recruitment is no longer active.", ephemeral=True)
        return
    if session.status != "recruiting":
        await interaction.response.send_message(
            f"Signups are closed for this session ({session.status}).", ephemeral=True
        )
        return

    async with get_session() as db:
        existing = await db.get(Signup, (session.id, interaction.user.id))
        if existing is None:
            db.add(Signup(
                session_id=session.id,
                discord_id=interaction.user.id,
                status=status,
                roles=None,
            ))
        else:
            existing.status = status
            existing.roles = None
            existing.signed_up_at = datetime.utcnow()
        await db.commit()

    await interaction.response.send_message(confirm, ephemeral=True)
    # Bump the public embed counts
    await _refresh_public_counts(interaction.client, session.id)


async def _resolve_session_from_message(message_id: int) -> Optional[InhouseSession]:
    async with get_session() as db:
        stmt = select(InhouseSession).where(InhouseSession.recruit_msg_id == message_id)
        return (await db.execute(stmt)).scalar_one_or_none()


async def _is_linked_approved(discord_id: int) -> bool:
    """True if the user has an approved Riot link — otherwise matchmaking would
    seed them at the default 1200 rather than their real rank."""
    async with get_session() as db:
        p = await db.get(Player, discord_id)
    return p is not None and p.riot_puuid is not None and p.link_status == "approved"


# =============================================================================
# Role picker (ephemeral) — 6 toggle buttons + Done
# =============================================================================

class RolePickerView(discord.ui.View):
    """Ephemeral, short-lived view for picking roles. Not persistent —
    each picker is a fresh instance bound to (session_id, user_id).

    Toggle buttons: Top, Jungle, Mid, Bot, Support, Fill.
    "Fill" is mutually exclusive with the others (selecting Fill clears the
    rest; selecting any specific role clears Fill).
    """

    # 5 minute timeout — picker disappears if user wanders off
    def __init__(self, session_id: int, user_id: int, selected: set[str]):
        super().__init__(timeout=300)
        self.session_id = session_id
        self.user_id = user_id
        self.selected: set[str] = set(selected)
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        # Five role-specific buttons + Fill, in fixed order
        button_specs = [
            ("TOP", ROLE_EMOJIS["TOP"]),
            ("JUNGLE", ROLE_EMOJIS["JUNGLE"]),
            ("MID", ROLE_EMOJIS["MID"]),
            ("BOT", ROLE_EMOJIS["BOT"]),
            ("SUPPORT", ROLE_EMOJIS["SUPPORT"]),
            ("FILL", ROLE_EMOJIS["FILL"]),
        ]
        for role, emoji in button_specs:
            is_selected = role in self.selected
            self.add_item(RoleToggleButton(role, emoji, is_selected))
        self.add_item(DoneButton())
        self.add_item(CancelButton())

    def toggle(self, role: str) -> None:
        if role == "FILL":
            if "FILL" in self.selected:
                self.selected.discard("FILL")
            else:
                self.selected = {"FILL"}
        else:
            self.selected.discard("FILL")
            if role in self.selected:
                self.selected.discard(role)
            else:
                self.selected.add(role)
        self._build_buttons()


class RoleToggleButton(discord.ui.Button):
    def __init__(self, role: str, emoji: str, selected: bool):
        super().__init__(
            label=role.title(),
            emoji=emoji,
            style=discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary,
        )
        self.role = role

    async def callback(self, interaction: discord.Interaction):
        view: RolePickerView = self.view  # type: ignore
        view.toggle(self.role)
        await interaction.response.edit_message(view=view)


class DoneButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Done", style=discord.ButtonStyle.primary, emoji="✅", row=1)

    async def callback(self, interaction: discord.Interaction):
        view: RolePickerView = self.view  # type: ignore
        if not view.selected:
            await interaction.response.send_message(
                "Pick at least one role (or **Fill** if you'll play anything).",
                ephemeral=True,
            )
            return

        async with get_session() as db:
            existing = await db.get(Signup, (view.session_id, view.user_id))
            roles_csv = ",".join(sorted(view.selected))
            if existing is None:
                db.add(Signup(
                    session_id=view.session_id,
                    discord_id=view.user_id,
                    status="playing",
                    roles=roles_csv,
                ))
            else:
                existing.status = "playing"
                existing.roles = roles_csv
                existing.signed_up_at = datetime.utcnow()
            await db.commit()

        nice_roles = ", ".join(view.selected)
        warn = ""
        if not await _is_linked_approved(view.user_id):
            warn = ("\n⚠️ You don't have an approved Riot link yet — run `/link` before game day, "
                    "or you'll be seeded at the default rating.")
        # Disable everything to make it clear it's saved
        for child in view.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ Saved: **{nice_roles}**. You can click 🎮 Playing again to change.{warn}",
            view=view,
        )
        await _refresh_public_counts(interaction.client, view.session_id)


class CancelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        for child in self.view.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled.", view=self.view)


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
        ps.chosen_index = choice_index
        ps.resolved_at = datetime.utcnow()
        await db.commit()
        await db.refresh(match)
        ps.resolved_match_id = match.id
        await db.commit()

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
            t1 = "\n".join(
                f"{ROLE_EMOJIS[role]} **{role}**: <@{pid}>"
                for role, pid in chosen["team1"].items()
            )
            t2 = "\n".join(
                f"{ROLE_EMOJIS[role]} **{role}**: <@{pid}>"
                for role, pid in chosen["team2"].items()
            )
            embed.add_field(name="🔵 Team 1 (Blue)", value=t1, inline=True)
            embed.add_field(name="🔴 Team 2 (Red)", value=t2, inline=True)
            embed.set_footer(
                text=f"Match {match.id} · Session #{session.id} (Option {label}) · "
                     f"Skill diff: {chosen['balance_diff']:.2f} · "
                     f"Game time: Thu 9:30 PM ET"
            )
            await channel.send(content="🔒 Teams finalized:", embed=embed)


# =============================================================================
# Public count refresh
# =============================================================================

async def _refresh_public_counts(bot: discord.Client, session_id: int) -> None:
    """Update the original public embed with current Playing/Not Playing/Commentator
    totals AND the list of users in each bucket."""
    async with get_session() as db:
        session = await db.get(InhouseSession, session_id)
        if session is None or session.recruit_msg_id is None:
            return
        signups = (await db.execute(
            select(Signup)
            .where(Signup.session_id == session_id)
            .order_by(Signup.signed_up_at.asc())
        )).scalars().all()

    # Bucket by status, in signup order
    buckets: dict[str, list[int]] = {"playing": [], "not_playing": [], "commentator": []}
    for s in signups:
        if s.status in buckets:
            buckets[s.status].append(s.discord_id)

    channel = bot.get_channel(session.recruit_channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(session.recruit_msg_id)
    except (discord.NotFound, discord.Forbidden):
        return

    tz = pytz.timezone("America/New_York")
    signups_close = (
        pytz.UTC.localize(session.signups_close_at).astimezone(tz)
        if session.signups_close_at else None
    )
    embed = _build_public_embed(session.game_date, signups_close, buckets, session_id=session.id)
    await msg.edit(embed=embed)


def _format_user_list(user_ids: list[int], max_chars: int = 950) -> str:
    """Format a list of discord IDs as @-mentions, one per line.
    Discord embed field values cap at 1024 chars; we truncate at 950 to leave
    room for the count-line and any "...and N more" suffix.
    """
    if not user_ids:
        return "*nobody yet*"
    lines: list[str] = []
    used = 0
    for i, uid in enumerate(user_ids):
        line = f"<@{uid}>"
        if used + len(line) + 1 > max_chars:
            remaining = len(user_ids) - i
            lines.append(f"…and {remaining} more")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _build_public_embed(
    game_date: date,
    signups_close: Optional[datetime],
    buckets: dict[str, list[int]],
    session_id: Optional[int] = None,
) -> discord.Embed:
    playing = buckets.get("playing", [])
    not_playing = buckets.get("not_playing", [])
    commentator = buckets.get("commentator", [])

    close_line = (
        f"**Signups close:** {signups_close.strftime('%a %b %d, %I:%M %p %Z')}"
        if signups_close else "**Signups stay open until an admin closes them.**"
    )

    embed = discord.Embed(
        title=f"🎮 Inhouse — Thursday {game_date.strftime('%b %d, %Y')} @ 9:30 PM ET",
        description=(
            f"Click a button below to RSVP. **Your role choice is private** "
            f"(only you and the bot will see it).\n\n{close_line}"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=f"🎮 Playing — **{len(playing)}**",
        value=_format_user_list(playing),
        inline=True,
    )
    embed.add_field(
        name=f"❌ Not Playing — **{len(not_playing)}**",
        value=_format_user_list(not_playing),
        inline=True,
    )
    embed.add_field(
        name=f"📺 Commentators — **{len(commentator)}**",
        value=_format_user_list(commentator),
        inline=True,
    )
    parts = []
    if session_id is not None:
        parts.append(f"Session #{session_id}")
    if len(playing) >= 10:
        parts.append("✅ Enough players to run a match")
    elif playing:
        parts.append(f"Need {10 - len(playing)} more to run a match")
    if parts:
        embed.set_footer(text=" · ".join(parts))
    return embed


# =============================================================================
# Cog
# =============================================================================

class RecruitmentCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config):
        self.bot = bot
        self.config = config
        self.tz = pytz.timezone(config.timezone)

    async def cog_load(self) -> None:
        # Persistent view registration so buttons survive restarts
        self.bot.add_view(RsvpView())
        self.bot.add_view(ProposalChoiceView())

    # ---------- Public API used by scheduler ----------

    async def post_recruitment(
        self,
        game_date: date,
        channel: Optional[discord.TextChannel] = None,
        open_ended: bool = False,
    ) -> InhouseSession:
        """Post the recruitment for a Thursday. If channel is None, uses the
        configured recruit channel. If open_ended, signups have no scheduled
        close (signups_close_at=NULL) and only close via /close-signups."""
        if open_ended:
            signups_close_local = None
        else:
            monday_before = game_date - timedelta(days=3)
            signups_close_local = self.tz.localize(
                datetime.combine(monday_before, datetime.min.time()).replace(hour=21, minute=30)
            )

        if channel is None:
            channel = await self._get_recruit_channel()
        if channel is None:
            raise RuntimeError("No recruit channel configured. Use /set-channel recruit first.")

        view = RsvpView()
        async with get_session() as db:
            session = InhouseSession(
                game_date=game_date,
                recruit_posted_at=datetime.utcnow(),
                signups_close_at=signups_close_local.astimezone(pytz.UTC).replace(tzinfo=None),
                recruit_channel_id=channel.id,
                status="recruiting",
            )
            db.add(session)
            await db.flush()  # assigns session.id before we build the embed/send
            embed = _build_public_embed(
                game_date, signups_close_local, buckets={}, session_id=session.id
            )
            msg = await channel.send(embed=embed, view=view)
            session.recruit_msg_id = msg.id
            await db.commit()
            await db.refresh(session)
        return session

    async def ensure_upcoming_recruitments(self) -> int:
        """Self-heal: post recruitment for any upcoming Thursday whose window has
        opened but has no session yet. Called on startup and by the Friday cron, so
        a missed cron run (e.g. the machine was down at 9 AM Friday) is recovered on
        the next boot instead of being silently skipped forever.

        No-ops with a warning if no recruit channel is configured — we won't post to
        an arbitrary fallback channel for an automated action. Returns count posted.
        """
        channel = await self._configured_recruit_channel()
        if channel is None:
            log.warning(
                "No recruit channel configured — skipping auto-post. "
                "Set one with /set-channel recruit."
            )
            return 0
        today = datetime.now(self.tz).date()
        posted = 0
        for d in upcoming_recruit_thursdays(today):
            async with get_session() as db:
                exists = (await db.execute(
                    select(InhouseSession).where(InhouseSession.game_date == d)
                )).scalars().first()
            if exists is not None:
                continue
            try:
                await self.post_recruitment(d, channel=channel)
                posted += 1
                log.info("Auto-post: created recruitment for %s", d)
            except Exception:
                log.exception("Auto-post: failed to create recruitment for %s", d)
        return posted

    async def close_signups_and_match(self, session_id: int) -> None:
        """Lock the session, refresh everyone's base_seed from current Riot rank,
        then run matchmaker on Playing signups, post teams."""
        # Refresh base_seed for all linked players BEFORE running matchmaker.
        # This is the "weekly auto-refresh" — solo queue rank changes between
        # Mondays now influence elo before matchmaking.
        try:
            admin_cog = self.bot.get_cog("AdminCog")
            if admin_cog is not None and hasattr(admin_cog, "_refresh_all_base_seeds"):
                result = await admin_cog._refresh_all_base_seeds()
                log.info("Pre-matchmaking sync: %s", result)
            else:
                log.warning("AdminCog._refresh_all_base_seeds not available; skipping pre-matchmaking sync")
        except Exception:
            log.exception("Pre-matchmaking base_seed refresh failed; continuing with stale ratings")

        async with get_session() as db:
            session = await db.get(InhouseSession, session_id)
            if session is None or session.status != "recruiting":
                log.warning("Session %s not in recruiting state", session_id)
                return

            playing_signups = (await db.execute(
                select(Signup)
                .where(Signup.session_id == session_id)
                .where(Signup.status == "playing")
            )).scalars().all()

            channel = self.bot.get_channel(session.recruit_channel_id)
            if channel is None:
                log.error("Recruit channel %s no longer accessible", session.recruit_channel_id)
                return

            if len(playing_signups) < 10:
                session.status = "cancelled"
                await db.commit()
                await channel.send(
                    f"❌ Inhouse for **{session.game_date.strftime('%a %b %d')}** cancelled — "
                    f"only {len(playing_signups)} signups (need 10)."
                )
                return

            # First 10 by signup time
            ordered = sorted(playing_signups, key=lambda s: s.signed_up_at)[:10]
            player_inputs = []
            for su in ordered:
                ratings = (await db.execute(
                    select(Rating).where(Rating.discord_id == su.discord_id)
                )).scalars().all()
                ratings_dict = {r.role: r.elo for r in ratings}
                for role in ROLES:
                    ratings_dict.setdefault(role, 1200)
                player_inputs.append(PlayerInput(
                    discord_id=su.discord_id,
                    preferred_roles=su.role_list,
                    ratings=ratings_dict,
                ))

            proposals = make_top_matches(player_inputs, n=3, min_diff=2)
            if not proposals:
                session.status = "cancelled"
                await db.commit()
                await channel.send(
                    "❌ Couldn't generate balanced teams — role coverage is impossible "
                    "with the current signups (e.g., nobody picked Support and no Fills). "
                    "An admin will need to swap people manually."
                )
                return

            # Save proposals for owner to choose from
            proposal_set = ProposalSet(
                session_id=session.id,
                proposals_json=json.dumps([p.to_json_dict() for p in proposals]),
            )
            db.add(proposal_set)
            session.status = "matched"
            await db.commit()
            await db.refresh(proposal_set)

            # Public placeholder
            await channel.send(
                f"🔒 Signups closed for **{session.game_date.strftime('%a %b %d')}** "
                f"(Session #{session.id}). Teams being finalized — check back shortly."
            )
            if len(playing_signups) > 10:
                bench = [s for s in playing_signups
                         if not any(
                             s.discord_id in p.team1.player_ids or
                             s.discord_id in p.team2.player_ids
                             for p in proposals
                         )]
                if bench:
                    bench_mentions = " ".join(f"<@{s.discord_id}>" for s in bench)
                    await channel.send(f"🪑 On the bench (signed up after first 10): {bench_mentions}")

        # DM the owner with the 3 options
        await self._send_proposal_dm(proposal_set.id, session.id, proposals)

    async def _send_proposal_dm(
        self, proposal_set_id: int, session_id: int, proposals: list,
    ) -> None:
        """DM the owner with the top-3 options + choice buttons."""
        try:
            owner = self.bot.get_user(self.config.owner_discord_id) or \
                    await self.bot.fetch_user(self.config.owner_discord_id)
        except (discord.NotFound, discord.HTTPException):
            log.error("Could not fetch owner user %s", self.config.owner_discord_id)
            await self._warn_owner_dm_failed(session_id)
            return

        async with get_session() as db:
            session = await db.get(InhouseSession, session_id)

        embeds = []
        for i, proposal in enumerate(proposals):
            label = chr(ord("A") + i)  # A, B, C
            embed = discord.Embed(
                title=f"Option {label}",
                color=[discord.Color.green(), discord.Color.blue(), discord.Color.purple()][i],
            )
            t1 = "\n".join(
                f"{ROLE_EMOJIS[role]} **{role}**: <@{pid}>"
                for role, pid in proposal.team1.by_role.items()
            )
            t2 = "\n".join(
                f"{ROLE_EMOJIS[role]} **{role}**: <@{pid}>"
                for role, pid in proposal.team2.by_role.items()
            )
            embed.add_field(name="🔵 Team 1", value=t1, inline=True)
            embed.add_field(name="🔴 Team 2", value=t2, inline=True)
            embed.set_footer(
                text=f"Skill diff: {proposal.balance_diff:.2f} · "
                     f"Off-role assignments: {proposal.role_penalty:.1f}"
            )
            embeds.append(embed)

        intro = discord.Embed(
            title=f"📊 Match options for Thursday {session.game_date.strftime('%b %d, %Y')}",
            description=(
                f"Three balanced options below, ranked by quality "
                f"(Option A is the most balanced).\n\n"
                f"Click a button to commit that option to the inhouse for elo tracking. "
                f"The chosen teams will be posted publicly to the recruit channel."
            ),
            color=discord.Color.gold(),
        )

        view = ProposalChoiceView(proposal_set_id, len(proposals))
        try:
            msg = await owner.send(embeds=[intro] + embeds, view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error("Could not DM owner: %s", e)
            await self._warn_owner_dm_failed(session_id)
            return

        async with get_session() as db:
            ps = await db.get(ProposalSet, proposal_set_id)
            if ps:
                ps.dm_message_id = msg.id
                await db.commit()

    async def _warn_owner_dm_failed(self, session_id: int) -> None:
        """Fallback when the owner can't be DM'd the match options: post a notice
        in the recruit channel so the session isn't silently wedged with no teams."""
        async with get_session() as db:
            session = await db.get(InhouseSession, session_id)
        if session is None or session.recruit_channel_id is None:
            return
        channel = self.bot.get_channel(session.recruit_channel_id)
        if channel is None:
            return
        try:
            await channel.send(
                "⚠️ Teams are ready, but I couldn't DM the bot owner the options to choose from. "
                "An admin can finalize manually with `/manual-match`, or the owner should enable DMs "
                "and re-run `/test-trigger-close`."
            )
        except discord.HTTPException:
            log.exception("Failed to post owner-DM-failure warning to channel")

    # ---------- Slash commands ----------

    @app_commands.command(name="recruit-now", description="(admin) Manually post a recruitment for a given Thursday.")
    @app_commands.describe(
        game_date="Game date in YYYY-MM-DD format (must be a Thursday)",
        channel="Where to post (defaults to the configured recruit channel, then this channel).",
        open_ended="If true, signups stay open until you run /close-signups (no Monday auto-close).",
    )
    async def recruit_now(
        self,
        interaction: discord.Interaction,
        game_date: str,
        channel: Optional[discord.TextChannel] = None,
        open_ended: bool = False,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        try:
            d = datetime.strptime(game_date, "%Y-%m-%d").date()
        except ValueError:
            await interaction.response.send_message("Bad date format. Use YYYY-MM-DD.", ephemeral=True)
            return
        if d.weekday() != 3:
            await interaction.response.send_message("Date must be a Thursday.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # Prefer an explicit channel, then the configured recruit channel
        # (/set-channel recruit), then fall back to the current channel.
        target = channel or await self._get_recruit_channel()
        if target is None and isinstance(interaction.channel, discord.TextChannel):
            target = interaction.channel
        try:
            session = await self.post_recruitment(d, channel=target, open_ended=open_ended)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)
            return
        posted_in = self.bot.get_channel(session.recruit_channel_id)
        where = posted_in.mention if isinstance(posted_in, discord.TextChannel) else "the recruit channel"
        close_note = (
            " Signups stay open until you run `/close-signups`."
            if open_ended else " Signups auto-close Monday 9:30 PM ET."
        )
        await interaction.followup.send(
            f"Posted recruitment for {d} in {where} (Session #{session.id}).{close_note}",
            ephemeral=True,
        )

    @app_commands.command(name="match-preview", description="(admin) Show what teams would be generated right now.")
    async def match_preview(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession).where(InhouseSession.status == "recruiting")
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.followup.send("No active recruiting session.", ephemeral=True)
                return
            playing_signups = (await db.execute(
                select(Signup)
                .where(Signup.session_id == session.id)
                .where(Signup.status == "playing")
            )).scalars().all()
            if len(playing_signups) < 10:
                await interaction.followup.send(
                    f"Only {len(playing_signups)} playing signups, need 10.", ephemeral=True
                )
                return
            ordered = sorted(playing_signups, key=lambda s: s.signed_up_at)[:10]
            player_inputs = []
            for su in ordered:
                ratings = (await db.execute(
                    select(Rating).where(Rating.discord_id == su.discord_id)
                )).scalars().all()
                ratings_dict = {r.role: r.elo for r in ratings}
                for role in ROLES:
                    ratings_dict.setdefault(role, 1200)
                player_inputs.append(PlayerInput(
                    discord_id=su.discord_id,
                    preferred_roles=su.role_list,
                    ratings=ratings_dict,
                ))
        proposals = make_top_matches(player_inputs, n=3, min_diff=2)
        if not proposals:
            await interaction.followup.send("No valid matchup possible with current signups.", ephemeral=True)
            return

        embeds = []
        for i, proposal in enumerate(proposals):
            label = chr(ord("A") + i)
            embed = discord.Embed(
                title=f"Preview — Option {label}",
                color=[discord.Color.green(), discord.Color.blue(), discord.Color.purple()][i],
            )
            t1 = "\n".join(f"**{role}**: <@{pid}>" for role, pid in proposal.team1.by_role.items())
            t2 = "\n".join(f"**{role}**: <@{pid}>" for role, pid in proposal.team2.by_role.items())
            embed.add_field(name="Team 1", value=t1, inline=True)
            embed.add_field(name="Team 2", value=t2, inline=True)
            embed.set_footer(
                text=f"Skill diff: {proposal.balance_diff:.2f} · "
                     f"Off-role: {proposal.role_penalty:.1f}"
            )
            embeds.append(embed)
        await interaction.followup.send(
            content=f"Top {len(proposals)} options (preview only — no commit):",
            embeds=embeds,
            ephemeral=True,
        )

    @app_commands.command(
        name="roster-template",
        description="(admin) Build an editable copy/paste roster from the current signups.",
    )
    async def roster_template(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession).where(InhouseSession.status == "recruiting")
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.followup.send("No active recruiting session.", ephemeral=True)
                return
            playing = (await db.execute(
                select(Signup)
                .where(Signup.session_id == session.id)
                .where(Signup.status == "playing")
            )).scalars().all()
            if len(playing) < 10:
                await interaction.followup.send(
                    f"Only {len(playing)} playing signups — need 10 to build a template.",
                    ephemeral=True,
                )
                return
            ordered = sorted(playing, key=lambda s: s.signed_up_at)[:10]
            player_inputs = []
            names: dict[int, str] = {}
            for su in ordered:
                ratings = (await db.execute(
                    select(Rating).where(Rating.discord_id == su.discord_id)
                )).scalars().all()
                ratings_dict = {r.role: r.elo for r in ratings}
                for role in ROLES:
                    ratings_dict.setdefault(role, 1200)
                player_inputs.append(PlayerInput(
                    discord_id=su.discord_id,
                    preferred_roles=su.role_list,
                    ratings=ratings_dict,
                ))
                p = await db.get(Player, su.discord_id)
                names[su.discord_id] = p.riot_game_name if p and p.riot_game_name else f"<@{su.discord_id}>"

        # Best-effort balanced seating as a starting point; fall back to arbitrary
        # seating (still a valid 5v5 block) if role coverage makes a balance impossible.
        proposal = make_match(player_inputs)
        if proposal:
            team1, team2 = proposal.team1.by_role, proposal.team2.by_role
            note = (f"Balanced starting point (skill diff {proposal.balance_diff:.0f}, "
                    f"off-role {proposal.role_penalty:.1f}).")
        else:
            ids = [su.discord_id for su in ordered]
            team1 = {ROLES[i]: ids[i] for i in range(5)}
            team2 = {ROLES[i]: ids[5 + i] for i in range(5)}
            note = ("⚠️ No balanced role assignment possible from current prefs — players "
                    "seated arbitrarily; fix the roles before submitting.")

        def who(team: dict[str, int]) -> str:
            return ", ".join(f"{r} {names.get(team[r], team[r])}" for r in ROLES if r in team)

        content = (
            f"🧩 Template from {len(ordered)} signups for **{session.game_date.strftime('%b %d')}**. {note}\n"
            f"Copy, edit, and paste into `/manual-match`:\n"
            f"```\n{format_roster_block(team1, team2)}\n```\n"
            f"**Team 1** — {who(team1)}\n"
            f"**Team 2** — {who(team2)}"
        )
        await interaction.followup.send(
            content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(
        name="signups",
        description="(admin) Show who signed up and the role(s) they picked.",
    )
    @app_commands.describe(
        session_id="Session ID (from the recruitment post footer). Defaults to the soonest active session."
    )
    async def signups(self, interaction: discord.Interaction, session_id: Optional[int] = None):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            if session_id is not None:
                session = await db.get(InhouseSession, session_id)
                if session is None:
                    await interaction.followup.send(f"Session #{session_id} not found.", ephemeral=True)
                    return
            else:
                session = (await db.execute(
                    select(InhouseSession).where(InhouseSession.status == "recruiting")
                    .order_by(InhouseSession.game_date.asc())
                )).scalars().first()
                if session is None:
                    await interaction.followup.send(
                        "No active recruiting session. Pass a `session_id` to view a specific one.",
                        ephemeral=True,
                    )
                    return
            sus = (await db.execute(
                select(Signup).where(Signup.session_id == session.id)
                .order_by(Signup.signed_up_at.asc())
            )).scalars().all()
            ids = [s.discord_id for s in sus]
            approved_ids = set()
            if ids:
                approved_ids = {
                    p.discord_id for p in (await db.execute(
                        select(Player).where(Player.discord_id.in_(ids))
                    )).scalars().all()
                    if p.riot_puuid and p.link_status == "approved"
                }

        playing = [s for s in sus if s.status == "playing"]
        not_playing = [s for s in sus if s.status == "not_playing"]
        commentator = [s for s in sus if s.status == "commentator"]

        lines = [
            f"📋 Signups for **{session.game_date.strftime('%a %b %d')}** · "
            f"Session #{session.id} ({session.status})",
            "",
            f"**🎮 Playing ({len(playing)})**",
        ]
        if playing:
            for s in playing:
                roles = ", ".join(s.role_list) if s.role_list else "—"
                flag = "" if s.discord_id in approved_ids else "  ⚠️ not linked"
                lines.append(f"• <@{s.discord_id}> — {roles}{flag}")
        else:
            lines.append("*nobody yet*")
        lines.append("")
        np = ", ".join(f"<@{s.discord_id}>" for s in not_playing) or "—"
        co = ", ".join(f"<@{s.discord_id}>" for s in commentator) or "—"
        lines.append(f"**❌ Not Playing ({len(not_playing)})**: {np}")
        lines.append(f"**📺 Commentators ({len(commentator)})**: {co}")

        # Ephemeral so the otherwise-private role picks aren't leaked publicly.
        # Send in <=1900-char chunks, with mentions rendered but not pinging.
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1900:
                await interaction.followup.send(
                    chunk, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
                )
                chunk = ""
            chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            await interaction.followup.send(
                chunk, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )

    @app_commands.command(
        name="cancel-session",
        description="(admin) Cancel a recruiting/matched session.",
    )
    @app_commands.describe(session_id="Session ID to cancel. Defaults to the soonest active session.")
    async def cancel_session(self, interaction: discord.Interaction, session_id: Optional[int] = None):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            if session_id is not None:
                session = await db.get(InhouseSession, session_id)
                if session is None:
                    await interaction.followup.send(f"Session #{session_id} not found.", ephemeral=True)
                    return
            else:
                session = (await db.execute(
                    select(InhouseSession).where(InhouseSession.status.in_(["recruiting", "closed", "matched"]))
                    .order_by(InhouseSession.game_date.asc())
                )).scalars().first()
                if session is None:
                    await interaction.followup.send("No active session to cancel.", ephemeral=True)
                    return
            if session.status in ("completed", "cancelled"):
                await interaction.followup.send(
                    f"Session #{session.id} is already {session.status}.", ephemeral=True
                )
                return
            prev = session.status
            session.status = "cancelled"
            sid, game_date, channel_id = session.id, session.game_date, session.recruit_channel_id
            await db.commit()

        if channel_id:
            ch = self.bot.get_channel(channel_id)
            if ch is not None:
                try:
                    await ch.send(
                        f"❌ Inhouse for **{game_date.strftime('%a %b %d')}** "
                        f"(Session #{sid}) has been cancelled."
                    )
                except discord.HTTPException:
                    log.exception("Failed to post cancellation notice")
        await interaction.followup.send(f"✅ Cancelled Session #{sid} (was {prev}).", ephemeral=True)

    @app_commands.command(
        name="remove-signup",
        description="(admin) Remove a player's signup from a session.",
    )
    @app_commands.describe(
        user="The player to remove",
        session_id="Session ID. Defaults to the soonest recruiting session.",
    )
    async def remove_signup(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        session_id: Optional[int] = None,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            if session_id is not None:
                session = await db.get(InhouseSession, session_id)
                if session is None:
                    await interaction.followup.send(f"Session #{session_id} not found.", ephemeral=True)
                    return
            else:
                session = (await db.execute(
                    select(InhouseSession).where(InhouseSession.status == "recruiting")
                    .order_by(InhouseSession.game_date.asc())
                )).scalars().first()
                if session is None:
                    await interaction.followup.send("No active recruiting session.", ephemeral=True)
                    return
            su = await db.get(Signup, (session.id, user.id))
            if su is None:
                await interaction.followup.send(
                    f"{user.display_name} isn't signed up for Session #{session.id}.", ephemeral=True
                )
                return
            await db.delete(su)
            await db.commit()
            sid = session.id

        await _refresh_public_counts(interaction.client, sid)
        await interaction.followup.send(
            f"✅ Removed {user.mention} from Session #{sid}.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="close-signups",
        description="(admin) Close signups for one recruitment (locks RSVPs).",
    )
    @app_commands.describe(
        session_id="Session ID to close (from the recruitment footer). Required if more than one is open."
    )
    async def close_signups(self, interaction: discord.Interaction, session_id: Optional[int] = None):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            if session_id is not None:
                session = await db.get(InhouseSession, session_id)
                if session is None:
                    await interaction.followup.send(f"Session #{session_id} not found.", ephemeral=True)
                    return
                if session.status != "recruiting":
                    await interaction.followup.send(
                        f"Session #{session.id} signups aren't open (status: {session.status}).",
                        ephemeral=True,
                    )
                    return
            else:
                # No id given: only act if exactly one recruitment is open, otherwise
                # refuse and list them — never close more than the one you mean.
                open_sessions = (await db.execute(
                    select(InhouseSession).where(InhouseSession.status == "recruiting")
                    .order_by(InhouseSession.game_date.asc())
                )).scalars().all()
                if not open_sessions:
                    await interaction.followup.send("No open recruiting sessions.", ephemeral=True)
                    return
                if len(open_sessions) > 1:
                    listing = "\n".join(
                        f"• Session #{s.id} — {s.game_date.strftime('%a %b %d')}" for s in open_sessions
                    )
                    await interaction.followup.send(
                        "Multiple recruitments are open — specify which with `session_id:`\n" + listing,
                        ephemeral=True,
                    )
                    return
                session = open_sessions[0]

            playing_count = len((await db.execute(
                select(Signup).where(Signup.session_id == session.id, Signup.status == "playing")
            )).scalars().all())
            session.status = "closed"
            sid, game_date, channel_id = session.id, session.game_date, session.recruit_channel_id
            await db.commit()

        if channel_id:
            ch = self.bot.get_channel(channel_id)
            if ch is not None:
                try:
                    await ch.send(
                        f"🔒 Signups closed for **{game_date.strftime('%a %b %d')}** "
                        f"(Session #{sid}) — {playing_count} playing."
                    )
                except discord.HTTPException:
                    log.exception("Failed to post close-signups notice")
        await interaction.followup.send(
            f"✅ Closed signups for Session #{sid} ({playing_count} playing). "
            f"Set teams with `/manual-match session_id:{sid}`.",
            ephemeral=True,
        )

    # ---------- Helpers ----------

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.name == self.config.admin_role_name for r in interaction.user.roles)

    async def _get_recruit_channel(self) -> Optional[discord.TextChannel]:
        async with get_session() as db:
            cfg = await db.get(GuildConfig, self.config.discord_guild_id)
        if cfg and cfg.recruit_channel_id:
            ch = self.bot.get_channel(cfg.recruit_channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch
        guild = self.bot.get_guild(self.config.discord_guild_id)
        if guild is None:
            return None
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch
        return None

    async def _configured_recruit_channel(self) -> Optional[discord.TextChannel]:
        """The explicitly-configured recruit channel only (no random fallback).
        Auto-posts use this so they never post to an arbitrary channel."""
        async with get_session() as db:
            cfg = await db.get(GuildConfig, self.config.discord_guild_id)
        if cfg and cfg.recruit_channel_id:
            ch = self.bot.get_channel(cfg.recruit_channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch
        return None

    async def _build_match_embed(
        self, session: InhouseSession, match: Match, proposal
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"🏆 Teams for Thursday {session.game_date.strftime('%b %d')}",
            color=discord.Color.green(),
        )
        t1 = "\n".join(f"{ROLE_EMOJIS[role]} **{role}**: <@{pid}>"
                       for role, pid in proposal.team1.by_role.items())
        t2 = "\n".join(f"{ROLE_EMOJIS[role]} **{role}**: <@{pid}>"
                       for role, pid in proposal.team2.by_role.items())
        embed.add_field(name="🔵 Team 1 (Blue)", value=t1, inline=True)
        embed.add_field(name="🔴 Team 2 (Red)", value=t2, inline=True)
        embed.set_footer(
            text=f"Match {match.id} · Skill diff: {proposal.balance_diff:.2f} · "
                 f"Game time: Thu 9:30 PM ET"
        )
        return embed
