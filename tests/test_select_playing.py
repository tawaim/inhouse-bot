"""Tests for _select_playing — the Allorim bench rule. Run: python -m pytest tests/"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot.cogs.recruitment import _is_allorim, _select_playing, _team_lines


def signup(pid: int, order: int):
    """A stand-in Signup: only discord_id and signed_up_at are read."""
    return SimpleNamespace(
        discord_id=pid,
        signed_up_at=datetime(2026, 1, 1) + timedelta(minutes=order),
    )


def player(pid: int, game_name="someone", tag="NA1"):
    return SimpleNamespace(discord_id=pid, riot_game_name=game_name, riot_tag_line=tag)


ALLORIM_ID = 999


def make_pool(n_others: int, with_allorim: bool):
    """n_others non-Allorim signups (ids 0..n-1) + optionally Allorim (id 999),
    Allorim signing up first so time-order alone would include him."""
    sus, players = [], {}
    if with_allorim:
        sus.append(signup(ALLORIM_ID, order=0))
        players[ALLORIM_ID] = player(ALLORIM_ID, game_name="Allorim", tag="NA1")
    for i in range(n_others):
        sus.append(signup(i, order=i + 1))
        players[i] = player(i)
    return sus, players


def test_is_allorim_case_insensitive():
    assert _is_allorim(player(1, "allorim", "na1"))
    assert _is_allorim(player(1, "Allorim", "NA1"))
    assert not _is_allorim(player(1, "allorim", "EUW"))   # wrong tag
    assert not _is_allorim(player(1, "Allorimm", "NA1"))  # wrong name
    assert not _is_allorim(None)


def test_allorim_benched_when_overflow():
    """11 signups (10 others + Allorim) -> Allorim is left out."""
    sus, players = make_pool(n_others=10, with_allorim=True)
    selected = {s.discord_id for s in _select_playing(sus, players)}
    assert len(selected) == 10
    assert ALLORIM_ID not in selected


def test_allorim_plays_at_exactly_ten():
    """9 others + Allorim = 10 total -> Allorim is needed and plays."""
    sus, players = make_pool(n_others=9, with_allorim=True)
    selected = {s.discord_id for s in _select_playing(sus, players)}
    assert len(selected) == 10
    assert ALLORIM_ID in selected


def test_allorim_benched_with_one_other_to_spare():
    """12 signups (11 others + Allorim): Allorim out, last non-Allorim also benched."""
    sus, players = make_pool(n_others=11, with_allorim=True)
    selected = {s.discord_id for s in _select_playing(sus, players)}
    assert len(selected) == 10
    assert ALLORIM_ID not in selected


def test_no_allorim_is_plain_first_ten():
    sus, players = make_pool(n_others=12, with_allorim=False)
    selected = [s.discord_id for s in _select_playing(sus, players)]
    assert selected == list(range(10))  # first 10 by signup order


def test_team_lines_render_top_down():
    """Rosters always render top-down (TOP, JUNGLE, MID, BOT, SUPPORT) using the
    short role labels, regardless of the dict's insertion order."""
    by_role = {"SUPPORT": 5, "MID": 3, "TOP": 1, "BOT": 4, "JUNGLE": 2}
    out = _team_lines(by_role, emoji=False)
    roles_in_order = [line.split("**")[1] for line in out.splitlines()]
    assert roles_in_order == ["TOP", "JUN", "MID", "BOT", "SUP"]
    assert out.splitlines()[0] == "**TOP**: <@1>"
