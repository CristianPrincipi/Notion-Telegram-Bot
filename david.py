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


# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
NOTION_KEY = os.environ.get("NOTION_KEY")
DATABASE_ID = os.environ.get("DATABASE_ID")
EXPENSES_ID = os.environ.get("EXPENSES_ID")
MONTH_ID = os.environ.get("MONTH_ID")
LETTI_ID = os.environ.get("LETTI_ID")
LITERATURE_ID = os.environ.get("LITERATURE_ID")
TASK_ID = os.environ.get("TASK_ID")
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
def budget():
    url = f"https://api.notion.com/v1/databases/{EXPENSES_ID}/query"

    # Filter by the relation to the current Month ID
    query_data = {
        "filter": {
            "property": "Account",
            "relation": {
                "contains": MONTH_ID
            }
        }
    }
    response = requests.post(url, headers=headers, json=query_data)

    if response.status_code != 200:
        print(f"Error: {response.status_code}")
        return None

    results = response.json().get("results", [])
    categories = {}
    category_Totals = []
    grand_Total = 0.0

    for page in results:
        props = page.get("properties", {})

        # Get Amount
        amount = props.get("Amount", {}).get("number", 0) or 0
        grand_Total += amount

        # Get Category Name
        cat_multi = props.get("Category", {}).get("multi_select", [])
        category_name = cat_multi[0].get("name", "Other") if cat_multi else "Other"

        # Aggregate by category
        category_Totals.append((category_name, amount))
        cat_Tot = {}

        for cat, amount in category_Totals:
            if cat in cat_Tot:
                cat_Tot[cat] += amount
            else:
              cat_Tot[cat] = amount

    # Construct Message
    msg = "💰 **Monthly Budget**\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"**Food: €{cat_Tot.get('Food', 0):.2f} \n**"
    msg += f"**Shopping: €{cat_Tot.get('Shopping', 0):.2f}\n**"
    msg += f"**Gift: €{cat_Tot.get('Gift', 0):.2f}\n**"
    msg += f"**Other: €{cat_Tot.get('Other', 0):.2f}\n**"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"**Total: €{grand_Total:.2f} \n**"
    msg += f"**Total: €{300 - grand_Total:.2f}**"

    return msg


# --- TASK LIST --- #
def task_List():
    url = f"https://api.notion.com/v1/databases/{TASK_ID}/query"
    query_data = {"filter": {"and": [{"property": "Date", "date": {"equals": datetime.now().strftime("%Y-%m-%d")}},{"property": " ", "checkbox": {"equals": False}}]}}
    response = requests.post(url, headers=headers, json=query_data)

    if response.status_code != 200:
        print(f"Error: {response.status_code}")
        return None

    results = response.json().get("results", [])
    msg = "📝 **Daily Tasks**\n━━━━━━━━━━━━━━━\n"

    if not results:
        msg += "No tasks for today!"
        return msg

    for page in results:
        props = page.get("properties", {})
        name = props.get("Name", {}).get("title", [{}])[0].get("plain_text", "Unnamed Task")
        priority = props.get("Priority", {}).get("select", {}).get("name", "No Priority")
        msg += f"•  {name} [{priority}]\n"

    return msg


# --- NEW TASK --- #
def add_Task(name, priority, date):

    # --- GENERATE DATE ---
    # Parse the input date string (DD.MM)
    parsed_date = datetime.strptime(date, "%d.%m")
    # Get the current year
    current_year = datetime.now().year
    # Replace the year in the parsed date with the current year and format it as YYYY-MM-DD
    format_date = parsed_date.replace(year=current_year).strftime("%Y-%m-%d")

    data = {
        "parent": {"database_id": TASK_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": name}}]},
            "Date": {"date": {"start": format_date}},
            "Priority": {"select": {"name": priority}}
        }
    }
    response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)

    # --- DEBUGGING ---
    if response.status_code != 200:
        print(f"Error: {response.status_code}")
        print(response.json())
    return response.status_code == 200


def check_Task(task_name):
    # 1. Find the task page ID
    url = f"https://api.notion.com/v1/databases/{TASK_ID}/query"
    query_data = {
        "filter": {
            "property": "Name",
            "title": {"contains": task_name.strip()}
        }
    }
    response = requests.post(url, headers=headers, json=query_data)

    if response.status_code != 200:
        print(f"Error querying Notion for task: {response.status_code}")
        return False, None

    results = response.json().get("results")

    if not results:
        print(f"No task found with name: {task_name}")
        return False, None

    page_id = results[0]["id"] # Retrieve the first matching result

    # 2. Update the checkbox property for the found page
    update_url = f"https://api.notion.com/v1/pages/{page_id}"
    update_data = {"properties": { " ": { "checkbox": True }}}

    update_response = requests.patch(update_url, headers=headers, json=update_data)

    if update_response.status_code != 200:
        print(f"Error updating task checkbox: {update_response.status_code}")
        print(update_response.json())
        return False, page_id

    return True, page_id


# --- NEW READED BOOK --- #
def add_New_Book(name, author, genre, file_id=None):
    """Create a new book entry. Returns page_id on success, None on failure."""
    properties = {
        "Name":   {"title": [{"text": {"content": name}}]},
        "Author": {"rich_text": [{"text": {"content": author}}]},
        "Genre":  {"multi_select": [{"name": genre}]},
        "Area":   {"relation": [{"id": LITERATURE_ID}]},
    }
    if file_id:
        properties["PDF_ID"] = {"rich_text": [{"text": {"content": file_id}}]}

    data = {"parent": {"database_id": LETTI_ID}, "properties": properties}
    response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)

    if response.status_code != 200:
        print(f"Errore: {response.status_code}")
        print(response.json())
        return None

    return response.json()["id"]


# --- NEW QUOTE FUNCTION ---
def find_Book_Page(book_name):
    """Search LETTI database for a book by name.
    Returns (page_id, pdf_file_id) — pdf_file_id is None if no PDF is attached."""
    url = f"https://api.notion.com/v1/databases/{LETTI_ID}/query"
    query_data = {
        "filter": {"property": "Name", "title": {"contains": book_name.strip()}}
    }
    response = requests.post(url, headers=headers, json=query_data)

    if response.status_code != 200:
        print(f"Errore query Notion: {response.status_code}")
        return None, None

    results = response.json().get("results")
    if not results:
        return None, None

    page = results[0]
    page_id = page["id"]
    pdf_props = page.get("properties", {}).get("PDF_ID", {}).get("rich_text", [])
    pdf_file_id = pdf_props[0]["plain_text"] if pdf_props else None
    return page_id, pdf_file_id


def update_book_pdf(page_id, file_id):
    """Store a Telegram file_id in the book's PDF_ID property.
    Auto-creates the property in the LETTI database schema if it doesn't exist yet."""
    prop_data = {"PDF_ID": {"rich_text": [{"text": {"content": file_id}}]}}

    resp = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
        json={"properties": prop_data},
    )

    if resp.status_code == 400:
        # Property likely missing from schema — create it, then retry
        db_resp = requests.patch(
            f"https://api.notion.com/v1/databases/{LETTI_ID}",
            headers=headers,
            json={"properties": {"PDF_ID": {"rich_text": {}}}},
        )
        if db_resp.status_code == 200:
            resp = requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={"properties": prop_data},
            )

    return resp.status_code == 200


def extract_quote_from_pdf(pdf_bytes: bytes, begin_text: str, end_text: str):
    """Extract the text between begin_text and end_text from a PDF.

    Handles multi-page documents and normalises whitespace for robust matching.
    Returns (extracted_quote: str, error: str | None).
    """
    import PyPDF2

    def _norm(text):
        """Collapse all whitespace to single spaces for fuzzy matching."""
        return re.sub(r"\s+", " ", text).strip()

    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            return None, "PDF appears to be empty."

        # Build one normalised string covering the entire book
        raw_pages = [page.extract_text() or "" for page in reader.pages]
        full_text = _norm("\n".join(raw_pages))

        norm_begin = _norm(begin_text)
        norm_end   = _norm(end_text)

        if not norm_begin or not norm_end:
            return None, "Begin or End text is empty."

        # Locate begin marker
        begin_pos = full_text.lower().find(norm_begin.lower())
        if begin_pos == -1:
            return None, (
                f"Begin text not found in PDF.\n"
                f"Searched for: '{begin_text[:80]}...'"
            )

        # Locate end marker (must come after begin)
        search_from = begin_pos + len(norm_begin)
        end_pos = full_text.lower().find(norm_end.lower(), search_from)
        if end_pos == -1:
            return None, (
                f"End text not found after the begin marker.\n"
                f"Searched for: '{end_text[:80]}...'"
            )

        # Extract inclusive of both markers
        quote = full_text[begin_pos : end_pos + len(norm_end)]
        return quote.strip(), None

    except PyPDF2.errors.PdfReadError as e:
        return None, f"Could not read PDF: {e}"
    except Exception as e:
        return None, f"PDF extraction error: {e}" 

def add_Quote(page_id, quote_title, quote_text):
    # --- ADD A QUOTE BLOCK IN THE BOOK'S PAGE --- #
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    data = {
        "children": [
            {
                "object": "block",
                "type": "heading_1",
                "heading_1":{
                    "rich_text": [{"type": "text", "text": {"content": quote_title}}],
                    "color": "green"
                }
            },
            {
                "object": "block",
                "type": "quote",
                "quote": {
                    "rich_text": [{"type": "text", "text": {"content": quote_text}}]
                }
            }
        ]
    }
    response = requests.patch(url, headers=headers, json=data)
    return response.status_code == 200


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

    response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)

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
    response = requests.post(url, headers=headers, json=query_data)

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
    update_response = requests.patch(update_url, headers=headers, json=update_data)

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
    response = requests.post(url, headers=headers, json=query_data)

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
    update_response = requests.patch(update_url, headers=headers, json={"archived": True})

    if update_response.status_code != 200:
        print(f"Error archiving expense: {update_response.status_code}")
        print(update_response.json())
        return False, page_id

    return True, page_id


# --- SCHEDULED JOB: SEND DAILY TASKS --- #
async def send_daily_tasks(context: ContextTypes.DEFAULT_TYPE):
    result_text = task_List()
    if result_text:
        await context.bot.send_message(chat_id=CHAT_ID, text=result_text, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Could not fetch tasks from Notion.")


# --- SCHEDULED JOB: SEND WEEKLY BUDGET RECAP --- #
async def send_weekly_budget(context: ContextTypes.DEFAULT_TYPE):
    result_text = budget()
    if result_text:
        await context.bot.send_message(chat_id=CHAT_ID, text=result_text, parse_mode='Markdown')
    else:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Could not fetch budget from Notion.")


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
            "📝 *ADD TASK*\n"
            "`Add t [Name] - [Priority] - [Date]`\n"
            "_Priority: l · m · h — Date: DD.MM or t for today_\n\n"
            "📋 *TASK LIST* — `T`\n"
            "✅ *CHECK TASK* — `C [Task Name]`\n\n"
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

    # --- REGEX FOR DAILY TASKS: Look for "T"
    if re.fullmatch(r"(?i)T", user_text):
        result_text = task_List()
        if result_text:
            await update.message.reply_text(result_text, parse_mode='Markdown')
        else:
            await update.message.reply_text("❌ Error: Could not see your tasks.")
        return

    # --- REGEX FOR NEW TASK: Look for "Add t [Name] - [Priority] - [Date]"
    PRIORITY_MAP = {"l": "Low", "m": "Mid", "h": "High"}

    task_pattern = r"(?i)add t (.+?) - (.+?) - (.+)"
    task_match = re.search(task_pattern, user_text)

    if task_match:
        name = task_match.group(1).strip()
        priority_input = task_match.group(2).strip()
        date_Input = task_match.group(3).strip()

        if date_Input == "t":
            date = datetime.now().strftime("%d.%m")
        else:
            date = date_Input

        priority = PRIORITY_MAP.get(priority_input.lower())

        if priority is None:
            await update.message.reply_text("❌ Error: Invalid priority. Please use 'l' (Low), 'm' (Medium), or 'h' (High).")
            return

        await update.message.reply_text(f"⏳ Adding '{name}' '{priority}' '{date}' to Notion Calendar...")

        # CALL THE NOTION FUNCTION
        success = add_Task(name, priority, date)

        if success:
            await update.message.reply_text(f"✅ Success! Task added to your database.")
        else:
            await update.message.reply_text("❌ Error: Could not connect to Notion. Check your API keys.")
        return

    # --- REGEX FOR CHECK TASK: Look for "C [Task Name]"
    check_task_match = re.fullmatch(r"(?i)C (.+)", user_text)
    if check_task_match:
        task_name = check_task_match.group(1).strip()

        await update.message.reply_text(f"⏳ Checking task '{task_name}' in Notion...")

        success, page_id = check_Task(task_name)

        if success:
            await update.message.reply_text(f"✅ Success! Task '{task_name}' marked as complete.")
        else:
            if page_id is None:
                await update.message.reply_text(f"❌ Error: Task '{task_name}' not found.")
            else:
                await update.message.reply_text(f"❌ Error: Could not update task '{task_name}'. Check your API keys or Notion permissions.")
        return

    # --- GENRE SHORTCUT MAP ---
    GENRE_MAP = {"s": "Satira", "h": "History", "m": "Manga", "p": "Poetry", "a": "Adventure", "ph": "Philosophy"}

    # --- REGEX FOR NEW BOOK: Look for "Add b [Book's Name] - [Author] - [Genre]"
    book_pattern = r"(?i)add b (.+?) - (.+?) - (.+)"
    book_match = re.search(book_pattern, user_text)

    if book_match:
        book_name = book_match.group(1).strip()
        author = book_match.group(2).strip()
        genre_input = book_match.group(3)

        genre = GENRE_MAP.get(genre_input.lower())

        if genre is None: # Added check for invalid genre
            await update.message.reply_text("❌ Error: Invalid genre. Please use 's', 'h', 'm', 'p', 'a', or 'ph'.")
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
        page_id, pdf_file_id = find_Book_Page(book_name)

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

    # --- CATEGORY SHORTCUT MAP ---
    CATEGORY_MAP = {"s": "Shopping", "f": "Food", "g": "Gift", "o": "Other"}

    # --- REGEX FOR UPDATE EXPENSE: Look for "U e [Name] [Amount] [Category]"
    update_expense_match = re.fullmatch(r"(?i)U e (.+?) (\d+\.?\d*)(?:\s+(\w+))?", user_text)
    if update_expense_match:
        name = update_expense_match.group(1).strip()
        amount = float(update_expense_match.group(2))
        category_input = update_expense_match.group(3)
        category = CATEGORY_MAP.get(category_input.lower() if category_input else "", "Food")

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
        category = CATEGORY_MAP.get(category_input.lower() if category_input else "", "Food")

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
        page_id, _ = find_Book_Page(book_name)
        if not page_id:
            await update.message.reply_text(f"⚠️ \'{book_name}\' not found in library.")
            return

        # Download PDF directly from the Telegram message
        await update.message.reply_text("📄 Reading PDF and extracting quote…")
        try:
            tg_file   = await context.bot.get_file(doc.file_id)
            pdf_bytes = bytes(await tg_file.download_as_bytearray())
        except Exception as e:
            await update.message.reply_text(f"❌ Could not download PDF: {e}")
            return

        # Extract quote
        quote_content, err = extract_quote_from_pdf(pdf_bytes, begin_text, end_text)
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
        job_queue.run_daily(send_daily_tasks, time=time(hour=7, minute=0, tzinfo=milan_tz))               # 7:00 AM Milan
        job_queue.run_daily(send_daily_tasks, time=time(hour=14, minute=20, tzinfo=milan_tz))             # 14:20 PM Milan
        job_queue.run_daily(send_weekly_budget,time=time(hour=9, minute=30, tzinfo=milan_tz),days=(5, 6)) # 0=Monday ... 6=Sunday
        print("✅ Scheduled jobs registered.")
    except Exception as e:
        print(f"⚠️ Scheduled jobs not available: {e}")
        print("Bot will still work normally — scheduling runs fine on Railway.")

    # LISTEN FOR ANY TEXT MESSAGE... (except commands)
    text_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(text_handler)
           
    doc_handler = MessageHandler(filters.Document.ALL, handle_document)
    application.add_handler(doc_handler)

    print("🤖 David online!")
    application.run_polling()
