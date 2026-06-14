"""Tests for the recruitment scheduler date math."""
from datetime import date, timedelta

from bot.services.scheduler import next_thursday_after


def test_friday_plus_6_lands_on_following_thursday():
    """The Friday job posts for the Thursday 6 days out — must be a Thursday."""
    friday = date(2026, 5, 22)  # a Friday (weekday 4)
    assert friday.weekday() == 4
    target = next_thursday_after(friday, days_ahead=6)
    assert target.weekday() == 3            # Thursday
    assert target == friday + timedelta(days=6)  # no snap needed from a Friday


def test_result_is_always_a_thursday_from_any_day():
    """Whatever day the job fires, the target snaps to a Thursday."""
    start = date(2026, 1, 1)
    for offset in range(14):
        d = start + timedelta(days=offset)
        target = next_thursday_after(d, days_ahead=6)
        assert target.weekday() == 3
        # And it's never in the past relative to d.
        assert target >= d


def test_monday_close_finds_this_weeks_thursday():
    """Replicates the Monday close job's 'this Thursday' computation."""
    monday = date(2026, 5, 25)  # a Monday (weekday 0)
    assert monday.weekday() == 0
    this_thursday = monday + timedelta(days=(3 - monday.weekday()) % 7)
    assert this_thursday.weekday() == 3
    assert this_thursday == monday + timedelta(days=3)


def test_upcoming_recruit_thursdays_skips_imminent():
    """An imminent Thursday (<4 days out, no signup window left) is NOT auto-posted —
    it's left for the admin to handle manually / open-ended."""
    from bot.cogs.recruitment import upcoming_recruit_thursdays
    got = upcoming_recruit_thursdays(date(2026, 5, 25))  # Monday
    assert date(2026, 5, 28) not in got    # 3 days out — skipped
    assert all(d.weekday() == 3 for d in got)


def test_upcoming_recruit_thursdays_posts_only_following_thursday():
    """The Friday cron posts the following Thursday (6 days out) and NOT the
    Thursday-after-next — no more posting two weeks in advance."""
    from bot.cogs.recruitment import upcoming_recruit_thursdays
    friday = date(2026, 5, 22)  # a Friday
    got = upcoming_recruit_thursdays(friday)
    assert date(2026, 5, 28) in got        # following Thursday (6 days out) — posted
    assert date(2026, 6, 4) not in got     # Thursday-after-next (13 days) — NOT posted
    assert all(d.weekday() == 3 for d in got)


def test_upcoming_recruit_thursdays_within_lead_and_horizon():
    from bot.cogs.recruitment import upcoming_recruit_thursdays
    base = date(2026, 5, 22)
    got = upcoming_recruit_thursdays(base)
    assert all(4 <= (d - base).days <= 6 for d in got)
