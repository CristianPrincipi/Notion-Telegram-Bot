import os
import io
import requests
import json
import logging
import re
import asyncio
import pytz
from datetime import datetime, time
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from telegram.ext import filters as tg_filters
from learn import handle_learn
from implement import handle_implement
from reminder import handle_remind
import PyPDF2

from config import (
    GENRE_MAP, CATEGORY_MAP, PRIORITY_MAP, DEFAULT_CATEGORY,
    genre_help, category_help, priority_help,
)
from notion_client import notion_request
from budget import budget
from proactive.scheduler import register_all


# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
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


# --- NOTION API ---

headers = {'Authorization': f"Bearer {NOTION_KEY}",
           'Content-Type': 'application/json',
           'Notion-Version': '2022-06-28'}


# --- NOTION FUNCTIONS --- #

# --- BUDGET --- #
# budget() now lives in budget.py (compute_budget / format_budget / budget), so
# the proactive jobs can reuse the raw numbers. Imported above.


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
    url = f"https://api.notion.com/v1/databases/{LETTI_ID}/query"
    response = notion_request("POST", url, json={
        "filter": {"property": "Name", "title": {"contains": book_name.strip()}}
    })
    if response.status_code != 200:
        print(f"Errore query Notion: {response.status_code}")
        return None
    results = response.json().get("results")
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
    today = datetime.now().strftime("%Y-%m-%d")

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
    url = f"https://api.notion.com/v1/databases/{EXPENSES_ID}/query"
    query_data = {
        "filter": {
            "property": "Name",
            "title": {"contains": name.strip()}
        }
    }
    response = notion_request("POST", url, json=query_data)

    if response.status_code != 200:
        print(f"Error querying Notion for expense: {response.status_code}")
        return False, None

    results = response.json().get("results", [])
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
    url = f"https://api.notion.com/v1/databases/{EXPENSES_ID}/query"
    query_data = {
        "filter": {
            "property": "Name",
            "title": {"contains": name.strip()}
        }
    }
    response = notion_request("POST", url, json=query_data)

    if response.status_code != 200:
        print(f"Error querying Notion for expense: {response.status_code}")
        return False, None

    results = response.json().get("results", [])
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


# --- SCHEDULED JOB: SEND WEEKLY BUDGET RECAP --- #
async def send_weekly_budget(context: ContextTypes.DEFAULT_TYPE):
    try:
        result_text = budget()
        if result_text:
            await context.bot.send_message(chat_id=CHAT_ID, text=result_text, parse_mode='Markdown')
        else:
            await context.bot.send_message(chat_id=CHAT_ID, text="❌ Could not fetch budget from Notion.")
    except Exception as e:
        await notify_error(context, "send_weekly_budget", e)


# --- TELEGRAM MESSAGE HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
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

    # --- REGEX FOR REMINDER: "Remind [Name] [Date] - [Time]" ---
    if re.match(r"(?i)remind\s+", user_text):
        await handle_remind(update, user_text)
        return

    # --- REGEX FOR NEW BOOK: Look for "Add b [Book's Name] - [Author] - [Genre]"
    book_pattern = r"(?i)add b (.+?) - (.+?) - (.+)"
    book_match = re.search(book_pattern, user_text)

    if book_match:
        book_name = book_match.group(1).strip()
        author = book_match.group(2).strip()
        genre_input = book_match.group(3)

        genre = GENRE_MAP.get(genre_input.lower())

        if genre is None: # Added check for invalid genre
            await update.message.reply_text(f"❌ Error: Invalid genre. Please use: {genre_help()}")
            return

        await update.message.reply_text(f"⏳ Adding '{book_name}' '{author}' '{genre_input}' to Notion...")

        # CALL THE NOTION FUNCTION
        page_id = add_New_Book(book_name, author, genre)

        if page_id:
            await update.message.reply_text(f"✅ Success! Book added to your database.")
        else:
            await update.message.reply_text("❌ Error: Could not connect to Notion. Check your API keys.")
        return

    # --- REGEX FOR QUOTES ---
    # Supports two formats:
    #   Manual:  Add q [Book] - [Title] - [Full quote]
    #   PDF:     Add q [Book] - [Title] - [Begin text] / [End text]
    quote_pattern = r"(?i)add q (.+?) - (.+?) - ([\s\S]+)"
    quote_match = re.search(quote_pattern, user_text)

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
    if re.match(r"(?i)learn\s+\w+", user_text):
        await handle_learn(update, user_text)
        return

    # --- REGEX FOR IMPLEMENT COMMAND: "Implement [Page Name] - [Target Area]" ---
    if re.match(r"(?i)implement\s+.+\s*-\s*.+", user_text):
        await handle_implement(update, user_text)
        return

    # --- REGEX FOR UPDATE EXPENSE: Look for "U e [Name] [Amount] [Category]"
    update_expense_match = re.fullmatch(r"(?i)U e (.+?) (\d+\.?\d*)(?:\s+(\w+))?", user_text)
    if update_expense_match:
        name = update_expense_match.group(1).strip()
        amount = float(update_expense_match.group(2))
        category_input = update_expense_match.group(3)
        category = CATEGORY_MAP.get(category_input.lower() if category_input else "", DEFAULT_CATEGORY)

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
    pattern = r"(?i)add e (.+?) (\d+\.?\d*)(?:\s+(\w+))?"
    expenses_match = re.search(pattern, user_text)

    if expenses_match:
        name = expenses_match.group(1).strip()
        amount = float(expenses_match.group(2))
        category_input = expenses_match.group(3)

        # --- IF NAME = C -> CARREFOUR
        if name == "c": name = "Carrefour"

        # --- IF CATEGORY = NULL -> FOOD
        category = CATEGORY_MAP.get(category_input.lower() if category_input else "", DEFAULT_CATEGORY)

        await update.message.reply_text(f"⏳ Adding '{name}' (€{amount}) to Notion...")

        # CALL THE NOTION FUNCTION
        success = add_Expenses(name, amount, category)

        if success:
            await update.message.reply_text(f"✅ Success! Expenses added to your database.")
        else:
            await update.message.reply_text("❌ Error: Could not connect to Notion. Check your API keys.")
    else:
        await update.message.reply_text("❓ I didn't get that. Try: 'Add e Carrefour 2.20'")


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
        tg_file    = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        await handle_learn(update, caption, file_bytes=bytes(file_bytes))
        return

    # ── Add q [Book] - [Title] - [Begin] / [End]  (extract quote from PDF) ────
    quote_pdf_match = re.match(r"(?i)add q (.+?) - (.+?) - (.+?) / (.+)", caption)
    if quote_pdf_match:
        if doc.mime_type != "application/pdf":
            await update.message.reply_text("❌ Please attach a PDF file.")
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

        # Download PDF from Telegram + extract quote — both run in a background
        # thread under a single timeout.
        #
        # WHY: tg_file.download_as_bytearray() has no built-in timeout and can hang
        # forever on Railway. requests.get() with timeout=30 fails fast if the
        # download stalls. asyncio.wait_for covers the entire operation so the
        # 2-minute cap is always enforced.
        await update.message.reply_text("📄 Reading PDF and extracting quote…")
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            # tg_file.file_path is the full Telegram CDN URL in PTB v20+

            def _download_and_extract():
                resp = requests.get(tg_file.file_path, timeout=30)
                resp.raise_for_status()
                return extract_quote_from_pdf(resp.content, begin_text, end_text)

            quote_content, err = await asyncio.wait_for(
                asyncio.to_thread(_download_and_extract),
                timeout=120,
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                "❌ Timed out after 2 minutes.\n"
                "Try shorter Begin/End markers or a smaller PDF."
            )
            return
        except Exception as e:
            await update.message.reply_text(f"❌ Download error: {e}")
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
    milan_tz = pytz.timezone("Europe/Rome")

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # --- SCHEDULED JOBS ---
    try:
        job_queue = application.job_queue

        # Steps 1–2 — Morning + Evening Briefings (today+budget / tomorrow).
        # These fully replace the old send_daily_reminders job.
        register_all(application, CHAT_ID)

        job_queue.run_daily(send_weekly_budget, time=time(hour=9, minute=30, tzinfo=milan_tz), days=(5, 6))  # 0=Mon ... 6=Sun
        print("✅ Scheduled jobs registered.")
    except Exception as e:
        print(f"⚠️ Scheduled jobs not available: {e}")
        print("Bot will still work normally — scheduling runs fine on Railway.")

    # LISTEN FOR ANY TEXT MESSAGE... (except commands)
    text_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(text_handler)

    doc_handler = MessageHandler(filters.Document.ALL, handle_document)
    application.add_handler(doc_handler)

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
