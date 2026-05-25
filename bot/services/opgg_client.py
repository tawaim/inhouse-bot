"""Async client for OP.GG's hosted MCP server.

Why this exists
---------------
Riot's API cannot give us a player's PAST-SEASON rank: LEAGUE-V4 returns only
the current split, and MATCH-V5 carries no rank/tier field at all. OP.GG, however,
snapshots each player's season-end Solo/Duo tier into its own DB and exposes it
through the `lol_get_summoner_profile` tool on its public MCP server.

We use exactly one capability: given a Riot ID, return the player's most recent
past-season Solo/Duo rank, so we can seed a currently-unranked player's base elo.

Transport
---------
The hosted endpoint (https://mcp-api.op.gg/mcp) speaks JSON-RPC 2.0 over MCP's
"Streamable HTTP" transport — no Node, no auth key, no session id needed. We drive
it with a tiny httpx client: `initialize` -> `notifications/initialized` -> `tools/call`.

Response format
---------------
OP.GG returns a token-compressed "class-repr" STRING (not JSON), e.g.
    ...Summoner([PreviousSeason(31,TierInfo("EMERALD",3)),...],[PreviousSeasonTier(31,
       [RankEntrie("SOLORANKED",RankInfo("EMERALD",3,"2026-01-08T18:44:41+09:00")),...])])
so we extract the few fields we need with regexes anchored on the stable class names.
`previous_seasons` is ordered most-recent-first and is Solo/Duo-only with no null
gaps, so [0] is the placement we want. We read the date from `previous_season_tiers`
anchored on that exact tier+division (OP.GG's projected output mislabels the queue
of null entries, so we never trust the SOLORANKED label by itself).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

ENDPOINT = "https://mcp-api.op.gg/mcp"

# Current LoL runs ~3 ranked splits per year, so treat a "season" as ~120 days
# of real elapsed time for the purpose of decaying a stale placement.
SEASON_LENGTH_DAYS = 120

# OP.GG encodes division as an int (1=I/top .. 4=IV/bottom; 5=V only on the
# pre-2019 5-division ladder). seed_from_rank ignores division for apex tiers.
_DIVISION_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}

# Riot platform routes -> OP.GG region slugs.
_REGION_MAP = {
    "na1": "na", "euw1": "euw", "eun1": "eune", "kr": "kr", "br1": "br",
    "jp1": "jp", "la1": "lan", "la2": "las", "oc1": "oce", "tr1": "tr",
    "ru": "ru", "ph2": "ph", "sg2": "sg", "th2": "th", "tw2": "tw", "vn2": "vn",
}

# Only the fields we parse. Keeps the payload small; we anchor the date lookup on
# tier+division, so we don't request the (unreliable) game_type label.
_DESIRED_FIELDS = [
    "data.summoner.previous_seasons[].season_id",
    "data.summoner.previous_seasons[].tier_info.tier",
    "data.summoner.previous_seasons[].tier_info.division",
    "data.summoner.previous_season_tiers[].rank_entries[].rank_info.tier",
    "data.summoner.previous_season_tiers[].rank_entries[].rank_info.division",
    "data.summoner.previous_season_tiers[].rank_entries[].rank_info.created_at",
]

# `PreviousSeason(<id>,TierInfo("<TIER>",<div>...))` — NOT PreviousSeasonTier
# (the `\(` right after the name prevents matching the longer class name).
_PREV_SEASON_RE = re.compile(r'PreviousSeason\((\d+),\w+\("([A-Z]+)",(\d+)')


def opgg_region(platform_route: str) -> str:
    """Map a Riot platform route (e.g. 'na1') to an OP.GG region slug ('na')."""
    p = (platform_route or "").lower()
    if p in _REGION_MAP:
        return _REGION_MAP[p]
    return re.sub(r"\d+$", "", p) or p


@dataclass
class PastSeasonRank:
    tier: str            # e.g. "EMERALD"
    division: str        # e.g. "III"
    season_id: int       # OP.GG internal season id of the placement
    seasons_elapsed: int  # whole ~120-day seasons between the placement and now


def _seasons_since(created_at_iso: str) -> int:
    """How many whole ~120-day seasons have elapsed since an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(created_at_iso)
    except (ValueError, TypeError):
        return 0
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now(timezone.utc)
    days = (now - dt).days
    if days <= 0:
        return 0
    return days // SEASON_LENGTH_DAYS


def parse_past_season(text: str) -> Optional[PastSeasonRank]:
    """Extract the most-recent Solo/Duo past-season rank from OP.GG's class-repr text."""
    m = _PREV_SEASON_RE.search(text)
    if not m:
        return None
    season_id = int(m.group(1))
    tier = m.group(2).upper()
    div_int = int(m.group(3))
    division = _DIVISION_ROMAN.get(div_int, "IV")

    # Find the placement date for this exact tier+division. RankInfo (with a date)
    # only appears in previous_season_tiers; the first match is the most recent.
    date_re = re.compile(
        r'RankInfo\("' + re.escape(tier) + r'",' + re.escape(str(div_int)) + r',"([^"]+)"\)'
    )
    dm = date_re.search(text)
    seasons_elapsed = _seasons_since(dm.group(1)) if dm else 0

    return PastSeasonRank(
        tier=tier, division=division, season_id=season_id, seasons_elapsed=seasons_elapsed
    )


class OpggClient:
    def __init__(self, endpoint: str = ENDPOINT, timeout: float = 15.0):
        self.endpoint = endpoint
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _parse_rpc(resp: httpx.Response) -> dict[str, Any]:
        """Streamable HTTP responses come back as JSON or as SSE; handle both."""
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            last: dict[str, Any] = {}
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk and chunk != "[DONE]":
                        import json
                        try:
                            last = json.loads(chunk)
                        except ValueError:
                            pass
            return last
        return resp.json()

    async def get_past_season_rank(
        self, game_name: str, tag_line: str, region: str
    ) -> Optional[PastSeasonRank]:
        """Return the player's most recent past-season Solo/Duo rank, or None if
        OP.GG has no history (or the call fails — we never raise to the caller)."""
        if not game_name or not tag_line:
            return None
        try:
            await self._client.post(self.endpoint, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "inhouse-bot", "version": "1.0"},
                },
            })
            await self._client.post(self.endpoint, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })
            resp = await self._client.post(self.endpoint, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {
                    "name": "lol_get_summoner_profile",
                    "arguments": {
                        "region": opgg_region(region),
                        "game_name": game_name,
                        "tag_line": tag_line,
                        "desired_output_fields": _DESIRED_FIELDS,
                    },
                },
            })
            data = self._parse_rpc(resp)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("OP.GG lookup failed for %s#%s: %s", game_name, tag_line, e)
            return None

        if data.get("error"):
            log.warning("OP.GG returned error for %s#%s: %s", game_name, tag_line, data["error"])
            return None

        blocks = data.get("result", {}).get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text:
            return None
        return parse_past_season(text)
