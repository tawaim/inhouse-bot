"""Recruitment sessions: when signups close, which session counts as "current",
and which signups get matched. No Discord — queries and pure functions only."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import select

from bot.db.models import Player, Session as InhouseSession, Signup


# Which evening (9:30 PM ET) of game week signups auto-close: days before Thursday.
CLOSE_DAY_OFFSETS = {"Monday": 3, "Tuesday": 2, "Wednesday": 1}


def signups_close_local(game_date: date, close_day: str, tz) -> datetime:
    """The tz-aware 9:30 PM close time on `close_day` of the game's week."""
    day = game_date - timedelta(days=CLOSE_DAY_OFFSETS[close_day])
    return tz.localize(datetime.combine(day, datetime.min.time()).replace(hour=21, minute=30))


# A session is "current" from when it is posted until the day after its game date.
# Anything older is stale and is never picked as a default target for a command.
CURRENT_GRACE_DAYS = 1


async def find_current_session(db, statuses, today: date) -> Optional[InhouseSession]:
    """The soonest session in `statuses` that hasn't gone stale, or None.

    Commands that act on "the active session" use this instead of "the oldest
    session with that status", which used to pick up months-old leftovers.
    """
    return (await db.execute(
        select(InhouseSession)
        .where(InhouseSession.status.in_(list(statuses)))
        .where(InhouseSession.game_date >= today - timedelta(days=CURRENT_GRACE_DAYS))
        .order_by(InhouseSession.game_date.asc())
    )).scalars().first()


# Allorim is held out of the game unless he's needed to field a full 10 — i.e.
# he only plays when there are exactly 10 signups and there's no one else to
# bench. Matched on the Riot account (game name + tagline) so it tracks the
# person regardless of Discord handle changes.
ALLORIM_GAME_NAME = "allorim"
ALLORIM_TAG_LINE = "na1"


def _is_allorim(player: Optional[Player]) -> bool:
    return (
        player is not None
        and (player.riot_game_name or "").lower() == ALLORIM_GAME_NAME
        and (player.riot_tag_line or "").lower() == ALLORIM_TAG_LINE
    )


def _select_playing(
    playing_signups: list[Signup], players_by_id: dict[int, Player]
) -> list[Signup]:
    """Pick the (up to 10) signups to actually matchmake, in signup order.

    Standard rule is first-10-by-signup-time, with one override: Allorim is
    benched whenever there are 10 or more other players to field a game from.
    He's only pulled in when removing him would drop the count below 10.
    """
    ordered = sorted(playing_signups, key=lambda s: s.signed_up_at)
    others = [s for s in ordered if not _is_allorim(players_by_id.get(s.discord_id))]
    allorim = [s for s in ordered if _is_allorim(players_by_id.get(s.discord_id))]
    pool = others if len(others) >= 10 else others + allorim
    return pool[:10]
