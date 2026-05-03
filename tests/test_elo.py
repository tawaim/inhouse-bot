"""Tests for chess elo seeding and update math."""
from bot.services.elo import (
    DEFAULT_ELO,
    K_ESTABLISHED,
    K_NEW,
    K_THRESHOLD,
    average_elo,
    expected_score,
    seed_from_rank,
    update_elo,
)


def test_unranked_returns_default():
    assert seed_from_rank(None, None) == DEFAULT_ELO


def test_higher_tier_higher_elo():
    assert seed_from_rank("CHALLENGER", "I") > seed_from_rank("IRON", "IV")


def test_division_bonus_within_tier():
    """Same tier, higher division -> higher seed."""
    assert seed_from_rank("GOLD", "I") > seed_from_rank("GOLD", "IV")


def test_case_insensitive():
    assert seed_from_rank("PLATINUM", "II") == seed_from_rank("platinum", "ii")


def test_unknown_tier_returns_default():
    assert seed_from_rank("MYTHIC", "I") == DEFAULT_ELO


def test_expected_score_equal_elo():
    """Equal players should have 50% expected score."""
    assert abs(expected_score(1500, 1500) - 0.5) < 1e-9


def test_expected_score_higher_player_favored():
    assert expected_score(1700, 1500) > 0.5
    assert expected_score(1500, 1700) < 0.5


def test_expected_score_chess_200pt_rule():
    """Chess elo: a 200pt advantage means ~76% expected score."""
    score = expected_score(1700, 1500)
    assert 0.74 < score < 0.78


def test_update_elo_winner_gains_loser_loses():
    new_winner, delta_w = update_elo(1500, 1500, won=True, games_played=10)
    new_loser, delta_l = update_elo(1500, 1500, won=False, games_played=10)
    assert delta_w > 0
    assert delta_l < 0
    # Equal players, equal magnitudes
    assert delta_w == -delta_l


def test_update_elo_upset_pays_more():
    """Beating a higher-rated opponent gains more elo than beating a peer."""
    _, delta_upset = update_elo(1300, 1700, won=True, games_played=10)
    _, delta_peer = update_elo(1300, 1300, won=True, games_played=10)
    assert delta_upset > delta_peer


def test_k_factor_drops_after_threshold():
    """First K_THRESHOLD games use K_NEW, then K_ESTABLISHED."""
    _, delta_new = update_elo(1500, 1500, won=True, games_played=K_THRESHOLD - 1)
    _, delta_old = update_elo(1500, 1500, won=True, games_played=K_THRESHOLD)
    # Both winners against equal opponent, but new player gains more
    assert delta_new > delta_old
    assert delta_new == K_NEW // 2  # round(40 * 0.5) = 20
    assert delta_old == K_ESTABLISHED // 2  # round(20 * 0.5) = 10


def test_average_elo_simple():
    assert average_elo([1000, 1500, 2000]) == 1500


def test_average_elo_empty_returns_default():
    assert average_elo([]) == DEFAULT_ELO


def test_full_matchup_team_avg_diff():
    """A team of 1700s beating a team of 1300s — winners gain less than peers would."""
    # Team1 (avg 1700) beats Team2 (avg 1300). Each team1 player's gain < gain vs peer.
    _, delta = update_elo(1700, 1300, won=True, games_played=20)
    # Expected score against 1300 is ~0.91; only 9% of K=20 = ~2 elo
    assert 1 <= delta <= 3
