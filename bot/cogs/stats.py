"""Leaderboard / stats cog."""
from __future__ import annotations

from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict

from sqlalchemy import select

from bot.db.models import GameStat, Match, MatchPerformance, Player, Rating
from bot.db.session import get_session

INHOUSE_ROLE = "INHOUSE"


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Inhouse leaderboard (net wins by default; sort:elo for rating).")
    @app_commands.describe(
        role="Filter by a specific role (optional)",
        sort="Rank by net wins (default) or INHOUSE elo",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        role: Optional[Literal["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]] = None,
        sort: Optional[Literal["net", "elo"]] = "net",
    ):
        await interaction.response.defer()
        guild = interaction.guild
        if guild is not None and not guild.chunked:
            try:
                await guild.chunk()  # populate member cache so display names resolve
            except Exception:
                pass
        async with get_session() as db:
            perf_stmt = (
                select(MatchPerformance, Match)
                .join(Match, Match.id == MatchPerformance.match_id)
                .where(Match.winner.isnot(None))
            )
            if role:
                perf_stmt = perf_stmt.where(MatchPerformance.role == role)
            perf_rows = (await db.execute(perf_stmt)).all()

            inhouse_ratings = {
                r.discord_id: r
                for r in (await db.execute(
                    select(Rating).where(Rating.role == INHOUSE_ROLE)
                )).scalars().all()
            }

            players = {
                p.discord_id: p
                for p in (await db.execute(select(Player))).scalars().all()
            }

        # Aggregate per player. Track game wins AND losses so we can rank by net
        # (wins - losses): raw win COUNT rewards volume (a 1-3 player floats up on
        # accumulated wins), while net goes negative for losing records and ranks
        # an 8-3 above a 1-0 above a 1-3.
        stats: dict[int, dict] = defaultdict(lambda: {
            "game_wins": 0, "game_losses": 0, "series_wins": 0, "series_losses": 0,
        })
        for perf, match in perf_rows:
            s = stats[perf.discord_id]
            won_games = max(match.team1_wins, match.team2_wins)
            lost_games = min(match.team1_wins, match.team2_wins)
            if perf.won:
                s["game_wins"] += won_games
                s["game_losses"] += lost_games
                s["series_wins"] += 1
            else:
                s["game_wins"] += lost_games
                s["game_losses"] += won_games
                s["series_losses"] += 1

        def elo(discord_id: int) -> int:
            r = inhouse_ratings.get(discord_id)
            return r.elo if r else 0

        def net_wins(kv):
            return kv[1]["game_wins"] - kv[1]["game_losses"]

        if sort == "elo":
            # Elo primary; net wins then series won as tiebreakers.
            ranked = sorted(
                stats.items(),
                key=lambda kv: (elo(kv[0]), net_wins(kv), kv[1]["series_wins"]),
                reverse=True,
            )
        else:
            # Net wins primary; series won as the tiebreaker, then INHOUSE elo.
            ranked = sorted(
                stats.items(),
                key=lambda kv: (net_wins(kv), kv[1]["series_wins"], elo(kv[0])),
                reverse=True,
            )

        lines = []
        for i, (discord_id, s) in enumerate(ranked, 1):
            player = players.get(discord_id)
            member = guild.get_member(discord_id) if guild else None
            if member is not None:
                raw_name = member.display_name           # Discord server name
            elif player and player.riot_game_name:
                raw_name = player.riot_game_name          # fallback: Riot name
            else:
                raw_name = f"<@{discord_id}>"             # last resort: mention
            name = discord.utils.escape_markdown(raw_name) if member or (player and player.riot_game_name) else raw_name
            gw, gl = s["game_wins"], s["game_losses"]
            net = gw - gl
            sw, sl = s["series_wins"], s["series_losses"]
            series_total = sw + sl
            series_pct = sw / series_total * 100 if series_total else 0
            lines.append(
                f"`{i:>2}.` **{name}** · **{net:+d}** ({gw}-{gl} games)"
                f" · {sw}-{sl} series ({series_pct:.0f}%) · {elo(discord_id)} elo"
            )

        desc = ""
        for n, ln in enumerate(lines):
            if len(desc) + len(ln) + 1 > 3900:
                desc += f"\n…and {len(lines) - n} more"
                break
            desc += ("\n" if desc else "") + ln

        title = f"🏆 Leaderboard — {role}" if role else "🏆 Inhouse Leaderboard"
        embed = discord.Embed(
            title=title,
            description=desc or "No games played yet.",
            color=discord.Color.gold(),
        )
        embed.set_footer(
            text="Ranked by INHOUSE elo · tiebreaker: net wins" if sort == "elo"
            else "Ranked by net wins (W−L) · tiebreaker: series won"
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
            day = match.game_date or match.reported_at
            when = day.strftime("%b %d") if day else "—"
            wl = "🟢 W" if perf.won else "🔴 L"
            d = perf.inhouse_elo_delta or 0
            lines.append(
                f"`{when}` {wl} {match.team1_wins}-{match.team2_wins} · "
                f"{perf.role} · **{d:+d}** (net {cum:+d})"
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

    @app_commands.command(
        name="recent-games",
        description="A player's most recent games: champion, KDA, and result.",
    )
    @app_commands.describe(user="Whose games to view (defaults to you)",
                           count="How many games to show (default 10, max 25)")
    async def recent_games(
        self, interaction: discord.Interaction,
        user: Optional[discord.Member] = None, count: Optional[int] = 10,
    ):
        await interaction.response.defer()
        target = user or interaction.user
        count = max(1, min(count or 10, 25))
        async with get_session() as db:
            player = await db.get(Player, target.id)
            rows = (await db.execute(
                select(GameStat, Match)
                .join(Match, Match.id == GameStat.match_id)
                .where(GameStat.discord_id == target.id)
                .order_by(GameStat.match_id.desc(), GameStat.game_no.desc())
                .limit(count)
            )).all()

        name = player.riot_game_name if (player and player.riot_game_name) else target.display_name
        if not rows:
            await interaction.followup.send(
                f"**{name}** has no per-game stats recorded yet (champ/KDA are entered from screenshots)."
            )
            return

        lines = []
        for gs, match in rows:
            wl = "🟢 W" if gs.won else "🔴 L"
            champ = gs.champion or "?"
            if None not in (gs.kills, gs.deaths, gs.assists):
                ratio = (gs.kills + gs.assists) / gs.deaths if gs.deaths else float(gs.kills + gs.assists)
                kda = f"{gs.kills}/{gs.deaths}/{gs.assists} ({ratio:.1f})"
            else:
                kda = "—"
            role = f" {gs.role}" if gs.role else ""
            day = match.game_date or match.reported_at
            when = day.strftime("%b %d") if day else "—"
            lines.append(f"`{when}` {wl} · **{champ}**{role} · {kda}  ·  `s{gs.match_id} g{gs.game_no}`")

        embed = discord.Embed(
            title=f"🎮 {name} — last {len(lines)} games",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=embed)
