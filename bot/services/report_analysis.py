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
from bot.services.elo import VALID_SERIES_SCORES
from bot.services.ocr import ParsedScoreboard

# Orientation (which screenshot team is which match team) is trusted only when at
# least this many of the 10 names landed on the roster AND one mapping beats the
# other by a clear margin. Below that we don't guess.
MIN_ORIENT_HITS = 4
MIN_ORIENT_MARGIN = 2


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
    """One roster slot in a game (aligned to the MATCH's team numbers).

    Roster-first: `discord_id` defaults to the rostered player (`rostered_id`) for
    this role; it differs only when OCR detected a sub or the admin overrode it.
    """
    team: int                       # 1 or 2 (match team, not screenshot order)
    role: str
    name_guess: str                 # the name read off the screenshot for this slot
    discord_id: Optional[int] = None    # who gets credit (rostered unless subbed)
    rostered_id: Optional[int] = None   # the match's default player for this slot
    champion: Optional[str] = None
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None
    # True when `name_guess` matched a known player. Only an UNRESOLVED read name
    # may be learned as an alias when the admin picks the slot's player.
    read_resolved: bool = False

    @property
    def is_sub(self) -> bool:
        return self.discord_id is not None and self.discord_id != self.rostered_id


@dataclass
class GameProposal:
    slots: list[SlotProposal] = field(default_factory=list)  # up to 10
    winner_hint: Optional[int] = None   # 1/2 aligned to match teams, or None
    notes: list[str] = field(default_factory=list)
    read_failed: bool = False           # the screenshot yielded no players at all
    # True when name overlap couldn't tell which screenshot team is which match
    # team. No winner is hinted in that case; the admin checks and may swap.
    orientation_uncertain: bool = False
    sb1_is_match_team1: bool = True     # the orientation this proposal was built with
    # What build_game_proposal was called with, so swapped() can rebuild.
    _source: Optional[tuple] = field(default=None, repr=False, compare=False)

    def unresolved(self) -> list[SlotProposal]:
        return [s for s in self.slots if s.discord_id is None]

    def team(self, n: int) -> list[SlotProposal]:
        return [s for s in self.slots if s.team == n]

    def swapped(self) -> Optional["GameProposal"]:
        """This game rebuilt with the screenshot's teams mapped the other way
        round (the admin's call, so the result is no longer 'uncertain'). None if
        the proposal wasn't built from a screenshot."""
        if self._source is None:
            return None
        parsed, resolve, fixed1, fixed2 = self._source
        return build_game_proposal(parsed, resolve, fixed1, fixed2,
                                   sb1_is_match_team1=not self.sb1_is_match_team1)


def _resolve_name(name_guess: str, resolve) -> tuple[Optional[int], str]:
    """Resolve a name read off a screenshot. Returns (discord_id, name): the
    matched text when it resolves, else (None, the full name as read).

    The full name is tried first. A read can carry trailing junk ("Zack Fox 12"),
    so shorter leading prefixes are tried too — but never down to a single word:
    one leading word ("Zack" out of "Zack Fox") is somebody else's name far too
    often, and a wrong match silently credits the wrong player. A single-word name
    still resolves, as the full name.
    """
    words = name_guess.split()
    if not words:
        return None, ""
    full = " ".join(words)
    did = resolve(full)
    if did is not None:
        return did, full
    for k in range(len(words) - 1, 1, -1):
        cand = " ".join(words[:k])
        did = resolve(cand)
        if did is not None:
            return did, cand
    return None, full


def _resolve_rows(rows, resolve):
    """[(row, discord_id | None, name)] for up to 5 rows, in display order."""
    out = []
    for row in rows[:5]:
        did, name = _resolve_name(row.name_guess, resolve)
        out.append((row, did, name))
    return out


def _overlap(ids: list[Optional[int]], fixed: dict[str, int]) -> int:
    fixedset = set(fixed.values())
    return sum(1 for i in ids if i is not None and i in fixedset)


def _attach(slot: SlotProposal, row, name: str, resolved: bool) -> None:
    slot.name_guess = name
    slot.read_resolved = resolved
    slot.champion = getattr(row, "champion", None)
    slot.kills = getattr(row, "kills", None)
    slot.deaths = getattr(row, "deaths", None)
    slot.assists = getattr(row, "assists", None)


def build_game_proposal(
    parsed: ParsedScoreboard,
    resolve: Callable[[str], Optional[int]],
    fixed_team1: dict[str, int],
    fixed_team2: dict[str, int],
    sb1_is_match_team1: Optional[bool] = None,
) -> GameProposal:
    """Build a GameProposal ROSTER-FIRST.

    Every slot defaults to the match's rostered player for its role. The
    screenshot is used only to:
      - attach champion/KDA to the player it names. A player who is ON the match
        roster keeps their ROSTERED team and role whichever row they appear in —
        row position is not role (the match-history screen lists the viewer first);
      - flag SUBS: a name that resolves to a known player who is on NEITHER
        roster takes the slot of a rostered player missing from that screenshot
        team;
      - hint the winner (parsed.winner), mapped onto the match's team numbers.

    Mapping screenshot teams to match teams ("orientation") needs enough names to
    land on the roster. When it can't be told apart it is NOT guessed: no winner
    is hinted, a note says so, and the admin can swap (GameProposal.swapped).
    Pass sb1_is_match_team1 to force the orientation.

    An unreadable screenshot still yields the full lineup from the roster — but
    with read_failed set and no winner, so it can't be committed blind.
    """
    prop = GameProposal(notes=list(parsed.notes), read_failed=parsed.unreadable)
    prop._source = (parsed, resolve, fixed_team1, fixed_team2)
    sb = {1: _resolve_rows(parsed.team1, resolve), 2: _resolve_rows(parsed.team2, resolve)}
    ids1 = [did for _, did, _ in sb[1]]
    ids2 = [did for _, did, _ in sb[2]]

    # ---- Orientation: which match team is SCREENSHOT team 1?
    identity = _overlap(ids1, fixed_team1) + _overlap(ids2, fixed_team2)
    swapped = _overlap(ids1, fixed_team2) + _overlap(ids2, fixed_team1)
    if sb1_is_match_team1 is not None:
        orient, certain = sb1_is_match_team1, True
    else:
        orient = identity >= swapped
        certain = (identity + swapped >= MIN_ORIENT_HITS
                   and abs(identity - swapped) >= MIN_ORIENT_MARGIN)
    prop.sb1_is_match_team1 = orient
    match_team_of = {1: 1 if orient else 2, 2: 2 if orient else 1}

    # ---- Slots, pre-filled from the roster.
    fixed_for = {1: fixed_team1, 2: fixed_team2}
    slots: dict[tuple[int, str], SlotProposal] = {}
    roster_pos: dict[int, tuple[int, str]] = {}
    for team in (1, 2):
        for role in ROLES:
            rostered = fixed_for[team].get(role)
            slots[(team, role)] = SlotProposal(
                team=team, role=role, name_guess="",
                discord_id=rostered, rostered_id=rostered)
            if rostered is not None:
                roster_pos.setdefault(rostered, (team, role))

    # ---- Rostered players: stats go to THEIR slot, wherever they were listed.
    seen: set[int] = set()
    leftovers: dict[int, list] = {1: [], 2: []}   # match team -> rows still to place
    crossed = 0
    for sb_team in (1, 2):
        mteam = match_team_of[sb_team]
        for row, did, name in sb[sb_team]:
            if did is not None and did in seen:
                # Two names resolved to one player (a bad alias?). Don't credit
                # them twice — treat the second as unrecognised.
                prop.notes.append(
                    f"“{name}” matched a player who is already in this game — "
                    "check that slot (an alias may be wrong).")
                leftovers[mteam].append((row, None, name))
            elif did is not None and did in roster_pos:
                seen.add(did)
                _attach(slots[roster_pos[did]], row, name, True)
                if roster_pos[did][0] != mteam:
                    crossed += 1
            else:
                if did is not None:
                    seen.add(did)
                leftovers[mteam].append((row, did, name))
    if crossed and certain:
        prop.notes.append(
            f"{crossed} rostered player(s) showed up on the other side in the screenshot — "
            "they were kept on their rostered team; fix the slot if they really switched.")

    # ---- Everyone else fills the slots whose rostered player wasn't seen, in
    # screen order. A known player there is a sub; an unrecognised name leaves the
    # rostered player in place (names change — failing to resolve is normal).
    for team in (1, 2):
        open_slots = [slots[(team, role)] for role in ROLES
                      if slots[(team, role)].rostered_id not in seen]
        rows = leftovers[team]
        for (row, did, name), slot in zip(rows, open_slots):
            _attach(slot, row, name, did is not None)
            if did is not None:
                slot.discord_id = did
        unknown = sum(1 for _, did, _ in rows if did is None)
        if unknown >= 2:
            prop.notes.append(
                f"Team {team}: {unknown} names weren't recognised; their champion/KDA were "
                "put on the remaining roster slots in screen order — check them.")
        if len(rows) > len(open_slots):
            prop.notes.append(
                f"Team {team}: {len(rows) - len(open_slots)} screenshot row(s) had no free slot.")

    # ---- Winner + orientation notes.
    if not parsed.unreadable and not certain:
        prop.orientation_uncertain = True
        prop.notes.append(
            f"Only {identity + swapped} of 10 names matched the match roster, so I can't tell "
            "which screenshot team is which. Check the teams (🔁 Subs / fix → Swap teams if "
            "they're reversed) and set the winner yourself.")
    elif parsed.winner in (1, 2):
        prop.winner_hint = match_team_of[parsed.winner]

    prop.slots = sorted(slots.values(), key=lambda s: (s.team, ROLES.index(s.role)))
    return prop


@dataclass
class ReportState:
    """Mutable working state for one /report, driven by the confirm View. Pure
    (no Discord) so the state transitions and commit-building are unit-testable."""
    match_id: int
    games: list[GameProposal]
    # Winner team (1/2) per game, in game order. None = not decided yet: there was
    # no trustworthy hint, and the admin must pick before the report can commit.
    winners: list[Optional[int]]

    @classmethod
    def from_proposals(cls, match_id: int, games: list[GameProposal]) -> "ReportState":
        # A winner is only ever pre-filled from a hint — never defaulted.
        winners = [g.winner_hint for g in games]
        return cls(match_id=match_id, games=games, winners=winners)

    @property
    def all_unreadable(self) -> bool:
        """Every screenshot failed to read — the lineups are just the roster."""
        return bool(self.games) and all(g.read_failed for g in self.games)

    def _slot(self, gi: int, team: int, role: str) -> Optional[SlotProposal]:
        for s in self.games[gi].slots:
            if s.team == team and s.role == role:
                return s
        return None

    def set_winner(self, gi: int, team: int) -> None:
        self.winners[gi] = team

    def toggle_winner(self, gi: int) -> None:
        """Cycle a game's winner: undecided -> team 1 -> team 2 -> team 1 ..."""
        self.winners[gi] = 2 if self.winners[gi] == 1 else 1

    def swap_teams(self, gi: int) -> bool:
        """Map this game's screenshot teams the other way round. Rebuilds the game
        from the screenshot (manual edits to this game are dropped) and takes the
        winner from the re-mapped hint. False if the game can't be rebuilt."""
        new = self.games[gi].swapped()
        if new is None:
            return False
        self.games[gi] = new
        self.winners[gi] = new.winner_hint
        return True

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
        """Whether the report can commit. Every game needs 10 resolved players, 5
        per team, nobody listed twice (within a team or across teams) and an
        explicit winner; and the games must add up to a real series result
        (2-0, 2-1, 1-2, 0-2, or a single game). Returns (ok, problems)."""
        problems: list[str] = []
        if not self.games:
            problems.append("No games to report")
        for gi, g in enumerate(self.games, start=1):
            ids = [s.discord_id for s in g.slots]
            if any(i is None for i in ids):
                problems.append(f"Game {gi}: unresolved player(s)")
            else:
                t1 = [s.discord_id for s in g.slots if s.team == 1]
                t2 = [s.discord_id for s in g.slots if s.team == 2]
                if len(t1) != 5 or len(t2) != 5:
                    problems.append(f"Game {gi}: need 5 players per team")
                if len(set(t1)) != len(t1) or len(set(t2)) != len(t2):
                    problems.append(f"Game {gi}: the same player is in two slots on one team")
                if set(t1) & set(t2):
                    problems.append(f"Game {gi}: a player is on both teams")
            if self.winners[gi - 1] not in (1, 2):
                problems.append(f"Game {gi}: pick the winner")
        if self.games and all(w in (1, 2) for w in self.winners):
            score = (self.winners.count(1), self.winners.count(2))
            if score not in VALID_SERIES_SCORES:
                problems.append(
                    f"Series score {score[0]}-{score[1]} isn't a best-of-3 result "
                    "(2-0, 2-1, 1-2, 0-2, or one game) — fix a winner or add the missing game")
        return (not problems, problems)

    def to_game_results(self) -> list[GameResult]:
        """Build the per-game commit input. Assumes ready() is true."""
        out: list[GameResult] = []
        for g, winner in zip(self.games, self.winners):
            if winner not in (1, 2):
                raise ValueError("a game has no winner — ready() must pass before committing")
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
