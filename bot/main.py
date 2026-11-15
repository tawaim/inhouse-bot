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
from bot.services.opgg_client import OpggClient
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

    # Slash commands only. A text prefix like "!" needs the message_content intent
    # (discord.py warns on every boot without it); mention-only needs nothing.
    bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
    riot = RiotClient(
        api_key=config.riot_api_key,
        region=config.riot_region,
        regional_route=config.riot_regional_route,
    )
    # OP.GG supplies past-season ranks (Riot's API can't) for seeding unranked players.
    opgg = OpggClient()

    # Register cogs
    await bot.add_cog(LinkingCog(bot, config, riot, opgg))
    await bot.add_cog(RecruitmentCog(bot, config))
    await bot.add_cog(AdminCog(bot, config, riot, opgg))
    await bot.add_cog(StatsCog(bot))

    commands_synced = False

    @bot.event
    async def on_ready():
        nonlocal commands_synced
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)
        # on_ready fires again after every gateway reconnect; the command list
        # can't change within a process, so sync once (retrying if it failed).
        if commands_synced:
            return
        # Sync slash commands to the configured guild for fast iteration
        guild = discord.Object(id=config.discord_guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        commands_synced = True
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
    await opgg.close()
    await close_db()
    for task in pending:
        task.cancel()

    # If the bot itself died (bad token, privileged intent not enabled, ...),
    # surface it: otherwise the exception is never retrieved, the process exits
    # 0 and nothing looks wrong. Re-raising makes the exit code non-zero.
    if bot_task in done and not bot_task.cancelled() and bot_task.exception() is not None:
        log.error("Bot stopped with an error: %r", bot_task.exception())
        raise bot_task.exception()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
