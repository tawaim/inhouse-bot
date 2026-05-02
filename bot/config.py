"""Centralized configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@dataclass(frozen=True)
class Config:
    discord_token: str
    discord_guild_id: int
    owner_discord_id: int  # who receives link approval DMs
    riot_api_key: str
    riot_region: str          # platform route, e.g. "na1"
    riot_regional_route: str  # regional route, e.g. "americas"
    admin_role_name: str
    database_url: str
    timezone: str

    @classmethod
    def load(cls) -> "Config":
        # Make sure ./data exists for sqlite
        Path("data").mkdir(exist_ok=True)
        return cls(
            discord_token=_required("DISCORD_TOKEN"),
            discord_guild_id=int(_required("DISCORD_GUILD_ID")),
            owner_discord_id=int(_required("OWNER_DISCORD_ID")),
            riot_api_key=_required("RIOT_API_KEY"),
            riot_region=os.getenv("RIOT_REGION", "na1"),
            riot_regional_route=os.getenv("RIOT_REGIONAL_ROUTE", "americas"),
            admin_role_name=os.getenv("ADMIN_ROLE_NAME", "League Admin"),
            database_url=os.getenv(
                "DATABASE_URL", "sqlite+aiosqlite:///./data/inhouse.db"
            ),
            timezone=os.getenv("TZ", "America/New_York"),
        )


# Role emoji mapping — used by recruitment reactions
ROLE_EMOJIS = {
    "TOP": "🛡️",
    "JUNGLE": "🌲",
    "MID": "⚔️",
    "BOT": "🏹",
    "SUPPORT": "💚",
    "FILL": "🎲",
}
EMOJI_TO_ROLE = {v: k for k, v in ROLE_EMOJIS.items()}
ROLES = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]  # ordered, no FILL
