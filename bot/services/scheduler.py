"""Scheduler: cron jobs for recruitment lifecycle.

  Monday  21:30 ET  ->  close THIS Thursday's session, run matchmaker, post teams

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


def setup_scheduler(bot: discord.Client, config: Config) -> AsyncIOScheduler:
    tz = pytz.timezone(config.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

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
                # Open-ended sessions (no scheduled close) are closed only via
                # /close-signups, so the Monday auto-close skips them.
                InhouseSession.signups_close_at.is_not(None),
            )
            session = (await db.execute(stmt)).scalar_one_or_none()
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
