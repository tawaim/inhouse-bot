"""Tests for the recruitment scheduler date math."""
from datetime import date, timedelta


def test_monday_close_finds_this_weeks_thursday():
    """Replicates the Monday close job's 'this Thursday' computation."""
    monday = date(2026, 5, 25)  # a Monday (weekday 0)
    assert monday.weekday() == 0
    this_thursday = monday + timedelta(days=(3 - monday.weekday()) % 7)
    assert this_thursday.weekday() == 3
    assert this_thursday == monday + timedelta(days=3)


def test_close_day_times_and_due_check():
    """A Tuesday-close session is skipped by Monday's run and closed by Tuesday's."""
    import pytz

    from bot.cogs.recruitment import signups_close_local
    from bot.services.scheduler import signups_due

    tz = pytz.timezone("America/New_York")
    thursday = date(2026, 11, 19)
    mon = signups_close_local(thursday, "Monday", tz)
    tue = signups_close_local(thursday, "Tuesday", tz)
    assert (mon.date(), mon.hour, mon.minute) == (date(2026, 11, 16), 21, 30)
    assert (tue.date(), tue.hour, tue.minute) == (date(2026, 11, 17), 21, 30)

    def utc(dt):
        return dt.astimezone(pytz.UTC).replace(tzinfo=None)

    monday_run, tuesday_run = utc(mon), utc(tue)
    assert signups_due(utc(mon), monday_run)          # default behaviour unchanged
    assert not signups_due(utc(tue), monday_run)      # Tuesday session waits
    assert signups_due(utc(tue), tuesday_run)
    assert not signups_due(None, tuesday_run)         # open-ended never auto-closes
