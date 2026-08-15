"""The Hard Rule 4 guards on `Cancel`, the calendar's destructive command.

WHAT IS BEING PROTECTED
-----------------------
`Cancel Dentist` resolves a NAME to a calendar event and then deletes it. That is
the same shape as `D e Coffee`, with one difference that makes it worse: Notion's
archive is reversible in place, and Google's delete is not. A wrong guess here is
not recoverable by un-archiving; it is recoverable only by re-creating something
from a snapshot, and only if a snapshot was taken.

So the same guards apply, and each section below drives the REAL handlers through
`david.handle_message` with Google replaced by a recorder. Asserting against the
shipping path matters more than usual, because two of the guards are about what
does NOT happen.

  1. ORDERING       — the lookup is `orderBy="startTime"`, so "the first match"
                      means the same event on two identical calls.
  2. SCOPE          — bounded to CANCEL_SEARCH_DAYS, and REFUSED rather than
                      widened when the read fails.
  3. DISAMBIGUATION — more than one match deletes NOTHING and asks.
  4. UNDO           — the reversal is snapshotted BEFORE the delete, which here
                      is the only order that can exist at all.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

import calendar_safety
import david
import page_lock
import pending_choice
from clients import calendar_client
from config import CANCEL_SEARCH_DAYS
from services import cancel
from conftest import FakeContext, FakeUpdate, run


@pytest.fixture(autouse=True)
def fresh_locks():
    page_lock._locks.clear()
    yield
    page_lock._locks.clear()


def at(hour, minute=0, *, day=20, month=8, year=2026):
    return calendar_client.TIMEZONE.localize(datetime(year, month, day, hour, minute))


def event(event_id, summary, start, end=None, all_day=False):
    """An item shaped the way _list_events_between returns one.

    `raw` carries the Google body the undo snapshot is built from. A double
    without it would let the delete look correct while recording a reversal that
    restores an empty event — the `written_ok` lesson, on a different client.
    """
    return {
        "id": event_id,
        "summary": summary,
        "start_dt": start,
        "end_dt": end,
        "all_day": all_day,
        "raw": {
            "id": event_id,
            "summary": summary,
            "etag": '"read-only"',
            "created": "2026-08-01T10:00:00Z",
            "organizer": {"email": "someone@example.com"},
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (end or start + timedelta(hours=1)).isoformat()},
        },
    }


class FakeCalendar:
    """Records every window read, delete and restore instead of calling Google."""

    def __init__(self, events, error=None):
        self.events   = list(events)
        self.error    = error
        self.windows  = []      # [days, ...] every scope the lookup asked for
        self.deleted  = []      # [event_id, ...] in order
        self.restored = []      # [body, ...] in order
        self.delete_error = None

    def get_events_in_window(self, days):
        self.windows.append(days)
        if self.error:
            return [], self.error
        return list(self.events), None

    def delete_event(self, event_id):
        if self.delete_error:
            return False, self.delete_error
        self.deleted.append(event_id)
        self.events = [e for e in self.events if e["id"] != event_id]
        return True, None

    def restore_event(self, body):
        self.restored.append(body)
        return "https://calendar.example/restored", None


def install(monkeypatch, events, error=None):
    fake = FakeCalendar(events, error)
    monkeypatch.setattr(cancel, "get_events_in_window", fake.get_events_in_window)
    monkeypatch.setattr(cancel, "delete_event", fake.delete_event)
    monkeypatch.setattr(cancel, "restore_event", fake.restore_event)
    return fake


def send(text, context):
    """Drive one real message through the real router."""
    update = FakeUpdate(text=text)
    run(david.handle_message(update, context))
    return update


TWO_DENTISTS = [
    event("evt-early", "Dentist", at(9, 0), at(10, 0)),
    event("evt-late",  "Dentist checkup", at(14, 30), at(15, 30), ),
]

ONE_GYM = [event("evt-gym", "Gym", at(19, 0), at(20, 0))]


# ─── 1. SCOPE ──────────────────────────────────────────────────────────────────

def test_the_lookup_is_bounded_to_the_configured_window(monkeypatch):
    """A destructive command with no scope offers to delete things you have
    forgotten booking. Asserted on the WINDOW asked for, because the bound is
    applied by Google and a fake returns whatever rows it is handed."""
    fake = install(monkeypatch, ONE_GYM)

    send("Cancel Gym", FakeContext())

    assert fake.windows == [CANCEL_SEARCH_DAYS]


def test_a_failed_lookup_is_refused_and_never_widened(monkeypatch):
    """REFUSING BEATS WIDENING.

    Falling back to an unbounded search on a failed windowed one would reach
    months of calendar at exactly the moment the client is unhappy — the same
    asymmetry find_expense_matches states about the month filter.
    """
    fake = install(monkeypatch, ONE_GYM, error="Could not fetch events: 503")

    update = send("Cancel Gym", FakeContext())

    assert fake.windows == [CANCEL_SEARCH_DAYS], "the search was retried wider"
    assert fake.deleted == [], "something was deleted after a failed lookup"
    assert update.message.replied_with("Could not look up")
    assert update.message.replied_with("503")


def test_a_failed_lookup_is_not_reported_as_nothing_found(monkeypatch):
    """An error is NOT an empty result.

    "Google is unreachable" and "you have nothing called Dentist booked" need
    opposite reactions, and reporting the first as the second is how a failed
    lookup becomes "it wasn't there anyway" — after which you never look again.
    """
    install(monkeypatch, [], error="Could not fetch events: 503")

    update = send("Cancel Dentist", FakeContext())

    assert not update.message.replied_with("No event matching")


def test_nothing_matching_says_so_and_names_the_window(monkeypatch):
    install(monkeypatch, ONE_GYM)

    update = send("Cancel Dentist", FakeContext())

    assert update.message.replied_with("No event matching 'Dentist'")
    assert update.message.replied_with(str(CANCEL_SEARCH_DAYS)), (
        "the reply did not say how far ahead it looked, so 'not found' is "
        "indistinguishable from 'outside the window'")


def test_matching_is_on_the_title_only(monkeypatch):
    """The Calendar API's own `q` searches descriptions, locations and attendees.

    Using it would make `Cancel Gym` match an unrelated event whose description
    merely mentions the gym — a destructive command that matches on prose is one
    that surprises you. The filter is applied here, on the summary.
    """
    noise = event("evt-noise", "Coffee with Sam", at(11, 0))
    noise["raw"]["description"] = "at the gym cafe"
    fake = install(monkeypatch, [noise] + ONE_GYM)

    send("Cancel Gym", FakeContext())

    assert fake.deleted == ["evt-gym"]


def test_matching_is_case_insensitive_and_substring(monkeypatch):
    fake = install(monkeypatch, ONE_GYM)

    send("Cancel gy", FakeContext())

    assert fake.deleted == ["evt-gym"]


# ─── 2. DISAMBIGUATION ─────────────────────────────────────────────────────────

def test_two_matching_events_delete_nothing_and_ask(monkeypatch):
    """THE guard. A destructive command whose target is ambiguous is not a
    command yet, and on a calendar the wrong guess is not un-archivable."""
    fake    = install(monkeypatch, TWO_DENTISTS)
    context = FakeContext()

    update = send("Cancel Dentist", context)

    assert fake.deleted == [], "an ambiguous cancel deleted something"
    assert update.message.replied_with("2"), "the count was not named"
    assert update.message.replied_with("Which one should I cancel?")


def test_the_numbered_list_names_the_times_that_tell_them_apart(monkeypatch):
    """Two events called Dentist differ by WHEN and by nothing else you can read.

    A list showing only titles would be two identical lines and a number to pick
    between them at random, which is the guess this path exists to remove.
    """
    install(monkeypatch, TWO_DENTISTS)

    update = send("Cancel Dentist", FakeContext())
    sent = "\n".join(update.message.reply_texts)

    assert "09:00" in sent and "14:30" in sent
    assert "Thursday 20 August" in sent, "the day was missing from the list"


def test_answering_with_a_number_deletes_the_event_at_that_index(monkeypatch):
    """ASSERTED ON THE ID, NOT THE NAME.

    Both matches contain "Dentist", so a name assertion passes whichever one was
    deleted — which is the whole failure this command is guarded against.
    """
    fake    = install(monkeypatch, TWO_DENTISTS)
    context = FakeContext()

    send("Cancel Dentist", context)
    update = send("2", context)

    assert fake.deleted == ["evt-late"], (
        f"deleted {fake.deleted} — the number did not select what was offered")
    assert update.message.replied_with("Cancelled")


def test_choosing_the_first_deletes_the_first(monkeypatch):
    """The mirror of the row above. One of the two passing alone would be
    satisfied by a command that always deletes the same index."""
    fake    = install(monkeypatch, TWO_DENTISTS)
    context = FakeContext()

    send("Cancel Dentist", context)
    send("1", context)

    assert fake.deleted == ["evt-early"]


def test_an_out_of_range_number_leaves_the_list_answerable(monkeypatch):
    """A mistyped `5` should cost a keystroke, not the whole command."""
    fake    = install(monkeypatch, TWO_DENTISTS)
    context = FakeContext()

    send("Cancel Dentist", context)
    missed = send("5", context)

    assert fake.deleted == []
    assert missed.message.replied_with("Pick a number between 1 and 2")

    send("2", context)
    assert fake.deleted == ["evt-late"], "the list stopped being answerable"


def test_the_pending_list_expires(monkeypatch):
    """A number typed much later must not delete an event you have forgotten
    was offered. Two minutes is the bound; this ages the record rather than
    sleeping through it."""
    fake    = install(monkeypatch, TWO_DENTISTS)
    context = FakeContext()

    send("Cancel Dentist", context)

    kind, pending, events = context.user_data[pending_choice.PENDING_KEY]
    context.user_data[pending_choice.PENDING_KEY] = (
        kind,
        pending._replace(expires_at=pending.expires_at
                         - pending_choice.PENDING_TTL_SECONDS - 1),
        events,
    )

    update = send("2", context)

    assert fake.deleted == [], "an expired list still deleted an event"
    assert update.message.replied_with("I didn't get that"), (
        "an expired list should leave a bare number unrecognised, "
        f"got {update.message.reply_texts}")


def test_a_single_match_is_deleted_without_asking(monkeypatch):
    """The disambiguation must not become a prompt on every cancel — a
    confirmation you always see is a confirmation you stop reading."""
    fake = install(monkeypatch, ONE_GYM)

    update = send("Cancel Gym", FakeContext())

    assert fake.deleted == ["evt-gym"]
    assert not update.message.replied_with("Which one")
    assert update.message.replied_with("Cancelled")


# ─── 3. ONE SLOT ACROSS EVERY DESTRUCTIVE COMMAND ──────────────────────────────

def test_an_ambiguous_cancel_replaces_a_live_expense_list(monkeypatch):
    """WHY THERE IS ONE SLOT AND NOT TWO.

    With a slot per feature, an expense list and an event list could be live at
    the same instant and `2` would mean whichever prompt you had scrolled to —
    the exact ambiguity a numbered list exists to remove, one level up. The last
    list printed is the one a number answers.
    """
    from services import expenses

    rows = [{"id": f"exp-{i}", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Coffee"}]},
        "Amount": {"number": 2.0 + i},
        "Date": {"date": {"start": "2026-08-06"}},
        "Category": {"multi_select": [{"name": "Food"}]}}} for i in range(2)]
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: (rows, None))
    deleted_expenses = []
    monkeypatch.setattr(expenses, "delete_Expense",
                        lambda page_id: (deleted_expenses.append(page_id), (True, None))[1])

    fake    = install(monkeypatch, TWO_DENTISTS)
    context = FakeContext()

    send("D e Coffee", context)            # expense list is live
    send("Cancel Dentist", context)        # event list replaces it
    send("1", context)

    assert deleted_expenses == [], "the number answered the list it replaced"
    assert fake.deleted == ["evt-early"]


# ─── 4. UNDO ───────────────────────────────────────────────────────────────────

def test_a_cancelled_event_can_be_put_back(monkeypatch):
    fake    = install(monkeypatch, ONE_GYM)
    context = FakeContext()

    send("Cancel Gym", context)
    update = send("undo", context)

    assert len(fake.restored) == 1
    assert fake.restored[0]["summary"] == "Gym"
    assert update.message.replied_with("Re-created")


def test_the_undo_body_carries_no_read_only_fields(monkeypatch):
    """Google rejects an insert containing id, etag, created or organizer.

    Storing the raw event would make `undo` a 400 rather than a restore — and it
    would fail only when you needed it, which is the worst time to find out.
    """
    fake    = install(monkeypatch, ONE_GYM)
    context = FakeContext()

    send("Cancel Gym", context)
    send("undo", context)

    body = fake.restored[0]
    for read_only in ("id", "etag", "created", "organizer", "updated", "htmlLink"):
        assert read_only not in body, f"the restore body carried {read_only!r}"
    assert body["start"] and body["end"], "the restore lost the event's time"


def test_the_undo_says_the_event_is_new_rather_than_restored(monkeypatch):
    """The message must not overclaim.

    The event comes back with a NEW ID, so anything Google keys to the old one —
    guest replies above all — does not come back with it. A confirmation that
    says "restored" is one you find out was wrong at the wrong moment.
    """
    install(monkeypatch, ONE_GYM)
    context = FakeContext()

    send("Cancel Gym", context)
    update = send("undo", context)

    assert update.message.replied_with("new event")


def test_undo_is_consumed_so_it_cannot_run_twice(monkeypatch):
    """Re-inserting a restored event would leave you with two of it."""
    fake    = install(monkeypatch, ONE_GYM)
    context = FakeContext()

    send("Cancel Gym", context)
    send("undo", context)
    second = send("undo", context)

    assert len(fake.restored) == 1
    assert second.message.replied_with("Nothing to undo")


def test_a_failed_delete_records_no_undo(monkeypatch):
    """An undo record for a delete that failed would offer to re-create an event
    still sitting on the calendar, and applying it would leave you with two."""
    fake = install(monkeypatch, ONE_GYM)
    fake.delete_error = "Could not delete event: 403"
    context = FakeContext()

    cancelled = send("Cancel Gym", context)
    undone    = send("undo", context)

    assert cancelled.message.replied_with("Could not cancel")
    assert cancelled.message.replied_with("403")
    assert fake.restored == []
    assert undone.message.replied_with("Nothing to undo")


def test_a_failed_restore_keeps_the_undo_available(monkeypatch):
    """The reversal has not happened, so it must stay offered — otherwise one
    transient 503 turns a recoverable delete into a permanent one."""
    fake    = install(monkeypatch, ONE_GYM)
    context = FakeContext()

    send("Cancel Gym", context)

    monkeypatch.setattr(cancel, "restore_event",
                        lambda body: (None, "Could not restore event: 503"))
    failed = send("undo", context)

    assert failed.message.replied_with("Could not put")
    assert pending_choice.undo_kind(context.user_data) == calendar_safety.KIND, (
        "the reversal was dropped after a failure that did not perform it")

    monkeypatch.setattr(cancel, "restore_event", fake.restore_event)
    send("undo", context)
    assert len(fake.restored) == 1


def test_undo_reverses_the_last_destructive_thing_whatever_it_was(monkeypatch):
    """One undo slot, like one pending slot.

    An expense delete followed by a cancel must leave `undo` pointing at the
    CANCEL. A per-feature slot would have `undo` reverse the expense and report
    success, while the event you actually just deleted stays deleted.
    """
    from services import expenses

    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: (
        [{"id": "exp-1", "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Coffee"}]},
            "Amount": {"number": 2.0},
            "Date": {"date": {"start": "2026-08-06"}},
            "Category": {"multi_select": [{"name": "Food"}]}}}], None))
    monkeypatch.setattr(expenses, "delete_Expense", lambda page_id: (True, None))
    unarchived = []
    monkeypatch.setattr(expenses, "set_archived",
                        lambda page_id, archived: (unarchived.append(page_id), (True, None))[1])

    fake    = install(monkeypatch, ONE_GYM)
    context = FakeContext()

    send("D e Coffee", context)
    send("Cancel Gym", context)
    send("undo", context)

    assert unarchived == [], "undo reversed the expense, not the cancel"
    assert len(fake.restored) == 1


# ─── 5. THE LOCK ───────────────────────────────────────────────────────────────

def test_two_overlapping_cancels_do_not_both_delete_the_same_event(monkeypatch):
    """The double-delete race, on a calendar.

    Find-then-mutate over two round trips: overlap two and both lookups run
    before either delete, so both resolve to the same event, both delete it, and
    the second reports success for a deletion it did not perform. Only reachable
    at all because the LOOKUP is inside the lock — covering the delete alone
    leaves both reads free to overlap.
    """
    import time

    fake = install(monkeypatch, ONE_GYM)
    real_lookup = fake.get_events_in_window

    def slow_lookup(days):
        # Snapshot BEFORE the sleep: Google evaluates the query server-side and
        # the response then travels back, so what the caller acts on is the state
        # as of the START of the round trip. Reading after the sleep would hand
        # back fresh data no real client could have had, and the race vanishes.
        result = real_lookup(days)
        time.sleep(0.05)
        return result

    monkeypatch.setattr(cancel, "get_events_in_window", slow_lookup)

    async def main():
        first  = david.handle_message(FakeUpdate(text="Cancel Gym"), FakeContext())
        second = david.handle_message(FakeUpdate(text="Cancel Gym"), FakeContext())
        await asyncio.gather(first, second)

    run(main())

    assert fake.deleted == ["evt-gym"], (
        f"deleted {fake.deleted} — the one event was deleted twice")


def test_a_cancel_waits_for_a_reminder_rather_than_racing_it(monkeypatch):
    """`Remind` and `Cancel` share CALENDAR_ID, and they should.

    A conflict check running while an event is mid-delete would see an event
    that is about to stop existing. Serialising them is free — both take about a
    second — and it is what one lock key per DATABASE buys.
    """
    install(monkeypatch, ONE_GYM)

    async def main():
        async with page_lock.page_lock(calendar_client.CALENDAR_ID):
            coro = david.handle_message(FakeUpdate(text="Cancel Gym"), FakeContext())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(coro, timeout=0.2)

    run(main())


# ─── 6. THE MESSAGES ───────────────────────────────────────────────────────────

def test_an_event_name_with_markdown_in_it_does_not_break_the_prompt(monkeypatch):
    """Event titles are arbitrary data from Google. One unbalanced `*` used to
    make Telegram reject the whole message, so the command looked ignored."""
    install(monkeypatch, [
        event("evt-1", "Gym *2 sets", at(9, 0)),
        event("evt-2", "Gym *3 sets", at(18, 0)),
    ])

    update = send("Cancel Gym", FakeContext())
    sent = "\n".join(update.message.reply_texts)

    assert r"\*" in sent, "an asterisk in an event title reached Telegram unescaped"


def test_an_event_with_no_title_does_not_break_the_lookup(monkeypatch):
    """An event with no summary is a legitimate event, and a `None.lower()` here
    would take down the whole command over a row it was never going to match."""
    bare = event("evt-bare", "Dentist", at(9, 0))
    bare["summary"] = None
    install(monkeypatch, [bare, TWO_DENTISTS[1]])

    update = send("Cancel Dentist", FakeContext())

    # It cannot MATCH on a missing title, so what is asserted is that the lookup
    # survives one — the other event is still offered, alone, and deleted.
    assert update.message.replied_with("Cancelled")
