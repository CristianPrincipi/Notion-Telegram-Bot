"""Blocking I/O must never run on the event loop.

THE BUG THIS LOCKS DOWN
-----------------------
python-telegram-bot processes updates sequentially on a single event loop. Every
Notion, Anthropic and PyPDF2 call in David is synchronous, so calling one
directly from an `async def` stops the ENTIRE bot for its duration — no other
command answered, no scheduled job fired. A `Learn video` on a long transcript
sits in a 300-second Anthropic read; David was dead for those five minutes.

The fix is `asyncio.to_thread`, with `asyncio.wait_for` on the operations that
could otherwise run forever. Both halves are asserted here:

  1. offloading   — the blocking function is handed to asyncio.to_thread
  2. bounding     — a long operation times out and says so, instead of hanging
  3. the point    — other traffic really does keep moving during a slow call

Why identity, not names: the tests replace each blocking function with a stub, so
recording the function OBJECT passed to to_thread and comparing it to the stub
proves the handler offloaded the thing it actually calls. A name check would pass
against a to_thread call on some other function entirely.
"""

import asyncio
import inspect
import threading
import time

import pytest
import responses

import david
from services import implement, implement_diet, learn
import month
import page_lock
import pkm
import proactive.scheduler as scheduler
import reminder
import bot.commands
import bot.implement
import bot.learn
from services import books, expenses
from conftest import FakeContext, FakeDocument, FakeUpdate, run, with_update


# ─── THE RECORDER ──────────────────────────────────────────────────────────────

@pytest.fixture
def offloaded(monkeypatch):
    """Record every function handed to asyncio.to_thread, and still run it.

    Patched on the asyncio module rather than per call site: every call site
    writes `asyncio.to_thread(...)`, so one patch covers all four modules and a
    new call site is picked up for free.
    """
    calls = []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording_to_thread)
    return calls


def stub_module(monkeypatch, module, stubs: dict):
    """Install sync doubles on `module` and hand back the dict for identity checks.

    The doubles stay SYNCHRONOUS on purpose. An async double would satisfy these
    tests even with the to_thread wrapping removed, which is the exact regression
    they exist to catch.
    """
    for name, fn in stubs.items():
        monkeypatch.setattr(module, name, fn)
    return stubs


def expect(stubs: dict, *names):
    """The function objects, in order, that a handler should have offloaded."""
    return [stubs[name] for name in names]


# ─── 1. DAVID.PY ───────────────────────────────────────────────────────────────

@pytest.fixture
def david_stubs(monkeypatch):
    """The blocking calls one command makes, wherever they now live.

    `budget` is still reached through david's namespace (the `B` command calls
    it directly); everything else moved to services/, so the stub goes on the
    service — which is also the module the handler resolves the name through at
    call time.
    """
    # ONE object installed in BOTH importers. `B` calls it through
    # bot.commands, the Sunday recap through david, and the assertions below
    # compare function identity — so a second lambda would make one of the two
    # tests fail against a stub that is correct.
    budget_stub = lambda: "MOCK BUDGET"          # noqa: E731
    stubs = stub_module(monkeypatch, david, {"budget": budget_stub})
    stub_module(monkeypatch, bot.commands, {"budget": budget_stub})
    stubs |= stub_module(monkeypatch, books, {
        "add_New_Book":   lambda name, author, genre: "book-page-id",
        "find_Book_Page": lambda book_name: "book-page-id",
        "add_Quote":      lambda page_id, quote_title, quote_text: True,
    })
    stubs |= stub_module(monkeypatch, expenses, {
        "add_Expenses":   lambda name, amount, category: True,
        # The destructive pair is find-then-write, and BOTH halves are blocking
        # Notion calls — the lookup is a database query, not a local decision.
        "find_expense_matches": lambda name: ([{"id": "exp-page-id", "properties": {}}], None),
        "update_Expense": lambda page_id, amount, category: (True, None),
        "delete_Expense": lambda page_id: (True, None),
    })
    return stubs


DAVID_COMMANDS = [
    ("B",                          ("budget",)),
    ("Add b Dune - Herbert - s",   ("add_New_Book",)),
    ("Add e Carrefour 2.20",       ("add_Expenses",)),
    ("U e Carrefour 12.50",        ("find_expense_matches", "update_Expense")),
    ("D e Carrefour",              ("find_expense_matches", "delete_Expense")),
    ("Add q Dune - On Fear - Fear is the mind-killer.",
     ("find_Book_Page", "add_Quote")),
]


@pytest.mark.parametrize("text, expected", DAVID_COMMANDS,
                         ids=[t[:38] for t, _ in DAVID_COMMANDS])
def test_every_david_command_runs_notion_off_the_loop(offloaded, david_stubs, text, expected):
    run(david.handle_message(FakeUpdate(text=text), FakeContext()))

    assert offloaded == expect(david_stubs, *expected), (
        f"{text!r} called Notion on the event loop instead of via to_thread")


def test_the_scheduled_budget_recap_runs_off_the_loop(offloaded, david_stubs):
    """A blocking scheduled job freezes the bot exactly like a blocking command."""
    context = FakeContext()

    run(david.send_budget_recap(context))

    assert offloaded == expect(david_stubs, "budget")
    assert context.bot.sent, "the recap was never sent"


@responses.activate
def test_the_pdf_quote_upload_runs_every_step_off_the_loop(offloaded, david_stubs, monkeypatch):
    """The upload path: download, parse and both Notion calls all get offloaded."""
    responses.add(responses.GET, "https://api.telegram.org/file/bot-token/doc.pdf",
                  body=b"%PDF-1.4 fake", status=200)
    monkeypatch.setattr(books, "extract_quote_from_pdf",
                        lambda pdf, begin, end: ("the quote", None))
    update = FakeUpdate(
        caption="Add q Dune - On Fear - Fear is / the mind-killer",
        document=FakeDocument())

    run(david.handle_document(update, FakeContext()))

    assert david_stubs["find_Book_Page"] in offloaded
    assert books.extract_quote_from_pdf in offloaded
    assert david_stubs["add_Quote"] in offloaded
    assert update.message.replied_with("Quote added")


# ─── 2. LEARN.PY ───────────────────────────────────────────────────────────────

@pytest.fixture
def learn_stubs(monkeypatch):
    return stub_module(monkeypatch, learn, {
        "extract_youtube":       lambda url: ("transcript", None),
        "extract_article":       lambda url: ({"title": "T", "author": "A", "text": "body"}, None),
        "extract_pdf":           lambda file_bytes: ("pdf text", None),
        "summarize_with_claude": lambda ctype, text, title="", source="": (
            {"title": "Summary", "tldr": "the gist", "sections": [], "key_takeaways": []}, None),
        "create_learn_page":     lambda ctype, title, blocks, metadata={}: (True, "page-1"),
    })


LEARN_COMMANDS = [
    ("Learn video https://youtu.be/abc",
     ("extract_youtube", "summarize_with_claude", "create_learn_page")),
    ("Learn article https://example.com/post",
     ("extract_article", "summarize_with_claude", "create_learn_page")),
    ("Learn book Sapiens",
     ("summarize_with_claude", "create_learn_page")),
]


@pytest.mark.parametrize("text, expected", LEARN_COMMANDS,
                         ids=[t[:38] for t, _ in LEARN_COMMANDS])
def test_learn_runs_fetch_claude_and_notion_off_the_loop(offloaded, learn_stubs, text, expected):
    update = FakeUpdate(text=text)

    run(learn.run_learn(text, **with_update(update)))

    assert offloaded == expect(learn_stubs, *expected)
    assert update.message.replied_with("Saved to Notion")


def test_learn_pdf_parses_off_the_loop(offloaded, learn_stubs):
    """PyPDF2 is CPU-bound, which pins the loop just as hard as a network call."""
    update = FakeUpdate(text="Learn pdf")

    run(learn.run_learn("Learn pdf", file_bytes=b"%PDF-1.4 fake", **with_update(update)))

    assert offloaded == expect(learn_stubs, "extract_pdf",
                               "summarize_with_claude", "create_learn_page")


# ─── 3. IMPLEMENT.PY ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_locks():
    """page_lock keeps module-level state; a leaked lock would poison later tests."""
    page_lock._locks.clear()
    yield
    page_lock._locks.clear()


def _fake_section(path="Perfect Process"):
    """A real implement.Section, so _sectioned_run can read .path/.style/.text."""
    section = implement.Section(path, 2, "heading-1")
    section.content_ids = ["old-1"]
    section.content_blocks = [{"id": "old-1", "type": "numbered_list_item",
                               "numbered_list_item": {"rich_text": [{"plain_text": "step"}]}}]
    return section


@pytest.fixture
def implement_stubs(monkeypatch):
    monkeypatch.setattr(implement, "get_area_db_id", lambda area: "area-db-1")
    monkeypatch.setattr(implement, "blocks_to_text", lambda blocks: "content")
    return stub_module(monkeypatch, implement, {
        "search_page_in_db":     lambda db, name, exact=False: ({"id": "page-1", "properties": {}}, None),
        "get_children":          lambda page_id: ([{"id": "old-1"}], None),
        "read_manual_sections":  lambda page_id: ([_fake_section()], None),
        "route_sections":        lambda paths, text, title: (
            {"affected": [{"path": "Perfect Process"}], "new_steps": []}, None),
        "merge_sections":        lambda targets, text, title: (
            {"updates": [{"path": "Perfect Process", "lines": ["merged"]}]}, None),
        "apply_section_updates": lambda page_id, updates, sections, new_paths=None: (1, []),
        "update_page":           lambda page_id, props: (True, None),
    })


def test_implement_runs_every_notion_and_claude_call_off_the_loop(offloaded, implement_stubs):
    """The full route-merge-write cycle, in order, all on worker threads.

    Asserted as an exact sequence rather than a membership check: a single call
    left on the event loop still freezes the bot, so "most of them are offloaded"
    is not a passing grade.
    """
    update = FakeUpdate(text="Implement Memory Techniques - Brain")

    run(implement.run_implement(update.message.text, **with_update(update)))

    assert offloaded == expect(
        implement_stubs,
        "search_page_in_db",       # find the Learn source page
        "get_children",            # read its content
        "search_page_in_db",       # find the area's Manual
        "read_manual_sections",    # index it by heading
        "route_sections",          # cheap: section names only
        "merge_sections",          # the slow one, affected sections only
        "apply_section_updates",   # append-then-delete, per section
        "update_page",             # tick 'Implemented'
    )
    assert update.message.replied_with("Manual updated")


# ─── 4. IMPLEMENT_DIET.PY ──────────────────────────────────────────────────────

@pytest.fixture
def diet_stubs(monkeypatch):
    return stub_module(monkeypatch, implement_diet, {
        "search_page_in_db":       lambda db, name, exact=False: ({"id": "summary-1",
                                                                   "properties": {}}, None),
        "get_children":            lambda block_id: ([{"type": "paragraph", "paragraph": {
                                       "rich_text": [{"plain_text": "summary text"}]}}], None),
        "find_or_create_diet_page": lambda: ("diet-page-1", False, None),
        "read_diet_structure":     lambda page_id: ({"Goals": {"Fat Loss": {}}},
                                                   {"Goals>Fat Loss": "block-1"}, None),
        "route_sections":          lambda paths, text, title: (
            {"affected": [{"path": "Goals>Fat Loss"}]}, None),
        "read_section_contents":   lambda sections: ({"Goals>Fat Loss": "current"}, None),
        "merge_sections":          lambda contents, text, title: (
            {"updates": [{"path": "Goals>Fat Loss", "mode": "merge", "bullets": ["x"]}]}, None),
        "update_page":             lambda page_id, props: (True, None),
        "apply_updates":           lambda updates, block_map: (1, []),
    })


def test_implement_diet_runs_every_call_off_the_loop(offloaded, diet_stubs):
    update = FakeUpdate(text="Implement Protein Basics - Diet")

    run(implement_diet.run_implement_diet("Protein Basics", **with_update(update)))

    assert offloaded == expect(
        diet_stubs,
        "search_page_in_db",        # find the Learn summary
        "get_children",             # read it
        "find_or_create_diet_page",  # build the skeleton on a first run
        "read_diet_structure",      # the taxonomy: levels 1-3, no content
        "route_sections",           # cheap: paths only
        "read_section_contents",    # level-4 reads, affected sections only
        "merge_sections",           # the slow one
        "update_page",              # tick 'Implemented'
        "apply_updates",            # the surgical writes
    )
    assert update.message.replied_with("section(s) modified")


# ─── 4b. THE CALENDAR PATHS ────────────────────────────────────────────────────
# Google Calendar's client is as blocking as Notion's, and the proactive jobs
# fire at fixed times whether or not you are mid-conversation.

def test_remind_calls_google_calendar_off_the_loop(offloaded, monkeypatch):
    stubs = stub_module(monkeypatch, reminder, {
        "find_conflicts": lambda start, end: ([], None),
        "create_event":   lambda name, start, minutes: ("https://cal/link", None),
    })
    update = FakeUpdate(text="Remind Dentist 12.06 - 14.30")

    run(reminder.handle_remind(update, update.message.text))

    assert offloaded == expect(stubs, "find_conflicts", "create_event")
    assert update.message.replied_with("Reminder set")


# ─── 4c. PKM.PY ────────────────────────────────────────────────────────────────

def test_get_walks_the_manual_off_the_loop(offloaded, monkeypatch):
    """build_index issues one Notion request per heading that has children — ~67
    on the Diet toggle tree, sequentially. Run on the event loop, that walk
    freezes every other command and every scheduled job for its whole duration.

    pkm.py shipped calling all three of these directly from the coroutine; it was
    only harmless because nothing imported it.
    """
    stubs = stub_module(monkeypatch, pkm, {
        "search_page_in_db": lambda db, name, exact=False: ({"id": "manual-1"}, None),
        "build_index":       lambda page_id: ([{"level": 2, "title": "Perfect Process",
                                                "norm": "perfect process",
                                                "content": []}], None),
        "_render":           lambda blocks: "rendered body",
    })
    update = FakeUpdate(text="Get Perfect Process - Brain")

    run(pkm.handle_get(update, update.message.text))

    assert offloaded == expect(stubs, "search_page_in_db", "build_index", "_render")
    assert update.message.replied_with("rendered body")


PROACTIVE_JOBS = [
    ("_morning_briefing_job", "build_morning_briefing"),
    ("_evening_briefing_job", "build_evening_briefing"),
    ("_budget_pacing_job",    "build_pacing_warning"),
    ("_month_rollover_job",   "build_rollover_message"),
    ("_heartbeat_job",        "build_heartbeat"),
]


@pytest.mark.parametrize("job, builder", PROACTIVE_JOBS, ids=[j for j, _ in PROACTIVE_JOBS])
def test_proactive_jobs_build_their_text_off_the_loop(offloaded, monkeypatch, job, builder):
    """A job that blocks the loop freezes inbound commands just as hard."""
    stubs = stub_module(monkeypatch, scheduler, {builder: lambda: ("COMPOSED TEXT", None)})
    context = FakeContext()
    context.job = type("Job", (), {"chat_id": "-100123"})()

    run(getattr(scheduler, job)(context))

    assert offloaded == expect(stubs, builder)
    assert context.bot.sent == [("-100123", "COMPOSED TEXT")]


# ─── 5. LONG OPERATIONS ARE BOUNDED ────────────────────────────────────────────
# to_thread alone stops a slow call freezing the bot, but it does not stop the
# COMMAND hanging forever. These assert the wait_for caps, and that the user is
# told rather than left waiting.
#
# Stalls are kept short: a to_thread worker cannot be cancelled, and asyncio.run
# waits for the executor to drain at shutdown, so the stall is added to the
# suite's runtime even though wait_for returned immediately.

STALL = 0.3
CAP   = 0.05


def stalls(*_args, **_kwargs):
    time.sleep(STALL)


def test_a_slow_claude_call_times_out_in_learn(learn_stubs, monkeypatch):
    monkeypatch.setattr(learn, "ANTHROPIC_TIMEOUT", CAP)
    monkeypatch.setattr(learn, "summarize_with_claude", stalls)
    update = FakeUpdate(text="Learn book Sapiens")

    run(learn.run_learn("Learn book Sapiens", **with_update(update)))

    assert update.message.replied_with("gave up")
    assert update.message.replied_with("Nothing was saved")


def test_a_slow_transcript_fetch_times_out(learn_stubs, monkeypatch):
    monkeypatch.setattr(learn, "SOURCE_FETCH_TIMEOUT", CAP)
    monkeypatch.setattr(learn, "extract_youtube", stalls)
    update = FakeUpdate(text="Learn video https://youtu.be/abc")

    run(learn.run_learn("Learn video https://youtu.be/abc", **with_update(update)))

    assert update.message.replied_with("timed out")


def test_a_slow_article_fetch_times_out(learn_stubs, monkeypatch):
    """requests' read timeout restarts on every byte, so a site trickling one
    byte at a time never trips it — this cap is the only whole-operation bound."""
    monkeypatch.setattr(learn, "SOURCE_FETCH_TIMEOUT", CAP)
    monkeypatch.setattr(learn, "extract_article", stalls)
    update = FakeUpdate(text="Learn article https://example.com/post")

    run(learn.run_learn("Learn article https://example.com/post", **with_update(update)))

    assert update.message.replied_with("timed out")


def test_a_slow_pdf_parse_times_out(learn_stubs, monkeypatch):
    monkeypatch.setattr(learn, "PDF_PARSE_TIMEOUT", CAP)
    monkeypatch.setattr(learn, "extract_pdf", stalls)
    update = FakeUpdate(text="Learn pdf")

    run(learn.run_learn("Learn pdf", file_bytes=b"%PDF-1.4 fake", **with_update(update)))

    assert update.message.replied_with("timed out")


def test_a_slow_merge_times_out_and_says_nothing_was_written(implement_stubs, monkeypatch):
    """The timeout fires BEFORE any write, so the Manual is provably untouched."""
    monkeypatch.setattr(implement, "ANTHROPIC_TIMEOUT", CAP)
    monkeypatch.setattr(implement, "merge_sections", stalls)
    written = []
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: written.append(updates))
    update = FakeUpdate(text="Implement Memory Techniques - Brain")

    run(implement.run_implement(update.message.text, **with_update(update)))

    assert update.message.replied_with("gave up")
    assert update.message.replied_with("unchanged")
    assert written == [], "wrote to the Manual despite timing out"


def test_a_slow_diet_analysis_times_out_and_says_nothing_was_written(diet_stubs, monkeypatch):
    monkeypatch.setattr(implement_diet, "ANTHROPIC_TIMEOUT", CAP)
    monkeypatch.setattr(implement_diet, "merge_sections", stalls)
    applied = []
    monkeypatch.setattr(implement_diet, "apply_updates",
                        lambda updates, block_map: applied.append(updates) or (0, []))
    update = FakeUpdate(text="Implement Protein Basics - Diet")

    run(implement_diet.run_implement_diet("Protein Basics", **with_update(update)))

    assert update.message.replied_with("gave up")
    assert update.message.replied_with("unchanged")
    assert applied == [], "wrote to the Diet page despite timing out"


def test_the_merge_timeout_releases_the_page_lock(implement_stubs, monkeypatch):
    """A timeout inside the lock must not wedge the Manual until the next restart."""
    monkeypatch.setattr(implement, "ANTHROPIC_TIMEOUT", CAP)
    monkeypatch.setattr(implement, "merge_sections", stalls)

    async def main():
        update = FakeUpdate(text="Implement Memory Techniques - Brain")
        await implement.run_implement(update.message.text, **with_update(update))
        # Must be free immediately — a short timeout, so a still-held lock fails
        # here rather than hanging the suite.
        async with page_lock.page_lock("area-db-1", timeout=0.01):
            return True

    assert run(main()) is True


# ─── 6. THE POINT OF ALL OF IT ─────────────────────────────────────────────────

def test_a_slow_command_no_longer_freezes_the_bot():
    """The original bug, end to end.

    While one command sits in a slow blocking call, other traffic — another
    command, or a scheduled job — must still get to run. This is what none of the
    assertions above actually prove: they show the plumbing, this shows the
    behaviour.

    Ordering is the assertion, not timing. Offloaded, the ticks all land while
    the blocking call is still asleep. On the event loop, the call runs to
    completion first and every tick lands after it — which is precisely the
    five-minute freeze.
    """
    events = []
    in_flight = threading.Event()

    def slow_budget():
        in_flight.set()
        time.sleep(STALL)
        events.append("blocking-call-returned")
        return "MOCK BUDGET"

    async def other_traffic():
        while not in_flight.is_set():      # don't start before the call is running
            await asyncio.sleep(0.001)
        for i in range(5):
            events.append(f"tick-{i}")
            await asyncio.sleep(0.001)

    async def main():
        update = FakeUpdate(text="B")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bot.commands, "budget", slow_budget)
            await asyncio.gather(
                david.handle_message(update, FakeContext()),
                other_traffic(),
            )
        return update

    update = run(main())

    assert update.message.replied_with("MOCK BUDGET"), "the command itself broke"
    assert events.index("tick-4") < events.index("blocking-call-returned"), (
        f"the event loop was blocked for the whole call — got {events}")


# ─── 7. GUARDS ─────────────────────────────────────────────────────────────────

OFFLOADED_FUNCTIONS = [
    david.budget,
    expenses.add_Expenses, expenses.find_expense_matches,
    expenses.update_Expense, expenses.delete_Expense,
    books.add_New_Book, books.find_Book_Page, books.add_Quote,
    books.extract_quote_from_pdf,
    learn.extract_youtube, learn.extract_article, learn.extract_pdf,
    learn.summarize_with_claude, learn.create_learn_page,
    implement.search_page_in_db, implement.get_children,
    implement.read_manual_sections, implement.route_sections, implement.merge_sections,
    implement.apply_section_updates, implement.build_manual,
    implement.create_manual_page, implement.append_children,
    implement.clear_page_blocks_by_id, implement.update_page,
    implement_diet.read_diet_structure, implement_diet.route_sections,
    implement_diet.read_section_contents, implement_diet.merge_sections,
    implement_diet.apply_updates, implement_diet.find_or_create_diet_page,
    reminder.find_conflicts, reminder.create_event,
    scheduler.build_morning_briefing, scheduler.build_evening_briefing,
    scheduler.build_pacing_warning, scheduler.build_rollover_message,
    scheduler.build_heartbeat,
    month.ensure_current_month_page,
    pkm.build_index, pkm._render,
]


@pytest.mark.parametrize("fn", OFFLOADED_FUNCTIONS, ids=lambda f: f.__name__)
def test_offloaded_functions_stay_synchronous(fn):
    """`asyncio.to_thread` on an `async def` returns the coroutine WITHOUT ever
    awaiting it: the call silently does nothing and the caller gets a coroutine
    object where it expected a result. Converting any of these to async has to
    fail here rather than in production."""
    assert not inspect.iscoroutinefunction(fn), (
        f"{fn.__name__} is now async but is still called via asyncio.to_thread")


def test_updates_are_still_processed_sequentially():
    """CONCURRENCY IS DELIBERATELY OFF — do not "fix" this test by enabling it.

    Moving blocking work onto threads frees the event loop; it does NOT make it
    safe to process two updates at once. Sequential processing is currently the
    only thing serialising the read-modify-write cycles that page_lock.py does
    not cover. Turning it on before those locks exist AND are verified
    reintroduces the lost-update bug page_lock.py was written to prevent.

    Inspects the call site because the Application is built in `__main__`, which
    is not importable — the same approach test_data_integrity.py uses for the
    weekday constants.
    """
    call_sites = [line.strip() for line in inspect.getsource(david).splitlines()
                  if "concurrent_updates(" in line and not line.strip().startswith("#")]

    assert call_sites == [], f"concurrency was enabled at: {call_sites}"
