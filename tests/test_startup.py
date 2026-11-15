"""Process-level startup behaviour in bot.main, plus deploy-config sanity checks.

bot.main.amain() is run for real with the Discord client, database, scheduler and
API clients replaced by stand-ins, so nothing connects anywhere.
"""
import asyncio
import tomllib
import types
from pathlib import Path

import pytest
from discord.ext import commands

import bot.main as main_mod

ROOT = Path(__file__).resolve().parent.parent


class _FakeTree:
    def __init__(self):
        self.syncs = 0
        self.fail_next = False

    def copy_global_to(self, guild):
        pass

    async def sync(self, guild=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("discord hiccup")
        self.syncs += 1
        return ["cmd"]


class _FakeBot:
    """Just enough of commands.Bot for amain()."""
    instances: list["_FakeBot"] = []
    start_error: Exception | None = None

    def __init__(self, command_prefix=None, intents=None, **kwargs):
        self.command_prefix = command_prefix
        self.intents = intents
        self.tree = _FakeTree()
        self.user = types.SimpleNamespace(id=999)
        self.events = {}
        self.closed = False
        self.on_start = None
        _FakeBot.instances.append(self)

    async def add_cog(self, cog):
        pass

    def add_view(self, view, **kwargs):
        pass

    def event(self, fn):
        self.events[fn.__name__] = fn
        return fn

    async def start(self, token):
        if self.on_start is not None:
            await self.on_start(self)
        if _FakeBot.start_error is not None:
            raise _FakeBot.start_error

    async def close(self):
        self.closed = True


class _Closable:
    def __init__(self, *a, **k):
        pass

    async def close(self):
        pass


@pytest.fixture
def fake_main(monkeypatch):
    _FakeBot.instances.clear()
    _FakeBot.start_error = None
    closed = []
    config = types.SimpleNamespace(
        discord_token="x", discord_guild_id=1, owner_discord_id=1, riot_api_key="x",
        riot_region="na1", riot_regional_route="americas", admin_role_name="Admin",
        database_url="sqlite+aiosqlite:///:memory:", timezone="UTC",
    )

    async def fake_init_db(_config):
        pass

    async def fake_close_db():
        closed.append(True)

    scheduler = types.SimpleNamespace(start=lambda: None, get_jobs=lambda: [],
                                      shutdown=lambda wait=False: closed.append("scheduler"))
    monkeypatch.setattr(main_mod.Config, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(main_mod, "init_db", fake_init_db)
    monkeypatch.setattr(main_mod, "close_db", fake_close_db)
    monkeypatch.setattr(main_mod.commands, "Bot", _FakeBot)
    monkeypatch.setattr(main_mod, "RiotClient", _Closable)
    monkeypatch.setattr(main_mod, "OpggClient", _Closable)
    monkeypatch.setattr(main_mod, "setup_scheduler", lambda bot, config: scheduler)
    return closed


def test_bot_crash_propagates_so_the_process_exits_nonzero(fake_main):
    """A bad token / disabled intent kills bot.start(); that must not exit 0."""
    _FakeBot.start_error = RuntimeError("Improper token has been passed.")
    with pytest.raises(RuntimeError, match="Improper token"):
        asyncio.run(main_mod.amain())
    assert True in fake_main and "scheduler" in fake_main, "still cleans up before failing"
    assert _FakeBot.instances[0].closed

    # main() lets it through too (an uncaught exception is exit status 1).
    with pytest.raises(RuntimeError, match="Improper token"):
        main_mod.main()


def test_clean_stop_still_exits_normally(fake_main):
    asyncio.run(main_mod.amain())   # bot.start() returns without error
    assert _FakeBot.instances[0].closed


def test_uses_mention_prefix_and_syncs_commands_once_per_process(fake_main):
    seen = {}

    async def reconnect_a_few_times(bot):
        on_ready = bot.events["on_ready"]
        bot.tree.fail_next = True
        with pytest.raises(RuntimeError):
            await on_ready()            # first sync fails...
        await on_ready()                # ...so the next READY retries it
        await on_ready()                # reconnects after that don't sync again
        await on_ready()
        seen["syncs"] = bot.tree.syncs

    _FakeBot.instances.clear()
    orig_init = _FakeBot.__init__

    def init(self, *a, **k):
        orig_init(self, *a, **k)
        self.on_start = reconnect_a_few_times

    _FakeBot.__init__ = init
    try:
        asyncio.run(main_mod.amain())
    finally:
        _FakeBot.__init__ = orig_init
    assert seen["syncs"] == 1
    bot = _FakeBot.instances[0]
    # "!" needs the message_content intent (off) and warns on every boot.
    assert bot.command_prefix is commands.when_mentioned
    assert bot.intents.message_content is False


def test_fly_restarts_the_machine_on_any_exit():
    cfg = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))
    assert cfg["restart"] == [{"policy": "always"}]
    assert cfg["mounts"][0]["destination"] == "/data"
    assert "http_service" not in cfg and "services" not in cfg, "worker app: no inbound services"


def test_env_example_and_readme_list_every_env_var_the_bot_reads():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "OWNER_DISCORD_ID", "RIOT_API_KEY",
                 "ANTHROPIC_API_KEY", "DATABASE_URL"):
        assert name in env_example, f"{name} missing from .env.example"
        assert f"`{name}`" in readme, f"{name} missing from README"
