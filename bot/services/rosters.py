"""Team rosters: the two team names, and parsing/printing the line-based roster
block that /manual-match, /pickup-series and /match-roster share. Pure functions."""
from __future__ import annotations

import re
from typing import Optional

from bot.config import ROLES


# Discord mention format: <@123456789> or <@!123456789> (the ! is for nicknames)
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(.+?)\s*$")

# Display + Discord ROLE names for the two teams (these keep their spaces).
# team1 -> "lo gang", team2 -> "team 10". Used by /match-channels and
# /clear-match-channels and the result embeds.
TEAM_NAMES = {1: "lo gang", 2: "team 10"}


def team_channel_name(team_name: str) -> str:
    """A team's text-channel name. Discord channel names can't contain spaces and
    are lowercased, so the channel is the hyphenated form of the role/display
    name: 'lo gang' -> 'lo-gang', 'team 10' -> 'team-10'."""
    return team_name.lower().replace(" ", "-")


def _team_no_for_label(label: Optional[str]) -> Optional[int]:
    """Map a header label read from a screenshot ('Logang', 'Team 10') to its
    TEAM_NAMES key, ignoring case/spaces/punctuation. Exact match only, so a
    generic 'Team 1' doesn't get mistaken for 'team 10'."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    if not label:
        return None
    for team_no, name in TEAM_NAMES.items():
        if norm(label) == norm(name):
            return team_no
    return None


class ManualMatchParseError(ValueError):
    """Raised when the manual match input is malformed. Message is shown to admin."""


def parse_manual_match(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Parse a strict line-based team roster.

    Expected format (case-insensitive headers, exact role names):
        TEAM 1
        TOP: <@123>
        JUNGLE: <@456>
        MID: <@789>
        BOT: <@111>
        SUPPORT: <@222>
        TEAM 2
        TOP: <@333>
        ...

    Returns (team1_dict, team2_dict) where each dict maps role -> discord_id.

    Raises ManualMatchParseError with a human-readable message on any issue.
    """
    if not text or not text.strip():
        raise ManualMatchParseError("Input is empty.")

    current_team: Optional[int] = None
    teams: dict[int, dict[str, int]] = {1: {}, 2: {}}

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        # Team header?
        upper = line.upper()
        if upper in ("TEAM 1", "TEAM1", "T1"):
            current_team = 1
            continue
        if upper in ("TEAM 2", "TEAM2", "T2"):
            current_team = 2
            continue

        # Role line
        if current_team is None:
            raise ManualMatchParseError(
                f"Line {line_num}: `{line}` — expected a TEAM header first."
            )
        m = _ROLE_LINE_RE.match(line)
        if not m:
            raise ManualMatchParseError(
                f"Line {line_num}: `{line}` — expected `ROLE: @user`."
            )

        role = m.group(1).upper()
        # Accept some shorthand
        role_aliases = {
            "TOP": "TOP",
            "JUNGLE": "JUNGLE", "JG": "JUNGLE", "JUNG": "JUNGLE",
            "MID": "MID", "MIDDLE": "MID",
            "BOT": "BOT", "BOTTOM": "BOT", "ADC": "BOT",
            "SUPPORT": "SUPPORT", "SUP": "SUPPORT", "SUPP": "SUPPORT",
        }
        if role not in role_aliases:
            raise ManualMatchParseError(
                f"Line {line_num}: unknown role `{role}`. "
                f"Use TOP / JUNGLE / MID / BOT / SUPPORT."
            )
        canonical = role_aliases[role]

        if canonical in teams[current_team]:
            raise ManualMatchParseError(
                f"Line {line_num}: {canonical} already assigned on team {current_team}."
            )

        mention_match = _MENTION_RE.search(m.group(2))
        if not mention_match:
            raise ManualMatchParseError(
                f"Line {line_num}: no Discord mention found in `{m.group(2)}`. "
                f"Use @-mentions, e.g. `TOP: @alice`."
            )
        teams[current_team][canonical] = int(mention_match.group(1))

    # Validate completeness
    for team_num in (1, 2):
        missing = [r for r in ROLES if r not in teams[team_num]]
        if missing:
            raise ManualMatchParseError(
                f"Team {team_num} is missing: {', '.join(missing)}."
            )

    # No duplicate players across teams
    t1_ids = set(teams[1].values())
    t2_ids = set(teams[2].values())
    overlap = t1_ids & t2_ids
    if overlap:
        raise ManualMatchParseError(
            f"Player(s) on both teams: {', '.join(f'<@{i}>' for i in overlap)}."
        )
    if len(t1_ids) != 5 or len(t2_ids) != 5:
        raise ManualMatchParseError(
            "Each team must have 5 distinct players (a player was listed twice)."
        )

    return teams[1], teams[2]


def format_roster_block(team1: dict[str, int], team2: dict[str, int]) -> str:
    """Render two role->discord_id team dicts as the line-based roster that
    parse_manual_match accepts. Inverse of parse_manual_match:
        parse_manual_match(format_roster_block(t1, t2)) == (t1, t2)
    Used by /match-roster to print a copy/paste-ready, editable roster.
    """
    lines = ["TEAM 1"]
    lines += [f"{r}: <@{team1[r]}>" for r in ROLES if r in team1]
    lines.append("TEAM 2")
    lines += [f"{r}: <@{team2[r]}>" for r in ROLES if r in team2]
    return "\n".join(lines)
