"""Tests for elo / TrueSkill seeding."""
from bot.services.elo import seed_from_rank, conservative_skill, ENV


def test_unranked_returns_default():
    mu, sigma = seed_from_rank(None, None)
    assert mu == ENV.mu
    assert sigma == ENV.sigma


def test_higher_tier_higher_mu():
    iron_mu, _ = seed_from_rank("IRON", "IV")
    challenger_mu, _ = seed_from_rank("CHALLENGER", "I")
    assert challenger_mu > iron_mu


def test_division_bonus_within_tier():
    gold_iv_mu, _ = seed_from_rank("GOLD", "IV")
    gold_i_mu, _ = seed_from_rank("GOLD", "I")
    assert gold_i_mu > gold_iv_mu


def test_higher_tier_lower_sigma():
    """Higher-rank players have more games observed -> we're more confident."""
    _, iron_sigma = seed_from_rank("IRON", "IV")
    _, challenger_sigma = seed_from_rank("CHALLENGER", "I")
    assert challenger_sigma < iron_sigma


def test_case_insensitive_tier():
    upper, _ = seed_from_rank("PLATINUM", "II")
    lower, _ = seed_from_rank("platinum", "ii")
    assert upper == lower


def test_conservative_skill_formula():
    assert conservative_skill(25.0, 8.333) == pytest.approx(25.0 - 3 * 8.333)


import pytest
