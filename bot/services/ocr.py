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
