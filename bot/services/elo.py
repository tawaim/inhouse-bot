"""Chess-style Elo rating system.

Rules
-----
- Default starting elo: 1200 (chess convention)
- K-factor: 40 for first 10 games, 20 thereafter (rapid initial convergence,
  then stable)
- Opponent rating: the OPPOSING TEAM'S AVERAGE elo (so each player's elo
  change depends on the team-strength matchup, not just their own role's opponent)
- Per-role tracking AND an "OVERALL" rating that updates every game regardless
  of role played

Each match updates TWO rows for each of the 10 players:
  - The role-specific rating (TOP/JUNGLE/MID/BOT/SUPPORT)
  - The OVERALL rating

Both updates use the same expected/actual formula but the K-factor and
opponent rating are computed separately for each scope (role vs. overall),
which keeps the math sane and prevents one rating from polluting the other.
"""
from __future__ import annotations

from typing import Optional

# ---------- Tuning constants ----------

DEFAULT_ELO = 1200
K_NEW = 40              # players with < K_THRESHOLD games
K_ESTABLISHED = 20
K_THRESHOLD = 10        # games-played boundary

OVERALL_ROLE = "OVERALL"

# Tier -> elo seed. ~200 between tiers (chess convention: 200 pts ≈ 75% expected
# win rate). Plus a small division-based bonus (I > IV).
TIER_SEED = {
    "IRON":         800,
    "BRONZE":      1000,
    "SILVER":      1200,
    "GOLD":        1400,
    "PLATINUM":    1600,
    "EMERALD":     1800,
    "DIAMOND":     2000,
    "MASTER":      2200,
    "GRANDMASTER": 2400,
    "CHALLENGER":  2600,
}
DIVISION_BONUS = {"I": 75, "II": 50, "III": 25, "IV": 0}


def seed_from_rank(tier: Optional[str], division: Optional[str]) -> int:
    """Return starting elo for a player given their solo queue rank.
    Returns DEFAULT_ELO (1200) for unranked players.
    """
    if not tier:
        return DEFAULT_ELO
    base = TIER_SEED.get(tier.upper())
    if base is None:
        return DEFAULT_ELO
    return base + DIVISION_BONUS.get((division or "IV").upper(), 0)


def expected_score(player_elo: int, opponent_elo: int) -> float:
    """Standard chess Elo expected-score formula. Returns probability the
    player beats the opponent, between 0 and 1.
    """
    return 1.0 / (1.0 + 10 ** ((opponent_elo - player_elo) / 400.0))


def k_factor(games_played: int) -> int:
    return K_NEW if games_played < K_THRESHOLD else K_ESTABLISHED


def update_elo(
    player_elo: int,
    opponent_elo: int,
    won: bool,
    games_played: int,
) -> tuple[int, int]:
    """Compute one player's new elo after a match.
    Returns (new_elo, delta) where delta is the signed change.
    """
    expected = expected_score(player_elo, opponent_elo)
    actual = 1.0 if won else 0.0
    k = k_factor(games_played)
    delta = round(k * (actual - expected))
    return player_elo + delta, delta


def average_elo(elos: list[int]) -> int:
    if not elos:
        return DEFAULT_ELO
    return round(sum(elos) / len(elos))


# ---------- Compatibility shim ----------
# The matchmaker used to use `conservative_skill(mu, sigma)` for ranking.
# With chess elo, "skill" IS the elo number, no uncertainty discount.
def conservative_skill(elo: int, _unused_sigma: float = 0.0) -> float:
    """Kept for backwards compatibility with matchmaking code that calls it.
    With chess elo there's no separate confidence factor — the elo IS the skill.
    """
    return float(elo)
