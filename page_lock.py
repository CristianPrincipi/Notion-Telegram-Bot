"""Per-target write locks for the Implement commands.

WHY THIS EXISTS
---------------
Notion has no transactions and no compare-and-set. An Implement run is a
read-merge-write cycle — read the current Manual, ask Claude to merge the new
material into it, write the result back — and the Claude call in the middle
takes tens of seconds. Two runs against the same Manual overlapping in that
window both read the same "before" state, and whichever writes second silently
discards the other's merge. Nothing errors; the content is just gone.

Serialising the whole cycle per target is the only fix available.

IN-PROCESS ONLY
---------------
These locks live in this module's memory, so they serialise within a single
Python process. David runs as one Railway worker (`worker: python david.py`),
which is why that is enough today. Running a second instance — a scaled dyno, a
blue/green deploy overlapping two containers — would leave each with its own
lock table and no mutual exclusion at all. That needs a shared lock: Redis
SETNX with a TTL, or a Notion property used as a mutex with a stale-lock
timeout.
"""

import asyncio
from contextlib import asynccontextmanager

# Held for the duration of a read-merge-write. Locks are created on demand and
# never evicted: one asyncio.Lock is a few dozen bytes and the key set is
# bounded by the number of areas configured, so the table cannot grow unbounded.
_locks: dict[str, asyncio.Lock] = {}

# Deliberately short. The point is to REFUSE a concurrent run, not to queue it:
# a merge takes tens of seconds, so silently waiting would look like the bot had
# hung. This value only absorbs a momentary overlap between two near-instant
# acquisitions.
LOCK_TIMEOUT_SECONDS = 2.0


class PageBusy(Exception):
    """Another update to the same target is already running."""


@asynccontextmanager
async def page_lock(key: str, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Serialise the read-merge-write cycle for one target.

    Raises PageBusy if the lock is not free within `timeout`, so the caller can
    tell the user rather than queue silently.

    `key` identifies the TARGET DOCUMENT, not necessarily a page ID: for the
    Manual flow it is the area database ID, because the Manual's own page ID is
    not known until the lookup that this lock has to cover — and on a first run
    the page does not exist yet, so keying on it would let two runs both decide
    to create one. There is exactly one Manual per area database, so locking the
    area is the same granularity, and it covers the create race too.
    """
    lock = _locks.setdefault(key, asyncio.Lock())

    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        raise PageBusy(key) from None

    try:
        yield
    finally:
        lock.release()
