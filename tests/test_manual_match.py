"""Tests for the /manual-match input parser."""
import pytest

from bot.cogs.admin import ManualMatchParseError, parse_manual_match


VALID_ROSTER = """\
TEAM 1
TOP: <@1001>
JUNGLE: <@1002>
MID: <@1003>
BOT: <@1004>
SUPPORT: <@1005>
TEAM 2
TOP: <@2001>
JUNGLE: <@2002>
MID: <@2003>
BOT: <@2004>
SUPPORT: <@2005>
"""


def test_valid_roster_parses_cleanly():
    t1, t2 = parse_manual_match(VALID_ROSTER)
    assert t1 == {"TOP": 1001, "JUNGLE": 1002, "MID": 1003, "BOT": 1004, "SUPPORT": 1005}
    assert t2 == {"TOP": 2001, "JUNGLE": 2002, "MID": 2003, "BOT": 2004, "SUPPORT": 2005}


def test_nickname_mention_format():
    """Discord uses <@!id> for users with server nicknames; both forms must work."""
    text = VALID_ROSTER.replace("<@1001>", "<@!1001>")
    t1, _ = parse_manual_match(text)
    assert t1["TOP"] == 1001


def test_role_aliases():
    """JG, ADC, SUP, etc. should resolve to canonical roles."""
    text = """\
TEAM 1
TOP: <@1>
JG: <@2>
MIDDLE: <@3>
ADC: <@4>
SUP: <@5>
TEAM 2
TOP: <@6>
JUNG: <@7>
MID: <@8>
BOTTOM: <@9>
SUPP: <@10>
"""
    t1, t2 = parse_manual_match(text)
    assert set(t1.keys()) == {"TOP", "JUNGLE", "MID", "BOT", "SUPPORT"}
    assert set(t2.keys()) == {"TOP", "JUNGLE", "MID", "BOT", "SUPPORT"}
    assert t1["JUNGLE"] == 2
    assert t1["BOT"] == 4


def test_team_header_variants():
    """T1/T2/TEAM1/TEAM2 should all work."""
    text = VALID_ROSTER.replace("TEAM 1", "T1").replace("TEAM 2", "T2")
    t1, t2 = parse_manual_match(text)
    assert len(t1) == 5 and len(t2) == 5


def test_blank_lines_ignored():
    text = VALID_ROSTER.replace("\nTEAM 2", "\n\n\nTEAM 2\n")
    t1, t2 = parse_manual_match(text)
    assert len(t1) == 5 and len(t2) == 5


def test_empty_input_rejected():
    with pytest.raises(ManualMatchParseError, match="empty"):
        parse_manual_match("")


def test_role_before_team_header_rejected():
    text = "TOP: <@1>\nTEAM 1\n"
    with pytest.raises(ManualMatchParseError, match="TEAM header"):
        parse_manual_match(text)


def test_unknown_role_rejected():
    text = VALID_ROSTER.replace("TOP: <@1001>", "TANK: <@1001>")
    with pytest.raises(ManualMatchParseError, match="unknown role"):
        parse_manual_match(text)


def test_missing_role_rejected():
    text = VALID_ROSTER.replace("SUPPORT: <@1005>\n", "")
    with pytest.raises(ManualMatchParseError, match="missing"):
        parse_manual_match(text)


def test_duplicate_role_on_same_team_rejected():
    text = VALID_ROSTER.replace("MID: <@1003>", "TOP: <@1003>")
    with pytest.raises(ManualMatchParseError, match="already assigned"):
        parse_manual_match(text)


def test_no_mention_rejected():
    text = VALID_ROSTER.replace("<@1001>", "alice")
    with pytest.raises(ManualMatchParseError, match="no Discord mention"):
        parse_manual_match(text)


def test_player_on_both_teams_rejected():
    text = VALID_ROSTER.replace("<@2001>", "<@1001>")
    with pytest.raises(ManualMatchParseError, match="both teams"):
        parse_manual_match(text)


def test_garbage_line_rejected():
    text = VALID_ROSTER + "this is just garbage\n"
    with pytest.raises(ManualMatchParseError, match="ROLE: @user"):
        parse_manual_match(text)
