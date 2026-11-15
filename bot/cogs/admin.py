"""Admin commands: result reporting, channel config, rank syncing."""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal, Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, func, select, update

from bot.cogs.recruitment import RecruitmentCog
from bot.cogs.views.manual_match import (  # noqa: F401  (re-exported for callers of bot.cogs.admin)
    PICKUP_DUPLICATE_WINDOW,
    EditRosterView,
    ManualMatchModal,
    PickupSeriesModal,
    ResolveNamesModal,
    _recent_duplicate_pickup,
)
from bot.cogs.views.match_channels import _ScreenshotChannelsConfirmView
from bot.cogs.views.report import (  # noqa: F401  (re-exported for callers of bot.cogs.admin)
    ALL_UNREADABLE_MSG,
    ReportConfirmView,
    _ChampionModal,
    _commit_status_message,
    _GameSelect,
    _report_embed,
    _report_notes,
    _SlotPlayerSelect,
    _SlotSelect,
    _WinnerButton,
)
from bot.config import Config, ROLES, format_team_lines
from bot.db.models import Alias, GameStat, GuildConfig, Match, MatchPerformance, Player, ProposalSet, Rating, Session as InhouseSession, Signup
from bot.db.session import get_session
from bot.services.champions import resolve_champion
from bot.services.report_analysis import (
    GameResult,
    ReportState,
    _resolve_name,
    build_game_proposal,
)
from bot.services.ocr import MAX_IMAGE_BYTES, ParsedScoreboard, parse_scoreboard_image
from bot.services.names import (  # noqa: F401  (re-exported for callers of bot.cogs.admin)
    NameMatch,
    _build_candidate_index,
    _norm_name,
    _resolve_single_name,
    build_resolver,
    learn_alias,
    match_player_names,
    resolve_name_block,
)
from bot.services import results, seeding
from bot.services.ratings_lock import ratings_lock
from bot.services.sessions import find_current_session
from bot.services.rosters import (  # noqa: F401  (re-exported for callers of bot.cogs.admin)
    TEAM_NAMES,
    ManualMatchParseError,
    _MENTION_RE,
    _ROLE_LINE_RE,
    _team_no_for_label,
    format_roster_block,
    parse_manual_match,
    team_channel_name,
)
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


def _bot_tz(cog):
    """The bot's configured timezone (UTC if there's no config — e.g. when a test
    calls a cog method with a dummy self)."""
    tzname = getattr(getattr(cog, "config", None), "timezone", None) or "UTC"
    try:
        return pytz.timezone(tzname)
    except pytz.UnknownTimeZoneError:
        return pytz.utc




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

    async def _build_team_channels(
        self,
        guild: discord.Guild,
        guild_id: int,
        team1: dict[str, int],
        team2: dict[str, int],
        header: str,
    ) -> str:
        """Create-or-reuse the per-team roles + private channels for the given
        rosters (role -> discord_id) and return a human-readable summary (success,
        or the reason it couldn't). Does not send any message itself, so callers
        control the response."""
        async with get_session() as db:
            cfg = await db.get(GuildConfig, guild_id)

        category = (
            guild.get_channel(cfg.match_category_id)
            if cfg and cfg.match_category_id else None
        )
        if not isinstance(category, discord.CategoryChannel):
            return (
                "⚠️ No match category configured — run `/set-match-category`, "
                "then re-run `/match-channels`."
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
        return f"✅ {header} team channels ready:\n" + "\n".join(lines)

    async def _build_match_channels(
        self, guild: discord.Guild, guild_id: int, match_id: int
    ) -> str:
        """Team channels for a saved match's rosters. Shared by /match-channels and
        /manual-match's create_channels flag."""
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                return f"❌ Match {match_id} not found — channels not created."
            team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}
        return await self._build_team_channels(
            guild, guild_id, team1, team2, header=f"Match {match_id}"
        )

    @app_commands.command(
        name="match-channels",
        description="(admin) Create private per-team roles + channels from a match ID or a roster screenshot.",
    )
    @app_commands.describe(
        match_id="The match ID to build team channels for",
        screenshot="Instead of a match ID: an image showing both teams (roster table, lobby, or scoreboard)",
    )
    async def match_channels(
        self,
        interaction: discord.Interaction,
        match_id: Optional[int] = None,
        screenshot: Optional[discord.Attachment] = None,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        if (match_id is None) == (screenshot is None):
            await interaction.response.send_message(
                "Pass exactly one of `match_id` or `screenshot`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        if match_id is not None:
            summary = await self._build_match_channels(
                interaction.guild, interaction.guild_id, match_id
            )
            await interaction.followup.send(summary, ephemeral=True)
            return

        # --- Screenshot path: OCR the two teams, preview, confirm, then build ---
        guild = interaction.guild
        if guild is not None and not guild.chunked:
            try:
                await guild.chunk()
            except Exception:
                log.warning("match-channels: guild chunk failed; matching on cache", exc_info=True)
        resolve, labels = await self._build_resolver(guild)

        image = await screenshot.read()
        parsed = await asyncio.to_thread(parse_scoreboard_image, image)
        if not parsed.team1 and not parsed.team2:
            note = " ".join(parsed.notes) or "no players read"
            await interaction.followup.send(
                f"❌ Couldn't read teams from that image ({note}). "
                f"Use `/resolve-names` + `/manual-match` instead.", ephemeral=True
            )
            return

        team1, team2, preview, unresolved = self._resolve_screenshot_teams(
            parsed, resolve, labels
        )
        header = "**Teams read from the screenshot** — confirm to create channels/roles:\n"
        if parsed.notes:
            header += "⚠️ " + " ".join(parsed.notes) + "\n"
        if unresolved:
            header += (
                f"⚠️ {len(unresolved)} name(s) I couldn't match will be **skipped** — "
                f"fix with `/set-alias` and re-run, or confirm without them.\n"
            )
        view = _ScreenshotChannelsConfirmView(self, team1, team2, interaction.user.id)
        await interaction.followup.send(header + preview, view=view, ephemeral=True)

    def _resolve_screenshot_teams(self, parsed, resolve, labels):
        """Map OCR'd rows (scoreboard order = TOP→SUPPORT) to rosters. Returns
        (team1, team2, preview_text, unresolved_names)."""
        unresolved: list[str] = []
        rosters: dict[int, dict[str, int]] = {1: {}, 2: {}}
        lines: list[str] = []
        # The image lists teams in display order (left/top first), which may not
        # match TEAM_NAMES — orient by the header names when they're readable.
        rows1, rows2 = parsed.team1, parsed.team2
        if _team_no_for_label(parsed.team1_label) == 2 or _team_no_for_label(parsed.team2_label) == 1:
            rows1, rows2 = rows2, rows1
        for team_no, rows in ((1, rows1), (2, rows2)):
            lines.append(f"\n**{TEAM_NAMES[team_no]}**")
            for i, role in enumerate(ROLES):
                if i >= len(rows):
                    lines.append(f"• {role}: ❓ *(no row read)*")
                    continue
                did, shown = _resolve_name(rows[i].name_guess, resolve)
                if did is not None:
                    rosters[team_no][role] = did
                    lines.append(f"• {role}: {labels.get(did, shown)} (read: “{rows[i].name_guess}”)")
                else:
                    unresolved.append(rows[i].name_guess)
                    lines.append(f"• {role}: ❓ couldn't match “{rows[i].name_guess}”")
        return rosters[1], rosters[2], "\n".join(lines), unresolved

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
        return await build_resolver(guild)

    async def _learn_alias(self, name: str, discord_id: int) -> tuple[str, Optional[int]]:
        return await learn_alias(name, discord_id)

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
        msg = await self._rescore(match_id, interaction.user.id)
        await interaction.followup.send(msg, ephemeral=True)

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

        async def read_one(att: discord.Attachment) -> ParsedScoreboard:
            """One attachment -> ParsedScoreboard. Never raises: anything that stops
            the read becomes a note the admin sees on the confirm message."""
            ctype = (getattr(att, "content_type", None) or "").lower()
            size = getattr(att, "size", None) or 0
            if ctype and not ctype.startswith("image/"):
                return ParsedScoreboard(notes=[
                    f"“{att.filename}” is not an image ({ctype}) — enter the lineup manually."])
            if size > MAX_IMAGE_BYTES:
                return ParsedScoreboard(notes=[
                    f"“{att.filename}” is too large ({size / (1024 * 1024):.1f} MB; the limit is "
                    f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB) — re-save it smaller or as a JPEG."])
            try:
                data = await att.read()
            except Exception as e:
                log.warning("report: couldn't download %s", att.filename, exc_info=True)
                return ParsedScoreboard(notes=[
                    f"Couldn't download “{att.filename}” ({type(e).__name__}) — enter the lineup manually."])
            # Vision reads are blocking HTTP calls — run them off the event loop.
            return await asyncio.to_thread(parse_scoreboard_image, data)

        # Concurrently (one per uploaded game) so the bot stays responsive.
        parsed_list = await asyncio.gather(*(read_one(a) for a in attachments))
        proposals = [
            build_game_proposal(p, resolve, fixed_t1, fixed_t2) for p in parsed_list
        ]

        state = ReportState.from_proposals(match_id, proposals)
        view = ReportConfirmView(self, state, labels, game1.url, interaction.user.id)
        view.message = await interaction.followup.send(
            content=ALL_UNREADABLE_MSG if state.all_unreadable else None,
            embed=_report_embed(state, labels), view=view, ephemeral=True)

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
                'Invalid series score. Use "2-0", "2-1", "1-2", or "0-2" (team1-team2), '
                'or "1-0" / "0-1" for a single game.',
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

        status = await self._commit_result(match_id, team1_wins, team2_wins, None, interaction.user.id, None)
        if status != "ok":
            await interaction.followup.send(_commit_status_message(status, match_id), ephemeral=True)
            return
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

    @app_commands.command(name="sync-ranks", description="(admin) Refresh Riot rank for all linked players. Updates base_seed only.")
    async def sync_ranks(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self._refresh_all_base_seeds()
        except RiotAuthError:
            await interaction.followup.send(
                "❌ Riot API key rejected — nothing was changed. "
                "Update `RIOT_API_KEY` and run `/sync-ranks` again.",
                ephemeral=True,
            )
            return
        headline = (
            f"{'⚠️' if result['errors'] else '✅'} Synced {result['updated']} players · "
            f"updated base_seed on {result['rows_updated']} rating rows · "
            f"{result['skipped']} manually-seeded (skipped) · "
            f"{result['errors']} errors"
        )
        for chunk in seeding.format_sync_report(result, headline):
            await interaction.followup.send(chunk, ephemeral=True)

    async def _refresh_all_base_seeds(self) -> dict:
        """Refresh every linked player's base_seed from current Riot rank (see
        bot.services.seeding). Looked up on this cog by the recruitment close and
        the weekly scheduler job."""
        return await seeding.refresh_all_base_seeds(self.riot, self.opgg)

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
        from bot.db.session import backup_database
        try:
            await backup_database("reseed-all")
        except Exception:
            log.exception("reseed-all: pre-reseed backup failed; nothing changed")
            await interaction.followup.send(
                "❌ Couldn't write a database backup first, so nothing was changed. Check the bot log.",
                ephemeral=True,
            )
            return
        try:
            result = await self._refresh_all_base_seeds()
        except RiotAuthError:
            await interaction.followup.send("❌ Riot API key rejected — nothing was changed.", ephemeral=True)
            return
        headline = (
            f"{'⚠️' if result['errors'] else '✅'} Reseed complete · "
            f"refreshed base_seed for {result['updated']} players "
            f"({result['rows_updated']} rating rows) · "
            f"{result['skipped']} manually-seeded (skipped) · "
            f"inhouse_modifier preserved · "
            f"{result['errors']} errors"
        )
        for chunk in seeding.format_sync_report(result, headline):
            await interaction.followup.send(chunk, ephemeral=True)

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
            from bot.services.riot_client import RiotUnavailable
            try:
                data = await seeding.fetch_seed_data(
                    self.riot, self.opgg,
                    player.riot_puuid, player.riot_game_name, player.riot_tag_line, player.region
                )
            except RiotAuthError:
                await interaction.followup.send(
                    "❌ Riot API key rejected — nothing was changed.", ephemeral=True)
                return
            except RiotUnavailable:
                await interaction.followup.send(
                    "⚠️ Riot's API is unavailable right now, so nothing was changed"
                    + (" (the manual seed is still locked)" if was_manual else "")
                    + ". Try again in a few minutes.",
                    ephemeral=True,
                )
                return
            change = await seeding.apply_seed_data(db, player, data)
            rows = change["rows"]
            await db.commit()
        log.info("refresh-seed by %s: %s (was_manual=%s, rows=%s, %s -> %s: %s)", interaction.user.id,
                 user.id, was_manual, rows, change["old"], change["new"], change["reason"])
        unlock = "🔓 Cleared manual seed for" if was_manual else "🔄 Refreshed"
        if rows:
            await interaction.followup.send(
                f"{unlock} {user.mention} and re-derived base_seed: "
                f"{change['old'] if change['old'] is not None else 'none'} → **{change['new']}** "
                f"({change['reason']}; {rows} rating rows updated).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"{unlock} {user.mention}, but base_seed was left unchanged ({change['reason']}). "
                f"They'll be picked up by the next refresh once a rank is available.",
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
        # Snapshot first: this is the one command that can't be undone.
        from bot.db.session import backup_database
        try:
            backup = await backup_database("clear-matches")
        except Exception:
            log.exception("clear-matches: pre-delete backup failed; nothing deleted")
            await interaction.followup.send(
                "❌ Couldn't write a database backup first, so nothing was deleted. Check the bot log.",
                ephemeral=True,
            )
            return
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
            f"(elo back to base seed). Riot links and base seeds preserved."
            + (f"\nBackup taken first: `backups/{backup.name}`" if backup else ""),
            ephemeral=True,
        )

    # ---------- rating writes: thin wrappers over bot.services.results ----------
    # The report views, /pickup-series and the tests call these on the cog. The
    # only thing they take from it is the bot's timezone.

    async def _commit_result(
        self,
        match_id: int,
        team1_wins: int,
        team2_wins: int,
        screenshot_url: str | None,
        admin_id: int,
        parsed,
        db=None,
    ) -> str:
        return await results.commit_result(
            match_id, team1_wins, team2_wins, screenshot_url, admin_id, parsed,
            db=db, tz=_bot_tz(self))

    async def _commit_games(
        self,
        match_id: int,
        games: list["GameResult"],
        screenshot_url: str | None,
        admin_id: int,
        db=None,
    ) -> str:
        return await results.commit_games(
            match_id, games, screenshot_url, admin_id, db=db, tz=_bot_tz(self))

    async def _revert_result(self, match_id: int, db=None) -> tuple[str, int, str]:
        return await results.revert_result(match_id, db=db, tz=_bot_tz(self))

    async def _games_from_gamestats(self, db, match) -> list:
        return await results.games_from_gamestats(db, match)

    async def _rescore(self, match_id: int, admin_id: int) -> str:
        return await results.rescore(match_id, admin_id, tz=_bot_tz(self))

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
        description="(admin) Close signups now and DM the owner the 3 team options.",
    )
    @app_commands.describe(session_id="Session ID. Defaults to the current open session.")
    async def test_trigger_close(self, interaction: discord.Interaction, session_id: Optional[int] = None):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        cog = self.bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            await interaction.followup.send("Recruitment cog not loaded.", ephemeral=True)
            return

        async with get_session() as db:
            if session_id is not None:
                session = await db.get(InhouseSession, session_id)
            else:
                session = await find_current_session(db, ("recruiting", "closed"), cog._today())
            if session is None:
                await interaction.followup.send(
                    "No open session to close. Pass `session_id:` to pick one.", ephemeral=True)
                return
            sid = session.id

        try:
            outcome = await cog.close_signups_and_match(sid)
        except Exception as e:
            log.exception("test-trigger-close failed")
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)
            return
        messages = {
            "matched": f"✅ Session #{sid} closed. The owner was DM'd the 3 options.",
            "dm_failed": f"⚠️ Session #{sid} closed and options were generated, but the owner DM "
                         f"failed. Fix DMs, then `/resend-options session_id:{sid}`.",
            "waiting": f"⏳ Session #{sid} can't be matched yet (not enough players, or roles "
                       f"can't be covered). It was left as is — see the recruit channel.",
            "not_open": f"Session #{sid} isn't open (already matched, completed or cancelled).",
            "no_channel": f"❌ I can't reach Session #{sid}'s recruit channel right now. Nothing changed.",
        }
        await interaction.followup.send(messages.get(outcome, outcome), ephemeral=True)

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
                # Fallback: the current session (recruiting, closed, or matched) —
                # never a stale one whose game date has passed. An old session
                # left open used to capture new matches (one got attached to a
                # session two months earlier).
                today = datetime.now(pytz.timezone(self.config.timezone)).date()
                session = await find_current_session(db, ("recruiting", "closed", "matched"), today)
                if session is None:
                    await interaction.response.send_message(
                        "No current session. Use `/recruit-now` first, or pass `session_id:` / "
                        "`game_date:` to target an older one.",
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
