"""Ambiguity and reversal for the destructive expense commands.

WHY THIS EXISTS
---------------
`U e Coffee 4` and `D e Coffee` were find-then-mutate over a `contains` filter,
and they acted on `results[0]`. Notion does not document an ordering for query
results, so "the first match" was whatever the API happened to return — with two
Coffees on the page you were told "deleted successfully" and one of them was
gone, but not reliably the one you meant. Nothing errored, and the row that
vanished was only discoverable by opening Notion.

Three separate things make that safe, and they are deliberately independent:

  1. ORDERING. Every expense lookup now sorts `created_time` descending, so
     "the first match" means the most recent one — a definition that holds
     across calls instead of changing between them. That alone fixes the
     single-match case, which is most of them.

  2. DISAMBIGUATION (this module). When more than one row matches, no write
     happens at all. The matches are listed with the numbers that separate them
     — amount, date, category — and the command waits for you to pick. A
     destructive command with an ambiguous target is not a command yet.

  3. UNDO (this module). Every destructive write records how to reverse itself
     BEFORE it is made, so the answer to "that took the wrong row" is one word
     rather than a trip to the Notion trash.

WHAT MOVED OUT, AND WHAT DID NOT
--------------------------------
The state machine itself — the two-minute expiry, what counts as a selection,
what an out-of-range number does, and the single slot all of it lives in — is
`pending_choice.py` now, shared with `calendar_safety.py`. It moved when `Cancel`
needed the same behaviour over completely different fields, and it moved rather
than being copied because the alternative is two live lists that one bare number
cannot tell apart. See that module's docstring for the argument.

What stayed here is everything expense-shaped: which fields tell two matching
rows apart, how a Notion page reduces to one, what a reversal of an expense write
looks like, and every message either prints. The public surface below is
unchanged — `services/expenses.py` and the tests call exactly what they called
before.

NO ConversationHandler. It would mean registering a second handler group and
teaching it the owner filter, for a single one-shot question whose whole state
is "which list did I just print". A dict with a deadline is the smaller thing
that fails in more obvious ways.

This module holds NO Notion calls and no Telegram sends: it decides and it
formats. `services/expenses.py` owns the queries, the writes and the locking;
`bot/` owns the routing and hands the dict down.
"""

from typing import NamedTuple

import pending_choice
from clients.notion_client import get_page_title
from pending_choice import PENDING_TTL_SECONDS, parse_selection  # noqa: F401 — re-exported
from telegram_text import escape_md

# Which feature owns the shared pending/undo slot when it holds an expense.
KIND = "expense"

# The two destructive actions. `Add e` is not one: it creates a row and targets
# nothing, so it can be neither ambiguous nor wrong about which row it hit.
DELETE = "delete"
UPDATE = "update"

# Re-exported so the tests and the router keep naming one place for the slot.
# There is only one now, and it is not expense-specific — see pending_choice.
PENDING_KEY = pending_choice.PENDING_KEY
UNDO_KEY    = pending_choice.UNDO_KEY


class Choice(NamedTuple):
    """One row the user has to choose between, reduced to what tells them apart.

    Two expenses matching `Coffee` differ by amount, date and category — the
    page ID they also differ by is meaningless to a human, so it is carried but
    never shown.
    """

    page_id: str
    name: str
    amount: float | None
    date: str
    category: str


class Pending(NamedTuple):
    """A destructive command that is waiting to be told which row it meant.

    `amount` and `category` are the NEW values an `U e` will write once a row is
    chosen; they are None for a delete. Carrying them here is what lets the
    follow-up be a bare number instead of the whole command retyped.
    """

    action: str
    query: str
    amount: float | None
    category: str | None
    choices: tuple
    expires_at: float

    def expired(self, now: float | None = None) -> bool:
        return pending_choice.is_expired(self.expires_at, now)


class Undo(NamedTuple):
    """How to reverse the last destructive write, recorded before it is made.

    For a DELETE that is `{"archived": False}` — Notion's archive is reversible,
    which is why `D e` was safe to build on it in the first place.

    For an UPDATE it is the page's previous `Amount` and `Category`, snapshotted
    from the row that the lookup already returned. That costs no extra read: the
    query that found the row carried its properties along. Without the snapshot
    an overwrite is unrecoverable — Notion keeps no property history an
    integration can reach.
    """

    action: str
    page_id: str
    name: str
    properties: dict | None


# ─── READING A NOTION ROW ──────────────────────────────────────────────────────
# Both helpers below are defensive about missing properties on purpose. A row
# whose `Amount` was cleared by hand in Notion is a legitimate row, and a
# KeyError here would take down the very prompt that exists to prevent a wrong
# write.

def choice_from_page(page: dict) -> Choice:
    """Reduce a Notion expense row to the fields that distinguish it."""
    props = page.get("properties") or {}
    categories = (props.get("Category") or {}).get("multi_select") or []
    return Choice(
        page_id=page.get("id") or "",
        name=get_page_title(page),
        amount=(props.get("Amount") or {}).get("number"),
        date=((props.get("Date") or {}).get("date") or {}).get("start") or "",
        category=", ".join(option.get("name") or "" for option in categories).strip(", "),
    )


def previous_properties(page: dict) -> dict:
    """The Notion properties payload that would put `page` back as it is now.

    Shaped as a `pages` PATCH body's `properties` value, so undoing an update is
    the same call as making one — with the old numbers.
    """
    props = page.get("properties") or {}
    categories = (props.get("Category") or {}).get("multi_select") or []
    return {
        "Amount": {"number": (props.get("Amount") or {}).get("number")},
        "Category": {"multi_select": [{"name": option["name"]} for option in categories
                                      if option.get("name")]},
    }


# ─── THE PENDING CHOICE ────────────────────────────────────────────────────────

def remember_pending(user_data, action: str, query: str, pages: list,
                     amount: float | None = None, category: str | None = None,
                     now: float | None = None) -> Pending:
    """Park a destructive command until it is told which row it meant.

    A second ambiguous command REPLACES the first rather than queueing behind
    it. David has one user and prints one list at a time, so the only list a
    number can sensibly refer to is the last one printed — keeping an older one
    answerable would make `1` mean whichever prompt you had scrolled to. That
    argument is why the slot is shared with `Cancel` rather than duplicated:
    two independent lists would put it back, across features.
    """
    pending = Pending(
        action=action,
        query=query,
        amount=amount,
        category=category,
        choices=tuple(choice_from_page(page) for page in pages),
        expires_at=pending_choice.deadline(now),
    )
    pending_choice.remember(user_data, KIND, pending, pages)
    return pending


def has_pending(user_data, now: float | None = None) -> bool:
    """True when a live list of EXPENSE matches is waiting for a number.

    Narrower than `pending_choice.has_pending`, which answers for any feature.
    The dispatch loop asks the general one and routes by kind; this one is for a
    caller that specifically means expenses.
    """
    return (pending_choice.has_pending(user_data, now)
            and pending_choice.kind_of(user_data) == KIND)


def take_pending(user_data, selection: int, now: float | None = None):
    """Resolve a reply to the printed list. Returns (pending, page, error)."""
    return pending_choice.take(user_data, KIND, selection, now)


def clear_pending(user_data) -> None:
    pending_choice.clear(user_data)


# ─── THE UNDO RECORD ───────────────────────────────────────────────────────────

def remember_undo(user_data, action: str, page_id: str, name: str,
                  properties: dict | None = None) -> None:
    """Record how to reverse a destructive write.

    `properties` must be SNAPSHOTTED from the row before the write, even though
    this is called after it succeeds. Once the PATCH lands the old Amount is
    gone from Notion, so a snapshot re-read afterwards would faithfully record
    the new value as if it were the old one — an undo that changes nothing and
    reports success. `previous_properties` is built from the page object the
    lookup already returned, which is why that ordering is possible at all.
    """
    pending_choice.remember_undo(
        user_data, KIND,
        Undo(action=action, page_id=page_id, name=name, properties=properties))


def take_undo(user_data):
    """The last reversible EXPENSE action, consumed. Returns (undo, error).

    Consumed rather than kept so `undo` twice cannot re-run the same reversal.
    Un-archiving an already-live page is harmless, but re-applying a snapshot
    after you have deliberately re-edited the row would quietly undo that edit
    too.
    """
    return pending_choice.take_undo(user_data, KIND)


# ─── MESSAGES ──────────────────────────────────────────────────────────────────
# Everything below is interpolated into a Markdown message, so every value that
# came from Notion or from the user goes through escape_md. An expense named
# `Coffee *2` is ordinary; an unescaped one makes Telegram reject the whole
# prompt and the command looks like it was ignored. See telegram_text.py.

def format_choices(pending: Pending) -> str:
    """The numbered list, with the fields that tell two same-named rows apart."""
    verb = "delete" if pending.action == DELETE else "update"
    minutes = PENDING_TTL_SECONDS // 60

    lines = [
        f"⚠️ *{len(pending.choices)}* expenses this month match "
        f"'{escape_md(pending.query)}'.",
        f"Which one should I {verb}?",
        "",
    ]
    for number, choice in enumerate(pending.choices, start=1):
        details = [f"€{choice.amount:.2f}" if choice.amount is not None else "no amount"]
        if choice.date:
            details.append(choice.date)
        if choice.category:
            details.append(escape_md(choice.category))
        lines.append(f"*{number}.* {escape_md(choice.name)} — {' · '.join(details)}")

    lines += ["", f"Reply with a number. Nothing is {verb}d until you do, "
                  f"and this list lapses in {minutes} minutes."]
    return "\n".join(lines)


def format_undo_offer(action: str, name: str) -> str:
    """The one-line reminder appended to a successful destructive write."""
    return f"Send `undo` to put {escape_md(name)} back." if action == DELETE else \
           f"Send `undo` to restore {escape_md(name)}'s previous amount."


def format_undone(undo: Undo) -> str:
    if undo.action == DELETE:
        return f"↩️ Restored *{escape_md(undo.name)}*."
    return f"↩️ Put *{escape_md(undo.name)}* back to its previous amount and category."
