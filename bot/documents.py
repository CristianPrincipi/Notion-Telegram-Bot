"""File uploads — the second router, dispatching on the caption.

Separate from david's registry because the input is different in kind: a
`(caption, mime type)` pair rather than a text message, matched with re.match
against two shapes rather than fullmatch against a table. It is small enough to
read at a glance, which is why it has never needed the registry treatment.

The validation stays INLINE while the work is detached: it is pure and
network-free, so a wrong file type or an oversized upload is refused
immediately rather than from a background task.
"""

import logging
import re
from functools import partial

from telegram import Update
from telegram.ext import ContextTypes

from bot.learn import learn_pdf_upload
from bot.notify import for_update
from bot.tasks import run_detached
from clients.telegram_files import download_pdf_attachment, validate_pdf_attachment
from observability import record_command, set_correlation_id
from services import books
from telegram_text import reply

logger = logging.getLogger(__name__)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file uploads. Dispatches based on the message caption.

    Supported captions:
      Learn pdf                                          → summarise PDF, save to Learn DB
      Add q [Book] - [Title] - [Begin text] / [End text] → extract quote from attached PDF
    """
    # Same correlation tagging as handle_message — a PDF upload is a command too,
    # and it is the one most likely to run detached and interleave with another.
    set_correlation_id(getattr(update, "update_id", None))
    record_command()

    doc     = update.message.document
    caption = (update.message.caption or "").strip()
    logger.info("Received document with caption: %s", caption)

    # ── Learn pdf ──────────────────────────────────────────────────────────────
    if re.match(r"(?i)learn\s+pdf", caption):
        # Validated inline — it is pure and network-free, so a wrong file type or
        # an oversized upload is refused immediately rather than from a task.
        # download_pdf_attachment checks again; calling it twice costs nothing.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return
        run_detached(context, update,
                     learn_pdf_upload(update, context, doc, caption), "learn-pdf")
        return

    # ── Add q [Book] - [Title] - [Begin] / [End]  (extract quote from PDF) ────
    quote_pdf_match = re.match(r"(?i)add q (.+?) - (.+?) - (.+?) / (.+)", caption)
    if quote_pdf_match:
        # Checked before the Notion lookup so a wrong-format file costs no API call.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return

        # The download is BOUND, not performed: services/books.py decides when
        # (after the book is found, so a caption naming a book you do not own
        # costs no bytes) without ever seeing a PTB context of its own.
        notify, notify_md = for_update(update)
        run_detached(
            context, update,
            books.run_quote_from_pdf(
                quote_pdf_match.group(1).strip(),   # book name
                quote_pdf_match.group(2).strip(),   # quote title
                quote_pdf_match.group(3).strip(),   # begin text
                quote_pdf_match.group(4).strip(),   # end text
                download=partial(download_pdf_attachment, context, doc),
                notify=notify, notify_md=notify_md,
            ),
            "quote-pdf")
        return

    # ── Unknown caption ────────────────────────────────────────────────────────
    await reply(
        update,
        "📎 File received. Supported captions:\n\n"
        "`Learn pdf` — summarise and save to Learn DB\n"
        "`Add q [Book] - [Title] - [Begin] / [End]` — extract quote from this PDF",
    )
