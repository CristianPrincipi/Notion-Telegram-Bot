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
    "OWNER_ID":        "424242",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
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
    # Set so config.validate() emits no optional-variable warning during the
    # suite — test_config_validate asserts on an empty warning list.
    "LOG_LEVEL":       "INFO",
    # Keep the Google client from ever building a real service.
    "GOOGLE_CREDENTIALS_JSON": "",
}
for _key, _value in FAKE_ENV.items():
    os.environ[_key] = _value

EXPENSES_ID = FAKE_ENV["EXPENSES_ID"]
MONTH_ID    = FAKE_ENV["MONTH_ID"]
LETTI_ID    = FAKE_ENV["LETTI_ID"]
OWNER_ID    = int(FAKE_ENV["OWNER_ID"])
NOTION_BASE = "https://api.notion.com/v1"


# ─── ASYNC HELPER ──────────────────────────────────────────────────────────────

def run(coro):
    """Run a coroutine from a sync test, then drain any detached commands.

    Deliberately plain asyncio instead of pytest-asyncio: the handlers under test
    are self-contained coroutines, so there is nothing to gain from an extra
    plugin whose mode/marker API keeps changing between majors.

    THE DRAIN: the long commands (Learn, Implement, both PDF uploads) are
    dispatched with Application.create_task and no longer awaited by their
    handler, so the handler returns while the real work is still pending. A test
    that only awaited the handler would assert against replies that had not been
    sent yet. Draining here keeps every existing test honest without each one
    having to know which commands are detached.

    Bounded by wait_for on purpose: a detached task that never finishes should
    turn the suite RED, not hang it. A wedged CI run is far worse to diagnose.
    """
    async def _run_then_drain():
        result = await coro
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=10)
        return result

    return asyncio.run(_run_then_drain())


# ─── TELEGRAM DOUBLES ──────────────────────────────────────────────────────────

class FakeDocument:
    """Stands in for telegram.Document on an uploaded file."""

    def __init__(self, file_id="file-123", mime_type="application/pdf",
                 file_name="book.pdf", file_size=1024):
        self.file_id   = file_id
        self.mime_type = mime_type
        self.file_name = file_name
        self.file_size = file_size      # Telegram may omit this — None is valid


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
        self.sent_kwargs  = []      # [kwargs, ...] — parallel to `sent`
        self.get_file_ids = []

    async def get_file(self, file_id):
        self.get_file_ids.append(file_id)
        return self._file

    async def send_message(self, chat_id, text, **kwargs):
        # `sent` keeps its (chat_id, text) shape so the existing equality
        # assertions stay readable; kwargs go alongside it rather than into it.
        # They used to be dropped on the floor, which made parse_mode — the thing
        # that silently broke every error report — untestable.
        self.sent.append((chat_id, text))
        self.sent_kwargs.append(kwargs)

    @property
    def sent_full(self):
        """[(chat_id, text, kwargs), ...] for tests that care about parse_mode."""
        return [(c, t, k) for (c, t), k in zip(self.sent, self.sent_kwargs)]


class FakeApplication:
    """Stands in for telegram.ext.Application, for context.application.create_task.

    Mirrors the real signature, including `update`, so a call site that forgets
    to pass it — and would silently rob the error handler of its context — fails
    here rather than in production.
    """

    def __init__(self):
        self.tasks = []          # [(name, update), ...] in dispatch order

    def create_task(self, coroutine, update=None, name=None):
        self.tasks.append((name, update))
        return asyncio.create_task(coroutine, name=name)

    @property
    def task_names(self):
        return [name for name, _ in self.tasks]


class FakeContext:
    """Stands in for ContextTypes.DEFAULT_TYPE."""

    def __init__(self, file=None):
        self.bot = FakeBot(file=file)
        self.application = FakeApplication()
