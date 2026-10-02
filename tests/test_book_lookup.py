"""find_Book_Page: three answers, not two.

It returned a bare page id or `None`, and `None` meant both "no such book" and
"Notion did not answer" — the collapse CLAUDE.md forbids ("An error is never an
empty result"). So during an outage `Add q Dune - …` replied "I didn't find
'Dune' in the library": a confident statement about your library, made when
David had not been able to read it, sending you to check a title that was fine.

It now answers like search_page_in_db: `(page_id, None)` found, `(None, None)`
nothing matches, `(None, error)` the read failed — and both `Add q` paths say
which one happened.
"""

import pytest

from conftest import run
from services import books


@pytest.fixture
def library(monkeypatch):
    """The Books database as query_database returns it: (rows, error)."""
    answer = {"value": ([], None)}
    monkeypatch.setattr(books, "query_database", lambda *a, **k: answer["value"])
    return answer


def test_a_book_that_exists_is_found(library):
    library["value"] = ([{"id": "dune-page"}], None)

    assert books.find_Book_Page("Dune") == ("dune-page", None)


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
