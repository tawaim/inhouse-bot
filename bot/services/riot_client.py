"""Async wrapper for the Riot Games API.

Endpoints used:
  - account-v1: Riot ID -> PUUID  (regional route)
  - league-v4: ranked entries by PUUID  (platform route)
  - match-v5: match list & detail by PUUID  (regional route)

Rate limits on a personal key:
  20 requests / 1 second
  100 requests / 2 minutes
We use a simple async semaphore + sleep on 429.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)


@dataclass
class RiotAccount:
    puuid: str
    game_name: str
    tag_line: str


@dataclass
class RankEntry:
    queue_type: str   # "RANKED_SOLO_5x5" or "RANKED_FLEX_SR"
    tier: str         # "GOLD"
    rank: str         # "II"
    league_points: int
    wins: int
    losses: int


@dataclass
class MatchSummary:
    """Subset of match-v5 fields we care about for skill signals."""
    match_id: str
    queue_id: int
    role: Optional[str]      # "TOP" / "JUNGLE" / "MIDDLE" / "BOTTOM" / "UTILITY"
    champion: str
    kills: int
    deaths: int
    assists: int
    won: bool


# Map Riot's role naming to ours
RIOT_ROLE_MAP = {
    "TOP": "TOP",
    "JUNGLE": "JUNGLE",
    "MIDDLE": "MID",
    "BOTTOM": "BOT",
    "UTILITY": "SUPPORT",
}


class RiotClient:
    def __init__(self, api_key: str, region: str = "na1", regional_route: str = "americas"):
        self.api_key = api_key
        self.region = region
        self.regional_route = regional_route
        self._client = httpx.AsyncClient(
            headers={"X-Riot-Token": api_key},
            timeout=httpx.Timeout(10.0),
        )
        # Conservative concurrency cap so we don't blow the burst limit
        self._sem = asyncio.Semaphore(10)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: Optional[dict[str, Any]] = None) -> Optional[Any]:
        """GET with rate-limit handling. Returns parsed JSON (dict or list), or None on 404."""
        async with self._sem:
            for attempt in range(3):
                resp = await self._client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "1"))
                    log.warning("Riot 429, sleeping %ss", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code in (500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt)
                    continue
                if resp.status_code in (401, 403):
                    log.error("Riot auth error %s on %s — key may be expired", resp.status_code, url)
                    raise RiotAuthError(f"Auth failed: {resp.status_code}")
                resp.raise_for_status()
            log.error("Riot API exhausted retries for %s", url)
            return None

    # --- account-v1 (regional) ---

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> Optional[RiotAccount]:
        url = (
            f"https://{self.regional_route}.api.riotgames.com"
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        data = await self._get(url)
        if not data:
            return None
        return RiotAccount(
            puuid=data["puuid"],
            game_name=data["gameName"],
            tag_line=data["tagLine"],
        )

    # --- league-v4 (platform) ---

    async def get_ranked_entries(self, puuid: str) -> list[RankEntry]:
        url = (
            f"https://{self.region}.api.riotgames.com"
            f"/lol/league/v4/entries/by-puuid/{puuid}"
        )
        data = await self._get(url)
        if not data:
            return []
        return [
            RankEntry(
                queue_type=e["queueType"],
                tier=e["tier"],
                rank=e["rank"],
                league_points=e["leaguePoints"],
                wins=e["wins"],
                losses=e["losses"],
            )
            for e in data
        ]

    async def get_solo_rank(self, puuid: str) -> Optional[RankEntry]:
        for entry in await self.get_ranked_entries(puuid):
            if entry.queue_type == "RANKED_SOLO_5x5":
                return entry
        return None

    # --- match-v5 (regional) ---

    async def get_recent_match_ids(self, puuid: str, count: int = 20, queue: int = 420) -> list[str]:
        """queue=420 is Ranked Solo/Duo. Pass queue=None for all."""
        url = (
            f"https://{self.regional_route}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        )
        params: dict[str, Any] = {"count": count}
        if queue:
            params["queue"] = queue
        # Route through _get so 429/5xx are retried with backoff, like every other call.
        data = await self._get(url, params=params)
        return data or []

    async def get_match_summary(self, match_id: str, puuid: str) -> Optional[MatchSummary]:
        url = (
            f"https://{self.regional_route}.api.riotgames.com"
            f"/lol/match/v5/matches/{match_id}"
        )
        data = await self._get(url)
        if not data:
            return None
        # Find the participant matching this puuid
        for p in data["info"]["participants"]:
            if p["puuid"] != puuid:
                continue
            return MatchSummary(
                match_id=match_id,
                queue_id=data["info"]["queueId"],
                role=RIOT_ROLE_MAP.get(p.get("teamPosition", "")),
                champion=p["championName"],
                kills=p["kills"],
                deaths=p["deaths"],
                assists=p["assists"],
                won=p["win"],
            )
        return None

    async def infer_primary_role(self, puuid: str, sample: int = 20) -> Optional[str]:
        """Return the role most-played in the player's recent ranked games."""
        match_ids = await self.get_recent_match_ids(puuid, count=sample, queue=420)
        if not match_ids:
            return None
        role_counts: dict[str, int] = {}
        # Fetch up to `sample` matches concurrently (semaphore bounds it)
        results = await asyncio.gather(
            *(self.get_match_summary(mid, puuid) for mid in match_ids),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, MatchSummary) and r.role:
                role_counts[r.role] = role_counts.get(r.role, 0) + 1
        if not role_counts:
            return None
        return max(role_counts, key=role_counts.get)


class RiotAuthError(Exception):
    """Raised when the Riot API rejects our key (401/403)."""
