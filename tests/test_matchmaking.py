"""Tests for matchmaking. Run with: python -m pytest tests/"""
import pytest

from bot.services.matchmaking import PlayerInput, make_match


def make_player(pid: int, prefs: list[str], skill: int = 1200) -> PlayerInput:
    """Helper: make a player with the same elo across all roles."""
    return PlayerInput(
        discord_id=pid,
        preferred_roles=prefs,
        ratings={r: skill for r in ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]},
    )


def test_perfect_balance_with_all_fills():
    """10 players, all FILL, all equal skill — should produce balance_diff ~0."""
    players = [make_player(i, ["FILL"], skill=1200) for i in range(10)]
    proposal = make_match(players)
    assert proposal is not None
    assert proposal.balance_diff < 0.01


def test_role_assignment_respects_preferences():
    """5 specialists per team, no fills — every player must get their role."""
    players = []
    for team in range(2):
        for i, role in enumerate(["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]):
            players.append(make_player(team * 5 + i, [role]))
    proposal = make_match(players)
    assert proposal is not None
    assert proposal.role_penalty == 0.0


def test_impossible_when_nobody_supports():
    """If 0 of 10 want SUPPORT or FILL, no valid match exists."""
    # 10 players, all only want MID
    players = [make_player(i, ["MID"]) for i in range(10)]
    proposal = make_match(players)
    assert proposal is None


def test_balance_separates_strong_from_weak():
    """5 strong + 5 weak players should be split, not bunched."""
    strong = [make_player(i, ["FILL"], skill=40.0) for i in range(5)]
    weak = [make_player(i + 5, ["FILL"], skill=10.0) for i in range(5)]
    proposal = make_match(strong + weak)
    assert proposal is not None
    # Each team should have ~mix of strong and weak. Expected per-team total
    # at perfect split: 2 strong + 3 weak (or 3+2) = 80+30 or 120+20. The
    # algorithm should pick the closest split, which is 110/120 (diff 10) or
    # similar. Critical: it must NOT put all 5 strong on one team (200 vs 50).
    assert proposal.balance_diff < 100  # would be 150 if all strong on one team


def test_fill_player_takes_unwanted_role():
    """4 specialists + 1 FILL should let FILL take the empty role."""
    team_specs = [
        ("TOP", 1), ("JUNGLE", 2), ("MID", 3), ("BOT", 4),  # 4 specialists, no support
        ("FILL", 5),
    ]
    team1 = [make_player(pid, [role]) for role, pid in team_specs]
    team2_specs = [
        ("TOP", 6), ("JUNGLE", 7), ("MID", 8), ("BOT", 9),
        ("FILL", 10),
    ]
    team2 = [make_player(pid, [role]) for role, pid in team2_specs]
    proposal = make_match(team1 + team2)
    assert proposal is not None
    # The FILL players (5 and 10) must end up in SUPPORT
    assert proposal.team1.by_role["SUPPORT"] in (5, 10)
    assert proposal.team2.by_role["SUPPORT"] in (5, 10)


def test_wrong_player_count_raises():
    players = [make_player(i, ["FILL"]) for i in range(9)]
    with pytest.raises(ValueError):
        make_match(players)


def test_top_matches_returns_diverse_options():
    """make_top_matches with n=3 returns up to 3 options that differ by ≥2 swaps."""
    from bot.services.matchmaking import make_top_matches, _team_swap_count
    # Mixed skills + all FILL = lots of legal proposals to choose from
    players = [
        make_player(i, ["FILL"], skill=20.0 + i * 2.0)  # skills 20, 22, 24, ..., 38
        for i in range(10)
    ]
    proposals = make_top_matches(players, n=3, min_diff=2)
    assert len(proposals) == 3
    # Cost should be non-decreasing (best first)
    assert proposals[0].cost <= proposals[1].cost <= proposals[2].cost
    # Every pair differs by ≥2 swaps
    for i in range(len(proposals)):
        for j in range(i + 1, len(proposals)):
            assert _team_swap_count(proposals[i], proposals[j]) >= 2


def test_top_matches_n_one_matches_make_match():
    """With n=1, make_top_matches should return the same proposal as make_match."""
    from bot.services.matchmaking import make_top_matches
    players = [make_player(i, ["FILL"], skill=20.0 + i * 2.0) for i in range(10)]
    single = make_match(players)
    top1 = make_top_matches(players, n=1, min_diff=0)
    assert len(top1) == 1
    assert single is not None
    # Cost should match exactly
    assert abs(single.cost - top1[0].cost) < 1e-9


def test_top_matches_returns_empty_when_no_legal():
    """If no legal assignment exists, returns empty list (not None)."""
    from bot.services.matchmaking import make_top_matches
    players = [make_player(i, ["MID"]) for i in range(10)]  # nobody supports
    proposals = make_top_matches(players, n=3)
    assert proposals == []


def test_matchmaker_output_roundtrips_as_roster_block():
    """/roster-template path: matchmaker teams -> copy/paste block -> parses back."""
    from bot.cogs.admin import format_roster_block, parse_manual_match
    players = []
    for team in range(2):
        for i, role in enumerate(["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]):
            players.append(make_player(team * 5 + i, [role]))
    proposal = make_match(players)
    assert proposal is not None
    t1, t2 = proposal.team1.by_role, proposal.team2.by_role
    assert parse_manual_match(format_roster_block(t1, t2)) == (t1, t2)
