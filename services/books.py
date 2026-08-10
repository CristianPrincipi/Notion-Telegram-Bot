"""Books and the quotes that go on them.

Moved out of david.py unchanged: the Notion writes, the PyPDF2 walk, the two
timeouts around it, and every message. `update.message.reply_text` became
`notify`, `telegram_text.reply` became `notify_md`, and the one thing a service
genuinely cannot do for itself — fetch a file from Telegram's CDN — is handed in
as `download`.

WHY `download` IS A CALLBACK AND NOT AN IMPORT. `clients.telegram_files`
downloads from a `context.bot` that only the bot layer has. Injecting the bound
call keeps the ordering identical (the download still happens between the same
two messages) without this module ever seeing a PTB object. A test passes a
function returning bytes; nothing has to be faked.
"""

import asyncio
import io
import logging
import os
import re

import PyPDF2

from clients import telegram_files
from clients.notion_client import (
    CREATED_DESC, append_children, body_excerpt, notion_request, query_database,
)
from config import GENRE_MAP, genre_help
from telegram_text import escape_md

logger = logging.getLogger(__name__)

LETTI_ID      = os.environ.get("LETTI_ID")
LITERATURE_ID = os.environ.get("LITERATURE_ID")


# --- NOTION FUNCTIONS --- #
#
# SYNCHRONOUS and blocking, like every other Notion call in David. Reached only
# through asyncio.to_thread; see the note in services/expenses.py.


# --- NEW READED BOOK --- #
def add_New_Book(name, author, genre):
    """Create a new book entry in Notion. Returns page_id on success, None on failure."""
    data = {
        "parent": {"database_id": LETTI_ID},
        "properties": {
            "Name":   {"title": [{"text": {"content": name}}]},
            "Author": {"rich_text": [{"text": {"content": author}}]},
            "Genre":  {"multi_select": [{"name": genre}]},
            "Area":   {"relation": [{"id": LITERATURE_ID}]},
        }
    }
    response = notion_request("POST", "https://api.notion.com/v1/pages", json=data)
    if response.status_code != 200:
        logger.error("add_New_Book failed: Notion %s: %s",
                     response.status_code, body_excerpt(response))
        return None
    return response.json()["id"]


# --- NEW QUOTE FUNCTION ---
def find_Book_Page(book_name):
    """Search LETTI database for a book by name. Returns page_id or None.

    Sorted newest-first, so with two editions of the same title in the library
    the quote lands on the same one every time instead of on whichever row
    Notion happened to return first. See notion_client.CREATED_DESC.
    """
    results, err = query_database(
        LETTI_ID,
        filter_obj={"property": "Name", "title": {"contains": book_name.strip()}},
        sorts=CREATED_DESC,
    )
    if err:
        logger.error("find_Book_Page(%r) failed: %s", book_name, err)
        return None
    return results[0]["id"] if results else None


def extract_quote_from_pdf(pdf_bytes: bytes, begin_text: str, end_text: str):
    """Extract text between begin_text and end_text from a PDF.

    Processes pages incrementally — stops as soon as both markers are found,
    so large books don't require reading every page.
    Returns (extracted_quote: str, error: str | None).
    Always run via asyncio.to_thread() — never call directly from the event loop.
    """

    def _norm(t):
        return re.sub(r"\s+", " ", t or "").strip()

    norm_begin = _norm(begin_text).lower()
    norm_end   = _norm(end_text).lower()

    if not norm_begin or not norm_end:
        return None, "Begin or End text cannot be empty."

    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return None, "PDF appears to be empty."

        accumulated     = ""
        begin_pos_found = -1

        for page in reader.pages:
            accumulated += " " + _norm(page.extract_text())
            acc_lower    = accumulated.lower()

            if begin_pos_found == -1:
                bp = acc_lower.find(norm_begin)
                if bp != -1:
                    begin_pos_found = bp

            if begin_pos_found != -1:
                search_from = begin_pos_found + len(norm_begin)
                ep = acc_lower.find(norm_end, search_from)
                if ep != -1:
                    raw = accumulated[begin_pos_found : ep + len(norm_end)]
                    return _norm(raw), None

        if begin_pos_found == -1:
            return None, f"Begin text not found in PDF.\nSearched for: \'{begin_text[:100]}\'"
        return None, f"End text not found after begin marker.\nSearched for: \'{end_text[:100]}\'"

    except PyPDF2.errors.PdfReadError as e:
        return None, f"Could not read PDF: {e}"
    except Exception as e:
        return None, f"PDF extraction error: {e}"


def chunk_text(text, size=1800):
    """Split long text into chunks compatible with Notion limits."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def add_Quote(page_id, quote_title, quote_text):
    """Add a quote section to a book page. Returns (written, error).

    A long quote is many blocks and Notion caps an append at 100 of them, so this
    is several requests with no transaction across them. It used to run its own
    copy of that batching loop and return a bare `False` the moment one batch
    failed — while every batch before it was already on the page. You were told
    the quote had not been saved, two fifths of it had, and re-running appended
    those two fifths a second time on top.

    So it returns notion_client's Written, which says how much landed, and it
    borrows the batching from `append_children` rather than repeating it — the
    loop here was that function minus the `after` anchor.
    """
    children = [
        {
            "object": "block",
            "type": "heading_1",
            "heading_1": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": quote_title[:2000]
                        }
                    }
                ],
                "color": "green"
            }
        }
    ]

    for chunk in chunk_text(quote_text):
        children.append({
            "object": "block",
            "type": "quote",
            "quote": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": chunk
                        }
                    }
                ]
            }
        })

    written, err = append_children(page_id, children)
    if err:
        logger.error("add_Quote block append failed after %s: %s", written.summary, err)
    return written, err


def partial_quote_warning(written) -> str:
    """What to say when part of a quote is on the page and part is not.

    Named and shared because both quote flows have to say it and it is the whole
    point of the change: the re-run advice is what stops a partial write becoming
    a duplicated one.
    """
    return (f"⚠️ Saved partially — {written.summary} written.\n"
            "Re-sending this command appends a SECOND copy of the part that "
            "already landed. Delete the incomplete quote in Notion first.")


# --- THE FLOWS --- #

async def run_add_book(name, author, genre_shortcut, *, notify, notify_md=None):
    """`Add b [Name] - [Author] - [Genre]`.

    Takes the genre SHORTCUT rather than the resolved name, because both the
    refusal and the acknowledgement quote back what you typed — `s`, not
    `Satira`. GENRE_MAP is config, not command syntax, so resolving it here
    keeps the whole of "add a book" in one place.
    """
    genre = GENRE_MAP.get(genre_shortcut.lower())

    if genre is None: # Added check for invalid genre
        await notify(f"❌ Error: Invalid genre. Please use: {genre_help()}")
        return

    await notify(f"⏳ Adding '{name}' '{author}' '{genre_shortcut}' to Notion...")

    # CALL THE NOTION FUNCTION
    page_id = await asyncio.to_thread(add_New_Book, name, author, genre)

    if page_id:
        await notify("✅ Success! Book added to your database.")
    else:
        await notify("❌ Error: Could not connect to Notion. Check your API keys.")


async def run_add_quote(book_name, quote_title, quote_content, *, notify, notify_md=None):
    """`Add q [Book] - [Title] - [Full quote]`, and the refusal for the PDF form."""
    notify_md = notify_md or notify

    await notify(f"🔍 Searching '{book_name}' in library...")
    page_id = await asyncio.to_thread(find_Book_Page, book_name)

    if not page_id:
        await notify(f"⚠️ I didn't find '{book_name}' in the library.")
        return

    # --- PDF EXTRACTION MODE: attach PDF with this caption instead ---
    if " / " in quote_content:
        await notify_md(
            "📎 To extract a quote from a PDF, *attach the PDF file* and use it as the caption:\n\n"
            "`Add q [Book] - [Title] - [Begin text] / [End text]`",
        )
        return

    # --- MANUAL MODE: full quote provided directly ---
    written, err = await asyncio.to_thread(add_Quote, page_id, quote_title, quote_content)
    if not err:
        await notify(f"✍️ Quote added to '{book_name}'!")
    elif written.partial:
        await notify(f"⚠️ Quote only partly added to '{book_name}'.\n"
                     + partial_quote_warning(written))
    else:
        await notify("❌ Error during quote transcription.")


async def run_quote_from_pdf(book_name, quote_title, begin_text, end_text,
                             *, download, notify, notify_md=None):
    """`Add q [Book] - [Title] - [Begin] / [End]` sent as an attachment's caption.

    `download` is an awaitable returning (pdf_bytes, error) — the bot layer binds
    it to the attached document. Called only after the book is found, so a
    caption naming a book you do not own costs no bytes.
    """
    notify_md = notify_md or notify

    # Find book in Notion
    await notify(f"🔍 Searching \'{book_name}\' in library…")
    page_id = await asyncio.to_thread(find_Book_Page, book_name)
    if not page_id:
        await notify(f"⚠️ \'{book_name}\' not found in library.")
        return

    await notify("📄 Reading PDF and extracting quote…")

    pdf_bytes, err = await download()
    if err:
        await notify(err)
        return

    # Extraction stays on a worker thread under its own cap: it parses every
    # page of the PDF, which would otherwise block the event loop (see the
    # note on extract_quote_from_pdf).
    #
    # The cap is telegram_files' own DOWNLOAD_TIMEOUT_SECONDS, read through the
    # module rather than imported by value — david.py shared one constant
    # between the download and this parse, and reading it live keeps that one
    # source of truth. (That the parse cap is named after the download is
    # pre-existing; renaming it would be a behaviour change to argue for
    # separately.)
    try:
        quote_content, err = await asyncio.wait_for(
            asyncio.to_thread(extract_quote_from_pdf, pdf_bytes, begin_text, end_text),
            timeout=telegram_files.DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await notify(
            "❌ Timed out after 2 minutes.\n"
            "Try shorter Begin/End markers or a smaller PDF."
        )
        return

    if err:
        await notify(f"❌ {err}")
        return

    # Preview. This is raw text sliced out of an uploaded PDF at an arbitrary
    # 300-character boundary and dropped inside italic markers — the single most
    # likely value in the whole bot to contain a stray _ * ` or [.
    preview = quote_content[:300] + ("..." if len(quote_content) > 300 else "")
    await notify_md(
        f"📖 *Extracted* ({len(quote_content)} chars):\n\n_{escape_md(preview)}_",
    )

    # Save to Notion
    written, err = await asyncio.to_thread(add_Quote, page_id, quote_title, quote_content)
    if not err:
        await notify(f"✍️ Quote added to \'{book_name}\'!")
    elif written.partial:
        await notify(f"⚠️ Quote only partly added to \'{book_name}\'.\n"
                     + partial_quote_warning(written))
    else:
        await notify("❌ Error saving quote to Notion.")
