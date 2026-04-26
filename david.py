import os
import requests
import json
import logging
import re
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters


# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
NOTION_KEY = os.environ.get("NOTION_KEY")
DATABASE_ID = os.environ.get("DATABASE_ID")
EXPENSES_ID = os.environ.get("EXPENSES_ID")
MONTH_ID = os.environ.get("MONTH_ID")
LETTI_ID = os.environ.get("LETTI_ID")
LITERATURE_ID = os.environ.get("LITERATURE_ID")
TASK_ID = os.environ.get("TASK_ID")

# --- NOTION API ---

headers = {'Authorization': f"Bearer {NOTION_KEY}",
           'Content-Type': 'application/json',
           'Notion-Version': '2022-06-28'}


# --- NOTION FUNCTIONS --- #

# --- NEW TASK --- #
def add_Task(name, priority, date):

    # --- GENERATE DATE ---
    # Parse the input date string (DD.MM)
    parsed_date = datetime.strptime(date, "%d.%m")
    # Get the current year
    current_year = datetime.now().year
    # Replace the year in the parsed date with the current year and format it as YYYY-MM-DD
    format_date = parsed_date.replace(year=current_year).strftime("%Y-%m-%d")

    # Example of what you might add (uncomment and adapt to your task database structure):
    data = {
        "parent": {"database_id": TASK_ID}, # Replace with your tasks database ID
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


# --- TELEGRAM MESSAGE HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    print(f"Received: {user_text}") # So you can see it in Colab logs

    # --- REGEX FOR HELP COMMAND: Look for "h"
    if re.fullmatch(r"(?i)h|help|aiuto", user_text):
      await update.message.reply_text(f"📖 \nAdd b [Book's Name] - [Author] - [Genre] \n\n 🖋️ \n add q [Book's Name] - [Title] - [Quote] \n\n 💵 \n Add e [Name] [Amount] [Category] \n\n 📝 \n Add t [Name] - [Priority] - [Date]")
      return

    # REGEX FOR NEW TASK: Look for "Add t [Name] - [Priority] - [Date]"
    PRIORITY_MAP = {
        "l": "Low",
        "m": "Medium",
        "h": "High"
    }

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

    # --- GENRE SHORTCUT MAP ---
    GENRE_MAP = {
    "s": "Satira",
    "h": "History",
    "m": "Manga",
    "p": "Poetry",
    "a": "Adventure",
    "ph": "Philosophy"
    }

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
    CATEGORY_MAP = {
    "s": "Shopping",
    "f": "Food",
    "g": "Gift",
    "o": "Other"
    }

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

# --- START THE BOT ---
if __name__ == '__main__':
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # LISTEN FOR ANY TEXT MESSAGE... (except commands)
    text_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(text_handler)

    print("🤖 Bot is starting... Go to Telegram and message it!")
    application.run_polling()