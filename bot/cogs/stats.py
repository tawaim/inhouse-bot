"""Leaderboard / stats cog."""
from __future__ import annotations

from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.db.models import Player, Rating
from bot.db.session import get_session
from bot.services.elo import conservative_skill


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Inhouse elo leaderboard.")
    @app_commands.describe(role="Filter by a specific role (optional)")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        role: Optional[Literal["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]] = None,
    ):
        await interaction.response.defer()
        async with get_session() as db:
            if role:
                ratings = (await db.execute(
                    select(Rating, Player)
                    .join(Player, Player.discord_id == Rating.discord_id)
                    .where(Rating.role == role)
                    .where(Rating.games_played > 0)
                )).all()
                # Sort by conservative skill, top 15
                ratings.sort(key=lambda r: conservative_skill(r[0].mu, r[0].sigma), reverse=True)
                ratings = ratings[:15]
                lines = []
                for i, (rating, player) in enumerate(ratings, 1):
                    skill = conservative_skill(rating.mu, rating.sigma)
                    name = player.riot_game_name or f"<@{player.discord_id}>"
                    lines.append(f"`{i:>2}.` {skill:>5.1f}  **{name}** · {rating.games_played} games")
                embed = discord.Embed(
                    title=f"🏆 Leaderboard — {role}",
                    description="\n".join(lines) or "No games played yet.",
                    color=discord.Color.gold(),
                )
            else:
                # Aggregate across all roles: best per player
                rows = (await db.execute(
                    select(Player, Rating)
                    .join(Rating, Rating.discord_id == Player.discord_id)
                    .where(Rating.games_played > 0)
                )).all()
                best_by_player: dict[int, tuple[Player, Rating, float]] = {}
                for player, rating in rows:
                    skill = conservative_skill(rating.mu, rating.sigma)
                    cur = best_by_player.get(player.discord_id)
                    if cur is None or skill > cur[2]:
                        best_by_player[player.discord_id] = (player, rating, skill)
                ranked = sorted(best_by_player.values(), key=lambda x: x[2], reverse=True)[:15]
                lines = []
                for i, (player, rating, skill) in enumerate(ranked, 1):
                    name = player.riot_game_name or f"<@{player.discord_id}>"
                    lines.append(
                        f"`{i:>2}.` {skill:>5.1f}  **{name}** · best: {rating.role} · "
                        f"{rating.games_played} games"
                    )
                embed = discord.Embed(
                    title="🏆 Inhouse Leaderboard (best role per player)",
                    description="\n".join(lines) or "No games played yet.",
                    color=discord.Color.gold(),
                )
        await interaction.followup.send(embed=embed)
