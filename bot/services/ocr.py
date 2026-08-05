"""Screenshot reading for inhouse game results — backed by Claude vision.

The League end-of-game scoreboard varies in layout/resolution, and the post-game
splash screen shows champions only as portraits (no text), so classic OCR
(tesseract) couldn't read names/KDA reliably and never read champions at all.
We now hand the screenshot to a Claude vision model, which extracts each team's
five players (summoner name, champion, KDA) and which team won, in one call.

The flow is unchanged from the caller's side:

  1. Admin uploads screenshot(s) via /report
  2. parse_scoreboard_image() returns a ParsedScoreboard (two teams + a winner hint)
  3. report_analysis aligns it to the rostered teams; the admin confirms/edits
  4. Nothing auto-commits — the confirmation step always runs.

Requires ANTHROPIC_API_KEY in the environment (Fly secret in prod). If the key or
SDK is missing, or the call fails, we return an empty board with a note so the
admin can still enter the lineup by hand.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Vision model used to read scoreboards. Override with OCR_MODEL if needed.
_MODEL = os.environ.get("OCR_MODEL", "claude-opus-4-8")


# =============================================================================
# Data model (unchanged — report_analysis + tests depend on these)
# =============================================================================
@dataclass
class ScoreRow:
    """One player row from a game scoreboard."""
    raw: str
    name_guess: str
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None
    champion: Optional[str] = None


@dataclass
class ParsedScoreboard:
    """Structured result of reading ONE game screenshot. team1 is the top team as
    shown, team2 the bottom team; each is in scoreboard order (TOP→SUPPORT)."""
    team1: list[ScoreRow] = field(default_factory=list)
    team2: list[ScoreRow] = field(default_factory=list)
    banner: Optional[str] = None  # "VICTORY" (top team won) | "DEFEAT" | None
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)


# =============================================================================
# Text parsing — kept for unit tests and as a no-vision fallback. Pure function.
# =============================================================================
_KDA_LOOSE_RE = re.compile(r"(\d{1,2})\s*[/\\|]\s*(\d{1,2})\s*[/\\|]\s*(\d{1,2})")
_TEAM1_RE = re.compile(r"team\s*1\b", re.IGNORECASE)
_TEAM2_RE = re.compile(r"team\s*2\b", re.IGNORECASE)
_PORTRAIT_MARKERS = "@()[]{}©®*|"


def _detect_banner(text: str) -> Optional[str]:
    """Find the VICTORY/DEFEAT banner, tolerating garble ('puCTORY')."""
    u = text.upper()
    has_victory = "VICTORY" in u or "CTORY" in u
    has_defeat = "DEFEAT" in u or "EFEAT" in u
    if has_victory and not has_defeat:
        return "VICTORY"
    if has_defeat and not has_victory:
        return "DEFEAT"
    return None


def _clean_name(before_kda: str) -> str:
    """Best-guess player name from noisy text before the KDA."""
    s = before_kda.strip()
    last = -1
    for i, ch in enumerate(s[:14]):
        if ch in _PORTRAIT_MARKERS:
            last = i
    if last >= 0:
        s = s[last + 1:]
    out: list[str] = []
    for tok in s.split():
        alpha = re.sub(r"[^A-Za-z]", "", tok)
        is_namey = len(alpha) >= 2 and len(alpha) >= len(tok) - 1
        if not out:
            if is_namey:
                out.append(alpha)
            continue
        if is_namey:
            out.append(alpha)
        else:
            break
    return " ".join(out).strip()


def _extract_rows(lines: list[str], limit: int = 5) -> list[ScoreRow]:
    rows: list[ScoreRow] = []
    for line in lines:
        if len(rows) >= limit:
            break
        if sum(c.isalpha() for c in line) < 2:
            continue
        kda = _KDA_LOOSE_RE.search(line)
        k = d = a = None
        before = line
        if kda:
            k, d, a = int(kda.group(1)), int(kda.group(2)), int(kda.group(3))
            before = line[: kda.start()]
        rows.append(ScoreRow(raw=line, name_guess=_clean_name(before),
                             kills=k, deaths=d, assists=a))
    return rows


def parse_scoreboard_text(text: str) -> ParsedScoreboard:
    """Parse raw text of ONE game into two team blocks + a winner hint. Pure
    function (no image work) so it's unit-testable against fixture text."""
    result = ParsedScoreboard(raw_text=text, banner=_detect_banner(text))
    lines = text.splitlines()
    t1_idx = t2_idx = None
    for i, line in enumerate(lines):
        if t1_idx is None and _TEAM1_RE.search(line):
            t1_idx = i
        elif t2_idx is None and _TEAM2_RE.search(line):
            t2_idx = i
    if t1_idx is None or t2_idx is None or t2_idx <= t1_idx:
        result.notes.append(
            "Could not locate both TEAM 1 / TEAM 2 sections — admin must enter the lineup."
        )
        return result
    result.team1 = _extract_rows(lines[t1_idx + 1 : t2_idx])
    result.team2 = _extract_rows(lines[t2_idx + 1 :])
    if len(result.team1) != 5 or len(result.team2) != 5:
        result.notes.append(
            f"Read {len(result.team1)} + {len(result.team2)} players "
            "(expected 5 + 5) — verify the lineup."
        )
    return result


# =============================================================================
# Vision reading (Claude) — one screenshot -> ParsedScoreboard
# =============================================================================
_VISION_PROMPT = (
    "This image is a League of Legends end-of-game scoreboard (either the in-client "
    "post-game splash or the full match summary).\n\n"
    "Extract both teams exactly as displayed. The TOP team is team1, the BOTTOM team "
    "is team2. Within each team, list the 5 players in the order they appear top to "
    "bottom — this is role order: TOP, JUNGLE, MID, BOT, SUPPORT.\n\n"
    "For each player provide:\n"
    "- name: the player's SUMMONER/account name (the primary player label), NOT the "
    "champion. Copy it exactly, including capitalization, spaces, and underscores.\n"
    "- champion: the champion that player is on. If the champion name is written, use "
    "it; otherwise identify the champion from the portrait icon.\n"
    "- kills, deaths, assists: the three K / D / A numbers for that player, in order.\n\n"
    "Set winning_team to 1 if the top team won or 2 if the bottom team won — decide "
    "from the VICTORY/DEFEAT banner if present, otherwise from the score/kill totals.\n\n"
    "Read the actual pixels; do not guess or invent players. If a value is unreadable, "
    "use your best read."
)


def _media_type(b: bytes) -> str:
    """Sniff the image media type from magic bytes (no Pillow needed)."""
    if b[:8].startswith(b"\x89PNG"):
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] in (b"GIF8",):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"  # default; League screenshots are PNG


def _player_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "champion": {"type": "string"},
            "kills": {"type": "integer"},
            "deaths": {"type": "integer"},
            "assists": {"type": "integer"},
        },
        "required": ["name", "champion", "kills", "deaths", "assists"],
        "additionalProperties": False,
    }


_BOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "team1": {"type": "array", "items": _player_schema()},
        "team2": {"type": "array", "items": _player_schema()},
        "winning_team": {"type": "integer", "enum": [1, 2]},
    },
    "required": ["team1", "team2", "winning_team"],
    "additionalProperties": False,
}


def _row(p: dict) -> ScoreRow:
    champ = (p.get("champion") or "").strip() or None
    name = (p.get("name") or "").strip()

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return ScoreRow(
        raw=name, name_guess=name, champion=champ,
        kills=_int(p.get("kills")), deaths=_int(p.get("deaths")), assists=_int(p.get("assists")),
    )


def parse_scoreboard_image(image_bytes: bytes) -> ParsedScoreboard:
    """Read ONE game screenshot with Claude vision into two teams + a winner hint.

    Best-effort and never raises: any failure (missing key/SDK, API error, bad
    output) returns an empty board with a note so the admin can enter the lineup
    by hand. Synchronous — call via asyncio.to_thread from async code.
    """
    if not image_bytes:
        return ParsedScoreboard(notes=["Empty image — enter the lineup manually."])
    try:
        import anthropic  # lazy: keeps import-time light and SDK optional
    except ImportError:
        return ParsedScoreboard(notes=["anthropic SDK not installed — enter the lineup manually."])
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return ParsedScoreboard(notes=["ANTHROPIC_API_KEY not set — enter the lineup manually."])

    data = base64.standard_b64encode(image_bytes).decode("utf-8")
    media = _media_type(image_bytes)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}},
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }],
            output_config={"format": {"type": "json_schema", "schema": _BOARD_SCHEMA}},
        )
    except Exception as e:  # network, auth, API, model — all non-fatal here
        log.exception("Claude scoreboard read failed")
        return ParsedScoreboard(notes=[f"Vision read failed ({e}). Enter the lineup manually."])

    if resp.stop_reason == "refusal":
        return ParsedScoreboard(notes=["Vision read was declined — enter the lineup manually."])

    import json
    try:
        text = next(b.text for b in resp.content if b.type == "text")
        board = json.loads(text)
    except (StopIteration, json.JSONDecodeError, AttributeError) as e:
        log.warning("Claude scoreboard output unparseable: %s", e)
        return ParsedScoreboard(notes=["Could not parse the scoreboard — enter the lineup manually."])

    sb = ParsedScoreboard(
        raw_text=text,
        team1=[_row(p) for p in (board.get("team1") or [])[:5]],
        team2=[_row(p) for p in (board.get("team2") or [])[:5]],
        banner="VICTORY" if board.get("winning_team") == 1 else "DEFEAT",
    )
    if len(sb.team1) != 5 or len(sb.team2) != 5:
        sb.notes.append(
            f"Read {len(sb.team1)} + {len(sb.team2)} players (expected 5 + 5) — verify the lineup."
        )
    return sb
