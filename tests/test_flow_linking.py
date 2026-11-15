"""End-to-end tests for account linking: /link, the owner's Approve/Reject DM,
/link-user, /unlink and /profile — through the real LinkingCog on the fake Discord.
"""
import sqlite3
from datetime import date, datetime

from sqlalchemy import event, select
from sqlalchemy.orm import Session as OrmSession

from bot.db.models import GameStat, Match, MatchPerformance, Player, Rating
from bot.db.session import database_file, get_session
from bot.services.riot_client import RiotAuthError, RiotUnavailable
from tests.harness import flow

ACCOUNT_A = "AAAAAAAA-account-one-0000000000"
ACCOUNT_B = "BBBBBBBB-account-two-0000000000"


async def _rating_rows(discord_id: int) -> dict:
    async with get_session() as db:
        rows = (await db.execute(select(Rating).where(Rating.discord_id == discord_id))).scalars().all()
        return {r.role: (r.elo, r.base_seed, r.inhouse_modifier, r.games_played) for r in rows}


async def _give_history(discord_id: int, modifier: int = 45, games: int = 7) -> None:
    """Make a player 'established': inhouse results on their ratings."""
    async with get_session() as db:
        for r in (await db.execute(select(Rating).where(Rating.discord_id == discord_id))).scalars():
            r.inhouse_modifier = modifier
            r.elo = r.base_seed + modifier
            r.games_played = games
        await db.commit()


async def _unlink_and_request(world, member, riot_id: str, puuid: str):
    """An established player unlinks, then asks to link another account (pending)."""
    world.riot.accounts[riot_id.lower()] = puuid
    await world.run(world.linking.unlink, user=member)
    return await world.run(world.linking.link, user=member, riot_id=riot_id)


# ----------------------------------------------------------------- item 1: reject must not wipe history

@flow
async def test_rejecting_a_relink_keeps_an_established_players_history(world):
    (veteran,) = await world.add_players(1, elo=1600)
    await _give_history(veteran.id)
    before = await _rating_rows(veteran.id)

    ix = await _unlink_and_request(world, veteran, "Smurf#NA1", ACCOUNT_A)
    assert "submitted" in ix.text
    rejected = await world.reject_link(reason="not your account")
    assert "Rejected" in world.link_request_message().text

    player = await world.player(veteran.id)
    assert player is not None, "an established player must never be deleted"
    assert await _rating_rows(veteran.id) == before, "ratings and inhouse history must survive"
    # Back in the unlinked state, free to /link again.
    assert player.riot_puuid is None and player.riot_game_name is None
    assert player.link_status == "approved"
    assert player.previous_riot_puuid == "puuid101"
    assert "rejected" in veteran.dm.texts[-1]
    again = await world.run(world.linking.link, user=veteran, riot_id="Smurf#NA1")
    assert "submitted" in again.text, rejected.text


@flow
async def test_rejecting_a_brand_new_request_deletes_the_placeholder(world):
    world.riot.accounts["newbie#na1"] = ACCOUNT_A
    await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")
    assert (await world.player(world.rando.id)).link_status == "pending"
    await world.reject_link()
    assert await world.player(world.rando.id) is None


@flow
async def test_owner_dm_failure_keeps_an_established_players_history(world):
    (veteran,) = await world.add_players(1, elo=1600)
    await _give_history(veteran.id)
    before = await _rating_rows(veteran.id)

    world.owner.dms_closed = True
    ix = await _unlink_and_request(world, veteran, "Smurf#NA1", ACCOUNT_A)
    assert "Couldn't reach the bot owner" in ix.text

    player = await world.player(veteran.id)
    assert player is not None and player.riot_puuid is None and player.link_status == "approved"
    assert await _rating_rows(veteran.id) == before

    # A brand-new user in the same situation leaves nothing behind.
    world.riot.accounts["newbie#na1"] = ACCOUNT_B
    await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")
    assert await world.player(world.rando.id) is None


# ----------------------------------------------------------------- item 2: Riot outages in the link flows

@flow
async def test_link_reports_riot_outage(world):
    world.riot.accounts["newbie#na1"] = ACCOUNT_A
    world.riot.fail = RiotUnavailable("retries exhausted (HTTP 503)")
    ix = await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")
    assert "unavailable" in ix.text and "Try again" in ix.text
    assert await world.player(world.rando.id) is None

    world.riot.fail = RiotUnavailable("unexpected HTTP 400", status=400)
    ix = await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")
    assert "HTTP 400" in ix.text and "spelling" in ix.text


@flow
async def test_link_user_reports_riot_outage_and_writes_nothing(world):
    world.riot.accounts["newbie#na1"] = ACCOUNT_A
    world.riot.fail_puuids[ACCOUNT_A] = RiotUnavailable("timeout")
    ix = await world.run(world.linking.link_user, member=world.rando, riot_id="Newbie#NA1")
    assert "unavailable" in ix.text
    assert await world.player(world.rando.id) is None


@flow
async def test_approve_during_riot_outage_leaves_request_pending(world):
    world.riot.accounts["newbie#na1"] = ACCOUNT_A
    await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")

    world.riot.fail = RiotUnavailable("retries exhausted")
    ix = await world.approve_link()
    assert "unavailable" in ix.text and "still pending" in ix.text
    assert (await world.player(world.rando.id)).link_status == "pending"
    assert await _rating_rows(world.rando.id) == {}, "must not seed 1200 as if unranked"

    world.riot.fail = RiotAuthError("403")
    assert "key rejected" in (await world.approve_link()).text

    # Riot comes back: the same DM still works.
    world.riot.fail = None
    world.riot.ranks[ACCOUNT_A] = ("GOLD", "II", 30)
    await world.approve_link()
    assert (await world.player(world.rando.id)).link_status == "approved"
    assert (await _rating_rows(world.rando.id))["MID"][1] == 1500


# ----------------------------------------------------------------- item 11: stale DMs, double clicks, races

@flow
async def test_stale_approval_dm_is_refused_as_superseded(world):
    world.riot.accounts["first#na1"] = ACCOUNT_A
    world.riot.accounts["second#na1"] = ACCOUNT_B
    world.riot.ranks[ACCOUNT_A] = ("CHALLENGER", "I", 900)
    world.riot.ranks[ACCOUNT_B] = ("SILVER", "IV", 0)

    await world.run(world.linking.link, user=world.rando, riot_id="First#NA1")
    stale = world.link_request_message()
    await world.run(world.linking.unlink, user=world.rando)
    await world.run(world.linking.link, user=world.rando, riot_id="Second#NA1")
    current = world.link_request_message()
    assert stale is not current
    stale_view = stale.view

    # Approving the OLD DM must not approve the new, different account.
    ix = await world.approve_link(stale)
    assert "superseded" in ix.text
    player = await world.player(world.rando.id)
    assert player.link_status == "pending" and player.riot_puuid == ACCOUNT_B
    assert await _rating_rows(world.rando.id) == {}
    assert "Superseded" in stale.text and stale.view is None

    # Rejecting from the old DM must not cancel the new request either
    # (put its buttons back, as if this click had raced the edit above).
    stale.view = stale_view
    ix = await world.reject_link(stale)
    assert "superseded" in ix.text
    assert (await world.player(world.rando.id)).link_status == "pending"

    # The current DM works, and seeds from the account it shows.
    await world.approve_link(current)
    player = await world.player(world.rando.id)
    assert player.link_status == "approved" and player.riot_game_name == "Second"
    assert (await _rating_rows(world.rando.id))["TOP"][1] == 1200


@flow
async def test_unlink_while_approval_is_in_flight_writes_nothing(world):
    world.riot.accounts["newbie#na1"] = ACCOUNT_A
    world.riot.ranks[ACCOUNT_A] = ("GOLD", "IV", 0)
    await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")

    async def unlink_meanwhile():
        await world.run(world.linking.unlink, user=world.rando)

    world.riot.before_call = unlink_meanwhile   # lands while Approve is waiting on Riot
    ix = await world.approve_link()
    assert "no longer pending" in ix.text
    player = await world.player(world.rando.id)
    assert player.riot_puuid is None
    assert await _rating_rows(world.rando.id) == {}, "must not seed ratings for a cancelled request"
    assert not any("approved" in t for t in world.rando.dm.texts)


@flow
async def test_double_click_on_approve_applies_once(world):
    world.riot.accounts["newbie#na1"] = ACCOUNT_A
    world.riot.ranks[ACCOUNT_A] = ("GOLD", "IV", 0)
    await world.run(world.linking.link, user=world.rando, riot_id="Newbie#NA1")
    inner = []

    async def second_click():
        inner.append(await world.approve_link())

    world.riot.before_call = second_click
    outer = await world.approve_link()
    assert "no longer pending" in outer.text
    assert (await world.player(world.rando.id)).link_status == "approved"
    assert len(await _rating_rows(world.rando.id)) == 6
    assert sum("approved" in t for t in world.rando.dm.texts) == 1, "the user is told once"


@flow
async def test_two_users_linking_the_same_account_concurrently(world):
    """The UNIQUE(riot_puuid) violation is reported, not raised."""
    world.riot.accounts["shared#na1"] = ACCOUNT_A
    db_file = str(database_file(world.config.database_url))

    fired = []

    def someone_else_claims_it(*_):
        # Fires between /link's "is it taken?" check and its commit.
        if fired:
            return
        fired.append(True)
        conn = sqlite3.connect(db_file)
        conn.execute(
            "INSERT INTO players (discord_id, riot_puuid, region, manual_seed, link_status, created_at) "
            "VALUES (777, ?, 'na1', 0, 'pending', '2026-01-01 00:00:00')", (ACCOUNT_A,))
        conn.commit()
        conn.close()

    event.listen(OrmSession, "before_flush", someone_else_claims_it)
    try:
        ix = await world.run(world.linking.link, user=world.rando, riot_id="Shared#NA1")
    finally:
        event.remove(OrmSession, "before_flush", someone_else_claims_it)
    assert fired and "just claimed" in ix.text
    assert await world.player(world.rando.id) is None
    assert world.link_request_message() is None, "no approval DM for a request that didn't stick"


@flow
async def test_link_user_racing_another_link_for_the_same_account(world):
    world.riot.accounts["shared#na1"] = ACCOUNT_A
    real_get_solo_rank = world.riot.get_solo_rank

    async def claimed_while_waiting_on_riot(puuid):
        async with get_session() as db:
            db.add(Player(discord_id=777, riot_puuid=ACCOUNT_A, link_status="pending"))
            await db.commit()
        return await real_get_solo_rank(puuid)

    world.riot.get_solo_rank = claimed_while_waiting_on_riot
    ix = await world.run(world.linking.link_user, member=world.rando, riot_id="Shared#NA1")
    assert "just linked by someone else" in ix.text
    assert await world.player(world.rando.id) is None
    assert await _rating_rows(world.rando.id) == {}


# ----------------------------------------------------------------- item 12: /unlink on a pending request

@flow
async def test_unlinking_a_pending_request_keeps_the_previous_account(world):
    (veteran,) = await world.add_players(1)
    await _unlink_and_request(world, veteran, "Other#NA1", ACCOUNT_A)
    assert (await world.player(veteran.id)).previous_riot_puuid == "puuid101"

    ix = await world.run(world.linking.unlink, user=veteran)   # cancel the pending request
    assert "Pending request cancelled" in ix.text
    player = await world.player(veteran.id)
    assert player.riot_puuid is None
    assert player.previous_riot_puuid == "puuid101", "an unapproved account must not become 'previous'"


# ----------------------------------------------------------------- item 13: /profile KDA

@flow
async def test_profile_kda_uses_per_game_rows(world):
    (p,) = await world.add_players(1)
    async with get_session() as db:
        # Series 1: reported game by game -> KDA lives on GameStat, series row has NULLs.
        db.add(Match(id=1, team1_json="{}", team2_json="{}", winner=1, team1_wins=2, team2_wins=0,
                     game_date=date(2026, 6, 4), reported_at=datetime(2026, 6, 4, 23)))
        db.add(MatchPerformance(match_id=1, discord_id=p.id, role="MID", won=True))
        db.add(GameStat(match_id=1, game_no=1, discord_id=p.id, role="MID", kills=4, deaths=2, assists=6, won=True))
        db.add(GameStat(match_id=1, game_no=2, discord_id=p.id, role="MID", kills=2, deaths=2, assists=2, won=True))
        # Series 2: old-style report -> KDA on the series row, no GameStat.
        db.add(Match(id=2, team1_json="{}", team2_json="{}", winner=2, team1_wins=0, team2_wins=2,
                     game_date=date(2026, 6, 11), reported_at=datetime(2026, 6, 11, 23)))
        db.add(MatchPerformance(match_id=2, discord_id=p.id, role="MID", won=False,
                                kills=10, deaths=5, assists=5))
        await db.commit()

    ix = await world.run(world.linking.profile, user=p)
    # 3 KDA lines in total: (4+2+10)/3, (2+2+5)/3, (6+2+5)/3 — not divided by 2 series,
    # and not just the one series row that happens to carry a KDA.
    assert "2 series · 1W-1L (50%) · 5.3/3.0/4.3" in ix.text, ix.text
