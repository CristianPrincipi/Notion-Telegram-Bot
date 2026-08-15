"""`Agenda [day]` — the adapter for services/agenda.py.

Runs INLINE, not detached: one Google Calendar read, already off the event loop
inside the service, and read-only work cannot reorder against a write the way a
detached command could. Same reasoning `Get` carries.
"""

from bot.notify import for_update
from services.agenda import run_agenda


async def cmd_agenda(update, context, args):
    await handle_agenda(update, args.get("day"))


async def handle_agenda(update, day):
    """Bind this message's reply channels and run the command.

    Takes the parsed day token rather than the whole message, unlike
    `handle_remind`: the registry pattern captures it in one group, so re-parsing
    the text inside the service would be a second grammar for one optional word.
    `None` means no token was given, which the service reads as today.
    """
    notify, notify_md = for_update(update)
    await run_agenda(day, notify=notify, notify_md=notify_md)
