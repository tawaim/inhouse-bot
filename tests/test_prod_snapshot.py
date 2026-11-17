"""Regression checks against a real copy of the production database.

Skipped unless data/prod_inhouse.db exists (pull one with the command in
CLAUDE.md). The snapshot is COPIED to a temp dir first — these tests never write
to the file in data/ and never touch production.

They answer: "if I deploy this code, will it boot on the real data and keep the
ratings intact?"
"""
import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

from bot.cogs.admin import AdminCog
from bot.db.models import GameStat, Match
from bot.db.session import close_db, get_session, init_db
from tests.harness import make_config

SNAPSHOT = Path(__file__).parent.parent / "data" / "prod_inhouse.db"
pytestmark = pytest.mark.skipif(not SNAPSHOT.exists(), reason="no data/prod_inhouse.db snapshot")


def _copy(tmp_path) -> Path:
    dst = tmp_path / "prod_copy.db"
    shutil.copyfile(SNAPSHOT, dst)
    return dst


def _dump(path: Path) -> dict:
    """Row counts + every rating, read with plain sqlite3."""
    c = sqlite3.connect(path)
    try:
        counts = {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in (
            "players", "ratings", "sessions", "signups", "matches", "match_performances",
            "game_stats", "aliases", "proposal_sets")}
        ratings = c.execute(
            "select discord_id, role, elo, base_seed, inhouse_modifier, games_played "
            "from ratings order by discord_id, role").fetchall()
        return {"counts": counts, "ratings": ratings}
    finally:
        c.close()


def test_boot_migrations_preserve_all_data(tmp_path):
    """Starting the bot on the real DB (create_all + migrations) loses nothing."""
    db = _copy(tmp_path)
    before = _dump(db)

    async def body():
        await init_db(make_config(str(db)))
        await close_db()

    asyncio.run(body())
    after = _dump(db)
    assert after == before
    c = sqlite3.connect(db)
    assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
    c.close()


def test_ratings_are_internally_consistent(tmp_path):
    c = sqlite3.connect(_copy(tmp_path))
    try:
        assert c.execute(
            "select count(*) from ratings where elo != base_seed + inhouse_modifier").fetchone()[0] == 0
        assert c.execute("select count(*) from ratings where games_played < 0").fetchone()[0] == 0
        assert c.execute(
            "select count(*) from ratings r where not exists "
            "(select 1 from players p where p.discord_id = r.discord_id)").fetchone()[0] == 0
        # Every reported match has a consistent score and winner.
        bad = c.execute(
            "select id from matches where winner is not null and "
            "(team1_wins = team2_wins or (winner = 1) != (team1_wins > team2_wins))").fetchall()
        assert bad == []
    finally:
        c.close()


def test_unreport_then_rereport_latest_match_is_lossless(tmp_path):
    """Undoing and re-applying the newest reported series is stable: doing it
    twice gives the same ratings both times, and nobody's game count or seed moves.

    It is not compared with the ratings in the snapshot. A re-report scores the
    series from the players' current seeds, so it only reproduces the original
    numbers while no participant's seed has changed since the report (a rank sync
    moved one after match 16, which shifted that lane's delta from 30 to 28).
    """
    db = _copy(tmp_path)

    async def body():
        await init_db(make_config(str(db)))
        async with get_session() as s:
            latest = (await s.execute(
                select(Match).where(Match.winner.is_not(None)).order_by(Match.id.desc())
            )).scalars().first()
            if latest is None:
                await close_db()
                pytest.skip("snapshot has no reported matches")
            has_games = (await s.execute(
                select(GameStat).where(GameStat.match_id == latest.id)
            )).scalars().first() is not None
        if not has_games:
            await close_db()
            pytest.skip("latest match predates per-game stats")
        before = _dump(db)["ratings"]
        async with get_session() as s:
            m = await s.get(Match, latest.id)
            games = await AdminCog._games_from_gamestats(None, s, m)
        replays = []
        for _ in range(2):
            status, _, _ = await AdminCog._revert_result(None, latest.id)
            assert status == "ok"
            assert _dump(db)["ratings"] != before
            await AdminCog._commit_games(None, latest.id, games, None, 1)
            replays.append(_dump(db)["ratings"])
        await close_db()
        return before, replays

    before, (first, second) = asyncio.run(body())
    assert first == second
    # (discord_id, role, elo, base_seed, inhouse_modifier, games_played)
    assert [(r[0], r[1], r[3], r[5]) for r in first] == [(r[0], r[1], r[3], r[5]) for r in before]
    assert all(r[2] == r[3] + r[4] for r in first)
