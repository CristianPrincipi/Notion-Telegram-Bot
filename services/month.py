"""
The month page every expense relates to — found, named and rolled over by David.

WHY THIS EXISTS
---------------
Every expense row relates to a month page through the Expenses `Account` column,
and that page's ID was `MONTH_ID` in Railway. On the 1st of each month you had to
create the new page in Notion, copy its ID, paste it into Railway and wait for a
redeploy. Forget, and nothing breaks loudly: expenses keep being written — into
LAST month's page — and `B` keeps answering with last month's total. The
diagnostic in services/notion_ids.py even ended with "this is a manual monthly
step for now (I can automate it if you want)".

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
In memory, for the life of the process, and nowhere else. Notion is
authoritative; this is a cache, and a fresh process simply asks again.

THERE USED TO BE A JSON FILE TOO, and dropping it is why the seed below reads
the way it does. `.month_state.json` existed to save a restart one resolve — two
API calls, once per container — which is not worth a persistence story on a
platform whose filesystem dies with every deploy. But it had quietly acquired a
second job: while the file existed it held a CORRECTLY RESOLVED page, so the
seed path below was never reached. The file was load-bearing by accident, for a
hazard that should not have existed.

`MONTH_ID` is read as a LAST-RESORT FALLBACK, not as an answer. It used to be
adopted at boot as "the current month's page" — labelled with today's period, so
`current_month_id()` saw a fresh-looking cache and handed it straight back
without ever asking Notion. When the variable had gone stale (which is its whole
documented lifecycle: it is a value you paste in once) every expense written
between a deploy and that night's 00:05 rollover was filed against LAST month's
page, and the budget answered for the wrong month. Both look completely normal.

So a fresh process now starts with `period = None`, which reads as "older than
any real month" and forces the first caller to resolve from Notion. The seed is
kept only for where it is genuinely useful: if that resolve FAILS, David falls
back to it rather than to nothing. A stale page beats no page during a Notion
outage — but only after Notion has been asked and could not answer.

BLOCKING, LIKE EVERY OTHER NOTION CALL
--------------------------------------
Everything here is synchronous and must be reached through `asyncio.to_thread`
(see the note at the top of david.py). The serialisation primitive is therefore
`threading.Lock`, NOT page_lock.py: page_lock is an asyncio lock, which would
provide no mutual exclusion at all between two worker threads.
"""

import asyncio
import logging
import os
import threading
from typing import NamedTuple

from clients.calendar_client import now_local
from config import EXPENSE_MONTH_RELATION
from clients.notion_client import (
    create_page, get_database, get_page_title, query_database, rich, update_page,
)
from telegram_text import escape_md

logger = logging.getLogger(__name__)

EXPENSES_ID = os.environ.get("EXPENSES_ID")

# The database month pages live in. Left unset it is discovered from the Expenses
# schema — the database the `Account` relation points at — so there is one less
# ID to keep correct in Railway. Set it only to override that discovery.
MONTHS_DB_ID = os.environ.get("MONTHS_DB_ID")

# The outage fallback described in the module docstring, not a live value and not
# an answer. Read once.
BOOTSTRAP_MONTH_ID = os.environ.get("MONTH_ID")

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
    """The outcome of one `ensure_current_month_page()` run.

    `period` is the month this run resolved ("2026-08"); `from_period` is the
    month David was pointing at when the run started, or None if it did not know
    yet. The pair is what separates a real rollover from a re-resolution of the
    month David is already on — see `rolled_over`.
    """

    page_id: str | None
    title: str
    action: str
    error: str | None = None
    period: str | None = None
    from_period: str | None = None

    @property
    def changed(self) -> bool:
        """True when this run WROTE something — created, renamed, or repointed.

        The headline predicate, not the notification one. A fresh process
        re-resolves the current month from Notion and lands on ADOPTED, which is
        `changed` (the pointer did move, from nothing to the resolved page) but
        is not a rollover.
        """
        return self.error is None and self.action != UNCHANGED

    @property
    def rolled_over(self) -> bool:
        """True when this run is worth interrupting you for.

        THE PREDICATE THE NIGHTLY JOB NOTIFIES ON, and the reason `changed` is
        not. `changed` is true of any run that wrote to Notion, including the
        ADOPTED that every fresh process produces — correct behaviour, correctly
        logged, and not news. Notifying on it meant a "✅ Monthly expenses page
        updated" on days David had simply been restarted, and a claim that
        arrives that often is one you stop reading.

        THE ORDINARY CASE is `period != from_period`: the month moved, which is
        what the message claims. In a healthy deploy that is the 1st and only the
        1st; if David was down over the 1st it is the first day it came back,
        which is precisely the run you want to hear about — and is why this is a
        period comparison rather than a `now_local().day == 1` check. Same period
        in and out is silence, whatever was written.

        THE from_period=None CASE is a process that had not resolved a month yet
        — so it cannot claim the month moved, it only knows where it landed.
        Every process now starts that way, since the resolved page is cached in
        memory only. Treating None as "moved" would put a false
        "✅ Monthly expenses page updated" on every deploy, which is the exact
        bug the `changed` → `rolled_over` switch was made to kill.

        Such a run speaks for exactly one reason: it CREATED the page. A month
        page that did not exist and now does is news whoever asked for it, and it
        can only happen once per month — every later run finds the page and lands
        on ADOPTED or UNCHANGED. Landing on a page that already existed is just a
        boot, and boots are not news.
        """
        if self.error is not None:
            return False
        if self.from_period is None:
            return self.action == CREATED
        return self.period != self.from_period


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


def _initial_state() -> dict:
    """Where David starts from: knowing nothing, holding the seed in reserve.

    THE PERIOD IS ALWAYS None HERE, even when MONTH_ID is set, and that is the
    whole point. `period = None` reads as "older than any real month", so the
    first caller resolves from Notion instead of trusting what it was handed.

    Labelling the seed with the CURRENT period — which this used to do — told
    `current_month_id()` that an environment variable pasted in weeks ago was
    today's answer, and it returned it without a single API call. See the module
    docstring.

    The page ID is still carried, so `current_month_id()` has something to fall
    back to if that first resolve fails.
    """
    return {"period": None, "page_id": BOOTSTRAP_MONTH_ID, "title": ""}


_state = _initial_state()


def _remember(period: str, page_id: str, title: str) -> None:
    """Adopt a resolved page as the current month's, for the life of the process.

    Ignores a result for any month that is not the current one: `when` exists so
    a past month can be repaired by hand, and doing that must not repoint live
    expense writes at a page from last year.
    """
    if period != period_key():
        logger.info("Resolved %s (%s) — not the current month, so the pointer is unchanged.",
                    title, page_id)
        return

    _state.update(period=period, page_id=page_id, title=title)


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
        # Read BEFORE anything resolves or _remember() overwrites it: this is the
        # month David was on when the run started, and the only way a later reader
        # can tell "the month moved" from "the same month was re-resolved after a
        # restart". See Rollover.rolled_over.
        from_period = _state["period"]

        def outcome(page_id, action, error=None) -> Rollover:
            return Rollover(page_id, title, action, error, period, from_period)

        def failed(error: str) -> Rollover:
            logger.error("Month rollover to %s failed: %s", title, error)
            # from_period on both sides: a failed run moved nothing, so it must
            # not read as a rollover even if the clock says the month turned.
            return Rollover(_state["page_id"], _state["title"], UNCHANGED, error,
                            from_period, from_period)

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
            return outcome(page_id, CREATED)

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
            return outcome(page_id, RENAMED)

        # ── Already correct ────────────────────────────────────────────────────
        if _state["period"] == period and _state["page_id"] == page_id:
            logger.info("Month page '%s' (%s) is already current.", title, page_id)
            return outcome(page_id, UNCHANGED)

        logger.info("Month page '%s' (%s) adopted.", title, page_id)
        _remember(period, page_id, title)
        return outcome(page_id, ADOPTED)


def current_month_id() -> str | None:
    """The page ID this month's expenses relate to. THE replacement for MONTH_ID.

    A plain memory read in the normal case. It talks to Notion in exactly two
    situations, and both are the point of the function:

      • the FIRST call in a process, because the cache starts empty. That is one
        resolve per container — two API calls — and it is what makes a stale
        MONTH_ID harmless: David asks Notion which page August 2026 is rather
        than believing a value pasted into Railway weeks ago.
      • when the cached answer belongs to an EARLIER month than today, which
        happens when the scheduled rollover could not run — typically because
        David was redeployed or down over the 1st. That check is what keeps a
        missed rollover from quietly filing a week of expenses into last month.

    Deliberately one-directional: a cached period that is NEWER than the clock is
    left alone. Months only roll forward, so a backwards jump is a clock problem,
    and re-resolving on it would write the wrong month into the cache.

    If the resolve fails, the last known page ID is returned rather than None —
    on a first call that is the MONTH_ID seed, which is the only job that
    variable still has. A Notion outage fails the expense write immediately
    afterwards anyway, so refusing here would replace one error with an emptier
    one — but the failure IS logged, and the scheduled job reports it to Telegram.
    """
    with _lock:
        now = period_key()
        if _state["period"] is not None and _state["period"] >= now:
            return _state["page_id"]

        logger.info("Month page not resolved yet for %s (had %s) — asking Notion.",
                    now, _state["period"])
        result = ensure_current_month_page()
        if result.error:
            logger.error("Falling back to the last known month page %s.", _state["page_id"])
            return _state["page_id"]
        return result.page_id


# ─── REPORTING ─────────────────────────────────────────────────────────────────

def format_rollover(result: Rollover) -> str:
    """The Telegram message for a run — used by the job and the `Month` command.

    Sent with parse_mode="Markdown" by both callers.

    On the SUCCESS path the interpolated values are David's own: a month name, a
    year and a Notion UUID. The page ID is in backticks so it is one tap to copy
    into Railway.

    The ERROR path is a different matter, and this docstring used to claim it was
    safe when it was not: result.error is a raw Notion string like
    "Notion 400: body.properties.rich_text should be defined", whose underscores
    made the whole failure notice unsendable. It is escaped.
    """
    if result.error:
        return (f"⚠️ Could not update the monthly expenses page ({canonical_title()}):\n"
                f"{escape_md(result.error)}")

    detail = {
        CREATED:   "Created it — new expenses relate to it from now on.",
        RENAMED:   "Renamed an existing page to the standard format.",
        ADOPTED:   "The page already existed; expenses now relate to it.",
        UNCHANGED: "Already up to date — nothing to change.",
    }[result.action]

    headline = ("✅ Monthly expenses page updated"
                if result.changed else "🗓️ Monthly expenses page")

    return f"{headline}: *{result.title}*\n`{result.page_id}`\n{detail}"


# ─── THE COMMAND ───────────────────────────────────────────────────────────────

async def run_month(*, notify, notify_md=None) -> None:
    """`Month` — run the rollover now and report, whether or not it changed anything.

    The manual counterpart to the scheduled job: the same idempotent call, so
    sending it twice is harmless. Useful right after fixing a Notion permission,
    and it prints the current page ID.

    The report goes out on the Markdown channel — format_rollover puts the page
    ID in a `code span` so it is one tap to copy, and escapes the Notion error on
    the failure path. The progress line is plain because there is nothing in it
    to format.
    """
    notify_md = notify_md or notify

    await notify("🗓️ Checking the monthly expenses page…")
    result = await asyncio.to_thread(ensure_current_month_page)
    await notify_md(format_rollover(result))
