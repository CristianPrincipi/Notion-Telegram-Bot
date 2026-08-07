import os
import io
import logging
import requests
import re
import asyncio
import pytz
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from learn import SUPPORTED_TYPES, handle_learn
from implement import handle_implement
from reminder import handle_remind
from calendar_client import now_local
from notion_ids import handle_diag, handle_find, handle_dbs
import PyPDF2

import config
import expense_safety
from budget import budget
from config import (
    GENRE_MAP, CATEGORY_MAP, DEFAULT_CATEGORY, EXPENSE_MONTH_RELATION,
    PROACTIVE_TIMEZONE, SUNDAY, category_help, genre_help,
)
from month import current_month_id, handle_month
from notion_client import (
    CREATED_DESC, body_excerpt, notion_request, query_database, set_archived, update_page,
)
from pkm import handle_get
from observability import record_command, record_error, set_correlation_id, setup_logging
from page_lock import WRITE_LOCK_TIMEOUT_SECONDS, PageBusy, page_lock
from proactive.scheduler import register_all
from telegram_text import escape_md, reply, send

# Configured at import so config.validate() can still be the first statement in
# __main__ and have somewhere to send its warnings. Level comes from LOG_LEVEL;
# the format carries a per-update correlation ID. See observability.py.
setup_logging()
logger = logging.getLogger("david")


# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
DATABASE_ID = os.environ.get("DATABASE_ID")
EXPENSES_ID = os.environ.get("EXPENSES_ID")
LETTI_ID = os.environ.get("LETTI_ID")
LITERATURE_ID = os.environ.get("LITERATURE_ID")
CHAT_ID = os.environ.get("CHAT_ID")
LEARN_ID = os.environ.get("LEARN_ID")
DIET_ID = os.environ.get("DIET_ID")
BRAIN_ID = os.environ.get("BRAIN_ID")
FINANCE_ID = os.environ.get("FINANCE_ID")


# --- EXPENSE WRITE SERIALISATION ---
# `U e` and `D e` are both find-then-mutate: query the Expenses DB by name, take
# results[0], then PATCH that page. The query and the PATCH are two round trips,
# and between them another run can read the SAME results[0].
#
# The delete case is the one that bites. Notion excludes archived pages from
# query results, which is what makes `D e Carrefour` twice in a row correctly
# delete two different rows — the second query no longer sees the first one.
# Overlap them and both queries run before either archive, both resolve to the
# same page, and both archive it. You are told "deleted successfully" twice and
# one row is still there.
#
# Locked on EXPENSES_ID rather than on the expense name: page_lock keys must be
# database ids so the lock table stays bounded (see page_lock.py), and expense
# names come from the user. Serialising every expense write costs nothing here —
# they take about a second and David has one user.
#
# `Add e` is deliberately NOT locked. It is a bare create with no preceding read,
# so it cannot double-target a row or lose an update. Locking it would only
# serialise it against the other two, which does not make "add X" and "delete X"
# sent at the same instant any less ambiguous than they already are.
BUSY_EXPENSE_MESSAGE = ("⏳ Another expense write is still running. "
                        "Give it a second and try again.")


# --- PDF ATTACHMENT LIMITS ---
MAX_PDF_MB    = 15
MAX_PDF_BYTES = MAX_PDF_MB * 1024 * 1024
HTTP_TIMEOUT_SECONDS     = 30    # per-request cap: fail fast on a stalled socket
DOWNLOAD_TIMEOUT_SECONDS = 120   # whole-operation cap


# --- NOTION FUNCTIONS --- #
#
# Everything in this section is SYNCHRONOUS and makes blocking HTTP calls. None
# of it may be called directly from an `async def` — python-telegram-bot runs
# updates on one event loop, so a blocking call here stops every other command
# and every scheduled job for its whole duration. Call them with
#   await asyncio.to_thread(fn, ...)
# as the handlers below do. They stay sync so they remain directly testable.

# --- BUDGET --- #
# Imported, not defined here. budget.py owns the aggregation and the recap text,
# and proactive/ already reads its compute_budget() for the morning pace tag and
# the pacing warning. david.py used to carry a second, independent copy: same
# maths, same output, but a second place to fix a pagination or rounding bug and
# a second place to forget. budget.py was written as its replacement and says so
# in its own docstring — the `B` command simply never switched over.
#
# Still reached as david.budget, so the call sites and the spies in the tests are
# unchanged.


# --- NEW READED BOOK --- #
def add_New_Book(name, author, genre):
    """Create a new book entry in Notion. Returns page_id on success, None on failure."""
    data = {
        "parent": {"database_id": LETTI_ID},
        "properties": {
            "Name":   {"title": [{"text": {"content": name}}]},
            "Author": {"rich_text": [{"text": {"content": author}}]},
            "Genre":  {"multi_select": [{"name": genre}]},
            "Area":   {"relation": [{"id": LITERATURE_ID}]},
        }
    }
    response = notion_request("POST", "https://api.notion.com/v1/pages", json=data)
    if response.status_code != 200:
        logger.error("add_New_Book failed: Notion %s: %s",
                     response.status_code, body_excerpt(response))
        return None
    return response.json()["id"]


# --- NEW QUOTE FUNCTION ---
def find_Book_Page(book_name):
    """Search LETTI database for a book by name. Returns page_id or None.

    Sorted newest-first, so with two editions of the same title in the library
    the quote lands on the same one every time instead of on whichever row
    Notion happened to return first. See notion_client.CREATED_DESC.
    """
    results, err = query_database(
        LETTI_ID,
        filter_obj={"property": "Name", "title": {"contains": book_name.strip()}},
        sorts=CREATED_DESC,
    )
    if err:
        logger.error("find_Book_Page(%r) failed: %s", book_name, err)
        return None
    return results[0]["id"] if results else None





def extract_quote_from_pdf(pdf_bytes: bytes, begin_text: str, end_text: str):
    """Extract text between begin_text and end_text from a PDF.

    Processes pages incrementally — stops as soon as both markers are found,
    so large books don't require reading every page.
    Returns (extracted_quote: str, error: str | None).
    Always run via asyncio.to_thread() — never call directly from the event loop.
    """

    def _norm(t):
        return re.sub(r"\s+", " ", t or "").strip()

    norm_begin = _norm(begin_text).lower()
    norm_end   = _norm(end_text).lower()

    if not norm_begin or not norm_end:
        return None, "Begin or End text cannot be empty."

    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return None, "PDF appears to be empty."

        accumulated     = ""
        begin_pos_found = -1

        for page in reader.pages:
            accumulated += " " + _norm(page.extract_text())
            acc_lower    = accumulated.lower()

            if begin_pos_found == -1:
                bp = acc_lower.find(norm_begin)
                if bp != -1:
                    begin_pos_found = bp

            if begin_pos_found != -1:
                search_from = begin_pos_found + len(norm_begin)
                ep = acc_lower.find(norm_end, search_from)
                if ep != -1:
                    raw = accumulated[begin_pos_found : ep + len(norm_end)]
                    return _norm(raw), None

        if begin_pos_found == -1:
            return None, f"Begin text not found in PDF.\nSearched for: \'{begin_text[:100]}\'"
        return None, f"End text not found after begin marker.\nSearched for: \'{end_text[:100]}\'"

    except PyPDF2.errors.PdfReadError as e:
        return None, f"Could not read PDF: {e}"
    except Exception as e:
        return None, f"PDF extraction error: {e}"


def chunk_text(text, size=1800):
    """Split long text into chunks compatible with Notion limits."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def add_Quote(page_id, quote_title, quote_text):
    """Add a quote section to a book page, automatically splitting long quotes."""

    children = [
        {
            "object": "block",
            "type": "heading_1",
            "heading_1": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": quote_title[:2000]
                        }
                    }
                ],
                "color": "green"
            }
        }
    ]

    for chunk in chunk_text(quote_text):
        children.append({
            "object": "block",
            "type": "quote",
            "quote": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": chunk
                        }
                    }
                ]
            }
        })

    url = f"https://api.notion.com/v1/blocks/{page_id}/children"

    for i in range(0, len(children), 100):
        batch = children[i:i + 100]

        response = notion_request(
                 "PATCH",
                 url,
                 json={"children": batch}
        )

        if response.status_code != 200:
            logger.error("add_Quote block append failed: Notion %s: %s",
                         response.status_code, body_excerpt(response))
            return False

    return True


# --- NEW EXPENSES FUNCTION ---
def add_Expenses(name, amount, category):

    # --- GENERATE TODAY DATE ---
    # Europe/Rome, not the host clock: Railway runs UTC, so a naive now() files
    # anything logged after local midnight under YESTERDAY — and at a month
    # boundary, into the wrong month's budget entirely.
    today = now_local().strftime("%Y-%m-%d")

    # The month page, resolved now rather than read from MONTH_ID at import. That
    # is what fixes the other half of the same bug: the date was already right at
    # a month boundary, but the relation still pointed at the previous month's
    # page until the environment variable was updated by hand. See month.py.
    month_id = current_month_id()
    if not month_id:
        logger.error("add_Expenses: no month page resolved — send `Month`, or check `Diag`.")
        return False

    data = {
        "parent": {"database_id": EXPENSES_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": name}}]},
            "Amount": {"number": amount},
            "Date": {"date": {"start": today}},
            "Category":{"multi_select": [{"name": category}]},
            EXPENSE_MONTH_RELATION: {"relation": [{"id": month_id}]}
        }
    }

    response = notion_request("POST", "https://api.notion.com/v1/pages", json=data)

    if response.status_code != 200:
        logger.error("add_Expenses failed: Notion %s: %s",
                     response.status_code, body_excerpt(response))

    return response.status_code == 200


# --- FINDING THE EXPENSE A DESTRUCTIVE COMMAND MEANS ---
#
# SPLIT OUT OF THE WRITES ON PURPOSE. `update_Expense` and `delete_Expense` used
# to do their own lookup and act on results[0], which made "which row did that
# hit?" unanswerable from outside them — there was no point between finding and
# mutating at which anything could be shown to you or counted. Both writes now
# take a page ID that something else chose, which is what lets the caller stop
# and ask when the choice is not obvious.
#
# TWO NARROWINGS, both closing a way to hit the wrong row:
#
#   sorts=CREATED_DESC   — "the first match" now means the most recent one, the
#                          same way on every call. See notion_client.
#   the month filter     — the search covers THIS month only, so `D e Coffee`
#                          cannot reach into last December for a coffee you have
#                          long since forgotten. It also matches how you think
#                          about expenses: the budget is monthly, so the row you
#                          mean is one of this month's.

def find_expense_matches(name):
    """Expenses in the CURRENT month whose Name contains `name`, newest first.

    Returns (pages, error) — full page objects, not IDs, because the caller
    needs their Amount, Date and Category both to tell two matches apart in the
    prompt and to snapshot the old values for `undo`. Those properties come back
    with the query, so carrying them costs no extra request.
    """
    month_id = current_month_id()
    if not month_id:
        # REFUSING BEATS WIDENING. Falling back to an unscoped search here would
        # silently restore the exact reach this filter exists to remove, and it
        # would do it precisely when David is least sure of its own state.
        return [], ("I could not work out which month page to search — "
                    "send `Month` to re-resolve it, or `Diag` to see why.")

    return query_database(
        EXPENSES_ID,
        filter_obj={"and": [
            {"property": "Name", "title": {"contains": name.strip()}},
            {"property": EXPENSE_MONTH_RELATION, "relation": {"contains": month_id}},
        ]},
        sorts=CREATED_DESC,
    )


# --- UPDATE EXPENSES FUNCTION ---
def update_Expense(page_id, amount, category):
    """Overwrite one expense's amount and category. Returns (ok, error)."""
    update_response = notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        json={"properties": {
            "Amount": {"number": amount},
            "Category": {"multi_select": [{"name": category}]},
        }},
    )

    if update_response.status_code != 200:
        logger.error("update_Expense(%s) failed: Notion %s: %s",
                     page_id, update_response.status_code, body_excerpt(update_response))
        return False, f"Notion {update_response.status_code}: {body_excerpt(update_response)}"

    return True, None


# --- DELETE EXPENSES FUNCTION ---
def delete_Expense(page_id):
    """Archive one expense. Returns (ok, error).

    Notion has no hard delete for an integration, which is what makes this
    reversible — `undo` sends the same page back with archived=False.
    """
    update_response = notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        json={"archived": True},
    )

    if update_response.status_code != 200:
        logger.error("delete_Expense(%s) failed: Notion %s: %s",
                     page_id, update_response.status_code, body_excerpt(update_response))
        return False, f"Notion {update_response.status_code}: {body_excerpt(update_response)}"

    return True, None


# --- DESTRUCTIVE EXPENSE COMMANDS (`U e`, `D e`, `undo`) --- #
#
# Both destructive commands run the same three steps — find, choose, write —
# and differ only in which write they end at, so they share the pair below
# rather than each carrying its own copy of the ambiguity and undo handling.
# The state machine and every message live in expense_safety.py; what stays here
# is the Notion I/O and the locking, which is what david.py owns.

async def _start_destructive_expense(update, context, action, name,
                                     amount=None, category=None):
    """Resolve which expense `name` means, then either write or ask.

    THE ONE RULE: more than one match means NOTHING is written. A destructive
    command whose target is ambiguous is not a command yet, and guessing at it
    is the failure this whole path exists to remove — the write is cheap to
    repeat and the wrong write is expensive to notice.

    THE LOOKUP IS INSIDE THE LOCK, and has to be. This is a find-then-mutate
    spanning two round trips, so a second expense write slipping between them
    would let both resolve to the same row and archive it twice — the exact race
    page_lock.py's docstring names as the reason its keys are database ids.
    Only the single-match path writes here; the ambiguous one releases the lock
    and waits for a number, because holding it across a reply from you would
    stall every other expense command for as long as you took to answer.
    """
    try:
        async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            matches, err = await asyncio.to_thread(find_expense_matches, name)

            if err is None and len(matches) == 1:
                await _apply_destructive_expense(
                    update, context, action, matches[0], amount, category)
                return
    except PageBusy:
        await update.message.reply_text(BUSY_EXPENSE_MESSAGE)
        return

    if err:
        # An error is NOT an empty result: "Notion is down" and "you have no
        # Coffee this month" need opposite reactions, and reporting the first as
        # the second is how a failed lookup turns into "it wasn't there anyway".
        await reply(update, f"❌ Could not look up '{escape_md(name)}':\n{escape_md(err)}")
        return

    if not matches:
        await update.message.reply_text(
            f"❌ Error: no expense matching '{name}' this month.")
        return

    pending = expense_safety.remember_pending(
        context, action, name, matches, amount=amount, category=category)
    await reply(update, expense_safety.format_choices(pending))


async def _apply_destructive_expense(update, context, action, page, amount, category):
    """Make the write, and record how to reverse it. CALL UNDER THE EXPENSE LOCK.

    The undo snapshot is taken from `page` — the row as the lookup found it —
    and is therefore the state BEFORE this write, even though it is stored
    after. Re-reading the page afterwards would faithfully record the new amount
    as the old one, which is worse than having no undo at all.
    """
    choice   = expense_safety.choice_from_page(page)
    previous = (expense_safety.previous_properties(page)
                if action == expense_safety.UPDATE else None)

    if action == expense_safety.DELETE:
        success, err = await asyncio.to_thread(delete_Expense, choice.page_id)
    else:
        success, err = await asyncio.to_thread(
            update_Expense, choice.page_id, amount, category)

    if not success:
        verb = "delete" if action == expense_safety.DELETE else "update"
        await reply(update, f"❌ Could not {verb} '{escape_md(choice.name)}':\n{escape_md(err)}")
        return

    # Only now. An undo record for a write that failed would offer to reverse
    # something that never happened.
    expense_safety.remember_undo(context, action, choice.page_id, choice.name, previous)

    headline = (f"🗑️ Deleted *{escape_md(choice.name)}*"
                if action == expense_safety.DELETE else
                f"✅ Updated *{escape_md(choice.name)}* to €{amount:.2f} [{escape_md(category)}]")
    await reply(update, f"{headline}\n{expense_safety.format_undo_offer(action, choice.name)}")


async def handle_expense_selection(update, context, selection: int):
    """A bare number answering the numbered list of matches.

    No lookup runs here: the page was chosen from a list David printed, so this
    is a write against a known ID rather than a find-then-mutate. The lock is
    still taken, to keep it ordered against the other expense writes.
    """
    pending, page, err = expense_safety.take_pending(context, selection)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    try:
        async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            await _apply_destructive_expense(update, context, pending.action, page,
                                             pending.amount, pending.category)
    except PageBusy:
        await update.message.reply_text(BUSY_EXPENSE_MESSAGE)


async def handle_undo(update, context):
    """`undo` — reverse the last destructive expense write.

    Both branches are ordinary writes against a page ID David already holds, so
    neither re-runs a lookup: an undo that had to find its own target could pick
    a different row than the one it is undoing, which would make the recovery
    command a third way to hit the wrong expense.
    """
    undo, err = expense_safety.take_undo(context)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    try:
        async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            if undo.action == expense_safety.DELETE:
                success, err = await asyncio.to_thread(set_archived, undo.page_id, False)
            else:
                success, err = await asyncio.to_thread(update_page, undo.page_id, undo.properties)
    except PageBusy:
        # Put it back: the reversal has not happened, so it must stay available.
        expense_safety.remember_undo(context, undo.action, undo.page_id,
                                     undo.name, undo.properties)
        await update.message.reply_text(BUSY_EXPENSE_MESSAGE)
        return

    if not success:
        expense_safety.remember_undo(context, undo.action, undo.page_id,
                                     undo.name, undo.properties)
        await reply(update, f"❌ Could not undo '{escape_md(undo.name)}':\n{escape_md(err)}")
        return

    await reply(update, expense_safety.format_undone(undo))


# --- DETACHED (BACKGROUND) COMMANDS --- #

def run_detached(context: ContextTypes.DEFAULT_TYPE, update: Update, coro, name: str):
    """Run a long command as a background task instead of awaiting it inline.

    WHY THIS EXISTS
    ---------------
    Moving blocking work onto worker threads freed the event LOOP, but
    python-telegram-bot still processes updates one at a time: it will not look
    at the next update until this handler returns. So a five-minute `Learn video`
    left every other command sitting in the queue behind it, even though the loop
    itself was idle the whole time.

    Detaching only the genuinely long commands fixes that without enabling
    concurrent_updates. Everything else — expenses, budget, quotes, books,
    reminders, diagnostics — stays strictly sequential, so a write followed by a
    read of the same data still cannot be reordered. `Add e` then `B` always
    reports the new total. That guarantee is the reason this is a per-command
    decision and not a global switch.

    WHAT THIS ALLOWS TO OVERLAP
    ---------------------------
    Two detached commands can now run at once, which is exactly what the locks
    are for: two Implements against the same area are refused by the area lock,
    and two against Diet by the Diet lock. Nothing detached here touches the
    expense or calendar paths.

    Uses Application.create_task rather than asyncio.create_task so exceptions
    reach the global error handler instead of vanishing into a dropped task, and
    so a task still in flight is awaited on shutdown rather than killed
    mid-write. Passing `update` is what gives that error handler its context.
    """
    return context.application.create_task(coro, update=update, name=name)


# --- ERROR REPORTING HELPERS --- #
#
# BOTH REPORTERS BELOW SEND PLAIN TEXT. Do not add parse_mode back.
#
# They interpolate an exception string, and Notion 400 bodies and Python
# tracebacks routinely carry unbalanced * _ ` and [. Under parse_mode="Markdown"
# that made the ERROR REPORT ITSELF raise BadRequest, the bare `except` swallowed
# it, and the error being reported was lost — the precise failure these functions
# exist to prevent.
#
# escape_md() is NOT the fix here, which is why these two are the only senders in
# the codebase exempt from telegram_text: the exception text sat inside a `code
# span`, and Markdown v1 does not honour backslash escapes inside code spans.
# Plain text is the only formatting that cannot fail.

async def notify_error(context: ContextTypes.DEFAULT_TYPE, where: str, err: Exception):
    """Send a Telegram message to the owner when something fails silently in the background."""
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ David error in {where}:\n{type(err).__name__}: {err}",
        )
    except Exception:
        # Last resort: the report itself could not be delivered. The log is the
        # only remaining channel, so this must never raise.
        logger.exception("notify_error could not report the failure in %s: %s", where, err)


async def on_error(update, context):
    """Global handler: any unhandled exception in a handler lands here.

    At module scope rather than nested inside __main__ so the suite can drive it —
    the same treatment register_jobs and register_handlers already get.
    """
    err = context.error
    record_error()
    logger.error("Unhandled %s: %s", type(err).__name__, err, exc_info=err)
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ David hit an error:\n{type(err).__name__}: {err}",
        )
    except Exception:
        logger.exception("on_error could not report the failure.")


# --- SCHEDULED JOB: SEND BUDGET RECAP --- #
async def send_budget_recap(context: ContextTypes.DEFAULT_TYPE):
    try:
        result_text = await asyncio.to_thread(budget)
        if result_text:
            await send(context.bot, CHAT_ID, result_text)
        else:
            await context.bot.send_message(chat_id=CHAT_ID, text="❌ Could not fetch budget from Notion.")
    except Exception as e:
        await notify_error(context, "send_budget_recap", e)


# --- SCHEDULED JOB REGISTRATION --- #

def register_jobs(application, chat_id) -> bool:
    """Attach every scheduled job. Call once, at startup. Returns True if scheduling is on.

    Two families:
      - the weekly budget recap, defined above
      - the proactive briefings, which live in proactive/ and are attached by
        proactive.scheduler.register_all

    The briefings REPLACE the old send_daily_reminders job rather than joining
    it: the morning briefing owns the 07:30 slot that job used, and the evening
    briefing owns tomorrow's events. Running both would send today's events
    twice at 07:30 and tomorrow's twice (07:30 and 20:00).

    Like register_handlers, this is a function rather than inline __main__ code
    so the wiring is importable and can be tested.
    """
    try:
        job_queue = application.job_queue
        if job_queue is None:
            logger.warning("JobQueue unavailable — scheduled jobs not registered "
                           "(install python-telegram-bot[job-queue]).")
            return False

        # Budget recap — the full per-category breakdown, Sunday 09:30.
        # Distinct from the proactive one-line pace tag and the pacing warning.
        #
        # SUNDAY is a named constant on purpose: a bare integer here is exactly
        # how this call site ran on the wrong days for months (see config.py).
        job_queue.run_daily(
            send_budget_recap,
            time=time(hour=9, minute=30, tzinfo=pytz.timezone(PROACTIVE_TIMEZONE)),
            days=(SUNDAY,),
            name="budget_recap",
        )

        # Morning briefing (07:30), evening briefing (20:00), budget pacing (13:00).
        register_all(application, chat_id)
        return True
    except Exception:
        logger.exception("Scheduled jobs not available — commands still work.")
        return False


# --- ACCESS CONTROL --- #
# David is single-user: it spends the owner's Notion and Anthropic quota and can
# write to their databases, so every command is owner-only. Authorization is a
# python-telegram-bot filter rather than a check inside handle_message, so an
# unauthorized update is dropped by the dispatcher and never reaches handler
# code — a new command cannot forget to check.

def build_owner_filter(owner_id: int):
    """Filter matching only the owner's messages."""
    return filters.User(user_id=owner_id)


def register_handlers(application, owner_id: int):
    """Attach every message handler, all gated on the owner.

    Lives here rather than inline in __main__ so the authorization wiring is
    importable and can be tested against real Update objects. Testing a filter
    rebuilt inside a test would only prove the test agrees with itself.
    """
    owner_only = build_owner_filter(owner_id)

    # LISTEN FOR ANY TEXT MESSAGE... (except commands)
    # ~EDITED_MESSAGE: python-telegram-bot matches edited_message by default, so
    # without it, correcting a typo in an expense re-runs the command and creates
    # a SECOND Notion entry.
    application.add_handler(MessageHandler(
        filters.TEXT & (~filters.COMMAND) & owner_only
        & (~filters.UpdateType.EDITED_MESSAGE),
        handle_message))

    # Same for uploads: editing a file's caption would re-download the PDF and
    # append the quote to Notion a second time.
    application.add_handler(MessageHandler(
        filters.Document.ALL & owner_only & (~filters.UpdateType.EDITED_MESSAGE),
        handle_document))

    # Catch-all for everyone else. Registered LAST: within a handler group PTB
    # stops at the first match, so the owner's messages are taken by the handlers
    # above and never reach this one — and it is scoped to ~owner_only anyway.
    application.add_handler(MessageHandler(~owner_only, handle_unauthorized))


async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log an unauthorized attempt and drop it.

    Deliberately silent — replying would confirm to whoever probed the bot that
    it is live and listening. Nothing here touches Notion or the Anthropic API.
    """
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    content = (getattr(message, "text", None) or getattr(message, "caption", None) or "")

    logger.warning(
        "Unauthorized attempt — user_id=%s username=%s chat_id=%s content=%r",
        getattr(user, "id", None),
        getattr(user, "username", None),
        getattr(chat, "id", None),
        content[:80],
    )


# --- COMMAND ARGUMENT PARSING --- #
# Every command pattern below is matched with re.fullmatch, never re.search: a
# partial match must fail loudly rather than execute a command the user only
# mentioned in passing. That only holds if the incoming text is stripped first,
# which handle_message does — otherwise a trailing space from a phone keyboard
# would defeat every command instead of just `B`.

# Accepts a dot or a comma decimal separator. Italian keyboards produce commas
# by reflex, and the old \d+\.?\d* matched "2" out of "2,20" and dropped the
# rest, recording EUR 2.00 as a success.
AMOUNT = r"(?P<amount>\d+(?:[.,]\d+)?)"


def parse_amount(raw: str):
    """Parse an amount written with either separator. Returns (amount, error)."""
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None, f"❌ Error: '{raw}' is not a valid amount."
    if value <= 0:
        return None, f"❌ Error: amount must be greater than zero, got {value:g}."
    return value, None


def resolve_category(raw):
    """Map a category shortcut to its Notion name. Returns (category, error).

    An ABSENT category falls back to the default. A SUPPLIED but unrecognised one
    is an error — otherwise a typo silently files the expense under Food, which
    is indistinguishable from having meant the default. Mirrors how genre already
    behaves.
    """
    if raw is None or not raw.strip():
        return DEFAULT_CATEGORY, None
    category = CATEGORY_MAP.get(raw.strip().lower())
    if category is None:
        return None, (f"❌ Error: unknown category '{raw.strip()}'. "
                      f"Please use: {category_help()}")
    return category, None


# --- COMMAND HANDLERS --- #
#
# One per registry entry, all with the same shape: (update, context, args),
# where `args` is the match's groupdict(). They are the bodies of what used to be
# the branches of one long if/elif chain in handle_message.
#
# Every downstream call below is resolved through david's own module namespace at
# call time (`add_Expenses(...)`, not a reference captured into the registry),
# which is what keeps the spies in tests/test_router.py pointed at the code the
# bot actually runs.

async def _cmd_help(update, context, args):
    await reply(update, build_help())


async def _cmd_undo(update, context, args):
    await handle_undo(update, context)


async def _cmd_budget(update, context, args):
    result_text = await asyncio.to_thread(budget)
    if result_text:
        # format_budget escapes the Notion category names it interpolates.
        await reply(update, result_text)
    else:
        await update.message.reply_text("❌ Error: Could not calculate budget.")


async def _cmd_diag(update, context, args):
    await handle_diag(update)


async def _cmd_dbs(update, context, args):
    await handle_dbs(update)


async def _cmd_month(update, context, args):
    await handle_month(update)


async def _cmd_find(update, context, args):
    await handle_find(update, args["query"])


async def _cmd_get(update, context, args):
    await handle_get(update, args["text"])


async def _cmd_remind(update, context, args):
    await handle_remind(update, args["text"])


async def _cmd_add_book(update, context, args):
    book_name   = args["name"].strip()
    author      = args["author"].strip()
    genre_input = args["genre"].strip()

    genre = GENRE_MAP.get(genre_input.lower())

    if genre is None: # Added check for invalid genre
        await update.message.reply_text(f"❌ Error: Invalid genre. Please use: {genre_help()}")
        return

    await update.message.reply_text(f"⏳ Adding '{book_name}' '{author}' '{genre_input}' to Notion...")

    # CALL THE NOTION FUNCTION
    page_id = await asyncio.to_thread(add_New_Book, book_name, author, genre)

    if page_id:
        await update.message.reply_text("✅ Success! Book added to your database.")
    else:
        await update.message.reply_text("❌ Error: Could not connect to Notion. Check your API keys.")


async def _cmd_add_quote(update, context, args):
    book_name     = args["book"].strip()
    quote_title   = args["title"].strip()
    quote_content = args["body"].strip()

    await update.message.reply_text(f"🔍 Searching '{book_name}' in library...")
    page_id = await asyncio.to_thread(find_Book_Page, book_name)

    if not page_id:
        await update.message.reply_text(f"⚠️ I didn't find '{book_name}' in the library.")
        return

    # --- PDF EXTRACTION MODE: attach PDF with this caption instead ---
    if " / " in quote_content:
        await reply(
            update,
            "📎 To extract a quote from a PDF, *attach the PDF file* and use it as the caption:\n\n"
            "`Add q [Book] - [Title] - [Begin text] / [End text]`",
        )
        return

    # --- MANUAL MODE: full quote provided directly ---
    if await asyncio.to_thread(add_Quote, page_id, quote_title, quote_content):
        await update.message.reply_text(f"✍️ Quote added to '{book_name}'!")
    else:
        await update.message.reply_text("❌ Error during quote transcription.")


async def _cmd_learn(update, context, args):
    # Detached: fetch + Claude can run for minutes. See run_detached.
    run_detached(context, update, handle_learn(update, args["text"]), "learn")


async def _cmd_implement(update, context, args):
    # Detached: the Claude merge alone is tens of seconds, under a page lock.
    run_detached(context, update, handle_implement(update, args["text"]), "implement")


async def _cmd_update_expense(update, context, args):
    name = args["name"].strip()

    amount, err = parse_amount(args["amount"])
    if err:
        await update.message.reply_text(err)
        return

    category, err = resolve_category(args["category"])
    if err:
        await update.message.reply_text(err)
        return

    await update.message.reply_text(f"🔍 Finding '{name}' to update to €{amount} [{category}]...")
    await _start_destructive_expense(update, context, expense_safety.UPDATE,
                                     name, amount=amount, category=category)


async def _cmd_delete_expense(update, context, args):
    name = args["name"].strip()

    await update.message.reply_text(f"🔍 Finding '{name}' to delete...")
    await _start_destructive_expense(update, context, expense_safety.DELETE, name)


async def _cmd_add_expense(update, context, args):
    name = args["name"].strip()

    amount, err = parse_amount(args["amount"])
    if err:
        await update.message.reply_text(err)
        return

    # --- IF NAME = C -> CARREFOUR (case-insensitive, like the command itself)
    if name.lower() == "c": name = "Carrefour"

    # --- CATEGORY: absent -> default, supplied but unknown -> error
    category, err = resolve_category(args["category"])
    if err:
        await update.message.reply_text(err)
        return

    await update.message.reply_text(f"⏳ Adding '{name}' (€{amount}) to Notion...")

    # CALL THE NOTION FUNCTION
    success = await asyncio.to_thread(add_Expenses, name, amount, category)

    if success:
        await update.message.reply_text("✅ Success! Expenses added to your database.")
    else:
        await update.message.reply_text("❌ Error: Could not connect to Notion. Check your API keys.")


# --- THE COMMAND REGISTRY --- #
#
# WHY A REGISTRY AND NOT AN if/elif CHAIN
# ---------------------------------------
# The chain this replaces held three things that could not be read off the page:
# which pattern wins (control-flow order), what each branch parses out
# (group(1)/group(2)/group(3), counted by eye), and what the command claims to do
# (a hand-written help string somewhere else entirely). The last one had already
# drifted: help advertised `Learn recipe`, which is not in learn.SUPPORTED_TYPES
# and answers "Unknown type recipe", it left out `Learn podcast` which does work,
# and it never mentioned that `Implement … - Diet` takes a different path from
# every other area.
#
# So all three now come from one declaration per command:
#   pattern      — compiled, matched with fullmatch, NAMED groups only
#   handler      — the branch body, above
#   help         — what `h` prints, GENERATED from this list (see build_help)
#   destructive  — see below
#
# NAMED GROUPS, NOT POSITIONAL. `U e (.+?) (\d+…)(?:\s+(\w+))?` fed group(3) to
# the category and group(2) to the amount; adding an optional group anywhere to
# their left silently renumbered both, and the result would have been a wrong
# amount written to Notion, not an exception.
#
# ORDER IS STILL PRECEDENCE — dispatch takes the first pattern that fullmatches,
# so this list IS the precedence table the order of the `if`s used to be. It is
# arranged for reading (and for the help it generates) rather than to resolve
# conflicts, which is sound only while there are none to resolve: every pattern
# is anchored on a distinct literal prefix (`add b`, `add q`, `add e`, `u e`,
# `d e`, `undo`, `learn`, …), so under fullmatch no string satisfies two. That is
# not left as an assurance — tests/test_router.py drives every input in its table
# through every pattern and fails if any input matches more than one.

@dataclass(frozen=True)
class Help:
    """How one command presents itself in the generated help message."""

    label: str                            # "📖 *ADD BOOK*" — emoji + bold name
    usage: tuple[str, ...] = ()           # rendered as `code` spans
    notes: tuple[str, ...] = ()           # rendered as _italic_ lines beneath
    inline: bool = False                  # "LABEL — `usage`" on one line
    group: str = ""                       # commands sharing a key share a block


@dataclass(frozen=True)
class HelpGroup:
    """A block several commands render into together."""

    label: str | None = None              # set when the members share ONE line
    notes: tuple[str, ...] = ()           # italic lines closing the block


@dataclass(frozen=True)
class Command:
    """One thing David answers to."""

    name: str                             # the trigger, verbatim: "Add e", "U e"
    pattern: re.Pattern
    handler: Callable                     # async (update, context, args) -> None
    help: Help | None = None
    # DESTRUCTIVE = mutates or removes a row that already exists, so it must go
    # through expense_safety's find-then-choose path instead of writing straight
    # away (Hard Rule 4). It is not a second copy of that wiring: the guards are
    # driven by the expense_safety.UPDATE/DELETE action the handler passes to
    # _start_destructive_expense, and there never was a hardcoded list to
    # replace. What the flag drives is the shared warning in the generated help,
    # and tests/test_router.py asserts the flag and the guarded path agree — a
    # destructive command that skipped the lookup turns that test red.
    destructive: bool = False


# The Learn types come from learn.SUPPORTED_TYPES, not from a list written out
# here — that is the exact drift this registry exists to make impossible. The
# hints are decoration: a type with no hint still prints, a hint for a type that
# is not supported never does.
_LEARN_ARG_HINTS = {
    "video":   "https://youtu.be/...",
    "article": "https://...",
    "book":    "[Title]",
    "podcast": "https://...",
    "pdf":     "",
}

_LEARN_USAGE = tuple(
    f"Learn {content_type} {_LEARN_ARG_HINTS.get(content_type, '[source]')}".strip()
    for content_type in SUPPORTED_TYPES
)

HELP_GROUPS = {
    "notion_ids": HelpGroup(label="🩺 *FIND NOTION IDs*"),
    "expense":    HelpGroup(notes=(f"Categories: {category_help()}",)),
}

COMMANDS = [
    Command(
        name="Add b",
        pattern=re.compile(r"add b (?P<name>.+?) - (?P<author>.+?) - (?P<genre>.+)", re.I),
        handler=_cmd_add_book,
        help=Help("📖 *ADD BOOK*",
                  usage=("Add b [Name] - [Author] - [Genre]",),
                  notes=(f"Genres: {genre_help()}",)),
    ),
    Command(
        name="Add q",
        # Two shapes, one pattern: a full quote, or the "[Begin] / [End]" markers
        # that only mean something on an attached PDF — the handler tells the
        # second apart and explains the upload flow.
        pattern=re.compile(r"add q (?P<book>.+?) - (?P<title>.+?) - (?P<body>[\s\S]+)", re.I),
        handler=_cmd_add_quote,
        help=Help("🖋️ *ADD QUOTE*",
                  usage=("Add q [Book] - [Title] - [Full quote]",
                         "Add q [Book] - [Title] - [Begin text] / [End text]"),
                  notes=("The second form reads the quote out of a PDF — attach it "
                         "and send the command as the caption",)),
    ),
    Command(
        name="Remind",
        # Only the prefix is checked here; validating the date and time is
        # handle_remind's job, so a malformed one still reaches it and gets a
        # usage message instead of "I didn't get that".
        pattern=re.compile(r"(?P<text>remind\s+.+)", re.I),
        handler=_cmd_remind,
        help=Help("📅 *REMINDER*",
                  usage=("Remind [Name] [DD.MM] - [HH.MM]",
                         "Remind [Name] [DD.MM.YYYY] - [HH.MM]"),
                  notes=("e.g. Remind Dentist 12.06 - 14.30 (time is 24h)",
                         "Without a year: a date over a day past means next year, and "
                         "one inside that window is queried rather than guessed")),
    ),
    Command(
        name="Add e",
        pattern=re.compile(rf"add e (?P<name>.+?) {AMOUNT}(?:\s+(?P<category>\w+))?", re.I),
        handler=_cmd_add_expense,
        help=Help("💵 *ADD EXPENSE*",
                  usage=("Add e [Name] [Amount] [Category]",),
                  inline=True, group="expense"),
    ),
    Command(
        name="U e",
        pattern=re.compile(rf"u e (?P<name>.+?) {AMOUNT}(?:\s+(?P<category>\w+))?", re.I),
        handler=_cmd_update_expense,
        help=Help("✏️ *UPDATE EXPENSE*",
                  usage=("U e [Name] [Amount] [Category]",),
                  inline=True, group="expense"),
        destructive=True,
    ),
    Command(
        name="D e",
        pattern=re.compile(r"d e (?P<name>.+)", re.I),
        handler=_cmd_delete_expense,
        help=Help("🗑️ *DELETE EXPENSE*",
                  usage=("D e [Name]",),
                  inline=True, group="expense"),
        destructive=True,
    ),
    Command(
        name="undo",
        pattern=re.compile(r"undo", re.I),
        handler=_cmd_undo,
        help=Help("↩️ *UNDO*", usage=("undo",), inline=True,
                  notes=("Reverses the last delete or update",)),
    ),
    Command(
        name="B",
        pattern=re.compile(r"b", re.I),
        handler=_cmd_budget,
        help=Help("💰 *BUDGET*", usage=("B",), inline=True),
    ),
    Command(
        name="Month",
        # Idempotent, so sending it twice is harmless; the scheduled job runs the
        # exact same call at 00:05 every night.
        pattern=re.compile(r"month", re.I),
        handler=_cmd_month,
        help=Help("🗓️ *MONTH PAGE*", usage=("Month",), inline=True,
                  notes=("Rolls over automatically on the 1st; this forces a check",)),
    ),
    Command(
        name="Diag",
        pattern=re.compile(r"diag", re.I),
        handler=_cmd_diag,
        help=Help("", usage=("Diag",), group="notion_ids"),
    ),
    Command(
        name="Find",
        pattern=re.compile(r"find\s+(?P<query>.+)", re.I),
        handler=_cmd_find,
        help=Help("", usage=("Find [name]",), group="notion_ids"),
    ),
    Command(
        name="DBs",
        pattern=re.compile(r"dbs", re.I),
        handler=_cmd_dbs,
        help=Help("", usage=("DBs",), group="notion_ids"),
    ),
    Command(
        name="Learn",
        pattern=re.compile(r"(?P<text>learn\s+\w+[\s\S]*)", re.I),
        handler=_cmd_learn,
        help=Help("🧠 *LEARN*", usage=_LEARN_USAGE,
                  notes=("Learn pdf needs the file attached, with the command as the caption",)),
    ),
    Command(
        name="Implement",
        pattern=re.compile(r"(?P<text>implement\s+.+\s*-\s*.+)", re.I),
        handler=_cmd_implement,
        help=Help("🔧 *IMPLEMENT*",
                  usage=("Implement [Page Name] - [Area]",),
                  notes=("Merges a Learn page into an Area Manual",
                         "Area `Diet` is different: it merges into the structured Diet "
                         "page (categories > rows > attributes), not a flat Manual")),
    ),
    Command(
        name="Get",
        # The separator is a SPACE-hyphen-SPACE, matching pkm.GET_PATTERN, so a
        # topic containing a hyphen ("Step-by-Step Breakdown") is not split at
        # it. Without the " - [Area]" it is not a command, exactly like
        # `Implement`.
        #
        # Runs INLINE, not detached: it is read-only, so it cannot reorder
        # against a write the way a detached command could. The slow case is a
        # toggle manual (Diet), where build_index walks the tree one request per
        # heading.
        pattern=re.compile(r"(?P<text>get\s+.+\s+-\s+.+)", re.I),
        handler=_cmd_get,
        help=Help("🔎 *GET*",
                  usage=("Get [Topic] - [Area]", "Get ? - [Area]"),
                  notes=("? in place of the topic lists every topic in that area",)),
    ),
    Command(
        name="h",
        pattern=re.compile(r"h|help|aiuto", re.I),
        handler=_cmd_help,
        help=Help("❓ *HELP*", usage=("h", "help", "aiuto"), inline=True),
    ),
]


# --- THE GENERATED HELP MESSAGE --- #

def _render_entry(entry: Help) -> list[str]:
    """One command's own lines: label, usage, notes."""
    spans = " · ".join(f"`{usage}`" for usage in entry.usage)
    if entry.inline:
        lines = [f"{entry.label} — {spans}" if spans else entry.label]
    else:
        lines = [entry.label] + [f"`{usage}`" for usage in entry.usage]
    return lines + [f"_{note}_" for note in entry.notes]


def build_help() -> str:
    """The `h` message, built from COMMANDS.

    GENERATED rather than written out so it cannot drift from what the bot
    actually answers to, which is the whole point — the hand-written version it
    replaces advertised `Learn recipe` (rejected by handle_learn as an unknown
    type), omitted `Learn podcast`, and said nothing about `Implement … - Diet`
    taking a different path.

    Commands appear in registry order, grouped by Help.group; a group with a
    label collapses its members onto one line, and one with notes closes on them.
    """
    blocks: dict[str, list[Command]] = {}
    for command in COMMANDS:
        if command.help is None:
            continue
        blocks.setdefault(command.help.group or command.name, []).append(command)

    rendered = []
    for key, members in blocks.items():
        group = HELP_GROUPS.get(key, HelpGroup())

        if group.label:
            spans = " · ".join(f"`{usage}`"
                               for member in members for usage in member.help.usage)
            lines = [f"{group.label} — {spans}"]
        else:
            lines = [line for member in members for line in _render_entry(member.help)]

        notes = list(group.notes)
        # The destructive flag earning its keep: the guard that makes `U e` and
        # `D e` safe is only reassuring if you know it is there, and naming the
        # commands from the flag means a third one cannot be added without this
        # sentence growing to cover it.
        guarded = [member.name for member in members if member.destructive]
        if guarded:
            notes.append(" and ".join(f"`{name}`" for name in guarded)
                         + " search this month only. Several matches → I list them "
                           "and wait for a number.")
        lines += [f"_{note}_" for note in notes]

        rendered.append("\n".join(lines))

    return "\n\n".join(rendered)


# --- TELEGRAM MESSAGE HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Tag every log line produced while handling this update — including from the
    # worker threads and detached tasks it spawns — with the update's ID, so one
    # command's whole trace is greppable even when two overlap.
    set_correlation_id(getattr(update, "update_id", None))
    record_command()

    # Stripped once, here: every pattern is a fullmatch, so stray leading or
    # trailing whitespace would otherwise miss every command.
    user_text = (update.message.text or "").strip()
    logger.info("Received: %s", user_text)

    # --- PENDING CHOICE: a bare number answering a printed list of matches ---
    # AHEAD OF THE REGISTRY, and not a Command itself, because it is the only
    # thing David answers that depends on STATE rather than on the text: while a
    # list is live the reply to it is a plain integer, which every pattern
    # rejects, so it would fall through to "I didn't get that" with the list left
    # unanswered. Guarded on there BEING a live list, so a stray "2" with nothing
    # pending stays an unrecognised message rather than a command with no visible
    # effect. See expense_safety.py.
    if expense_safety.has_pending(context):
        selection = expense_safety.parse_selection(user_text)
        if selection is not None:
            await handle_expense_selection(update, context, selection)
            return

    # fullmatch, never search: a partial match must fail loudly rather than
    # execute a command the user only mentioned in passing.
    for command in COMMANDS:
        match = command.pattern.fullmatch(user_text)
        if match:
            await command.handler(update, context, match.groupdict())
            return

    await update.message.reply_text("❓ I didn't get that. Try: 'Add e Carrefour 2.20'")


# --- PDF ATTACHMENT DOWNLOAD --- #

def validate_pdf_attachment(doc) -> str | None:
    """Cheap local checks on an attachment. Returns an error message, or None if OK.

    Split out from the download so a caller can reject a bad file before spending
    a Notion lookup on it. Pure and network-free, so calling it twice is free.
    """
    if doc is None:
        return "❌ No file attached."

    if doc.mime_type != "application/pdf":
        return "❌ Please attach a PDF file."

    # Telegram reports file_size up front for most uploads — reject oversized
    # files before downloading them. It is optional in the API, so the real
    # size is checked again after the download.
    if doc.file_size is not None and doc.file_size > MAX_PDF_BYTES:
        return (f"❌ That PDF is {doc.file_size / 1024 / 1024:.1f} MB. "
                f"The limit is {MAX_PDF_MB} MB.")

    return None


async def download_pdf_attachment(context: ContextTypes.DEFAULT_TYPE, doc):
    """Validate and download an attached PDF. Returns (pdf_bytes, error_message).

    WHY THIS EXISTS: tg_file.download_as_bytearray() has no built-in timeout and
    can hang forever on Railway. requests.get() with a timeout fails fast if the
    download stalls, and asyncio.wait_for caps the whole operation regardless.
    Both attachment paths in handle_document go through here, so neither can
    regress to an unbounded download.
    """
    err = validate_pdf_attachment(doc)
    if err:
        return None, err

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        # tg_file.file_path is the full Telegram CDN URL in PTB v20+

        def _download():
            resp = requests.get(tg_file.file_path, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.content

        content = await asyncio.wait_for(
            asyncio.to_thread(_download),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return None, ("❌ Download timed out after 2 minutes.\n"
                      "Try a smaller PDF.")
    except Exception as e:
        return None, f"❌ Download error: {e}"

    # file_size is optional in the Telegram API, so re-check what actually arrived.
    if len(content) > MAX_PDF_BYTES:
        return None, (f"❌ That PDF is {len(content) / 1024 / 1024:.1f} MB. "
                      f"The limit is {MAX_PDF_MB} MB.")

    return content, None


# --- UPLOAD WORK (run detached; see run_detached) --- #
# Split out of handle_document so the slow half — a download capped at 2 minutes,
# PyPDF2 parsing, and for Learn a full Claude summarisation — can run as a
# background task while the cheap validation stays inline and rejects a bad file
# immediately.

async def _learn_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            doc, caption: str):
    await update.message.reply_text("⏳ Downloading your PDF…")
    file_bytes, err = await download_pdf_attachment(context, doc)
    if err:
        await update.message.reply_text(err)
        return
    await handle_learn(update, caption, file_bytes=file_bytes)


async def _quote_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, doc,
                            book_name: str, quote_title: str,
                            begin_text: str, end_text: str):
    # Find book in Notion
    await update.message.reply_text(f"🔍 Searching \'{book_name}\' in library…")
    page_id = await asyncio.to_thread(find_Book_Page, book_name)
    if not page_id:
        await update.message.reply_text(f"⚠️ \'{book_name}\' not found in library.")
        return

    await update.message.reply_text("📄 Reading PDF and extracting quote…")

    pdf_bytes, err = await download_pdf_attachment(context, doc)
    if err:
        await update.message.reply_text(err)
        return

    # Extraction stays on a worker thread under its own cap: it parses every
    # page of the PDF, which would otherwise block the event loop (see the
    # note on extract_quote_from_pdf).
    try:
        quote_content, err = await asyncio.wait_for(
            asyncio.to_thread(extract_quote_from_pdf, pdf_bytes, begin_text, end_text),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "❌ Timed out after 2 minutes.\n"
            "Try shorter Begin/End markers or a smaller PDF."
        )
        return

    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    # Preview. This is raw text sliced out of an uploaded PDF at an arbitrary
    # 300-character boundary and dropped inside italic markers — the single most
    # likely value in the whole bot to contain a stray _ * ` or [.
    preview = quote_content[:300] + ("..." if len(quote_content) > 300 else "")
    await reply(
        update,
        f"📖 *Extracted* ({len(quote_content)} chars):\n\n_{escape_md(preview)}_",
    )

    # Save to Notion
    if await asyncio.to_thread(add_Quote, page_id, quote_title, quote_content):
        await update.message.reply_text(f"✍️ Quote added to \'{book_name}\'!")
    else:
        await update.message.reply_text("❌ Error saving quote to Notion.")


# --- HANDLER FUNCTION FOR PDF ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file uploads. Dispatches based on the message caption.

    Supported captions:
      Learn pdf                                          → summarise PDF, save to Learn DB
      Add q [Book] - [Title] - [Begin text] / [End text] → extract quote from attached PDF
    """
    # Same correlation tagging as handle_message — a PDF upload is a command too,
    # and it is the one most likely to run detached and interleave with another.
    set_correlation_id(getattr(update, "update_id", None))
    record_command()

    doc     = update.message.document
    caption = (update.message.caption or "").strip()
    logger.info("Received document with caption: %s", caption)

    # ── Learn pdf ──────────────────────────────────────────────────────────────
    if re.match(r"(?i)learn\s+pdf", caption):
        # Validated inline — it is pure and network-free, so a wrong file type or
        # an oversized upload is refused immediately rather than from a task.
        # download_pdf_attachment checks again; calling it twice costs nothing.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return
        run_detached(context, update,
                     _learn_pdf_upload(update, context, doc, caption), "learn-pdf")
        return

    # ── Add q [Book] - [Title] - [Begin] / [End]  (extract quote from PDF) ────
    quote_pdf_match = re.match(r"(?i)add q (.+?) - (.+?) - (.+?) / (.+)", caption)
    if quote_pdf_match:
        # Checked before the Notion lookup so a wrong-format file costs no API call.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return

        run_detached(
            context, update,
            _quote_pdf_upload(
                update, context, doc,
                quote_pdf_match.group(1).strip(),   # book name
                quote_pdf_match.group(2).strip(),   # quote title
                quote_pdf_match.group(3).strip(),   # begin text
                quote_pdf_match.group(4).strip(),   # end text
            ),
            "quote-pdf")
        return

    # ── Unknown caption ────────────────────────────────────────────────────────
    await reply(
        update,
        "📎 File received. Supported captions:\n\n"
        "`Learn pdf` — summarise and save to Learn DB\n"
        "`Add q [Book] - [Title] - [Begin] / [End]` — extract quote from this PDF",
    )


# --- START THE BOT ---
if __name__ == '__main__':
    config.validate()

    # UPDATES STAY SEQUENTIAL — do not add .concurrent_updates() here.
    #
    # This is a decision, not a leftover. Responsiveness is bought a different
    # way: the long commands (Learn, Implement, the PDF uploads) are dispatched
    # with run_detached, so they no longer hold up the queue, while every fast
    # command still runs to completion before the next update is looked at.
    #
    # Global concurrency would buy nothing on top of that and would cost the one
    # guarantee sequential dispatch still provides: ORDERING. `Add e Carrefour 5`
    # followed by `B` must report the new total. Locks cannot give that back —
    # they stop two cycles interleaving, they do not decide which runs first. So
    # the choice was per-command, and it is asserted in tests/test_concurrency.py
    # (test_fast_commands_stay_sequential).
    #
    # The locks are in place either way — expenses, calendar, and both Implement
    # flows — because run_detached does let two long commands overlap.
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # --- SCHEDULED JOBS ---
    register_jobs(application, CHAT_ID)

    # --- HANDLERS (owner-only) ---
    # config.validate() already proved OWNER_ID is set and numeric.
    register_handlers(application, int(OWNER_ID))

    # --- GLOBAL ERROR HANDLER ---
    # Any unhandled exception in a handler lands here and is reported to you,
    # instead of dying silently in the Railway logs. Defined at module scope.
    application.add_error_handler(on_error)

    logger.info("🤖 David online!")
    application.run_polling()
