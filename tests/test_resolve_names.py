"""Tests for /resolve-names name matching and the alias table.

Drives the real resolve_name_block against a temp SQLite DB and a fake guild
whose get_member returns lightweight stand-ins (display_name / name / nick /
global_name) — no Discord gateway needed.
"""
import json
import types

from sqlalchemy import select

from bot.cogs.admin import AdminCog, resolve_name_block
from bot.config import Config
from bot.db.models import Alias, GuildConfig, Match, Player
from bot.db.session import close_db, get_session, init_db


def _config(db_path: str) -> Config:
    return Config(
        discord_token="x", discord_guild_id=1, owner_discord_id=1,
        riot_api_key="x", riot_region="na1", riot_regional_route="americas",
        admin_role_name="Admin", database_url=f"sqlite+aiosqlite:///{db_path}",
        timezone="UTC",
    )


def _member(display_name, name=None, nick=None, global_name=None):
    return types.SimpleNamespace(
        display_name=display_name, name=name or display_name,
        nick=nick, global_name=global_name,
    )


# discord_id -> (riot_game_name, fake member)
PLAYERS = {
    1: ("Carter", _member("carter_k", name="carterk")),
    2: ("Robo", _member("Robo")),
    3: ("Robby", _member("Robby")),
    4: ("JR", _member("JR")),
    5: ("Kaari", _member("kaari (inhouse arena champ)")),
}


class _FakeGuild:
    def __init__(self, members):
        self._members = members

    def get_member(self, did):
        return self._members.get(did)

    def get_channel(self, cid):
        return None  # no category resolvable in these tests


async def _seed_players():
    async with get_session() as db:
        for pid, (riot, _m) in PLAYERS.items():
            db.add(Player(
                discord_id=pid, riot_game_name=riot, riot_tag_line="NA1",
                riot_puuid=f"puuid{pid}", link_status="approved",
            ))
        await db.commit()


def _guild():
    return _FakeGuild({pid: m for pid, (_r, m) in PLAYERS.items()})


def test_exact_match_resolves_and_learns(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "a.db")))
        try:
            await _seed_players()
            block, notes, learned = await resolve_name_block(_guild(), "Carter")
            assert block == "<@1>"
            assert learned == 1
            # The exact match was persisted as an alias.
            async with get_session() as db:
                row = await db.get(Alias, "carter")
                assert row is not None and row.discord_id == 1
        finally:
            await close_db()

    asyncio.run(body())


def test_paren_suffix_display_name_matches(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "b.db")))
        try:
            await _seed_players()
            # 'kaari' should match display name 'kaari (inhouse arena champ)'.
            block, _notes, _learned = await resolve_name_block(_guild(), "kaari")
            assert block == "<@5>"
        finally:
            await close_db()

    asyncio.run(body())


def test_alias_takes_priority_over_exact(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "c.db")))
        try:
            await _seed_players()
            # Point 'robo' at Robby's id; alias must win over the exact Robo match.
            async with get_session() as db:
                db.add(Alias(alias_norm="robo", discord_id=3, alias="Robo"))
                await db.commit()
            block, _notes, learned = await resolve_name_block(_guild(), "Robo")
            assert block == "<@3>"
            assert learned == 0  # alias hit doesn't re-learn
        finally:
            await close_db()

    asyncio.run(body())


def test_roster_lines_and_headers_preserved(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "d.db")))
        try:
            await _seed_players()
            text = "TEAM 1\nTOP: Carter\nJUNGLE: Robo\n"
            block, _notes, _learned = await resolve_name_block(_guild(), text)
            assert block == "TEAM 1\nTOP: <@1>\nJUNGLE: <@2>"
        finally:
            await close_db()

    asyncio.run(body())


def test_fuzzy_guess_single_candidate(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "e.db")))
        try:
            await _seed_players()
            block, notes, learned = await resolve_name_block(_guild(), "Carte")
            assert "<@1>" in block
            assert learned == 0  # fuzzy is never auto-saved
            assert any("Guessed" in n for n in notes)
        finally:
            await close_db()

    asyncio.run(body())


def test_ambiguous_fuzzy_flagged_not_resolved(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "f.db")))
        try:
            await _seed_players()
            # 'Rob' is close to both Robo and Robby -> ambiguous, no mention emitted.
            block, notes, _learned = await resolve_name_block(_guild(), "Rob")
            assert "<@2>" not in block and "<@3>" not in block
            assert any("close to" in n for n in notes)
        finally:
            await close_db()

    asyncio.run(body())


def test_no_match_reported(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "g.db")))
        try:
            await _seed_players()
            block, notes, _learned = await resolve_name_block(_guild(), "Zzqxywv")
            assert "❌" in block
            assert any("No match" in n for n in notes)
        finally:
            await close_db()

    asyncio.run(body())


# --- create_channels flag: early returns of the shared channel builder ---

def test_build_channels_match_not_found(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "h.db")))
        try:
            dummy = types.SimpleNamespace()  # method doesn't touch self before these returns
            result = await AdminCog._build_match_channels(dummy, _guild(), 1, match_id=999)
            assert "not found" in result
        finally:
            await close_db()

    asyncio.run(body())


def test_build_channels_no_category_configured(tmp_path):
    import asyncio

    async def body():
        await init_db(_config(str(tmp_path / "i.db")))
        try:
            async with get_session() as db:
                db.add(GuildConfig(guild_id=1, match_category_id=None))
                m = Match(
                    session_id=None,
                    team1_json=json.dumps({"TOP": 1}),
                    team2_json=json.dumps({"TOP": 2}),
                )
                db.add(m)
                await db.commit()
                await db.refresh(m)
                mid = m.id
            dummy = types.SimpleNamespace()
            result = await AdminCog._build_match_channels(dummy, _guild(), 1, match_id=mid)
            assert "No match category configured" in result
        finally:
            await close_db()

    asyncio.run(body())
