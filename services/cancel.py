"""`Cancel [Name]` — delete a calendar event David (or you) created.

`Remind` has been creating events since it was written and nothing anywhere could
remove one. This is the other half, and because it is a destructive command that
resolves a NAME to a thing it destroys, Hard Rule 4 applies in full.

THE FOUR GUARDS, and they are the expense ones with the nouns changed:

  1. ORDERING IS DEFINED. The lookup goes through
     `clients.calendar_client.get_events_in_window`, which queries Google with
     `orderBy="startTime"` — so "the first match" means the earliest one and
     means it identically on two calls. That is what `CREATED_DESC` buys for the
     expense lookups, except Google gives it and Notion does not.

  2. THE SEARCH IS BOUNDED, AND REFUSED RATHER THAN WIDENED. `CANCEL_SEARCH_DAYS`
     from today. Widening on a failure would restore exactly the reach the window
     exists to remove, at the moment David is least sure of its own state — the
     same asymmetry `find_expense_matches` states about the month filter.

  3. MORE THAN ONE MATCH DELETES NOTHING and asks, via `calendar_safety`.

  4. THE REVERSAL IS RECORDED BEFORE THE DELETE, snapshotted from the event the
     LOOKUP returned. Here that is not merely the right ordering, it is the only
     possible one: after `events.delete` there is nothing left to read.

AND THE LOCK COVERS THE LOOKUP, NOT JUST THE DELETE. Find-then-mutate over two
round trips: two cancels overlapping would both resolve to the same event, both
delete it, and the second would report success for a deletion it did not perform
— the double-delete race from the expense path, on a calendar. `Remind` already
locks `CALENDAR_ID` for its own check-then-act, so the two serialise against each
other for free, which is right: a conflict check that runs while an event is
being deleted should not see it.
"""

import asyncio
import logging

import calendar_safety
from clients.calendar_client import (
    CALENDAR_ID, delete_event, get_events_in_window, restore_event,
)
from config import CANCEL_SEARCH_DAYS
from page_lock import WRITE_LOCK_TIMEOUT_SECONDS, PageBusy, page_lock
from telegram_text import escape_md

logger = logging.getLogger(__name__)

# Cancel [Name]. Greedy and unvalidated, exactly like `D e (?P<name>.+)`: what
# follows is the name, and a name that matches nothing is reported as matching
# nothing. There is no amount or category to parse out, so there is nothing here
# for a stricter pattern to protect.
CANCEL_PATTERN = r"(?i)cancel\s+(?P<name>.+)"

BUSY_CALENDAR_MESSAGE = ("⏳ Another calendar write is still running. "
                         "Give it a second and try again.")


def find_event_matches(name: str):
    """Events in the search window whose title contains `name`. Returns (events, error).

    Case-insensitive substring, which is what `contains` gives on the Notion
    side and therefore what you already expect from `D e`. The filtering is done
    HERE rather than by Google because the Calendar API's `q` parameter is a
    full-text search across description, location and attendees as well as the
    title — so `Cancel Gym` would match an unrelated event whose description
    mentions the gym, and a destructive command that matches on prose is a
    destructive command that surprises you.

    Full event dicts, not IDs: the caller needs the times to tell two matches
    apart in the prompt, and the raw item to snapshot for `undo`. Both arrive
    with the query, so carrying them costs no extra request.
    """
    events, err = get_events_in_window(CANCEL_SEARCH_DAYS)
    if err:
        # REFUSING BEATS WIDENING, and this is the branch where that matters:
        # falling back to an unbounded search on a failed windowed one would
        # reach months of calendar at exactly the moment the client is unhappy.
        return [], err

    needle = name.strip().lower()
    return [e for e in events if needle in (e.get("summary") or "").lower()], None


async def run_cancel(user_data, name: str, *, notify, notify_md=None) -> None:
    """Resolve which event `name` means, then either delete it or ask.

    THE ONE RULE: more than one match deletes NOTHING. A destructive command
    whose target is ambiguous is not a command yet — and a wrongly deleted
    calendar event is worse than a wrongly archived Notion row, because Notion's
    archive is reversible in place and Google's delete is not.

    THE LOOKUP IS INSIDE THE LOCK. Only the single-match path deletes here; the
    ambiguous one releases the lock and waits for your number, because holding it
    across a reply would stall `Remind` for as long as you took to answer, and
    the selection writes by event ID so it has no lookup left to protect.
    """
    notify_md = notify_md or notify

    await notify(f"🔍 Finding '{name}' to cancel…")

    try:
        async with page_lock(CALENDAR_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            matches, err = await asyncio.to_thread(find_event_matches, name)

            if err is None and len(matches) == 1:
                await _apply_cancel(user_data, matches[0],
                                    notify=notify, notify_md=notify_md)
                return
    except PageBusy:
        await notify(BUSY_CALENDAR_MESSAGE)
        return

    if err:
        # An error is NOT an empty result. "Google is unreachable" and "you have
        # nothing called Dentist booked" need opposite reactions, and reporting
        # the first as the second is how a failed lookup becomes "it wasn't
        # there anyway" — and then you never look again.
        await notify_md(f"❌ Could not look up '{escape_md(name)}':\n{escape_md(err)}")
        return

    if not matches:
        await notify(f"❌ No event matching '{name}' in the next "
                     f"{CANCEL_SEARCH_DAYS} days.")
        return

    pending = calendar_safety.remember_pending(user_data, name, matches)
    await notify_md(calendar_safety.format_choices(pending))


async def _apply_cancel(user_data, event, *, notify, notify_md) -> None:
    """Delete one event, and record how to put it back. CALL UNDER THE CALENDAR LOCK.

    The undo snapshot is built from `event` — the item as the lookup found it —
    BEFORE the delete, which here is the only order that can work at all: an
    event that has been deleted cannot be read back to snapshot.
    """
    choice = calendar_safety.choice_from_event(event)
    undo   = calendar_safety.undo_from_event(event)

    if not choice.event_id:
        # Nothing to delete BY ID, and deleting by anything else is the guess
        # this whole path exists to refuse.
        await notify_md(f"❌ '{escape_md(choice.summary)}' has no event ID I can "
                        f"cancel — open it in Google Calendar instead.")
        return

    success, err = await asyncio.to_thread(delete_event, choice.event_id)

    if not success:
        await notify_md(f"❌ Could not cancel '{escape_md(choice.summary)}':\n"
                        f"{escape_md(err)}")
        return

    # Only now. An undo record for a delete that failed would offer to re-create
    # an event that is still sitting on the calendar, and applying it would leave
    # you with two.
    calendar_safety.remember_undo(user_data, undo)

    await notify_md(calendar_safety.format_cancelled(choice))


async def run_selection(user_data, selection: int, *, notify, notify_md=None) -> None:
    """A bare number answering the numbered list of matching events.

    No lookup runs here: the event was chosen from a list David printed, so this
    is a delete against a known ID rather than a find-then-mutate. The lock is
    still taken, to keep it ordered against `Remind` and against another cancel.
    """
    notify_md = notify_md or notify

    _pending, event, err = calendar_safety.take_pending(user_data, selection)
    if err:
        await notify(f"❌ {err}")
        return

    try:
        async with page_lock(CALENDAR_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            await _apply_cancel(user_data, event, notify=notify, notify_md=notify_md)
    except PageBusy:
        await notify(BUSY_CALENDAR_MESSAGE)


async def run_undo(user_data, *, notify, notify_md=None) -> None:
    """`undo` after a `Cancel` — re-create the event from its snapshot.

    An ordinary insert against a body David already holds, so it re-runs no
    lookup: an undo that had to find its own target could pick a different event
    than the one it is undoing, which would make the recovery command a third way
    to hit the wrong one.
    """
    notify_md = notify_md or notify

    undo, err = calendar_safety.take_undo(user_data)
    if err:
        await notify(f"❌ {err}")
        return

    try:
        async with page_lock(CALENDAR_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            _link, err = await asyncio.to_thread(restore_event, undo.body)
    except PageBusy:
        # Put it back: the event has not been re-created, so the reversal must
        # stay available.
        calendar_safety.remember_undo(user_data, undo)
        await notify(BUSY_CALENDAR_MESSAGE)
        return

    if err:
        calendar_safety.remember_undo(user_data, undo)
        await notify_md(f"❌ Could not put '{escape_md(undo.summary)}' back:\n"
                        f"{escape_md(err)}")
        return

    await notify_md(calendar_safety.format_restored(undo))
