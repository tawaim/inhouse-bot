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
    shown (TEAM 1 / blue), team2 the bottom team (TEAM 2 / red).

    Rows are in DISPLAY order, which is not reliably role order: the post-game
    lobby lists TOP→SUPPORT, but the match-history scoreboard lists the viewing
    player first on their own team. Don't read a role off a row's position.
    """
    team1: list[ScoreRow] = field(default_factory=list)
    team2: list[ScoreRow] = field(default_factory=list)
    # The VICTORY/DEFEAT banner exactly as shown. It is from the point of view of
    # whoever took the screenshot — it does NOT say whether the top team won. Use
    # `winner` for that.
    banner: Optional[str] = None
    team1_label: Optional[str] = None  # team name from the header, if shown
    team2_label: Optional[str] = None
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)
    # Which SCREENSHOT team won (1 = top, 2 = bottom), or None when the image
    # gives no outcome or its evidence disagrees (see decide_winner).
    winner: Optional[int] = None
    team1_kills: Optional[int] = None   # kill total from each team's header
    team2_kills: Optional[int] = None
    viewer_team: Optional[int] = None   # team of the highlighted (viewing) player

    @property
    def unreadable(self) -> bool:
        """True when no player was read at all (vision failed / wrong image)."""
        return not self.team1 and not self.team2


def decide_winner(
    team1_kills: Optional[int],
    team2_kills: Optional[int],
    banner: Optional[str],
    viewer_team: Optional[int],
    model_vote: Optional[int] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Which screenshot team (1 top / 2 bottom) won, from independent evidence:

      - the header kill totals (the winner almost always has more kills),
      - the banner applied to the VIEWER's team (the banner is relative to
        whoever took the screenshot, so it is useless without knowing their team),
      - the vision model's own conclusion, when there is one.

    Returns (winner, problem). The winner is set only when every available piece
    of evidence agrees; if they disagree, or there is none, it is None — the admin
    picks. `problem` is a human-readable note when the evidence conflicts.
    """
    votes: dict[str, int] = {}
    if team1_kills is not None and team2_kills is not None and team1_kills != team2_kills:
        votes["kill totals"] = 1 if team1_kills > team2_kills else 2
    if banner in ("VICTORY", "DEFEAT") and viewer_team in (1, 2):
        votes["banner"] = viewer_team if banner == "VICTORY" else 3 - viewer_team
    if model_vote in (1, 2):
        votes["vision read"] = model_vote
    picked = set(votes.values())
    if len(picked) == 1:
        return picked.pop(), None
    if len(picked) > 1:
        detail = ", ".join(f"{k} say team {v}" for k, v in votes.items())
        return None, f"Couldn't tell who won ({detail}) — set the winner yourself."
    return None, None


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
    # "TEAM 1 19/26/36 53,677" — the header carries the team's K/D/A total. With no
    # way to know the viewer's team from text, kills are the only winner evidence.
    for attr, idx in (("team1_kills", t1_idx), ("team2_kills", t2_idx)):
        m = _KDA_LOOSE_RE.search(lines[idx])
        if m:
            setattr(result, attr, int(m.group(1)))
    result.winner, problem = decide_winner(
        result.team1_kills, result.team2_kills, result.banner, result.viewer_team)
    if problem:
        result.notes.append(problem)
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
    "This image shows two teams of five League of Legends players. It is one of:\n"
    "(A) the post-game LOBBY screen: champion names are written as text and each team "
    "is listed in role order (top, jungle, mid, bot, support);\n"
    "(B) the MATCH-HISTORY scoreboard: champions appear only as portraits, and one row "
    "is highlighted (gold text/border) — that row is the VIEWER, the player whose "
    "client took the screenshot. The viewer is listed FIRST on their own team, out of "
    "role order;\n"
    "(C) a plain roster — a spreadsheet/table or typed list with two team columns or "
    "sections and roles labeled top/jung(le)/mid/bot(adc)/supp(ort).\n\n"
    "TEAMS. On a game screen (A or B) the upper team (header 'TEAM 1', blue) is team1 "
    "and the lower team ('TEAM 2', red) is team2. In a roster, the LEFT column (or "
    "first-listed section) is team1 and the other is team2. Set team1_name / team2_name "
    "to each team's name exactly as written in its header or column title (e.g. "
    "'Team 10', 'lo gang'); null if the image shows no team name.\n\n"
    "PLAYERS. On a game screen, list each team's players in exactly the order they are "
    "displayed, top to bottom — do not reorder them. In a roster, order each team by "
    "its role labels: top, jungle, mid, bot, support. For each player provide:\n"
    "- name: the player's SUMMONER/account name (the primary player label), NOT the "
    "champion. Copy it exactly, including capitalization, spaces, and underscores.\n"
    "- champion: the champion that player is on. If the champion name is written, use "
    "it; otherwise identify the champion from the portrait icon. null if the image "
    "shows no champions (e.g. a roster).\n"
    "- kills, deaths, assists: the three K / D / A numbers for that player, in order. "
    "null if the image shows no KDA.\n"
    "- is_viewer: true only for the single highlighted row on a match-history "
    "scoreboard; false for every other row and on screens with no highlighted row.\n\n"
    "TEAM TOTALS. Each team's header shows that team's total kills / deaths / assists "
    "and its gold. Set team1_kills / team2_kills to the KILLS number from each header; "
    "null if it is not shown.\n\n"
    "OUTCOME. Set banner to the large VICTORY or DEFEAT text if the screen shows one, "
    "else null. The banner is written from the VIEWER's point of view: it says whether "
    "the viewer's own team won. It does NOT say whether the upper team won — a VICTORY "
    "banner on a screenshot taken by a player on the lower team means the lower team "
    "won. Then set team1_won and team2_won. Use both kinds of evidence: (1) the team "
    "with more kills and more gold in its header almost always won; (2) the banner, "
    "applied to the team the highlighted viewer is on. If the two agree, or only one "
    "is available, mark the winning team true and the other false. If they disagree, "
    "or there is no usable evidence (e.g. a roster), set both to null.\n\n"
    "Read the actual pixels; do not guess or invent players. If a value is unreadable, "
    "use your best read."
)

# Anthropic's per-image limit, and the formats the vision API accepts.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# One screenshot read must not hang the /report interaction.
_TIMEOUT_SECONDS = 60.0


def _sniff_media_type(b: bytes) -> Optional[str]:
    """The image media type from magic bytes (no Pillow needed), or None when the
    bytes aren't one of the formats the vision API accepts."""
    if b[:8].startswith(b"\x89PNG"):
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] in (b"GIF8",):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def _media_type(b: bytes) -> str:
    """Media type for the API request. Callers validate with image_problem() first;
    PNG is only a last-resort default."""
    return _sniff_media_type(b) or "image/png"


def image_problem(image_bytes: bytes) -> Optional[str]:
    """Why these bytes can't be sent to the vision model, or None if they can.
    Checked before any API call so a bad upload gets a clear message instead of an
    opaque API error."""
    if not image_bytes:
        return "Empty image — enter the lineup manually."
    if len(image_bytes) > MAX_IMAGE_BYTES:
        mb = len(image_bytes) / (1024 * 1024)
        return (f"Screenshot is too large ({mb:.1f} MB; the limit is "
                f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB) — re-save it smaller or as a JPEG, "
                "or enter the lineup manually.")
    if _sniff_media_type(image_bytes) is None:
        return ("That file isn't a PNG, JPEG, GIF or WebP image — upload a screenshot, "
                "or enter the lineup manually.")
    return None


def _player_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "champion": {"type": ["string", "null"]},
            "kills": {"type": ["integer", "null"]},
            "deaths": {"type": ["integer", "null"]},
            "assists": {"type": ["integer", "null"]},
            "is_viewer": {"type": "boolean"},
        },
        "required": ["name", "champion", "kills", "deaths", "assists", "is_viewer"],
        "additionalProperties": False,
    }


_BOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "team1": {"type": "array", "items": _player_schema()},
        "team2": {"type": "array", "items": _player_schema()},
        "team1_name": {"type": ["string", "null"]},
        "team2_name": {"type": ["string", "null"]},
        "team1_kills": {"type": ["integer", "null"]},
        "team2_kills": {"type": ["integer", "null"]},
        "banner": {"anyOf": [{"type": "string", "enum": ["VICTORY", "DEFEAT"]}, {"type": "null"}]},
        "team1_won": {"type": ["boolean", "null"]},
        "team2_won": {"type": ["boolean", "null"]},
    },
    "required": ["team1", "team2", "team1_name", "team2_name", "team1_kills",
                 "team2_kills", "banner", "team1_won", "team2_won"],
    "additionalProperties": False,
}


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _row(p: dict) -> ScoreRow:
    champ = (p.get("champion") or "").strip() or None
    name = (p.get("name") or "").strip()
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
    problem = image_problem(image_bytes)
    if problem:
        return ParsedScoreboard(notes=[problem])
    try:
        import anthropic  # lazy: keeps import-time light and SDK optional
    except ImportError:
        return ParsedScoreboard(notes=["anthropic SDK not installed — enter the lineup manually."])
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return ParsedScoreboard(notes=["ANTHROPIC_API_KEY not set — enter the lineup manually."])

    data = base64.standard_b64encode(image_bytes).decode("utf-8")
    media = _media_type(image_bytes)
    try:
        # Bounded: a hung read would otherwise sit on the SDK's 10-minute default
        # while the admin stares at "thinking…". One retry, so worst case ~2 min.
        client = anthropic.Anthropic(timeout=_TIMEOUT_SECONDS, max_retries=1)
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
    except anthropic.APITimeoutError:
        log.warning("Claude scoreboard read timed out")
        return ParsedScoreboard(notes=[
            f"Vision read timed out after {_TIMEOUT_SECONDS:.0f}s — enter the lineup manually."])
    except Exception as e:  # network, auth, API, model — all non-fatal here
        log.exception("Claude scoreboard read failed")
        detail = f"{type(e).__name__}: {e}"[:200]
        return ParsedScoreboard(notes=[f"Vision read failed ({detail}). Enter the lineup manually."])

    if resp.stop_reason == "refusal":
        return ParsedScoreboard(notes=["Vision read was declined — enter the lineup manually."])

    import json
    try:
        text = next(b.text for b in resp.content if b.type == "text")
        board = json.loads(text)
        return board_to_scoreboard(board, text)
    except Exception as e:  # truncated / malformed output — still never raise
        log.warning("Claude scoreboard output unparseable: %s", e)
        return ParsedScoreboard(notes=["Could not parse the scoreboard — enter the lineup manually."])


def board_to_scoreboard(board: dict, raw_text: str = "") -> ParsedScoreboard:
    """Turn the vision model's JSON (shape: _BOARD_SCHEMA) into a ParsedScoreboard,
    cross-checking the winner. Pure — no network — so it is unit-testable."""
    rows1 = list(board.get("team1") or [])[:5]
    rows2 = list(board.get("team2") or [])[:5]
    banner = board.get("banner")
    banner = banner.upper() if isinstance(banner, str) else None
    if banner not in ("VICTORY", "DEFEAT"):
        banner = None
    # The viewer is the one highlighted row; ignore the flag if the model set it
    # on several rows or on both teams.
    flagged = [team for team, rows in ((1, rows1), (2, rows2))
               for p in rows if p.get("is_viewer") is True]
    viewer_team = flagged[0] if len(flagged) == 1 else None

    won1, won2 = board.get("team1_won"), board.get("team2_won")
    model_vote = 1 if (won1 is True and won2 is not True) else \
        2 if (won2 is True and won1 is not True) else None

    sb = ParsedScoreboard(
        raw_text=raw_text,
        team1=[_row(p) for p in rows1],
        team2=[_row(p) for p in rows2],
        banner=banner,
        team1_label=(board.get("team1_name") or "").strip() or None,
        team2_label=(board.get("team2_name") or "").strip() or None,
        team1_kills=_int(board.get("team1_kills")),
        team2_kills=_int(board.get("team2_kills")),
        viewer_team=viewer_team,
    )
    sb.winner, problem = decide_winner(
        sb.team1_kills, sb.team2_kills, banner, viewer_team, model_vote)
    if problem:
        sb.notes.append(problem)
    if len(sb.team1) != 5 or len(sb.team2) != 5:
        sb.notes.append(
            f"Read {len(sb.team1)} + {len(sb.team2)} players (expected 5 + 5) — verify the lineup."
        )
    return sb
