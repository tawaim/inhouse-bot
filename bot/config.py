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

ROLE_SHORT = {"TOP": "TOP", "JUNGLE": "JUN", "MID": "MID", "BOT": "BOT", "SUPPORT": "SUP"}
NAME_MAX = 14
# The key for rosters rendered with both elo maps (see format_team_lines).
ELO_KEY = "Key: name - role elo (INHOUSE elo)"


def format_team_lines(
    by_role: dict[str, int],
    emoji: bool = True,
    name_map: dict[int, str] | None = None,
    elo_map: dict[int, float] | None = None,
    inhouse_map: dict[int, float] | None = None,
) -> str:
    """Render a team roster top-down: TOP, JUN, MID, BOT, SUP.

    If name_map is provided, names are shown as plain text truncated to NAME_MAX
    characters instead of Discord mentions. If elo_map is provided, each name is
    followed by " - <role elo>" (the caller passes each player's elo in the role
    they are seated in), and by " (<INHOUSE elo>)" as well when inhouse_map is
    provided: the layout ELO_KEY describes.
    """
    out = []
    for role in ROLES:
        if role not in by_role:
            continue
        uid = by_role[role]
        prefix = f"{ROLE_EMOJIS[role]} " if emoji else ""
        if name_map is not None:
            name = name_map.get(uid, str(uid))
            if len(name) > NAME_MAX:
                name = name[:NAME_MAX - 1] + "…"
        else:
            name = f"<@{uid}>"
        elo = ""
        if elo_map is not None and uid in elo_map:
            elo = f" - {elo_map[uid]:.0f}"
            if inhouse_map is not None and uid in inhouse_map:
                elo += f" ({inhouse_map[uid]:.0f})"
        out.append(f"{prefix}**{ROLE_SHORT[role]}**: {name}{elo}")
    return "\n".join(out)
