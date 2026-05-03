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
from bot.db.models import Player, Rating
from bot.db.session import get_session
from bot.services.elo import seed_from_rank
from bot.services.riot_client import RiotAccount, RiotAuthError, RiotClient

log = logging.getLogger(__name__)


class LinkApprovalView(discord.ui.View):
    """Persistent view shown in the admin channel for each pending /link request.

    Buttons survive bot restarts because we set timeout=None and use stable
    custom_ids. Button handlers re-fetch state from the DB rather than relying
    on instance attributes, since after a restart the view is re-instantiated
    with placeholder values.
    """

    def __init__(self, target_discord_id: int, riot_id_display: str, config: Config, riot: RiotClient, bot: commands.Bot):
        super().__init__(timeout=None)
        self.target_discord_id = target_discord_id
        self.riot_id_display = riot_id_display
        self.config = config
        self.riot = riot
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

        async with get_session() as db:
            player = await db.get(Player, target_id)
            if player is None:
                return
            player.solo_tier = rank.tier if rank else None
            player.solo_rank = rank.rank if rank else None
            player.solo_lp = rank.league_points if rank else None
            player.primary_role = primary_role
            player.riot_last_synced = datetime.utcnow()
            player.link_status = "approved"

            seed_mu, seed_sigma = seed_from_rank(player.solo_tier, player.solo_rank)
            for role in ROLES:
                existing = await db.get(Rating, (target_id, role))
                if existing is None:
                    db.add(Rating(
                        discord_id=target_id,
                        role=role,
                        mu=seed_mu,
                        sigma=seed_sigma,
                        games_played=0,
                    ))
            await db.commit()

        for child in self.children:
            child.disabled = True
        rank_str = f"{rank.tier} {rank.rank} ({rank.league_points} LP)" if rank else "Unranked"
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
    def __init__(self, bot: commands.Bot, config: Config, riot: RiotClient):
        self.bot = bot
        self.config = config
        self.riot = riot

    async def cog_load(self) -> None:
        """Re-register the persistent view so buttons on old messages work after restart."""
        # Discord matches buttons by custom_id; the placeholder args are never read at click time.
        self.bot.add_view(LinkApprovalView(0, "", self.config, self.riot, self.bot))

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
            interaction.user.id, riot_id_display, self.config, self.riot, self.bot
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

        async with get_session() as db:
            player = await db.get(Player, member.id)
            if player is None:
                player = Player(discord_id=member.id)
                db.add(player)
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

            seed_mu, seed_sigma = seed_from_rank(player.solo_tier, player.solo_rank)
            for role in ROLES:
                existing_rating = await db.get(Rating, (member.id, role))
                if existing_rating is None:
                    db.add(Rating(
                        discord_id=member.id,
                        role=role,
                        mu=seed_mu,
                        sigma=seed_sigma,
                        games_played=0,
                    ))
            await db.commit()

        rank_str = f"{rank.tier} {rank.rank} ({rank.league_points} LP)" if rank else "Unranked"
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
            player.riot_puuid = None
            player.riot_game_name = None
            player.riot_tag_line = None
            player.solo_tier = None
            player.solo_rank = None
            player.solo_lp = None
            player.link_status = "approved"  # so a future /link doesn't see them as pending
            await db.commit()

        msg = "Riot account unlinked."
        if was_pending:
            msg += " (Pending request cancelled.)"
        msg += " Inhouse elo preserved."
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

        if ratings:
            ratings_by_role = {r.role: r for r in ratings}
            lines = []
            for role in ROLES:
                r = ratings_by_role.get(role)
                if r:
                    skill = r.mu - 3 * r.sigma
                    lines.append(f"**{role}**: {skill:.1f} (μ={r.mu:.1f}, σ={r.sigma:.1f}) · {r.games_played} games")
            embed.add_field(name="Inhouse Elo", value="\n".join(lines) or "No ratings yet", inline=False)

        await interaction.response.send_message(embed=embed)
