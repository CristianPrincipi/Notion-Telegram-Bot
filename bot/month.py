"""`Month` — the adapter for services/month.py.

Runs INLINE: two Notion calls at most, already off the event loop inside the
service, and it is the command you send right after fixing a Notion permission —
waiting for it is the point.

No splitting here (see bot/long_messages.py): the reply is a headline, one page
ID and one line of detail, and it cannot grow with the size of the workspace.
"""

from bot.notify import for_update
from services.month import run_month


async def cmd_month(update, context, args):
    await handle_month(update)


async def handle_month(update):
    """Bind this message's reply channels and run the command."""
    notify, notify_md = for_update(update)
    await run_month(notify=notify, notify_md=notify_md)
