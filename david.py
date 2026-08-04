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
from config import (
    BUDGET_CEILING, GENRE_MAP, CATEGORY_MAP, DEFAULT_CATEGORY,
    PROACTIVE_TIMEZONE, SUNDAY, category_help, genre_help,
)
from notion_client import notion_request, query_database
from proactive.scheduler import register_all

# Configured at import so config.validate() can still be the first statement in
# __main__ and have somewhere to send its warnings.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("david")


# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
NOTION_KEY = os.environ.get("NOTION_KEY")
DATABASE_ID = os.environ.get("DATABASE_ID")
EXPENSES_ID = os.environ.get("EXPENSES_ID")
MONTH_ID = os.environ.get("MONTH_ID")
LETTI_ID = os.environ.get("LETTI_ID")
LITERATURE_ID = os.environ.get("LITERATURE_ID")
CHAT_ID = os.environ.get("CHAT_ID")
LEARN_ID = os.environ.get("LEARN_ID")
DIET_ID = os.environ.get("DIET_ID")
BRAIN_ID = os.environ.get("BRAIN_ID")
FINANCE_ID = os.environ.get("FINANCE_ID")


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

# --- BUDGET --- #
def budget():
    # Paginated: Notion caps a query at 100 rows, so a single request silently
    # understates the total from the 101st expense of the month onwards.
    results, err = query_database(
        EXPENSES_ID,
        filter_obj={"property": "Account", "relation": {"contains": MONTH_ID}},
    )

    if err:
        print(f"Error: {err}")
        return None

    # Single-pass aggregation — one dict, no variable shadowing, O(n) not O(n²)
    cat_Tot = {}
    grand_Total = 0.0

    for page in results:
        props = page.get("properties", {})

        line_amount = props.get("Amount", {}).get("number", 0) or 0
        grand_Total += line_amount

        cat_multi = props.get("Category", {}).get("multi_select", [])
        category_name = cat_multi[0].get("name", "Other") if cat_multi else "Other"

        cat_Tot[category_name] = cat_Tot.get(category_name, 0) + line_amount

    remaining = BUDGET_CEILING - grand_Total

    # Construct message — show every category dynamically (not just the hardcoded 4)
    msg = "💰 **Monthly Budget**\n"
    msg += "━━━━━━━━━━━━━━━\n"
    for cat in sorted(cat_Tot, key=lambda c: cat_Tot[c], reverse=True):
        msg += f"**{cat}: €{cat_Tot[cat]:.2f}**\n"
    msg += "━━━━━━━━━━━━━━━\n"
    msg += f"**Spent: €{grand_Total:.2f}**\n"
    msg += f"**Remaining: €{remaining:.2f}** (of €{BUDGET_CEILING:.0f})"

    return msg


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
        print(f"Errore: {response.status_code}, {response.json()}")
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
        print(f"Errore query Notion: {err}")
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
            print("\n===== NOTION ERROR =====")
            print(f"Status: {response.status_code}")
            print(response.text)
            print("========================\n")
            return False

    return True


# --- NEW EXPENSES FUNCTION ---
def add_Expenses(name, amount, category):

    # --- GENERATE TODAY DATE ---
    # Europe/Rome, not the host clock: Railway runs UTC, so a naive now() files
    # anything logged after local midnight under YESTERDAY — and at a month
    # boundary, into the wrong month's budget entirely.
    today = now_local().strftime("%Y-%m-%d")

    data = {
        "parent": {"database_id": EXPENSES_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": name}}]},
            "Amount": {"number": amount},
            "Date": {"date": {"start": today}},
            "Category":{"multi_select": [{"name": category}]},
            "Account": {"relation": [{"id": MONTH_ID}]}
        }
    }

    response = notion_request("POST", "https://api.notion.com/v1/pages", json=data)

    # --- DEBUGGING --- #
    if response.status_code != 200:
        print(f"Errore: {response.status_code}")
        print(response.json())

    return response.status_code == 200


# --- UPDATE EXPENSES FUNCTION ---
def update_Expense(name, amount, category):
    # 1. Find the expense page ID by name
    results, err = query_database(
        EXPENSES_ID,
        filter_obj={"property": "Name", "title": {"contains": name.strip()}},
    )

    if err:
        print(f"Error querying Notion for expense: {err}")
        return False, None

    if not results:
        print(f"No expense found with name: {name}")
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
        print(f"Error updating expense: {update_response.status_code}")
        print(update_response.json())
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
        print(f"Error querying Notion for expense: {err}")
        return False, None

    if not results:
        print(f"No expense found with name: {name}")
        return False, None

    page_id = results[0]["id"]

    # 2. Archive the page (Notion API does not support hard delete)
    update_url = f"https://api.notion.com/v1/pages/{page_id}"
    update_response = notion_request("PATCH", update_url, json={"archived": True})

    if update_response.status_code != 200:
        print(f"Error archiving expense: {update_response.status_code}")
        print(update_response.json())
        return False, page_id

    return True, page_id


# --- ERROR REPORTING HELPER --- #
async def notify_error(context: ContextTypes.DEFAULT_TYPE, where: str, err: Exception):
    """Send a Telegram message to the owner when something fails silently in the background."""
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ David error in *{where}*:\n`{type(err).__name__}: {err}`",
            parse_mode="Markdown",
        )
    except Exception:
        print(f"[notify_error] failed to report error in {where}: {err}")


# --- SCHEDULED JOB: SEND BUDGET RECAP --- #
async def send_budget_recap(context: ContextTypes.DEFAULT_TYPE):
    try:
        result_text = budget()
        if result_text:
            await context.bot.send_message(chat_id=CHAT_ID, text=result_text, parse_mode='Markdown')
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
            print("⚠️ JobQueue unavailable — scheduled jobs not registered "
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
    except Exception as e:
        print(f"⚠️ Scheduled jobs not available: {e}")
        print("Bot will still work normally — scheduling runs fine on Railway.")
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
    # Stripped once, here: every pattern below is a fullmatch, so stray leading
    # or trailing whitespace would otherwise miss every command.
    user_text = (update.message.text or "").strip()
    print(f"Received: {user_text}") # So you can see it in Colab logs

    # --- REGEX FOR HELP COMMAND: Look for "h"
    if re.fullmatch(r"(?i)h|help|aiuto", user_text):
        await update.message.reply_text(
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
            "🩺 *FIND NOTION IDs* — `Diag` · `Find [name]` · `DBs`\n\n"
            "🧠 *LEARN*\n"
            "`Learn video https://youtu.be/...`\n"
            "`Learn article https://...`\n"
            "`Learn book [Title]`\n"
            "`Learn recipe https://...`\n"
            "`Learn pdf`  _(attach PDF as caption)_\n\n"
            "🔧 *IMPLEMENT*\n"
            "`Implement [Page Name] - [Area]`\n"
            "_Merges a Learn page into an Area Manual_",
            parse_mode="Markdown",
        )
        return

    # --- REGEX FOR BUDGET: Look for "B"
    if re.fullmatch(r"(?i)B", user_text):
        result_text = budget()
        if result_text:
            await update.message.reply_text(result_text, parse_mode='Markdown')
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

    # --- FIND: "Find [query]" → search pages/databases by name, return IDs ---
    find_match = re.fullmatch(r"(?i)find\s+(.+)", user_text)
    if find_match:
        await handle_find(update, find_match.group(1))
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
        page_id = add_New_Book(book_name, author, genre)

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
        page_id = find_Book_Page(book_name)

        if not page_id:
            await update.message.reply_text(f"⚠️ I didn't find '{book_name}' in the library.")
            return

        # --- PDF EXTRACTION MODE: attach PDF with this caption instead ---
        if " / " in quote_content:
            await update.message.reply_text(
                "📎 To extract a quote from a PDF, *attach the PDF file* and use it as the caption:\n\n"
                "`Add q [Book] - [Title] - [Begin text] / [End text]`",
                parse_mode="Markdown",
            )
            return

        # --- MANUAL MODE: full quote provided directly ---
        if add_Quote(page_id, quote_title, quote_content):
            await update.message.reply_text(f"✍️ Quote added to '{book_name}'!")
        else:
            await update.message.reply_text("❌ Error during quote transcription.")
        return

    # --- REGEX FOR LEARN COMMAND: "Learn [type] [source]" ---
    if re.fullmatch(r"(?i)learn\s+\w+[\s\S]*", user_text):
        await handle_learn(update, user_text)
        return

    # --- REGEX FOR IMPLEMENT COMMAND: "Implement [Page Name] - [Target Area]" ---
    if re.fullmatch(r"(?i)implement\s+.+\s*-\s*.+", user_text):
        await handle_implement(update, user_text)
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

        success, page_id = update_Expense(name, amount, category)

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

        success, page_id = delete_Expense(name)

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
        success = add_Expenses(name, amount, category)

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


# --- HANDLER FUNCTION FOR PDF ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file uploads. Dispatches based on the message caption.

    Supported captions:
      Learn pdf                                          → summarise PDF, save to Learn DB
      Add q [Book] - [Title] - [Begin text] / [End text] → extract quote from attached PDF
    """
    doc     = update.message.document
    caption = (update.message.caption or "").strip()

    # ── Learn pdf ──────────────────────────────────────────────────────────────
    if re.match(r"(?i)learn\s+pdf", caption):
        await update.message.reply_text("⏳ Downloading your PDF…")
        file_bytes, err = await download_pdf_attachment(context, doc)
        if err:
            await update.message.reply_text(err)
            return
        await handle_learn(update, caption, file_bytes=file_bytes)
        return

    # ── Add q [Book] - [Title] - [Begin] / [End]  (extract quote from PDF) ────
    quote_pdf_match = re.match(r"(?i)add q (.+?) - (.+?) - (.+?) / (.+)", caption)
    if quote_pdf_match:
        # Checked before the Notion lookup so a wrong-format file costs no API call.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return

        book_name   = quote_pdf_match.group(1).strip()
        quote_title = quote_pdf_match.group(2).strip()
        begin_text  = quote_pdf_match.group(3).strip()
        end_text    = quote_pdf_match.group(4).strip()

        # Find book in Notion
        await update.message.reply_text(f"🔍 Searching \'{book_name}\' in library…")
        page_id = find_Book_Page(book_name)
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

        # Preview
        preview = quote_content[:300] + ("..." if len(quote_content) > 300 else "")
        await update.message.reply_text(
            f"📖 *Extracted* ({len(quote_content)} chars):\n\n_{preview}_",
            parse_mode="Markdown",
        )

        # Save to Notion
        if add_Quote(page_id, quote_title, quote_content):
            await update.message.reply_text(f"✍️ Quote added to \'{book_name}\'!")
        else:
            await update.message.reply_text("❌ Error saving quote to Notion.")
        return

    # ── Unknown caption ────────────────────────────────────────────────────────
    await update.message.reply_text(
        "📎 File received. Supported captions:\n\n"
        "`Learn pdf` — summarise and save to Learn DB\n"
        "`Add q [Book] - [Title] - [Begin] / [End]` — extract quote from this PDF",
        parse_mode="Markdown",
    )


# --- START THE BOT ---
if __name__ == '__main__':
    config.validate()

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # --- SCHEDULED JOBS ---
    register_jobs(application, CHAT_ID)

    # --- HANDLERS (owner-only) ---
    # config.validate() already proved OWNER_ID is set and numeric.
    register_handlers(application, int(OWNER_ID))

    # --- GLOBAL ERROR HANDLER ---
    # Any unhandled exception in a handler lands here and is reported to you,
    # instead of dying silently in the Railway logs.
    async def on_error(update, context):
        err = context.error
        print(f"[on_error] {type(err).__name__}: {err}")
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ David hit an error:\n`{type(err).__name__}: {err}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"[on_error] failed to report: {e}")

    application.add_error_handler(on_error)

    print("🤖 David online!")
    application.run_polling()
