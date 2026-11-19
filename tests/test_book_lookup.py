"""find_Book_Page: three answers, not two.

It returned a bare page id or `None`, and `None` meant both "no such book" and
"Notion did not answer" — the collapse CLAUDE.md forbids ("An error is never an
empty result"). So during an outage `Add q Dune / …` replied "I didn't find
'Dune' in the library": a confident statement about your library, made when
David had not been able to read it, sending you to check a title that was fine.

It now answers like search_page_in_db: `(page_id, None)` found, `(None, None)`
nothing matches, `(None, error)` the read failed — and both `Add q` paths say
which one happened.
"""

import pytest

from conftest import run, written_ok
from services import books
from services.books import Book


@pytest.fixture
def library(monkeypatch):
    """The Books database as query_database returns it: (rows, error)."""
    answer = {"value": ([], None)}
    monkeypatch.setattr(books, "query_database", lambda *a, **k: answer["value"])
    return answer


def row(page_id, title):
    """One Letti row as the query returns it: an id and a title property."""
    return {"id": page_id,
            "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}}}


def test_a_book_that_exists_is_found(library):
    library["value"] = ([row("dune-page", "Dune")], None)

    assert books.find_Book_Page("Dune") == (Book("dune-page", "Dune"), None)


def test_a_book_that_does_not_exist_is_not_an_error(library):
    library["value"] = ([], None)

    assert books.find_Book_Page("Dune") == (None, None)


def test_a_failed_read_is_an_error_not_a_missing_book(library):
    library["value"] = ([], "Notion API error 503: service unavailable")

    page_id, err = books.find_Book_Page("Dune")

    assert page_id is None
    assert "503" in err


def _said(coro_factory):
    said = []

    async def collect(text):
        said.append(text)

    run(coro_factory(collect))
    return said


async def _never_download():
    raise AssertionError("the PDF was downloaded for a book David could not look up")


@pytest.mark.parametrize("command", [
    lambda notify: books.run_add_quote("Dune", "Fear", "I must not fear.", notify=notify),
    lambda notify: books.run_quote_from_pdf("Dune", "Fear", "I must", "gone",
                                            download=_never_download, notify=notify),
], ids=["typed quote", "quote from PDF"])
def test_an_outage_is_reported_as_an_outage(library, command):
    """THE BUG: both paths answered a Notion failure with "not found"."""
    library["value"] = ([], "Notion API error 503: service unavailable")

    said = _said(command)

    assert not any("didn't find" in m or "not found" in m for m in said), said
    assert any("Could not search your library" in m for m in said), said
    assert any("503" in m for m in said), "the Notion error was not passed on"


@pytest.mark.parametrize("command", [
    lambda notify: books.run_add_quote("Dune", "Fear", "I must not fear.", notify=notify),
    lambda notify: books.run_quote_from_pdf("Dune", "Fear", "I must", "gone",
                                            download=_never_download, notify=notify),
], ids=["typed quote", "quote from PDF"])
def test_a_missing_book_is_still_reported_as_missing(library, command):
    """The other half — a fix that called everything an outage would pass above."""
    library["value"] = ([], None)

    said = _said(command)

    assert any("Dune" in m and "library" in m and "Could not search" not in m
               for m in said[1:]), said


# ─── WHICH ROW: AN EXACT TITLE FIRST ───────────────────────────────────────────
# Notion is asked for every title CONTAINING the search, newest first, and the
# first row used to be taken whatever it was: with "Dune" and a newer
# "Dune Messiah" in Letti, a quote from Dune went to Dune Messiah — and the reply
# named "Dune", the title you typed, so nothing showed it.

def test_an_exact_title_wins_over_a_newer_title_containing_it(library):
    library["value"] = ([row("messiah", "Dune Messiah"), row("dune", "Dune")], None)

    assert books.find_Book_Page("Dune") == (Book("dune", "Dune"), None)


def test_exact_ignores_case_and_runs_of_spaces(library):
    library["value"] = ([row("messiah-2", "Dune Messiah II"), row("messiah", "Dune  Messiah")], None)

    assert books.find_Book_Page("  dune   MESSIAH ") == (Book("messiah", "Dune  Messiah"), None)


def test_with_no_exact_title_the_newest_containing_it_is_still_found(library):
    """The fallback stays: a Letti row may carry more than the EPUB or your typing."""
    library["value"] = ([row("messiah", "Dune Messiah"), row("children", "Children of Dune")], None)

    assert books.find_Book_Page("Dune") == (Book("messiah", "Dune Messiah"), None)


def test_a_short_search_needs_an_exact_title(library):
    """A Kindle book titled "It" matched any title containing "it"."""
    library["value"] = ([row("little", "Little Women")], None)
    assert books.find_Book_Page("It") == (None, None)

    library["value"] = ([row("little", "Little Women"), row("it", "It")], None)
    assert books.find_Book_Page("It") == (Book("it", "It"), None)


def test_the_cut_off_is_four_characters():
    assert books.MIN_CONTAINS_CHARS == 4


# ─── THE REPLY NAMES THE ROW IT WROTE TO ───────────────────────────────────────

@pytest.fixture
def quote_written(monkeypatch):
    written = []
    monkeypatch.setattr(books, "add_Quote",
                        lambda page_id, title, text: written.append(page_id) or (written_ok(2), None))
    monkeypatch.setattr(books, "extract_quote_from_pdf", lambda *a: ("I must not fear.", None))
    return written


async def _pdf_bytes():
    return b"%PDF-1.4", None


@pytest.mark.parametrize("command", [
    lambda notify: books.run_add_quote("Dune", "Fear", "I must not fear.", notify=notify),
    lambda notify: books.run_quote_from_pdf("Dune", "Fear", "I must", "fear",
                                            download=_pdf_bytes, notify=notify),
], ids=["typed quote", "quote from PDF"])
def test_the_reply_names_the_book_the_quote_went_to(library, quote_written, command):
    library["value"] = ([row("messiah", "Dune Messiah")], None)

    said = _said(command)

    assert quote_written == ["messiah"]
    assert any("added to 'Dune Messiah'" in m and "'Dune'" in m for m in said), said


def test_an_exact_match_is_named_once(library, quote_written):
    library["value"] = ([row("dune", "Dune")], None)

    said = _said(lambda notify: books.run_add_quote("dune", "Fear", "I must not fear.",
                                                    notify=notify))

    assert said[-1] == "✍️ Quote added to 'Dune'!", said
