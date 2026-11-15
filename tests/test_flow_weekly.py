"""End-to-end regression tests for the weekly flow, through the real cogs:

    /recruit-now -> RSVP buttons -> close + matchmake -> owner picks from DM
    -> teams posted -> /report-manual -> /unreport
"""
from datetime import date

from tests.harness import assert_ratings_consistent, flow

THURSDAY = date(2030, 5, 16)  # far-future Thursday so "is it in the past" checks never bite


async def _recruit_and_fill(world, n=10, elo=None):
    elos = elo or [1500 + 40 * i for i in range(n)]
    players = await world.add_players(n, elo=elos)
    session = await world.recruit(THURSDAY)
    for p in players:
        done = await world.signup(session.id, p, roles=("FILL",))
        assert "Saved" in done.text
    return players, session


@flow
async def test_full_week_happy_path(world):
    players, session = await _recruit_and_fill(world)

    # The recruitment post is public and shows all ten signups.
    post = await world.recruit_message(session.id)
    assert "Playing — **10**" in post.text

    await world.close(session.id)
    assert (await world.session(session.id)).status == "matched"

    # The owner is DM'd the options with pick buttons.
    options = world.owner_options_message()
    assert options is not None and "Option A" in options.text

    # Picking from the DM (interaction.guild is None there) commits a Match AND
    # posts the final teams publicly. Regression: this used to crash before posting.
    await world.pick(0)
    matches = await world.matches(session.id)
    assert len(matches) == 1
    assert any("Teams finalized" in t for t in world.recruit_channel.texts)
    final = next(t for t in world.recruit_channel.texts if "Teams finalized" in t)
    assert all(p.display_name in final for p in players)

    # Picking again is refused and creates nothing new.
    again = await world.pick(1, message=options)
    assert "already resolved" in again.text
    assert len(await world.matches(session.id)) == 1

    # Report the series, then undo it: ratings must return exactly.
    before = await world.ratings()
    rep = await world.run(world.admin_cog.report_manual, series_score="2-1", match_id=matches[0].id)
    assert "recorded" in rep.text
    after = await world.ratings()
    assert after != before
    assert_ratings_consistent(after)
    assert (await world.session(session.id)).status == "completed"
    m = (await world.matches(session.id))[0]
    assert (m.team1_wins, m.team2_wins, m.winner) == (2, 1, 1)

    # Reporting twice is refused and changes nothing.
    dup = await world.run(world.admin_cog.report_manual, series_score="0-2", match_id=m.id)
    assert "already reported" in dup.text
    assert await world.ratings() == after

    undo = await world.run(world.admin_cog.unreport, match_id=m.id)
    assert "Un-reported" in undo.text
    assert await world.ratings() == before


@flow
async def test_non_admin_cannot_run_admin_commands(world):
    await world.add_players(10)
    ix = await world.run(world.recruitment.recruit_now, user=world.rando, game_date=THURSDAY.isoformat())
    assert "League Admin only" in ix.text
    assert world.recruit_channel.sent == []


@flow
async def test_rsvp_buttons_update_public_counts(world):
    players = await world.add_players(3)
    session = await world.recruit(THURSDAY)
    await world.signup(session.id, players[0], roles=("TOP", "MID"))
    await world.rsvp(session.id, players[1], "not_playing")
    await world.rsvp(session.id, players[2], "commentator")
    post = await world.recruit_message(session.id)
    assert "Playing — **1**" in post.text
    assert "Not Playing — **1**" in post.text
    assert "Commentators — **1**" in post.text
    su = {s.discord_id: s for s in await world.signups(session.id)}
    assert su[players[0].id].roles == "MID,TOP"

    # Changing your mind replaces the earlier answer rather than adding a second row.
    await world.rsvp(session.id, players[0], "not_playing")
    post = await world.recruit_message(session.id)
    assert "Playing — **0**" in post.text and "Not Playing — **2**" in post.text
