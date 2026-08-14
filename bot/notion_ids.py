"""`Diag` / `Find [name]` / `DBs` — the adapters for services/notion_ids.py.

All three run INLINE: they are read-only, and the slow one (`Diag`) is a handful
of Notion round trips already off the event loop inside the service.

THE MARKDOWN CHANNEL IS THE ONE THAT SPLITS HERE, which is the opposite of
bot/pkm.py. These reports are David's own markup — every ID in a `code span` so
it is one tap to copy into Railway — and they are the replies long enough to
exceed Telegram's limit: a workspace with forty databases, or a `Find` matching
everything. The progress lines and the raw Notion errors stay on the plain
channel, unsplit, because they are short and because escaping cannot save an
error string inside a code span anyway.
"""

from functools import partial

from bot.long_messages import send_long
from bot.notify import for_update
from services.notion_ids import run_dbs, run_diag, run_find


async def cmd_diag(update, context, args):
    await handle_diag(update)


async def cmd_find(update, context, args):
    await handle_find(update, args["query"])


async def cmd_dbs(update, context, args):
    await handle_dbs(update)


async def handle_diag(update):
    """Bind this message's reply channels and run the command."""
    notify, notify_md = for_update(update)
    await run_diag(notify=notify, notify_md=partial(send_long, notify_md))


async def handle_find(update, query: str):
    notify, notify_md = for_update(update)
    await run_find(query, notify=notify, notify_md=partial(send_long, notify_md))


async def handle_dbs(update):
    notify, notify_md = for_update(update)
    await run_dbs(notify=notify, notify_md=partial(send_long, notify_md))
