"""`Cancel [Name]` and the number that answers it — the adapter for services/cancel.py.

Runs INLINE, not detached: one calendar read and one delete, both already off the
event loop inside the service. Detaching it would let two cancels overlap for no
gain — they take about a second — and the lock would then be doing work the
sequential dispatch does for free.
"""

from bot.notify import for_update
from services import cancel


async def cmd_cancel(update, context, args):
    await handle_cancel(update, context, args["name"].strip())


async def handle_cancel(update, context, name: str):
    notify, notify_md = for_update(update)
    await cancel.run_cancel(context.user_data, name,
                            notify=notify, notify_md=notify_md)


async def handle_event_selection(update, context, selection: int):
    """A bare number answering a printed list of matching events.

    Not a Command: it depends on STATE rather than on the text, so david's
    dispatch loop checks for a live list before it walks the registry and routes
    the number by which feature owns it. See pending_choice.py.
    """
    notify, notify_md = for_update(update)
    await cancel.run_selection(context.user_data, selection,
                               notify=notify, notify_md=notify_md)
