"""Router tests for file uploads — `handle_document` dispatches on the caption.

Same idea as test_router.py, but the input is (caption, mime type) instead of a
text message. Kept separate because the PDF quote path also has to download the
file, which needs `responses` rather than a plain spy.
"""

import pytest
import responses

import david
from conftest import FakeContext, FakeDocument, FakeUpdate, run

FILE_URL   = "https://api.telegram.org/file/bot-token/doc.pdf"
PDF_BYTES  = b"%PDF-1.4 pretend this is a book"
BOOK_PAGE  = "book-page-id"

QUOTE_CAPTION = "Add q Dune - On Fear - Fear is the mind-killer / I will face my fear"


@pytest.fixture
def spies(monkeypatch):
    """Record the Notion calls the document handler makes."""
    calls = {}

    def find_Book_Page(book_name):
        calls["find_Book_Page"] = {"book_name": book_name}
        return BOOK_PAGE

    def add_Quote(page_id, quote_title, quote_text):
        calls["add_Quote"] = {"page_id": page_id, "quote_title": quote_title,
                              "quote_text": quote_text}
        return True

    def extract_quote_from_pdf(pdf_bytes, begin_text, end_text):
        calls["extract"] = {"pdf_bytes": pdf_bytes, "begin_text": begin_text,
                            "end_text": end_text}
        return "Fear is the mind-killer. I will face my fear", None

    async def handle_learn(update, user_text, file_bytes=None):
        calls["handle_learn"] = {"user_text": user_text, "file_bytes": file_bytes}

    monkeypatch.setattr(david, "find_Book_Page", find_Book_Page)
    monkeypatch.setattr(david, "add_Quote", add_Quote)
    monkeypatch.setattr(david, "extract_quote_from_pdf", extract_quote_from_pdf)
    monkeypatch.setattr(david, "handle_learn", handle_learn)
    return calls


def upload(caption, mime_type="application/pdf"):
    return FakeUpdate(caption=caption, document=FakeDocument(mime_type=mime_type))


# ─── CAPTION DISPATCH ──────────────────────────────────────────────────────────

def test_learn_pdf_caption_routes_to_learn_with_the_file(spies):
    update = upload("Learn pdf")

    run(david.handle_document(update, FakeContext()))

    assert spies["handle_learn"]["user_text"] == "Learn pdf"
    assert spies["handle_learn"]["file_bytes"] == b"%PDF-1.4 fake"
    assert "add_Quote" not in spies


def test_learn_pdf_caption_is_case_insensitive(spies):
    run(david.handle_document(upload("LEARN PDF"), FakeContext()))

    assert "handle_learn" in spies


@responses.activate
def test_quote_caption_extracts_from_the_attached_pdf(spies):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert spies["find_Book_Page"] == {"book_name": "Dune"}
    assert spies["extract"]["begin_text"] == "Fear is the mind-killer"
    assert spies["extract"]["end_text"]   == "I will face my fear"
    assert spies["extract"]["pdf_bytes"]  == PDF_BYTES
    assert spies["add_Quote"]["page_id"]     == BOOK_PAGE
    assert spies["add_Quote"]["quote_title"] == "On Fear"
    assert update.message.replied_with("Quote added to 'Dune'")


@responses.activate
def test_quote_caption_previews_what_it_extracted(spies):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Extracted")
    assert update.message.replied_with("Fear is the mind-killer")


def test_quote_caption_rejects_a_non_pdf_attachment(spies):
    update = upload(QUOTE_CAPTION, mime_type="image/jpeg")

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Please attach a PDF")
    assert spies == {}, "a non-PDF must not reach Notion at all"


def test_quote_caption_reports_a_book_that_is_not_in_the_library(spies, monkeypatch):
    monkeypatch.setattr(david, "find_Book_Page", lambda book_name: None)
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("'Dune' not found in library")
    assert "add_Quote" not in spies


@responses.activate
def test_quote_caption_reports_a_failed_extraction(spies, monkeypatch):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    monkeypatch.setattr(david, "extract_quote_from_pdf",
                        lambda b, s, e: (None, "Begin text not found in PDF."))
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Begin text not found in PDF.")
    assert "add_Quote" not in spies


@responses.activate
def test_quote_caption_reports_a_failed_download(spies):
    responses.add(responses.GET, FILE_URL, status=500, body="telegram is down")
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Download error")
    assert "add_Quote" not in spies


@responses.activate
def test_quote_caption_reports_a_failed_notion_save(spies, monkeypatch):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    monkeypatch.setattr(david, "add_Quote", lambda page_id, title, text: False)
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Error saving quote to Notion")


@pytest.mark.parametrize("caption", [
    "",
    "here you go",
    "Add q Dune - On Fear - no slash separator",   # needs " / " to be a PDF quote
    "Implement Something - Brain",                 # text-only command, not a file one
])
def test_unrecognised_captions_explain_the_supported_ones(spies, caption):
    update = upload(caption)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("File received. Supported captions")
    assert spies == {}


# ─── PURE HELPERS ──────────────────────────────────────────────────────────────

def test_extract_quote_rejects_empty_markers():
    quote, err = david.extract_quote_from_pdf(PDF_BYTES, "", "end")

    assert quote is None
    assert "cannot be empty" in err


def test_extract_quote_reports_an_unreadable_pdf():
    quote, err = david.extract_quote_from_pdf(b"this is not a pdf", "begin", "end")

    assert quote is None
    assert err


def test_chunk_text_respects_the_notion_block_limit():
    chunks = david.chunk_text("x" * 4200)

    assert [len(c) for c in chunks] == [1800, 1800, 600]
    assert "".join(chunks) == "x" * 4200


def test_chunk_text_leaves_a_short_quote_alone():
    assert david.chunk_text("a short quote") == ["a short quote"]
