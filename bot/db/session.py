"""Async SQLAlchemy session factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Config
from bot.db.models import Base, Match

log = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _add_missing_columns(conn) -> None:
    """Add columns that exist in the models but not yet in the live DB.

    `create_all` only creates whole missing tables; it never adds new columns to
    an existing table. This lightweight forward-only migration ALTERs in any
    columns the models gained since the DB was created, so deploys don't crash
    with 'no such column'. Idempotent and best-effort (per-column try/except).
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
            default = getattr(col, "default", None)
            if default is not None and not getattr(default, "is_callable", False):
                arg = getattr(default, "arg", None)
                if isinstance(arg, bool):
                    ddl += f" DEFAULT {1 if arg else 0}"
                elif isinstance(arg, (int, float)):
                    ddl += f" DEFAULT {arg}"
                elif isinstance(arg, str):
                    ddl += f" DEFAULT '{arg}'"
            try:
                conn.exec_driver_sql(ddl)
                log.info("Schema migration: added %s.%s", table.name, col.name)
            except Exception:
                log.exception("Schema migration: failed to add %s.%s", table.name, col.name)


def _fix_matches_session_id_nullable(conn) -> None:
    """Make matches.session_id nullable if an old schema created it NOT NULL.

    Sessionless pickup games (/pickup-series) store session_id=NULL, but the
    original `matches` table declared it NOT NULL. SQLite can't drop a NOT NULL
    constraint in place and create_all won't alter an existing table, so we
    rebuild the table (rename → recreate from the model → copy rows → drop old).
    Data-preserving and idempotent.
    """
    insp = inspect(conn)
    if not insp.has_table("matches"):
        return
    sid = next((c for c in insp.get_columns("matches") if c["name"] == "session_id"), None)
    if sid is None or sid.get("nullable", True):
        return  # already nullable (or column missing) — nothing to do

    log.warning("Schema migration: rebuilding `matches` to make session_id NULLABLE")
    old_cols = [c["name"] for c in insp.get_columns("matches")]
    # Copy shared columns straight across. For NOT-NULL columns the old table
    # lacks (e.g. team1_wins/team2_wins added later), supply their default as a
    # literal — SQLAlchemy's `default=` is ORM-applied, not a SQL DEFAULT.
    insert_names = [c.name for c in Match.__table__.columns if c.name in old_cols]
    select_exprs = [f'"{c}"' for c in insert_names]
    for col in Match.__table__.columns:
        if col.name in old_cols or col.nullable:
            continue
        arg = getattr(getattr(col, "default", None), "arg", None)
        if isinstance(arg, bool):
            lit = "1" if arg else "0"
        elif isinstance(arg, (int, float)):
            lit = str(arg)
        elif isinstance(arg, str):
            lit = "'" + arg.replace("'", "''") + "'"
        else:
            lit = "0"
        insert_names.append(col.name)
        select_exprs.append(lit)
    cols_sql = ", ".join(f'"{c}"' for c in insert_names)
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.exec_driver_sql("ALTER TABLE matches RENAME TO _matches_old")
    Match.__table__.create(conn)  # recreate with the current (nullable) schema
    conn.exec_driver_sql(
        f"INSERT INTO matches ({cols_sql}) SELECT {', '.join(select_exprs)} FROM _matches_old"
    )
    conn.exec_driver_sql("DROP TABLE _matches_old")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    log.info("Schema migration: matches.session_id is now nullable")


def _fix_matches_autoincrement(conn) -> None:
    """Rebuild `matches` with a true AUTOINCREMENT id if it lacks one.

    A plain `INTEGER PRIMARY KEY` reuses the highest freed rowid after a delete,
    so a deleted match's id gets recycled by the next insert (this is how two
    different series both became "Match 5"). AUTOINCREMENT tracks a high-water
    mark in `sqlite_sequence` and never reuses ids. create_all won't alter an
    existing table, so rebuild it (rename → recreate from the model → copy rows →
    drop old). Row ids are preserved; copying them seeds sqlite_sequence to the
    current max. Runs after _add_missing_columns so the old table has every model
    column to copy across. Data-preserving and idempotent.
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
    old_cols = {c["name"] for c in insp.get_columns("matches")}
    copy = [c.name for c in Match.__table__.columns if c.name in old_cols]
    copy_sql = ", ".join(f'"{c}"' for c in copy)
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.exec_driver_sql("ALTER TABLE matches RENAME TO _matches_old")
    Match.__table__.create(conn)  # recreate WITH sqlite_autoincrement
    conn.exec_driver_sql(
        f"INSERT INTO matches ({copy_sql}) SELECT {copy_sql} FROM _matches_old"
    )
    conn.exec_driver_sql("DROP TABLE _matches_old")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    log.info("Schema migration: matches.id is now AUTOINCREMENT")


async def init_db(config: Config) -> None:
    """Create tables if missing, then run lightweight forward-only migrations."""
    global _engine, _sessionmaker
    _engine = create_async_engine(config.database_url, echo=False, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_fix_matches_session_id_nullable)
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_fix_matches_autoincrement)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a fresh session. Caller is responsible for committing."""
    if _sessionmaker is None:
        raise RuntimeError("DB not initialized. Call init_db() first.")
    async with _sessionmaker() as session:
        yield session


async def close_db() -> None:
    if _engine is not None:
        await _engine.dispose()
