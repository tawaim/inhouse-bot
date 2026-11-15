"""End-to-end tests for rank syncing and seeding: /sync-ranks, /reseed-all,
/refresh-seed, and the safety backups around the destructive admin commands.
"""
import sqlite3

import pytest
from sqlalchemy import select

from bot.db.models import Match, Player, Rating
from bot.db.session import database_file, get_session
from bot.services.opgg_client import PastSeasonRank
from bot.services.riot_client import RiotAuthError, RiotUnavailable
from tests.harness import assert_ratings_consistent, flow


async def _seeds(world) -> dict[int, int]:
    """discord_id -> base_seed (asserting all six rows agree)."""
    out: dict[int, set] = {}
    for (pid, _role), (_elo, base, _mod, _games) in (await world.ratings()).items():
        out.setdefault(pid, set()).add(base)
    assert all(len(v) == 1 for v in out.values()), out
    return {pid: v.pop() for pid, v in out.items()}


async def _set_player(discord_id: int, **fields) -> None:
    async with get_session() as db:
        p = await db.get(Player, discord_id)
        for k, v in fields.items():
            setattr(p, k, v)
        await db.commit()


def _past(tier="EMERALD", division="III", seasons=1, date_known=True) -> PastSeasonRank:
    return PastSeasonRank(tier=tier, division=division, season_id=31,
                          seasons_elapsed=seasons, date_known=date_known)


# ----------------------------------------------------------------- item 2: outages never look like "unranked"

@flow
async def test_riot_outage_keeps_seed_and_is_reported_by_name(world):
    await world.add_players(2, elo=[1500, 1700])
    world.riot.ranks["puuid101"] = ("GOLD", "IV", 10)                    # 1400
    world.riot.fail_puuids["puuid102"] = RiotUnavailable("retries exhausted (HTTP 503)")
    # If the outage were mistaken for "unranked", this would re-seed P102 to 800.
    world.opgg.past["p102#na1"] = _past("IRON", "IV", seasons=0)

    ix = await world.run(world.admin_cog.sync_ranks)

    assert await _seeds(world) == {101: 1400, 102: 1700}
    assert (await world.player(101)).riot_last_synced is not None
    assert (await world.player(102)).riot_last_synced is None, "a failed lookup is not a sync"
    assert world.opgg.lookups == [], "OP.GG must not be consulted for a player Riot didn't answer for"
    assert "Synced 1 players" in ix.text and "1 errors" in ix.text
    assert "P102 (Riot unavailable)" in ix.text
    assert "P101: 1500 → 1400" in ix.text and "rank change: unranked → GOLD IV" in ix.text


@flow
async def test_total_outage_changes_nothing(world):
    await world.add_players(3, elo=1650)
    world.riot.fail = RiotUnavailable("timeout")
    ix = await world.run(world.admin_cog.sync_ranks)
    assert await _seeds(world) == {101: 1650, 102: 1650, 103: 1650}
    assert "3 errors" in ix.text and all(f"P{n}" in ix.text for n in (101, 102, 103))

    # The Monday close job uses the same routine and must not raise.
    result = await world.admin_cog._refresh_all_base_seeds()
    assert result["errors"] == 3 and result["updated"] == 0


# ----------------------------------------------------------------- item 3: no DB lock held across network calls

@flow
async def test_no_database_lock_is_held_while_waiting_on_riot(world):
    await world.add_players(3, elo=1500)
    for n in (101, 102, 103):
        world.riot.ranks[f"puuid{n}"] = ("PLATINUM", "IV", 0)
    db_file = str(database_file(world.config.database_url))
    real = world.riot.get_solo_rank
    writes = []

    async def write_from_elsewhere_then_answer(puuid):
        # Another part of the bot (an RSVP click, a /report) writes while the sync
        # is mid-way through its Riot calls. timeout=0: fail at once if locked.
        conn = sqlite3.connect(db_file, timeout=0)
        try:
            conn.execute("UPDATE guild_config SET results_channel_id = ?", (len(writes),))
            conn.commit()
            writes.append(puuid)
        finally:
            conn.close()
        return await real(puuid)

    world.riot.get_solo_rank = write_from_elsewhere_then_answer
    await world.run(world.admin_cog.sync_ranks)
    assert len(writes) == 3
    assert await _seeds(world) == {101: 1600, 102: 1600, 103: 1600}


# ----------------------------------------------------------------- item 4: /sync-ranks output

@flow
async def test_sync_ranks_reports_a_rejected_api_key(world):
    await world.add_players(2, elo=1500)
    world.riot.ranks["puuid101"] = ("GOLD", "IV", 0)
    world.riot.fail_puuids["puuid102"] = RiotAuthError("403")
    ix = await world.run(world.admin_cog.sync_ranks)
    assert "key rejected" in ix.text
    assert await _seeds(world) == {101: 1500, 102: 1500}, "an auth failure must not half-apply"


@flow
async def test_sync_ranks_lists_changes_skips_and_errors(world):
    await world.add_players(5, elo=1500)
    world.riot.ranks["puuid101"] = ("GOLD", "II", 55)          # 1500 -> 1500: no change, not listed
    world.riot.ranks["puuid102"] = ("DIAMOND", "IV", 0)        # 1500 -> 2000
    await _set_player(102, solo_tier="EMERALD", solo_rank="I", solo_lp=99)
    await _set_player(103, manual_seed=True)
    world.riot.fail_puuids["puuid104"] = RiotUnavailable("x")
    world.opgg.past["p105#na1"] = _past("EMERALD", "III", seasons=2)   # 1850 - 200

    ix = await world.run(world.admin_cog.sync_ranks)
    text = ix.text
    assert "P102: 1500 → 2000 — rank change: EMERALD I → DIAMOND IV" in text
    assert "P105: 1500 → 1650 — past-season decay: last ranked EMERALD III 2 season(s) ago, 1850 − 200" in text
    assert "P101" not in text
    assert "Skipped (manual seed)" in text and "• P103" in text
    assert "Errors (seed left unchanged)" in text and "• P104 (Riot unavailable)" in text
    assert await _seeds(world) == {101: 1500, 102: 2000, 103: 1500, 104: 1500, 105: 1650}
    assert_ratings_consistent(await world.ratings())


@flow
async def test_sync_ranks_output_respects_discords_message_limit(world):
    await world.add_players(60, elo=1500)
    for n in range(101, 161):
        world.riot.ranks[f"puuid{n}"] = ("MASTER", "I", 0)
    ix = await world.run(world.admin_cog.sync_ranks)
    assert len(ix.replies) > 1, "60 changes don't fit in one message"
    assert all(len(m.content) <= 2000 for m in ix.replies)
    assert "P101: 1500 → 2200" in ix.text

    # A huge league is truncated rather than flooding the channel.
    await world.add_players(200, elo=1500, start_id=1000)
    for n in range(1000, 1200):
        world.riot.ranks[f"puuid{n}"] = ("MASTER", "I", 0)
    ix = await world.run(world.admin_cog.sync_ranks)
    assert len(ix.replies) <= 4 and all(len(m.content) <= 2000 for m in ix.replies)
    assert "more line(s)" in ix.replies[-1].content


# ----------------------------------------------------------------- item 5: renamed Riot IDs

@flow
async def test_sync_follows_a_riot_id_rename_before_asking_opgg(world):
    await world.add_players(1, elo=1500)
    world.riot.names["puuid101"] = ("Renamed Guy", "NEW")
    world.opgg.past["renamed guy#new"] = _past("PLATINUM", "I", seasons=0)   # 1750
    # Someone else has since taken the old name: must NOT be used.
    world.opgg.past["p101#na1"] = _past("CHALLENGER", "I", seasons=0)

    ix = await world.run(world.admin_cog.sync_ranks)

    player = await world.player(101)
    assert (player.riot_game_name, player.riot_tag_line) == ("Renamed Guy", "NEW")
    assert world.opgg.lookups == ["Renamed Guy#NEW"]
    assert await _seeds(world) == {101: 1750}
    assert "Renamed Guy: 1500 → 1750" in ix.text and "Riot ID was P101#NA1" in ix.text


# ----------------------------------------------------------------- item 6: becoming unranked clears the stale rank

@flow
async def test_becoming_unranked_clears_solo_rank_fields(world):
    await world.add_players(2, elo=1900)
    for n in (101, 102):
        await _set_player(n, solo_tier="EMERALD", solo_rank="II", solo_lp=64)
    world.opgg.past["p102#na1"] = _past("EMERALD", "II", seasons=1)   # 1900 - 100

    ix = await world.run(world.admin_cog.sync_ranks)

    for n in (101, 102):
        p = await world.player(n)
        assert (p.solo_tier, p.solo_rank, p.solo_lp) == (None, None, None)
    assert await _seeds(world) == {101: 1900, 102: 1800}
    assert "P101: seed kept at 1900 — became unranked; no past-season rank found" in ix.text
    assert "P102: 1900 → 1800 — became unranked; past-season decay" in ix.text


# ----------------------------------------------------------------- item 7: past-season decay

@flow
async def test_past_rank_without_a_date_keeps_the_existing_seed(world):
    await world.add_players(2, elo=1650)
    world.opgg.past["p101#na1"] = _past("EMERALD", "III", seasons=0, date_known=False)
    world.opgg.past["p102#na1"] = _past("EMERALD", "III", seasons=3)          # 1850 - 300

    ix = await world.run(world.admin_cog.sync_ranks)

    # Undated: NOT re-seeded to 1850 as if it were zero seasons old.
    assert await _seeds(world) == {101: 1650, 102: 1550}
    assert "P101: seed kept at 1650" in ix.text and "no placement date" in ix.text
    assert "P102: 1650 → 1550 — past-season decay" in ix.text and "1850 − 300" in ix.text


@flow
async def test_refresh_seed_reports_outage_and_keeps_manual_lock(world):
    (p,) = await world.add_players(1, elo=1500)

    async def as_admin(command, **kwargs):
        # world.run() reserves `user` for "who is invoking"; these commands take a `user` option.
        ix = world.ix()
        await command.callback(command.binding, ix, **kwargs)
        return ix

    await as_admin(world.admin_cog.set_seed, user=p, seed=1777)
    world.riot.fail = RiotUnavailable("x")
    ix = await as_admin(world.admin_cog.refresh_seed, user=p)
    assert "unavailable" in ix.text
    assert (await world.player(p.id)).manual_seed is True
    assert await _seeds(world) == {p.id: 1777}

    world.riot.fail = None
    world.riot.ranks["puuid101"] = ("SILVER", "I", 0)
    ix = await as_admin(world.admin_cog.refresh_seed, user=p)
    assert "1777 → **1350**" in ix.text
    assert (await world.player(p.id)).manual_seed is False


# ----------------------------------------------------------------- item 10: backups before destructive commands

def _backups(world, tag: str) -> list:
    return sorted((database_file(world.config.database_url).parent / "backups").glob(f"*-{tag}.db"))


async def _add_match() -> None:
    async with get_session() as db:
        db.add(Match(team1_json="{}", team2_json="{}", winner=1, team1_wins=2, team2_wins=0))
        await db.commit()


@flow
async def test_clear_matches_backs_up_first(world):
    await world.add_players(1, elo=1500)
    await _add_match()
    ix = await world.run(world.admin_cog.clear_matches, confirm="yes")
    assert "Cleared **1 match(es)**" in ix.text
    (backup,) = _backups(world, "clear-matches")
    assert backup.name in ix.text
    conn = sqlite3.connect(str(backup))
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1, "backup predates the delete"
    conn.close()
    assert await world.matches() == []

    # Asking without confirm:yes neither deletes nor backs up.
    await world.run(world.admin_cog.clear_matches)
    assert len(_backups(world, "clear-matches")) == 1


@flow
async def test_clear_matches_refuses_when_the_backup_fails(world):
    await _add_match()

    async def broken_backup(reason="manual"):
        raise OSError("disk full")

    mp = pytest.MonkeyPatch()
    mp.setattr("bot.db.session.backup_database", broken_backup)
    try:
        ix = await world.run(world.admin_cog.clear_matches, confirm="yes")
    finally:
        mp.undo()
    assert "nothing was deleted" in ix.text
    assert len(await world.matches()) == 1


@flow
async def test_reseed_all_backs_up_first(world):
    await world.add_players(2, elo=1500)
    world.riot.ranks["puuid101"] = ("GOLD", "I", 0)
    ix = await world.run(world.admin_cog.reseed_all, confirm="yes")
    assert "Reseed complete" in ix.text and "P101: 1500 → 1550" in ix.text
    (backup,) = _backups(world, "reseed-all")
    conn = sqlite3.connect(str(backup))
    seeds = {r[0] for r in conn.execute("SELECT base_seed FROM ratings WHERE discord_id = 101")}
    conn.close()
    assert seeds == {1500}, "backup holds the pre-reseed seeds"
    assert await _seeds(world) == {101: 1550, 102: 1500}
