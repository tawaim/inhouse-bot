"""Scheduler: cron jobs for recruitment lifecycle.

  Mon/Tue/Wed  21:30 ET  ->  if THIS Thursday's session has reached its signup
                             close time: close it, run matchmaker, post teams

Each recruitment stores its own close time (Monday by default; /recruit-now's
close_day can pick Tuesday or Wednesday), so the job runs all three evenings and
only acts once that time has arrived.

Recruitments are posted manually via /recruit-now — there is no auto-post.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import discord
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from bot.config import Config
from bot.db.models import Session as InhouseSession
from bot.db.session import get_session

log = logging.getLogger(__name__)

# Tolerance for a run that fires a hair before the stored close time.
_DUE_SLACK = timedelta(minutes=5)


def signups_due(signups_close_at: datetime | None, now_utc: datetime) -> bool:
    """True once a session's scheduled close (naive UTC) has arrived. Open-ended
    sessions (no close time) are never due; they close only via /close-signups."""
    return signups_close_at is not None and signups_close_at <= now_utc + _DUE_SLACK


def setup_scheduler(bot: discord.Client, config: Config) -> AsyncIOScheduler:
    tz = pytz.timezone(config.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    async def monday_close_job() -> None:
        from bot.cogs.recruitment import RecruitmentCog
        cog = bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            log.error("RecruitmentCog not loaded; skipping close job")
            return
        # Find the session whose Thursday is THIS week
        today = datetime.now(tz).date()
        this_thursday = today + timedelta(days=(3 - today.weekday()) % 7)
        async with get_session() as db:
            stmt = select(InhouseSession).where(
                InhouseSession.game_date == this_thursday,
                InhouseSession.status == "recruiting",
                # Open-ended sessions (no scheduled close) are closed only via
                # /close-signups, so the auto-close skips them.
                InhouseSession.signups_close_at.is_not(None),
            )
            session = (await db.execute(stmt)).scalar_one_or_none()
        if session is not None and not signups_due(session.signups_close_at, datetime.utcnow()):
            log.info("Session %d closes at %s UTC; not due yet", session.id, session.signups_close_at)
            return
        if session is None:
            log.info("No auto-closing recruiting session for %s; nothing to close", this_thursday)
            return
        try:
            await cog.close_signups_and_match(session.id)
            log.info("Closed and matched session %d (%s)", session.id, this_thursday)
        except Exception:
            log.exception("Failed to close session %d", session.id)

    # misfire_grace_time lets a run that's slightly late (busy loop, brief downtime)
    # still fire instead of being dropped; coalesce collapses pile-ups into one run.
    # Mon/Tue/Wed 9:30 PM (job id kept for continuity)
    scheduler.add_job(
        monday_close_job,
        CronTrigger(day_of_week="mon,tue,wed", hour=21, minute=30),
        id="monday_close",
        replace_existing=True,
        misfire_grace_time=3 * 3600,
        coalesce=True,
    )

    return scheduler
