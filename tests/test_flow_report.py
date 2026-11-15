"""End-to-end tests for reporting a result and committing Elo: /report and its
confirm view, /report-manual, /unreport, /rescore-match and /pickup-series, on
the fake Discord in tests/harness.py. No vision call is ever made — each
"screenshot" reads as a ParsedScoreboard the test supplies.
"""
import asyncio
from datetime import date, datetime

import pytest
import pytz
from sqlalchemy import delete, select, update

from bot.cogs.admin import (
    AdminCog, GameResult, PickupSeriesModal, _GameSelect, _SlotPlayerSelect, _SlotSelect,
    format_roster_block,
)
from bot.config import ROLES
from bot.db.models import Alias, GameStat, Match, MatchPerformance, Player, ProposalSet, Rating
from bot.db.session import get_session
from bot.services.elo import INHOUSE_ROLE
from tests.harness import (
    FakeAttachment, assert_ratings_consistent, failed_board, find_item, flow, make_board,
)

T1 = [101, 102, 103, 104, 105]
T2 = [106, 107, 108, 109, 110]
SEEDS = [1700, 1650, 1600, 1550, 1500, 1480, 1520, 1610, 1590, 1660, 1400, 1450]
ADMIN = 2  # world.admin's id


def names(ids):
    return [f"P{i}" for i in ids]


def clean_board(winner=1):
    """Both teams in role order, every name resolvable, screenshot team `winner` won."""
    return make_board(names(T1), names(T2), winner=winner)


def roster(ids):
    return dict(zip(ROLES, ids))


async def setup(world, n=12):
    await world.add_players(n, elo=SEEDS[:n])
    return await world.add_match(T1, T2)


async def open_slot(world, view, gi, team, role):
    """Fix mode -> pick the slot -> return the player picker."""
    await world.click(find_item(view, "Subs / fix"))
    if len(view.state.games) > 1:
        await world.click(find_item(view, cls=_GameSelect), values=[str(gi)])
    await world.click(find_item(view, cls=_SlotSelect), values=[f"{gi}:{team}:{role}"])
    return find_item(view, cls=_SlotPlayerSelect)


# ----------------------------------------------------------------- 1. exact unreport

@flow
async def test_unreport_is_exact_when_a_player_changes_role_mid_series(world):
    """101 is TOP in games 1 and 3 but MID in game 2 (trading lanes with 103).
    Un-reporting must take each game's Elo off the role it was earned on."""
    match_id = await setup(world)
    before = await world.ratings()
    t1, t2 = roster(T1), roster(T2)
    traded = {**t1, "TOP": 103, "MID": 101}
    games = [GameResult(winner=1, team1=t1, team2=t2),
             GameResult(winner=2, team1=traded, team2=t2),
             GameResult(winner=1, team1=t1, team2=t2)]
    assert await world.admin_cog._commit_games(match_id, games, None, ADMIN) == "ok"

    after = await world.ratings()
    assert_ratings_consistent(after)
    assert after[(101, "TOP")][3] == 2 and after[(101, "MID")][3] == 1   # games per role
    assert after[(103, "MID")][3] == 2 and after[(103, "TOP")][3] == 1
    stats = await world.game_stats(match_id)
    assert len(stats) == 30
    assert all(s.role_elo_delta is not None and s.inhouse_elo_delta is not None for s in stats)

    status, count, prev = await world.admin_cog._revert_result(match_id)
    assert (status, count, prev) == ("ok", 10, "2-1")
    assert await world.ratings() == before
    assert await world.game_stats(match_id) == []


@flow
async def test_unreport_still_works_for_matches_committed_before_per_game_deltas(world):
    """Production has matches whose GameStat rows predate the delta columns (NULL).
    They must un-report through MatchPerformance exactly as before."""
    match_id = await setup(world)
    before = await world.ratings()
    t1, t2 = roster(T1), roster(T2)
    games = [GameResult(winner=1, team1=t1, team2=t2),
             GameResult(winner=2, team1={**t1, "JUNGLE": 111}, team2=t2),   # same-role sub
             GameResult(winner=1, team1=t1, team2=t2)]
    assert await world.admin_cog._commit_games(match_id, games, None, ADMIN) == "ok"
    async with get_session() as db:  # make the rows look like pre-migration ones
        await db.execute(update(GameStat).values(role_elo_delta=None, inhouse_elo_delta=None))
        await db.commit()

    status, count, _ = await world.admin_cog._revert_result(match_id)
    assert status == "ok" and count == 11
    assert await world.ratings() == before


# ----------------------------------------------------------------- 2. visible failures

@flow
async def test_failed_reads_are_shown_and_cannot_be_committed_blind(world):
    match_id = await setup(world)
    ix = await world.report(match_id, [
        failed_board("Vision read failed (APIStatusError: boom). Enter the lineup manually."),
        failed_board("Vision read timed out after 60s — enter the lineup manually."),
    ])
    assert "None of the screenshots could be read" in ix.text
    assert "boom" in ix.text and "timed out after 60s" in ix.text      # the notes are rendered
    view = world.report_view(ix)
    assert view.state.winners == [None, None]                          # no default to team 1
    assert "winner?" in find_item(view, "G1").label

    click = await world.click(find_item(view, "Commit"))
    assert "Can't commit yet" in click.text and "pick the winner" in click.text
    assert (await world.match(match_id)).winner is None

    await world.set_winners(view, [1, 1])
    assert find_item(view, "G1").label == "G1: lo gang won"
    click = await world.click(find_item(view, "Commit"))
    assert "✅ Match" in click.text and "recorded" in click.text
    m = await world.match(match_id)
    assert (m.winner, m.team1_wins, m.team2_wins) == (1, 2, 0)


@flow
async def test_one_failed_read_is_noted_per_game(world):
    match_id = await setup(world)
    ix = await world.report(match_id, [clean_board(1), failed_board("Vision read was declined.")])
    assert "None of the screenshots" not in ix.text
    assert "Game 2:" in ix.text and "Vision read was declined." in ix.text
    assert world.report_view(ix).state.winners == [1, None]


@flow
async def test_bad_attachments_get_a_clear_note_and_are_never_downloaded(world):
    match_id = await setup(world)
    txt = FakeAttachment("notes.txt", b"hello", content_type="text/plain")
    huge = FakeAttachment("huge.png", b"\x89PNG-1", size=11 * 1024 * 1024)
    ix = await world.report(match_id, [None, None], attachments=[txt, huge])
    assert "“notes.txt” is not an image" in ix.text
    assert "“huge.png” is too large (11.0 MB" in ix.text
    assert txt.reads == 0 and huge.reads == 0
    assert "None of the screenshots could be read" in ix.text


# ----------------------------------------------------------------- 3 / 4. alignment through the view

@flow
async def test_report_viewer_first_screenshot_commits_rostered_roles(world):
    """Match-history order: the viewer (104, BOT) heads team 1's list. The commit
    must still put everyone in their rostered role."""
    match_id = await setup(world)
    board = make_board([("P104", "Jinx", (9, 1, 5)), "P101", "P102", "P103", "P105"],
                       names(T2), winner=1)
    ix = await world.report(match_id, [board])
    view = world.report_view(ix)
    assert "🔁" not in ix.text                                         # nobody flagged as a sub
    click = await world.click(find_item(view, "Commit"))
    assert "✅ Match" in click.text
    stats = {s.discord_id: s for s in await world.game_stats(match_id)}
    assert stats[104].role == "BOT" and stats[104].champion == "Jinx" and stats[104].kills == 9
    assert stats[101].role == "TOP" and stats[103].role == "MID"


@flow
async def test_swap_teams_control(world):
    """Unknown names: orientation can't be told, so no winner is hinted and the view
    offers a swap that moves the screenshot's sub to the other team."""
    match_id = await setup(world)
    board = make_board(["x0", "x1", "x2", "x3", "P111"], ["y0", "y1", "y2", "y3", "y4"], winner=1)
    ix = await world.report(match_id, [board])
    assert "can't tell which screenshot team is which" in ix.text
    view = world.report_view(ix)
    assert view.state.winners == [None]
    assert view.state._slot(0, 1, "SUPPORT").discord_id == 111

    await world.click(find_item(view, "Subs / fix"))
    click = await world.click(find_item(view, "Swap teams"))
    assert "Teams swapped by you" in click.text
    assert view.state._slot(0, 2, "SUPPORT").discord_id == 111
    assert view.state._slot(0, 1, "SUPPORT").discord_id == 105
    assert view.state.winners == [2]


@flow
async def test_commit_failure_is_reported_to_the_admin(world):
    match_id = await setup(world)
    view = world.report_view(await world.report(match_id, [clean_board(1)]))

    async def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    world.admin_cog._commit_games = boom
    click = await world.click(find_item(view, "Commit"))
    assert "NOT recorded" in click.text and "disk full" in click.text
    assert (await world.match(match_id)).winner is None

    del world.admin_cog._commit_games                      # the fault clears; retry works
    click = await world.click(find_item(view, "Commit"))
    assert "✅ Match" in click.text and (await world.match(match_id)).winner == 1


# ----------------------------------------------------------------- 5 / 6. slot picker

@flow
async def test_picking_a_sub_does_not_rebind_the_rostered_players_name(world):
    """The MID row read 'P103' and resolved to 103. Putting sub 111 in that slot
    must NOT save 'p103' as an alias of 111."""
    match_id = await setup(world)
    view = world.report_view(await world.report(match_id, [clean_board(1)]))
    picker = await open_slot(world, view, 0, 1, "MID")
    click = await world.click(picker, values=[world.guild.members[111]])
    assert view.state._slot(0, 1, "MID").discord_id == 111
    assert await world.aliases() == {}
    assert "Learned alias" not in click.text


@flow
async def test_unresolved_name_is_learned_in_full_and_announced(world):
    match_id = await setup(world)
    board = make_board(["P101", "P102", "Renamed Guy 77", "P104", "P105"], names(T2), winner=1)
    view = world.report_view(await world.report(match_id, [board]))
    assert view.state._slot(0, 1, "MID").discord_id == 103          # roster kept
    picker = await open_slot(world, view, 0, 1, "MID")
    click = await world.click(picker, values=[world.guild.members[103]])
    assert await world.aliases() == {"renamed guy 77": 103}          # the whole name, not "renamed"
    assert "Learned alias “Renamed Guy 77” → <@103>" in click.text


@flow
async def test_learn_alias_never_overwrites(world):
    await setup(world)
    cog = world.admin_cog
    assert await cog._learn_alias("Some Name", 103) == ("learned", None)
    assert await cog._learn_alias("some  name", 103) == ("exists", None)
    assert await cog._learn_alias("SOME NAME", 104) == ("conflict", 103)
    assert await world.aliases() == {"some name": 103}
    assert await cog._learn_alias("   ", 104) == ("skipped", None)


@flow
async def test_existing_alias_conflict_is_reported_not_overwritten(world):
    """The read name is unresolved in this report, but by the time the admin picks
    a player another admin has bound that name to someone else."""
    match_id = await setup(world)
    board = make_board(["P101", "P102", "Renamed Guy 77", "P104", "P105"], names(T2), winner=1)
    view = world.report_view(await world.report(match_id, [board]))
    async with get_session() as db:
        db.add(Alias(alias_norm="renamed guy 77", discord_id=112, alias="Renamed Guy 77"))
        await db.commit()
    picker = await open_slot(world, view, 0, 1, "MID")
    click = await world.click(picker, values=[world.guild.members[103]])
    assert "already an alias for <@112>" in click.text
    assert await world.aliases() == {"renamed guy 77": 112}


@flow
async def test_sub_picker_rejects_unlinked_members_and_bots(world):
    match_id = await setup(world)
    stranger = world.add_member(900, "stranger")                    # never linked
    pending = world.add_member(901, "pending")
    async with get_session() as db:
        db.add(Player(discord_id=901, riot_game_name="Pend", riot_tag_line="NA1",
                      riot_puuid="puuid901", link_status="pending"))
        await db.commit()
    view = world.report_view(await world.report(match_id, [clean_board(1)]))
    picker = await open_slot(world, view, 0, 1, "MID")

    for who, why in ((stranger, "not a linked, approved player"),
                     (pending, "not a linked, approved player"),
                     (world.guild.me, "a bot")):
        click = await world.click(picker, values=[who])
        assert why in click.text and "slot was not changed" in click.text
        assert view.state._slot(0, 1, "MID").discord_id == 103

    await world.click(picker, values=[world.guild.members[111]])    # a real player is fine
    assert view.state._slot(0, 1, "MID").discord_id == 111


# ----------------------------------------------------------------- 8. truthful status, double click

@flow
async def test_commit_reports_what_actually_happened(world):
    match_id = await setup(world)
    cog = world.admin_cog
    game = GameResult(winner=1, team1=roster(T1), team2=roster(T2))
    assert await cog._commit_games(9999, [game], None, ADMIN) == "not_found"
    assert await cog._commit_games(match_id, [], None, ADMIN) == "empty"
    assert await cog._commit_result(9999, 2, 0, None, ADMIN, None) == "not_found"
    before = await world.ratings()
    assert await cog._commit_games(match_id, [game, game], None, ADMIN) == "ok"
    after = await world.ratings()
    assert after != before
    assert await cog._commit_games(match_id, [game, game], None, ADMIN) == "already_reported"
    assert await cog._commit_result(match_id, 2, 0, None, ADMIN, None) == "already_reported"
    assert await world.ratings() == after


@flow
async def test_view_does_not_claim_success_when_someone_else_reported_first(world):
    match_id = await setup(world)
    view = world.report_view(await world.report(match_id, [clean_board(1), clean_board(1)]))
    assert await world.admin_cog._commit_result(match_id, 0, 2, None, 1, None) == "ok"
    after_other = await world.ratings()

    click = await world.click(find_item(view, "Commit"))
    assert "✅ Match" not in click.text and "already reported" in click.text
    assert await world.ratings() == after_other
    assert (await world.match(match_id)).winner == 2


@flow
async def test_double_click_commit_fires_once(world):
    match_id = await setup(world)
    view = world.report_view(await world.report(match_id, [clean_board(1), clean_board(1)]))
    commit = find_item(view, "Commit")
    a = world.ix(world.admin, message=view.message)
    b = world.ix(world.admin, message=view.message)
    await asyncio.gather(commit.callback(a), commit.callback(b))

    texts = [a.text, b.text]
    assert sum("✅ Match" in t for t in texts) == 1
    assert sum("Already committing" in t for t in texts) == 1
    assert all(c.disabled for c in view.children)
    assert (await world.ratings())[(101, INHOUSE_ROLE)][3] == 2      # two games, counted once


# ----------------------------------------------------------------- 9. series validation

@flow
async def test_impossible_series_scores_are_refused(world):
    match_id = await setup(world)
    for bad in ("1-1", "3-0", "5-0", "0-0"):
        ix = await world.run(world.admin_cog.report_manual, series_score=bad, match_id=match_id)
        assert "Invalid series score" in ix.text, bad
    assert (await world.match(match_id)).winner is None

    view = world.report_view(await world.report(match_id, [clean_board(1), clean_board(2)]))
    click = await world.click(find_item(view, "Commit"))
    assert "1-1 isn't a best-of-3 result" in click.text
    assert (await world.match(match_id)).winner is None

    ix = await world.run(world.admin_cog.report_manual, series_score="2-1", match_id=match_id)
    assert "✅ Match" in ix.text and (await world.match(match_id)).winner == 1


# ----------------------------------------------------------------- 10. 30 slots

@flow
async def test_every_slot_of_a_three_game_series_is_editable(world):
    match_id = await setup(world)
    view = world.report_view(
        await world.report(match_id, [clean_board(1), clean_board(2), clean_board(1)]))
    await world.click(find_item(view, "Subs / fix"))
    reachable = set()
    for gi in range(3):
        await world.click(find_item(view, cls=_GameSelect), values=[str(gi)])
        options = find_item(view, cls=_SlotSelect).options
        assert len(options) <= 25
        reachable |= {o.value for o in options}
    assert reachable == {f"{gi}:{team}:{role}" for gi in range(3) for team in (1, 2) for role in ROLES}

    # Game 3's last slot — one of the five the old single 25-option menu dropped.
    await world.click(find_item(view, cls=_SlotSelect), values=["2:2:SUPPORT"])
    await world.click(find_item(view, cls=_SlotPlayerSelect), values=[world.guild.members[112]])
    assert view.state._slot(2, 2, "SUPPORT").discord_id == 112
    await world.click(find_item(view, "Done"))
    click = await world.click(find_item(view, "Commit"))
    assert "✅ Match" in click.text
    assert {s.discord_id for s in await world.game_stats(match_id) if s.game_no == 3} >= {112}


# ----------------------------------------------------------------- 7. rescore

async def _commit_three_games(world, match_id):
    t1, t2 = roster(T1), roster(T2)
    games = [GameResult(winner=1, team1=t1, team2=t2, champions={101: "Garen"}, kdas={101: (1, 2, 3)}),
             GameResult(winner=2, team1={**t1, "JUNGLE": 111}, team2=t2),
             GameResult(winner=1, team1=t1, team2=t2)]
    assert await world.admin_cog._commit_games(match_id, games, "http://shot", ADMIN) == "ok"


def _stat_key(s):
    return (s.game_no, s.discord_id, s.role, s.won, s.champion, s.kills,
            s.role_elo_delta, s.inhouse_elo_delta)


@flow
async def test_rescore_reproduces_the_same_result(world):
    match_id = await setup(world)
    await _commit_three_games(world, match_id)
    ratings = await world.ratings()
    stats = [_stat_key(s) for s in await world.game_stats(match_id)]

    ix = await world.run(world.admin_cog.rescore_match, match_id=match_id)
    assert "✅ Rescored Match" in ix.text
    assert await world.ratings() == ratings
    assert [_stat_key(s) for s in await world.game_stats(match_id)] == stats
    m = await world.match(match_id)
    assert (m.winner, m.team1_wins, m.team2_wins, m.screenshot_url) == (1, 2, 1, "http://shot")


@flow
async def test_rescore_is_atomic_when_the_recommit_fails(world):
    """If re-applying fails after the old result was reversed, NOTHING may change —
    the lineups only exist in the rows the reverse step deletes."""
    match_id = await setup(world)
    await _commit_three_games(world, match_id)
    ratings = await world.ratings()
    stats = [_stat_key(s) for s in await world.game_stats(match_id)]

    async def boom(self, db, *args, **kwargs):
        raise RuntimeError("boom")

    import bot.services.results as results_mod

    real = results_mod.commit_games_in
    results_mod.commit_games_in = boom
    try:
        ix = await world.run(world.admin_cog.rescore_match, match_id=match_id)
    finally:
        results_mod.commit_games_in = real
    assert "failed" in ix.text and "nothing was changed" in ix.text
    assert await world.ratings() == ratings
    assert [_stat_key(s) for s in await world.game_stats(match_id)] == stats
    async with get_session() as db:
        assert len((await db.execute(select(MatchPerformance))).scalars().all()) == 11
    assert (await world.match(match_id)).winner == 1


@flow
async def test_rescore_refuses_incomplete_saved_games(world):
    match_id = await setup(world)
    await _commit_three_games(world, match_id)
    ratings = await world.ratings()

    async with get_session() as db:      # a row with no role
        await db.execute(update(GameStat).where(
            GameStat.game_no == 2, GameStat.discord_id == 111).values(role=None))
        await db.commit()
    ix = await world.run(world.admin_cog.rescore_match, match_id=match_id)
    assert "Can't rescore" in ix.text and "Game 2" in ix.text
    assert await world.ratings() == ratings

    async with get_session() as db:      # a missing player (4 + 5)
        await db.execute(delete(GameStat).where(
            GameStat.game_no == 2, GameStat.discord_id == 111))
        await db.commit()
    ix = await world.run(world.admin_cog.rescore_match, match_id=match_id)
    assert "Can't rescore" in ix.text and "need 5" in ix.text
    assert await world.ratings() == ratings
    assert (await world.match(match_id)).winner == 1


# ----------------------------------------------------------------- 11. lock

@flow
async def test_concurrent_commits_do_not_lose_updates(world):
    """Two admins report two matches of the same ten players at the same moment.
    Every rating must end up as its seed plus the sum of ALL recorded deltas."""
    m1 = await setup(world)
    m2 = await world.add_match(T2, T1)
    cog = world.admin_cog
    results = await asyncio.gather(cog._commit_result(m1, 2, 0, None, 1, None),
                                   cog._commit_result(m2, 2, 1, None, 2, None))
    assert results == ["ok", "ok"]

    ratings = await world.ratings()
    assert_ratings_consistent(ratings)
    role_sum, overall_sum, games = {}, {}, {}
    for s in await world.game_stats():
        role_sum[(s.discord_id, s.role)] = role_sum.get((s.discord_id, s.role), 0) + s.role_elo_delta
        overall_sum[s.discord_id] = overall_sum.get(s.discord_id, 0) + s.inhouse_elo_delta
        games[s.discord_id] = games.get(s.discord_id, 0) + 1
    assert set(games.values()) == {5}                                   # 2 + 3 games each
    for pid in T1 + T2:
        assert ratings[(pid, INHOUSE_ROLE)][2] == overall_sum[pid]
        assert ratings[(pid, INHOUSE_ROLE)][3] == 5
    for (pid, role), total in role_sum.items():
        assert ratings[(pid, role)][2] == total


# ----------------------------------------------------------------- 12. game_date / predicted_balance

@flow
async def test_commit_sets_game_date(world):
    await world.add_players(10, elo=SEEDS[:10])
    session = await world.recruit(date(2030, 5, 16))
    in_session = await world.add_match(T1, T2, session_id=session.id)
    pickup = await world.add_match(T1, T2)
    preset = await world.add_match(T1, T2)
    async with get_session() as db:
        (await db.get(Match, preset)).game_date = date(2029, 1, 2)
        await db.commit()
    for mid in (in_session, pickup, preset):
        assert await world.admin_cog._commit_result(mid, 2, 0, None, ADMIN, None) == "ok"

    assert (await world.match(in_session)).game_date == date(2030, 5, 16)   # the session's night
    today = datetime.now(pytz.timezone(world.config.timezone)).date()
    assert (await world.match(pickup)).game_date == today                   # bot timezone
    assert (await world.match(preset)).game_date == date(2029, 1, 2)        # never overwritten


@flow
async def test_unreport_clears_only_a_balance_the_commit_set(world):
    await world.add_players(10, elo=SEEDS[:10])
    session = await world.recruit(date(2030, 5, 16))
    manual = await world.add_match(T1, T2)                               # predicted_balance NULL
    made = await world.add_match(T1, T2, session_id=session.id, predicted_balance=12.5)
    async with get_session() as db:                                      # as the matchmaker leaves it
        db.add(ProposalSet(session_id=session.id, proposals_json="[]", chosen_index=0,
                           resolved_match_id=made))
        await db.commit()
    cog = world.admin_cog

    assert await cog._commit_result(manual, 2, 0, None, ADMIN, None) == "ok"
    first = (await world.match(manual)).predicted_balance
    assert first == float(abs(sum(SEEDS[:5]) - sum(SEEDS[5:10])))
    assert (await cog._revert_result(manual))[0] == "ok"
    assert (await world.match(manual)).predicted_balance is None        # not left stale

    # Ratings move (another series), then the manual match is re-reported: the
    # balance must be recomputed from the ratings as they are now.
    assert await cog._commit_result(made, 0, 2, None, ADMIN, None) == "ok"
    assert await cog._commit_result(manual, 2, 0, None, ADMIN, None) == "ok"
    assert (await world.match(manual)).predicted_balance != first
    assert (await cog._revert_result(manual))[0] == "ok"

    assert (await world.match(made)).predicted_balance == 12.5
    assert (await cog._revert_result(made))[0] == "ok"
    assert (await world.match(made)).predicted_balance == 12.5          # the matchmaker's, kept
    async with get_session() as db:
        diffs = [r.total_balance_diff for r in (await db.execute(select(Rating))).scalars().all()]
    assert all(abs(d) < 1e-9 for d in diffs)                            # fully unwound


# ----------------------------------------------------------------- 14. pickup-series

async def _submit_pickup(world, score="2-1", commit=None):
    modal = PickupSeriesModal(commit_callback=commit or world.admin_cog._commit_result)
    modal.roster._value = format_roster_block(roster(T1), roster(T2))
    modal.series_score._value = score
    ix = world.ix(world.admin)
    await modal.on_submit(ix)
    return ix


@flow
async def test_pickup_series_retry_is_not_recorded_twice(world):
    await world.add_players(10, elo=SEEDS[:10])
    first = await _submit_pickup(world)
    assert "Pickup series recorded as match" in first.text
    ratings = await world.ratings()
    assert ratings[(101, INHOUSE_ROLE)][3] == 3

    again = await _submit_pickup(world)                                 # the admin retries
    assert "already recorded as **match" in again.text and "nothing was recorded" in again.text
    assert len(await world.matches()) == 1
    assert await world.ratings() == ratings

    # After un-reporting it, the same rosters can be recorded again.
    match_id = (await world.matches())[0].id
    assert (await world.admin_cog._revert_result(match_id))[0] == "ok"
    third = await _submit_pickup(world, score="0-2")
    assert "Pickup series recorded as match" in third.text


@flow
async def test_pickup_series_failure_leaves_no_orphan_match(world):
    await world.add_players(10, elo=SEEDS[:10])
    before = await world.ratings()

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await _submit_pickup(world, commit=boom)
    assert await world.matches() == []
    assert await world.ratings() == before

    bad = await _submit_pickup(world, score="1-1")
    assert "Invalid series score" in bad.text and await world.matches() == []


# ----------------------------------------------------------------- 15. timeout

@flow
async def test_expired_report_says_so(world):
    match_id = await setup(world)
    view = world.report_view(await world.report(match_id, [clean_board(1)]))
    await view.on_timeout()
    assert "expired" in view.message.content and "nothing was saved" in view.message.content
    assert view.message.view is None

    # After the admin has interacted, the newest interaction's token is used.
    view = world.report_view(await world.report(match_id, [clean_board(1)]))
    click = await world.click(find_item(view, "Subs / fix"))
    await view.on_timeout()
    assert "expired" in click.text and "Run `/report` again" in click.text
    assert (await world.match(match_id)).winner is None
