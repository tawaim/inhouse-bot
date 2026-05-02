"""Async SQLAlchemy session factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import Config
from bot.db.models import Base

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def init_db(config: Config) -> None:
    """Create tables if missing. Called once at startup."""
    global _engine, _sessionmaker
    _engine = create_async_engine(config.database_url, echo=False, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
