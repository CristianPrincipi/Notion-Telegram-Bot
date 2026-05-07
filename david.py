import os
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
        msg += f"• [{priority}] {name}\n"

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
def add_New_Book(name, author, genre):

    data = {
        "parent": {"database_id": LETTI_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": name}}]},
            "Author": {"rich_text": [{"text": {"content": author}}]},
            "Genre": {"multi_select": [{"name": genre}]},
            "Area": {"relation": [{"id": LITERATURE_ID}]}
        }
    }

    response = requests.post("https://api.notion.com/v1/pages", headers=headers, json=data)

    # --- DEBUGGING ---
    if response.status_code != 200:
        print(f"Errore: {response.status_code}")
        print(response.json())

    return response.status_code == 200


# --- NEW QUOTE FUNCTION ---
def find_Book_Page(book_name):
    # --- FIND THE ID PAGE THROUGH BOOK'S NAME
    url = f"https://api.notion.com/v1/databases/{LETTI_ID}/query"

    query_data = {
        "filter": {
            "property": "Name",
            "title": {"contains": book_name.strip()} #Remove blank spaces
        }
    }
    response = requests.post(url, headers=headers, json=query_data)

    if response.status_code != 200:
        print(f"Errore query Notion: {response.status_code}")
        return None

    results = response.json().get("results")

    if results:
        return results[0]["id"] # Retrieve the most equal results
    return None

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
      await update.message.reply_text(f" 📖 BOOK \nAdd b [Book's Name] - [Author] - [Genre] \n\n 🖋️ QUOTE \n add q [Book's Name] - [Title] - [Quote] \n\n 📝 TASK \n Add t [Name] - [Priority] - [Date] \n\n 📋 Tasks list \n  T \n\n 💵 EXPENSE \n Add e [Name] [Amount] [Category] \n\n 💰 Budget \n  B \n\n 🧠 Learn .. \n Learn video https://youtu.be/... \n Learn article https://...\n Learn pdf  [attach PDF]")
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
    PRIORITY_MAP = {"l": "Low", "m": "Medium", "h": "High"}

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
        success = add_New_Book(book_name, author, genre)

        if success:
            await update.message.reply_text(f"✅ Success! Book added to your database.")
        else:
            await update.message.reply_text("❌ Error: Could not connect to Notion. Check your API keys.")
        return

    # --- REGEX FOR QUOTES: Look for "add q [Book's Name] - [Title] - [Quote]"
    quote_pattern = r"(?i)add q (.+?) - (.+?) - ([\s\S]+)"
    quote_match = re.search(quote_pattern, user_text)

    if quote_match:
        book_name = quote_match.group(1).strip()
        quote_title = quote_match.group(2).strip()
        quote_content = quote_match.group(3).strip()

        await update.message.reply_text(f"🔍 Searching '{book_name}' in library...")

        page_id = find_Book_Page(book_name)

        if page_id:
            if add_Quote(page_id, quote_title, quote_content):
                await update.message.reply_text(f"✍️ Quote added to '{book_name}'!")
            else:
                await update.message.reply_text("❌ Error during quote transcription.")
        else:
            await update.message.reply_text(f"⚠️ I didn't find '{book_name}' in the library.")
        return

    # --- CATEGORY SHORTCUT MAP ---
    CATEGORY_MAP = {"s": "Shopping", "f": "Food", "g": "Gift", "o": "Other"}

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
               
    # --- REGEX FOR LEARN COMMAND: "Learn [type] [source]" ---
    if re.match(r"(?i)learn\s+\w+", user_text):
        await handle_learn(update, user_text)
        return


# --- HANDLER FUNCTION FOR PDF ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles file uploads. If caption starts with 'Learn pdf', runs the Learn pipeline."""
    doc     = update.message.document
    caption = (update.message.caption or "").strip()

    if not re.match(r"(?i)learn\s+pdf", caption):
        await update.message.reply_text(
            "📎 File received. To summarise it, send it again with caption: `Learn pdf`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("⏳ Downloading your PDF…")

    tg_file    = await context.bot.get_file(doc.file_id)
    file_bytes = await tg_file.download_as_bytearray()

    await handle_learn(update, caption, file_bytes=bytes(file_bytes))


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
