"""Quotes sent from the Kindle (the KOReader `citazioni` plugin).

The same write as `Add q` — find the book in Letti, append a heading and one
quote block with `books.add_Quote` — with two differences the Kindle needs:

  A MISSING BOOK IS CREATED, not refused. On the Kindle there is nobody to read
  "I didn't find it" and retype the command; the highlight would sit in a queue
  forever. The book is created with the title and author from the EPUB and an
  EMPTY genre (`add_New_Book(genre=None)`): David cannot know it, and a guessed
  genre would look like your choice.

  A RETRY IS ANSWERED, NOT REPEATED. The Kindle queues highlights while offline
  and resends one whose response it never saw. Each carries an id; an id already
  saved is answered from memory without a second write. The memory is a bounded
  in-process dict — harmless to lose (hard rule 1): after a restart a retry can
  only duplicate if its first response was also lost.

THE FIND-OR-CREATE RUNS INSIDE page_lock(LETTI_ID). Two highlights from a new
book, close together, would otherwise both miss the lookup and create the book
twice. The key is the database, never the title (see page_lock.py).

Returns (result, error) like every fallible service function. `error` is a
KindleError whose `retryable` tells the Kindle whether to keep the quote queued
(Notion was unreachable) or to give up on it (the quote itself is refused).
"""

import asyncio
import logging
import re
from collections import OrderedDict
from typing import NamedTuple

from config import KINDLE_SEEN_IDS
from page_lock import WRITE_LOCK_TIMEOUT_SECONDS, PageBusy, page_lock
from services import books
from services.books import LETTI_ID

logger = logging.getLogger(__name__)


class KindleError(NamedTuple):
    message: str
    retryable: bool


class KindleQuote(NamedTuple):
    id: str
    book: str
    author: str
    title: str
    text: str


# id -> result of the write that saved it. Insertion-ordered, oldest evicted.
_saved: "OrderedDict[str, dict]" = OrderedDict()


def _remember(quote_id, result):
    if not quote_id:
        return
    _saved[quote_id] = result
    while len(_saved) > KINDLE_SEEN_IDS:
        _saved.popitem(last=False)


def title_candidates(title):
    """The book title as the EPUB has it, then its main title alone.

    EPUB titles often carry the subtitle ("Psicopolitica. Il neoliberismo e le
    nuove tecniche del potere") while the Letti row has only the main title, and
    find_Book_Page matches "Notion title CONTAINS the search", so the full string
    can never find the shorter row. The prefix before the first ':' / '.' / ' - '
    / '(' is tried second; the full title always wins when both match.
    """
    title = " ".join(title.split())
    candidates = [title]
    prefix = re.split(r"\s*(?::|\.\s|\s[-–—]\s|\()", title, maxsplit=1)[0].strip()
    # A main title too short to search ("Io", "Mr") is not tried alone: under
    # books.MIN_CONTAINS_CHARS it would only ever match itself exactly.
    if len(prefix) >= books.MIN_CONTAINS_CHARS and prefix != title:
        candidates.append(prefix)
    return candidates


async def _find_book(title):
    """(Book, None) / (None, None) / (None, error) — find_Book_Page's three answers."""
    for candidate in title_candidates(title):
        found, err = await asyncio.to_thread(books.find_Book_Page, candidate)
        if err:
            return None, err
        if found:
            return found, None
    return None, None


def validate(quote):
    """A refusal for a quote that can never be saved, or None."""
    if not quote.book.strip():
        return "The highlight has no book title."
    if not quote.text.strip():
        return "The highlight is empty."
    return books.quote_too_long(quote.text)


async def run_kindle_quote(quote, *, notify):
    """Save one Kindle highlight. Returns (result, error); reports through `notify`.

    result = {"book": str, "created": bool, "duplicate": bool} — `book` is the
    Letti row's own title, which need not be the EPUB's.
    """
    book = " ".join(quote.book.split())
    title = quote.title.strip() or book

    refusal = validate(quote)
    if refusal:
        await notify(f"❌ Kindle quote from '{book or '?'}' not saved: {refusal}")
        return None, KindleError(refusal, retryable=False)

    try:
        async with page_lock(LETTI_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            # Checked INSIDE the lock: a retry that arrives while the first send
            # is still writing waits for it, then finds its id here.
            if quote.id and quote.id in _saved:
                return {**_saved[quote.id], "duplicate": True}, None

            found, err = await _find_book(book)
            if err:
                await notify(f"❌ Kindle quote from '{book}' not saved: could not search "
                             f"your library. It stays queued on the Kindle. Notion said:\n{err}")
                return None, KindleError(f"Notion lookup failed: {err}", retryable=True)

            created = False
            if not found:
                page_id, err = await asyncio.to_thread(
                    books.add_New_Book, book, quote.author.strip(), None)
                if err:
                    await notify(f"❌ Kindle quote from '{book}' not saved: the book is not in "
                                 "your library and creating it failed. It stays queued on the "
                                 f"Kindle. Notion said:\n{err}")
                    return None, KindleError(f"Could not create the book in Notion: {err}",
                                             retryable=True)
                found, created = books.Book(page_id, book), True

            written, err = await asyncio.to_thread(books.add_Quote, found.id, title, quote.text)
            if err and written.partial:
                # Part of it is on the page: a retry would append a second copy,
                # so the Kindle must NOT keep it queued.
                await notify(f"⚠️ Kindle quote only partly added to {books.landed_on(found, book)}.\n"
                             + books.partial_quote_warning(written))
                return None, KindleError("Saved partially — fix it in Notion", retryable=False)
            if err:
                await notify(f"❌ Kindle quote from '{book}' not saved. It stays queued on "
                             f"the Kindle. Notion said:\n{err}")
                return None, KindleError(f"Notion write failed: {err}", retryable=True)

            result = {"book": found.title, "created": created, "duplicate": False}
            _remember(quote.id, result)
    except PageBusy:
        return None, KindleError("Your library is busy, try again in a moment", retryable=True)

    logger.info("Kindle quote saved to %r (created=%s)", found.title, created)
    message = f"📖 Kindle quote added to {books.landed_on(found, book)}: {title}"
    if created:
        message += "\nThe book was not in your library, so it was created — with no genre."
    await notify(message)
    return result, None
