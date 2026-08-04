"""Races that only exist once updates stop being processed one at a time.

WHY THESE ARE NEW
-----------------
Every bug in this file was unreachable while python-telegram-bot processed
updates sequentially: a handler ran to completion before the next update was
even looked at, so no two find-then-mutate cycles could interleave. Sequential
processing was doing the job of a lock table, silently, for free.

Turning on concurrent_updates takes that away. Each test here drives two REAL
handlers concurrently and asserts the outcome you would get from running them
one after the other. They are the evidence behind enabling concurrency — the
"verified" half of "locks in place AND verified".

Every fake below sleeps between its read and its write. That is not padding: an
instant double is effectively atomic, so without the sleep there is no window to
interleave in and the test would pass against the bug it exists to catch. The
sleeps run inside asyncio.to_thread workers, so the event loop stays free and
the second handler genuinely reaches the same code at the same time.
"""

import asyncio
import re
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import calendar_client
import david
import page_lock
import reminder
from conftest import FakeContext, FakeUpdate, run

REPO = Path(__file__).resolve().parent.parent

# Wide enough that the second run QUEUES rather than being refused — these are
# fast writes, and refusing a rapid pair of expense edits would be wrong.
ROUND_TRIP = 0.05


@pytest.fixture(autouse=True)
def fresh_locks():
    page_lock._locks.clear()
    yield
    page_lock._locks.clear()


# ─── 1. THE DOUBLE-DELETE RACE ─────────────────────────────────────────────────

class FakeExpenses:
    """A minimal Notion stand-in for the Expenses database.

    Models the one behaviour the race depends on: Notion excludes ARCHIVED pages
    from query results. That is what makes `D e Carrefour` twice in a row delete
    two different rows — the second query no longer sees the first one.
    """

    def __init__(self, page_ids):
        self.rows = [{"id": pid, "archived": False} for pid in page_ids]
        self.archived = []          # ordered log of what actually got archived

    def query_database(self, db_id, filter_obj=None, sorts=None, page_size=100):
        # Snapshot BEFORE the sleep, not after. Notion evaluates the query
        # server-side and the response then travels back over the wire, so what
        # the caller acts on is the state as of the START of the round trip.
        # Reading self.rows after the sleep would hand back fresh data that a
        # real client could not have had, and the race would vanish.
        snapshot = [{"id": r["id"]} for r in self.rows if not r["archived"]]
        time.sleep(ROUND_TRIP)
        return snapshot, None

    def notion_request(self, method, url, **kwargs):
        page_id = url.rsplit("/", 1)[-1]
        if kwargs.get("json", {}).get("archived"):
            for row in self.rows:
                if row["id"] == page_id:
                    row["archived"] = True
            self.archived.append(page_id)
        return SimpleNamespace(status_code=200, json=lambda: {})


@pytest.fixture
def expenses(monkeypatch):
    fake = FakeExpenses(["exp-1", "exp-2"])
    monkeypatch.setattr(david, "query_database", fake.query_database)
    monkeypatch.setattr(david, "notion_request", fake.notion_request)
    return fake


def send(text):
    update = FakeUpdate(text=text)
    return david.handle_message(update, FakeContext()), update


def test_two_overlapping_deletes_remove_two_different_rows(expenses):
    """THE expense race.

    `D e Carrefour` is find-then-mutate: query by name, take results[0], archive
    it. Overlap two of them and both queries run before either archive, so both
    resolve to the SAME page and both archive it. You are told "deleted
    successfully" twice and the second row is still sitting there.

    Run one after the other, two deletes remove two rows. That is the outcome
    the lock has to reproduce.
    """
    async def main():
        first, u1 = send("D e Carrefour")
        second, u2 = send("D e Carrefour")
        await asyncio.gather(first, second)
        return u1, u2

    u1, u2 = run(main())

    assert expenses.archived == ["exp-1", "exp-2"], (
        f"archived {expenses.archived} — the same row was deleted twice")
    assert u1.message.replied_with("deleted successfully")
    assert u2.message.replied_with("deleted successfully")


@pytest.mark.parametrize("commands", [
    ("D e Carrefour", "D e Carrefour"),
    ("U e Carrefour 5", "U e Carrefour 9"),
    ("U e Carrefour 5", "D e Carrefour"),
], ids=["delete+delete", "update+update", "update+delete"])
def test_no_two_expense_writes_are_ever_in_flight_together(expenses, monkeypatch, commands):
    """The invariant the lock exists to hold, asserted directly.

    Each of these is a find-then-mutate spanning two round trips. What makes
    them safe is that no second one may be anywhere inside that span, whichever
    pair overlaps — so this counts occupancy rather than checking one specific
    corrupted outcome.
    """
    state = {"inside": 0, "peak": 0}

    def tracked(fn):
        def wrapper(*args, **kwargs):
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
            try:
                return fn(*args, **kwargs)
            finally:
                state["inside"] -= 1
        return wrapper

    monkeypatch.setattr(david, "update_Expense", tracked(david.update_Expense))
    monkeypatch.setattr(david, "delete_Expense", tracked(david.delete_Expense))

    async def main():
        first, _ = send(commands[0])
        second, _ = send(commands[1])
        await asyncio.gather(first, second)

    run(main())

    assert state["peak"] == 1, (
        f"{state['peak']} expense writes were inside their find-then-mutate at once")


def test_adding_an_expense_is_not_blocked_by_an_update(expenses):
    """`Add e` is deliberately NOT locked — it is a bare create with no read.

    Asserted so the exclusion is a decision on the record rather than an
    oversight: it must still go through while the expense lock is held.
    """
    async def main():
        async with page_lock.page_lock(david.EXPENSES_ID):
            coro, update = send("Add e Carrefour 2.20")
            await asyncio.wait_for(coro, timeout=2)
            return update

    update = run(main())

    assert update.message.replied_with("Success"), (
        "Add e queued behind the expense lock — it should not take it at all")


# ─── 2. THE REMINDER CHECK-THEN-ACT ────────────────────────────────────────────

class FakeCalendar:
    def __init__(self):
        self.events = []

    def find_conflicts(self, start_dt, end_dt):
        # Snapshot before the round trip — see FakeExpenses.query_database.
        overlapping = [e for e in self.events
                       if e["start_dt"] < end_dt and e["end_dt"] > start_dt]
        time.sleep(ROUND_TRIP)
        return overlapping, None

    def create_event(self, summary, start_dt, minutes):
        self.events.append({"summary": summary, "start_dt": start_dt,
                            "end_dt": start_dt + timedelta(minutes=minutes),
                            "all_day": False})
        return "https://calendar.example/event", None


def test_two_overlapping_reminders_still_warn_about_each_other(monkeypatch):
    """The conflict check must not go blind for the pair it exists to catch.

    find_conflicts-then-create_event is a check-then-act. Run two at once and
    each checks a calendar that does not yet contain the other, so both report a
    clear slot and neither warns — precisely when a warning matters most.
    """
    calendar = FakeCalendar()
    monkeypatch.setattr(reminder, "find_conflicts", calendar.find_conflicts)
    monkeypatch.setattr(reminder, "create_event", calendar.create_event)

    async def one(name):
        update = FakeUpdate(text=f"Remind {name} 12.06 - 14.30")
        await reminder.handle_remind(update, update.message.text)
        return update

    async def main():
        return await asyncio.gather(one("Dentist"), one("Gym"))

    first, second = run(main())

    assert len(calendar.events) == 2, "both reminders should still be created"
    warned = [u for u in (first, second) if u.message.replied_with("Heads up")]
    assert len(warned) == 1, (
        "exactly the second reminder should warn — got "
        f"{len(warned)} warnings, so the conflict check ran blind")


# ─── 3. THREAD SAFETY OF THE SHARED CLIENTS ────────────────────────────────────
# Not locks, but the same class of problem: state that was only ever touched by
# one thread at a time until blocking work moved onto a thread pool.

def test_the_calendar_service_is_per_thread():
    """google-api-python-client sits on httplib2, which is not thread-safe.

    httplib2.Http keeps a plain dict of live connections keyed by host and
    reuses them, so two threads sharing one service can be handed the same
    socket. A single lazily-built module-level service was safe only because
    updates were sequential.
    """
    from concurrent.futures import ThreadPoolExecutor

    calendar_client._thread_local.service = "main-thread-service"
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            from_worker = pool.submit(
                lambda: getattr(calendar_client._thread_local, "service", None)).result()

        assert from_worker is None, "a worker thread saw the main thread's service"
        assert calendar_client._get_service()[0] == "main-thread-service", (
            "_get_service no longer reads the thread-local service")
    finally:
        del calendar_client._thread_local.service


def test_the_shared_calendar_service_singleton_is_gone():
    """A module-level `_service` reintroduces the shared httplib2 connection."""
    assert not hasattr(calendar_client, "_service"), (
        "calendar_client._service is back — that is one Http shared by every thread")


# ─── 4. THE KEYING RULE ────────────────────────────────────────────────────────

# page_lock keys must be DATABASE ids: page ids are not known until the lookup
# the lock has to cover, and user-controlled keys (an expense name, a book
# title) would grow the lock table without bound. See page_lock.py.
ALLOWED_LOCK_KEYS = {"area_db_id", "DIET_ID", "EXPENSES_ID", "CALENDAR_ID"}

LOCKING_MODULES = ["david.py", "implement.py", "implement_diet.py", "reminder.py"]


def test_every_lock_is_keyed_on_a_database_id():
    """Inspects the CALL SITES, because no runtime assertion can tell a database
    id from a page id — both are opaque Notion UUIDs at run time. The whole
    value of the rule is that the next reader sees a database name.
    """
    offenders = []
    for filename in LOCKING_MODULES:
        source = (REPO / filename).read_text(encoding="utf-8")
        for match in re.finditer(r"(?<!def )page_lock\(\s*([A-Za-z_][\w.]*)", source):
            key = match.group(1)
            if key not in ALLOWED_LOCK_KEYS:
                offenders.append(f"{filename}: page_lock({key})")

    assert offenders == [], (
        f"locks keyed on something other than a database id: {offenders}")


def test_every_locking_module_is_actually_checked():
    """Guards the list above: a new module that locks must be added to it."""
    locking = {p.name for p in REPO.glob("*.py")
               if "page_lock(" in p.read_text(encoding="utf-8")
               and p.name != "page_lock.py"}

    assert locking == set(LOCKING_MODULES), (
        f"LOCKING_MODULES is stale — repo locks in {sorted(locking)}")
