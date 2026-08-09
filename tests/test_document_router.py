"""Router tests for file uploads — `handle_document` dispatches on the caption.

Same idea as test_router.py, but the input is (caption, mime type) instead of a
text message. Kept separate because the PDF quote path also has to download the
file, which needs `responses` rather than a plain spy.
"""

import pytest
import responses

import bot.learn
import david
from clients import telegram_files
from services import books
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

    monkeypatch.setattr(books, "find_Book_Page", find_Book_Page)
    monkeypatch.setattr(books, "add_Quote", add_Quote)
    monkeypatch.setattr(books, "extract_quote_from_pdf", extract_quote_from_pdf)
    # Patched on bot.learn, not david: the caption path runs through
    # bot.learn.learn_pdf_upload, which resolves the name in its own module.
    # The TEXT command still goes through david's namespace — see test_router.
    monkeypatch.setattr(bot.learn, "handle_learn", handle_learn)
    return calls


def upload(caption, mime_type="application/pdf"):
    return FakeUpdate(caption=caption, document=FakeDocument(mime_type=mime_type))


# ─── CAPTION DISPATCH ──────────────────────────────────────────────────────────

@responses.activate
def test_learn_pdf_caption_routes_to_learn_with_the_file(spies):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    update = upload("Learn pdf")

    run(david.handle_document(update, FakeContext()))

    assert spies["handle_learn"]["user_text"] == "Learn pdf"
    assert spies["handle_learn"]["file_bytes"] == PDF_BYTES
    assert "add_Quote" not in spies


@responses.activate
def test_learn_pdf_caption_is_case_insensitive(spies):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)

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
    monkeypatch.setattr(books, "find_Book_Page", lambda book_name: None)
    update = upload(QUOTE_CAPTION)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("'Dune' not found in library")
    assert "add_Quote" not in spies


@responses.activate
def test_quote_caption_reports_a_failed_extraction(spies, monkeypatch):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    monkeypatch.setattr(books, "extract_quote_from_pdf",
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
    monkeypatch.setattr(books, "add_Quote", lambda page_id, title, text: False)
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


# ─── ATTACHMENT LIMITS ─────────────────────────────────────────────────────────
# Both attachment paths share download_pdf_attachment, so both must reject the
# same files. Parametrised over the two captions to keep them from drifting.

BOTH_PDF_CAPTIONS = pytest.mark.parametrize("caption", ["Learn pdf", QUOTE_CAPTION])


@BOTH_PDF_CAPTIONS
def test_non_pdf_is_rejected_on_both_paths(spies, caption):
    update = upload(caption, mime_type="application/epub+zip")

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Please attach a PDF")
    assert spies == {}, "a non-PDF reached Notion or Claude"


@BOTH_PDF_CAPTIONS
def test_oversized_pdf_is_rejected_before_downloading(spies, caption):
    """Telegram reports file_size up front, so an oversized file costs no bytes."""
    oversized = FakeDocument(file_size=telegram_files.MAX_PDF_BYTES + 1)
    update = FakeUpdate(caption=caption, document=oversized)

    # No responses.activate: any HTTP call at all would raise ConnectionError.
    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("The limit is 15 MB")
    assert spies == {}


@BOTH_PDF_CAPTIONS
@responses.activate
def test_pdf_at_the_size_limit_is_accepted(spies, caption):
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    at_limit = FakeDocument(file_size=telegram_files.MAX_PDF_BYTES)
    update = FakeUpdate(caption=caption, document=at_limit)

    run(david.handle_document(update, FakeContext()))

    assert not update.message.replied_with("The limit is")
    assert spies != {}, "a file exactly at the limit should be accepted"


@BOTH_PDF_CAPTIONS
@responses.activate
def test_oversized_pdf_is_rejected_when_telegram_omits_the_size(spies, caption):
    """file_size is optional in the Telegram API — the real size is re-checked."""
    responses.add(responses.GET, FILE_URL, body=b"x" * (telegram_files.MAX_PDF_BYTES + 1), status=200)
    no_size = FakeDocument(file_size=None)
    update = FakeUpdate(caption=caption, document=no_size)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("The limit is 15 MB")
    assert "handle_learn" not in spies
    assert "add_Quote" not in spies


@BOTH_PDF_CAPTIONS
@responses.activate
def test_download_failure_is_reported_on_both_paths(spies, caption):
    responses.add(responses.GET, FILE_URL, status=500, body="telegram is down")
    update = upload(caption)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("Download error")
    assert "handle_learn" not in spies
    assert "add_Quote" not in spies


@BOTH_PDF_CAPTIONS
def test_download_is_bounded_by_a_timeout_on_both_paths(spies, caption, monkeypatch):
    """A hung Telegram CDN must not wedge the handler forever.

    The real cap is 2 minutes; the test shortens it so the suite stays fast, and
    stalls the download so the timeout is what actually fires.

    The stall is only 0.5s because asyncio.to_thread threads cannot be cancelled:
    asyncio.run() waits for the executor to drain at shutdown, so a longer stall
    would be added to the suite's runtime even though wait_for returned promptly.
    """
    import time as time_module

    monkeypatch.setattr(telegram_files, "DOWNLOAD_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(telegram_files.requests, "get", lambda *a, **kw: time_module.sleep(0.5))
    update = upload(caption)

    run(david.handle_document(update, FakeContext()))

    assert update.message.replied_with("timed out")
    assert "handle_learn" not in spies
    assert "add_Quote" not in spies


@responses.activate
def test_download_uses_a_per_request_timeout(spies):
    """A stalled socket must fail fast rather than sit for the full 2 minutes."""
    responses.add(responses.GET, FILE_URL, body=PDF_BYTES, status=200)
    seen = {}
    real_get = telegram_files.requests.get

    def spy_get(url, **kwargs):
        seen.update(kwargs)
        return real_get(url, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(telegram_files.requests, "get", spy_get)
        run(david.handle_document(upload("Learn pdf"), FakeContext()))

    assert seen.get("timeout") == telegram_files.HTTP_TIMEOUT_SECONDS


# ─── PURE HELPERS ──────────────────────────────────────────────────────────────

def test_extract_quote_rejects_empty_markers():
    quote, err = books.extract_quote_from_pdf(PDF_BYTES, "", "end")

    assert quote is None
    assert "cannot be empty" in err


def test_extract_quote_reports_an_unreadable_pdf():
    quote, err = books.extract_quote_from_pdf(b"this is not a pdf", "begin", "end")

    assert quote is None
    assert err


def test_chunk_text_respects_the_notion_block_limit():
    chunks = books.chunk_text("x" * 4200)

    assert [len(c) for c in chunks] == [1800, 1800, 600]
    assert "".join(chunks) == "x" * 4200


def test_chunk_text_leaves_a_short_quote_alone():
    assert books.chunk_text("a short quote") == ["a short quote"]
