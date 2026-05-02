"""TrueSkill rating engine.

Each player has a per-role rating: (mu, sigma, games_played).
Initial mu is seeded from solo queue rank when /link is called; falls back to 25.0.
"""
from __future__ import annotations

from typing import Iterable, Optional

import trueskill

# Default TrueSkill env. mu=25, sigma=25/3 ≈ 8.333, beta=mu/6, tau=mu/300, draw_prob=0.
ENV = trueskill.TrueSkill(draw_probability=0.0)

# Tier -> mu seed. Roughly maps Iron→bottom, Challenger→top, with sigma reduced
# proportionally to confidence (higher rank = more games observed = lower sigma).
TIER_SEED = {
    "IRON":        (15.0, 8.0),
    "BRONZE":      (18.0, 7.5),
    "SILVER":      (22.0, 7.0),
    "GOLD":        (25.0, 6.5),
    "PLATINUM":    (28.0, 6.0),
    "EMERALD":     (31.0, 5.5),
    "DIAMOND":     (34.0, 5.0),
    "MASTER":      (37.0, 4.5),
    "GRANDMASTER": (39.0, 4.0),
    "CHALLENGER":  (40.0, 4.0),
}
# Within a tier, division I > II > III > IV. Add up to +1.5 mu for division I.
DIVISION_BONUS = {"I": 1.5, "II": 1.0, "III": 0.5, "IV": 0.0}


def seed_from_rank(tier: Optional[str], division: Optional[str]) -> tuple[float, float]:
    """Return (mu, sigma) seed for a player given their solo queue rank."""
    if not tier:
        return ENV.mu, ENV.sigma
    base_mu, sigma = TIER_SEED.get(tier.upper(), (ENV.mu, ENV.sigma))
    bonus = DIVISION_BONUS.get((division or "IV").upper(), 0.0)
    return base_mu + bonus, sigma


def rating_from_db(mu: float, sigma: float) -> trueskill.Rating:
    return ENV.create_rating(mu=mu, sigma=sigma)


def conservative_skill(mu: float, sigma: float) -> float:
    """Conservative skill estimate: mu - 3*sigma. Used for matchmaking & leaderboards."""
    return mu - 3 * sigma


def update_team_ratings(
    team1: list[trueskill.Rating],
    team2: list[trueskill.Rating],
    team1_won: bool,
) -> tuple[list[trueskill.Rating], list[trueskill.Rating]]:
    """Run a single 5v5 update. Returns new ratings in same order."""
    if team1_won:
        ranks = [0, 1]  # team1 finished 1st (lower = better in TrueSkill)
    else:
        ranks = [1, 0]
    new1, new2 = ENV.rate([team1, team2], ranks=ranks)
    return list(new1), list(new2)


def team_skill(ratings: Iterable[trueskill.Rating]) -> float:
    """Sum of conservative skill across team. Used to compare two teams' strength."""
    return sum(conservative_skill(r.mu, r.sigma) for r in ratings)
