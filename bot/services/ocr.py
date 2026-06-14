"""Screenshot OCR for inhouse game results.

The League end-of-game scoreboard varies in layout/resolution, so this is
inherently best-effort. The flow is:

  1. Admin uploads screenshot via /report
  2. We run pytesseract on the full image and the cropped scoreboard region
  3. Parse out: which team won (VICTORY / DEFEAT banner), player names, KDA
  4. Return a ParsedResult and let the admin confirm/edit before saving

We do NOT trust the OCR enough to auto-commit. Confirmation step always.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image
import pytesseract

log = logging.getLogger(__name__)

# Common scoreboard parsing — KDA looks like "12 / 4 / 8" or "12/4/8"
KDA_RE = re.compile(r"\b(\d{1,2})\s*[/\\]\s*(\d{1,2})\s*[/\\]\s*(\d{1,2})\b")
# Riot ID looks like "Name#TAG"
RIOT_ID_RE = re.compile(r"\b([A-Za-z0-9 ]{3,16})#([A-Za-z0-9]{2,5})\b")


@dataclass
class ParsedPlayerLine:
    raw_text: str
    riot_id: Optional[str] = None
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None


@dataclass
class ParsedResult:
    winner: Optional[int]  # 1 or 2 if detected, else None
    confidence: float       # 0..1 rough heuristic
    players: list[ParsedPlayerLine] = field(default_factory=list)
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)


def parse_screenshot(image_bytes: bytes) -> ParsedResult:
    """Run OCR and pull out winner + per-row KDAs."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        log.exception("Failed to open uploaded image")
        return ParsedResult(winner=None, confidence=0.0, notes=[f"Image error: {e}"])

    # Run tesseract on the full image. For better accuracy on scoreboard text
    # we could crop, but resolution varies wildly.
    try:
        text = pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError:
        return ParsedResult(
            winner=None,
            confidence=0.0,
            notes=["Tesseract not installed on host. Use /report-manual instead."],
        )
    except Exception as e:
        log.exception("Tesseract failed")
        return ParsedResult(winner=None, confidence=0.0, notes=[f"OCR failed: {e}"])

    result = ParsedResult(winner=None, confidence=0.0, raw_text=text)

    upper = text.upper()
    # The big banner at the top says "VICTORY" or "DEFEAT" for the player who
    # took the screenshot. Without knowing whose POV the screenshot is, we
    # just record what we saw and let the admin confirm.
    if "VICTORY" in upper and "DEFEAT" in upper:
        # Both shown — likely a spectator/end-of-game summary scoreboard
        result.notes.append("Both VICTORY and DEFEAT detected; admin must specify winner.")
    elif "VICTORY" in upper:
        result.notes.append("VICTORY banner detected (assumes screenshotter's team won).")
        result.confidence = 0.5
    elif "DEFEAT" in upper:
        result.notes.append("DEFEAT banner detected (assumes screenshotter's team lost).")
        result.confidence = 0.5

    # Parse line by line for player rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        kda_match = KDA_RE.search(line)
        riot_match = RIOT_ID_RE.search(line)
        if not (kda_match or riot_match):
            continue
        p = ParsedPlayerLine(raw_text=line)
        if kda_match:
            p.kills = int(kda_match.group(1))
            p.deaths = int(kda_match.group(2))
            p.assists = int(kda_match.group(3))
        if riot_match:
            p.riot_id = f"{riot_match.group(1).strip()}#{riot_match.group(2)}"
        result.players.append(p)

    if 5 <= len(result.players) <= 12:
        result.confidence = max(result.confidence, 0.6)
    if len(result.players) == 10:
        result.confidence = max(result.confidence, 0.8)

    return result


# =============================================================================
# Per-game structured parsing (one screenshot = one game)
# =============================================================================
# Real-world OCR of LoL scoreboards is noisy: names read fairly well, KDA reads
# ~half the time (slashes get mangled: "14/2/6" -> "14/276"), and the post-game
# splash-art screen is the worst. So this parser is deliberately best-effort —
# it produces a best-guess lineup + KDA that the admin CONFIRMS before commit.
# The reliable signals are: the TEAM 1 / TEAM 2 section split and row order
# (which is role order, TOP→SUPPORT). Names are extracted as guesses and matched
# against linked players downstream; the VICTORY/DEFEAT banner is a hint only
# (it reflects whoever took the screenshot, not "team 1").

# KDA tolerant of mangled separators: /, \, |, or stray letters OCR slips in.
_KDA_LOOSE_RE = re.compile(r"(\d{1,2})\s*[/\\|]\s*(\d{1,2})\s*[/\\|]\s*(\d{1,2})")
_TEAM1_RE = re.compile(r"team\s*1\b", re.IGNORECASE)
_TEAM2_RE = re.compile(r"team\s*2\b", re.IGNORECASE)
# Characters OCR emits for the champion-portrait circle that precedes a name.
_PORTRAIT_MARKERS = "@()[]{}©®®*|"


@dataclass
class ScoreRow:
    """One player row from a game scoreboard (best-effort)."""
    raw: str
    name_guess: str
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None
    champion: Optional[str] = None


@dataclass
class ParsedScoreboard:
    """Structured result of OCRing ONE game screenshot."""
    team1: list[ScoreRow] = field(default_factory=list)
    team2: list[ScoreRow] = field(default_factory=list)
    banner: Optional[str] = None  # "VICTORY" | "DEFEAT" | None — POV hint only
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)


def _detect_banner(text: str) -> Optional[str]:
    """Find the VICTORY/DEFEAT banner, tolerating OCR garble ('puCTORY')."""
    u = text.upper()
    has_victory = "VICTORY" in u or "CTORY" in u
    has_defeat = "DEFEAT" in u or "EFEAT" in u
    if has_victory and not has_defeat:
        return "VICTORY"
    if has_defeat and not has_victory:
        return "DEFEAT"
    return None  # both or neither — admin denotes the winner anyway


def _clean_name(before_kda: str) -> str:
    """Pull a best-guess player name out of the noisy text that precedes the KDA.

    The row looks like '<level> <portrait-marker> Name <item garbage>'. We cut to
    just after the last portrait marker in the leading segment, then take the
    leading run of alphabetic tokens (names can be two words like 'Zack Fox'),
    stopping at the first token that's clearly item/stat noise.
    """
    s = before_kda.strip()
    # Cut to just past the last portrait marker that appears early in the row.
    last = -1
    for i, ch in enumerate(s[:14]):
        if ch in _PORTRAIT_MARKERS:
            last = i
    if last >= 0:
        s = s[last + 1:]
    tokens = s.split()
    out: list[str] = []
    for tok in tokens:
        alpha = re.sub(r"[^A-Za-z]", "", tok)
        # A name token is mostly letters. Allow internal digits a name may have
        # only once we've already started (rare); first token must be ≥2 letters.
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


def _looks_like_row(line: str) -> bool:
    """A player row has a level number and some letters; skip blank/divider lines
    and the stat-header line right under the TEAM header."""
    if not line.strip():
        return False
    letters = sum(c.isalpha() for c in line)
    return letters >= 2


def _extract_rows(lines: list[str], limit: int = 5) -> list[ScoreRow]:
    rows: list[ScoreRow] = []
    for line in lines:
        if len(rows) >= limit:
            break
        if not _looks_like_row(line):
            continue
        kda = _KDA_LOOSE_RE.search(line)
        k = d = a = None
        before = line
        if kda:
            k, d, a = int(kda.group(1)), int(kda.group(2)), int(kda.group(3))
            before = line[: kda.start()]
        name = _clean_name(before)
        if not name:
            # No recoverable name — still emit a placeholder row so the admin
            # sees the slot and fills it; row order maps to role downstream.
            name = ""
        rows.append(ScoreRow(raw=line, name_guess=name, kills=k, deaths=d, assists=a))
    return rows


def parse_scoreboard_text(text: str) -> ParsedScoreboard:
    """Parse raw OCR text of ONE game into two team blocks + a winner hint.

    Pure function (no image work) so it's unit-testable against fixture text.
    """
    result = ParsedScoreboard(raw_text=text)
    result.banner = _detect_banner(text)

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


def _preprocess(img: "Image.Image") -> "Image.Image":
    """Light preprocessing to help tesseract on scoreboard text: grayscale +
    2x upscale. Threshold is intentionally skipped — League's varied backgrounds
    make a global threshold hurt as often as it helps."""
    img = img.convert("L")
    w, h = img.size
    return img.resize((w * 2, h * 2))


def parse_scoreboard_image(image_bytes: bytes) -> ParsedScoreboard:
    """OCR one game screenshot and parse it into a ParsedScoreboard."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        log.exception("Failed to open uploaded image")
        return ParsedScoreboard(notes=[f"Image error: {e}"])
    try:
        text = pytesseract.image_to_string(_preprocess(img), config="--psm 6")
    except pytesseract.TesseractNotFoundError:
        return ParsedScoreboard(
            notes=["Tesseract not installed on host. Enter the lineup manually."]
        )
    except Exception as e:
        log.exception("Tesseract failed")
        return ParsedScoreboard(notes=[f"OCR failed: {e}"])
    return parse_scoreboard_text(text)
