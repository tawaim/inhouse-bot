"""One lock for every write to the `ratings` table.

Committing a result, un-reporting one and rescoring one each read ratings, do
maths on them and write them back. Two admins acting at the same moment could
interleave those steps and lose an update, so every such operation runs under
this lock:

    from bot.services.ratings_lock import ratings_lock

    async with ratings_lock():
        async with get_session() as db:
            ...read ratings, write ratings, commit...

The lock is NOT re-entrant: take it once at the outermost level and pass the open
session down (see AdminCog._commit_games / _revert_result's `db=` argument).

The bot runs a single event loop, so in production this is one module-level
asyncio.Lock. It is keyed by the running loop only because an asyncio.Lock binds
to the loop it was first contended on, and the test-suite starts a fresh loop per
test.
"""
from __future__ import annotations

import asyncio
import weakref

_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def ratings_lock() -> asyncio.Lock:
    """The rating-write lock for the running event loop."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    return lock
