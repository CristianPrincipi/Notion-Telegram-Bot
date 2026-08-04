"""Shared test fixtures for David.

The fake environment is installed at IMPORT time — before any project module is
imported — because david.py, budget.py and notion_client.py all read os.environ
at module scope. Values are forced (not setdefault) so the suite behaves
identically on a laptop that has the real bot credentials exported and on a
clean CI runner.

Nothing here touches the network: `responses` intercepts HTTP in the Notion
tests, and the router tests replace every Notion/Telegram call with a spy.
"""

import asyncio
import os

# ─── FAKE ENVIRONMENT (must run before project imports) ────────────────────────

FAKE_ENV = {
    "TELEGRAM_TOKEN":  "test-telegram-token",
    "NOTION_KEY":      "test-notion-key",
    "DATABASE_ID":     "test-database-id",
    "EXPENSES_ID":     "test-expenses-id",
    "MONTH_ID":        "test-month-id",
    "LETTI_ID":        "test-letti-id",
    "LITERATURE_ID":   "test-literature-id",
    "CHAT_ID":         "test-chat-id",
    "LEARN_ID":        "test-learn-id",
    "DIET_ID":         "test-diet-id",
    "BRAIN_ID":        "test-brain-id",
    "FINANCE_ID":      "test-finance-id",
    "BUDGET_CEILING":  "300",
    # Keep the Google client from ever building a real service.
    "GOOGLE_CREDENTIALS_JSON": "",
}
for _key, _value in FAKE_ENV.items():
    os.environ[_key] = _value

EXPENSES_ID = FAKE_ENV["EXPENSES_ID"]
MONTH_ID    = FAKE_ENV["MONTH_ID"]
LETTI_ID    = FAKE_ENV["LETTI_ID"]
NOTION_BASE = "https://api.notion.com/v1"


# ─── ASYNC HELPER ──────────────────────────────────────────────────────────────

def run(coro):
    """Run a coroutine from a sync test.

    Deliberately plain asyncio instead of pytest-asyncio: the handlers under test
    are self-contained coroutines, so there is nothing to gain from an extra
    plugin whose mode/marker API keeps changing between majors.
    """
    return asyncio.run(coro)


# ─── TELEGRAM DOUBLES ──────────────────────────────────────────────────────────

class FakeDocument:
    """Stands in for telegram.Document on an uploaded file."""

    def __init__(self, file_id="file-123", mime_type="application/pdf", file_name="book.pdf"):
        self.file_id   = file_id
        self.mime_type = mime_type
        self.file_name = file_name


class FakeMessage:
    """Records every reply instead of calling Telegram."""

    def __init__(self, text=None, caption=None, document=None):
        self.text     = text
        self.caption  = caption
        self.document = document
        self.replies  = []          # [(text, kwargs), ...]

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    @property
    def reply_texts(self):
        return [text for text, _ in self.replies]

    def replied_with(self, needle):
        """True if any reply contains `needle`."""
        return any(needle in text for text in self.reply_texts)


class FakeUpdate:
    def __init__(self, text=None, caption=None, document=None):
        self.message = FakeMessage(text=text, caption=caption, document=document)


class FakeFile:
    """Stands in for the object returned by bot.get_file()."""

    def __init__(self, file_path="https://api.telegram.org/file/bot-token/doc.pdf", content=b"%PDF-1.4 fake"):
        self.file_path = file_path
        self._content  = content

    async def download_as_bytearray(self):
        return bytearray(self._content)


class FakeBot:
    def __init__(self, file=None):
        self._file        = file or FakeFile()
        self.sent         = []      # [(chat_id, text), ...]
        self.get_file_ids = []

    async def get_file(self, file_id):
        self.get_file_ids.append(file_id)
        return self._file

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


class FakeContext:
    """Stands in for ContextTypes.DEFAULT_TYPE."""

    def __init__(self, file=None):
        self.bot = FakeBot(file=file)
