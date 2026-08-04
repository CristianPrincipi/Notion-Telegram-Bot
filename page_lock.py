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

ALWAYS KEY ON THE DATABASE, NEVER ON THE PAGE
---------------------------------------------
Every key passed to page_lock() is a DATABASE id. That is a rule, not a
convention, and it exists for two reasons:

  1. The page id is usually not known until the lookup the lock has to cover,
     and on a first run the page does not exist at all — so a run that keys on
     the page id has to do its find-or-create OUTSIDE the lock, and two runs can
     then both decide to create one. That is not hypothetical: the Diet flow
     shipped that way and would have built two Diet pages, each with a full
     skeleton, the first time two Implements overlapped.
  2. It keeps the key space bounded by the number of configured databases, so
     the table below cannot grow without bound. Keying on anything
     user-controlled — an expense name, a book title — would let a long-running
     process accumulate a lock per distinct string ever used.

There is at most one Manual per area database and one Diet page per Diet
database, so locking the database is the same granularity as locking the page,
and it covers the create race for free. For the expense commands it is coarser —
all expense writes serialise — which costs nothing here: they take about a
second and David has exactly one user.

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

# For the SHORT write cycles (expenses, reminders) the trade-off inverts. Those
# finish in about a second, so waiting is invisible and refusing would be
# actively wrong — firing off two expense edits in a row is normal use, not a
# collision to report. Long enough to queue a handful of them, short enough that
# a genuinely wedged holder still surfaces as an error rather than a hang.
WRITE_LOCK_TIMEOUT_SECONDS = 15.0


class PageBusy(Exception):
    """Another update to the same target is already running."""


@asynccontextmanager
async def page_lock(key: str, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Serialise the read-merge-write cycle for one target.

    Raises PageBusy if the lock is not free within `timeout`, so the caller can
    tell the user rather than queue silently.

    `key` is always a DATABASE id — see the module docstring for why that is a
    rule and not a preference. The find-or-create for the target page must happen
    INSIDE the lock, or two runs can both decide to create it.
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
