"""Running a long command without holding up the queue."""

from telegram import Update
from telegram.ext import ContextTypes


def run_detached(context: ContextTypes.DEFAULT_TYPE, update: Update, coro, name: str):
    """Run a long command as a background task instead of awaiting it inline.

    WHY THIS EXISTS
    ---------------
    Moving blocking work onto worker threads freed the event LOOP, but
    python-telegram-bot still processes updates one at a time: it will not look
    at the next update until this handler returns. So a five-minute `Learn video`
    left every other command sitting in the queue behind it, even though the loop
    itself was idle the whole time.

    Detaching only the genuinely long commands fixes that without enabling
    concurrent_updates. Everything else — expenses, budget, quotes, books,
    reminders, diagnostics — stays strictly sequential, so a write followed by a
    read of the same data still cannot be reordered. `Add e` then `B` always
    reports the new total. That guarantee is the reason this is a per-command
    decision and not a global switch.

    WHAT THIS ALLOWS TO OVERLAP
    ---------------------------
    Two detached commands can now run at once, which is exactly what the locks
    are for: two Implements against the same area are refused by the area lock,
    and two against Diet by the Diet lock. Nothing detached here touches the
    expense or calendar paths.

    Uses Application.create_task rather than asyncio.create_task so exceptions
    reach the global error handler instead of vanishing into a dropped task, and
    so a task still in flight is awaited on shutdown rather than killed
    mid-write. Passing `update` is what gives that error handler its context.
    """
    return context.application.create_task(coro, update=update, name=name)
