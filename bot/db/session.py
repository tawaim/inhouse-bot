"""Async SQLAlchemy session factory, startup migrations, and backups."""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Optional

from sqlalchemy import event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from bot.config import Config
from bot.db.models import Base, Match

log = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_db_path: Optional[Path] = None  # the SQLite file; None for in-memory databases

BACKUP_KEEP = 14  # newest N files kept in <db dir>/backups


def database_file(database_url: str) -> Optional[Path]:
    """The SQLite file a database URL points at, or None for in-memory / non-file DBs."""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    db = url.database
    if not db or db == ":memory:" or db.startswith("file:"):
        return None
    return Path(db)


def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
    """Runs on every new connection. WAL lets readers proceed while one writer
    holds the lock; busy_timeout makes a second writer wait instead of failing
    immediately with 'database is locked'.

    Deliberately does NOT enable PRAGMA foreign_keys: the bot has always run
    with them off and the delete paths rely on that.
    """
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA journal_mode=WAL")
    finally:
        cur.close()


def _require_mounted_volume(db_path: Optional[Path]) -> None:
    """On Fly, refuse to start unless the DB directory is a real mount.

    If the volume is missing (detached, wrong region, renamed) the image's empty
    /data directory would silently get a brand-new empty database and the league
    would appear wiped. Failing loudly here keeps the real data safe on the volume.
    """
    if not os.environ.get("FLY_APP_NAME") or db_path is None:
        return
    directory = os.path.dirname(os.path.abspath(str(db_path)))
    if not os.path.ismount(directory):
        raise RuntimeError(
            f"Refusing to start: running on Fly (FLY_APP_NAME is set) but the database "
            f"directory {directory!r} is not a mounted volume. Starting now would create an "
            f"empty database. Check the [[mounts]] section of fly.toml and `fly volumes list`."
        )


def _default_literal(col) -> Optional[str]:
    """SQL literal for a column's scalar Python-side default, or None if it has
    none (or the default is a callable such as datetime.utcnow)."""
    default = getattr(col, "default", None)
    if default is None or getattr(default, "is_callable", False):
        return None
    arg = getattr(default, "arg", None)
    if isinstance(arg, bool):
        return "1" if arg else "0"
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        return "'" + arg.replace("'", "''") + "'"
    return None


def _add_missing_columns(conn) -> None:
    """Add columns that exist in the models but not yet in the live DB.

    `create_all` only creates whole missing tables; it never adds new columns to
    an existing table. This lightweight forward-only migration ALTERs in any
    columns the models gained since the DB was created, so deploys don't crash
    with 'no such column'. Idempotent. Any failure aborts startup.
    """
    insp = inspect(conn)
    dialect = conn.dialect
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            coltype = col.type.compile(dialect=dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            # Include a scalar default so existing rows get a sane value; skip
            # callable defaults (e.g. datetime.utcnow) — those fill in on insert.
            lit = _default_literal(col)
            if lit is not None:
                ddl += f" DEFAULT {lit}"
            # A failure here is fatal: booting with a column the models expect
            # but the DB lacks only moves the crash to the first query that uses it.
            try:
                conn.exec_driver_sql(ddl)
            except Exception as e:
                raise RuntimeError(
                    f"Schema migration failed: could not add {table.name}.{col.name} ({e})"
                ) from e
            log.info("Schema migration: added %s.%s", table.name, col.name)


def _rebuild_table(conn, table) -> None:
    """Rebuild `table` from its model, preserving every row and index.

    Uses the order SQLite documents for schema changes ALTER can't express:
    create the new table under a temporary name, copy the rows, drop the old
    table, rename the new one into place. The obvious alternative — rename the
    old table away first — is what broke production: since SQLite 3.26,
    `ALTER TABLE x RENAME TO y` also rewrites every OTHER table's
    `REFERENCES x` to `REFERENCES y`, so child tables were left pointing at a
    temporary table that was then dropped.

    Must run inside a transaction with foreign keys off (see _run_migrations).
    """
    name = table.name
    tmp = f"_new_{name}"
    old_cols = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{name}")').fetchall()}

    # Copy shared columns straight across. NOT NULL columns get their model
    # default wherever the old row has NULL or the old table lacks the column
    # (SQLAlchemy's `default=` is ORM-applied, not a SQL DEFAULT).
    insert_names: list[str] = []
    select_exprs: list[str] = []
    for col in table.columns:
        lit = _default_literal(col)
        if col.name in old_cols:
            expr = f'"{col.name}"'
            if not col.nullable and not col.primary_key and lit is not None:
                expr = f"COALESCE({expr}, {lit})"
        elif not col.nullable:
            expr = lit if lit is not None else "0"
        else:
            continue
        insert_names.append(col.name)
        select_exprs.append(expr)
    cols_sql = ", ".join(f'"{c}"' for c in insert_names)

    old_indexes = conn.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (name,)
    ).fetchall()
    has_seq = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
    ).fetchone() is not None
    old_seq = None
    if has_seq:
        row = conn.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name=?", (name,)).fetchone()
        old_seq = row[0] if row else None
    rows_before = conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{name}"').scalar()

    ddl = str(CreateTable(table).compile(dialect=conn.dialect))
    new_ddl, n = re.subn(
        r'CREATE TABLE\s+"?' + re.escape(name) + r'"?\s*\(', f'CREATE TABLE "{tmp}" (', ddl, count=1
    )
    if n != 1:
        raise RuntimeError(f"Schema migration: could not derive DDL for {name}")
    conn.exec_driver_sql(new_ddl)
    conn.exec_driver_sql(
        f'INSERT INTO "{tmp}" ({cols_sql}) SELECT {", ".join(select_exprs)} FROM "{name}"'
    )
    conn.exec_driver_sql(f'DROP TABLE "{name}"')            # also drops its indexes
    conn.exec_driver_sql(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')

    for idx in table.indexes:
        idx.create(conn, checkfirst=True)
    for idx_name, idx_sql in old_indexes:                   # indexes the model doesn't declare
        exists = conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (idx_name,)
        ).fetchone()
        if exists is None:
            conn.exec_driver_sql(idx_sql)

    # Keep the AUTOINCREMENT high-water mark so ids of deleted rows stay retired.
    if old_seq is not None and "AUTOINCREMENT" in new_ddl.upper():
        row = conn.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name=?", (name,)).fetchone()
        if row is None:
            conn.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (name, old_seq))
        elif row[0] < old_seq:
            conn.exec_driver_sql("UPDATE sqlite_sequence SET seq=? WHERE name=?", (old_seq, name))

    rows_after = conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{name}"').scalar()
    if rows_after != rows_before:
        raise RuntimeError(
            f"Schema migration: rebuilding {name} changed its row count "
            f"({rows_before} -> {rows_after}); aborting"
        )


def _fix_matches_session_id_nullable(conn) -> None:
    """Make matches.session_id nullable if an old schema created it NOT NULL.

    Sessionless pickup games (/pickup-series) store session_id=NULL, but the
    original `matches` table declared it NOT NULL. SQLite can't drop a NOT NULL
    constraint in place and create_all won't alter an existing table, so we
    rebuild the table (see _rebuild_table). Data-preserving and idempotent.
    """
    insp = inspect(conn)
    if not insp.has_table("matches"):
        return
    sid = next((c for c in insp.get_columns("matches") if c["name"] == "session_id"), None)
    if sid is None or sid.get("nullable", True):
        return  # already nullable (or column missing) — nothing to do

    log.warning("Schema migration: rebuilding `matches` to make session_id NULLABLE")
    _rebuild_table(conn, Match.__table__)
    log.info("Schema migration: matches.session_id is now nullable")


def _fix_matches_autoincrement(conn) -> None:
    """Rebuild `matches` with a true AUTOINCREMENT id if it lacks one.

    A plain `INTEGER PRIMARY KEY` reuses the highest freed rowid after a delete,
    so a deleted match's id gets recycled by the next insert (this is how two
    different series both became "Match 5"). AUTOINCREMENT tracks a high-water
    mark in `sqlite_sequence` and never reuses ids. create_all won't alter an
    existing table, so rebuild it (see _rebuild_table). Row ids are preserved;
    copying them seeds sqlite_sequence to the current max. Runs after
    _add_missing_columns so the old table has every model column to copy across.
    Data-preserving and idempotent.
    """
    insp = inspect(conn)
    if not insp.has_table("matches"):
        return
    row = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='matches'"
    ).fetchone()
    if row and "AUTOINCREMENT" in (row[0] or "").upper():
        return  # already migrated

    log.warning("Schema migration: rebuilding `matches` to add AUTOINCREMENT")
    _rebuild_table(conn, Match.__table__)
    log.info("Schema migration: matches.id is now AUTOINCREMENT")


def _abort_if_crashed_migration(conn) -> None:
    """A leftover `_matches_old` table means an earlier `matches` rebuild died
    half-way; `matches` may be missing or empty. Never guess — stop the boot."""
    row = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_matches_old'"
    ).fetchone()
    if row is not None:
        raise RuntimeError(
            "Refusing to start: the database contains a `_matches_old` table, left behind by "
            "a schema migration that crashed part-way. `matches` may be incomplete. Restore "
            "from a backup (see the `backups` folder next to the database) or repair by hand."
        )


def _repair_matches_old_references(conn) -> None:
    """Rebuild child tables whose foreign keys point at `_matches_old`.

    Earlier versions of the `matches` rebuilds renamed `matches` to
    `_matches_old` first; SQLite >= 3.26 then rewrote the foreign keys of
    match_performances / proposal_sets / game_stats to reference
    `_matches_old`, which was dropped moments later. Harmless only while
    foreign keys stay off. Idempotent: does nothing once the DDL is clean.
    """
    rows = conn.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name != '_matches_old'"
    ).fetchall()
    for name, sql in rows:
        if "_matches_old" not in (sql or ""):
            continue
        table = Base.metadata.tables.get(name)
        if table is None:
            raise RuntimeError(
                f"Schema migration: table {name!r} references `_matches_old` but is not a "
                f"known model table; repair it by hand."
            )
        log.warning("Schema migration: rebuilding `%s` (its foreign key pointed at _matches_old)", name)
        _rebuild_table(conn, table)
        log.info("Schema migration: `%s` foreign keys repaired", name)


def _run_migrations(conn) -> None:
    """Create missing tables, then run the forward-only migrations — all inside
    one explicit transaction, so a failure rolls everything back and aborts boot."""
    _abort_if_crashed_migration(conn)
    # This pragma is a no-op inside a transaction, so set it first. Foreign keys
    # are off by default; table rebuilds require it and the bot runs with it off.
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    # The sqlite3 driver doesn't open a transaction for DDL on its own; without
    # this each CREATE/DROP/RENAME would commit individually.
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    Base.metadata.create_all(conn)
    _fix_matches_session_id_nullable(conn)
    _add_missing_columns(conn)
    _fix_matches_autoincrement(conn)
    _repair_matches_old_references(conn)


async def backup_database(reason: str = "manual") -> Optional[Path]:
    """Write a consistent copy of the database to `<db dir>/backups/` and prune
    to the newest BACKUP_KEEP files. Returns the new file's path.

    Uses `VACUUM INTO`, which is safe while the bot is running. No-op (returns
    None) for in-memory databases or before the database file exists. Raises if
    the backup itself fails, so destructive commands can refuse to proceed.
    """
    if _engine is None or _db_path is None:
        return None
    if not _db_path.exists() or _db_path.stat().st_size == 0:
        return None
    backup_dir = _db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    tag = re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") or "manual"
    target = backup_dir / f"{_db_path.stem}-{stamp}-{tag}.db"
    async with _engine.connect() as conn:
        # VACUUM can't run inside a transaction.
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM INTO ?", (str(target),))
    log.info("Database backup written: %s (%s)", target, reason)

    backups = sorted(backup_dir.glob(f"{_db_path.stem}-*.db"), key=lambda f: f.name)
    for stale in backups[:-BACKUP_KEEP]:
        try:
            stale.unlink()
        except OSError:
            log.warning("Could not delete old backup %s", stale)
    return target


async def init_db(config: Config) -> None:
    """Open the database, back it up, then create tables and run migrations."""
    global _engine, _sessionmaker, _db_path
    db_path = database_file(config.database_url)
    _require_mounted_volume(db_path)
    existed = db_path is not None and db_path.exists() and db_path.stat().st_size > 0

    _engine = create_async_engine(config.database_url, echo=False, future=True)
    event.listen(_engine.sync_engine, "connect", _set_sqlite_pragmas)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    _db_path = db_path

    if existed:
        # Snapshot BEFORE migrations touch anything. A failed backup is logged but
        # doesn't keep the bot down (a full disk shouldn't cancel game night).
        try:
            await backup_database("boot")
        except Exception:
            log.exception("Boot backup failed; continuing without one")

    async with _engine.begin() as conn:
        await conn.run_sync(_run_migrations)
        players = (await conn.exec_driver_sql("SELECT COUNT(*) FROM players")).scalar()
        matches = (await conn.exec_driver_sql("SELECT COUNT(*) FROM matches")).scalar()
    log.info(
        "Database ready: %s — %s players, %s matches%s",
        db_path if db_path is not None else "(in-memory)", players, matches,
        "" if existed or db_path is None else " (new database file created)",
    )


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh session. Caller is responsible for committing."""
    if _sessionmaker is None:
        raise RuntimeError("DB not initialized. Call init_db() first.")
    async with _sessionmaker() as session:
        yield session


async def close_db() -> None:
    if _engine is None:
        return
    await _engine.dispose()
