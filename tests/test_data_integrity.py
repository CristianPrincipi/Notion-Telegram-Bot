"""Six silent-wrong-result bugs — RED first, then fixed.

Every bug here produces a wrong result with no exception and no warning: a total
that is quietly too low, an amount quietly rounded down, a date quietly a day
off, a job quietly on the wrong day, a category quietly replaced, an entry
quietly duplicated. Nothing in the logs would ever tell you.

Each test asserts the CORRECT behaviour, so on current main it fails. The test
name says what should happen; the docstring says what happens instead today.
"""

import pathlib
import re
from datetime import datetime, timezone

import pytest
import responses
from telegram import Chat, Document, Message, Update
from telegram import User as TelegramUser
from telegram.ext import ApplicationBuilder

import budget as budget_module
from clients import calendar_client
import config
import david
from services import books, expenses
from services import learn
from conftest import EXPENSES_ID, NOTION_BASE, OWNER_ID, FakeContext, FakeUpdate, run, written_ok

QUERY_URL = f"{NOTION_BASE}/databases/{EXPENSES_ID}/query"
PAGES_URL = f"{NOTION_BASE}/pages"


# ─── SHARED DOUBLES ────────────────────────────────────────────────────────────

def expense_row(amount, category="Food", index=0):
    return {
        "id": f"page-{index}",
        "properties": {
            "Amount":   {"number": amount},
            "Category": {"multi_select": [{"name": category}]},
        },
    }


def two_pages_of_expenses(first=100, second=50, each=1.0):
    """Notion caps a query at 100 rows and signals the rest via has_more."""
    responses.add(responses.POST, QUERY_URL, status=200, json={
        "results": [expense_row(each, index=i) for i in range(first)],
        "has_more": True, "next_cursor": "cursor-1"})
    responses.add(responses.POST, QUERY_URL, status=200, json={
        "results": [expense_row(each, index=first + i) for i in range(second)],
        "has_more": False, "next_cursor": None})
    return (first + second) * each


@pytest.fixture
def notion_writes(monkeypatch):
    """Record what would be written to Notion, without writing it."""
    calls = []
    monkeypatch.setattr(expenses, "add_Expenses",
                        lambda name, amount, category:
                        calls.append({"name": name, "amount": amount,
                                      "category": category}) or True)
    monkeypatch.setattr(books, "add_New_Book",
                        lambda name, author, genre:
                        calls.append({"name": name, "author": author,
                                      "genre": genre}) or "book-id")
    # The quote path must be stubbed too, or the mid-sentence `Add q` case below
    # reaches find_Book_Page and makes a REAL request to api.notion.com — which
    # would pass for the wrong reason and break the suite's offline guarantee.
    monkeypatch.setattr(books, "find_Book_Page", lambda book_name: "book-page-id")
    monkeypatch.setattr(books, "add_Quote",
                        lambda page_id, quote_title, quote_text:
                        calls.append({"quote_title": quote_title,
                                      "quote_text": quote_text})
                        or (written_ok(2), None))
    return calls


# ─── BUG 1: PAGINATION ─────────────────────────────────────────────────────────
# Past 100 expenses in a month, every total is silently understated.

@responses.activate
def test_budget_totals_every_page_not_just_the_first():
    """Today: budget() sends one query and ignores has_more, so it reports €100
    of a real €150 — and the more you spend, the more wrong it gets."""
    expected = two_pages_of_expenses()

    text, _ = david.budget()

    assert f"€{expected:.2f}" in text, f"expected €{expected:.2f} in: {text}"


@responses.activate
def test_compute_budget_totals_every_page_not_just_the_first():
    """Same bug in the OTHER budget implementation — the one feeding the morning
    briefing and the pacing warning, both of which now actually run."""
    expected = two_pages_of_expenses()

    assert budget_module.compute_budget()[0]["total"] == expected


@responses.activate
def test_book_lookup_searches_past_the_first_page():
    """find_Book_Page stops at row 100, so a book further down 'does not exist'."""
    responses.add(responses.POST, f"{NOTION_BASE}/databases/test-letti-id/query",
                  status=200, json={"results": [], "has_more": True,
                                    "next_cursor": "cursor-1"})
    responses.add(responses.POST, f"{NOTION_BASE}/databases/test-letti-id/query",
                  status=200, json={"results": [{"id": "found-on-page-2"}],
                                    "has_more": False, "next_cursor": None})

    assert books.find_Book_Page("Dune") == "found-on-page-2"


# ─── BUG 2: COMMA DECIMALS AND UNANCHORED MATCHING ─────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("Add e c 2,20", 2.20),
    ("Add e Carrefour 12,50", 12.50),
    ("Add e Carrefour 12.50", 12.50),
    ("Add e Carrefour 12", 12.0),
])
def test_comma_and_dot_decimals_both_parse(notion_writes, text, expected):
    """Today: ',20' is dropped as unmatched trailing text and the command still
    SUCCEEDS, so €2,20 is recorded as €2.00. Wrong number, no error."""
    run(david.handle_message(FakeUpdate(text=text), FakeContext()))

    assert notion_writes, f"{text!r} recorded nothing at all"
    assert notion_writes[0]["amount"] == expected


@pytest.mark.parametrize("text", ["Add e Free 0", "Add e Refund -5", "Add e Free 0,00"])
def test_non_positive_amounts_are_rejected(notion_writes, text):
    """Today: €0 sails through and creates a meaningless Notion row."""
    update = FakeUpdate(text=text)

    run(david.handle_message(update, FakeContext()))

    assert notion_writes == [], f"{text!r} wrote {notion_writes}"


@pytest.mark.parametrize("text", [
    "Please add e coffee 3 for me",
    "note: Add q Dune - On Fear - Fear is the mind-killer.",
    "I should add b Dune - Herbert - s sometime",
])
def test_commands_do_not_fire_mid_sentence(notion_writes, text):
    """Today: the patterns run through re.search, so merely MENTIONING a command
    inside a sentence executes it and writes to Notion."""
    run(david.handle_message(FakeUpdate(text=text), FakeContext()))

    assert notion_writes == [], f"{text!r} executed and wrote {notion_writes}"


def test_a_stray_trailing_space_still_runs_the_command():
    """Today: `B ` misses re.fullmatch entirely and answers "I didn't get that".
    Phone keyboards add that space constantly."""
    update = FakeUpdate(text="B ")

    run(david.handle_message(update, FakeContext()))

    assert not update.message.replied_with("I didn't get that")


# ─── BUG 3: TIMEZONE ───────────────────────────────────────────────────────────

@pytest.fixture
def midnight_in_rome(monkeypatch):
    """23:30 UTC on 10 June == 01:30 Europe/Rome on 11 June.

    Patches the clock in BOTH modules: calendar_client is where the time now
    comes from, and david only had its own `datetime` before the fix. raising=
    False keeps the david patch from erroring once that import is gone — the
    calendar_client patch is what makes the assertion meaningful either way.
    """
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            utc = cls(2025, 6, 10, 23, 30, tzinfo=timezone.utc)
            return utc.replace(tzinfo=None) if tz is None else utc.astimezone(tz)

    monkeypatch.setattr(david, "datetime", FrozenClock, raising=False)
    monkeypatch.setattr(calendar_client, "datetime", FrozenClock)


@responses.activate
def test_expense_is_dated_in_rome_not_utc(midnight_in_rome, monkeypatch):
    """Today: Railway runs UTC and add_Expenses uses a naive datetime.now(), so
    anything logged after local midnight is filed under YESTERDAY — and at a
    month boundary, into the wrong month's budget entirely."""
    # Pinned so the assertion is about the DATE. The month page is cached in
    # memory only, so an unpinned first call would resolve it from Notion and put
    # a schema request in front of the page create inspected below.
    monkeypatch.setattr(expenses, "current_month_id", lambda: "test-month-id")
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": "new-page"})

    expenses.add_Expenses("Late night kebab", 8.50, "Food")

    body = responses.calls[0].request.body
    sent = body.decode() if isinstance(body, bytes) else body
    assert '"start": "2025-06-11"' in sent, f"dated wrongly: {sent}"


# ─── BUG 4: WEEKDAY MAPPING ────────────────────────────────────────────────────

def test_budget_recap_runs_on_sunday_only():
    """Today: fires Saturday AND Sunday. (It fired Fri+Sat before PR #3 — PTB v20
    renumbered days from monday-sunday to SUNDAY-saturday and the call site kept
    a stale '0=Mon' comment.)"""
    application = ApplicationBuilder().token("123456:test-token").build()
    david.register_jobs(application, "-100123")

    recap = [j for j in application.job_queue.jobs() if "budget" in j.name and "pacing" not in j.name]
    assert len(recap) == 1, f"expected one recap job, got {[j.name for j in recap]}"
    assert str(recap[0].job.trigger.fields[4]) == "sun"


def test_the_recap_job_is_not_called_weekly():
    """It runs on a fixed day, so `send_weekly_budget` is a misleading name."""
    assert hasattr(david, "send_budget_recap")


def _bare_weekday_integers(path):
    """Every `days=(...)` in a file that spells a weekday as a literal integer.

    Returns [(line, arg)]. Textual on purpose: the whole point of the convention
    is what the next reader SEES, and both spellings are the same object at
    runtime.
    """
    source = path.read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r"days=\(([^)]*)\)", source):
        arg = match.group(1)
        if any(token.strip().isdigit() for token in arg.split(",")):
            offenders.append((source[:match.start()].count("\n") + 1, arg))
    return offenders


def test_weekdays_are_named_not_bare_integers():
    """A bare `days=(6, 0)` is exactly how the original bug survived review.

    Inspects the CALL SITE, not just whether the constant exists: SUNDAY == 0, so
    `days=(0,)` schedules identically and no runtime assertion can tell the two
    apart. This is deliberately a convention test — the entire value of the
    constant is that the next reader sees a weekday name instead of an integer
    whose meaning changed between library versions.

    SCANS THE WHOLE REPO, not just david.register_jobs. It used to inspect that
    one function, so when scheduling moved into proactive/scheduler.py the guard
    kept passing over a call site it could not see, and `HEARTBEAT_DAY = 0` shipped
    with a comment naming the day the constant should have. Same blind spot
    test_concurrency.test_every_locking_module_is_actually_checked exists to close
    for page_lock: a guard that only looks where the bug WAS is not a guard.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    sources = [path for directory in ("", "bot", "clients", "services", "proactive")
               for path in sorted((root / directory).glob("*.py"))]

    offenders = [f"{path.name}:{line}: days=({arg})"
                 for path in sources for line, arg in _bare_weekday_integers(path)]

    assert offenders == [], (
        "bare weekday integers — use config.SUNDAY … config.SATURDAY:\n"
        + "\n".join(offenders))

    assert any("days=(" in path.read_text(encoding="utf-8") for path in sources), (
        "no days= argument anywhere — the guard is scanning the wrong files")


def test_the_weekday_guard_can_actually_detect_an_offender(tmp_path):
    """A guard that cannot fail is not a guard — and this one silently could not
    for as long as it inspected a single function."""
    offender = tmp_path / "bad.py"
    offender.write_text("jq.run_daily(job, time=t, days=(0,))\n", encoding="utf-8")

    assert _bare_weekday_integers(offender) == [(1, "0,")]


WEEKDAY_NAMES = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY",
                 "THURSDAY", "FRIDAY", "SATURDAY")


def test_every_constant_that_reaches_days_is_a_named_weekday():
    """The scan above cannot see a bare integer hidden behind a constant.

    `days=(HEARTBEAT_DAY,)` reads as a name at the call site while
    `HEARTBEAT_DAY = 0` sat in config.py — which is how it shipped: the integer
    moved one file away instead of going away, carrying a `# SUNDAY` comment, the
    same kind of comment that made the recap fire on the wrong days for months.

    Follows the names FROM the call sites rather than matching on a `_DAY`
    suffix: BUDGET_PACING_MIN_DAY is a day of the MONTH and has nothing to do
    with this, and a guard that cannot tell those apart gets loosened until it
    stops guarding.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    sources = [path for directory in ("", "bot", "clients", "services", "proactive")
               for path in sorted((root / directory).glob("*.py"))]

    names = set()
    for path in sources:
        for arg in re.findall(r"days=\(([^)]*)\)", path.read_text(encoding="utf-8")):
            names.update(t.strip() for t in arg.split(",") if t.strip().isidentifier())

    assert names, "no names reach days= — is the guard scanning the right files?"

    config_source = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    for name in sorted(names - set(WEEKDAY_NAMES)):
        value = re.search(rf"^{name}\s*=\s*([^#\n]+)", config_source, re.MULTILINE)
        assert value, f"{name} reaches days= but is not defined in config.py"
        assert value.group(1).strip() in WEEKDAY_NAMES, (
            f"config.{name} = {value.group(1).strip()} — use a named weekday, "
            "not a literal")


# ─── BUG 5: PARSING ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Add b Dune - Herbert - s ",
    "Add b Dune - Herbert - s\n",
    "Add b Dune - Herbert -  s  ",
])
def test_genre_survives_surrounding_whitespace(notion_writes, text):
    """Today: group(3) is the only captured group never stripped, so a trailing
    space makes a valid genre 'invalid' — while groups 1 and 2 ARE stripped."""
    run(david.handle_message(FakeUpdate(text=text), FakeContext()))

    assert notion_writes, f"{text!r} was rejected"
    assert notion_writes[0]["genre"] == "Satira"


def test_carrefour_shortcut_is_case_insensitive(notion_writes):
    """Today: `if name == "c"` is case-sensitive inside a case-insensitive
    command, so `Add e C 5` files a expense literally named "C"."""
    run(david.handle_message(FakeUpdate(text="Add e C 5"), FakeContext()))

    assert notion_writes[0]["name"] == "Carrefour"


@pytest.mark.parametrize("text", ["Add e Beer 5 zzz", "U e Beer 5 qq"])
def test_an_unknown_category_is_an_error_not_a_silent_default(notion_writes, text):
    """Today: a typo'd category is indistinguishable from omitting one, so the
    expense is quietly filed under Food. Genre already errors properly."""
    update = FakeUpdate(text=text)

    run(david.handle_message(update, FakeContext()))

    assert notion_writes == [], f"{text!r} wrote {notion_writes} despite a bad category"
    assert update.message.replied_with("ategor")


def test_an_absent_category_still_defaults(notion_writes):
    """The other half: omitting the category must KEEP working."""
    run(david.handle_message(FakeUpdate(text="Add e Carrefour 2.20"), FakeContext()))

    assert notion_writes[0]["category"] == "Food"


# ─── BUG 6: EDITED MESSAGES ────────────────────────────────────────────────────

PDF = Document(file_id="f", file_unique_id="u", mime_type="application/pdf", file_size=10)


def owner_message(text=None, caption=None, document=None):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=OWNER_ID, type=Chat.PRIVATE),
        from_user=TelegramUser(id=OWNER_ID, is_bot=False, first_name="Owner"),
        text=text, caption=caption, document=document,
    )


def edited_update(text=None, caption=None, document=None):
    """An Update carrying edited_message rather than message."""
    return Update(update_id=1,
                  edited_message=owner_message(text, caption, document))


def registered_handler(callback):
    application = ApplicationBuilder().token("123456:test-token").build()
    david.register_handlers(application, OWNER_ID)
    for group in application.handlers.values():
        for handler in group:
            if handler.callback is callback:
                return handler
    raise AssertionError(f"{callback.__name__} not registered")


def registered_text_handler():
    return registered_handler(david.handle_message)


def test_editing_a_message_does_not_re_run_the_command():
    """Today: MessageHandler matches edited_message by default, so correcting a
    typo in an expense runs it AGAIN and creates a second Notion entry."""
    assert not registered_text_handler().check_update(edited_update("Add e Carrefour 2.20"))


def test_editing_a_caption_does_not_re_download_the_pdf():
    """Same bug on the upload path: editing a file's caption would re-download
    the PDF and append the quote to Notion a second time."""
    edit = edited_update(caption="Learn pdf", document=PDF)

    assert not registered_handler(david.handle_document).check_update(edit)


def test_a_new_message_still_reaches_the_handler():
    """Guard against over-correcting bug 6 into blocking normal traffic."""
    fresh = Update(update_id=1, message=owner_message(text="Add e Carrefour 2.20"))

    assert registered_text_handler().check_update(fresh)


def test_a_new_upload_still_reaches_the_document_handler():
    fresh = Update(update_id=1, message=owner_message(caption="Learn pdf", document=PDF))

    assert registered_handler(david.handle_document).check_update(fresh)


# ─── BUG 8: A MUTABLE DEFAULT ARGUMENT ─────────────────────────────────────────

def test_learn_page_metadata_is_not_shared_between_calls(monkeypatch):
    """`metadata: dict = {}` builds ONE dict, at definition time.

    Every call that omitted the argument got that same object — the function's
    own state, dressed as a parameter. Nothing writes to it today, so this is a
    trap rather than a live bug: the first line that ever did would leak one
    Learn page's author onto the next one, and only for the calls that passed no
    metadata, which is not a shape anyone debugs quickly.
    """
    created = []
    monkeypatch.setattr(learn, "create_page",
                        lambda db, props, children=None, icon=None: (created.append(props), "p1")[1])

    ok, _, _ = learn.create_learn_page("book", "First", [], metadata={"author": "Herbert"})
    assert ok
    assert "Author" in created[0]

    # No metadata this time: the author must not survive from the call above.
    ok, _, _ = learn.create_learn_page("book", "Second", [])
    assert ok
    assert "Author" not in created[1]

    assert learn.create_learn_page.__defaults__ == (None,), (
        "metadata is back to a mutable default"
    )
