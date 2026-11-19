"""Kindle highlights → Letti, through services/kindle.py and bot/kindle_http.py.

What each group locks down:

  THE WRITE       a found book gets the quote; a missing one is created with an
                  EMPTY Genre (no guessed genre is ever sent) and then gets it;
                  the EPUB's subtitle does not stop the main title from matching.
  THE QUEUE       every failure tells the Kindle whether to keep the highlight:
                  Notion down → retryable; refused or half-written → not, since a
                  retry of a partial write appends a second copy.
  RETRIES         the same id twice writes once — also when the retry arrives
                  while the first send is still writing.
  THE GATE        no token, wrong token → 401 before anything is read or written.
"""

import asyncio
import json
import time

import pytest
import responses
from aiohttp.test_utils import TestClient, TestServer

from bot import kindle_http
from clients import notion_client
from conftest import LETTI_ID, NOTION_BASE, run, written_half, written_nothing, written_ok
import page_lock
from services import books, kindle

TOKEN = "kindle-secret"


@pytest.fixture(autouse=True)
def fresh_seen_ids():
    """`kindle._saved` lives as long as the process; each test starts empty."""
    kindle._saved.clear()
    yield
    kindle._saved.clear()


@pytest.fixture(autouse=True)
def fresh_locks():
    """page_lock's table outlives each asyncio.run; a lock from an old loop breaks the next."""
    page_lock._locks.clear()
    yield
    page_lock._locks.clear()


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    monkeypatch.setattr(notion_client.time, "sleep", lambda seconds: None)


def quote(**overrides):
    fields = {"id": "q-1", "book": "Psicopolitica", "author": "Byung-Chul Han",
              "title": "Cap. 2 · p. 31", "text": "Oggi non lavoriamo per i nostri bisogni."}
    fields.update(overrides)
    return kindle.KindleQuote(**fields)


class Library:
    """A stand-in for Letti: titles → page ids, plus a record of every write."""

    def __init__(self, titles=(), lookup_error=None, create_ok=True, written=None):
        self.pages = {title: f"page-{i}" for i, title in enumerate(titles)}
        self.lookup_error = lookup_error
        self.create_ok = create_ok
        self.written = written if written is not None else written_ok(2)
        self.searches, self.created, self.quotes = [], [], []

    def find(self, name):
        self.searches.append(name)
        if self.lookup_error:
            return None, self.lookup_error
        for title, page in self.pages.items():
            if name.strip() in title:
                return books.Book(page, title), None
        return None, None

    def create(self, name, author, genre=None):
        time.sleep(0.05)  # widen the find-or-create window for the race test
        self.created.append((name, author, genre))
        if not self.create_ok:
            return None, "Notion 400: body failed validation"
        self.pages[name] = f"page-new-{len(self.created)}"
        return self.pages[name], None

    def add_quote(self, page_id, title, text):
        self.quotes.append((page_id, title, text))
        err = None if self.written.batches_done == self.written.batches_total else "Notion 500"
        return self.written, err

    def install(self, monkeypatch):
        monkeypatch.setattr(books, "find_Book_Page", self.find)
        monkeypatch.setattr(books, "add_New_Book", self.create)
        monkeypatch.setattr(books, "add_Quote", self.add_quote)
        return self


def collect():
    sent = []

    async def notify(text):
        sent.append(text)

    return sent, notify


# ─── THE WRITE ─────────────────────────────────────────────────────────────────

def test_a_quote_lands_on_the_book_it_names(monkeypatch):
    lib = Library(["Psicopolitica"]).install(monkeypatch)
    sent, notify = collect()

    result, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert err is None
    assert result == {"book": "Psicopolitica", "created": False, "duplicate": False}
    assert lib.quotes == [("page-0", "Cap. 2 · p. 31", "Oggi non lavoriamo per i nostri bisogni.")]
    assert lib.created == []
    assert sent and "Psicopolitica" in sent[0]


def test_a_missing_book_is_created_with_no_genre_then_quoted(monkeypatch):
    lib = Library([]).install(monkeypatch)
    sent, notify = collect()

    result, err = run(kindle.run_kindle_quote(quote(book="Il manuale", author="Epitteto"), notify=notify))

    assert err is None and result["created"] is True
    assert lib.created == [("Il manuale", "Epitteto", None)]
    assert lib.quotes[0][0] == "page-new-1"
    assert "no genre" in sent[-1]


@responses.activate
def test_creating_a_book_with_no_genre_sends_no_genre_column():
    """The Notion request itself: an empty Genre is ABSENT, not `[{"name": None}]`."""
    responses.add(responses.POST, f"{NOTION_BASE}/pages", status=200, json={"id": "new-book"})

    page_id, err = books.add_New_Book("Il manuale", "Epitteto", None)

    assert (page_id, err) == ("new-book", None)
    sent = json.loads(responses.calls[0].request.body)
    assert sent["parent"] == {"database_id": LETTI_ID}
    assert "Genre" not in sent["properties"]
    assert sent["properties"]["Author"]["rich_text"][0]["text"]["content"] == "Epitteto"


@responses.activate
def test_add_b_still_sends_the_genre_it_was_given():
    responses.add(responses.POST, f"{NOTION_BASE}/pages", status=200, json={"id": "new-book"})

    books.add_New_Book("Candido", "Voltaire", "Satira")

    sent = json.loads(responses.calls[0].request.body)
    assert sent["properties"]["Genre"] == {"multi_select": [{"name": "Satira"}]}


def test_the_epub_subtitle_does_not_hide_the_book(monkeypatch):
    """Letti has "Psicopolitica"; the EPUB says "Psicopolitica. Il neoliberismo…"."""
    lib = Library(["Psicopolitica"]).install(monkeypatch)
    _, notify = collect()

    _, err = run(kindle.run_kindle_quote(
        quote(book="Psicopolitica. Il neoliberismo e le nuove tecniche del potere"), notify=notify))

    assert err is None
    assert lib.created == [], "a second Psicopolitica row was created"
    assert lib.searches == ["Psicopolitica. Il neoliberismo e le nuove tecniche del potere",
                            "Psicopolitica"]


def test_the_message_and_the_answer_name_the_row_the_quote_went_to(monkeypatch):
    """The notice named the EPUB's title, so a quote filed under a different row
    — the main title, or a longer one containing it — never showed where it went."""
    Library(["Psicopolitica"]).install(monkeypatch)
    sent, notify = collect()

    result, _ = run(kindle.run_kindle_quote(
        quote(book="Psicopolitica. Il neoliberismo e le nuove tecniche del potere"), notify=notify))

    assert result["book"] == "Psicopolitica"
    assert "added to 'Psicopolitica'" in sent[-1], sent
    assert "Il neoliberismo" in sent[-1], "the EPUB's own title should still be named"


def test_a_book_found_under_its_own_title_is_named_once(monkeypatch):
    Library(["Psicopolitica"]).install(monkeypatch)
    sent, notify = collect()

    run(kindle.run_kindle_quote(quote(), notify=notify))

    assert sent[-1] == "📖 Kindle quote added to 'Psicopolitica': Cap. 2 · p. 31"


@pytest.mark.parametrize("title, expected", [
    ("Psicopolitica. Il neoliberismo", ["Psicopolitica. Il neoliberismo", "Psicopolitica"]),
    ("Sapiens: Da animali a dèi", ["Sapiens: Da animali a dèi", "Sapiens"]),
    ("Dune (Ciclo di Dune Vol. 1)", ["Dune (Ciclo di Dune Vol. 1)", "Dune"]),
    ("Il manuale", ["Il manuale"]),
    ("Io: una storia", ["Io: una storia"]),  # "Io" is too short to search alone
    ("Mr. Mercedes", ["Mr. Mercedes"]),       # "Mr" is too short: an abbreviation, not a title
])
def test_title_candidates(title, expected):
    assert kindle.title_candidates(title) == expected


# ─── THE QUEUE ─────────────────────────────────────────────────────────────────

def test_a_failed_lookup_writes_nothing_and_stays_queued(monkeypatch):
    """An outage is not a missing book: reading it as one would create a duplicate."""
    lib = Library(["Psicopolitica"], lookup_error="Notion 503").install(monkeypatch)
    sent, notify = collect()

    result, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert result is None and err.retryable is True
    assert lib.created == [] and lib.quotes == []
    assert "stays queued" in sent[0]


def test_a_failed_create_stays_queued(monkeypatch):
    lib = Library([], create_ok=False).install(monkeypatch)
    sent, notify = collect()

    _, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert err.retryable is True and lib.quotes == []
    assert "Notion 400" in err.message, "Notion's reason was dropped"
    assert "Notion 400" in sent[0]


@responses.activate
def test_a_network_failure_creating_the_book_stays_queued_and_raises_nothing(monkeypatch):
    """add_New_Book let requests' exception escape: the Kindle got a 500, and a
    raw traceback, instead of "keep it queued"."""
    monkeypatch.setattr(books, "find_Book_Page", lambda name: (None, None))
    monkeypatch.setattr(books, "add_Quote", lambda *a: pytest.fail("quoted a book never created"))
    responses.add(responses.POST, f"{NOTION_BASE}/pages",
                  body=notion_client.requests.ConnectionError("connection reset"))
    sent, notify = collect()

    result, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert result is None and err.retryable is True
    assert "connection reset" in sent[0]


def test_a_quote_too_long_for_one_block_is_refused_for_good(monkeypatch):
    lib = Library(["Psicopolitica"]).install(monkeypatch)
    _, notify = collect()

    _, err = run(kindle.run_kindle_quote(quote(text="x" * (books.QUOTE_MAX_CHARS + 1)), notify=notify))

    assert err.retryable is False
    assert lib.searches == [] and lib.quotes == []


@pytest.mark.parametrize("field", ["book", "text"])
def test_an_empty_book_or_text_is_refused_for_good(monkeypatch, field):
    lib = Library(["Psicopolitica"]).install(monkeypatch)
    _, notify = collect()

    _, err = run(kindle.run_kindle_quote(quote(**{field: "   "}), notify=notify))

    assert err.retryable is False and lib.quotes == []


def test_a_partial_write_is_never_retried(monkeypatch):
    """Part of the quote is on the page: a retry would append a second copy."""
    Library(["Psicopolitica"], written=written_half(1, 2, 2)).install(monkeypatch)
    sent, notify = collect()

    _, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert err.retryable is False
    assert "SECOND copy" in sent[0]


def test_a_write_that_landed_nothing_stays_queued(monkeypatch):
    Library(["Psicopolitica"], written=written_nothing()).install(monkeypatch)
    _, notify = collect()

    _, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert err.retryable is True


def test_a_failed_quote_is_not_remembered(monkeypatch):
    """Otherwise the retry that SHOULD write would be answered as a duplicate."""
    lib = Library(["Psicopolitica"], written=written_nothing()).install(monkeypatch)
    _, notify = collect()
    run(kindle.run_kindle_quote(quote(), notify=notify))

    lib.written = written_ok(2)
    result, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert err is None and result["duplicate"] is False
    assert len(lib.quotes) == 2


# ─── RETRIES ───────────────────────────────────────────────────────────────────

def test_the_same_id_twice_writes_once(monkeypatch):
    lib = Library(["Psicopolitica"]).install(monkeypatch)
    sent, notify = collect()

    run(kindle.run_kindle_quote(quote(), notify=notify))
    result, err = run(kindle.run_kindle_quote(quote(), notify=notify))

    assert err is None and result["duplicate"] is True
    assert len(lib.quotes) == 1
    assert len(sent) == 1, "the retry announced a second save"


def test_a_retry_arriving_mid_write_waits_and_writes_nothing(monkeypatch):
    """The id check is INSIDE the lock, so an overlapping retry sees the first save."""
    lib = Library([]).install(monkeypatch)  # creating the book is the slow step
    _, notify = collect()

    async def both():
        return await asyncio.gather(kindle.run_kindle_quote(quote(), notify=notify),
                                    kindle.run_kindle_quote(quote(), notify=notify))

    (first, _), (second, _) = run(both())

    assert len(lib.quotes) == 1
    assert sorted([first["duplicate"], second["duplicate"]]) == [False, True]


def test_two_quotes_from_a_new_book_create_it_once(monkeypatch):
    """THE RACE the lock exists for: both miss the lookup without it."""
    lib = Library([]).install(monkeypatch)
    _, notify = collect()

    async def both():
        return await asyncio.gather(
            kindle.run_kindle_quote(quote(id="a", book="Il manuale"), notify=notify),
            kindle.run_kindle_quote(quote(id="b", book="Il manuale"), notify=notify))

    results = run(both())

    assert all(err is None for _, err in results)
    assert len(lib.created) == 1, f"the book was created {len(lib.created)} times"
    assert len(lib.quotes) == 2


def test_ids_are_forgotten_oldest_first(monkeypatch):
    monkeypatch.setattr(kindle, "KINDLE_SEEN_IDS", 2)
    for quote_id in ("a", "b", "c"):
        kindle._remember(quote_id, {"book": "x"})

    assert list(kindle._saved) == ["b", "c"]


# ─── THE HTTP GATE ─────────────────────────────────────────────────────────────

def call(method, path, *, headers=None, body=None, raw=None):
    """One request against build_app; returns (status, json)."""
    sent, notify = collect()

    async def go():
        app = kindle_http.build_app(token=TOKEN, notify=notify)
        async with TestClient(TestServer(app)) as client:
            kwargs = {"headers": headers or {}}
            if raw is not None:
                kwargs["data"] = raw
            elif body is not None:
                kwargs["json"] = body
            resp = await client.request(method, path, **kwargs)
            return resp.status, await resp.json()

    return run(go())


AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"id": "q-1", "book": "Psicopolitica", "author": "Han", "title": "p. 31", "text": "Una frase."}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"},
                                     {"Authorization": TOKEN}])
def test_no_token_no_write(monkeypatch, headers):
    lib = Library(["Psicopolitica"]).install(monkeypatch)

    status, body = call("POST", "/kindle/quote", headers=headers, body=BODY)

    assert status == 401 and body["ok"] is False
    assert lib.searches == [] and lib.quotes == []


def test_ping_needs_the_token():
    assert call("GET", "/kindle/ping")[0] == 401
    assert call("GET", "/kindle/ping", headers=AUTH) == (200, {"ok": True})


def test_a_saved_quote_answers_200(monkeypatch):
    Library(["Psicopolitica"]).install(monkeypatch)

    status, body = call("POST", "/kindle/quote", headers=AUTH, body=BODY)

    assert status == 200
    assert body == {"ok": True, "book": "Psicopolitica", "created": False, "duplicate": False}


def test_a_retryable_failure_answers_503(monkeypatch):
    Library(["Psicopolitica"], lookup_error="Notion 503").install(monkeypatch)

    status, body = call("POST", "/kindle/quote", headers=AUTH, body=BODY)

    assert status == 503 and body["retry"] is True


def test_a_refusal_answers_422(monkeypatch):
    Library(["Psicopolitica"]).install(monkeypatch)

    status, body = call("POST", "/kindle/quote", headers=AUTH, body={**BODY, "text": ""})

    assert status == 422 and body["retry"] is False


@pytest.mark.parametrize("raw, body", [
    ("not json", None),
    (None, ["a", "list"]),
    (None, {**BODY, "text": 42}),
    (None, {**BODY, "book": "x" * 501}),
])
def test_a_malformed_body_answers_400(monkeypatch, raw, body):
    lib = Library(["Psicopolitica"]).install(monkeypatch)

    status, _ = call("POST", "/kindle/quote", headers=AUTH, body=body, raw=raw)

    assert status == 400 and lib.quotes == []


def test_the_endpoint_stays_off_without_a_token(monkeypatch, caplog):
    monkeypatch.setattr(kindle_http, "KINDLE_TOKEN", "")

    runner = run(kindle_http.start(bot=None, chat_id="x"))

    assert runner is None
    assert "KINDLE_TOKEN is not set" in caplog.text


# ─── REVIEW FINDINGS (PR #69) ──────────────────────────────────────────────────

def test_a_busy_port_does_not_stop_david(monkeypatch, caplog):
    """post_init does not swallow exceptions: a bind failure here would abort the
    whole bot. It must log and carry on without the endpoint."""
    import socket

    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    monkeypatch.setattr(kindle_http, "KINDLE_TOKEN", TOKEN)
    monkeypatch.setattr(kindle_http, "KINDLE_PORT", port)
    try:
        runner = run(kindle_http.start(bot=None, chat_id="x"))
    finally:
        blocker.close()

    assert runner is None
    assert "could not start" in caplog.text


class FailingBot:
    async def send_message(self, chat_id, text):
        raise RuntimeError("Telegram is down")


def test_a_failed_chat_message_keeps_the_partial_write_a_422(monkeypatch):
    """With notify raising, the handler answered 500 — which the Kindle retries,
    appending a second copy of a quote already partly on the page."""
    from bot.notify import for_chat

    lib = Library(["Psicopolitica"], written=written_half(1, 2, 2)).install(monkeypatch)

    async def go():
        app = kindle_http.build_app(token=TOKEN, notify=for_chat(FailingBot(), "chat"))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/kindle/quote", headers=AUTH, json=BODY)
            return resp.status, await resp.json()

    status, body = run(go())

    assert status == 422 and body["retry"] is False
    assert len(lib.quotes) == 1


def test_a_failed_chat_message_does_not_undo_a_save(monkeypatch):
    Library(["Psicopolitica"]).install(monkeypatch)
    from bot.notify import for_chat

    async def go():
        app = kindle_http.build_app(token=TOKEN, notify=for_chat(FailingBot(), "chat"))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/kindle/quote", headers=AUTH, json=BODY)
            return resp.status

    assert run(go()) == 200
