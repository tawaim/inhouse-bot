"""Keeping the recruit channel's pins current: the ongoing recruitment or teams
post is pinned, and the bot's posts for past events are unpinned."""
from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)


async def pin_current(message, bot_user) -> None:
    """Pin `message` and unpin everything else the bot has pinned in that
    channel. Pins made by people are left alone. Never raises: a pin that
    fails (e.g. the bot can't manage messages there) must not undo the post."""
    channel = message.channel
    bot_id = getattr(bot_user, "id", None)
    try:
        for old in await channel.pins():
            if old.id != message.id and getattr(old.author, "id", None) == bot_id:
                await old.unpin(reason="Past inhouse event")
        await message.pin(reason="Current inhouse event")
    except discord.HTTPException:
        log.warning("Couldn't update the pins in channel %s", getattr(channel, "id", "?"), exc_info=True)
