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
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image
import pytesseract

log = logging.getLogger(__name__)

# Production (Docker) has tesseract on PATH; on dev machines point at it with the
# TESSERACT_CMD env var (e.g. C:\Program Files\Tesseract-OCR\tesseract.exe).
_TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if _TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD

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
# Splits ONE game screenshot into the two teams + rows and pulls KDA. Real OCR is
# noisy (KDA slashes mangle: "14/2/6" -> "14/276"; the post-game splash screen is
# worst), so it's best-effort: a starting point the admin confirms. Champions come
# from icon matching (champion_vision), names from the alias resolver downstream;
# the VICTORY/DEFEAT banner is a hint only (it reflects whoever took the shot).

_KDA_LOOSE_RE = re.compile(r"(\d{1,2})\s*[/\\|]\s*(\d{1,2})\s*[/\\|]\s*(\d{1,2})")
_TEAM1_RE = re.compile(r"team\s*1\b", re.IGNORECASE)
_TEAM2_RE = re.compile(r"team\s*2\b", re.IGNORECASE)
_PORTRAIT_MARKERS = "@()[]{}©®*|"


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
    """Best-guess player name from the noisy text before the KDA. The row looks
    like '<level> <portrait-marker> Name <item garbage>'; cut past the last early
    portrait marker, then take the leading run of name-ish tokens."""
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
        if sum(c.isalpha() for c in line) < 2:  # skip blank/divider/header lines
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
    """Parse raw OCR text of ONE game into two team blocks + a winner hint.
    Pure function (no image work) so it's unit-testable against fixture text."""
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


# KDA column geometry (match-history), as a fraction of image width, and a
# digits+slash whitelist so tesseract stops mangling slashes ("5/1/12" -> "5477").
_KDA_X_FRAC = (0.61, 0.74)  # stop short of the CS column (~0.78) to avoid bleed
_KDA_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789/"


def _grouped_rows(img: "Image.Image") -> list[tuple[int, str]]:
    """OCR with word boxes, regrouped into VISUAL rows by y. Returns (y_center,
    line_text) per row. image_to_string scatters a player's name and stats across
    lines; grouping word boxes by row keeps each player on one line and gives the
    row's y so we can crop its KDA cell precisely."""
    from pytesseract import Output
    data = pytesseract.image_to_data(img, output_type=Output.DICT, config="--psm 6")
    toks = []
    for i, t in enumerate(data["text"]):
        t = t.strip()
        try:
            conf = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if t and conf >= 30:
            toks.append((data["top"][i], data["left"][i], data["height"][i], t))
    if not toks:
        return []
    toks.sort()
    band = max(10, int(1.3 * sorted(h for _, _, h, _ in toks)[len(toks) // 2]))
    rows: list[tuple[int, str]] = []
    cur: list[tuple[int, int, str]] = []  # (left, top+h/2, text)
    cur_y = None
    for top, left, h, t in toks:
        if cur_y is None or abs(top - cur_y) <= band:
            cur.append((left, top + h // 2, t))
            cur_y = top if cur_y is None else cur_y
        else:
            ys = [c[1] for c in cur]
            rows.append((sum(ys) // len(ys), " ".join(c[2] for c in sorted(cur))))
            cur, cur_y = [(left, top + h // 2, t)], top
    if cur:
        ys = [c[1] for c in cur]
        rows.append((sum(ys) // len(ys), " ".join(c[2] for c in sorted(cur))))
    return rows


def _ocr_kda_cell(img: "Image.Image", cy: int, band: int) -> Optional[tuple[int, int, int]]:
    """OCR just the KDA cell at row-center cy with a digits-only whitelist."""
    W, H = img.size
    x1, x2 = int(W * _KDA_X_FRAC[0]), int(W * _KDA_X_FRAC[1])
    cell = img.crop((x1, max(0, cy - band), x2, min(H, cy + band)))
    try:
        txt = pytesseract.image_to_string(cell, config=_KDA_CONFIG)
    except Exception:
        return None
    m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})", txt)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _overlay_kda(img: "Image.Image", rows: list[tuple[int, str]], sb: ParsedScoreboard) -> None:
    """Fill each ScoreRow's KDA from a targeted crop of its row's KDA cell. Aligns
    to the same player rows parse_scoreboard_text picked (same 'has letters' filter
    and order), so crop and name stay in sync."""
    t1 = t2 = None
    for i, (_, txt) in enumerate(rows):
        if t1 is None and _TEAM1_RE.search(txt):
            t1 = i
        elif t2 is None and _TEAM2_RE.search(txt):
            t2 = i
    if t1 is None or t2 is None or t2 <= t1:
        return
    band = max(12, int(0.018 * img.size[1]))

    def player_ys(start: int, stop: Optional[int]) -> list[int]:
        ys = []
        for j in range(start + 1, stop if stop is not None else len(rows)):
            cy, txt = rows[j]
            if sum(c.isalpha() for c in txt) >= 2:
                ys.append(cy)
            if len(ys) >= 5:
                break
        return ys

    for scorerow, cy in zip(sb.team1, player_ys(t1, t2)):
        kda = _ocr_kda_cell(img, cy, band)
        if kda:
            scorerow.kills, scorerow.deaths, scorerow.assists = kda
    for scorerow, cy in zip(sb.team2, player_ys(t2, None)):
        kda = _ocr_kda_cell(img, cy, band)
        if kda:
            scorerow.kills, scorerow.deaths, scorerow.assists = kda


def parse_scoreboard_image(image_bytes: bytes) -> ParsedScoreboard:
    """OCR one game screenshot (grayscale + 2x upscale) and parse it into teams,
    with KDA read from targeted per-row crops (more reliable than full-image OCR)."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((img.width * 2, img.height * 2))
    except Exception as e:
        log.exception("Failed to open uploaded image")
        return ParsedScoreboard(notes=[f"Image error: {e}"])
    try:
        rows = _grouped_rows(img)
    except pytesseract.TesseractNotFoundError:
        return ParsedScoreboard(notes=["Tesseract not installed. Enter the lineup manually."])
    except Exception as e:
        log.exception("Tesseract failed")
        return ParsedScoreboard(notes=[f"OCR failed: {e}"])
    sb = parse_scoreboard_text("\n".join(t for _, t in rows))
    try:
        _overlay_kda(img, rows, sb)
    except Exception:
        log.warning("KDA overlay failed; leaving KDA blank", exc_info=True)
    return sb
