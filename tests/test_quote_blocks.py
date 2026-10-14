"""A quote is ONE quote block in Notion, however long it is.

THE BUG THIS LOCKS DOWN
-----------------------
Notion caps one piece of text at 2000 characters, so `add_Quote` cuts a quote
every 1800. It then gave EACH piece a quote block of its own, and a pasted
quote of 2500 characters showed up in the book as two quotes. The cap is per
piece, not per block: one block holds up to 100 pieces, and Notion joins them
seamlessly, so a cut mid-word does not show.

So the pieces go into one block's `rich_text`. One block is also where it
stops: a passage longer than 100 pieces is refused before anything is sent,
because a second block of that size would push a request past Notion's 500 KB
(append_children batches by block count, not bytes). Only the PDF form can
get there — Telegram caps a typed message at 4096 characters.
"""

import json

import pytest
import responses

from clients import notion_client
from conftest import NOTION_BASE, FakeUpdate, run, with_update, written_ok
from services import books

PAGE_ID      = "book-page-1"
CHILDREN_URL = f"{NOTION_BASE}/blocks/{PAGE_ID}/children"
PDF_BYTES    = b"%PDF-1.4 pretend this is a book"


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Retry backoff sleeps 1s/2s/4s — skip it so the suite stays fast."""
    monkeypatch.setattr(notion_client.time, "sleep", lambda seconds: None)


def passage(length):
    """`length` characters that never repeat a run, so a piece put back in the
    wrong order, or dropped, changes the text."""
    text = "".join(f"{i:06d} " for i in range(length // 7 + 1))
    return text[:length]


def sent_children():
    """The `children` of the one append request the test expects."""
    assert len(responses.calls) == 1, f"{len(responses.calls)} requests, expected one"
    return json.loads(responses.calls[0].request.body)["children"]


def pieces_of(block):
    return [part["text"]["content"] for part in block["quote"]["rich_text"]]


# ─── THE BLOCK ─────────────────────────────────────────────────────────────────

@responses.activate
def test_a_quote_longer_than_one_piece_is_still_one_quote():
    """THE BUG: 2500 characters were sent as two quote blocks."""
    responses.add(responses.PATCH, CHILDREN_URL, status=200,
                  json={"results": [{"id": "heading"}, {"id": "quote"}]})
    text = passage(2500)

    written, err = books.add_Quote(PAGE_ID, "On Fear", text)

    assert err is None
    heading, *quotes = sent_children()
    assert heading["type"] == "heading_1"
    assert [block["type"] for block in quotes] == ["quote"], "one quote, not one per piece"
    assert "".join(pieces_of(quotes[0])) == text
    assert all(len(piece) <= 2000 for piece in pieces_of(quotes[0])), "Notion's cap per piece"


@responses.activate
def test_the_longest_quote_allowed_still_fits_one_block():
    """The boundary from below: exactly 100 pieces is saved, as one block."""
    responses.add(responses.PATCH, CHILDREN_URL, status=200,
                  json={"results": [{"id": "heading"}, {"id": "quote"}]})
    text = passage(books.QUOTE_MAX_CHARS)

    written, err = books.add_Quote(PAGE_ID, "On Fear", text)

    assert err is None
    _, quote = sent_children()
    assert len(pieces_of(quote)) == 100, "Notion's cap on pieces in one block"
    assert "".join(pieces_of(quote)) == text


@responses.activate
def test_a_passage_one_block_cannot_hold_is_refused_before_any_request():
    """The boundary from above: one character more and nothing is sent at all."""
    text = passage(books.QUOTE_MAX_CHARS + 1)

    written, err = books.add_Quote(PAGE_ID, "On Fear", text)

    assert len(responses.calls) == 0, "a refused quote still reached Notion"
    assert err and f"{books.QUOTE_MAX_CHARS:,}" in err
    assert not written and not written.partial


# ─── THE PDF FLOW ──────────────────────────────────────────────────────────────

async def _download():
    return PDF_BYTES, None


@pytest.fixture
def pdf_quote(monkeypatch):
    """The PDF flow with the book found and a spy on the write."""
    saved = []
    monkeypatch.setattr(books, "find_Book_Page", lambda name: (PAGE_ID, None))
    monkeypatch.setattr(books, "add_Quote",
                        lambda page_id, title, text: saved.append(text) or (written_ok(2), None))

    def extracting(text):
        monkeypatch.setattr(books, "extract_quote_from_pdf", lambda *args: (text, None))
        update = FakeUpdate(caption="Add q Dune / On Fear / Begin / End")
        run(books.run_quote_from_pdf("Dune", "On Fear", "Begin", "End",
                                     download=_download, **with_update(update)))
        return update, saved

    return extracting


def test_a_passage_too_long_for_one_quote_is_refused_out_loud(pdf_quote):
    """Said before the preview, with the length and the cap, and nothing saved —
    rather than a preview followed by a bare "Error saving quote"."""
    update, saved = pdf_quote(passage(books.QUOTE_MAX_CHARS + 1))

    assert saved == []
    assert update.message.replied_with(f"{books.QUOTE_MAX_CHARS + 1:,} characters")
    assert update.message.replied_with(f"{books.QUOTE_MAX_CHARS:,}")
    assert not update.message.replied_with("Extracted")


def test_the_longest_passage_allowed_is_saved(pdf_quote):
    text = passage(books.QUOTE_MAX_CHARS)

    update, saved = pdf_quote(text)

    assert saved == [text]
    assert update.message.replied_with("Quote added")
