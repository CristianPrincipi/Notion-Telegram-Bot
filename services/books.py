"""Books and the quotes that go on them.

Moved out of david.py unchanged: the Notion writes, the pypdf walk, the two
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
from typing import NamedTuple

import pypdf

from clients import telegram_files
from clients.notion_client import (
    CREATED_DESC, Written, append_children, body_excerpt, get_page_title, notion_request,
    query_database,
)
from config import (
    BOOK_AREA_RELATION, BOOK_AUTHOR_PROPERTY, BOOK_GENRE_PROPERTY, BOOK_TITLE_PROPERTY,
    GENRE_MAP, genre_help,
)
from telegram_text import escape_md

logger = logging.getLogger(__name__)

LETTI_ID      = os.environ.get("LETTI_ID")
LITERATURE_ID = os.environ.get("LITERATURE_ID")


class Book(NamedTuple):
    """A Letti row as a lookup found it: where to write, and what it is called."""

    id: str
    title: str


# --- NOTION FUNCTIONS --- #
#
# SYNCHRONOUS and blocking, like every other Notion call in David. Reached only
# through asyncio.to_thread; see the note in services/expenses.py.


# --- NEW READED BOOK --- #
def add_New_Book(name, author, genre=None):
    """Create a new book entry in Notion. Returns (page_id, error) and never raises.

    `genre=None` leaves the Genre column EMPTY rather than inventing one: a book
    created from a Kindle highlight has no genre David could know, and a guessed
    one would read as a choice you made. You fill it in Notion if you want it.

    It returned a bare page id or None, and let a network failure raise: `Add b`
    answered every failure with "Check your API keys" — a validation error
    included — and the Kindle endpoint answered 500 instead of "keep it queued".
    """
    properties = {
        BOOK_TITLE_PROPERTY:  {"title": [{"text": {"content": name}}]},
        BOOK_AUTHOR_PROPERTY: {"rich_text": [{"text": {"content": author}}]},
        BOOK_AREA_RELATION:   {"relation": [{"id": LITERATURE_ID}]},
    }
    if genre is not None:
        properties[BOOK_GENRE_PROPERTY] = {"multi_select": [{"name": genre}]}
    data = {
        "parent": {"database_id": LETTI_ID},
        "properties": properties,
    }
    try:
        response = notion_request("POST", "https://api.notion.com/v1/pages", json=data)
        if response.status_code != 200:
            err = f"Notion {response.status_code}: {body_excerpt(response)}"
            logger.error("add_New_Book failed: %s", err)
            return None, err
        return response.json()["id"], None
    except Exception as e:
        logger.error("add_New_Book failed: %s: %s", type(e).__name__, e)
        return None, str(e) or type(e).__name__


# A search shorter than this matches a title only EXACTLY. Notion is asked for
# every title CONTAINING the search, and "It" or "Io" is inside half a library.
MIN_CONTAINS_CHARS = 4


def same_title(a: str, b: str) -> bool:
    """Equal once case and runs of whitespace are ignored — what "exact" means here.

    Punctuation and accents still count: "Dune." and "Dune" are two titles.
    """
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def landed_on(book: Book, searched: str) -> str:
    """The row a quote went to, for a reply: 'Dune' — or, when the row is not
    titled what was searched, 'Dune Messiah' (searched for 'Dune').

    The replies used to name the SEARCH, so a quote filed under another row
    read as filed where you asked.
    """
    if same_title(book.title, searched):
        return f"'{book.title}'"
    return f"'{book.title}' (searched for '{searched}')"


# --- NEW QUOTE FUNCTION ---
def find_Book_Page(book_name):
    """Search LETTI database for a book by name. Returns (Book, error).

    Three answers, like search_page_in_db: (Book, None) found, (None, None)
    nothing matches, (None, error) the read failed. It used to return a bare
    None for the last two, so during a Notion outage `Add q` told you the book
    was not in your library — a confident answer about data it had not read.

    WHICH ROW. Notion returns every title CONTAINING the search. A title equal
    to the search (`same_title`) wins; failing that, the newest row containing
    it — for a search of at least MIN_CONTAINS_CHARS. It used to take the newest
    row whatever it was, so with "Dune" and a newer "Dune Messiah" in Letti a
    quote from Dune went to Dune Messiah, and a Kindle book titled "It" went to
    any title with "it" in it.

    Sorted newest-first, so with two editions of the same title in the library
    the quote lands on the same one every time instead of on whichever row
    Notion happened to return first. See notion_client.CREATED_DESC.

    The Book carries the row's own title, so a reply can say where the quote went.
    """
    name = " ".join(book_name.split())
    results, err = query_database(
        LETTI_ID,
        filter_obj={"property": BOOK_TITLE_PROPERTY, "title": {"contains": name}},
        sorts=CREATED_DESC,
    )
    if err:
        logger.error("find_Book_Page(%r) failed: %s", book_name, err)
        return None, err

    rows = [Book(result["id"], get_page_title(result)) for result in results]
    exact = next((row for row in rows if same_title(row.title, name)), None)
    if exact:
        return exact, None
    if len(name) < MIN_CONTAINS_CHARS or not rows:
        return None, None
    return rows[0], None


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
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
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

    except pypdf.errors.PdfReadError as e:
        return None, f"Could not read PDF: {e}"
    except Exception as e:
        return None, f"PDF extraction error: {e}"


# Notion caps one piece of text at 2000 characters (1800 leaves a margin) and
# one block at 100 pieces, so this is the most one quote block can hold: about
# 180 KB of Latin text, inside Notion's 500 KB per request.
QUOTE_PIECE_CHARS = 1800
QUOTE_MAX_PIECES  = 100
QUOTE_MAX_CHARS   = QUOTE_PIECE_CHARS * QUOTE_MAX_PIECES


def chunk_text(text, size=QUOTE_PIECE_CHARS):
    """Split long text into chunks compatible with Notion limits."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def quote_too_long(quote_text):
    """The refusal for a passage one quote block cannot hold, or None."""
    if len(quote_text) <= QUOTE_MAX_CHARS:
        return None
    return (f"❌ That passage is {len(quote_text):,} characters, and one quote holds "
            f"at most {QUOTE_MAX_CHARS:,}. Nothing was saved — pick Begin and End "
            "markers closer together.")


def add_Quote(page_id, quote_title, quote_text):
    """Add a quote section to a book page. Returns (written, error).

    ONE QUOTE, ONE BLOCK. The text is cut into 1800-character pieces for
    Notion's per-piece cap, and every piece goes into the SAME quote block —
    Notion joins them seamlessly. Each piece used to get a block of its own, so
    a pasted quote of 2500 characters showed up in the book as two quotes.

    A passage one block cannot hold is refused here, before any request: a
    second block of that size would take a request past Notion's 500 KB,
    because append_children batches by block count, not bytes.

    It returns notion_client's Written, like every batched append: a heading
    and one block are a single request, so a quote lands whole or not at all,
    but the callers keep the partial-write branch the convention asks for.
    """
    refusal = quote_too_long(quote_text)
    if refusal:
        return Written(), refusal

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

    children.append({
        "object": "block",
        "type": "quote",
        "quote": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {
                        "content": piece
                    }
                }
                for piece in chunk_text(quote_text)
            ]
        }
    })

    written, err = append_children(page_id, children)
    if err:
        logger.error("add_Quote block append failed after %s: %s", written.summary, err)
    return written, err


def partial_quote_report(written, where: str, what: str = "Quote") -> str:
    """What to say when part of a quote is on the page and part is not — or when
    Notion did not say whether it is (`written.unsure`).

    Named and shared because the quote flows and the Kindle have to say it and it
    is the whole point of the change: the re-run advice is what stops a partial
    write becoming a duplicated one.
    """
    if written.unsure:
        return (f"⚠️ {what} for {where} not confirmed: Notion did not answer, so it "
                "may be on the page.\nCheck the book before re-sending — if it is "
                "there, re-sending appends a SECOND copy.")
    return (f"⚠️ {what} only partly added to {where}.\n"
            f"⚠️ Saved partially — {written.summary} written.\n"
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
    _, err = await asyncio.to_thread(add_New_Book, name, author, genre)

    if err:
        # Plain: the error is Notion's (or the network's), not David's.
        await notify(f"❌ Could not add '{name}' to your library. Notion said:\n{err}")
    else:
        await notify("✅ Success! Book added to your database.")


async def _report_lookup_failure(book_name, err, notify):
    """An outage is not a missing book: saying "not found" would send you to check
    a title that was fine. Plain text — the error is Notion's, not David's."""
    await notify(f"❌ Could not search your library for '{book_name}', so nothing "
                 f"was saved. Notion said:\n{err}")


async def run_add_quote(book_name, quote_title, quote_content, *, notify, notify_md=None):
    """`Add q [Book] / [Title] / [Full quote]`, and the refusal for the PDF form.

    Only " | " marks the PDF form; a slash in a typed quote is the quote's own.
    """
    notify_md = notify_md or notify

    await notify(f"🔍 Searching '{book_name}' in library...")
    book, err = await asyncio.to_thread(find_Book_Page, book_name)

    if err:
        await _report_lookup_failure(book_name, err, notify)
        return
    if not book:
        await notify(f"⚠️ I didn't find '{book_name}' in the library.")
        return

    # --- PDF EXTRACTION MODE: attach PDF with this caption instead ---
    if " | " in quote_content:
        await notify_md(
            "📎 To extract a quote from a PDF, *attach the PDF file* and use it as the caption:\n\n"
            "`Add q [Book] / [Title] / [Begin text] | [End text]`",
        )
        return

    # --- MANUAL MODE: full quote provided directly ---
    written, err = await asyncio.to_thread(add_Quote, book.id, quote_title, quote_content)
    if not err:
        await notify(f"✍️ Quote added to {landed_on(book, book_name)}!")
    elif written.partial:
        await notify(partial_quote_report(written, landed_on(book, book_name)))
    else:
        await notify("❌ Error during quote transcription.")


async def run_quote_from_pdf(book_name, quote_title, begin_text, end_text,
                             *, download, notify, notify_md=None):
    """`Add q [Book] / [Title] / [Begin] | [End]` sent as an attachment's caption.

    `download` is an awaitable returning (pdf_bytes, error) — the bot layer binds
    it to the attached document. Called only after the book is found, so a
    caption naming a book you do not own costs no bytes.
    """
    notify_md = notify_md or notify

    # Find book in Notion
    await notify(f"🔍 Searching \'{book_name}\' in library…")
    book, err = await asyncio.to_thread(find_Book_Page, book_name)
    if err:
        await _report_lookup_failure(book_name, err, notify)
        return
    if not book:
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

    # Refused before the preview: add_Quote would refuse it too, but only as a
    # bare "Error saving quote" after showing you a passage it was not saving.
    refusal = quote_too_long(quote_content)
    if refusal:
        await notify(refusal)
        return

    # Preview. This is raw text sliced out of an uploaded PDF at an arbitrary
    # 300-character boundary and dropped inside italic markers — the single most
    # likely value in the whole bot to contain a stray _ * ` or [.
    preview = quote_content[:300] + ("..." if len(quote_content) > 300 else "")
    await notify_md(
        f"📖 *Extracted* ({len(quote_content)} chars):\n\n_{escape_md(preview)}_",
    )

    # Save to Notion
    written, err = await asyncio.to_thread(add_Quote, book.id, quote_title, quote_content)
    if not err:
        await notify(f"✍️ Quote added to {landed_on(book, book_name)}!")
    elif written.partial:
        await notify(partial_quote_report(written, landed_on(book, book_name)))
    else:
        await notify("❌ Error saving quote to Notion.")
