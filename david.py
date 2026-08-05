import os
import io
import logging
import requests
import re
import asyncio
import pytz
from datetime import time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from learn import handle_learn
from implement import handle_implement
from reminder import handle_remind
from calendar_client import now_local
from notion_ids import handle_diag, handle_find, handle_dbs
import PyPDF2

import config
from budget import budget
from config import (
    GENRE_MAP, CATEGORY_MAP, DEFAULT_CATEGORY, EXPENSE_MONTH_RELATION,
    PROACTIVE_TIMEZONE, SUNDAY, category_help, genre_help,
)
from month import current_month_id, handle_month
from notion_client import body_excerpt, notion_request, query_database
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
NOTION_KEY = os.environ.get("NOTION_KEY")
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


# --- NOTION API ---

headers = {'Authorization': f"Bearer {NOTION_KEY}",
           'Content-Type': 'application/json',
           'Notion-Version': '2022-06-28'}


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
    """Search LETTI database for a book by name. Returns page_id or None."""
    results, err = query_database(
        LETTI_ID,
        filter_obj={"property": "Name", "title": {"contains": book_name.strip()}},
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


# --- UPDATE EXPENSES FUNCTION ---
def update_Expense(name, amount, category):
    # 1. Find the expense page ID by name
    results, err = query_database(
        EXPENSES_ID,
        filter_obj={"property": "Name", "title": {"contains": name.strip()}},
    )

    if err:
        logger.error("update_Expense(%r): Notion query failed: %s", name, err)
        return False, None

    if not results:
        logger.info("update_Expense(%r): no matching expense.", name)
        return False, None

    page_id = results[0]["id"]

    # 2. Patch the page with the new amount and category
    update_url = f"https://api.notion.com/v1/pages/{page_id}"
    update_data = {
        "properties": {
            "Amount": {"number": amount},
            "Category": {"multi_select": [{"name": category}]}
        }
    }
    update_response = notion_request("PATCH", update_url, json=update_data)

    if update_response.status_code != 200:
        logger.error("update_Expense(%r) failed: Notion %s: %s",
                     name, update_response.status_code, body_excerpt(update_response))
        return False, page_id

    return True, page_id


# --- DELETE EXPENSES FUNCTION ---
def delete_Expense(name):
    # 1. Find the expense page ID by name
    results, err = query_database(
        EXPENSES_ID,
        filter_obj={"property": "Name", "title": {"contains": name.strip()}},
    )

    if err:
        logger.error("delete_Expense(%r): Notion query failed: %s", name, err)
        return False, None

    if not results:
        logger.info("delete_Expense(%r): no matching expense.", name)
        return False, None

    page_id = results[0]["id"]

    # 2. Archive the page (Notion API does not support hard delete)
    update_url = f"https://api.notion.com/v1/pages/{page_id}"
    update_response = notion_request("PATCH", update_url, json={"archived": True})

    if update_response.status_code != 200:
        logger.error("delete_Expense(%r) failed: Notion %s: %s",
                     name, update_response.status_code, body_excerpt(update_response))
        return False, page_id

    return True, page_id


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
AMOUNT = r"(\d+(?:[.,]\d+)?)"


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


# --- TELEGRAM MESSAGE HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Tag every log line produced while handling this update — including from the
    # worker threads and detached tasks it spawns — with the update's ID, so one
    # command's whole trace is greppable even when two overlap.
    set_correlation_id(getattr(update, "update_id", None))
    record_command()

    # Stripped once, here: every pattern below is a fullmatch, so stray leading
    # or trailing whitespace would otherwise miss every command.
    user_text = (update.message.text or "").strip()
    logger.info("Received: %s", user_text)

    # --- REGEX FOR HELP COMMAND: Look for "h"
    if re.fullmatch(r"(?i)h|help|aiuto", user_text):
        await reply(
            update,
            "📖 *ADD BOOK*\n"
            "`Add b [Name] - [Author] - [Genre]`\n"
            "_Genres: s · h · m · p · a · ph_\n\n"
            "🖋️ *ADD QUOTE — manual*\n"
            "`Add q [Book] - [Title] - [Full quote]`\n\n"
            "📄 *ADD QUOTE — from PDF*\n"
            "_Attach the PDF and use this caption:_\n"
            "`Add q [Book] - [Title] - [Begin text] / [End text]`\n\n"
            "📅 *REMINDER*\n"
            "`Remind [Name] [Date] - [Time]`\n"
            "_e.g. Remind Dentist 12.06 - 14.30 (date DD.MM, time HH.MM 24h)_\n\n"
            "💵 *ADD EXPENSE* — `Add e [Name] [Amount] [Category]`\n"
            "✏️ *UPDATE EXPENSE* — `U e [Name] [Amount] [Category]`\n"
            "🗑️ *DELETE EXPENSE* — `D e [Name]`\n"
            "_Categories: s · f · g · o_\n\n"
            "💰 *BUDGET* — `B`\n\n"
            "🗓️ *MONTH PAGE* — `Month`\n"
            "_Rolls over automatically on the 1st; this forces a check_\n\n"
            "🩺 *FIND NOTION IDs* — `Diag` · `Find [name]` · `DBs`\n\n"
            "🧠 *LEARN*\n"
            "`Learn video https://youtu.be/...`\n"
            "`Learn article https://...`\n"
            "`Learn book [Title]`\n"
            "`Learn recipe https://...`\n"
            "`Learn pdf`  _(attach PDF as caption)_\n\n"
            "🔧 *IMPLEMENT*\n"
            "`Implement [Page Name] - [Area]`\n"
            "_Merges a Learn page into an Area Manual_\n\n"
            "🔎 *GET*\n"
            "`Get [Topic] - [Area]`\n"
            "`Get ? - [Area]`  _(list every topic)_",
        )
        return

    # --- REGEX FOR BUDGET: Look for "B"
    if re.fullmatch(r"(?i)B", user_text):
        result_text = await asyncio.to_thread(budget)
        if result_text:
            # format_budget escapes the Notion category names it interpolates.
            await reply(update, result_text)
        else:
            await update.message.reply_text("❌ Error: Could not calculate budget.")
        return

    # --- DIAGNOSTIC: "Diag" → introspect Notion IDs + schema, report to Telegram ---
    if re.fullmatch(r"(?i)diag", user_text):
        await handle_diag(update)
        return

    # --- LIST DATABASES: "DBs" → every database the integration can see + its ID ---
    if re.fullmatch(r"(?i)dbs", user_text):
        await handle_dbs(update)
        return

    # --- MONTH: "Month" → force the monthly-page rollover now and report ---
    # Idempotent, so sending it twice is harmless; the scheduled job runs the
    # exact same call at 00:05 every night.
    if re.fullmatch(r"(?i)month", user_text):
        await handle_month(update)
        return

    # --- FIND: "Find [query]" → search pages/databases by name, return IDs ---
    find_match = re.fullmatch(r"(?i)find\s+(.+)", user_text)
    if find_match:
        await handle_find(update, find_match.group(1))
        return

    # --- RETRIEVE: "Get [Topic] - [Area]" → read a section back out of a Manual ---
    # The separator is a SPACE-hyphen-SPACE, matching pkm.GET_PATTERN, so a topic
    # containing a hyphen ("Step-by-Step Breakdown") is not split at it. Without
    # the " - [Area]" it is not a command, exactly like `Implement`.
    #
    # Runs INLINE, not detached: it is read-only, so it cannot reorder against a
    # write the way a detached command could. The slow case is a toggle manual
    # (Diet), where build_index walks the tree one request per heading.
    if re.fullmatch(r"(?i)get\s+.+\s+-\s+.+", user_text):
        await handle_get(update, user_text)
        return

    # --- REGEX FOR REMINDER: "Remind [Name] [Date] - [Time]" ---
    if re.fullmatch(r"(?i)remind\s+(.+)", user_text):
        await handle_remind(update, user_text)
        return

    # --- REGEX FOR NEW BOOK: Look for "Add b [Book's Name] - [Author] - [Genre]"
    book_pattern = r"(?i)add b (.+?) - (.+?) - (.+)"
    book_match = re.fullmatch(book_pattern, user_text)

    if book_match:
        book_name = book_match.group(1).strip()
        author = book_match.group(2).strip()
        genre_input = book_match.group(3).strip()

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
        return

    # --- REGEX FOR QUOTES ---
    # Supports two formats:
    #   Manual:  Add q [Book] - [Title] - [Full quote]
    #   PDF:     Add q [Book] - [Title] - [Begin text] / [End text]
    quote_pattern = r"(?i)add q (.+?) - (.+?) - ([\s\S]+)"
    quote_match = re.fullmatch(quote_pattern, user_text)

    if quote_match:
        book_name     = quote_match.group(1).strip()
        quote_title   = quote_match.group(2).strip()
        quote_content = quote_match.group(3).strip()

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
        return

    # --- REGEX FOR LEARN COMMAND: "Learn [type] [source]" ---
    # Detached: fetch + Claude can run for minutes. See run_detached.
    if re.fullmatch(r"(?i)learn\s+\w+[\s\S]*", user_text):
        run_detached(context, update, handle_learn(update, user_text), "learn")
        return

    # --- REGEX FOR IMPLEMENT COMMAND: "Implement [Page Name] - [Target Area]" ---
    # Detached: the Claude merge alone is tens of seconds, under a page lock.
    if re.fullmatch(r"(?i)implement\s+.+\s*-\s*.+", user_text):
        run_detached(context, update, handle_implement(update, user_text), "implement")
        return

    # --- REGEX FOR UPDATE EXPENSE: Look for "U e [Name] [Amount] [Category]"
    update_expense_match = re.fullmatch(rf"(?i)U e (.+?) {AMOUNT}(?:\s+(\w+))?", user_text)
    if update_expense_match:
        name = update_expense_match.group(1).strip()

        amount, err = parse_amount(update_expense_match.group(2))
        if err:
            await update.message.reply_text(err)
            return

        category, err = resolve_category(update_expense_match.group(3))
        if err:
            await update.message.reply_text(err)
            return

        await update.message.reply_text(f"⏳ Updating '{name}' to €{amount} [{category}]...")

        try:
            async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
                success, page_id = await asyncio.to_thread(update_Expense, name, amount, category)
        except PageBusy:
            await update.message.reply_text(BUSY_EXPENSE_MESSAGE)
            return

        if success:
            await update.message.reply_text(f"✅ Expense '{name}' updated successfully!")
        else:
            if page_id is None:
                await update.message.reply_text(f"❌ Error: Expense '{name}' not found.")
            else:
                await update.message.reply_text(f"❌ Error: Could not update '{name}'. Check your API keys.")
        return

    # --- REGEX FOR DELETE EXPENSE: Look for "D e [Name]"
    delete_expense_match = re.fullmatch(r"(?i)D e (.+)", user_text)
    if delete_expense_match:
        name = delete_expense_match.group(1).strip()

        await update.message.reply_text(f"⏳ Deleting expense '{name}'...")

        try:
            async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
                success, page_id = await asyncio.to_thread(delete_Expense, name)
        except PageBusy:
            await update.message.reply_text(BUSY_EXPENSE_MESSAGE)
            return

        if success:
            await update.message.reply_text(f"🗑️ Expense '{name}' deleted successfully!")
        else:
            if page_id is None:
                await update.message.reply_text(f"❌ Error: Expense '{name}' not found.")
            else:
                await update.message.reply_text(f"❌ Error: Could not delete '{name}'. Check your API keys.")
        return

    # REGEX FOR EXPENSES: Look for "Add e [Name] [Amount] [Category]"
    pattern = rf"(?i)add e (.+?) {AMOUNT}(?:\s+(\w+))?"
    expenses_match = re.fullmatch(pattern, user_text)

    if expenses_match:
        name = expenses_match.group(1).strip()

        amount, err = parse_amount(expenses_match.group(2))
        if err:
            await update.message.reply_text(err)
            return

        # --- IF NAME = C -> CARREFOUR (case-insensitive, like the command itself)
        if name.lower() == "c": name = "Carrefour"

        # --- CATEGORY: absent -> default, supplied but unknown -> error
        category, err = resolve_category(expenses_match.group(3))
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
    else:
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
