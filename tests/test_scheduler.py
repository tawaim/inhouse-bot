"""Tests for the recruitment scheduler date math."""
from datetime import date, timedelta

from bot.services.scheduler import next_thursday_after


def test_friday_plus_13_lands_on_thursday():
    """The Friday job posts for the Thursday 13 days out — must be a Thursday."""
    friday = date(2026, 5, 22)  # a Friday (weekday 4)
    assert friday.weekday() == 4
    target = next_thursday_after(friday, days_ahead=13)
    assert target.weekday() == 3            # Thursday
    assert target == friday + timedelta(days=13)  # no snap needed from a Friday


def test_result_is_always_a_thursday_from_any_day():
    """Whatever day the job fires, the target snaps to a Thursday."""
    start = date(2026, 1, 1)
    for offset in range(14):
        d = start + timedelta(days=offset)
        target = next_thursday_after(d, days_ahead=13)
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


def test_upcoming_recruit_thursdays_returns_next_two():
    """Self-heal reconciliation should target the next ~2 open Thursdays."""
    from bot.cogs.recruitment import upcoming_recruit_thursdays
    # From a Monday, the next two Thursdays fall within the 13-day window.
    got = upcoming_recruit_thursdays(date(2026, 5, 25))
    assert got == [date(2026, 5, 28), date(2026, 6, 4)]
    assert all(d.weekday() == 3 for d in got)


def test_upcoming_recruit_thursdays_includes_today_if_thursday():
    from bot.cogs.recruitment import upcoming_recruit_thursdays
    got = upcoming_recruit_thursdays(date(2026, 5, 28))  # a Thursday
    assert got[0] == date(2026, 5, 28)
    assert all(d.weekday() == 3 for d in got)
