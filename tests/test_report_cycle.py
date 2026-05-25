"""Integration test for the report -> idempotency -> unreport cycle.

Drives the real AdminCog._commit_result / _revert_result against a temp SQLite
DB. These methods don't touch self, so we invoke them with a dummy `self`,
avoiding Discord/cog machinery.
"""
import asyncio
import json
import types

from bot.cogs.admin import AdminCog
from bot.config import Config
from bot.db.models import Match, MatchPerformance, Player, Rating
from bot.db.session import close_db, get_session, init_db
from bot.services.elo import INHOUSE_ROLE

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
        from sqlalchemy import select
        rows = (await db.execute(select(Rating))).scalars().all()
        return {(r.discord_id, r.role): (r.elo, r.inhouse_modifier, r.games_played) for r in rows}


async def _seed_match() -> int:
    async with get_session() as db:
        for pid in range(1, 11):
            db.add(Player(
                discord_id=pid, riot_game_name=f"P{pid}", riot_tag_line="NA1",
                riot_puuid=f"puuid{pid}", link_status="approved",
            ))
            for role in ROLES_PLUS:
                db.add(Rating(
                    discord_id=pid, role=role, elo=1500, base_seed=1500,
                    inhouse_modifier=0, games_played=15,  # >10 so K=20
                ))
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
        dummy = types.SimpleNamespace()  # _commit_result/_revert_result don't use self

        # --- Report a 2-0 win for team1 ---
        await AdminCog._commit_result(dummy, match_id, 2, 0, None, 999, None)
        after = await _snapshot()

        # Team1 gained, team2 lost; everyone's role + INHOUSE games_played += 1.
        for role, pid in TEAM1.items():
            elo, mod, games = after[(pid, role)]
            assert mod > 0 and elo == 1500 + mod and games == 16
            assert after[(pid, INHOUSE_ROLE)][1] > 0
        for role, pid in TEAM2.items():
            elo, mod, games = after[(pid, role)]
            assert mod < 0 and elo == 1500 + mod and games == 16

        # Deltas were stored on the performance rows (needed for exact reversal).
        async with get_session() as db:
            from sqlalchemy import select
            perfs = (await db.execute(select(MatchPerformance))).scalars().all()
            assert len(perfs) == 10
            assert all(p.role_elo_delta != 0 and p.inhouse_elo_delta != 0 for p in perfs)
            match = await db.get(Match, match_id)
            assert match.winner == 1 and match.team1_wins == 2 and match.team2_wins == 0

        # --- Idempotency: a second report must NOT double-apply ---
        await AdminCog._commit_result(dummy, match_id, 2, 0, None, 999, None)
        assert await _snapshot() == after

        # --- Unreport: fully restores the pre-report state ---
        status, count, prev = await AdminCog._revert_result(dummy, match_id)
        assert status == "ok" and count == 10 and prev == "2-0"
        assert await _snapshot() == baseline

        async with get_session() as db:
            from sqlalchemy import select
            assert (await db.execute(select(MatchPerformance))).scalars().all() == []
            match = await db.get(Match, match_id)
            assert match.winner is None and match.team1_wins == 0 and match.team2_wins == 0

        await close_db()

    asyncio.run(body())
