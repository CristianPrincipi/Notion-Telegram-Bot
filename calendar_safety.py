"""Ambiguity and reversal for `Cancel` — expense_safety's mirror for the calendar.

WHY THIS EXISTS
---------------
`Cancel Dentist` is find-then-mutate over a name, which is the shape Hard Rule 4
governs: resolve a name to something, then destroy it. Every failure mode the
expense commands were fixed for is available here, and two of them are worse
because a calendar event cannot be un-archived.

The three guards are the same three, adapted:

  1. ORDERING. Google's events.list is queried with `orderBy="startTime"`, so
     "the first match" means the earliest one and means it identically on two
     calls. Google gives that for free; Notion documents no ordering, which is
     why `CREATED_DESC` had to be asked for explicitly over there.

  2. DISAMBIGUATION (this module). More than one match deletes nothing. The
     matches are listed with the thing that separates them — the day and time —
     and the command waits for a number.

  3. UNDO (this module). The reversal is recorded before the delete, from the
     event the lookup already returned.

WHAT THE UNDO CAN AND CANNOT PUT BACK
-------------------------------------
Google has no reversible archive. `events.delete` is final, so `undo` RE-CREATES
the event from a snapshot rather than un-deleting it, and the difference is worth
stating plainly because it is visible to you afterwards:

  restored — the title, the day and time, all-day-ness, description, location,
             recurrence and the event's own alerts. Everything David itself
             writes with `Remind`, and everything you would look for.

  not      — the event's ID, and therefore anything keyed to it: attendee
             responses, the original creator, per-guest state. `Remind` creates
             none of those, but an event you made in Google with three guests on
             it comes back without their replies.

So the confirmation says the event was re-created, not that the cancellation was
undone. A message claiming more than the mechanism delivers is how you find out
months later, which is the failure this codebase is largely written against.

The state machine underneath — the single slot, the two-minute expiry, the
strict-digit selection — is `pending_choice.py`, shared with `expense_safety.py`.

This module holds NO Google calls and no Telegram sends: it decides and it
formats. `services/cancel.py` owns the lookup, the delete and the lock.
"""

from typing import NamedTuple

import pending_choice
from clients.calendar_client import restorable_body
from pending_choice import PENDING_TTL_SECONDS
from telegram_text import escape_md

# Which feature owns the shared pending/undo slot when it holds an event.
KIND = "event"


class Choice(NamedTuple):
    """One event the user has to choose between, reduced to what tells them apart.

    Two events called `Dentist` differ by WHEN, and by nothing else a person
    would use. The event ID they also differ by is meaningless to read, so it is
    carried and never shown — and it is the only field the delete actually uses.
    """

    event_id: str
    summary: str
    start_dt: object
    end_dt: object
    all_day: bool


class Pending(NamedTuple):
    """A `Cancel` that is waiting to be told which event it meant.

    No equivalent of the expense `amount`/`category`: a cancel carries no new
    values to write, so the follow-up number is the whole of the missing input.
    """

    query: str
    choices: tuple
    expires_at: float

    def expired(self, now: float | None = None) -> bool:
        return pending_choice.is_expired(self.expires_at, now)


class Undo(NamedTuple):
    """How to put a cancelled event back, recorded before it is deleted.

    `body` is an insert body, not the raw Google item: the raw item carries
    read-only fields (id, etag, created, organizer, …) that an insert rejects
    outright, so storing it would make undo a 400 rather than a restore.

    Built from the event the LOOKUP returned. After the delete there is nothing
    left to read, so a snapshot taken any later cannot exist at all — the
    expense path's "never re-read after the write" rule, in its strictest form.
    """

    summary: str
    body: dict


def choice_from_event(event: dict) -> Choice:
    """Reduce a calendar event to the fields that distinguish it.

    Defensive about a missing summary for the same reason the expense reader is
    about a missing Amount: an untitled event is a legitimate event, and a
    KeyError here would take down the prompt that exists to prevent a wrong
    delete.
    """
    return Choice(
        event_id=event.get("id") or "",
        summary=event.get("summary") or "(no title)",
        start_dt=event.get("start_dt"),
        end_dt=event.get("end_dt"),
        all_day=bool(event.get("all_day")),
    )


def undo_from_event(event: dict) -> Undo:
    """The reversal for `event`, snapshotted from the lookup's own result."""
    return Undo(summary=event.get("summary") or "(no title)",
                body=restorable_body(event.get("raw") or {}))


# ─── THE PENDING CHOICE ────────────────────────────────────────────────────────

def remember_pending(user_data, query: str, events: list,
                     now: float | None = None) -> Pending:
    """Park a `Cancel` until it is told which event it meant.

    Shares one slot with the expense commands, so an ambiguous `Cancel` replaces
    a live expense list and vice versa. That is deliberate: David prints one list
    at a time and a bare number can only sensibly answer the last one printed.
    """
    pending = Pending(
        query=query,
        choices=tuple(choice_from_event(event) for event in events),
        expires_at=pending_choice.deadline(now),
    )
    pending_choice.remember(user_data, KIND, pending, events)
    return pending


def has_pending(user_data, now: float | None = None) -> bool:
    """True when a live list of EVENT matches is waiting for a number."""
    return (pending_choice.has_pending(user_data, now)
            and pending_choice.kind_of(user_data) == KIND)


def take_pending(user_data, selection: int, now: float | None = None):
    """Resolve a reply to the printed list. Returns (pending, event, error)."""
    return pending_choice.take(user_data, KIND, selection, now)


def remember_undo(user_data, undo: Undo) -> None:
    pending_choice.remember_undo(user_data, KIND, undo)


def take_undo(user_data):
    """The last cancelled event, consumed. Returns (undo, error)."""
    return pending_choice.take_undo(user_data, KIND)


# ─── MESSAGES ──────────────────────────────────────────────────────────────────
# Everything below is interpolated into a Markdown message, so every value that
# came from Google or from the user goes through escape_md. An event named
# `Gym *2` is ordinary; an unescaped one makes Telegram reject the whole prompt
# and the command looks like it was ignored. See telegram_text.py.

def when(choice: Choice) -> str:
    """'Friday 07 August at 10:00' — enough to tell two same-named events apart.

    THE WEEKDAY AND THE DATE ARE BOTH THERE, and that is the same decision
    `Remind`'s confirmation makes: a bare time is not enough to choose between
    two Dentists this week, and a bare date is not enough to notice you are about
    to delete the wrong one. The year is left out unless it differs, because
    every event in the search window is within a month or so of today.
    """
    start = choice.start_dt
    if start is None:
        return "at an unknown time"
    if choice.all_day:
        return f"{start.strftime('%A %d %B')} (all day)"
    if choice.end_dt is not None:
        return (f"{start.strftime('%A %d %B')} at "
                f"{start.strftime('%H:%M')}–{choice.end_dt.strftime('%H:%M')}")
    return f"{start.strftime('%A %d %B')} at {start.strftime('%H:%M')}"


def format_choices(pending: Pending) -> str:
    """The numbered list, with the times that tell two same-named events apart."""
    minutes = PENDING_TTL_SECONDS // 60

    lines = [
        f"⚠️ *{len(pending.choices)}* events match "
        f"'{escape_md(pending.query)}'.",
        "Which one should I cancel?",
        "",
    ]
    for number, choice in enumerate(pending.choices, start=1):
        lines.append(f"*{number}.* {escape_md(choice.summary)} — "
                     f"{escape_md(when(choice))}")

    lines += ["", f"Reply with a number. Nothing is cancelled until you do, "
                  f"and this list lapses in {minutes} minutes."]
    return "\n".join(lines)


def format_cancelled(choice: Choice) -> str:
    return (f"🗑️ Cancelled *{escape_md(choice.summary)}* — "
            f"{escape_md(when(choice))}.\n"
            f"Send `undo` to put it back.")


def format_restored(undo: Undo) -> str:
    """The confirmation, and it does not overclaim.

    "Re-created", not "restored": the event comes back with a NEW ID, so
    anything Google keyed to the old one — attendee replies above all — does not
    come back with it. Saying so costs one clause and stops you discovering it
    at the wrong moment.
    """
    return (f"↩️ Re-created *{escape_md(undo.summary)}* on your calendar.\n"
            f"_It is a new event, so any guest replies on the original are not "
            f"restored._")
