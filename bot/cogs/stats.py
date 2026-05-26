"""Leaderboard / stats cog."""
from __future__ import annotations

from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.db.models import GameStat, Match, MatchPerformance, Player, Rating
from bot.db.session import get_session
# elo is just an int; no import needed

INHOUSE_ROLE = "INHOUSE"


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
            # No role -> rank by the overall INHOUSE rating (every series counts,
            # across all roles). With a role -> rank by that role's rating.
            scope_role = role or "INHOUSE"
            rating_rows = (await db.execute(
                select(Rating, Player)
                .join(Player, Player.discord_id == Rating.discord_id)
                .where(Rating.games_played > 0)
                .where(Rating.role == scope_role)
            )).all()

            perf_stmt = select(MatchPerformance)
            if role:
                perf_stmt = perf_stmt.where(MatchPerformance.role == role)
            perfs = (await db.execute(perf_stmt)).scalars().all()

            # For the overall view, label each player's best (highest-elo) role.
            best_role: dict[int, tuple[str, int]] = {}
            if not role:
                for r in (await db.execute(
                    select(Rating).where(Rating.role != "INHOUSE").where(Rating.games_played > 0)
                )).scalars().all():
                    cur = best_role.get(r.discord_id)
                    if cur is None or r.elo > cur[1]:
                        best_role[r.discord_id] = (r.role, r.elo)

        # Tally W/L. Per-role view keys by (player, role); the overall view keys by
        # player, so it sums every series that player has appeared in.
        wl: dict = {}
        for p in perfs:
            key = (p.discord_id, p.role) if role else p.discord_id
            b = wl.setdefault(key, {"wins": 0, "losses": 0})
            b["wins" if p.won else "losses"] += 1

        ranked = sorted(rating_rows, key=lambda rp: rp[0].elo, reverse=True)[:15]
        lines = []
        for i, (rating, player) in enumerate(ranked, 1):
            name = player.riot_game_name or f"<@{player.discord_id}>"
            games = rating.games_played
            rec = wl.get((player.discord_id, role) if role else player.discord_id,
                         {"wins": 0, "losses": 0})
            wins, losses = rec["wins"], rec["losses"]
            wr = (wins / games * 100) if games else 0
            if role:
                tail = ""
            else:
                br = best_role.get(player.discord_id)
                tail = f" · top {br[0]}" if br else ""
            lines.append(
                f"`{i:>2}.` **{name}** · {wins}W-{losses}L ({wr:.0f}%) · "
                f"{games} games · Elo {rating.elo}{tail}"
            )
        embed = discord.Embed(
            title=f"🏆 Leaderboard — {role}" if role else "🏆 Inhouse Leaderboard (overall)",
            description="\n".join(lines) or "No games played yet.",
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="elo-history",
        description="Show a player's match-by-match inhouse elo changes.",
    )
    @app_commands.describe(user="Whose history to show (defaults to you)")
    async def elo_history(
        self, interaction: discord.Interaction, user: Optional[discord.Member] = None
    ):
        target = user or interaction.user
        await interaction.response.defer()
        async with get_session() as db:
            rows = (await db.execute(
                select(MatchPerformance, Match)
                .join(Match, Match.id == MatchPerformance.match_id)
                .where(MatchPerformance.discord_id == target.id)
                .order_by(Match.reported_at.asc(), Match.id.asc())
            )).all()
            inhouse = await db.get(Rating, (target.id, INHOUSE_ROLE))

        if not rows:
            await interaction.followup.send(
                f"{target.display_name} has no recorded inhouse games yet."
            )
            return

        # Running cumulative of the INHOUSE modifier across all games (in order).
        lines = []
        cum = 0
        for perf, match in rows:
            cum += perf.inhouse_elo_delta or 0
            when = match.reported_at.strftime("%b %d") if match.reported_at else "—"
            wl = "🟢 W" if perf.won else "🔴 L"
            d = perf.inhouse_elo_delta or 0
            lines.append(
                f"`{when}` {wl} {match.team1_wins}-{match.team2_wins} · "
                f"{perf.role} · **{d:+d}** (cum {cum:+d})"
            )

        shown = lines[-15:]  # last 15 games to stay within embed limits
        embed = discord.Embed(
            title=f"📈 {target.display_name}'s Inhouse Elo History",
            color=discord.Color.blurple(),
        )
        if inhouse is not None:
            embed.description = (
                f"**Current INHOUSE elo: {inhouse.elo}** "
                f"(rank base {inhouse.base_seed}, inhouse {inhouse.inhouse_modifier:+d}, "
                f"{inhouse.games_played} games)"
            )
        omitted = len(lines) - len(shown)
        field_name = f"Last {len(shown)} games" + (f" (+{omitted} earlier)" if omitted else "")
        embed.add_field(name=field_name, value="\n".join(shown), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="player-stats",
        description="Full inhouse stats for a player: record, roles, KDA, champions.",
    )
    @app_commands.describe(user="Whose stats to view (defaults to you)")
    async def player_stats(
        self, interaction: discord.Interaction, user: Optional[discord.Member] = None
    ):
        await interaction.response.defer()
        target = user or interaction.user
        async with get_session() as db:
            player = await db.get(Player, target.id)
            perfs = (await db.execute(
                select(MatchPerformance).where(MatchPerformance.discord_id == target.id)
            )).scalars().all()
            ratings = {r.role: r for r in (await db.execute(
                select(Rating).where(Rating.discord_id == target.id)
            )).scalars().all()}
            # Per-game champ/KDA (manually ingested from screenshots).
            gstats = (await db.execute(
                select(GameStat).where(GameStat.discord_id == target.id)
            )).scalars().all()

        name = player.riot_game_name if (player and player.riot_game_name) else target.display_name
        if not perfs and not gstats:
            await interaction.followup.send(f"**{name}** has no recorded inhouse games yet.")
            return

        series = len(perfs)
        s_wins = sum(1 for p in perfs if p.won)
        s_losses = series - s_wins
        wr = s_wins / series * 100 if series else 0
        inhouse = ratings.get(INHOUSE_ROLE)

        # Per-role series record: role -> [series, wins]
        role_agg: dict[str, list[int]] = {}
        for p in perfs:
            g = role_agg.setdefault(p.role, [0, 0])
            g[0] += 1
            g[1] += 1 if p.won else 0

        # Per-game record + champions: champ -> [games, wins]
        g_total = len(gstats)
        g_wins = sum(1 for s in gstats if s.won)
        champ_agg: dict[str, list[int]] = {}
        for s in gstats:
            if s.champion:
                c = champ_agg.setdefault(s.champion, [0, 0])
                c[0] += 1
                c[1] += 1 if s.won else 0

        embed = discord.Embed(title=f"📊 {name} — Inhouse Stats", color=discord.Color.blurple())
        elo_txt = str(inhouse.elo) if inhouse else "—"
        overall = f"**{s_wins}W–{s_losses}L** ({wr:.0f}%) · {series} series · INHOUSE Elo **{elo_txt}**"
        if g_total:
            overall += f"\nGames: {g_wins}W–{g_total - g_wins}L ({g_total} total)"
        embed.add_field(name="Overall", value=overall, inline=False)

        role_lines = []
        for role in ("TOP", "JUNGLE", "MID", "BOT", "SUPPORT"):
            g = role_agg.get(role)
            if not g:
                continue
            r = ratings.get(role)
            elo_r = f" · Elo {r.elo}" if r else ""
            role_lines.append(f"**{role}** — {g[1]}W–{g[0] - g[1]}L ({g[0]} series){elo_r}")
        if role_lines:
            embed.add_field(name="By role", value="\n".join(role_lines), inline=False)

        # KDA from per-game rows that have it.
        kda = [s for s in gstats if None not in (s.kills, s.deaths, s.assists)]
        if kda:
            tk = sum(s.kills for s in kda)
            td = sum(s.deaths for s in kda)
            ta = sum(s.assists for s in kda)
            n = len(kda)
            ratio = (tk + ta) / td if td else float(tk + ta)
            embed.add_field(
                name="KDA",
                value=(f"**{ratio:.2f}** KDA · {tk / n:.1f} / {td / n:.1f} / {ta / n:.1f} avg "
                       f"· {tk}/{td}/{ta} total ({n} games)"),
                inline=False,
            )
        else:
            embed.add_field(name="KDA", value="_not recorded yet_", inline=False)

        if champ_agg:
            champ_lines = [
                f"**{c}** — {w}W–{g - w}L ({g})"
                for c, (g, w) in sorted(champ_agg.items(), key=lambda kv: (-kv[1][0], kv[0]))
            ][:12]
            embed.add_field(name="Champions played", value="\n".join(champ_lines), inline=False)
        else:
            embed.add_field(name="Champions played", value="_not recorded yet_", inline=False)

        await interaction.followup.send(embed=embed)
