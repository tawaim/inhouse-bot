"""Tests for champion name resolution (bot/services/champions.py)."""
from bot.services.champions import (
    CHAMPIONS,
    normalize,
    resolve_champion,
    suggest_champions,
)


def test_exact_canonical_names_all_resolve():
    """Every canonical name resolves to itself."""
    for champ in CHAMPIONS:
        assert resolve_champion(champ) == champ


def test_normalize_collapses_punctuation_and_case():
    assert normalize("Kai'Sa") == "kaisa"
    assert normalize("Dr. Mundo") == "drmundo"
    assert normalize("Nunu & Willump") == "nunuwillump"
    assert normalize("  LeBlanc ") == "leblanc"


def test_punctuation_and_spacing_variants_resolve():
    assert resolve_champion("kaisa") == "Kai'Sa"
    assert resolve_champion("KAI SA") == "Kai'Sa"
    assert resolve_champion("dr mundo") == "Dr. Mundo"
    assert resolve_champion("chogath") == "Cho'Gath"
    assert resolve_champion("leesin") == "Lee Sin"


def test_community_shorthands_resolve():
    assert resolve_champion("mf") == "Miss Fortune"
    assert resolve_champion("ww") == "Warwick"
    assert resolve_champion("j4") == "Jarvan IV"
    assert resolve_champion("tf") == "Twisted Fate"
    assert resolve_champion("asol") == "Aurelion Sol"
    assert resolve_champion("lb") == "LeBlanc"


def test_fuzzy_typos_resolve():
    assert resolve_champion("leblnc") == "LeBlanc"
    assert resolve_champion("kayle") == "Kayle"
    assert resolve_champion("tristana") == "Tristana"


def test_unknown_returns_none():
    assert resolve_champion("zzzznotachamp") is None
    assert resolve_champion("") is None
    assert resolve_champion("   ") is None


def test_suggest_prefix_first():
    out = suggest_champions("ka", n=5)
    # All suggestions start with 'ka' when enough exist (Kaisa, Kalista, Karma,
    # Karthus, Kassadin, Katarina...).
    assert "Kai'Sa" in out
    assert all(normalize(c).startswith("ka") for c in out)


def test_suggest_handles_empty():
    out = suggest_champions("", n=3)
    assert len(out) == 3
