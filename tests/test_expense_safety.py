"""The three guards on a destructive expense command.

WHAT IS BEING PROTECTED
-----------------------
`D e Coffee` used to query with `contains`, take `results[0]`, and archive it.
Notion documents no ordering for query results, so with two Coffees on the page
the row that disappeared was arbitrary — and the reply said "deleted
successfully" either way. The failure was silent at every layer: no exception, a
successful HTTP call, a confident confirmation, and a missing row you would only
find by opening Notion.

Each section below drives the REAL handlers through `david.handle_message`, with
Notion replaced by a fake that records what was written. Asserting against the
shipping path matters more than usual here: two of the three guards are about
what does NOT happen, and a test that rebuilt the logic could only prove it
agrees with itself.

  1. ORDERING       — every lookup sorts created_time descending, so "first
                      match" means the same row on two identical calls.
  2. SCOPE          — the lookup is filtered to the current month's page.
  3. DISAMBIGUATION — more than one match writes NOTHING and asks.
  4. UNDO           — both writes record how to reverse themselves.
"""

import asyncio
from types import SimpleNamespace

import pytest

import bot.budget
import bot.implement
import bot.learn
import david
from services import books, expenses
import expense_safety
import page_lock
from conftest import EXPENSES_ID, FakeContext, FakeUpdate, run

MONTH_PAGE = "month-page-id"


@pytest.fixture(autouse=True)
def fresh_locks():
    page_lock._locks.clear()
    yield
    page_lock._locks.clear()


# ─── A NOTION STAND-IN THAT RECORDS ────────────────────────────────────────────

def expense_row(page_id, name, amount, date="2026-08-06", category="Food"):
    """A Notion page object shaped the way a database query returns one."""
    return {
        "id": page_id,
        "properties": {
            "Name":     {"type": "title", "title": [{"plain_text": name}]},
            "Amount":   {"number": amount},
            "Date":     {"date": {"start": date}},
            "Category": {"multi_select": [{"name": category}]},
        },
    }


class FakeNotion:
    """Records every query and PATCH instead of making them."""

    def __init__(self, rows):
        self.rows     = rows
        self.queries  = []      # [(db_id, filter_obj, sorts), ...]
        self.patches  = []      # [(page_id, body), ...]
        self.fail_write = False

    def query_database(self, db_id, filter_obj=None, sorts=None, page_size=100):
        self.queries.append((db_id, filter_obj, sorts))
        return list(self.rows), None

    def notion_request(self, method, url, **kwargs):
        page_id = url.rsplit("/", 1)[-1]
        body    = kwargs.get("json", {})
        if self.fail_write:
            return SimpleNamespace(status_code=400, text="Notion 400: nope",
                                   json=lambda: {})
        self.patches.append((page_id, body))
        return SimpleNamespace(status_code=200, json=lambda: {}, text="")

    # What the undo path calls, via notion_client rather than david's own PATCH.
    def set_archived(self, page_id, archived):
        self.patches.append((page_id, {"archived": archived}))
        return True, None

    def update_page(self, page_id, properties):
        self.patches.append((page_id, {"properties": properties}))
        return True, None

    @property
    def archived_ids(self):
        return [pid for pid, body in self.patches if body.get("archived") is True]


def install(monkeypatch, rows, month_id=MONTH_PAGE):
    fake = FakeNotion(rows)
    monkeypatch.setattr(expenses, "query_database", fake.query_database)
    monkeypatch.setattr(expenses, "notion_request", fake.notion_request)
    monkeypatch.setattr(expenses, "set_archived", fake.set_archived)
    monkeypatch.setattr(expenses, "update_page", fake.update_page)
    monkeypatch.setattr(expenses, "current_month_id", lambda: month_id)
    return fake


def send(text, context):
    """Drive one real message through the real router."""
    update = FakeUpdate(text=text)
    run(david.handle_message(update, context))
    return update


# ─── 1. ORDERING ───────────────────────────────────────────────────────────────

def test_every_expense_lookup_sorts_newest_first(monkeypatch):
    """Without a sort, "the first match" is whatever Notion felt like returning.

    The bug this closes is not that the order was wrong — it is that there was
    no order, so two identical commands could resolve to two different rows.
    """
    fake = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])

    send("D e Coffee", FakeContext())

    _, _, sorts = fake.queries[0]
    assert sorts == [{"timestamp": "created_time", "direction": "descending"}], (
        f"lookup ran unsorted: {sorts}")


def test_the_book_lookup_is_sorted_too(monkeypatch):
    """find_Book_Page reads results[0] as well, so it has the same exposure."""
    captured = {}

    def query_database(db_id, filter_obj=None, sorts=None, page_size=100):
        captured["sorts"] = sorts
        return [{"id": "book-1"}], None

    monkeypatch.setattr(books, "query_database", query_database)

    books.find_Book_Page("Dune")

    assert captured["sorts"] == [{"timestamp": "created_time", "direction": "descending"}]


# ─── 2. SCOPE ──────────────────────────────────────────────────────────────────

def test_the_lookup_is_confined_to_the_current_month(monkeypatch):
    """`D e Coffee` must not be able to reach last December's coffee."""
    fake = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])

    send("D e Coffee", FakeContext())

    db_id, filter_obj, _ = fake.queries[0]
    assert db_id == EXPENSES_ID
    conditions = filter_obj["and"]
    assert {"property": "Name", "title": {"contains": "Coffee"}} in conditions
    assert {"property": expenses.EXPENSE_MONTH_RELATION,
            "relation": {"contains": MONTH_PAGE}} in conditions


def test_an_unresolvable_month_refuses_rather_than_searching_every_month(monkeypatch):
    """Widening the search on failure would restore the exact reach the filter removes.

    The tempting fallback — "month unknown, so search everything" — turns a
    Notion hiccup into a command that can archive a row from any month, which is
    worse than not running at all.
    """
    fake = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)], month_id=None)

    update = send("D e Coffee", FakeContext())

    assert fake.queries == [], "it queried Notion without a month to scope to"
    assert fake.patches == [], "it wrote to Notion without knowing the month"
    assert update.message.replied_with("Could not look up")


def test_a_failed_lookup_is_not_reported_as_nothing_found(monkeypatch):
    """The collapse this codebase keeps paying for, asserted for this path.

    "Notion is down" and "you have no Coffee this month" need opposite
    reactions — retry versus stop looking — so they must not produce the same
    message.
    """
    monkeypatch.setattr(expenses, "current_month_id", lambda: MONTH_PAGE)
    monkeypatch.setattr(expenses, "query_database",
                        lambda *a, **kw: ([], "Notion 502: bad gateway"))

    update = send("D e Coffee", FakeContext())

    assert update.message.replied_with("Could not look up")
    assert not update.message.replied_with("no expense matching")


# ─── 3. DISAMBIGUATION ─────────────────────────────────────────────────────────

TWO_COFFEES = [
    expense_row("exp-new", "Coffee", 4.50, date="2026-08-06"),
    expense_row("exp-old", "Coffee beans", 12.00, date="2026-08-02", category="Shopping"),
]


def test_two_matches_write_nothing_and_ask(monkeypatch):
    """THE headline guarantee: an ambiguous destructive command is not a command."""
    fake = install(monkeypatch, TWO_COFFEES)

    update = send("D e Coffee", FakeContext())

    assert fake.patches == [], f"it wrote to Notion while ambiguous: {fake.patches}"
    assert update.message.replied_with("2")
    assert update.message.replied_with("Coffee beans"), "the list must name the rows"
    assert update.message.replied_with("Reply with a number")


def test_the_prompt_shows_what_tells_the_rows_apart(monkeypatch):
    """Two rows both called Coffee are only distinguishable by their numbers."""
    install(monkeypatch, TWO_COFFEES)

    update = send("D e Coffee", FakeContext())
    text = "\n".join(update.message.reply_texts)

    assert "€4.50" in text and "€12.00" in text
    assert "2026-08-06" in text and "2026-08-02" in text


def test_selecting_a_number_deletes_exactly_that_row(monkeypatch):
    """The follow-up is a bare number, carried by user_data — same context."""
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)
    update = send("2", context)

    assert fake.archived_ids == ["exp-old"], (
        f"archived {fake.archived_ids} — the selection picked the wrong row")
    assert update.message.replied_with("Deleted")


def test_selecting_a_number_updates_exactly_that_row(monkeypatch):
    """`U e` carries the new amount across the question and applies it on answer."""
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("U e Coffee 7.25 s", context)
    update = send("1", context)

    assert len(fake.patches) == 1
    page_id, body = fake.patches[0]
    assert page_id == "exp-new"
    assert body["properties"]["Amount"] == {"number": 7.25}
    assert body["properties"]["Category"] == {"multi_select": [{"name": "Shopping"}]}
    assert update.message.replied_with("Updated")


def test_an_out_of_range_number_keeps_the_list_answerable(monkeypatch):
    """A mistyped 5 should not cost you the whole command."""
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)
    first = send("5", context)
    assert first.message.replied_with("between 1 and 2")
    assert fake.patches == []

    second = send("1", context)
    assert fake.archived_ids == ["exp-new"]
    assert second.message.replied_with("Deleted")


def test_the_pending_list_expires(monkeypatch):
    """A number typed much later must not archive a row you have forgotten about.

    The deadline is what stops `2` — meant as an amount, or a mis-tap — from
    resolving against a list printed an hour ago.
    """
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)

    # Age the pending record past its TTL rather than sleeping two minutes.
    # The slot holds (kind, pending, items) — the kind arrived when `Cancel`
    # started sharing it, and it is what routes the number to the right handler.
    kind, pending, pages = context.user_data[expense_safety.PENDING_KEY]
    context.user_data[expense_safety.PENDING_KEY] = (
        kind,
        pending._replace(expires_at=pending.expires_at - expense_safety.PENDING_TTL_SECONDS - 1),
        pages,
    )

    update = send("2", context)

    assert fake.patches == [], "an expired list still archived a row"
    assert update.message.replied_with("I didn't get that"), (
        "an expired list should leave a bare number unrecognised, "
        f"got {update.message.reply_texts}")


def test_a_bare_number_with_nothing_pending_is_not_a_command(monkeypatch):
    fake = install(monkeypatch, TWO_COFFEES)

    update = send("2", FakeContext())

    assert fake.patches == []
    assert update.message.replied_with("I didn't get that")


def test_a_single_match_still_runs_straight_through(monkeypatch):
    """The prompt is for ambiguity only — one match must not cost a round trip."""
    fake = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])

    update = send("D e Coffee", FakeContext())

    assert fake.archived_ids == ["exp-1"]
    assert not update.message.replied_with("Reply with a number")


def test_the_confirmation_goes_out_as_markdown(monkeypatch):
    """The end-to-end half of the notify-binding guard (see test_telegram_text).

    `🗑️ Deleted *Coffee*` is built by services/expenses.py with the name escaped
    at the interpolation site, so it only renders if the bot layer sent it down
    the Markdown channel. The unit test proves for_update() returns the pair the
    right way round; this proves the HANDLER passes them to the service in that
    order — a swap there would put an escaped, unformatted string on the wire and
    every text assertion in this file would still pass.
    """
    install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])

    update = send("D e Coffee", FakeContext())

    confirmations = [kwargs for text, kwargs in update.message.replies
                     if "Deleted" in text]
    assert confirmations == [{"parse_mode": "Markdown"}], (
        f"the delete confirmation was not sent as Markdown: {update.message.replies}")


# ─── 4. UNDO ───────────────────────────────────────────────────────────────────

def test_undo_restores_a_deleted_expense(monkeypatch):
    fake    = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])
    context = FakeContext()

    send("D e Coffee", context)
    assert fake.archived_ids == ["exp-1"]

    update = send("undo", context)

    assert ("exp-1", {"archived": False}) in fake.patches, (
        f"undo did not un-archive the row: {fake.patches}")
    assert update.message.replied_with("Restored")


def test_undo_puts_back_the_amount_an_update_overwrote(monkeypatch):
    """The snapshot must be the row as it was FOUND, not as it was left.

    Notion keeps no property history an integration can read, so an undo that
    re-read the page after the write would restore the new amount over itself
    and report success.
    """
    fake    = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0, category="Food")])
    context = FakeContext()

    send("U e Coffee 99 s", context)
    update = send("undo", context)

    restored = [body for pid, body in fake.patches
                if pid == "exp-1" and "properties" in body][-1]
    assert restored["properties"]["Amount"] == {"number": 3.0}, (
        f"undo restored the wrong amount: {restored}")
    assert restored["properties"]["Category"] == {"multi_select": [{"name": "Food"}]}
    assert update.message.replied_with("previous amount")


def test_undo_with_nothing_recorded_says_so(monkeypatch):
    install(monkeypatch, [])

    update = send("undo", FakeContext())

    assert update.message.replied_with("Nothing to undo")


def test_undo_is_consumed_so_it_cannot_run_twice(monkeypatch):
    """Re-applying a snapshot after a deliberate re-edit would undo that too."""
    fake    = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])
    context = FakeContext()

    send("D e Coffee", context)
    send("undo", context)
    before = len(fake.patches)

    update = send("undo", context)

    assert len(fake.patches) == before, "the second undo wrote to Notion again"
    assert update.message.replied_with("Nothing to undo")


def test_a_failed_write_records_no_undo(monkeypatch):
    """Offering to reverse something that never happened is its own wrong answer."""
    fake    = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])
    fake.fail_write = True
    context = FakeContext()

    failed = send("D e Coffee", context)
    assert failed.message.replied_with("Could not delete")

    update = send("undo", context)
    assert update.message.replied_with("Nothing to undo")


def test_a_failed_undo_stays_available(monkeypatch):
    """Consuming the record on a failed reversal would strand the row."""
    fake    = install(monkeypatch, [expense_row("exp-1", "Coffee", 3.0)])
    context = FakeContext()

    send("D e Coffee", context)
    monkeypatch.setattr(expenses, "set_archived", lambda pid, archived: (False, "Notion 502"))

    first = send("undo", context)
    assert first.message.replied_with("Could not undo")

    monkeypatch.setattr(expenses, "set_archived", fake.set_archived)
    second = send("undo", context)
    assert second.message.replied_with("Restored")


# ─── 5. THE PROMPT SURVIVES AWKWARD DATA ───────────────────────────────────────

def test_an_expense_named_with_markdown_does_not_break_the_prompt(monkeypatch):
    """One unescaped asterisk makes Telegram reject the whole message.

    The prompt interpolates names straight out of Notion, so it is exactly the
    kind of sender telegram_text.py exists for.
    """
    install(monkeypatch, [
        expense_row("exp-1", "Coffee *2", 4.0),
        expense_row("exp-2", "Coffee_beans", 12.0),
    ])

    update = send("D e Coffee", FakeContext())

    text = "\n".join(update.message.reply_texts)
    assert r"Coffee \*2" in text, f"an asterisk reached Telegram unescaped: {text}"
    assert r"Coffee\_beans" in text


def test_a_row_with_no_amount_still_lists(monkeypatch):
    """A cleared Amount in Notion is a legitimate row, not a crash."""
    rows = [expense_row("exp-1", "Coffee", 4.0), expense_row("exp-2", "Coffee", None)]
    install(monkeypatch, rows)

    update = send("D e Coffee", FakeContext())

    assert update.message.replied_with("no amount")


# ─── 6. THE SELECTION DOES NOT HIJACK THE ROUTER ───────────────────────────────

def test_a_pending_list_does_not_swallow_other_commands(monkeypatch):
    """A live prompt must not turn the next real command into a selection."""
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)
    monkeypatch.setattr(bot.budget, "budget", lambda: ("MOCK BUDGET", None))
    update = send("B", context)

    assert update.message.replied_with("MOCK BUDGET")
    assert fake.patches == []


def test_a_second_ambiguous_command_replaces_the_first(monkeypatch):
    """One user, one printed list — a number can only mean the latest one."""
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)
    send("U e Coffee 5", context)
    send("1", context)

    assert fake.archived_ids == [], "the number answered the older, deleted list"
    assert fake.patches[0][1]["properties"]["Amount"] == {"number": 5.0}


def test_two_selections_do_not_both_apply(monkeypatch):
    """The pending record is consumed, so a second number finds nothing to answer."""
    fake    = install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)
    send("1", context)
    update = send("2", context)

    assert fake.archived_ids == ["exp-new"], f"archived twice: {fake.archived_ids}"
    assert update.message.replied_with("I didn't get that")


# ─── 7. THE PROMPT DOES NOT HOLD THE EXPENSE LOCK ──────────────────────────────

def test_waiting_for_a_selection_does_not_block_other_expense_writes(monkeypatch):
    """Holding the lock across a question would stall every write until you answer.

    The prompt can sit unanswered for two minutes. The lock is taken again when
    the number arrives, which is what keeps that wait off everyone else.
    """
    install(monkeypatch, TWO_COFFEES)
    context = FakeContext()

    send("D e Coffee", context)

    async def main():
        # Acquiring it at all proves the prompt released it.
        async with asyncio.timeout(1):
            async with page_lock.page_lock(EXPENSES_ID):
                return True

    assert run(main()) is True
