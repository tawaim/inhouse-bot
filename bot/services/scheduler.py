"""Scheduler: cron jobs for recruitment lifecycle.

  Friday  09:00 ET  ->  post recruitment for the Thursday 13 days from now
  Monday  21:30 ET  ->  close THIS Thursday's session, run matchmaker, post teams
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import discord
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from bot.config import Config
from bot.db.models import Session as InhouseSession
from bot.db.session import get_session

log = logging.getLogger(__name__)


def next_thursday_after(d: date, days_ahead: int = 13) -> date:
    """Return the date `days_ahead` days from `d`, then snap to next Thursday
    if it isn't one. With days_ahead=13 from a Friday, the result IS the
    Thursday-after-next, no snap needed — but we snap defensively in case
    the job runs on the wrong day (e.g., admin manually fires it).
    """
    target = d + timedelta(days=days_ahead)
    # Thursday is weekday 3 (Mon=0)
    days_to_thursday = (3 - target.weekday()) % 7
    return target + timedelta(days=days_to_thursday)


def setup_scheduler(bot: discord.Client, config: Config) -> AsyncIOScheduler:
    tz = pytz.timezone(config.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    async def friday_recruit_job() -> None:
        from bot.cogs.recruitment import RecruitmentCog
        cog = bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            log.error("RecruitmentCog not loaded; skipping recruit job")
            return
        try:
            # Posts the newly-opened Thursday (and backfills any earlier missed one).
            posted = await cog.ensure_upcoming_recruitments()
            log.info("Friday recruit job posted %d recruitment(s)", posted)
        except Exception:
            log.exception("Failed to post recruitment")

    async def monday_close_job() -> None:
        from bot.cogs.recruitment import RecruitmentCog
        cog = bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            log.error("RecruitmentCog not loaded; skipping close job")
            return
        # Find the session whose Thursday is THIS week (3 days from Monday)
        today = datetime.now(tz).date()
        this_thursday = today + timedelta(days=(3 - today.weekday()) % 7)
        async with get_session() as db:
            stmt = select(InhouseSession).where(
                InhouseSession.game_date == this_thursday,
                InhouseSession.status == "recruiting",
            )
            session = (await db.execute(stmt)).scalar_one_or_none()
        if session is None:
            log.info("No active recruiting session for %s; nothing to close", this_thursday)
            return
        try:
            await cog.close_signups_and_match(session.id)
            log.info("Closed and matched session %d (%s)", session.id, this_thursday)
        except Exception:
            log.exception("Failed to close session %d", session.id)

    # misfire_grace_time lets a run that's slightly late (busy loop, brief downtime)
    # still fire instead of being dropped; coalesce collapses pile-ups into one run.
    # (Hard catch-up across longer downtime is handled by the startup reconciliation
    # in ensure_upcoming_recruitments — the in-memory job store has no run history.)
    # Friday 9:00 AM (Mon=0..Sun=6, Friday=4)
    scheduler.add_job(
        friday_recruit_job,
        CronTrigger(day_of_week="fri", hour=9, minute=0),
        id="friday_recruit",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
    )
    # Monday 9:30 PM
    scheduler.add_job(
        monday_close_job,
        CronTrigger(day_of_week="mon", hour=21, minute=30),
        id="monday_close",
        replace_existing=True,
        misfire_grace_time=3 * 3600,
        coalesce=True,
    )

    return scheduler
