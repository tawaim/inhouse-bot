"""Tests for the recruitment scheduler date math."""
from datetime import date, timedelta


def test_monday_close_finds_this_weeks_thursday():
    """Replicates the Monday close job's 'this Thursday' computation."""
    monday = date(2026, 5, 25)  # a Monday (weekday 0)
    assert monday.weekday() == 0
    this_thursday = monday + timedelta(days=(3 - monday.weekday()) % 7)
    assert this_thursday.weekday() == 3
    assert this_thursday == monday + timedelta(days=3)
