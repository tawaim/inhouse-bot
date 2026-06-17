"""Riot account linking with admin approval.

Flow:
  /link <riot_id>           -> creates a 'pending' player row, posts approval
                               prompt to admin channel with Approve/Reject buttons.
                               Re-linking is blocked until /unlink runs.
  /link-user @user <riot_id> -> admin-only, skips approval, goes straight to 'approved'
  /unlink                    -> user removes their own link (works for pending or approved)
  /profile [@user]           -> shows linked Riot info + per-role inhouse elo
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.config import Config, ROLES
from bot.db.models import MatchPerformance, Player, Rating
from bot.db.session import get_session
from bot.services.elo import INHOUSE_ROLE, seed_from_past_season, seed_from_rank
from bot.services.opgg_client import OpggClient
from bot.services.riot_client import RiotAccount, RiotAuthError, RiotClient

log = logging.getLogger(__name__)


class LinkApprovalView(discord.ui.View):
    """Persistent view shown in the admin channel for each pending /link request.

    Buttons survive bot restarts because we set timeout=None and use stable
    custom_ids. Button handlers re-fetch state from the DB rather than relying
    on instance attributes, since after a restart the view is re-instantiated
    with placeholder values.
    """

    def __init__(self, target_discord_id: int, riot_id_display: str, config: Config, riot: RiotClient, opgg: OpggClient, bot: commands.Bot):
        super().__init__(timeout=None)
        self.target_discord_id = target_discord_id
        self.riot_id_display = riot_id_display
        self.config = config
        self.riot = riot
        self.opgg = opgg
        self.bot = bot

    async def _is_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.config.owner_discord_id

    @staticmethod
    def _extract_target_from_message(message: discord.Message) -> Optional[int]:
        """Pull the target Discord ID out of the embed footer ('Discord ID: 123 · ...')."""
        if not message.embeds:
            return None
        footer = message.embeds[0].footer.text or ""
        if "Discord ID:" not in footer:
            return None
        try:
            return int(footer.split("Discord ID:")[1].split("·")[0].strip())
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _extract_riot_id_from_message(message: discord.Message) -> str:
        """Pull the Riot ID display string out of the embed description."""
        if not message.embeds or not message.embeds[0].description:
            return "(unknown)"
        # Description format: "<@id> wants to link...\n**GameName#TAG**"
        for line in message.embeds[0].description.splitlines():
            line = line.strip()
            if line.startswith("**") and line.endswith("**") and "#" in line:
                return line.strip("*")
        return "(unknown)"

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅", custom_id="link_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_owner(interaction):
            await interaction.response.send_message("Bot owner only.", ephemeral=True)
            return
        await interaction.response.defer()

        target_id = self._extract_target_from_message(interaction.message)
        riot_id_display = self._extract_riot_id_from_message(interaction.message)
        if target_id is None:
            await interaction.followup.send("Couldn't read target user from the embed.", ephemeral=True)
            return

        async with get_session() as db:
            player = await db.get(Player, target_id)
            if player is None or player.link_status != "pending":
                await interaction.followup.send(
                    "This link is no longer pending (already approved/rejected/unlinked).",
                    ephemeral=True,
                )
                return
            puuid = player.riot_puuid
            game_name = player.riot_game_name
            tag_line = player.riot_tag_line
            region = player.region
            if not puuid:
                await interaction.followup.send("Internal error: pending player has no PUUID.", ephemeral=True)
                return

        try:
            rank = await self.riot.get_solo_rank(puuid)
            primary_role = await self.riot.infer_primary_role(puuid)
        except RiotAuthError:
            await interaction.followup.send(
                "Riot API key rejected. Approval not committed — fix the key and try again.",
                ephemeral=True,
            )
            return
        # Unranked this split? Fall back to OP.GG's most recent past-season rank.
        past = None
        if rank is None:
            past = await self.opgg.get_past_season_rank(game_name, tag_line, region)

        async with get_session() as db:
            player = await db.get(Player, target_id)
            if player is None:
                return
            # Detect whether this is a re-link of a DIFFERENT account
            # (vs same account). If different, we'll reseed elo on existing
            # Rating rows while preserving games_played.
            new_puuid = player.riot_puuid  # already populated by /link
            account_changed = (
                player.previous_riot_puuid is not None
                and player.previous_riot_puuid != new_puuid
            )

            player.solo_tier = rank.tier if rank else None
            player.solo_rank = rank.rank if rank else None
            player.solo_lp = rank.league_points if rank else None
            player.primary_role = primary_role
            player.riot_last_synced = datetime.utcnow()
            player.link_status = "approved"

            # Seed elo: prefer current rank, fall back to OP.GG past-season rank
            # (division-aware, with recency decay), default to 1200 if neither.
            if rank:
                seed_elo = seed_from_rank(player.solo_tier, player.solo_rank)
            elif past:
                seed_elo = seed_from_past_season(past.tier, past.division, past.seasons_elapsed)
            else:
                seed_elo = 1200
            # Only re-seed an existing base_seed from real rank/past data, never
            # from the 1200 default (mirrors _refresh_all_base_seeds).
            seed_from_data = bool(rank or past)
            player.last_synced_seed_elo = seed_elo

            for role in [*ROLES, INHOUSE_ROLE]:
                existing = await db.get(Rating, (target_id, role))
                if existing is None:
                    # First time linking — create the row with base_seed=seed,
                    # modifier=0, displayed elo = seed
                    db.add(Rating(
                        discord_id=target_id,
                        role=role,
                        elo=seed_elo,
                        base_seed=seed_elo,
                        inhouse_modifier=0,
                        games_played=0,
                    ))
                elif (seed_from_data or account_changed) and not player.manual_seed:
                    # Re-seed base_seed on any re-link with real rank/past data
                    # (not just an account swap), keeping modifier and
                    # games_played intact. Recompute displayed elo. Skipped when
                    # an admin has set a manual seed (/set-seed) — that sticks
                    # until /refresh-seed clears it.
                    existing.base_seed = seed_elo
                    existing.elo = existing.base_seed + existing.inhouse_modifier
                # else: same account/no rank data, or a manual seed is locked in

            # Now that we've used previous_riot_puuid, clear it
            player.previous_riot_puuid = None
            await db.commit()

        for child in self.children:
            child.disabled = True
        if rank:
            rank_str = f"{rank.tier} {rank.rank} ({rank.league_points} LP)"
        elif past:
            ago = f"{past.seasons_elapsed} season(s) ago" if past.seasons_elapsed else "this past season"
            rank_str = (
                f"Unranked this split — last ranked **{past.tier} {past.division}** "
                f"({ago}); seeded at **{seed_elo}** after recency decay"
            )
        else:
            rank_str = "Unranked (seeded at default 1200)"
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.green()
        embed.add_field(
            name="Approved",
            value=f"by {interaction.user.mention} · seeded as {rank_str}",
            inline=False,
        )
        await interaction.message.edit(embed=embed, view=self)

        await _notify_user(
            self.bot,
            target_id,
            self.config.discord_guild_id,
            f"✅ Your Riot link **{riot_id_display}** was approved by {interaction.user.display_name}. "
            f"You're seeded as **{rank_str}**. Use `/profile` to see your stats.",
        )

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="❌", custom_id="link_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_owner(interaction):
            await interaction.response.send_message("Bot owner only.", ephemeral=True)
            return
        target_id = self._extract_target_from_message(interaction.message)
        riot_id_display = self._extract_riot_id_from_message(interaction.message)
        if target_id is None:
            await interaction.response.send_message("Couldn't read target user from the embed.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RejectReasonModal(target_id, riot_id_display, self.config, self.bot, self)
        )


class RejectReasonModal(discord.ui.Modal, title="Reject Link Request"):
    reason = discord.ui.TextInput(
        label="Reason (shown to user)",
        placeholder="e.g., 'That's not your account' or 'Wrong Riot ID, retry'",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=400,
    )

    def __init__(
        self,
        target_discord_id: int,
        riot_id_display: str,
        config: Config,
        bot: commands.Bot,
        view: LinkApprovalView,
    ):
        super().__init__()
        self.target_discord_id = target_discord_id
        self.riot_id_display = riot_id_display
        self.config = config
        self.bot = bot
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with get_session() as db:
            player = await db.get(Player, self.target_discord_id)
            if player is None or player.link_status != "pending":
                await interaction.followup.send("Already resolved.", ephemeral=True)
                return
            await db.delete(player)
            await db.commit()

        for child in self.view.children:
            child.disabled = True
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.red()
        embed.add_field(
            name="Rejected",
            value=f"by {interaction.user.mention}\n**Reason:** {self.reason.value}",
            inline=False,
        )
        await interaction.message.edit(embed=embed, view=self.view)

        await _notify_user(
            self.bot,
            self.target_discord_id,
            self.config.discord_guild_id,
            f"❌ Your Riot link **{self.riot_id_display}** was rejected.\n"
            f"**Reason:** {self.reason.value}\n"
            f"You can run `/link` again with a corrected Riot ID.",
        )


async def _notify_user(bot: commands.Bot, user_id: int, guild_id: int, message: str) -> None:
    """DM the user. If DMs are disabled, just log it — we no longer have an
    admin channel to fall back to."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(message)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
        log.warning("Could not DM user %s (%s): %s", user_id, type(e).__name__, e)


class LinkingCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config, riot: RiotClient, opgg: OpggClient):
        self.bot = bot
        self.config = config
        self.riot = riot
        self.opgg = opgg

    async def cog_load(self) -> None:
        """Re-register the persistent view so buttons on old messages work after restart."""
        # Discord matches buttons by custom_id; the placeholder args are never read at click time.
        self.bot.add_view(LinkApprovalView(0, "", self.config, self.riot, self.opgg, self.bot))

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.name == self.config.admin_role_name for r in interaction.user.roles)

    async def _validate_riot_id(
        self, raw: str
    ) -> tuple[Optional[RiotAccount], Optional[str]]:
        """Returns (account, error_message). Exactly one is non-None on success/failure."""
        if "#" not in raw:
            return None, "Riot ID must be in the form `GameName#TAG`."
        game_name, _, tag_line = raw.partition("#")
        game_name = game_name.strip()
        tag_line = tag_line.strip()
        if not game_name or not tag_line:
            return None, "Both the name and tag must be non-empty."
        try:
            account = await self.riot.get_account_by_riot_id(game_name, tag_line)
        except RiotAuthError:
            return None, "Riot API rejected our key. Ping an admin."
        if account is None:
            return None, f"Couldn't find Riot account `{game_name}#{tag_line}`."
        return account, None

    @app_commands.command(name="link", description="Link your Riot ID. Requires admin approval.")
    @app_commands.describe(riot_id="Your Riot ID in the form GameName#TAG")
    async def link(self, interaction: discord.Interaction, riot_id: str):
        await interaction.response.defer(ephemeral=True)

        # Block re-linking
        async with get_session() as db:
            existing = await db.get(Player, interaction.user.id)
            if existing and existing.riot_puuid:
                if existing.link_status == "pending":
                    await interaction.followup.send(
                        f"You already have a pending link for **{existing.riot_game_name}#{existing.riot_tag_line}** "
                        f"awaiting admin approval. Run `/unlink` to cancel and try a different account.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"You're already linked as **{existing.riot_game_name}#{existing.riot_tag_line}**. "
                        f"Run `/unlink` first if you want to switch accounts.",
                        ephemeral=True,
                    )
                return

        account, err = await self._validate_riot_id(riot_id)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        # Make sure account isn't already claimed by another user
        async with get_session() as db:
            stmt = select(Player).where(
                Player.riot_puuid == account.puuid,
                Player.discord_id != interaction.user.id,
            )
            other = (await db.execute(stmt)).scalar_one_or_none()
            if other is not None:
                await interaction.followup.send(
                    f"That Riot account is already linked to <@{other.discord_id}>. "
                    f"If this is a mistake, ask them to run `/unlink`.",
                    ephemeral=True,
                )
                return

            player = await db.get(Player, interaction.user.id)
            if player is None:
                player = Player(discord_id=interaction.user.id)
                db.add(player)
            player.riot_game_name = account.game_name
            player.riot_tag_line = account.tag_line
            player.riot_puuid = account.puuid
            player.region = self.config.riot_region
            player.link_status = "pending"
            await db.commit()

        riot_id_display = f"{account.game_name}#{account.tag_line}"

        embed = discord.Embed(
            title="🔗 Link Request",
            description=(
                f"<@{interaction.user.id}> wants to link their Discord account to:\n"
                f"**{riot_id_display}**"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Discord ID: {interaction.user.id} · PUUID: {account.puuid[:16]}…")
        view = LinkApprovalView(
            interaction.user.id, riot_id_display, self.config, self.riot, self.opgg, self.bot
        )

        # DM the owner. If we can't reach them, roll back the pending row and
        # tell the user to retry — better than leaving the request orphaned.
        try:
            owner = self.bot.get_user(self.config.owner_discord_id) or \
                    await self.bot.fetch_user(self.config.owner_discord_id)
            await owner.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
            log.error(
                "Couldn't DM owner %s for link request from %s: %s",
                self.config.owner_discord_id, interaction.user.id, e,
            )
            async with get_session() as db:
                player = await db.get(Player, interaction.user.id)
                if player:
                    await db.delete(player)
                    await db.commit()
            await interaction.followup.send(
                "❌ Couldn't reach the bot owner to send your request. "
                "Try again later or contact them directly.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"⏳ Link request submitted for **{riot_id_display}**. "
            f"You'll be DM'd when it's approved or rejected.",
            ephemeral=True,
        )

    @app_commands.command(name="link-user", description="(admin) Link a Discord member to a Riot ID, no approval needed.")
    @app_commands.describe(member="The Discord member to link", riot_id="Their Riot ID in the form GameName#TAG")
    async def link_user(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        riot_id: str,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        async with get_session() as db:
            existing = await db.get(Player, member.id)
            if existing and existing.riot_puuid and existing.link_status == "approved":
                await interaction.followup.send(
                    f"{member.display_name} is already linked as "
                    f"**{existing.riot_game_name}#{existing.riot_tag_line}**. "
                    f"They must `/unlink` first.",
                    ephemeral=True,
                )
                return

        account, err = await self._validate_riot_id(riot_id)
        if err:
            await interaction.followup.send(err, ephemeral=True)
            return

        async with get_session() as db:
            stmt = select(Player).where(
                Player.riot_puuid == account.puuid,
                Player.discord_id != member.id,
            )
            other = (await db.execute(stmt)).scalar_one_or_none()
            if other is not None:
                await interaction.followup.send(
                    f"That Riot account is already linked to <@{other.discord_id}>.",
                    ephemeral=True,
                )
                return

        try:
            rank = await self.riot.get_solo_rank(account.puuid)
            primary_role = await self.riot.infer_primary_role(account.puuid)
        except RiotAuthError:
            await interaction.followup.send("Riot API rejected our key.", ephemeral=True)
            return
        past = None
        if rank is None:
            past = await self.opgg.get_past_season_rank(
                account.game_name, account.tag_line, self.config.riot_region
            )

        async with get_session() as db:
            player = await db.get(Player, member.id)
            if player is None:
                player = Player(discord_id=member.id)
                db.add(player)
            account_changed = (
                player.previous_riot_puuid is not None
                and player.previous_riot_puuid != account.puuid
            )
            player.riot_game_name = account.game_name
            player.riot_tag_line = account.tag_line
            player.riot_puuid = account.puuid
            player.region = self.config.riot_region
            player.solo_tier = rank.tier if rank else None
            player.solo_rank = rank.rank if rank else None
            player.solo_lp = rank.league_points if rank else None
            player.primary_role = primary_role
            player.riot_last_synced = datetime.utcnow()
            player.link_status = "approved"

            if rank:
                seed_elo = seed_from_rank(player.solo_tier, player.solo_rank)
            elif past:
                seed_elo = seed_from_past_season(past.tier, past.division, past.seasons_elapsed)
            else:
                seed_elo = 1200
            # Did the seed come from real rank/past data, or just the 1200 default?
            # Only real data should overwrite an existing base_seed (mirrors
            # _refresh_all_base_seeds), so a transient "no rank found" never
            # clobbers a good seed.
            seed_from_data = bool(rank or past)
            player.last_synced_seed_elo = seed_elo

            for role in [*ROLES, INHOUSE_ROLE]:
                existing_rating = await db.get(Rating, (member.id, role))
                if existing_rating is None:
                    db.add(Rating(
                        discord_id=member.id,
                        role=role,
                        elo=seed_elo,
                        base_seed=seed_elo,
                        inhouse_modifier=0,
                        games_played=0,
                    ))
                elif (seed_from_data or account_changed) and not player.manual_seed:
                    # Re-seed from current rank/past on any re-link (not just an
                    # account swap), preserving the inhouse_modifier (W/L).
                    # Skipped when an admin has locked in a manual seed
                    # (/set-seed) — only /refresh-seed clears that.
                    existing_rating.base_seed = seed_elo
                    existing_rating.elo = existing_rating.base_seed + existing_rating.inhouse_modifier
            player.previous_riot_puuid = None
            await db.commit()

        if rank:
            rank_str = f"{rank.tier} {rank.rank} ({rank.league_points} LP)"
        elif past:
            rank_str = f"Unranked (last ranked {past.tier} {past.division} → seeded {seed_elo})"
        else:
            rank_str = "Unranked (seeded 1200)"
        await interaction.followup.send(
            f"✅ Linked {member.mention} → **{account.game_name}#{account.tag_line}** · "
            f"Solo Q: **{rank_str}** · Main: **{primary_role or 'unknown'}**",
            ephemeral=True,
        )

    @app_commands.command(name="unlink", description="Remove your Riot account link.")
    async def unlink(self, interaction: discord.Interaction):
        async with get_session() as db:
            player = await db.get(Player, interaction.user.id)
            if player is None or not player.riot_puuid:
                await interaction.response.send_message("You have no linked Riot account.", ephemeral=True)
                return
            was_pending = player.link_status == "pending"
            # Remember the unlinked PUUID so we can detect "re-linking same account"
            # vs "switched accounts" on the next /link.
            player.previous_riot_puuid = player.riot_puuid
            player.riot_puuid = None
            player.riot_game_name = None
            player.riot_tag_line = None
            player.solo_tier = None
            player.solo_rank = None
            player.solo_lp = None
            player.last_synced_seed_elo = None
            player.link_status = "approved"  # so a future /link doesn't see them as pending
            await db.commit()

        msg = "Riot account unlinked."
        if was_pending:
            msg += " (Pending request cancelled.)"
        msg += " Inhouse games_played preserved; elo will be reseeded if you re-link a different account."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="profile", description="Show your inhouse profile.")
    @app_commands.describe(user="Whose profile to view (defaults to you)")
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        async with get_session() as db:
            player = await db.get(Player, target.id)
            ratings = (await db.execute(
                select(Rating).where(Rating.discord_id == target.id)
            )).scalars().all()

        if player is None:
            await interaction.response.send_message(
                f"{target.display_name} hasn't linked yet. Use `/link` to get started.", ephemeral=True
            )
            return

        embed = discord.Embed(title=f"{target.display_name}'s Inhouse Profile", color=discord.Color.blurple())

        if player.link_status == "pending":
            embed.add_field(
                name="⏳ Status",
                value=f"Pending admin approval for **{player.riot_game_name}#{player.riot_tag_line}**",
                inline=False,
            )
        elif player.riot_game_name:
            embed.add_field(
                name="Riot ID",
                value=f"{player.riot_game_name}#{player.riot_tag_line}",
                inline=True,
            )
        if player.solo_tier:
            embed.add_field(
                name="Solo Queue",
                value=f"{player.solo_tier} {player.solo_rank} · {player.solo_lp} LP",
                inline=True,
            )
        if player.primary_role:
            embed.add_field(name="Main Role", value=player.primary_role, inline=True)

        # Pull per-role performance stats
        async with get_session() as db:
            perfs = (await db.execute(
                select(MatchPerformance).where(MatchPerformance.discord_id == target.id)
            )).scalars().all()
        stats_by_role = _aggregate_stats(perfs)
        overall = _aggregate_stats(perfs, all_roles=True)

        # Overall summary line (if any games played)
        if overall["games"] > 0:
            embed.add_field(
                name="📊 Overall",
                value=_format_stat_line(overall),
                inline=False,
            )

        # Per-role breakdown: stats + elo. Also show INHOUSE elo prominently.
        if ratings:
            ratings_by_role = {r.role: r for r in ratings}

            # Surface the INHOUSE elo at the top
            overall_rating = ratings_by_role.get(INHOUSE_ROLE)
            if overall_rating:
                modifier_str = (
                    f"({overall_rating.inhouse_modifier:+d})"
                    if overall_rating.inhouse_modifier != 0
                    else ""
                )
                embed.add_field(
                    name="🏆 Inhouse Elo",
                    value=(
                        f"**{overall_rating.elo}** {modifier_str}\n"
                        f"Base from rank: {overall_rating.base_seed}\n"
                        f"Inhouse W/L: {overall_rating.inhouse_modifier:+d}"
                    ),
                    inline=False,
                )

            lines = []
            for role in ROLES:
                r = ratings_by_role.get(role)
                stats = stats_by_role.get(role, {"games": 0, "wins": 0, "losses": 0, "k": 0, "d": 0, "a": 0})
                if r:
                    mod_str = f"({r.inhouse_modifier:+d})" if r.inhouse_modifier != 0 else ""
                    elo_label = f"Elo **{r.elo}** {mod_str}".strip()
                    if stats["games"] == 0:
                        lines.append(f"**{role}** — *no games yet* · {elo_label}")
                    else:
                        lines.append(f"**{role}** — {_format_stat_line(stats)} · {elo_label}")
            embed.add_field(name="By Role", value="\n".join(lines) or "No ratings yet", inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="admin-list-players",
        description="(admin) Show all linked players with their rank and inhouse elo.",
    )
    @app_commands.describe(
        filter="Which players to show (default: approved)",
    )
    async def admin_list_players(
        self,
        interaction: discord.Interaction,
        filter: Optional[str] = "approved",
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        if filter not in ("approved", "pending", "all"):
            await interaction.response.send_message(
                "filter must be one of: approved, pending, all", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        async with get_session() as db:
            stmt = select(Player).where(Player.riot_puuid.is_not(None))
            if filter == "approved":
                stmt = stmt.where(Player.link_status == "approved")
            elif filter == "pending":
                stmt = stmt.where(Player.link_status == "pending")
            stmt = stmt.order_by(Player.riot_game_name.asc())
            players = (await db.execute(stmt)).scalars().all()

            if not players:
                await interaction.followup.send(
                    f"No players found (filter: {filter}).", ephemeral=True
                )
                return

            # Use the INHOUSE row's games_played (it's incremented every game,
            # so it equals the total inhouse games)
            from bot.db.models import Rating
            rating_stmt = (
                select(Rating.discord_id, Rating.games_played)
                .where(Rating.role == INHOUSE_ROLE)
            )
            games_by_player = dict((await db.execute(rating_stmt)).all())

        # Build output. Discord embed field values cap at 1024 chars; if the list
        # is long we split across multiple fields.
        lines: list[str] = []
        for p in players:
            mention = f"<@{p.discord_id}>"
            riot = f"`{p.riot_game_name}#{p.riot_tag_line}`"
            rank = (
                f"{p.solo_tier} {p.solo_rank}" if p.solo_tier
                else "Unranked"
            )
            role = p.primary_role or "?"
            games = games_by_player.get(p.discord_id, 0) or 0
            status_marker = "⏳ " if p.link_status == "pending" else ""
            lines.append(
                f"{status_marker}{mention} · {riot} · {rank} · main: {role} · {games} inhouse games"
            )

        title_filter = {
            "approved": "Approved",
            "pending": "Pending Approval",
            "all": "All",
        }[filter]
        embed = discord.Embed(
            title=f"📋 Linked Players ({title_filter}) — {len(players)} total",
            color=discord.Color.blurple(),
        )

        # Pack lines into fields, respecting Discord's 1024 char/field cap
        chunks: list[str] = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 1000:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        # Discord allows up to 25 fields per embed; in practice we'll hit message
        # length limits long before that, but cap defensively.
        for i, chunk in enumerate(chunks[:25]):
            embed.add_field(
                name=f"Players {i+1}/{len(chunks)}" if len(chunks) > 1 else "\u200b",
                value=chunk,
                inline=False,
            )

        if len(chunks) > 25:
            embed.set_footer(text=f"Showing first 25 chunks; {len(players)} players total.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="admin-list-elos",
        description="(admin) Show every linked player and all 6 elos (TOP/JG/MID/BOT/SUP/INHOUSE).",
    )
    @app_commands.describe(sort_by="What to sort the table by (default: inhouse elo descending)")
    async def admin_list_elos(
        self,
        interaction: discord.Interaction,
        sort_by: Optional[str] = "inhouse",
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        sort_by = (sort_by or "inhouse").lower()
        valid_sorts = {"inhouse", "top", "jungle", "mid", "bot", "support", "name"}
        if sort_by not in valid_sorts:
            await interaction.response.send_message(
                f"sort_by must be one of: {', '.join(sorted(valid_sorts))}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            players = (await db.execute(
                select(Player).where(
                    Player.riot_puuid.is_not(None),
                    Player.link_status == "approved",
                ).order_by(Player.riot_game_name.asc())
            )).scalars().all()
            ratings = (await db.execute(select(Rating))).scalars().all()

        if not players:
            await interaction.followup.send("No linked players yet.", ephemeral=True)
            return

        # Index ratings: discord_id -> {role: (elo, modifier)}
        elos_by_player: dict[int, dict[str, tuple[int, int]]] = {}
        for r in ratings:
            elos_by_player.setdefault(r.discord_id, {})[r.role] = (r.elo, r.inhouse_modifier)

        # Build rows: (name, top, jg, mid, bot, sup, inhouse, ih_mod)
        rows = []
        for p in players:
            e = elos_by_player.get(p.discord_id, {})
            name = p.riot_game_name or f"<@{p.discord_id}>"
            ih_elo, ih_mod = e.get("INHOUSE", (1200, 0))
            rows.append({
                "name": name,
                "top": e.get("TOP", (1200, 0))[0],
                "jungle": e.get("JUNGLE", (1200, 0))[0],
                "mid": e.get("MID", (1200, 0))[0],
                "bot": e.get("BOT", (1200, 0))[0],
                "support": e.get("SUPPORT", (1200, 0))[0],
                "inhouse": ih_elo,
                "ih_mod": ih_mod,
            })

        if sort_by == "name":
            rows.sort(key=lambda r: r["name"].lower())
        else:
            rows.sort(key=lambda r: r[sort_by], reverse=True)

        # Fixed-width table inside a code block. Last column shows the +/-
        # modifier on the INHOUSE rating so you can see how each player has
        # done in inhouses regardless of solo queue rank changes.
        header = f"{'Player':<14} {'TOP':>5} {'JG':>5} {'MID':>5} {'BOT':>5} {'SUP':>5} {'IH':>5} {'IH±':>6}"
        sep = "-" * len(header)
        lines = [header, sep]
        for r in rows:
            short_name = (r["name"][:13] + "…") if len(r["name"]) > 14 else r["name"]
            mod_str = f"{r['ih_mod']:+d}" if r['ih_mod'] != 0 else "0"
            lines.append(
                f"{short_name:<14} "
                f"{r['top']:>5} {r['jungle']:>5} {r['mid']:>5} "
                f"{r['bot']:>5} {r['support']:>5} {r['inhouse']:>5} {mod_str:>6}"
            )

        # Discord message limit is 2000 chars. Split into chunks if needed.
        # Each line is ~50 chars; ~35 players fit in one message.
        chunks: list[str] = []
        current_lines = [header, sep]
        current_len = sum(len(s) + 1 for s in current_lines)
        for line in lines[2:]:
            if current_len + len(line) + 1 > 1900:
                chunks.append("```\n" + "\n".join(current_lines) + "\n```")
                current_lines = [header, sep, line]
                current_len = sum(len(s) + 1 for s in current_lines)
            else:
                current_lines.append(line)
                current_len += len(line) + 1
        if current_lines:
            chunks.append("```\n" + "\n".join(current_lines) + "\n```")

        prefix = f"📊 Elos for {len(rows)} linked players (sorted by {sort_by}):\n"
        await interaction.followup.send(prefix + chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)



def _aggregate_stats(perfs: list, all_roles: bool = False) -> dict | dict[str, dict]:
    """Aggregate match performances into per-role stats dicts.
    If all_roles=True, returns a single dict aggregating across all roles.
    Otherwise returns dict[role, dict].
    """
    if all_roles:
        bucket = {"games": 0, "wins": 0, "losses": 0, "k": 0, "d": 0, "a": 0}
        for p in perfs:
            bucket["games"] += 1
            if p.won:
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1
            bucket["k"] += p.kills or 0
            bucket["d"] += p.deaths or 0
            bucket["a"] += p.assists or 0
        return bucket

    by_role: dict[str, dict] = {}
    for p in perfs:
        b = by_role.setdefault(p.role, {"games": 0, "wins": 0, "losses": 0, "k": 0, "d": 0, "a": 0})
        b["games"] += 1
        if p.won:
            b["wins"] += 1
        else:
            b["losses"] += 1
        b["k"] += p.kills or 0
        b["d"] += p.deaths or 0
        b["a"] += p.assists or 0
    return by_role


def _format_stat_line(stats: dict) -> str:
    """Render a stats dict as a one-line string. Handles missing KDA gracefully."""
    games = stats["games"]
    if games == 0:
        return "no series"
    wins, losses = stats["wins"], stats["losses"]
    wr = (wins / games * 100) if games else 0
    line = f"{games} series · {wins}W-{losses}L ({wr:.0f}%)"
    if stats["k"] or stats["d"] or stats["a"]:
        avg_k = stats["k"] / games
        avg_d = stats["d"] / games
        avg_a = stats["a"] / games
        kda = (avg_k + avg_a) / max(avg_d, 1.0)
        line += f" · {avg_k:.1f}/{avg_d:.1f}/{avg_a:.1f} ({kda:.1f} KDA)"
    return line
