"""Integration test for the report -> idempotency -> unreport cycle.

Drives the real AdminCog._commit_result / _commit_games / _revert_result against
a temp SQLite DB. These methods don't touch self, so we invoke them with a dummy
`self`, avoiding Discord/cog machinery.

Elo is applied PER GAME to whoever actually played, so games_played counts actual
games and a sub earns elo only for the games they played. /unreport must restore
the exact pre-report state regardless.
"""
import asyncio
import json

from sqlalchemy import select

from bot.cogs.admin import AdminCog, GameResult
from bot.config import Config
from bot.db.models import GameStat, Match, MatchPerformance, Player, Rating
from bot.db.session import close_db, get_session, init_db
from bot.services.elo import INHOUSE_ROLE, update_elo_team_game

ROLES_PLUS = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT", INHOUSE_ROLE]
TEAM1 = {"TOP": 1, "JUNGLE": 2, "MID": 3, "BOT": 4, "SUPPORT": 5}
TEAM2 = {"TOP": 6, "JUNGLE": 7, "MID": 8, "BOT": 9, "SUPPORT": 10}


def _config(db_path: str) -> Config:
    return Config(
        discord_token="x", discord_guild_id=1, owner_discord_id=1,
        riot_api_key="x", riot_region="na1", riot_regional_route="americas",
        admin_role_name="Admin", database_url=f"sqlite+aiosqlite:///{db_path}",
        timezone="UTC",
    )


async def _snapshot() -> dict:
    """(discord_id, role) -> (elo, inhouse_modifier, games_played) for all ratings."""
    async with get_session() as db:
        rows = (await db.execute(select(Rating))).scalars().all()
        return {(r.discord_id, r.role): (r.elo, r.inhouse_modifier, r.games_played) for r in rows}


async def _seed_players(ids, elo=1500, games=15) -> None:
    async with get_session() as db:
        for pid in ids:
            db.add(Player(
                discord_id=pid, riot_game_name=f"P{pid}", riot_tag_line="NA1",
                riot_puuid=f"puuid{pid}", link_status="approved",
            ))
            for role in ROLES_PLUS:
                db.add(Rating(
                    discord_id=pid, role=role, elo=elo, base_seed=elo,
                    inhouse_modifier=0, games_played=games,  # >10 so K behaviour is stable
                ))
        await db.commit()


async def _seed_match() -> int:
    await _seed_players(range(1, 11))
    async with get_session() as db:
        m = Match(session_id=None, team1_json=json.dumps(TEAM1), team2_json=json.dumps(TEAM2))
        db.add(m)
        await db.commit()
        await db.refresh(m)
        return m.id


def test_report_idempotency_and_unreport_roundtrip(tmp_path):
    async def body():
        await init_db(_config(str(tmp_path / "cycle.db")))
        match_id = await _seed_match()
        baseline = await _snapshot()
        cog = AdminCog.__new__(AdminCog)

        # --- Report a 2-0 win for team1 (TWO games) ---
        await cog._commit_result(match_id, 2, 0, None, 999, None)
        after = await _snapshot()

        # Team1 gained, team2 lost; games_played += 2 (two actual games).
        for role, pid in TEAM1.items():
            elo, mod, games = after[(pid, role)]
            assert mod > 0 and elo == 1500 + mod and games == 17
            assert after[(pid, INHOUSE_ROLE)][1] > 0
            assert after[(pid, INHOUSE_ROLE)][2] == 17
        for role, pid in TEAM2.items():
            elo, mod, games = after[(pid, role)]
            assert mod < 0 and elo == 1500 + mod and games == 17

        async with get_session() as db:
            # One aggregate MatchPerformance per player (summed deltas).
            perfs = (await db.execute(select(MatchPerformance))).scalars().all()
            assert len(perfs) == 10
            assert all(p.role_elo_delta != 0 and p.inhouse_elo_delta != 0 for p in perfs)
            # Per-game GameStat rows: 10 players x 2 games.
            assert len((await db.execute(select(GameStat))).scalars().all()) == 20
            match = await db.get(Match, match_id)
            assert match.winner == 1 and match.team1_wins == 2 and match.team2_wins == 0

        # --- Idempotency: a second report must NOT double-apply ---
        await cog._commit_result(match_id, 2, 0, None, 999, None)
        assert await _snapshot() == after

        # --- Unreport: fully restores the pre-report state ---
        status, count, prev = await cog._revert_result(match_id)
        assert status == "ok" and count == 10 and prev == "2-0"
        assert await _snapshot() == baseline

        async with get_session() as db:
            assert (await db.execute(select(MatchPerformance))).scalars().all() == []
            assert (await db.execute(select(GameStat))).scalars().all() == []
            match = await db.get(Match, match_id)
            assert match.winner is None and match.team1_wins == 0 and match.team2_wins == 0

        await close_db()

    asyncio.run(body())


def test_single_game_uses_prematch_lane_opponent_elos(tmp_path):
    """Regression: lane bias must read PRE-GAME elos. Team 1 is scored before
    Team 2 within a game, so Team 2's lane opponents (Team 1) must not reflect
    Team 1's just-applied gains. One game, so each stored delta equals a single
    update_elo_team_game call we can recompute exactly."""
    elos = {1: 1600, 2: 1650, 3: 1650, 4: 1400, 5: 1450,
            6: 1700, 7: 1950, 8: 1700, 9: 1600, 10: 1400}
    t1_avg = round(sum(elos[p] for p in TEAM1.values()) / 5)
    t2_avg = round(sum(elos[p] for p in TEAM2.values()) / 5)

    async def body():
        await init_db(_config(str(tmp_path / "lane.db")))
        async with get_session() as db:
            for pid, e in elos.items():
                db.add(Player(discord_id=pid, riot_game_name=f"P{pid}", riot_tag_line="NA1",
                              riot_puuid=f"puuid{pid}", link_status="approved"))
                for role in ROLES_PLUS:
                    db.add(Rating(discord_id=pid, role=role, elo=e, base_seed=e,
                                  inhouse_modifier=0, games_played=15))
            m = Match(session_id=None, team1_json=json.dumps(TEAM1), team2_json=json.dumps(TEAM2))
            db.add(m)
            await db.commit()
            await db.refresh(m)
            match_id = m.id

        cog = AdminCog.__new__(AdminCog)
        await cog._commit_result(match_id, 1, 0, None, 999, None)  # one game, team1 wins

        async with get_session() as db:
            perfs = {p.discord_id: p for p in
                     (await db.execute(select(MatchPerformance))).scalars().all()}

        for role, pid in TEAM1.items():
            _, exp = update_elo_team_game(t1_avg, t2_avg, elos[pid], elos[TEAM2[role]], won=True)
            assert perfs[pid].inhouse_elo_delta == exp
        for role, pid in TEAM2.items():
            _, exp = update_elo_team_game(t2_avg, t1_avg, elos[pid], elos[TEAM1[role]], won=False)
            assert perfs[pid].inhouse_elo_delta == exp

        await close_db()

    asyncio.run(body())


def test_per_game_sub_attribution_and_roundtrip(tmp_path):
    """A 3-game series where a sub (pid 11) plays only game 2 at jungle. The
    regular jungler gets 2 games, the sub gets 1 — and /unreport restores both."""
    async def body():
        await init_db(_config(str(tmp_path / "sub.db")))
        await _seed_players(list(range(1, 11)) + [11])
        async with get_session() as db:
            m = Match(session_id=None, team1_json=json.dumps(TEAM1), team2_json=json.dumps(TEAM2))
            db.add(m)
            await db.commit()
            await db.refresh(m)
            match_id = m.id

        baseline = await _snapshot()
        cog = AdminCog.__new__(AdminCog)

        t1_sub = {**TEAM1, "JUNGLE": 11}  # pid 11 subs jungle for game 2 only
        games = [
            GameResult(winner=1, team1=dict(TEAM1), team2=dict(TEAM2)),  # regular jungle, win
            GameResult(winner=2, team1=t1_sub, team2=dict(TEAM2)),       # sub plays, loss
            GameResult(winner=2, team1=dict(TEAM1), team2=dict(TEAM2)),  # regular jungle, loss
        ]
        await cog._commit_games(match_id, games, None, 999)

        after = await _snapshot()
        # Regular jungler (pid 2): games 1 + 3 -> +2 games at JUNGLE and INHOUSE.
        assert after[(2, "JUNGLE")][2] == 17
        assert after[(2, INHOUSE_ROLE)][2] == 17
        # Sub (pid 11): only game 2 -> +1 game.
        assert after[(11, "JUNGLE")][2] == 16
        assert after[(11, INHOUSE_ROLE)][2] == 16
        # A non-jungle regular (pid 1, TOP): all 3 games.
        assert after[(1, "TOP")][2] == 18
        assert after[(1, INHOUSE_ROLE)][2] == 18

        async with get_session() as db:
            match = await db.get(Match, match_id)
            assert match.winner == 2 and match.team1_wins == 1 and match.team2_wins == 2
            assert len((await db.execute(select(GameStat))).scalars().all()) == 30  # 10 x 3
            # 11 distinct players appeared (10 regulars + the sub).
            assert len((await db.execute(select(MatchPerformance))).scalars().all()) == 11

        # Unreport restores everyone — including the sub — to baseline.
        status, _count, prev = await cog._revert_result(match_id)
        assert status == "ok" and prev == "1-2"
        assert await _snapshot() == baseline
        async with get_session() as db:
            assert (await db.execute(select(GameStat))).scalars().all() == []
            assert (await db.execute(select(MatchPerformance))).scalars().all() == []

        await close_db()

    asyncio.run(body())
