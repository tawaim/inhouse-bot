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
    assert delta_new == K_NEW // 2  # round(60 * 0.5) = 30
    assert delta_old == K_ESTABLISHED // 2  # round(50 * 0.5) = 25


def test_average_elo_simple():
    assert average_elo([1000, 1500, 2000]) == 1500


def test_average_elo_empty_returns_default():
    assert average_elo([]) == DEFAULT_ELO


def test_full_matchup_team_avg_diff():
    """A team of 1700s beating a team of 1300s — winners gain less than peers would."""
    # Team1 (avg 1700) beats Team2 (avg 1300). Each team1 player's gain < gain vs peer.
    _, delta = update_elo(1700, 1300, won=True, games_played=20)
    # Expected score against 1300 is ~0.91; only ~9% of K=50 = ~5 elo
    assert 3 <= delta <= 7


def test_past_season_is_division_aware():
    """Past-season seed starts from the exact tier+division ladder value."""
    from bot.services.elo import seed_from_past_season
    # Emerald III, freshly placed (0 seasons elapsed) -> no decay -> 1850.
    assert seed_from_past_season("EMERALD", "III", 0) == seed_from_rank("EMERALD", "III")
    assert seed_from_past_season("EMERALD", "III", 0) == 1850


def test_past_season_decays_per_season():
    """Each elapsed season knocks off DECAY_PER_SEASON, capped at MAX_DECAY."""
    from bot.services.elo import seed_from_past_season, DECAY_PER_SEASON, MAX_DECAY
    base = seed_from_rank("EMERALD", "III")  # 1850
    assert seed_from_past_season("EMERALD", "III", 1) == base - DECAY_PER_SEASON  # 1750
    assert seed_from_past_season("EMERALD", "III", 2) == base - 2 * DECAY_PER_SEASON  # 1650
    # Decay is capped: a Challenger from many seasons ago loses at most MAX_DECAY.
    assert seed_from_past_season("CHALLENGER", "I", 99) == seed_from_rank("CHALLENGER", "I") - MAX_DECAY


def test_past_season_floors_at_iron():
    """Decay can't drop a seed below IRON's value (800)."""
    from bot.services.elo import seed_from_past_season, TIER_SEED
    # Iron IV, many seasons ago -> stays at 800.
    assert seed_from_past_season("IRON", "IV", 10) == TIER_SEED["IRON"]
    # Bronze with full decay would go below Iron; clamp to Iron.
    assert seed_from_past_season("BRONZE", "IV", 99) == TIER_SEED["IRON"]


def test_past_season_unranked_returns_default():
    from bot.services.elo import seed_from_past_season
    assert seed_from_past_season(None, None, 0) == DEFAULT_ELO
    assert seed_from_past_season("MYTHIC", "I", 0) == DEFAULT_ELO


def test_past_season_case_insensitive():
    from bot.services.elo import seed_from_past_season
    assert seed_from_past_season("DIAMOND", "II", 1) == seed_from_past_season("diamond", "ii", 1)


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
    """vs equal opponent, expected = 0.5, actual 1.0, K=50 -> +25 elo."""
    from bot.services.elo import update_elo_series
    _, delta = update_elo_series(1500, 1500, 2, 0, games_played=20)
    assert delta == 25


def test_series_2_1_equal_peers():
    """vs equal opponent, expected = 0.5, actual 0.667, K=50 -> +8 elo."""
    from bot.services.elo import update_elo_series
    _, delta = update_elo_series(1500, 1500, 2, 1, games_played=20)
    assert delta == 8
