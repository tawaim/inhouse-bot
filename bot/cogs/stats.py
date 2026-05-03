"""Leaderboard / stats cog."""
from __future__ import annotations

from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.db.models import MatchPerformance, Player, Rating
from bot.db.session import get_session
# elo is just an int; no import needed


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Inhouse leaderboard with wins, games, and elo.")
    @app_commands.describe(role="Filter by a specific role (optional)")
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        role: Optional[Literal["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]] = None,
    ):
        await interaction.response.defer()
        async with get_session() as db:
            rating_stmt = (
                select(Rating, Player)
                .join(Player, Player.discord_id == Rating.discord_id)
                .where(Rating.games_played > 0)
                # Always exclude INHOUSE — leaderboard ranks by per-role play
                .where(Rating.role != "INHOUSE")
            )
            if role:
                rating_stmt = rating_stmt.where(Rating.role == role)
            rating_rows = (await db.execute(rating_stmt)).all()

            perf_stmt = select(MatchPerformance)
            if role:
                perf_stmt = perf_stmt.where(MatchPerformance.role == role)
            perfs = (await db.execute(perf_stmt)).scalars().all()

        # Tally W/L per (discord_id, role) from match_performances
        wl_by_key: dict[tuple[int, str], dict] = {}
        for p in perfs:
            key = (p.discord_id, p.role)
            b = wl_by_key.setdefault(key, {"wins": 0, "losses": 0})
            if p.won:
                b["wins"] += 1
            else:
                b["losses"] += 1

        if role:
            rows = []
            for rating, player in rating_rows:
                skill = float(rating.elo)
                wl = wl_by_key.get((player.discord_id, role), {"wins": 0, "losses": 0})
                rows.append((rating, player, skill, wl["wins"], wl["losses"]))
            rows.sort(key=lambda r: r[2], reverse=True)
            rows = rows[:15]

            lines = []
            for i, (rating, player, skill, wins, losses) in enumerate(rows, 1):
                name = player.riot_game_name or f"<@{player.discord_id}>"
                games = rating.games_played
                wr = (wins / games * 100) if games else 0
                lines.append(
                    f"`{i:>2}.` **{name}** · {wins}W-{losses}L ({wr:.0f}%) · "
                    f"{games} games · Elo {skill:.0f}"
                )
            embed = discord.Embed(
                title=f"🏆 Leaderboard — {role}",
                description="\n".join(lines) or "No games played yet.",
                color=discord.Color.gold(),
            )
        else:
            # Best role per player, ranked by skill
            best_by_player: dict[int, tuple[Player, Rating, float, int, int]] = {}
            for rating, player in rating_rows:
                skill = float(rating.elo)
                wl = wl_by_key.get((player.discord_id, rating.role), {"wins": 0, "losses": 0})
                cur = best_by_player.get(player.discord_id)
                if cur is None or skill > cur[2]:
                    best_by_player[player.discord_id] = (
                        player, rating, skill, wl["wins"], wl["losses"]
                    )
            ranked = sorted(best_by_player.values(), key=lambda x: x[2], reverse=True)[:15]

            lines = []
            for i, (player, rating, skill, wins, losses) in enumerate(ranked, 1):
                name = player.riot_game_name or f"<@{player.discord_id}>"
                games = rating.games_played
                wr = (wins / games * 100) if games else 0
                lines.append(
                    f"`{i:>2}.` **{name}** · {wins}W-{losses}L ({wr:.0f}%) · "
                    f"{games} games on {rating.role} · Elo {skill:.0f}"
                )
            embed = discord.Embed(
                title="🏆 Inhouse Leaderboard (best role per player)",
                description="\n".join(lines) or "No games played yet.",
                color=discord.Color.gold(),
            )
        await interaction.followup.send(embed=embed)
