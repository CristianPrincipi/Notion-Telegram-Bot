"""`Remind [Name] [Date] [Time]` — the adapter for services/reminder.py.

Runs INLINE, not detached: one Google Calendar read and one write, both already
off the event loop inside the service.
"""

from bot.notify import for_update
from services.reminder import run_remind


async def cmd_remind(update, context, args):
    await handle_remind(update, args["text"])


async def handle_remind(update, user_text: str):
    """Bind this message's reply channels and run the command."""
    notify, notify_md = for_update(update)
    await run_remind(user_text, notify=notify, notify_md=notify_md)
