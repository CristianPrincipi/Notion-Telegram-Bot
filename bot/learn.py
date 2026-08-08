"""`Learn [type] [source]`, and the PDF upload that carries the same command.

Both are detached (see david.run_detached), so both are slow enough that the
progress messages are the point rather than decoration — which is why the
service reports through callbacks instead of returning a single result at the
end.
"""

from bot.notify import for_update
from bot.tasks import run_detached
from clients.telegram_files import download_pdf_attachment
from services.learn import run_learn


async def cmd_learn(update, context, args):
    # Detached: fetch + Claude can run for minutes. See run_detached.
    run_detached(context, update, handle_learn(update, args["text"]), "learn")


async def handle_learn(update, user_text: str, file_bytes: bytes | None = None):
    """Bind this message's reply channels and run the command."""
    notify, notify_md = for_update(update)
    await run_learn(user_text, file_bytes=file_bytes,
                    notify=notify, notify_md=notify_md)


async def learn_pdf_upload(update, context, doc, caption: str):
    """`Learn pdf` sent as an attachment's caption.

    The download happens HERE and not in the service, because it needs a
    `context.bot`. Learn's is a plain sequence — download, then summarise — so
    it is a straight await rather than the injected callback the quote path
    needs, where the download has to wait for a Notion lookup first.
    """
    await update.message.reply_text("⏳ Downloading your PDF…")
    file_bytes, err = await download_pdf_attachment(context, doc)
    if err:
        await update.message.reply_text(err)
        return
    await handle_learn(update, caption, file_bytes=file_bytes)
