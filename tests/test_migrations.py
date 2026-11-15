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

# --------------------------------------------------------------------------- helpers

def _init(db_path) -> None:
    """Boot the database once, the way the bot does."""
    async def body():
        try:
            await init_db(_config(str(db_path)))
        finally:
            await close_db()
    asyncio.run(body())


def _schema(db_path) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {name: sql or "" for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('table', 'index')")}
    finally:
        conn.close()


def _dump(db_path, tables=("matches", "match_performances", "game_stats", "proposal_sets")) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall() for t in tables}
    finally:
        conn.close()


def _seed_rows(db_path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO sessions (id, game_date, status) VALUES (1, '2026-05-14', 'completed')")
    for mid in (1, 2, 3):
        conn.execute(
            "INSERT INTO matches (id, session_id, team1_json, team2_json, winner, team1_wins, team2_wins) "
            "VALUES (?, 1, '{}', '{}', 1, 2, 1)", (mid,))
        for pid in (101, 102):
            conn.execute(
                "INSERT INTO match_performances (match_id, discord_id, role, won, role_elo_delta, "
                "inhouse_elo_delta) VALUES (?, ?, 'TOP', 1, 15, 14)", (mid, pid))
            for game in (1, 2):
                conn.execute(
                    "INSERT INTO game_stats (match_id, game_no, discord_id, role, champion, kills, "
                    "deaths, assists, won) VALUES (?, ?, ?, 'TOP', 'Garen', 3, 2, 1, 1)", (mid, game, pid))
        conn.execute(
            "INSERT INTO proposal_sets (session_id, proposals_json, resolved_match_id, created_at) "
            "VALUES (1, '[]', ?, '2026-05-11 21:30:00')", (mid,))
    conn.execute("DELETE FROM matches WHERE id = 3")  # leaves the AUTOINCREMENT high-water mark at 3
    conn.commit()
    conn.close()


def _break_like_the_old_migration(db_path) -> None:
    """Replay what the old rebuild did: rename `matches` away first. On SQLite
    >= 3.26 that rewrites the child tables' REFERENCES to the temporary name."""
    conn = sqlite3.connect(str(db_path))
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='matches'").fetchone()[0]
    conn.execute("ALTER TABLE matches RENAME TO _matches_old")
    conn.execute(ddl)
    conn.execute("INSERT INTO matches SELECT * FROM _matches_old")
    conn.execute("DROP TABLE _matches_old")
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- tests

def test_repairs_child_tables_that_reference_matches_old(tmp_path):
    """Production state: match_performances / game_stats / proposal_sets carry
    `REFERENCES "_matches_old"`. Boot must rebuild them, keep every row and index,
    and be a no-op the second time."""
    db = tmp_path / "broken.db"
    _init(db)
    _seed_rows(db)
    _break_like_the_old_migration(db)

    broken = _schema(db)
    children = ("match_performances", "game_stats", "proposal_sets")
    assert all("_matches_old" in broken[t] for t in children), "test setup: DDL should be broken"
    rows_before = _dump(db)
    indexes_before = {n for n, sql in broken.items() if "INDEX" in sql.split("(")[0]}
    assert "ix_proposal_sets_session_id" in indexes_before

    _init(db)

    repaired = _schema(db)
    assert not any("_matches_old" in sql for sql in repaired.values()), repaired
    assert not any(name.startswith("_new_") for name in repaired), repaired
    for t in children:
        assert "REFERENCES matches" in repaired[t]
    assert _dump(db) == rows_before, "every row must survive the repair"
    indexes_after = {n for n, sql in repaired.items() if "INDEX" in sql.split("(")[0]}
    assert indexes_after == indexes_before

    # Second boot: nothing left to do, schema byte-for-byte identical.
    _init(db)
    assert _schema(db) == repaired
    assert _dump(db) == rows_before

    # The repaired foreign keys resolve: with enforcement on, a child insert works
    # (in the broken state it failed with "no such table: main._matches_old").
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO game_stats (match_id, game_no, discord_id, won) VALUES (1, 3, 101, 1)")
    conn.rollback()
    conn.close()


def test_matches_rebuilds_do_not_break_child_foreign_keys(tmp_path):
    """The two `matches` rebuilds themselves must not rewrite child-table DDL."""
    from sqlalchemy import create_engine

    from bot.db.session import _fix_matches_autoincrement, _fix_matches_session_id_nullable

    db = tmp_path / "old.db"
    _init(db)
    _seed_rows(db)
    children_before = {k: v for k, v in _schema(db).items() if k != "matches"}
    rows_before = _dump(db)

    def downgrade_matches(session_id_null: str) -> None:
        """Put `matches` back into a historical shape without touching the children."""
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("ALTER TABLE matches RENAME TO m_tmp")
        conn.execute(
            f"CREATE TABLE matches (id INTEGER NOT NULL, session_id INTEGER {session_id_null}, "
            "game_date DATE, team1_json TEXT NOT NULL, team2_json TEXT NOT NULL, "
            "predicted_balance FLOAT, winner INTEGER, team1_wins INTEGER NOT NULL, "
            "team2_wins INTEGER NOT NULL, reported_by BIGINT, reported_at DATETIME, "
            "screenshot_url TEXT, PRIMARY KEY (id))")
        conn.execute("INSERT INTO matches SELECT * FROM m_tmp")
        conn.execute("DROP TABLE m_tmp")
        conn.commit()
        conn.close()

    engine = create_engine(f"sqlite:///{db}")
    for null_sql, fix in (("NOT NULL", _fix_matches_session_id_nullable),
                          ("", _fix_matches_autoincrement)):
        downgrade_matches(null_sql)
        assert "AUTOINCREMENT" not in _schema(db)["matches"]
        assert {k: v for k, v in _schema(db).items() if k != "matches"} == children_before
        with engine.begin() as c:
            fix(c)
        after = _schema(db)
        assert "AUTOINCREMENT" in after["matches"], fix.__name__
        assert {k: v for k, v in after.items() if k != "matches"} == children_before, fix.__name__
        assert not any("_matches_old" in sql for sql in after.values()), fix.__name__
        assert _dump(db) == rows_before, fix.__name__
    engine.dispose()


def test_autoincrement_high_water_mark_survives_rebuild(tmp_path):
    """Match 3 was deleted; after a rebuild the next match must be 4, not a reused 3."""
    from sqlalchemy import create_engine

    from bot.db.session import _rebuild_table

    db = tmp_path / "seq.db"
    _init(db)
    _seed_rows(db)
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as c:
        _rebuild_table(c, Match.__table__)
    engine.dispose()
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO matches (team1_json, team2_json, team1_wins, team2_wins) VALUES ('{}','{}',0,0)")
    conn.commit()
    assert conn.execute("SELECT MAX(id) FROM matches").fetchone()[0] == 4
    conn.close()


def test_boot_aborts_when_matches_old_table_exists(tmp_path):
    """A leftover `_matches_old` means an earlier migration crashed: never boot on it."""
    import pytest

    db = tmp_path / "crashed.db"
    _init(db)
    _seed_rows(db)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA legacy_alter_table=ON")
    conn.execute("ALTER TABLE matches RENAME TO _matches_old")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="_matches_old"):
        _init(db)
    # Nothing was created or changed by the aborted boot.
    schema = _schema(db)
    assert "matches" not in schema and "_matches_old" in schema


def test_failed_column_add_is_fatal(tmp_path):
    """_add_missing_columns used to log and carry on; now the boot must fail."""
    import pytest

    db = tmp_path / "badcol.db"
    conn = sqlite3.connect(str(db))
    # Same column, different case: the model's `alias` looks missing, and SQLite
    # (case-insensitive) rejects adding it as a duplicate.
    conn.execute("CREATE TABLE aliases (alias_norm VARCHAR(64) PRIMARY KEY, discord_id BIGINT, "
                 "ALIAS VARCHAR(64), created_at DATETIME)")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="aliases.alias"):
        _init(db)
    # The whole migration is one transaction: the failed boot left nothing behind.
    assert {n for n in _schema(db) if not n.startswith("sqlite_")} == {"aliases"}


def test_connections_use_wal_and_busy_timeout(tmp_path):
    cfg = _config(str(tmp_path / "pragmas.db"))

    async def body():
        await init_db(cfg)
        try:
            async with get_session() as s:
                conn = await s.connection()
                assert (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar() == "wal"
                assert (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar() == 15000
                assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 0
        finally:
            await close_db()

    asyncio.run(body())


def test_refuses_to_start_on_fly_without_a_mounted_volume(tmp_path, monkeypatch):
    import pytest

    db = tmp_path / "data" / "inhouse.db"
    db.parent.mkdir()
    monkeypatch.setenv("FLY_APP_NAME", "inhouse-league-bot")
    with pytest.raises(RuntimeError, match="not a mounted volume"):
        _init(db)
    assert not db.exists(), "must not create an empty database"

    # A real mount is accepted...
    monkeypatch.setattr("bot.db.session.os.path.ismount", lambda p: True)
    _init(db)
    assert db.exists()
    # ...and off Fly the check doesn't apply at all.
    monkeypatch.undo()
    _init(tmp_path / "local.db")


def test_backups_at_boot_and_on_demand(tmp_path):
    from bot.db import session as dbs

    db = tmp_path / "inhouse.db"
    backups = tmp_path / "backups"
    _init(db)
    assert not backups.exists(), "a brand-new database has nothing to back up"
    _seed_rows(db)

    async def body():
        await init_db(_config(str(db)))          # boot backup, taken before migrations
        try:
            boot = sorted(backups.glob("inhouse-*-boot.db"))
            assert len(boot) == 1
            made = [await dbs.backup_database("clear-matches") for _ in range(dbs.BACKUP_KEEP + 3)]
        finally:
            await close_db()
        return boot[0], made

    boot, made = asyncio.run(body())
    assert all(p is not None for p in made)
    kept = sorted(backups.glob("*.db"))
    assert len(kept) == dbs.BACKUP_KEEP, "only the newest 14 are kept"
    assert made[-1] in kept and boot not in kept
    # A backup is a complete, readable copy.
    conn = sqlite3.connect(str(made[-1]))
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM game_stats").fetchone()[0] == 12
    conn.close()


def test_boot_backup_is_taken_before_migrations(tmp_path):
    """The boot backup must capture the database as it was BEFORE this boot changed it."""
    db = tmp_path / "inhouse.db"
    _init(db)
    _seed_rows(db)
    _break_like_the_old_migration(db)
    _init(db)  # repairs the live DB
    assert "_matches_old" not in _schema(db)["game_stats"]
    (backup,) = (tmp_path / "backups").glob("inhouse-*-boot.db")
    assert "_matches_old" in _schema(backup)["game_stats"]


def test_backup_is_a_noop_for_in_memory_databases(tmp_path, monkeypatch):
    from bot.db import session as dbs

    monkeypatch.chdir(tmp_path)
    assert dbs.database_file("sqlite+aiosqlite:///:memory:") is None
    assert dbs.database_file("sqlite+aiosqlite://") is None
    assert dbs.database_file("sqlite+aiosqlite:////data/inhouse.db").as_posix() == "/data/inhouse.db"

    async def body():
        await init_db(_config(":memory:"))
        try:
            return await dbs.backup_database("boot")
        finally:
            await close_db()

    assert asyncio.run(body()) is None
    assert not (tmp_path / "backups").exists()
