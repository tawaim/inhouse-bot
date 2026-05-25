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
K_NEW = 60              # players with < K_THRESHOLD games (faster early convergence)
K_ESTABLISHED = 50
K_THRESHOLD = 10        # games-played boundary
# At these K's, a decisive series (2-0) vs an even opponent moves ±25 (established)
# / ±30 (new); a 2-1 moves ±8 / ±10 (less, since the series was closer).

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

# Rust/decay applied when seeding a currently-unranked player from a PAST season.
# Decay scales with how stale the placement is: DECAY_PER_SEASON elo per whole
# season elapsed since the placement, capped at MAX_DECAY (see seed_from_past_season).
DECAY_PER_SEASON = 100   # one division per season elapsed
MAX_DECAY = 400          # never knock off more than two full tiers


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


def seed_from_past_season(
    tier: Optional[str], division: Optional[str], seasons_elapsed: int = 0
) -> int:
    """Seed a currently-unranked player from their most recent PAST-SEASON
    Solo/Duo rank (sourced from OP.GG, since Riot's API has no history).

    Division-aware: starts from the exact ladder seed for the past tier+division
    (e.g. Emerald III = 1850), then applies recency decay of DECAY_PER_SEASON elo
    per whole season elapsed since the placement, capped at MAX_DECAY. Floored at
    IRON's seed value (800). Unknown/missing tier falls back to DEFAULT_ELO (1200).

    Example: Emerald III, 1 season ago -> 1850 - 100 = 1750.
    """
    if not tier or tier.upper() not in TIER_SEED:
        return DEFAULT_ELO
    base = seed_from_rank(tier, division)
    decay = min(DECAY_PER_SEASON * max(0, seasons_elapsed), MAX_DECAY)
    return max(TIER_SEED["IRON"], base - decay)


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


# A player's elo change is driven by the TEAM-vs-TEAM matchup (so teammates move
# by nearly the same amount), plus a small capped bias for how their own rating
# compares to their direct lane opponent's. The cap keeps teammates within ~10.
LANE_BIAS_K = 10     # mini-elo weight applied to the lane (role) matchup
LANE_BIAS_CAP = 5    # max elo the lane matchup can shift on top of the team result


def update_elo_team_series(
    team_avg: int,
    opp_team_avg: int,
    player_elo: int,
    lane_opponent_elo: int,
    player_team_wins: int,
    opponent_team_wins: int,
    games_played: int,
) -> tuple[int, int]:
    """Team-based series elo with a small lane-matchup bias.

    Bulk of the change is TEAM vs TEAM: K * (actual - expected(team_avg, opp_avg)),
    identical for everyone on a team. A small, capped term (±LANE_BIAS_CAP) nudges
    each player by how their own rating compares to their direct lane opponent, so
    holding a tough lane is rewarded a little. Net effect: teammates land within
    ~10 elo of each other instead of swinging wildly by individual rating.

    Returns (new_elo, delta).
    """
    total = player_team_wins + opponent_team_wins
    if total == 0:
        return player_elo, 0
    actual = player_team_wins / total
    k = k_factor(games_played)
    team_term = k * (actual - expected_score(team_avg, opp_team_avg))
    lane_term = LANE_BIAS_K * (actual - expected_score(player_elo, lane_opponent_elo))
    lane_term = max(-LANE_BIAS_CAP, min(LANE_BIAS_CAP, lane_term))
    delta = round(team_term + lane_term)
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
