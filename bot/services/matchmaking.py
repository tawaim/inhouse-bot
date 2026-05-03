"""Matchmaking: take signed-up players + role prefs, output two balanced teams.

Strategy
--------
With 10 players and 5 roles per team, the search space is small enough to
enumerate exhaustively in milliseconds:

  1. Filter to exactly 10 players (admin handles overflow before calling this).
  2. For every way to split players into Team A vs Team B (C(10,5) = 252 splits):
     a. For each team, find the best role assignment (Hungarian algorithm or
        full enumeration over 5! = 120 permutations).
     b. A player can only be assigned a role they listed in their prefs
        (FILL counts as all roles).
     c. If no valid assignment exists for either team, skip this split.
  3. Score each valid (split, assignment) pair by:
        balance_score = |team1_skill - team2_skill|     (lower is better)
        role_penalty  = sum over players of (0 if got primary role else penalty)
        total_cost    = balance_score + role_weight * role_penalty
  4. Return the lowest-cost solution.

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
    balance_diff: float          # |team1_skill - team2_skill|
    role_penalty: float          # how many players got non-primary roles
    cost: float

    def to_json_dict(self) -> dict:
        return {
            "team1": self.team1.by_role,
            "team2": self.team2.by_role,
            "balance_diff": self.balance_diff,
            "role_penalty": self.role_penalty,
        }


def _best_role_assignment(
    players: list[PlayerInput],
) -> Optional[tuple[TeamAssignment, float]]:
    """Given exactly 5 players, find the best legal role assignment.
    Returns (assignment, role_penalty) or None if no legal assignment exists.

    role_penalty counts how many players are NOT in their first listed
    preferred role. FILL players cost 0.5 (they're flexible by design but
    we still slightly prefer specialists in their roles).
    """
    if len(players) != 5:
        return None

    best: Optional[TeamAssignment] = None
    best_penalty = float("inf")
    best_skill = 0.0  # tiebreak: prefer higher-skill assignments (sanity)

    for perm in itertools.permutations(ROLES):
        # perm[i] is the role assigned to players[i]
        legal = True
        penalty = 0.0
        for player, role in zip(players, perm):
            if not player.can_play(role):
                legal = False
                break
            if "FILL" in player.preferred_roles:
                penalty += 0.5
            elif role != player.preferred_roles[0]:
                penalty += 1.0
        if not legal:
            continue
        assignment = TeamAssignment(by_role={role: p.discord_id for p, role in zip(players, perm)})
        # Lower penalty wins; tiebreak by total skill
        if penalty < best_penalty:
            best_penalty = penalty
            best = assignment
            best_skill = sum(p.skill_at(r) for p, r in zip(players, perm))

    if best is None:
        return None
    return best, best_penalty


def make_match(
    players: list[PlayerInput],
    role_weight: float = 3.0,
) -> Optional[MatchProposal]:
    """Generate the optimally balanced match for exactly 10 players.

    role_weight: how much one mis-assigned role costs in skill-points.
                 Higher = prioritize getting people their preferred roles
                 even at the cost of team balance.
    """
    proposals = make_top_matches(players, n=1, role_weight=role_weight, min_diff=0)
    return proposals[0] if proposals else None


def make_top_matches(
    players: list[PlayerInput],
    n: int = 3,
    role_weight: float = 3.0,
    min_diff: int = 2,
) -> list[MatchProposal]:
    """Generate the top-N most-balanced matches, with diversity enforcement.

    Diversity rule: each returned proposal must differ from every previously
    returned proposal by at least `min_diff` player-team-swaps. Two proposals
    differ by K swaps if K players are on different teams between them.

    Returns up to N proposals (may be fewer if the search space doesn't yield
    enough diverse-enough options).

    With min_diff=0, this just returns the N lowest-cost proposals (which may
    be near-duplicates of each other).
    """
    if len(players) != 10:
        raise ValueError(f"make_top_matches needs exactly 10 players, got {len(players)}")

    players_by_id = {p.discord_id: p for p in players}
    indices = list(range(10))

    # Collect every legal proposal first, sorted by cost
    all_proposals: list[MatchProposal] = []
    for team1_other in itertools.combinations(indices[1:], 4):
        team1_idx = (0,) + team1_other
        team2_idx = tuple(i for i in indices if i not in team1_idx)

        team1_players = [players[i] for i in team1_idx]
        team2_players = [players[i] for i in team2_idx]

        a1 = _best_role_assignment(team1_players)
        a2 = _best_role_assignment(team2_players)
        if a1 is None or a2 is None:
            continue

        assignment1, pen1 = a1
        assignment2, pen2 = a2

        skill1 = assignment1.total_skill(players_by_id)
        skill2 = assignment2.total_skill(players_by_id)
        balance = abs(skill1 - skill2)
        total_penalty = pen1 + pen2
        cost = balance + role_weight * total_penalty

        all_proposals.append(MatchProposal(
            team1=assignment1,
            team2=assignment2,
            balance_diff=balance,
            role_penalty=total_penalty,
            cost=cost,
        ))

    all_proposals.sort(key=lambda p: p.cost)

    # Greedy diversity filter: take the lowest-cost option, then the next
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
