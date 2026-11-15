"""Scheduler: background jobs for the recruitment lifecycle.

  every 5 min        ->  close any recruitment whose signup close time has arrived,
                         run the matchmaker and DM the owner the options
  Monday 18:00 ET    ->  refresh everyone's base seed from their current Riot rank

The close job is a poll rather than a cron at the close time. Each recruitment
stores its own close time (/recruit-now's close_day), and a poll means a close
that was missed — because the bot was restarting at 9:30 PM, or because fewer
than ten people had signed up yet — is simply picked up on the next run.
close_signups_and_match is safe to call repeatedly.

Recruitments are posted manually via /recruit-now — there is no auto-post.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import discord
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from bot.config import Config
from bot.db.models import Session as InhouseSession
from bot.db.session import get_session

log = logging.getLogger(__name__)

# Tolerance for a run that fires a hair before the stored close time.
_DUE_SLACK = timedelta(minutes=1)
# Open sessions are abandoned this long after their game date.
_STALE_AFTER = timedelta(days=1)
CLOSE_POLL_MINUTES = 5


def signups_due(signups_close_at: datetime | None, now_utc: datetime) -> bool:
    """True once a session's scheduled close (naive UTC) has arrived. Open-ended
    sessions (no close time) are never due; they close only via an admin command."""
    return signups_close_at is not None and signups_close_at <= now_utc + _DUE_SLACK


async def run_due_closes(cog, now_utc: datetime, today: date) -> list[tuple[int, str]]:
    """One pass of the close job. Returns [(session_id, outcome), ...] for the
    sessions it acted on (outcomes are close_signups_and_match's, or "expired").

    - Sessions still open more than a day after their game date are marked
      cancelled, so leftovers never linger as the "active" session.
    - Recruiting sessions whose close time has arrived are closed and matched.
      Under ten signups leaves the session open; it is retried on the next pass.
    """
    results: list[tuple[int, str]] = []
    async with get_session() as db:
        open_sessions = (await db.execute(
            select(InhouseSession)
            .where(InhouseSession.status.in_(["recruiting", "closed"]))
            .order_by(InhouseSession.game_date.asc())
        )).scalars().all()
        due_ids = []
        for s in open_sessions:
            if s.game_date < today - _STALE_AFTER:
                s.status = "cancelled"
                results.append((s.id, "expired"))
                log.info("Session %d (%s) expired without a match; marked cancelled", s.id, s.game_date)
            elif s.status == "recruiting" and s.game_date >= today and signups_due(s.signups_close_at, now_utc):
                due_ids.append(s.id)
        await db.commit()

    for sid in due_ids:
        try:
            outcome = await cog.close_signups_and_match(sid)
        except Exception:
            log.exception("Failed to close session %d", sid)
            outcome = "error"
        results.append((sid, outcome))
        if outcome != "waiting":  # "waiting" repeats every pass until ten are in
            log.info("Close job: session %d -> %s", sid, outcome)
    return results


def setup_scheduler(bot: discord.Client, config: Config) -> AsyncIOScheduler:
    tz = pytz.timezone(config.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    async def close_job() -> None:
        from bot.cogs.recruitment import RecruitmentCog
        cog = bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            log.error("RecruitmentCog not loaded; skipping close job")
            return
        await run_due_closes(cog, datetime.utcnow(), datetime.now(tz).date())

    async def rank_sync_job() -> None:
        admin_cog = bot.get_cog("AdminCog")
        if admin_cog is None:
            return
        try:
            result = await admin_cog._refresh_all_base_seeds()
            log.info("Weekly rank sync: %s", result)
        except Exception:
            log.exception("Weekly rank sync failed; seeds left as they were")

    # First run shortly after boot (catches a close missed during a restart),
    # then every few minutes.
    scheduler.add_job(
        close_job,
        IntervalTrigger(minutes=CLOSE_POLL_MINUTES),
        id="signup_close",
        replace_existing=True,
        next_run_time=datetime.now(tz) + timedelta(seconds=45),
        coalesce=True,
        max_instances=1,
    )
    # Ranks used to refresh only when a session closed, so quiet weeks left seeds
    # stale for months. This runs regardless of whether a game is scheduled.
    scheduler.add_job(
        rank_sync_job,
        CronTrigger(day_of_week="mon", hour=18, minute=0),
        id="weekly_rank_sync",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
        coalesce=True,
    )

    return scheduler
