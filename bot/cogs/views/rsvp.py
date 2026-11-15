"""The public recruitment message: its Playing / Not Playing / Commentator
buttons, the private role picker behind Playing, and the embed with the
running signup lists."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import discord
import pytz
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.config import ROLE_EMOJIS
from bot.db.models import Player, Session as InhouseSession, Signup
from bot.db.session import get_session



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
            # The picker can sit open for minutes; signups may have closed meanwhile.
            session = await db.get(InhouseSession, view.session_id)
            if session is None or session.status != "recruiting":
                for child in view.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    content="🔒 Signups closed before you hit Done, so this wasn't saved.", view=view
                )
                return
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
                # Editing roles keeps your place in line; only (re)joining resets it.
                if existing.status != "playing":
                    existing.signed_up_at = datetime.utcnow()
                existing.status = "playing"
                existing.roles = roles_csv
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()  # double-click raced itself; the first click saved it

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
        title=f"🎮 Inhouse — Thursday {game_date.strftime('%b %d, %Y')} @ 9:00 PM ET",
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
