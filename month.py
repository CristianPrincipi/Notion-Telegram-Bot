"""
The month page every expense relates to — found, named and rolled over by David.

WHY THIS EXISTS
---------------
Every expense row relates to a month page through the Expenses `Account` column,
and that page's ID was `MONTH_ID` in Railway. On the 1st of each month you had to
create the new page in Notion, copy its ID, paste it into Railway and wait for a
redeploy. Forget, and nothing breaks loudly: expenses keep being written — into
LAST month's page — and `B` keeps answering with last month's total. The
diagnostic in notion_ids.py even ended with "this is a manual monthly step for
now (I can automate it if you want)".

This module is that automation. It owns one question — *which page do this
month's expenses belong to?* — and answers it from Notion rather than from an
environment variable.

THE RESOLUTION RULE
-------------------
A month page is identified by its TITLE, in one canonical format: "August 2026".
On every run, against the database the month pages live in:

  1. a page whose title matches "August 2026" (ignoring case and extra spaces)
     is THE page. If its raw title differs from the canonical form ("august
     2026", "August 2026 "), it is renamed to it.
  2. otherwise a single page titled bare "August" is treated as the same month
     written the old way, and renamed to "August 2026".
  3. otherwise the page does not exist and is created.

That makes the whole operation idempotent: the second run finds the page the
first one created or renamed, changes nothing, and reports "unchanged". Running
it twice cannot produce two pages for one month.

Ambiguity is never resolved by guessing. Two pages titled "August" is a mistake
only you can settle, so it is reported as an error rather than picked from — the
one thing worse than a stale MONTH_ID is expenses split across two August pages.

WHERE THE ANSWER LIVES
----------------------
In memory, and in a small JSON file so a restart does not have to ask Notion
again. Neither is authoritative: Notion is. The file is a cache that can be
deleted at any time, which matters on Railway where the filesystem does not
survive a deploy.

`MONTH_ID` is still read — as the SEED. At first boot, with no cache file, it is
taken to be the current month's page (which is what it was when you set it), so
David starts working immediately, exactly as before. From the first rollover on,
this module's answer supersedes it, and the environment variable can be left to
go stale without consequence.

BLOCKING, LIKE EVERY OTHER NOTION CALL
--------------------------------------
Everything here is synchronous and must be reached through `asyncio.to_thread`
(see the note at the top of david.py). The serialisation primitive is therefore
`threading.Lock`, NOT page_lock.py: page_lock is an asyncio lock, which would
provide no mutual exclusion at all between two worker threads.
"""

import asyncio
import json
import logging
import os
import threading
from typing import NamedTuple

from calendar_client import now_local
from config import EXPENSE_MONTH_RELATION
from notion_client import (
    create_page, get_database, get_page_title, query_database, rich, update_page,
)

logger = logging.getLogger(__name__)

EXPENSES_ID = os.environ.get("EXPENSES_ID")

# The database month pages live in. Left unset it is discovered from the Expenses
# schema — the database the `Account` relation points at — so there is one less
# ID to keep correct in Railway. Set it only to override that discovery.
MONTHS_DB_ID = os.environ.get("MONTHS_DB_ID")

# The seed described in the module docstring, not a live value. Read once.
BOOTSTRAP_MONTH_ID = os.environ.get("MONTH_ID")

# Cache file. Best-effort: every read and write failure degrades to "ask Notion".
STATE_FILE = os.environ.get("MONTH_STATE_FILE", ".month_state.json")

# NOT strftime("%B"). That is locale-dependent, so the page title would silently
# become "agosto 2026" on a host with an Italian locale and stop matching the
# page created on an English one. The titles are data in Notion; they must not
# depend on the machine David happens to run on.
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# What a run did. Only the first three are worth telling you about.
CREATED   = "created"     # no page for this month existed
RENAMED   = "renamed"     # a page existed under another spelling; retitled
ADOPTED   = "adopted"     # the page existed and correct; David now points at it
UNCHANGED = "unchanged"   # already pointing at it — nothing was written


class Rollover(NamedTuple):
    """The outcome of one `ensure_current_month_page()` run."""

    page_id: str | None
    title: str
    action: str
    error: str | None = None

    @property
    def changed(self) -> bool:
        """True when this run actually moved something (so it is worth a ping)."""
        return self.error is None and self.action != UNCHANGED


# ─── NAMING ────────────────────────────────────────────────────────────────────

def canonical_title(when=None) -> str:
    """The one title format month pages use: 'August 2026'."""
    when = when or now_local()
    return f"{MONTH_NAMES[when.month - 1]} {when.year}"


def period_key(when=None) -> str:
    """'2026-08' — sortable, so a period comparison is a plain string compare."""
    when = when or now_local()
    return f"{when.year:04d}-{when.month:02d}"


def _normalise(title: str) -> str:
    """Fold the differences a title can pick up from being typed by hand."""
    return " ".join((title or "").split()).casefold()


# ─── STATE ─────────────────────────────────────────────────────────────────────
# _lock guards _state AND the whole find-or-create cycle, so two worker threads
# meeting at a month boundary cannot both decide to create the page. It is an
# RLock because current_month_id() calls ensure_current_month_page() while
# holding it.

_lock = threading.RLock()

# Filled once the month database is known. Its schema does not change between
# runs, so it is resolved on first use and then reused — a rollover costs one
# query, not three.
_schema: dict = {}


def _read_state_file() -> dict | None:
    """The cached answer from a previous run, or None if there isn't a usable one."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable month state file %s: %s", STATE_FILE, e)
        return None

    if not isinstance(data, dict) or not data.get("period") or not data.get("page_id"):
        logger.warning("Ignoring malformed month state file %s.", STATE_FILE)
        return None

    return {
        "period":  str(data["period"]),
        "page_id": str(data["page_id"]),
        "title":   str(data.get("title") or ""),
    }


def _initial_state() -> dict:
    """Where David starts from, in order of trustworthiness: cache, then seed.

    With neither, the period is left None — which reads as "older than any real
    month", so the first caller resolves it from Notion.
    """
    cached = _read_state_file()
    if cached:
        return cached
    if BOOTSTRAP_MONTH_ID:
        return {"period": period_key(), "page_id": BOOTSTRAP_MONTH_ID, "title": canonical_title()}
    return {"period": None, "page_id": None, "title": ""}


_state = _initial_state()


def _remember(period: str, page_id: str, title: str) -> None:
    """Adopt a resolved page as the current month's, and cache it for next boot.

    Ignores a result for any month that is not the current one: `when` exists so
    a past month can be repaired by hand, and doing that must not repoint live
    expense writes at a page from last year.
    """
    if period != period_key():
        logger.info("Resolved %s (%s) — not the current month, so MONTH_ID is unchanged.",
                    title, page_id)
        return

    _state.update(period=period, page_id=page_id, title=title)

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(_state, fh)
    except OSError as e:
        # A read-only or full filesystem costs the restart shortcut, nothing more:
        # the next run re-resolves from Notion and gets the same answer.
        logger.warning("Could not cache the month page to %s: %s", STATE_FILE, e)


# ─── THE MONTH DATABASE ────────────────────────────────────────────────────────

def _months_db_from_expenses_schema():
    """Follow the Expenses relation to the database month pages live in.

    Returns (database_id, error). This is why MONTHS_DB_ID is optional: the
    Expenses schema already names the target, and it cannot drift from what the
    expense writes actually use.
    """
    db, err = get_database(EXPENSES_ID)
    if err:
        return None, f"Could not read the Expenses database: {err}"

    prop = (db.get("properties") or {}).get(EXPENSE_MONTH_RELATION) or {}
    if prop.get("type") != "relation":
        return None, (
            f"The Expenses database has no '{EXPENSE_MONTH_RELATION}' relation column, so I "
            "cannot tell which database the month pages live in. Restore the column, or set "
            "MONTHS_DB_ID to that database."
        )

    db_id = (prop.get("relation") or {}).get("database_id")
    if not db_id:
        return None, (
            f"The Expenses '{EXPENSE_MONTH_RELATION}' relation does not name a target database. "
            "Set MONTHS_DB_ID."
        )
    return db_id, None


def _months_database():
    """(database_id, title_property_name, error), resolved once and cached.

    The title property is read from the schema rather than assumed to be called
    "Name": it is whatever the database calls it, and a wrong guess here would
    create pages with an empty title.
    """
    if _schema:
        return _schema["db_id"], _schema["title_prop"], None

    db_id = MONTHS_DB_ID
    if not db_id:
        db_id, err = _months_db_from_expenses_schema()
        if err:
            return None, None, err

    db, err = get_database(db_id)
    if err:
        return None, None, (
            f"Could not read the month database {db_id}: {err} "
            "(is it shared with your Notion integration?)"
        )

    title_prop = next(
        (name for name, prop in (db.get("properties") or {}).items()
         if prop.get("type") == "title"),
        None,
    )
    if not title_prop:
        return None, None, f"The month database {db_id} has no title column."

    _schema.update(db_id=db_id, title_prop=title_prop)
    return db_id, title_prop, None


# ─── FIND ──────────────────────────────────────────────────────────────────────

def _pick_month_page(pages: list, when):
    """The existing page for `when`, if there is one. Returns (page, error).

    (None, None) means "no page for this month" — the caller creates it.
    """
    canonical = _normalise(canonical_title(when))
    bare      = _normalise(MONTH_NAMES[when.month - 1])

    matches = [p for p in pages if _normalise(get_page_title(p)) == canonical]
    if matches:
        if len(matches) > 1:
            # Not fatal: they are already correctly titled, so expenses can only
            # have been related to one of them. Take the oldest — that is the one
            # the existing rows point at — and say so.
            logger.warning("%d pages are titled '%s'; using the oldest. Merge the duplicates.",
                           len(matches), canonical_title(when))
        return _oldest(matches), None

    # The old naming, from before this module existed: a bare month name. Only
    # renamed when there is exactly one, because "August" carries no year and two
    # of them could be two different years.
    legacy = [p for p in pages if _normalise(get_page_title(p)) == bare]
    if len(legacy) == 1:
        return legacy[0], None
    if len(legacy) > 1:
        return None, (
            f"{len(legacy)} pages are titled '{MONTH_NAMES[when.month - 1]}' with no year, so I "
            f"cannot tell which one is {canonical_title(when)}. Rename the right one and re-run."
        )

    return None, None


def _oldest(pages: list) -> dict:
    """The earliest-created of several pages — the one expenses already relate to."""
    return sorted(pages, key=lambda p: p.get("created_time") or "")[0]


# ─── THE ROLLOVER ──────────────────────────────────────────────────────────────

def ensure_current_month_page(when=None) -> Rollover:
    """Find, rename or create the month page, and point David's expenses at it.

    Idempotent — see the module docstring. Safe to call from the scheduled job,
    from the `Month` command, and from an expense write in the same minute.

    `when` overrides "now"; it exists so a missed month can be repaired by hand
    and so the tests do not have to wait for a month boundary. Resolving a month
    that is not the current one reports what it found without repointing
    MONTH_ID.

    Never raises: every failure comes back as `.error`, with the current pointer
    left untouched.
    """
    when   = when or now_local()
    title  = canonical_title(when)
    period = period_key(when)

    with _lock:
        def failed(error: str) -> Rollover:
            logger.error("Month rollover to %s failed: %s", title, error)
            return Rollover(_state["page_id"], _state["title"], UNCHANGED, error)

        db_id, title_prop, err = _months_database()
        if err:
            return failed(err)

        pages, err = query_database(db_id)
        if err:
            return failed(f"Could not list the month pages: {err}")

        page, err = _pick_month_page(pages, when)
        if err:
            return failed(err)

        # ── Create ─────────────────────────────────────────────────────────────
        if page is None:
            page_id, err = create_page(db_id, {title_prop: {"title": rich(title)}})
            if err:
                return failed(f"Could not create the '{title}' page: {err}")
            logger.info("Created the month page '%s' (%s).", title, page_id)
            _remember(period, page_id, title)
            return Rollover(page_id, title, CREATED)

        page_id  = page["id"]
        existing = get_page_title(page)

        # ── Rename ─────────────────────────────────────────────────────────────
        # Reached both for the old bare-month naming and for a page that matches
        # apart from case or stray spaces.
        if existing != title:
            _, err = update_page(page_id, {title_prop: {"title": rich(title)}})
            if err:
                return failed(f"Could not rename '{existing}' to '{title}': {err}")
            logger.info("Renamed the month page '%s' to '%s' (%s).", existing, title, page_id)
            _remember(period, page_id, title)
            return Rollover(page_id, title, RENAMED)

        # ── Already correct ────────────────────────────────────────────────────
        if _state["period"] == period and _state["page_id"] == page_id:
            logger.info("Month page '%s' (%s) is already current.", title, page_id)
            return Rollover(page_id, title, UNCHANGED)

        logger.info("Month page '%s' (%s) adopted.", title, page_id)
        _remember(period, page_id, title)
        return Rollover(page_id, title, ADOPTED)


def current_month_id() -> str | None:
    """The page ID this month's expenses relate to. THE replacement for MONTH_ID.

    A plain memory read in the normal case. It only talks to Notion when the
    cached answer belongs to an EARLIER month than today — which happens when the
    scheduled rollover could not run, typically because David was redeployed or
    down over the 1st. That check is what keeps a missed rollover from quietly
    filing a week of expenses into last month.

    Deliberately one-directional: a cached period that is NEWER than the clock is
    left alone. Months only roll forward, so a backwards jump is a clock problem,
    and re-resolving on it would write the wrong month into the cache.

    If that on-demand resolve fails, the last known page ID is returned rather
    than None. A Notion outage fails the expense write immediately afterwards
    anyway, so refusing here would only replace one error with an emptier one —
    but the failure IS logged, and the scheduled job reports it to Telegram.
    """
    with _lock:
        now = period_key()
        if _state["period"] is not None and _state["period"] >= now:
            return _state["page_id"]

        logger.warning("Month page is stale (cached %s, now %s) — resolving from Notion.",
                       _state["period"], now)
        result = ensure_current_month_page()
        if result.error:
            logger.error("Falling back to the last known month page %s.", _state["page_id"])
            return _state["page_id"]
        return result.page_id


# ─── REPORTING ─────────────────────────────────────────────────────────────────

def format_rollover(result: Rollover) -> str:
    """The Telegram message for a run — used by the job and the `Month` command.

    Markdown is safe here: the only interpolated values are a month name, a year,
    a Notion UUID and David's own wording. The page ID is in backticks so it is
    one tap to copy into Railway.
    """
    if result.error:
        return (f"⚠️ Could not update the monthly expenses page ({canonical_title()}):\n"
                f"{result.error}")

    detail = {
        CREATED:   "Created it — new expenses relate to it from now on.",
        RENAMED:   "Renamed an existing page to the standard format.",
        ADOPTED:   "The page already existed; expenses now relate to it.",
        UNCHANGED: "Already up to date — nothing to change.",
    }[result.action]

    headline = ("✅ Monthly expenses page updated"
                if result.changed else "🗓️ Monthly expenses page")

    return f"{headline}: *{result.title}*\n`{result.page_id}`\n{detail}"


# ─── TELEGRAM HANDLER ──────────────────────────────────────────────────────────

async def handle_month(update):
    """`Month` — run the rollover now and report, whether or not it changed anything.

    The manual counterpart to the scheduled job: the same idempotent call, so
    sending it twice is harmless. Useful right after fixing a Notion permission,
    and it prints the current page ID.
    """
    await update.message.reply_text("🗓️ Checking the monthly expenses page…")
    result = await asyncio.to_thread(ensure_current_month_page)
    await update.message.reply_text(format_rollover(result), parse_mode="Markdown")
