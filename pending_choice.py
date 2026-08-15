"""One pending choice, one undo record, shared by every destructive command.

WHY THIS EXISTS
---------------
`expense_safety.py` grew the machine first, and named it for expenses. `Cancel`
needs the same behaviour — more than one match writes nothing, the list is
answered with a bare number, it lapses after two minutes — over completely
different fields.

The obvious move was a sibling module with its own `PENDING_KEY`. It is the wrong
one, and `expense_safety.remember_pending`'s own docstring already says why: a
second ambiguous command REPLACES the first, because "David prints one list at a
time, so the only list a number can sensibly refer to is the last one printed".
That argument does not stop at the feature boundary. Two independent slots would
allow a live expense list AND a live event list at the same instant, and then `2`
means whichever prompt you had scrolled to — which is precisely the ambiguity the
whole disambiguation path exists to remove, reintroduced one level up.

So there is ONE slot, and it carries the KIND that owns it. The dispatch loop in
david.py asks whether anything is live and routes the number to that kind's
handler. The same is true of the undo record: `undo` reverses the last
destructive thing David did, not the last destructive *expense*.

WHAT LIVES HERE AND WHAT DOES NOT
---------------------------------
Here: the rules that must not differ between two destructive commands — the
two-minute expiry, what counts as a selection, what an out-of-range number does,
and the fact that there is one slot.

Not here: the fields that tell two matches apart, the wording of any message, and
the shape of a reversal. Those are per-feature and live in `expense_safety.py`
and `calendar_safety.py`, which are this module's two callers.

No Notion, no Google, no Telegram: this decides, it does not act.

WHERE THE STATE LIVES
---------------------
`user_data` — the plain dict python-telegram-bot keeps per user in memory, handed
in by the bot layer rather than reached through a `context`, so nothing here
needs PTB and a test needs nothing but a dict. It is also the right LIFETIME: a
pending choice is meaningless after a restart (the rows may have moved, so you
would have to be re-shown the list anyway) and an undo you can no longer perform
is better absent than stale. Nothing is written to disk, so Hard Rule 1 — Notion
is the single source of truth — is untouched.
"""

import time

# How long a printed list of matches stays answerable.
#
# THE EXPIRY IS THE POINT, NOT A DETAIL. A pending choice with no deadline turns a
# `2` typed an hour later — meant as an amount, a message to someone else, a
# mis-tap — into a deletion of something you have long forgotten was offered. Two
# minutes is long enough to read four lines and reply, and short enough that the
# prompt is still on screen when you do. After it lapses the number goes back to
# being an unrecognised message.
PENDING_TTL_SECONDS = 120

# Keys in user_data. Prefixed so they cannot collide with anything PTB or a
# future feature keeps there. Deliberately NOT named for a feature: that naming
# is what made the expense machine look un-shareable in the first place.
PENDING_KEY = "david_pending_choice"
UNDO_KEY    = "david_last_destructive"


def deadline(now: float | None = None) -> float:
    """When a list printed now stops being answerable."""
    return (time.monotonic() if now is None else now) + PENDING_TTL_SECONDS


def is_expired(expires_at: float, now: float | None = None) -> bool:
    return (time.monotonic() if now is None else now) >= expires_at


def parse_selection(text: str):
    """A bare number, or None if this message is not one.

    Deliberately strict: only digits, nothing else on the line. `2` is a
    selection, `2 please` and `€2` are not — a destructive write is the last
    place to start guessing what a message meant.
    """
    stripped = (text or "").strip()
    return int(stripped) if stripped.isdigit() else None


# ─── THE PENDING SLOT ──────────────────────────────────────────────────────────

def remember(user_data, kind: str, pending, items) -> None:
    """Park a destructive command until it is told which match it meant.

    `pending` is the feature's own record (it must carry `choices` and
    `expires_at`); `items` are the full objects those choices were reduced from,
    positionally parallel to them, so the write can act on the real thing rather
    than on the summary that was printed.
    """
    user_data[PENDING_KEY] = (kind, pending, items)


def has_pending(user_data, now: float | None = None) -> bool:
    """True when a live list of matches is waiting for a number.

    Used by the router to decide whether a bare number is a selection or just an
    unrecognised message. An EXPIRED pending answers False and is dropped here,
    so a lapsed prompt cannot be answered by a later one being printed.
    """
    entry = user_data.get(PENDING_KEY)
    if entry is None:
        return False
    _, pending, _ = entry
    if is_expired(pending.expires_at, now):
        user_data.pop(PENDING_KEY, None)
        return False
    return True


def kind_of(user_data):
    """Which feature owns the live list, or None. Peeks — does not consume.

    This is what makes one slot workable: the dispatch loop reads the kind to
    pick a handler, and the handler consumes the entry through `take`.
    """
    entry = user_data.get(PENDING_KEY)
    return None if entry is None else entry[0]


def take(user_data, kind: str, selection: int, now: float | None = None):
    """Resolve a reply to the printed list. Returns (pending, item, error).

    Consumes the pending choice on every outcome EXCEPT an out-of-range number:
    a mistyped `5` against three options should let you type `2` next, not force
    the whole command to be retyped.

    `kind` is checked rather than trusted. The dispatch loop routes by kind, so a
    mismatch here means two features disagree about who owns the live list — and
    the failure that would otherwise produce is a number answering the wrong
    command's prompt, which is exactly what one slot exists to prevent.
    """
    entry = user_data.get(PENDING_KEY)
    if entry is None:
        return None, None, "There is nothing waiting to be chosen."

    live_kind, pending, items = entry
    if live_kind != kind:
        return None, None, "That list is not the one waiting to be answered."

    if is_expired(pending.expires_at, now):
        user_data.pop(PENDING_KEY, None)
        return None, None, (
            f"That list expired after {PENDING_TTL_SECONDS // 60} minutes. "
            "Send the command again if you still want it."
        )

    if not 1 <= selection <= len(pending.choices):
        return None, None, f"Pick a number between 1 and {len(pending.choices)}."

    user_data.pop(PENDING_KEY, None)
    return pending, items[selection - 1], None


def clear(user_data) -> None:
    user_data.pop(PENDING_KEY, None)


# ─── THE UNDO SLOT ─────────────────────────────────────────────────────────────

def remember_undo(user_data, kind: str, record) -> None:
    """Record how to reverse a destructive write.

    Whatever the record holds must be SNAPSHOTTED from the object the LOOKUP
    returned, even though this is called after the write succeeds. Once the write
    lands, re-reading the target records the new state as if it were the old one
    — an undo that changes nothing and reports success. Both callers build their
    record before acting, which is what makes storing it afterwards safe.
    """
    user_data[UNDO_KEY] = (kind, record)


def undo_kind(user_data):
    """Which feature owns the last reversal, or None. Peeks — does not consume.

    `undo` is one command over two services, so the bot layer reads this to pick
    which one to call and that service consumes the record itself. Peeking rather
    than consuming here is what keeps the "put it back on failure" logic inside
    the service that knows when a reversal did not happen.
    """
    entry = user_data.get(UNDO_KEY)
    return None if entry is None else entry[0]


def take_undo(user_data, kind: str):
    """The last reversible action, consumed. Returns (record, error).

    Consumed rather than kept so `undo` twice cannot re-run the same reversal.
    Repeating a reversal is not harmless: re-applying a snapshot after you have
    deliberately re-edited the target would quietly undo that edit too, and
    re-inserting a restored calendar event would leave you with two of it.
    """
    entry = user_data.pop(UNDO_KEY, None)
    if entry is None:
        return None, "Nothing to undo — I have not deleted or changed anything yet."

    stored_kind, record = entry
    if stored_kind != kind:
        # Put it back: the caller asked the wrong service, and dropping the
        # record here would make a routing bug look like "nothing to undo".
        user_data[UNDO_KEY] = entry
        return None, "The last thing I did is not undoable by that command."
    return record, None
