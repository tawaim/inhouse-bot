"""Turn an OCR'd scoreboard into a proposed per-game commit plan.

Pure logic — no Discord, no DB. Given one game's ParsedScoreboard and a callback
that resolves a scoreboard name to a discord_id, produce a GameProposal the admin
confirms in /report. Team alignment, role assignment, and unmatched-flagging live
here so they're unit-testable; the cog supplies the resolver (alias index) and the
interactive corrections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from bot.config import ROLES  # ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]
from bot.services.ocr import ParsedScoreboard


@dataclass
class GameResult:
    """One game's ACTUAL lineup + outcome — the unit of per-game elo and stats.

    Subs are represented naturally: whoever actually played a role this game is the
    discord_id for that role. Consumed by AdminCog._commit_games.
    """
    winner: int                                          # 1 or 2
    team1: dict[str, int]                                # role -> discord_id
    team2: dict[str, int]                                # role -> discord_id
    champions: dict[int, Optional[str]] = field(default_factory=dict)  # discord_id -> champ
    kdas: dict[int, tuple] = field(default_factory=dict)              # discord_id -> (k,d,a)


@dataclass
class SlotProposal:
    """One resolved roster slot in a game (aligned to the MATCH's team numbers)."""
    team: int                       # 1 or 2 (match team, not screenshot order)
    role: str
    name_guess: str
    discord_id: Optional[int] = None  # None -> needs the ephemeral picker
    champion: Optional[str] = None
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None


@dataclass
class GameProposal:
    slots: list[SlotProposal] = field(default_factory=list)  # up to 10
    winner_hint: Optional[int] = None   # 1/2 aligned to match teams, or None
    notes: list[str] = field(default_factory=list)

    def unresolved(self) -> list[SlotProposal]:
        return [s for s in self.slots if s.discord_id is None]

    def team(self, n: int) -> list[SlotProposal]:
        return [s for s in self.slots if s.team == n]


def _resolve_name(name_guess: str, resolve) -> tuple[Optional[int], str]:
    """Resolve a noisy OCR name. OCR often appends item/stat garbage as extra
    'words' ("Alpha Reeune"), and real names can be two words ("Zack Fox"). So try
    the full guess, then progressively shorter leading prefixes, and take the first
    that resolves. Returns (discord_id, display_name); display falls back to the
    first word when nothing resolves (clean enough for the picker)."""
    words = name_guess.split()
    for k in range(len(words), 0, -1):
        cand = " ".join(words[:k])
        did = resolve(cand)
        if did is not None:
            return did, cand
    return None, (words[0] if words else "")


def _rows_to_slots(rows, resolve):
    """Map up to 5 scoreboard rows to (role, display_name, discord_id, row) in role
    order (the client lists TOP→SUPPORT)."""
    out = []
    for i, role in enumerate(ROLES):
        if i < len(rows):
            did, name = _resolve_name(rows[i].name_guess, resolve)
            out.append((role, name, did, rows[i]))
        else:
            out.append((role, "", None, None))
    return out


def _overlap(ids: list[Optional[int]], fixed: dict[str, int]) -> int:
    fixedset = set(fixed.values())
    return sum(1 for i in ids if i is not None and i in fixedset)


def build_game_proposal(
    parsed: ParsedScoreboard,
    resolve: Callable[[str], Optional[int]],
    fixed_team1: dict[str, int],
    fixed_team2: dict[str, int],
) -> GameProposal:
    """Build a GameProposal from one parsed scoreboard.

    - Resolves each row's name to a discord_id via `resolve` (None = needs picker).
    - Aligns the two SCREENSHOT teams to the MATCH's team1/team2 by membership
      overlap with the fixed roster (so subs, who are in neither, don't flip it).
    - Assigns roles by row order. Maps the VICTORY/DEFEAT banner to a winner hint
      (best-effort — the banner is the screenshotter's POV, so the admin confirms).
    """
    prop = GameProposal(notes=list(parsed.notes))
    sb1 = _rows_to_slots(parsed.team1, resolve)
    sb2 = _rows_to_slots(parsed.team2, resolve)
    ids1 = [did for _, _, did, _ in sb1]
    ids2 = [did for _, _, did, _ in sb2]

    # Which match-team does SCREENSHOT team 1 correspond to? Pick the alignment
    # that maximizes roster overlap; ties default to identity (sb1 -> match team1).
    identity = _overlap(ids1, fixed_team1) + _overlap(ids2, fixed_team2)
    swapped = _overlap(ids1, fixed_team2) + _overlap(ids2, fixed_team1)
    sb1_is_match_team1 = identity >= swapped

    for sb, match_team in ((sb1, 1 if sb1_is_match_team1 else 2),
                           (sb2, 2 if sb1_is_match_team1 else 1)):
        for role, name, did, row in sb:
            prop.slots.append(SlotProposal(
                team=match_team, role=role, name_guess=name, discord_id=did,
                kills=getattr(row, "kills", None),
                deaths=getattr(row, "deaths", None),
                assists=getattr(row, "assists", None),
            ))

    # Banner -> winner hint, aligned to match teams. VICTORY means screenshot
    # team 1 won; map that through the alignment. Unknown banner -> no hint.
    if parsed.banner == "VICTORY":
        prop.winner_hint = 1 if sb1_is_match_team1 else 2
    elif parsed.banner == "DEFEAT":
        prop.winner_hint = 2 if sb1_is_match_team1 else 1

    prop.slots.sort(key=lambda s: (s.team, ROLES.index(s.role)))
    return prop


@dataclass
class ReportState:
    """Mutable working state for one /report, driven by the confirm View. Pure
    (no Discord) so the state transitions and commit-building are unit-testable."""
    match_id: int
    games: list[GameProposal]
    winners: list[int]  # winner team (1/2) per game, in game order

    @classmethod
    def from_proposals(cls, match_id: int, games: list[GameProposal]) -> "ReportState":
        # Default each winner to its hint, else team 1 (admin toggles to fix).
        winners = [g.winner_hint or 1 for g in games]
        return cls(match_id=match_id, games=games, winners=winners)

    def _slot(self, gi: int, team: int, role: str) -> Optional[SlotProposal]:
        for s in self.games[gi].slots:
            if s.team == team and s.role == role:
                return s
        return None

    def set_winner(self, gi: int, team: int) -> None:
        self.winners[gi] = team

    def toggle_winner(self, gi: int) -> None:
        self.winners[gi] = 2 if self.winners[gi] == 1 else 1

    def set_player(self, gi: int, team: int, role: str, discord_id: int) -> None:
        s = self._slot(gi, team, role)
        if s:
            s.discord_id = discord_id

    def set_champion(self, gi: int, team: int, role: str, champion: str) -> None:
        s = self._slot(gi, team, role)
        if s:
            s.champion = champion

    def unresolved_slots(self) -> list[tuple[int, SlotProposal]]:
        """(game_index, slot) for every slot still missing a player."""
        return [(gi, s) for gi, g in enumerate(self.games)
                for s in g.slots if s.discord_id is None]

    def ready(self) -> tuple[bool, list[str]]:
        """Whether the report can commit: every game has 10 resolved players, 5
        distinct roles per team, and no player on both teams. Returns (ok, problems)."""
        problems: list[str] = []
        for gi, g in enumerate(self.games, start=1):
            ids = [s.discord_id for s in g.slots]
            if any(i is None for i in ids):
                problems.append(f"Game {gi}: unresolved player(s)")
                continue
            t1 = [s.discord_id for s in g.slots if s.team == 1]
            t2 = [s.discord_id for s in g.slots if s.team == 2]
            if len(t1) != 5 or len(t2) != 5:
                problems.append(f"Game {gi}: need 5 players per team")
            if set(t1) & set(t2):
                problems.append(f"Game {gi}: a player is on both teams")
        return (not problems, problems)

    def to_game_results(self) -> list[GameResult]:
        """Build the per-game commit input. Assumes ready() is true."""
        out: list[GameResult] = []
        for g, winner in zip(self.games, self.winners):
            t1: dict[str, int] = {}
            t2: dict[str, int] = {}
            champs: dict[int, Optional[str]] = {}
            kdas: dict[int, tuple] = {}
            for s in g.slots:
                if s.discord_id is None:
                    continue
                (t1 if s.team == 1 else t2)[s.role] = s.discord_id
                if s.champion:
                    champs[s.discord_id] = s.champion
                if s.kills is not None:
                    kdas[s.discord_id] = (s.kills, s.deaths, s.assists)
            out.append(GameResult(winner=winner, team1=t1, team2=t2,
                                  champions=champs, kdas=kdas))
        return out
