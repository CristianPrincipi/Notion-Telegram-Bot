"""`Add b` and `Add q` — the typed forms. The PDF form is in bot/documents.py."""

from bot.notify import for_update
from services import books


async def cmd_add_book(update, context, args):
    notify, notify_md = for_update(update)
    await books.run_add_book(
        args["name"].strip(), args["author"].strip(), args["genre"].strip(),
        notify=notify, notify_md=notify_md)


async def cmd_add_quote(update, context, args):
    notify, notify_md = for_update(update)
    await books.run_add_quote(
        args["book"].strip(), args["title"].strip(), args["body"].strip(),
        notify=notify, notify_md=notify_md)
