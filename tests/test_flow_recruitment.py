"""Flow regression tests for recruitment, signup close, team options and recovery.

Each test names the failure it guards against.
"""
import json
from datetime import date, datetime, timedelta

from bot.services.scheduler import run_due_closes
from tests.harness import flow

THURSDAY = date(2030, 5, 16)
NEXT_THURSDAY = date(2030, 5, 23)
NOW = datetime(2030, 5, 14, 2, 0)      # naive UTC "now" for scheduler passes
TODAY = date(2030, 5, 13)


async def _filled_session(world, n=10, game_date=THURSDAY, start_id=101):
    players = await world.add_players(n, elo=[1500 + 30 * i for i in range(n)], start_id=start_id)
    session = await world.recruit(game_date)
    for p in players:
        await world.signup(session.id, p)
    return players, session


async def _make_due(world, session_id, now=NOW):
    await world.set_session(session_id, signups_close_at=now - timedelta(minutes=10))


# ------------------------------------------------------------------ under ten players

@flow
async def test_under_ten_stays_open_notifies_once_then_matches(world):
    """Used to cancel the session permanently when fewer than 10 had signed up."""
    players, session = await _filled_session(world, n=9)
    await _make_due(world, session.id)

    assert await run_due_closes(world.recruitment, NOW, TODAY) == [(session.id, "waiting")]
    assert (await world.session(session.id)).status == "recruiting"
    notices = [t for t in world.recruit_channel.texts if "Only **9**" in t]
    assert len(notices) == 1

    # Retries don't repeat the notice.
    await run_due_closes(world.recruitment, NOW + timedelta(minutes=5), TODAY)
    await run_due_closes(world.recruitment, NOW + timedelta(minutes=10), TODAY)
    assert len([t for t in world.recruit_channel.texts if "Only **9**" in t]) == 1

    # The tenth player can still sign up, and the next pass makes teams.
    late = (await world.add_players(1, start_id=201))[0]
    assert "Saved" in (await world.signup(session.id, late)).text
    assert await run_due_closes(world.recruitment, NOW + timedelta(minutes=15), TODAY) == [(session.id, "matched")]
    assert (await world.session(session.id)).status == "matched"
    assert world.owner_options_message() is not None


@flow
async def test_uncoverable_roles_stays_open(world):
    """Used to cancel the session when the picked roles couldn't fill two teams."""
    players = await world.add_players(10)
    session = await world.recruit(THURSDAY)
    for p in players:
        await world.signup(session.id, p, roles=("TOP",))
    assert await world.close(session.id) == "waiting"
    assert (await world.session(session.id)).status == "recruiting"
    assert any("can't fill both teams" in t for t in world.recruit_channel.texts)


# ------------------------------------------------------------------ scheduler

@flow
async def test_close_is_not_due_before_its_time(world):
    players, session = await _filled_session(world)
    await world.set_session(session.id, signups_close_at=NOW + timedelta(hours=24))
    assert await run_due_closes(world.recruitment, NOW, TODAY) == []
    assert (await world.session(session.id)).status == "recruiting"


@flow
async def test_missed_close_is_caught_up_late(world):
    """A restart at the close time used to skip the close entirely."""
    players, session = await _filled_session(world)
    await world.set_session(session.id, signups_close_at=NOW - timedelta(hours=30))
    assert await run_due_closes(world.recruitment, NOW, TODAY) == [(session.id, "matched")]


@flow
async def test_open_ended_session_never_auto_closes(world):
    players = await world.add_players(10)
    session = await world.recruit(THURSDAY, open_ended=True)
    for p in players:
        await world.signup(session.id, p)
    assert await run_due_closes(world.recruitment, NOW + timedelta(days=2), THURSDAY) == []
    assert (await world.session(session.id)).status == "recruiting"


@flow
async def test_stale_open_session_expires_and_is_not_the_default(world):
    """Months-old 'recruiting' sessions used to be picked as the active session."""
    old = await world.recruit(THURSDAY, open_ended=True)
    players, current = await _filled_session(world, game_date=NEXT_THURSDAY)

    # A week later the old session is past its date: it is never the default target...
    world.recruitment._today = lambda: date(2030, 5, 20)
    ix = await world.run(world.admin_cog.make_teams)
    assert f"Session #{current.id} closed" in ix.text
    assert (await world.session(old.id)).status == "recruiting"

    # ...and the scheduler retires it.
    results = await run_due_closes(world.recruitment, datetime(2030, 5, 20, 12), date(2030, 5, 20))
    assert (old.id, "expired") in results
    assert (await world.session(old.id)).status == "cancelled"


# ------------------------------------------------------------------ recruit-now

@flow
async def test_cancelled_thursday_can_be_reposted(world):
    """Used to fail with a raw UNIQUE constraint error."""
    players = await world.add_players(2)
    first = await world.recruit(THURSDAY)
    await world.signup(first.id, players[0])
    await world.run(world.recruitment.cancel_session, session_id=first.id)
    assert (await world.session(first.id)).status == "cancelled"

    ix = await world.run(world.recruitment.recruit_now, game_date=THURSDAY.isoformat())
    assert "Posted recruitment" in ix.text
    revived = await world.session(first.id)
    assert revived.status == "recruiting"
    assert await world.signups(first.id) == []          # stale signups dropped
    assert "Playing — **0**" in (await world.recruit_message(first.id)).text


@flow
async def test_recruit_now_refuses_duplicate_past_and_already_closed(world):
    await world.recruit(THURSDAY)
    dup = await world.run(world.recruitment.recruit_now, game_date=THURSDAY.isoformat())
    assert "already a recruitment" in dup.text

    past = await world.run(world.recruitment.recruit_now, game_date="2020-01-02")
    assert "in the past" in past.text

    # Posting on Tuesday with the default Monday close would close immediately.
    world.recruitment._today = lambda: date(2030, 5, 21)
    import bot.cogs.recruitment as rec

    class _Tuesday(datetime):
        @classmethod
        def now(cls, tz=None):
            return tz.localize(datetime(2030, 5, 21, 12, 0)) if tz else datetime(2030, 5, 21, 12, 0)

    real = rec.datetime
    rec.datetime = _Tuesday
    try:
        late = await world.run(world.recruitment.recruit_now, game_date=NEXT_THURSDAY.isoformat())
        ok = await world.run(world.recruitment.recruit_now, game_date=NEXT_THURSDAY.isoformat(),
                             close_day="Wednesday")
    finally:
        rec.datetime = real
    assert "already passed" in late.text
    assert "Posted recruitment" in ok.text and "Wednesday" in ok.text


@flow
async def test_recruit_now_without_configured_channel_uses_current_channel(world):
    """Used to post in the first channel the bot could write to."""
    from bot.db.models import GuildConfig
    from bot.db.session import get_session
    async with get_session() as db:
        (await db.get(GuildConfig, world.config.discord_guild_id)).recruit_channel_id = None
        await db.commit()
    ix =await world.run(world.recruitment.recruit_now, channel=None, game_date=THURSDAY.isoformat())
    assert "Posted recruitment" in ix.text
    assert len(world.recruit_channel.sent) == 1 and world.other_channel.sent == []


# ------------------------------------------------------------------ signup edge cases

@flow
async def test_editing_roles_keeps_place_in_line(world):
    """Re-saving roles used to move the player to the back of the queue."""
    players = await world.add_players(3)
    session = await world.recruit(THURSDAY)
    for p in players:
        await world.signup(session.id, p, roles=("TOP",))
    order_before = [s.discord_id for s in await world.signups(session.id)]
    await world.signup(session.id, players[0], roles=("MID",))
    su = await world.signups(session.id)
    assert [s.discord_id for s in su] == order_before
    assert su[0].roles == "MID,TOP"


@flow
async def test_done_after_close_is_not_saved(world):
    """The role picker stays open 5 minutes; Done after the close used to write a signup."""
    from bot.cogs.recruitment import DoneButton, RsvpView

    players, session = await _filled_session(world)
    late = (await world.add_players(1, start_id=201))[0]
    open_ix = world.ix(late, message=await world.recruit_message(session.id))
    await RsvpView().playing.callback(open_ix)            # picker opened while recruiting
    picker = open_ix.replies[-1].view
    picker.toggle("FILL")
    await world.close(session.id)                          # ...then signups close

    done_ix = world.ix(late, message=open_ix.replies[-1])
    await next(c for c in picker.children if isinstance(c, DoneButton)).callback(done_ix)
    assert "wasn't saved" in done_ix.text
    assert late.id not in [s.discord_id for s in await world.signups(session.id)]


@flow
async def test_eleven_signups_benches_the_last(world):
    players, session = await _filled_session(world, n=11)
    assert await world.close(session.id) == "matched"
    options = world.owner_options_message().text
    assert players[-1].mention not in options and players[0].mention in options


@flow
async def test_making_teams_posts_nothing_in_the_recruit_channel(world):
    """Used to post 'Signups closed ... Teams being finalized' and the bench list."""
    players, session = await _filled_session(world, n=11)
    before = list(world.recruit_channel.texts)
    assert await world.close(session.id) == "matched"
    assert world.recruit_channel.texts == before
    assert world.owner_options_message() is not None


# ------------------------------------------------------------------ owner DM + recovery

@flow
async def test_owner_dm_failure_is_recoverable_with_resend(world):
    """A failed owner DM used to leave the session wedged with wrong recovery advice."""
    players, session = await _filled_session(world)
    world.owner.dms_closed = True
    assert await world.close(session.id) == "dm_failed"
    assert (await world.session(session.id)).status == "matched"
    assert any("/resend-options" in t for t in world.recruit_channel.texts)

    still = await world.run(world.recruitment.resend_options)
    assert "Still couldn't DM" in still.text

    world.owner.dms_closed = False
    ok = await world.run(world.recruitment.resend_options, session_id=session.id)
    assert "Re-sent" in ok.text
    await world.pick(0)
    assert len(await world.matches(session.id)) == 1
    assert any("Teams finalized" in t for t in world.recruit_channel.texts)


@flow
async def test_resent_options_supersede_nothing_and_old_dm_still_resolves_once(world):
    players, session = await _filled_session(world)
    await world.close(session.id)
    first_dm = world.owner_options_message()
    await world.run(world.recruitment.resend_options, session_id=session.id)
    second_dm = world.owner_options_message()
    assert second_dm is not first_dm
    await world.pick(1, message=second_dm)
    # The earlier copy of the DM no longer maps to a pending set.
    stale = await world.pick(0, message=first_dm)
    assert len(await world.matches(session.id)) == 1
    assert "Couldn't find" in stale.text or "already resolved" in stale.text


@flow
async def test_pick_is_refused_after_manual_teams_or_cancel(world):
    """A late DM click used to create a second match, or a match on a cancelled session."""
    import json
    from bot.db.models import Match
    from bot.db.session import get_session

    players, session = await _filled_session(world)
    await world.close(session.id)
    roles = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]
    async with get_session() as db:                        # an admin set teams by hand meanwhile
        db.add(Match(session_id=session.id,
                     team1_json=json.dumps(dict(zip(roles, [p.id for p in players[:5]]))),
                     team2_json=json.dumps(dict(zip(roles, [p.id for p in players[5:]])))))
        await db.commit()
    late = await world.pick(0)
    assert "no longer valid" in late.text
    assert len(await world.matches(session.id)) == 1

    others, session2 = await _filled_session(world, game_date=NEXT_THURSDAY, start_id=301)
    await world.close(session2.id)
    await world.run(world.recruitment.cancel_session, session_id=session2.id)
    cancelled = await world.pick(0)
    assert "no longer valid" in cancelled.text
    assert await world.matches(session2.id) == []


@flow
async def test_reopen_after_dropout_and_rematch(world):
    """There was no way to change the player list once signups had closed."""
    players, session = await _filled_session(world, n=11)
    await world.close(session.id)
    first_dm = world.owner_options_message()

    ix = await world.run(world.recruitment.reopen_signups, session_id=session.id)
    assert "Re-opened" in ix.text
    assert (await world.session(session.id)).status == "recruiting"

    # The old options are void once signups re-open.
    assert "replaced by a newer set" in (await world.pick(0, message=first_dm)).text

    drop =await world.rsvp(session.id, players[0], "not_playing")
    assert "Recorded" in drop.text
    done = await world.run(world.admin_cog.make_teams, session_id=session.id)
    assert "closed" in done.text
    await world.pick(0)
    m = (await world.matches(session.id))[0]
    assert str(players[0].id) not in m.team1_json + m.team2_json


@flow
async def test_reopen_refused_once_teams_exist(world):
    players, session = await _filled_session(world)
    await world.close(session.id)
    await world.pick(0)
    ix = await world.run(world.recruitment.reopen_signups, session_id=session.id)
    assert "already has teams" in ix.text
    assert (await world.session(session.id)).status == "matched"


@flow
async def test_close_signups_then_make_teams_matches(world):
    """/close-signups led to a 'closed' state that the matchmaker couldn't see."""
    players, session = await _filled_session(world)
    await world.run(world.recruitment.close_signups, session_id=session.id)
    assert (await world.session(session.id)).status == "closed"
    refused = await world.signup(session.id, (await world.add_players(1, start_id=201))[0])
    assert "closed" in refused.text
    ix = await world.run(world.admin_cog.make_teams)
    assert "DM'd the team options" in ix.text
    assert (await world.session(session.id)).status == "matched"


@flow
async def test_owner_gets_ten_options_and_can_pick_a_late_one(world):
    """The owner DM used to stop at three options (A/B/C)."""
    players, session = await _filled_session(world)
    assert await world.close(session.id) == "matched"
    dm = world.owner_options_message()
    assert len(dm.embeds) == 10                     # Discord's per-message embed limit
    assert [e.title for e in dm.embeds] == [f"Option {c}" for c in "ABCDEFGHIJ"]
    assert [b.label for b in dm.view.children] == [f"Pick {c}" for c in "ABCDEFGHIJ"]
    assert len({b.custom_id for b in dm.view.children}) == 10
    assert all(b.row == i // 5 for i, b in enumerate(dm.view.children))   # 5 buttons per row max
    assert sum(len(e) for e in dm.embeds) <= 6000   # Discord's per-message embed text limit

    wanted = dm.embeds[9]
    done = await world.pick(9)
    assert "Committed Option J" in done.text
    match = (await world.matches(session.id))[0]
    for field, saved in zip(wanted.fields, (match.team1_json, match.team2_json)):
        for uid in json.loads(saved).values():
            assert f"<@{uid}>" in field.value


@flow
async def test_options_dm_shows_each_players_inhouse_elo(world):
    """The options DM listed names only, so the owner picked without seeing anyone's elo."""
    from bot.db.models import Rating
    from bot.db.session import get_session

    players, session = await _filled_session(world)
    async with get_session() as db:                         # INHOUSE differs from the role elos
        for p in players:
            (await db.get(Rating, (p.id, "INHOUSE"))).elo += 7
        await db.commit()
    await world.close(session.id)
    dm = world.owner_options_message()
    assert "INHOUSE elo" in dm.content
    for embed in dm.embeds:
        lines = "\n".join(f.value for f in embed.fields)
        for i, p in enumerate(players):
            assert f"{p.mention} ({1500 + 30 * i + 7})" in lines
    assert sum(len(e) for e in dm.embeds) <= 6000   # Discord's per-message embed text limit


@flow
async def test_final_teams_post_shows_each_players_elo_in_their_role(world):
    """The final teams post listed names only."""
    from bot.db.models import Rating
    from bot.db.session import get_session

    players, session = await _filled_session(world)
    async with get_session() as db:                         # make role elos differ per role
        for p in players:
            for bump, role in enumerate(["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]):
                (await db.get(Rating, (p.id, role))).elo += bump
        await db.commit()
    await world.close(session.id)
    await world.pick(0)
    final = next(t for t in world.recruit_channel.texts if "Teams finalized" in t)
    match = (await world.matches(session.id))[0]
    ratings = await world.ratings()
    seated = {**json.loads(match.team1_json), **{f"2{r}": u for r, u in json.loads(match.team2_json).items()}}
    assert len(seated) == 10
    for role, uid in seated.items():
        role = role.lstrip("2")
        assert f"P{uid} ({ratings[(uid, role)][0]})" in final


@flow
async def test_picking_an_option_builds_the_team_channels(world):
    """Picking an option used to leave the team channels to a separate /match-channels run."""
    players, session = await _filled_session(world)
    category = await world.add_match_category()
    await world.close(session.id)
    done = await world.pick(0)
    match = (await world.matches(session.id))[0]

    assert "team channels ready" in done.text
    assert sorted(c.name for c in category.text_channels) == ["lo-gang", "team-10"]
    for chan_name, roster in (("lo-gang", match.team1_json), ("team-10", match.team2_json)):
        intro = next(c for c in category.text_channels if c.name == chan_name).texts[0]
        assert all(f"<@{uid}>" in intro.split("Vs.")[0] for uid in json.loads(roster).values())


@flow
async def test_pick_still_commits_when_no_match_category_is_set(world):
    players, session = await _filled_session(world)
    await world.close(session.id)
    done = await world.pick(0)
    assert "No match category configured" in done.text
    assert len(await world.matches(session.id)) == 1
    assert any("Teams finalized" in t for t in world.recruit_channel.texts)


@flow
async def test_make_teams_reruns_a_session_awaiting_a_pick(world):
    """Re-running used to need /reopen-signups first: /make-teams said the session wasn't open."""
    players, session = await _filled_session(world)
    await world.run(world.admin_cog.make_teams)
    first_dm = world.owner_options_message()
    channel_before = list(world.recruit_channel.texts)

    # The scheduler's path still leaves a session that is awaiting a pick alone.
    assert await world.close(session.id) == "not_open"
    assert world.owner_options_message() is first_dm

    ix = await world.run(world.admin_cog.make_teams)          # no id: finds the matched session
    assert "Re-ran the matchmaker" in ix.text
    second_dm = world.owner_options_message()
    assert second_dm is not first_dm
    assert (await world.session(session.id)).status == "matched"
    assert world.recruit_channel.texts == channel_before       # signups never re-opened publicly

    assert "replaced by a newer set" in (await world.pick(0, message=first_dm)).text
    await world.pick(0, message=second_dm)
    assert len(await world.matches(session.id)) == 1

    # Once an option is picked there is nothing to re-run.
    refused = await world.run(world.admin_cog.make_teams, session_id=session.id)
    assert "already has teams" in refused.text
    assert world.owner_options_message() is second_dm
    assert len(await world.matches(session.id)) == 1


@flow
async def test_make_teams_can_discard_picked_teams_and_start_over(world):
    """Once an option was picked there was no way to pick again short of editing the database."""
    players, session = await _filled_session(world)
    await world.close(session.id)
    picked_dm = world.owner_options_message()
    await world.pick(0)
    old = (await world.matches(session.id))[0]

    ix = await world.run(world.admin_cog.make_teams, session_id=session.id, discard_teams=True)
    assert "Re-ran the matchmaker" in ix.text and f"Discarded Match {old.id}" in ix.text
    assert await world.matches(session.id) == []
    assert (await world.session(session.id)).status == "matched"
    new_dm = world.owner_options_message()
    assert new_dm is not picked_dm

    await world.pick(3, message=new_dm)
    matches = await world.matches(session.id)
    assert len(matches) == 1 and matches[0].id != old.id
    assert len([t for t in world.recruit_channel.texts if "Teams finalized" in t]) == 2

    # A reported result is never discarded.
    await world.run(world.admin_cog.report_manual, series_score="2-0", match_id=matches[0].id)
    refused = await world.run(world.admin_cog.make_teams, session_id=session.id, discard_teams=True)
    assert "isn't open" in refused.text
    assert len(await world.matches(session.id)) == 1


@flow
async def test_make_teams_prefers_an_open_session_over_one_awaiting_a_pick(world):
    players, session = await _filled_session(world)
    await world.close(session.id)                               # this week: awaiting a pick
    first_dm = world.owner_options_message()
    others, next_session = await _filled_session(world, game_date=NEXT_THURSDAY, start_id=301)

    ix = await world.run(world.admin_cog.make_teams)
    assert f"Session #{next_session.id} closed" in ix.text
    assert (await world.session(next_session.id)).status == "matched"
    assert "replaced" not in (await world.pick(0, message=first_dm)).text   # this week's untouched
    assert len(await world.matches(session.id)) == 1


@flow
async def test_make_teams_reports_what_actually_happened(world):
    """Used to say 'Check your DMs' even when nothing was sent."""
    players, session = await _filled_session(world, n=4)
    ix = await world.run(world.admin_cog.make_teams)
    assert "can't be matched yet" in ix.text
    assert world.owner_options_message() is None


@flow
async def test_manual_match_default_ignores_stale_sessions(world):
    """A bare /manual-match used to attach to the oldest open session, however old."""
    await world.add_players(10)
    await world.recruit(THURSDAY, open_ended=True)
    # Far in the future, that session is stale: no default target.
    import bot.cogs.admin as admin_mod

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 8, 1, 12, 0, tzinfo=tz)

    real = admin_mod.datetime
    admin_mod.datetime = _Later
    try:
        ix = await world.run(world.admin_cog.manual_match)
    finally:
        admin_mod.datetime = real
    assert "No current session" in ix.text and ix.modal is None

    # With the session current, the modal opens for it.
    ok = await world.run(world.admin_cog.manual_match)
    assert ok.modal is not None


# ------------------------------------------------------------------ default roles

@flow
async def test_playing_defaults_to_last_weeks_roles_without_answering_the_picker(world):
    """Clicking Playing and ignoring the role picker used to leave you not signed up."""
    from bot.cogs.recruitment import RsvpView

    player = (await world.add_players(1))[0]
    last_week = await world.recruit(THURSDAY)
    await world.signup(last_week.id, player, roles=("MID", "SUPPORT"))

    this_week = await world.recruit(NEXT_THURSDAY)
    ix = world.ix(player, message=await world.recruit_message(this_week.id))
    await RsvpView().playing.callback(ix)          # ...and never touches the picker

    su = {s.discord_id: s for s in await world.signups(this_week.id)}
    assert su[player.id].status == "playing" and su[player.id].roles == "MID,SUPPORT"
    assert "Mid, Support" in ix.text and "from last time" in ix.text
    assert ix.replies[-1].view.selected == {"MID", "SUPPORT"}     # picker is pre-filled
    assert "Playing — **1**" in (await world.recruit_message(this_week.id)).text

    # They can still change the defaults and hit Done.
    await world.signup(this_week.id, player, roles=("TOP",))
    su = {s.discord_id: s for s in await world.signups(this_week.id)}
    assert su[player.id].roles == "MID,SUPPORT,TOP"


@flow
async def test_playing_with_no_history_still_requires_a_role_choice(world):
    from bot.cogs.recruitment import RsvpView

    player = (await world.add_players(1))[0]
    session = await world.recruit(THURSDAY)
    ix = world.ix(player, message=await world.recruit_message(session.id))
    await RsvpView().playing.callback(ix)
    assert "Pick your role(s)" in ix.text
    assert await world.signups(session.id) == []


@flow
async def test_defaults_use_the_most_recent_playing_signup_and_survive_not_playing(world):
    from bot.cogs.recruitment import RsvpView

    player = (await world.add_players(1))[0]
    older = await world.recruit(THURSDAY)
    await world.signup(older.id, player, roles=("BOT",))
    newer = await world.recruit(NEXT_THURSDAY)
    await world.signup(newer.id, player, roles=("JUNGLE",))   # BOT carried over, JUNGLE added
    third = await world.recruit(date(2030, 5, 30))

    # Said "not playing" first this week, then changed their mind: still gets defaults.
    await world.rsvp(third.id, player, "not_playing")
    ix = world.ix(player, message=await world.recruit_message(third.id))
    await RsvpView().playing.callback(ix)
    su = {s.discord_id: s for s in await world.signups(third.id)}
    assert su[player.id].status == "playing" and su[player.id].roles == "BOT,JUNGLE"
