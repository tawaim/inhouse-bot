"""Matchmaking: take signed-up players + role prefs, output two balanced teams.

Strategy
--------
With 10 players and 5 roles per team, the search space is small enough to
enumerate exhaustively in milliseconds:

  1. Filter to exactly 10 players (admin handles overflow before calling this).
  2. For every way to split players into Team A vs Team B (C(10,5) = 252 splits):
     a. Enumerate ALL legal role seatings for each team (full enumeration over
        5! = 120 permutations), where a seating is legal iff every player is in
        a role they picked (FILL counts as all roles).
     b. Pair every team-A seating against every team-B seating and keep the pair
        with the most even LANES — i.e. the smallest sum over the five roles of
        |team-A's player in that role - team-B's player in that role|, with
        overall team imbalance |t1 - t2| as a tiebreak. We try every seating
        rather than greedily slotting one in.
     c. If either team has no legal seating, skip this split.
  3. Rank the resulting (split, seating) proposals by lane disparity, then by
     team imbalance.

Priority order: (1) nobody off-role, (2) most even lanes, (3) most even teams.

There is deliberately no "off-role" cost. The role picker records the roles a
player is willing to play with no primary/secondary ordering, so every picked
role is equally fine — a player is never penalized for landing in one of their
own picks, and the hard legality rule means nobody is ever placed in a role they
didn't pick. role_penalty is therefore always 0 for a valid match and is kept
only for display/back-compat.

Lane disparity (rather than team-total balance) is the primary objective because
matching team totals can be gamed by trading skill across lanes — pairing a
smurf top with a feeder support to "balance" the sum — which produces lopsided
lanes. Minimizing per-lane gaps keeps each individual matchup fair.

Skill is per-role conservative TrueSkill (mu - 3*sigma). A player's skill
contribution depends on which role they're assigned to, since they have a
separate rating per role.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Optional

from bot.config import ROLES


@dataclass
class PlayerInput:
    discord_id: int
    preferred_roles: list[str]   # subset of ROLES, or ["FILL"] meaning all 5
    ratings: dict[str, int]      # role -> elo (chess-style, e.g. 1200)

    def can_play(self, role: str) -> bool:
        if "FILL" in self.preferred_roles:
            return True
        return role in self.preferred_roles

    def skill_at(self, role: str) -> float:
        return float(self.ratings.get(role, 1200))


@dataclass
class TeamAssignment:
    by_role: dict[str, int]  # role -> discord_id

    @property
    def player_ids(self) -> set[int]:
        return set(self.by_role.values())

    def total_skill(self, players_by_id: dict[int, PlayerInput]) -> float:
        return sum(
            players_by_id[pid].skill_at(role)
            for role, pid in self.by_role.items()
        )


@dataclass
class MatchProposal:
    team1: TeamAssignment
    team2: TeamAssignment
    balance_diff: float          # |team1_total - team2_total| (overall team imbalance)
    lane_diff: float             # sum over roles of |t1 lane skill - t2 lane skill|
    role_penalty: float          # always 0 now (no off-role cost); kept for back-compat
    cost: float                  # == lane_diff: the primary thing we minimize

    def to_json_dict(self) -> dict:
        return {
            "team1": self.team1.by_role,
            "team2": self.team2.by_role,
            "balance_diff": self.balance_diff,
            "lane_diff": self.lane_diff,
            "role_penalty": self.role_penalty,
        }


def _legal_assignments(
    players: list[PlayerInput],
) -> list[tuple[TeamAssignment, tuple[float, ...]]]:
    """Every legal role seating for exactly 5 players, as (assignment, skills).

    `skills` is the per-role skill vector in ROLES order (TOP, JUNGLE, MID, BOT,
    SUPPORT) so the caller can compare lane-vs-lane. A seating is legal iff each
    player is placed in a role they picked (a FILL player can take any role).
    Returns an empty list if the five can't cover five distinct roles (e.g.
    nobody picked SUPPORT and there's no FILL).
    """
    if len(players) != 5:
        return []

    out: list[tuple[TeamAssignment, tuple[float, ...]]] = []
    for perm in itertools.permutations(ROLES):
        # perm[i] is the role assigned to players[i]
        if not all(player.can_play(role) for player, role in zip(players, perm)):
            continue
        by_role = {role: p.discord_id for p, role in zip(players, perm)}
        role_skill = {role: p.skill_at(role) for p, role in zip(players, perm)}
        skills = tuple(role_skill[role] for role in ROLES)
        out.append((TeamAssignment(by_role=by_role), skills))
    return out


def make_match(
    players: list[PlayerInput],
    role_weight: float = 3.0,
) -> Optional[MatchProposal]:
    """Generate the optimally balanced match for exactly 10 players.

    role_weight is accepted for back-compat but no longer used: there is no
    off-role cost to weigh against balance (see module docstring).
    """
    proposals = make_top_matches(players, n=1, role_weight=role_weight, min_diff=0)
    return proposals[0] if proposals else None


def _best_lane_matchup(
    team1_players: list[PlayerInput],
    team2_players: list[PlayerInput],
) -> Optional[tuple[TeamAssignment, TeamAssignment, float, float]]:
    """Across every legal seating of both teams, the (team1, team2) pair with the
    most even lanes. Ranked by lane disparity first (sum over the 5 roles of the
    skill gap between the two players in that role), then by overall team
    imbalance as a tiebreak. Returns (team1, team2, lane_diff, team_diff) or None
    if either team can't be legally seated.

    This is the "try all iterations" step: every seating of one team is paired
    against every seating of the other so we find the genuinely best lane
    matchups rather than slotting people in.
    """
    t1 = _legal_assignments(team1_players)
    t2 = _legal_assignments(team2_players)
    if not t1 or not t2:
        return None

    best: Optional[tuple[TeamAssignment, TeamAssignment]] = None
    best_lane = float("inf")
    best_team = float("inf")
    for a1, v1 in t1:
        tot1 = v1[0] + v1[1] + v1[2] + v1[3] + v1[4]
        for a2, v2 in t2:
            lane = (
                abs(v1[0] - v2[0]) + abs(v1[1] - v2[1]) + abs(v1[2] - v2[2])
                + abs(v1[3] - v2[3]) + abs(v1[4] - v2[4])
            )
            if lane > best_lane:
                continue
            team = abs(tot1 - (v2[0] + v2[1] + v2[2] + v2[3] + v2[4]))
            if lane < best_lane or team < best_team:
                best_lane = lane
                best_team = team
                best = (a1, a2)

    assert best is not None
    return best[0], best[1], best_lane, best_team


def make_all_matches(players: list[PlayerInput]) -> list[MatchProposal]:
    """Every distinct team split that can be seated with nobody off-role, each
    with its most lane-even seating, sorted best-first (lane disparity, then team
    imbalance). One proposal per split — no diversity filtering, no top-N cap.
    """
    if len(players) != 10:
        raise ValueError(f"make_all_matches needs exactly 10 players, got {len(players)}")

    indices = list(range(10))
    all_proposals: list[MatchProposal] = []
    # Fixing player 0 on team1 enumerates each partition exactly once (the
    # complement is team2), so there are no mirror-image duplicates.
    for team1_other in itertools.combinations(indices[1:], 4):
        team1_idx = (0,) + team1_other
        team2_idx = tuple(i for i in indices if i not in team1_idx)

        team1_players = [players[i] for i in team1_idx]
        team2_players = [players[i] for i in team2_idx]

        matchup = _best_lane_matchup(team1_players, team2_players)
        if matchup is None:
            continue
        assignment1, assignment2, lane_diff, team_diff = matchup

        all_proposals.append(MatchProposal(
            team1=assignment1,
            team2=assignment2,
            balance_diff=team_diff,
            lane_diff=lane_diff,
            role_penalty=0.0,  # everyone is in a role they picked, by construction
            cost=lane_diff,
        ))

    # Priority: most even lanes first, then smallest overall team imbalance.
    all_proposals.sort(key=lambda p: (p.lane_diff, p.balance_diff))
    return all_proposals


def make_top_matches(
    players: list[PlayerInput],
    n: int = 3,
    role_weight: float = 3.0,
    min_diff: int = 2,
) -> list[MatchProposal]:
    """Generate the top-N matches (most even lanes first), with diversity
    enforcement.

    Diversity rule: each returned proposal must differ from every previously
    returned proposal by at least `min_diff` player-team-swaps. Two proposals
    differ by K swaps if K players are on different teams between them.

    Returns up to N proposals (may be fewer if the search space doesn't yield
    enough diverse-enough options).

    With min_diff=0, this just returns the N best proposals (which may be
    near-duplicates of each other). role_weight is accepted for back-compat
    but unused.
    """
    all_proposals = make_all_matches(players)  # validates count, sorted best-first

    # Greedy diversity filter: take the most-balanced option, then the next
    # option that differs by at least min_diff swaps from ALL already-chosen.
    selected: list[MatchProposal] = []
    for proposal in all_proposals:
        if len(selected) >= n:
            break
        if all(_team_swap_count(proposal, s) >= min_diff for s in selected):
            selected.append(proposal)

    return selected


def _team_swap_count(a: MatchProposal, b: MatchProposal) -> int:
    """How many players are on a different team in proposal `a` vs `b`?

    Note: team identity (1 vs 2) is arbitrary — being on "team 1 in A" and
    "team 2 in B" is only a swap if they're with a different group of teammates.
    We canonicalize by comparing team membership sets directly.
    """
    a_team1 = a.team1.player_ids
    b_team1 = b.team1.player_ids
    b_team2 = b.team2.player_ids
    # Match each of A's teams to whichever of B's teams overlaps more
    if len(a_team1 & b_team1) >= len(a_team1 & b_team2):
        # A's team1 ~ B's team1
        return len(a_team1 - b_team1) + len(a.team2.player_ids - b_team2)
    else:
        # A's team1 ~ B's team2
        return len(a_team1 - b_team2) + len(a.team2.player_ids - b_team1)
