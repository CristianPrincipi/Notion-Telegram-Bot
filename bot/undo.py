"""`undo` — reverse the last destructive thing David did, whatever it was.

WHY THIS IS ITS OWN MODULE NOW. `cmd_undo` used to live in `bot/expenses.py` and
call `services/expenses.run_undo` unconditionally, because an expense write was
the only destructive write there was. `Cancel` is the second, and the command
that reverses it is the same word.

WHAT THIS DECIDES, AND WHY THAT IS ALLOWED HERE. Nothing about how to reverse
anything — it reads which KIND owns the stored reversal and calls that service,
which consumes the record itself. That is dispatch, the same shape as david.py's
registry and its pending-number routing, not a rule about the work. The table
below is the whole of it, and adding a third destructive command means adding a
row rather than editing a branch.

WHY IT PEEKS RATHER THAN CONSUMES. Taking the record here would mean handing it
down to a service that no longer owns it — and every one of those services puts
the record BACK when the reversal fails or the lock is busy, so that `undo` stays
answerable. Peeking keeps the take-and-restore pair inside the module that knows
when a reversal did not happen.
"""

import calendar_safety
import expense_safety
import pending_choice
from bot.notify import for_update
from services import cancel, expenses

# kind → the service that reverses it. One row per destructive feature.
REVERSERS = {
    expense_safety.KIND:  expenses.run_undo,
    calendar_safety.KIND: cancel.run_undo,
}


async def cmd_undo(update, context, args):
    notify, notify_md = for_update(update)

    kind = pending_choice.undo_kind(context.user_data)
    reverse = REVERSERS.get(kind)
    if reverse is None:
        # Covers both "nothing recorded" and a kind no service claims. The
        # second cannot happen through the registry, and saying the same thing
        # for both is right anyway: from where you are sitting there is nothing
        # to undo either way.
        await notify("❌ Nothing to undo — I have not deleted or changed anything yet.")
        return

    await reverse(context.user_data, notify=notify, notify_md=notify_md)
