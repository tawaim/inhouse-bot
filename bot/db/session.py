"""Async SQLAlchemy session factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Config
from bot.db.models import Base

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


async def init_db(config: Config) -> None:
    """Create tables if missing, then add any columns the models have gained."""
    global _engine, _sessionmaker
    _engine = create_async_engine(config.database_url, echo=False, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


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
