"""Tests for recruitment posting: open-ended vs scheduled close.

post_recruitment doesn't touch self.bot when a channel is passed, and the only
Discord call is channel.send(), so we drive it with a tiny stub channel against
a temp DB — enough to catch the open-ended close-time regression.
"""
import asyncio
import types
from datetime import date

from bot.config import Config
from bot.cogs.recruitment import RecruitmentCog
from bot.db.session import close_db, init_db


def _config(db_path: str) -> Config:
    return Config(
        discord_token="x", discord_guild_id=1, owner_discord_id=1, riot_api_key="x",
        riot_region="na1", riot_regional_route="americas", admin_role_name="Admin",
        database_url=f"sqlite+aiosqlite:///{db_path}", timezone="UTC",
    )


class _Msg:
    id = 555


class _Chan:
    id = 999

    async def send(self, **kwargs):
        return _Msg()


def test_open_ended_post_has_no_close(tmp_path):
    cfg = _config(str(tmp_path / "r1.db"))

    async def body():
        await init_db(cfg)
        cog = RecruitmentCog(types.SimpleNamespace(), cfg)
        session = await cog.post_recruitment(date(2026, 6, 4), channel=_Chan(), open_ended=True)
        assert session.signups_close_at is None  # open-ended => no scheduled close
        assert session.status == "recruiting"
        await close_db()

    asyncio.run(body())


def test_scheduled_post_sets_close(tmp_path):
    cfg = _config(str(tmp_path / "r2.db"))

    async def body():
        await init_db(cfg)
        cog = RecruitmentCog(types.SimpleNamespace(), cfg)
        session = await cog.post_recruitment(date(2026, 6, 4), channel=_Chan(), open_ended=False)
        assert session.signups_close_at is not None  # Monday-before 9:30 PM
        await close_db()

    asyncio.run(body())
