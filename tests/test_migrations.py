"""Tests for the startup schema migrations in bot.db.session."""
import asyncio
import sqlite3

from bot.config import Config
from bot.db.models import Match
from bot.db.session import close_db, get_session, init_db


def _config(db_path: str) -> Config:
    return Config(
        discord_token="x", discord_guild_id=1, owner_discord_id=1, riot_api_key="x",
        riot_region="na1", riot_regional_route="americas", admin_role_name="Admin",
        database_url=f"sqlite+aiosqlite:///{db_path}", timezone="UTC",
    )


def test_migrates_matches_session_id_to_nullable(tmp_path):
    """An old DB with matches.session_id NOT NULL must be rebuilt to allow
    sessionless pickup games, preserving existing rows."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE matches ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, "
        "team1_json TEXT, team2_json TEXT, winner INTEGER)"
    )
    conn.execute("INSERT INTO matches (session_id, team1_json, team2_json) VALUES (5, '{}', '{}')")
    conn.commit()
    conn.close()

    cfg = _config(str(db))

    async def body():
        await init_db(cfg)  # runs _fix_matches_session_id_nullable
        # The thing that used to crash: a sessionless (pickup) match.
        async with get_session() as s:
            m = Match(session_id=None, team1_json="{}", team2_json="{}")
            s.add(m)
            await s.commit()
            await s.refresh(m)
            assert m.session_id is None
        await close_db()

    asyncio.run(body())

    conn = sqlite3.connect(str(db))
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(matches)")}
    assert info["session_id"][3] == 0, "session_id should be nullable after migration"
    rows = conn.execute("select id, session_id from matches order by id").fetchall()
    conn.close()
    assert (1, 5) in rows, "existing row must be preserved"


def test_migration_noop_on_fresh_db(tmp_path):
    """A fresh DB (created from the model) is already nullable — migration no-ops
    and pickup inserts work."""
    cfg = _config(str(tmp_path / "fresh.db"))

    async def body():
        await init_db(cfg)
        async with get_session() as s:
            m = Match(session_id=None, team1_json="{}", team2_json="{}")
            s.add(m)
            await s.commit()
            await s.refresh(m)
            assert m.id is not None and m.session_id is None
        await close_db()

    asyncio.run(body())


def test_game_stats_gains_per_game_delta_columns_with_old_rows_null(tmp_path):
    """A game_stats table from before per-game deltas gets the two columns on boot,
    and its existing rows read back as NULL (which is what routes them to the
    MatchPerformance-based unreport)."""
    db = tmp_path / "old_stats.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE game_stats (match_id INTEGER NOT NULL, game_no INTEGER NOT NULL, "
        "discord_id BIGINT NOT NULL, role VARCHAR(8), champion VARCHAR(32), kills INTEGER, "
        "deaths INTEGER, assists INTEGER, won BOOLEAN NOT NULL, "
        "PRIMARY KEY (match_id, game_no, discord_id))"
    )
    conn.execute("INSERT INTO game_stats (match_id, game_no, discord_id, role, won) "
                 "VALUES (1, 1, 42, 'MID', 1)")
    conn.commit()
    conn.close()

    async def body():
        await init_db(_config(str(db)))
        await close_db()

    asyncio.run(body())

    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(game_stats)")}
    row = conn.execute(
        "select discord_id, role, role_elo_delta, inhouse_elo_delta from game_stats").fetchone()
    conn.close()
    assert {"role_elo_delta", "inhouse_elo_delta"} <= cols
    assert row == (42, "MID", None, None)
