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


def test_linear_ladder_no_gaps():
    """Plat I + 50 == Emerald IV. Across the entire ladder, divisions are 50 apart
    and tiers are 200 apart, so the seed values form a smooth line."""
    assert seed_from_rank("PLATINUM", "IV") == 1600
    assert seed_from_rank("PLATINUM", "III") == 1650
    assert seed_from_rank("PLATINUM", "II") == 1700
    assert seed_from_rank("PLATINUM", "I") == 1750
    assert seed_from_rank("EMERALD", "IV") == 1800
    # Plat I + 50 == Emerald IV (no gap)
    assert seed_from_rank("EMERALD", "IV") - seed_from_rank("PLATINUM", "I") == 50


def test_apex_tiers_have_no_division_bonus():
    """Master/GM/Challenger don't have divisions. Should return the flat tier seed
    regardless of what division is passed in. Apex tiers are 100 elo apart."""
    assert seed_from_rank("MASTER", "I") == seed_from_rank("MASTER", "IV")
    assert seed_from_rank("MASTER", "I") == 2200
    assert seed_from_rank("GRANDMASTER", "II") == 2300
    assert seed_from_rank("CHALLENGER", "I") == 2500


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


def test_historical_rank_applies_rust_penalty():
    """Last-season rank should seed -200 elo from current-rank-IV equivalent."""
    from bot.services.elo import seed_from_historical_rank, RUST_PENALTY
    # Emerald IV current = 1800. Emerald historical = 1800 - 200 = 1600 (= Plat IV)
    assert seed_from_historical_rank("EMERALD") == seed_from_rank("PLATINUM", "IV")
    # Diamond historical = 2000 - 200 = 1800 (= Emerald IV)
    assert seed_from_historical_rank("DIAMOND") == seed_from_rank("EMERALD", "IV")
    # The penalty is exactly 200
    assert seed_from_historical_rank("EMERALD") == 1800 - RUST_PENALTY


def test_historical_rank_iron_floors_at_iron():
    """Iron has no lower seed; should stay at IRON's value (800)."""
    from bot.services.elo import seed_from_historical_rank, TIER_SEED
    assert seed_from_historical_rank("IRON") == TIER_SEED["IRON"]


def test_historical_rank_unranked_returns_default():
    from bot.services.elo import seed_from_historical_rank
    assert seed_from_historical_rank(None) == DEFAULT_ELO
    assert seed_from_historical_rank("MYTHIC") == DEFAULT_ELO


def test_historical_rank_case_insensitive():
    from bot.services.elo import seed_from_historical_rank
    assert seed_from_historical_rank("DIAMOND") == seed_from_historical_rank("diamond")


def test_parse_series_score_valid():
    from bot.services.elo import parse_series_score
    assert parse_series_score("2-0") == (2, 0)
    assert parse_series_score("2-1") == (2, 1)
    assert parse_series_score("1-2") == (1, 2)
    assert parse_series_score("0-2") == (0, 2)
    assert parse_series_score("3-2") == (3, 2)  # bo5
    assert parse_series_score("2:1") == (2, 1)  # alternate sep
    assert parse_series_score(" 2-1 ") == (2, 1)  # whitespace


def test_parse_series_score_invalid():
    from bot.services.elo import parse_series_score
    assert parse_series_score("") == (-1, -1)
    assert parse_series_score("foo") == (-1, -1)
    assert parse_series_score("2") == (-1, -1)
    assert parse_series_score("2-2") == (-1, -1)  # ties not allowed
    assert parse_series_score("-1-0") == (-1, -1)
    assert parse_series_score("99-0") == (-1, -1)  # too long


def test_series_2_0_gains_more_than_2_1():
    """A 2-0 sweep should award more elo than a 2-1 win."""
    from bot.services.elo import update_elo_series
    _, sweep_delta = update_elo_series(1500, 1500, 2, 0, games_played=20)
    _, narrow_delta = update_elo_series(1500, 1500, 2, 1, games_played=20)
    assert sweep_delta > narrow_delta > 0


def test_series_loss_costs_appropriately():
    """Losing 0-2 costs more than losing 1-2."""
    from bot.services.elo import update_elo_series
    _, swept_delta = update_elo_series(1500, 1500, 0, 2, games_played=20)
    _, narrow_loss = update_elo_series(1500, 1500, 1, 2, games_played=20)
    assert swept_delta < narrow_loss < 0


def test_series_2_0_equal_peers():
    """vs equal opponent, expected = 0.5, actual 1.0, K=20 -> +10 elo."""
    from bot.services.elo import update_elo_series
    _, delta = update_elo_series(1500, 1500, 2, 0, games_played=20)
    assert delta == 10


def test_series_2_1_equal_peers():
    """vs equal opponent, expected = 0.5, actual 0.667, K=20 -> +3 elo."""
    from bot.services.elo import update_elo_series
    _, delta = update_elo_series(1500, 1500, 2, 1, games_played=20)
    assert delta == 3
