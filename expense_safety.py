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

WHERE THE STATE LIVES
---------------------
`context.user_data`, which python-telegram-bot keeps per user in memory. That is
the right lifetime for both: a pending choice is meaningless after a restart
(you would have to be re-shown the list anyway, since the rows may have moved),
and an undo you can no longer perform is better absent than stale. Nothing here
is written to disk, so Rule 1 — Notion is the single source of truth — is not
touched.

THE EXPIRY IS THE POINT, NOT A DETAIL. A pending choice with no deadline turns a
`2` typed an hour later — meant as an amount, a message to someone else, a
mis-tap — into an archive of a row you have long forgotten was offered. Two
minutes is long enough to read four lines and reply, and short enough that the
prompt is still on screen when you do. After it lapses the number goes back to
being an unrecognised message.

NO ConversationHandler. It would mean registering a second handler group and
teaching it the owner filter, for a single one-shot question whose whole state
is "which list did I just print". A dict with a deadline is the smaller thing
that fails in more obvious ways.

This module holds NO Notion calls and no Telegram sends: it decides and it
formats. `david.py` owns the queries, the writes and the routing.
"""

import time
from typing import NamedTuple

from notion_client import get_page_title
from telegram_text import escape_md

# How long a printed list of matches stays answerable. See the module docstring —
# this is a safety bound, not a tuning knob.
PENDING_TTL_SECONDS = 120

# The two destructive actions. `Add e` is not one: it creates a row and targets
# nothing, so it can be neither ambiguous nor wrong about which row it hit.
DELETE = "delete"
UPDATE = "update"

# Keys in context.user_data. Prefixed so they cannot collide with anything PTB
# or a future feature keeps there.
PENDING_KEY = "expense_pending_choice"
UNDO_KEY    = "expense_last_destructive"


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
        return (time.monotonic() if now is None else now) >= self.expires_at


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

def remember_pending(context, action: str, query: str, pages: list,
                     amount: float | None = None, category: str | None = None,
                     now: float | None = None) -> Pending:
    """Park a destructive command until it is told which row it meant.

    A second ambiguous command REPLACES the first rather than queueing behind
    it. David has one user and prints one list at a time, so the only list a
    number can sensibly refer to is the last one printed — keeping an older one
    answerable would make `1` mean whichever prompt you had scrolled to.
    """
    now = time.monotonic() if now is None else now
    pending = Pending(
        action=action,
        query=query,
        amount=amount,
        category=category,
        choices=tuple(choice_from_page(page) for page in pages),
        expires_at=now + PENDING_TTL_SECONDS,
    )
    context.user_data[PENDING_KEY] = (pending, pages)
    return pending


def has_pending(context, now: float | None = None) -> bool:
    """True when a live list of matches is waiting for a number.

    Used by the router to decide whether a bare number is a selection or just an
    unrecognised message. An EXPIRED pending answers False and is dropped here,
    so a lapsed prompt cannot be answered by a later one being printed.
    """
    entry = context.user_data.get(PENDING_KEY)
    if entry is None:
        return False
    pending, _ = entry
    if pending.expired(now):
        context.user_data.pop(PENDING_KEY, None)
        return False
    return True


def take_pending(context, selection: int, now: float | None = None):
    """Resolve a reply to the printed list. Returns (pending, page, error).

    Consumes the pending choice on every outcome except an out-of-range number:
    a mistyped `5` against three options should let you type `2` next, not force
    the whole command to be retyped.
    """
    entry = context.user_data.get(PENDING_KEY)
    if entry is None:
        return None, None, "There is nothing waiting to be chosen."

    pending, pages = entry
    if pending.expired(now):
        context.user_data.pop(PENDING_KEY, None)
        return None, None, (
            f"That list expired after {PENDING_TTL_SECONDS // 60} minutes. "
            "Send the command again if you still want it."
        )

    if not 1 <= selection <= len(pending.choices):
        return None, None, (
            f"Pick a number between 1 and {len(pending.choices)}."
        )

    context.user_data.pop(PENDING_KEY, None)
    return pending, pages[selection - 1], None


def clear_pending(context) -> None:
    context.user_data.pop(PENDING_KEY, None)


def parse_selection(text: str):
    """A bare number, or None if this message is not one.

    Deliberately strict: only digits, nothing else on the line. `2` is a
    selection, `2 please` and `€2` are not — a destructive write is the last
    place to start guessing what a message meant.
    """
    stripped = (text or "").strip()
    return int(stripped) if stripped.isdigit() else None


# ─── THE UNDO RECORD ───────────────────────────────────────────────────────────

def remember_undo(context, action: str, page_id: str, name: str,
                  properties: dict | None = None) -> None:
    """Record how to reverse a destructive write.

    `properties` must be SNAPSHOTTED from the row before the write, even though
    this is called after it succeeds. Once the PATCH lands the old Amount is
    gone from Notion, so a snapshot re-read afterwards would faithfully record
    the new value as if it were the old one — an undo that changes nothing and
    reports success. `previous_properties` is built from the page object the
    lookup already returned, which is why that ordering is possible at all.
    """
    context.user_data[UNDO_KEY] = Undo(action=action, page_id=page_id,
                                       name=name, properties=properties)


def take_undo(context):
    """The last reversible action, consumed. Returns (undo, error).

    Consumed rather than kept so `undo` twice cannot re-run the same reversal.
    Un-archiving an already-live page is harmless, but re-applying a snapshot
    after you have deliberately re-edited the row would quietly undo that edit
    too.
    """
    undo = context.user_data.pop(UNDO_KEY, None)
    if undo is None:
        return None, "Nothing to undo — I have not deleted or updated an expense yet."
    return undo, None


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
