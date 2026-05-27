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
     b. Pair the team-A seatings with the team-B seatings and keep the pair with
        the smallest skill gap. We try every seating rather than greedily
        slotting one in, so balance is optimized across all of them.
     c. If either team has no legal seating, skip this split.
  3. Rank the resulting (split, seating) proposals by balance |t1 - t2|.

There is deliberately no "off-role" cost. The role picker records the roles a
player is willing to play with no primary/secondary ordering, so every picked
role is equally fine — a player is never penalized for landing in one of their
own picks, and the hard legality rule means nobody is ever placed in a role they
didn't pick. role_penalty is therefore always 0 for a valid match and is kept
only for display/back-compat.

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
    role_penalty: float          # always 0 now (no off-role cost); kept for back-compat
    cost: float

    def to_json_dict(self) -> dict:
        return {
            "team1": self.team1.by_role,
            "team2": self.team2.by_role,
            "balance_diff": self.balance_diff,
            "role_penalty": self.role_penalty,
        }


def _legal_assignments(
    players: list[PlayerInput],
) -> list[tuple[TeamAssignment, float]]:
    """Every legal role seating for exactly 5 players, as (assignment, skill).

    A seating is legal iff each player is placed in a role they picked (a FILL
    player can take any role). Returns an empty list if the five can't cover
    five distinct roles (e.g. nobody picked SUPPORT and there's no FILL). No
    seating is preferred here — all picked roles are equal — so the caller is
    free to choose among them purely for team balance.
    """
    if len(players) != 5:
        return []

    out: list[tuple[TeamAssignment, float]] = []
    for perm in itertools.permutations(ROLES):
        # perm[i] is the role assigned to players[i]
        if not all(player.can_play(role) for player, role in zip(players, perm)):
            continue
        assignment = TeamAssignment(
            by_role={role: p.discord_id for p, role in zip(players, perm)}
        )
        skill = sum(p.skill_at(role) for p, role in zip(players, perm))
        out.append((assignment, skill))
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


def _best_balanced_seating(
    team1_players: list[PlayerInput],
    team2_players: list[PlayerInput],
) -> Optional[tuple[TeamAssignment, TeamAssignment, float]]:
    """Across every legal seating of both teams, the pair with the smallest skill
    gap. Returns (team1, team2, balance) or None if either team can't be legally
    seated. This is the "try all iterations" step — we never just slot the first
    valid seating, we pick the best-balanced one."""
    t1 = _legal_assignments(team1_players)
    t2 = _legal_assignments(team2_players)
    if not t1 or not t2:
        return None

    # Smallest |s1 - s2| over the two seating sets: sort both by skill and walk
    # them with two pointers (O(n log n) instead of the n*n cross product).
    t1.sort(key=lambda x: x[1])
    t2.sort(key=lambda x: x[1])
    i = j = 0
    best: Optional[tuple[TeamAssignment, TeamAssignment, float]] = None
    best_balance = float("inf")
    while i < len(t1) and j < len(t2):
        a1, s1 = t1[i]
        a2, s2 = t2[j]
        balance = abs(s1 - s2)
        if balance < best_balance:
            best_balance = balance
            best = (a1, a2, balance)
        if s1 < s2:
            i += 1
        else:
            j += 1
    return best


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
    be near-duplicates of each other). role_weight is accepted for back-compat
    but unused.
    """
    if len(players) != 10:
        raise ValueError(f"make_top_matches needs exactly 10 players, got {len(players)}")

    indices = list(range(10))

    # Collect the best-balanced legal proposal for every distinct team split.
    all_proposals: list[MatchProposal] = []
    for team1_other in itertools.combinations(indices[1:], 4):
        team1_idx = (0,) + team1_other
        team2_idx = tuple(i for i in indices if i not in team1_idx)

        team1_players = [players[i] for i in team1_idx]
        team2_players = [players[i] for i in team2_idx]

        seating = _best_balanced_seating(team1_players, team2_players)
        if seating is None:
            continue
        assignment1, assignment2, balance = seating

        all_proposals.append(MatchProposal(
            team1=assignment1,
            team2=assignment2,
            balance_diff=balance,
            role_penalty=0.0,  # everyone is in a role they picked, by construction
            cost=balance,
        ))

    all_proposals.sort(key=lambda p: p.balance_diff)

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
