"""Bot entrypoint. Run with `python -m bot.main`."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
from discord.ext import commands

from bot.cogs.admin import AdminCog
from bot.cogs.linking import LinkingCog
from bot.cogs.recruitment import RecruitmentCog
from bot.cogs.stats import StatsCog
from bot.config import Config
from bot.db.session import close_db, init_db
from bot.services.riot_client import RiotClient
from bot.services.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")


async def amain() -> None:
    config = Config.load()
    await init_db(config)

    intents = discord.Intents.default()
    intents.message_content = False     # we use slash commands + buttons, no message content needed
    intents.members = True              # for role checks

    bot = commands.Bot(command_prefix="!", intents=intents)
    riot = RiotClient(
        api_key=config.riot_api_key,
        region=config.riot_region,
        regional_route=config.riot_regional_route,
    )

    # Register cogs
    await bot.add_cog(LinkingCog(bot, config, riot))
    await bot.add_cog(RecruitmentCog(bot, config))
    await bot.add_cog(AdminCog(bot, config, riot))
    await bot.add_cog(StatsCog(bot))

    @bot.event
    async def on_ready():
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)
        # Sync slash commands to the configured guild for fast iteration
        guild = discord.Object(id=config.discord_guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("Synced %d slash commands to guild %s", len(synced), config.discord_guild_id)

    scheduler = setup_scheduler(bot, config)
    scheduler.start()
    log.info("Scheduler started; jobs: %s", [j.id for j in scheduler.get_jobs()])

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _stop(*_):
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass  # Windows

    bot_task = asyncio.create_task(bot.start(config.discord_token))
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    log.info("Shutting down...")
    scheduler.shutdown(wait=False)
    await bot.close()
    await riot.close()
    await close_db()
    for task in pending:
        task.cancel()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
