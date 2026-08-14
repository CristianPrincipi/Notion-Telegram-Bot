"""`Diag` / `Find` / `DBs` — the read-only Notion ID diagnostics, without a bot.

WHY THIS FILE EXISTS
--------------------
notion_ids.py had no test file at all. It was reachable only through a fake
Update, which is exactly the shape that makes a module go untested: the effort
of building the transport outweighs the value of asserting on a diagnostic.
Splitting it into services/ removes that cost, and this is the proof — every
test here drives the real service with a list's `append` and no Update
anywhere.

Coverage is deliberately thin and aimed at the one thing these commands are
FOR: handing you an ID you can paste into Railway, and telling you when Notion
could not be reached rather than reporting an empty workspace. The diagnostic's
own branch logic (schema checks, the month-page section) is left to
build_diagnostic_report, which was already pure before the split and is not
what this milestone changed.
"""

import pytest

from conftest import run
from services import notion_ids


class Collected(list):
    """The messages a run produced, plus the notify that produced them.

    A list subclass so the assertions read as `any(... for message in said)` —
    the collector IS the transcript, which is the whole point of the interface
    being one callable.
    """

    async def notify(self, text):
        self.append(text)


@pytest.fixture
def said():
    """A notify that is one list append — the whole interface, no Update.

    notify_md defaults to notify, so a caller supplying only this gets every
    message as text. A scheduled job wanting to log the diagnostic needs no
    more than this.
    """
    return Collected()


def test_dbs_lists_every_database_with_its_id(said, monkeypatch):
    monkeypatch.setattr(notion_ids, "search_all", lambda query="", only=None: ([
        {"object": "database", "id": "1234abcd-5678-90ef-1234-567890abcdef",
         "title": [{"plain_text": "Expenses"}]}], None))

    run(notion_ids.run_dbs(notify=said.notify))

    assert any("Expenses" in message for message in said)
    assert any("1234abcd567890ef1234567890abcdef" in message for message in said), (
        "the ID went out dashed — the 32-char form is what Railway wants")


def test_dbs_reports_a_failed_search_rather_than_an_empty_workspace(said, monkeypatch):
    """The collapse this codebase keeps paying for, in the one command whose job
    is diagnosing a broken connection: "your integration can't see any databases"
    sends you to Notion's Connections menu, which is not the problem."""
    monkeypatch.setattr(notion_ids, "search_all",
                        lambda query="", only=None: ([], "Notion 401: API token is invalid"))

    run(notion_ids.run_dbs(notify=said.notify))

    assert any("401" in message for message in said)
    assert not any("can't see any databases" in message for message in said)


def test_find_needs_a_query_and_says_so(said):
    run(notion_ids.run_find("   ", notify=said.notify))

    assert any("Usage" in message for message in said)


def test_find_reports_pages_and_databases_differently(said, monkeypatch):
    monkeypatch.setattr(notion_ids, "search_all", lambda query="", only=None: ([
        {"object": "database", "id": "db-1", "title": [{"plain_text": "Expenses"}]},
        {"object": "page", "id": "pg-1", "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "August 2026"}]}}},
    ], None))

    run(notion_ids.run_find("august", notify=said.notify))

    body = "\n".join(said)
    assert "_(database)_" in body and "_(page)_" in body, (
        "a page and a database look identical in the results, and only one of "
        "them can be a MONTHS_DB_ID")


def test_diag_survives_a_crash_inside_the_report(said, monkeypatch):
    """The one branch that cannot be reached by any Notion double: the diagnostic
    is what you run when things are already broken, so it must report its own
    failure instead of raising into the error handler."""
    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(notion_ids, "build_diagnostic_report", explode)

    run(notion_ids.run_diag(notify=said.notify))

    assert any("Diagnostic crashed" in message for message in said)
    assert any("boom" in message for message in said)
