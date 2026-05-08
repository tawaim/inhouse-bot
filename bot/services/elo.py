"""Chess-style Elo rating system.

Rules
-----
- Default starting elo: 1200 (chess convention)
- K-factor: 40 for first 10 games, 20 thereafter (rapid initial convergence,
  then stable)
- Opponent rating: the OPPOSING TEAM'S AVERAGE elo (so each player's elo
  change depends on the team-strength matchup, not just their own role's opponent)
- Per-role tracking AND an "INHOUSE" rating that updates every game regardless
  of role played

Each match updates TWO rows for each of the 10 players:
  - The role-specific rating (TOP/JUNGLE/MID/BOT/SUPPORT)
  - The INHOUSE rating

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

INHOUSE_ROLE = "INHOUSE"

# Linear ladder. Each tier is 200 wide and divided into 4 divisions of 50
# elo each (IV = bottom, III, II, I = top). Master/GM/Challenger are flat
# (no divisions in League), so they sit at the bottom of their tier and the
# top of GM is the start of Challenger, etc.
#
# Bottom of each tier (i.e., "Tier IV"):
TIER_SEED = {
    "IRON":         800,
    "BRONZE":      1000,
    "SILVER":      1200,
    "GOLD":        1400,
    "PLATINUM":    1600,
    "EMERALD":     1800,
    "DIAMOND":     2000,
    "MASTER":      2200,
    "GRANDMASTER": 2300,
    "CHALLENGER":  2500,
}
# 50 elo per division. IV = bottom (0), I = top (150).
# Plat IV=1600, Plat III=1650, Plat II=1700, Plat I=1750, Emerald IV=1800.
DIVISION_BONUS = {"I": 150, "II": 100, "III": 50, "IV": 0}

# Master/GM/Challenger don't have divisions; treat them as flat-tier seeds.
_FLAT_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}

# How much elo we knock off when seeding from last-season (rust penalty).
# Equivalent to one full tier (4 divisions = 200 elo).
RUST_PENALTY = 200


def seed_from_rank(tier: Optional[str], division: Optional[str]) -> int:
    """Return starting elo for a player given their solo queue rank.
    Returns DEFAULT_ELO (1200) for unranked players.
    """
    if not tier:
        return DEFAULT_ELO
    tier = tier.upper()
    base = TIER_SEED.get(tier)
    if base is None:
        return DEFAULT_ELO
    if tier in _FLAT_TIERS:
        # Master/GM/Challenger are flat — no division offset
        return base
    return base + DIVISION_BONUS.get((division or "IV").upper(), 0)


def seed_from_historical_rank(tier: Optional[str]) -> int:
    """Seed from last-season's `highestTierAchieved` field, applying a
    fixed -200 elo rust penalty (one full tier worth of rust).

    No division info is available on `highestTierAchieved`, so we treat
    it as bottom-of-tier (IV-equivalent) before applying the penalty.
    Floored at IRON's seed value — can't go lower than 800.
    """
    if not tier:
        return DEFAULT_ELO
    tier = tier.upper()
    base = TIER_SEED.get(tier)
    if base is None:
        return DEFAULT_ELO
    return max(TIER_SEED["IRON"], base - RUST_PENALTY)


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


def update_elo_series(
    player_elo: int,
    opponent_elo: int,
    player_team_wins: int,
    opponent_team_wins: int,
    games_played: int,
) -> tuple[int, int]:
    """Compute one player's new elo after a SERIES.

    Score is wins / total_games (e.g., 2-0 = 1.0, 2-1 = 0.667, 1-2 = 0.333,
    0-2 = 0.0). Standard chess elo update against expected score, scaled by
    the player's K factor. K is NOT multiplied by series length — a series
    is treated as one rating event regardless of whether it went 2-0 or 2-1.

    Returns (new_elo, delta) where delta is the signed change.
    """
    total = player_team_wins + opponent_team_wins
    if total == 0:
        return player_elo, 0  # no games played, no update
    expected = expected_score(player_elo, opponent_elo)
    actual = player_team_wins / total
    k = k_factor(games_played)
    delta = round(k * (actual - expected))
    return player_elo + delta, delta


def parse_series_score(score: str) -> tuple[int, int]:
    """Parse '2-0', '2-1', '1-2', '0-2' (or '2:0' etc.) into (team1_wins, team2_wins).
    Returns (-1, -1) on invalid input.
    """
    cleaned = score.strip().replace(":", "-").replace(" ", "")
    parts = cleaned.split("-")
    if len(parts) != 2:
        return (-1, -1)
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return (-1, -1)
    # Allow any best-of-N up to 5; reject negatives and ties
    if a < 0 or b < 0 or a == b:
        return (-1, -1)
    if max(a, b) > 5:
        return (-1, -1)
    return (a, b)


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
