"""Table-driven router tests — the pre-deploy gate.

WHAT THIS LOCKS DOWN
--------------------
`david.handle_message` is one long if/elif chain of regexes. Which command wins
depends on the ORDER of those checks and on whether each uses `re.match`,
`re.search` or `re.fullmatch`. That is invisible when reading a single branch,
and it is exactly what breaks when a new command is added in the wrong place.

The table below drives the REAL `handle_message` with every Notion/Telegram call
replaced by a spy, then asserts:

  input string  →  which handler(s) fired, in order  →  with which parsed args

Tests run against the shipping code path, not a re-implementation of it, so a
route can never silently drift away from what the bot actually does.

HOW TO ADD A COMMAND
--------------------
1. Add its handler to `SPY_TARGETS` (name in david's namespace, its arg names).
2. Add a happy-path row plus its edge cases to `ROUTES`.
3. If it is reached by `re.search` (unanchored), add a precedence row proving it
   does not steal traffic from a command declared before it.

`known_bug` rows assert TODAY'S behaviour, which is wrong — each says so and why.
Fixing the bug makes that row fail; update the row in the same commit as the fix.
"""

from dataclasses import dataclass, field

import pytest

import david
from conftest import FakeContext, FakeUpdate, run

# ─── SENTINELS ─────────────────────────────────────────────────────────────────

NO_HANDLER   = "(no handler)"      # command answered with a reply only
BOOK_PAGE_ID = "book-page-id"      # what the stubbed find_Book_Page returns
BUDGET_TEXT  = "MOCK BUDGET RECAP"

# Plumbing values that are not parsed out of the user's text, so they are not
# part of what a route test is asserting.
UNINTERESTING_ARGS = {"_update", "page_id"}


# ─── SPY LAYER ─────────────────────────────────────────────────────────────────
# (attribute in david's namespace, positional arg names, return value, is_async)
# Arg names starting with "_" are recorded then dropped — see UNINTERESTING_ARGS.

SPY_TARGETS = [
    ("budget",           (),                                        BUDGET_TEXT,          False),
    ("handle_diag",      ("_update",),                              None,                 True),
    ("handle_dbs",       ("_update",),                              None,                 True),
    ("handle_month",     ("_update",),                              None,                 True),
    ("handle_find",      ("_update", "query"),                      None,                 True),
    ("handle_remind",    ("_update", "user_text"),                  None,                 True),
    ("handle_learn",     ("_update", "user_text", "file_bytes"),    None,                 True),
    ("handle_implement", ("_update", "user_text"),                  None,                 True),
    ("add_New_Book",     ("name", "author", "genre"),               BOOK_PAGE_ID,         False),
    ("find_Book_Page",   ("book_name",),                            BOOK_PAGE_ID,         False),
    ("add_Quote",        ("page_id", "quote_title", "quote_text"),  True,                 False),
    ("add_Expenses",     ("name", "amount", "category"),            True,                 False),
    ("update_Expense",   ("name", "amount", "category"),            (True, "exp-page-id"), False),
    ("delete_Expense",   ("name",),                                 (True, "exp-page-id"), False),
]


class Router:
    """Captures the handler chain a single message triggered."""

    def __init__(self):
        self.calls = []             # [(handler_name, {arg: value}), ...]

    @property
    def chain(self):
        return " → ".join(name for name, _ in self.calls) or NO_HANDLER

    @property
    def parsed_args(self):
        """Union of the parsed args across the chain, plumbing removed.

        A chain like find_Book_Page → add_Quote splits one command's arguments
        across two calls; merging them back gives the full parse of the input.
        """
        merged = {}
        for name, args in self.calls:
            for key, value in args.items():
                if key in UNINTERESTING_ARGS:
                    continue
                assert key not in merged, f"arg {key!r} recorded twice in chain (from {name})"
                merged[key] = value
        return merged


@pytest.fixture
def router(monkeypatch):
    """Replace every side-effecting call in david.py with a recorder."""
    spy = Router()

    for attr, argnames, result, is_async in SPY_TARGETS:
        def make(attr=attr, argnames=argnames, result=result):
            def record(*args, **kwargs):
                parsed = dict(zip(argnames, args))
                parsed.update(kwargs)
                spy.calls.append((attr, parsed))
                return result
            return record

        record = make()
        if is_async:
            def as_async(record=record):
                async def wrapper(*args, **kwargs):
                    return record(*args, **kwargs)
                return wrapper
            monkeypatch.setattr(david, attr, as_async())
        else:
            monkeypatch.setattr(david, attr, record)

    return spy


# ─── THE TABLE ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Route:
    """input_string → expected_handler(s) → expected_parsed_args."""

    text: str
    handler: str
    args: dict = field(default_factory=dict)
    note: str = ""
    known_bug: bool = False

    @property
    def id(self):
        label = self.note or self.handler
        preview = self.text if self.text.strip() else repr(self.text)
        return f"{preview[:44]} → {label}"


def reply(needle):
    """Expectation for routes that only answer with a message."""
    return {"reply_contains": needle}


ROUTES = [
    # ── HELP ───────────────────────────────────────────────────────────────────
    Route("h",     NO_HANDLER, reply("*ADD BOOK*"), "help"),
    Route("help",  NO_HANDLER, reply("*ADD BOOK*"), "help"),
    Route("aiuto", NO_HANDLER, reply("*ADD BOOK*"), "help (italian)"),
    Route("HELP",  NO_HANDLER, reply("*ADD BOOK*"), "help is case-insensitive"),

    # ── BUDGET ─────────────────────────────────────────────────────────────────
    Route("B", "budget", {}, "budget"),
    Route("b", "budget", {}, "budget, lowercase"),

    # ── NOTION ID DIAGNOSTICS ──────────────────────────────────────────────────
    Route("Diag", "handle_diag", {}, "diag"),
    Route("diag", "handle_diag", {}, "diag, lowercase"),
    Route("DBs",  "handle_dbs",  {}, "list databases"),
    Route("dbs",  "handle_dbs",  {}, "list databases, lowercase"),
    Route("Find July",         "handle_find", {"query": "July"},         "find by name"),
    Route("Find Expenses 2025", "handle_find", {"query": "Expenses 2025"}, "find keeps spaces in query"),

    # ── MONTH PAGE ─────────────────────────────────────────────────────────────
    Route("Month", "handle_month", {}, "force the monthly-page rollover"),
    Route("month", "handle_month", {}, "month rollover, lowercase"),
    Route("Month 8", NO_HANDLER, reply("I didn't get that"),
          "Month takes no argument"),

    # ── REMINDER ───────────────────────────────────────────────────────────────
    Route("Remind Dentist 12.06 - 14.30", "handle_remind",
          {"user_text": "Remind Dentist 12.06 - 14.30"}, "reminder"),
    Route("remind Dentist 12.06 - 14.30", "handle_remind",
          {"user_text": "remind Dentist 12.06 - 14.30"}, "reminder, lowercase"),
    # The router only checks for the "Remind " prefix; validating the date/time
    # is handle_remind's job, so a malformed one must still reach it (and get a
    # usage message) rather than falling through to the generic "didn't get that".
    Route("Remind Dentist", "handle_remind", {"user_text": "Remind Dentist"},
          "malformed reminder still routes to the reminder handler"),

    # ── ADD BOOK ───────────────────────────────────────────────────────────────
    Route("Add b Dune - Herbert - s", "add_New_Book",
          {"name": "Dune", "author": "Herbert", "genre": "Satira"}, "add book"),
    Route("Add b Dune - Herbert - ph", "add_New_Book",
          {"name": "Dune", "author": "Herbert", "genre": "Philosophy"}, "two-letter genre code"),
    Route("add b Dune - Herbert - H", "add_New_Book",
          {"name": "Dune", "author": "Herbert", "genre": "History"}, "genre code is case-insensitive"),
    Route("Add b   Dune   -   Frank Herbert   - s", "add_New_Book",
          {"name": "Dune", "author": "Frank Herbert", "genre": "Satira"},
          "name and author are stripped"),
    Route("Add b Dune - Herbert - z", NO_HANDLER, reply("Invalid genre"),
          "unknown genre is rejected before touching Notion"),
    Route("Add b Dune - Herbert - s ", "add_New_Book",
          {"name": "Dune", "author": "Herbert", "genre": "Satira"},
          "trailing space after the genre code is tolerated"),

    # ── ADD QUOTE ──────────────────────────────────────────────────────────────
    Route("Add q Dune - On Fear - Fear is the mind-killer.",
          "find_Book_Page → add_Quote",
          {"book_name": "Dune", "quote_title": "On Fear", "quote_text": "Fear is the mind-killer."},
          "add quote"),
    Route("Add q Dune - On Fear - line one\nline two\nline three",
          "find_Book_Page → add_Quote",
          {"book_name": "Dune", "quote_title": "On Fear",
           "quote_text": "line one\nline two\nline three"},
          "multi-line quote body is preserved"),
    # " / " in the body means "extract from the attached PDF", which only works
    # on a document upload — as plain text it must explain that, not save the
    # markers as if they were the quote.
    Route("Add q Dune - Ch 1 - Fear is / the mind-killer",
          "find_Book_Page", {"book_name": "Dune"},
          "begin / end markers without an attachment explain the PDF flow"),

    # ── LEARN ──────────────────────────────────────────────────────────────────
    Route("Learn video https://youtu.be/abc", "handle_learn",
          {"user_text": "Learn video https://youtu.be/abc"}, "learn video"),
    Route("Learn article https://example.com/post", "handle_learn",
          {"user_text": "Learn article https://example.com/post"}, "learn article"),
    Route("Learn book Sapiens", "handle_learn",
          {"user_text": "Learn book Sapiens"}, "learn book"),
    Route("Learn pdf", "handle_learn", {"user_text": "Learn pdf"},
          "'Learn pdf' as plain text routes to learn (which asks for the file)"),
    Route("Learn", NO_HANDLER, reply("I didn't get that"),
          "'Learn' with no type is not a learn command"),

    # ── IMPLEMENT ──────────────────────────────────────────────────────────────
    Route("Implement Memory Techniques - Brain", "handle_implement",
          {"user_text": "Implement Memory Techniques - Brain"}, "implement"),
    Route("Implement Protein Basics - Diet", "handle_implement",
          {"user_text": "Implement Protein Basics - Diet"}, "implement into Diet"),
    Route("Implement Memory Techniques", NO_HANDLER, reply("I didn't get that"),
          "implement without a target area is not a command"),

    # ── UPDATE EXPENSE ─────────────────────────────────────────────────────────
    Route("U e Carrefour 12.50 s", "update_Expense",
          {"name": "Carrefour", "amount": 12.50, "category": "Shopping"}, "update expense"),
    Route("U e Carrefour 12.50", "update_Expense",
          {"name": "Carrefour", "amount": 12.50, "category": "Food"},
          "update expense defaults to Food"),
    Route("u e Carrefour 12.50 G", "update_Expense",
          {"name": "Carrefour", "amount": 12.50, "category": "Gift"},
          "update expense, lowercase command + uppercase category"),
    Route("U e Carrefour 12", "update_Expense",
          {"name": "Carrefour", "amount": 12.0, "category": "Food"},
          "amount without decimals"),
    # `U e` uses re.fullmatch while `Add e` uses re.search, so trailing junk that
    # Add e would silently swallow makes U e fall through entirely.
    Route("U e Carrefour 12.50 s please", NO_HANDLER, reply("I didn't get that"),
          "U e is anchored: trailing words make the whole command miss"),

    # ── DELETE EXPENSE ─────────────────────────────────────────────────────────
    Route("D e Carrefour", "delete_Expense", {"name": "Carrefour"}, "delete expense"),
    Route("d e Carrefour", "delete_Expense", {"name": "Carrefour"}, "delete expense, lowercase"),
    # `D e (.+)` is greedy and unvalidated, so anything after the name is taken
    # as part of the name — and then simply won't be found in Notion.
    Route("D e Carrefour 5", "delete_Expense", {"name": "Carrefour 5"},
          "trailing text is swallowed into the expense name"),

    # ── ADD EXPENSE ────────────────────────────────────────────────────────────
    Route("Add e Carrefour 2.20 f", "add_Expenses",
          {"name": "Carrefour", "amount": 2.20, "category": "Food"}, "add expense"),
    Route("Add e Carrefour 2.20", "add_Expenses",
          {"name": "Carrefour", "amount": 2.20, "category": "Food"},
          "add expense defaults to Food"),
    Route("Add e Beer 5 s", "add_Expenses",
          {"name": "Beer", "amount": 5.0, "category": "Shopping"}, "category shortcut s"),
    Route("Add e Gift 20 G", "add_Expenses",
          {"name": "Gift", "amount": 20.0, "category": "Gift"}, "category code is case-insensitive"),
    Route("Add e c 5", "add_Expenses",
          {"name": "Carrefour", "amount": 5.0, "category": "Food"}, "'c' expands to Carrefour"),
    Route("add e carrefour 2.20 f", "add_Expenses",
          {"name": "carrefour", "amount": 2.20, "category": "Food"},
          "command is case-insensitive, name keeps its case"),
    Route("Add e C 5", "add_Expenses",
          {"name": "Carrefour", "amount": 5.0, "category": "Food"},
          "the 'c' shortcut is case-insensitive like the command"),
    Route("Add e Beer 5 zzz", NO_HANDLER, reply("unknown category"),
          "a typo'd category is reported, not silently defaulted"),
    Route("Add e Carrefour 2,20", "add_Expenses",
          {"name": "Carrefour", "amount": 2.20, "category": "Food"},
          "comma decimal parses to the same value as a dot"),
    Route("Add e Free 0", NO_HANDLER, reply("greater than zero"),
          "a zero amount is rejected"),

    # ── PRECEDENCE ─────────────────────────────────────────────────────────────
    # Reminder is checked before the expense commands, so an expense-looking
    # appointment name stays a reminder.
    Route("Remind Add e coffee 5 12.06 - 14.30", "handle_remind",
          {"user_text": "Remind Add e coffee 5 12.06 - 14.30"},
          "precedence: reminder beats expense"),
    # Quote is checked before the expense commands, so a quote may contain one.
    Route("Add q Dune - Note - Add e coffee 5",
          "find_Book_Page → add_Quote",
          {"book_name": "Dune", "quote_title": "Note", "quote_text": "Add e coffee 5"},
          "precedence: quote body may contain another command"),
    # Anchoring keeps `Add b` from hijacking an Implement that merely contains it.
    Route("Implement Add b guide - Notes - Brain", "handle_implement",
          {"user_text": "Implement Add b guide - Notes - Brain"},
          "precedence: 'Add b' inside an Implement no longer hijacks it"),
    Route("Please add e coffee 3 for me", NO_HANDLER, reply("I didn't get that"),
          "'add e' no longer fires mid-sentence"),
    # This row is what tells re.fullmatch apart from re.search for the quote
    # pattern — every other quote row matches the whole string either way.
    Route("note: Add q Dune - On Fear - Fear is the mind-killer.",
          NO_HANDLER, reply("I didn't get that"),
          "'Add q' no longer fires mid-sentence"),

    # ── UNRECOGNISED ───────────────────────────────────────────────────────────
    Route("hello", NO_HANDLER, reply("I didn't get that"), "plain chatter"),
    Route("", NO_HANDLER, reply("I didn't get that"), "empty message"),
    Route("   ", NO_HANDLER, reply("I didn't get that"), "whitespace-only message"),
    Route("Find", NO_HANDLER, reply("I didn't get that"), "'Find' with no query"),
    Route("Add e Carrefour", NO_HANDLER, reply("I didn't get that"), "expense with no amount"),
    Route("Add b Dune - Herbert", NO_HANDLER, reply("I didn't get that"), "book with no genre"),
    # handle_message strips before matching, so phone-keyboard whitespace is fine.
    Route("B ", "budget", {}, "a stray trailing space still runs the command"),
]


# ─── THE TEST ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", ROUTES, ids=[r.id for r in ROUTES])
def test_route(case, router):
    update = FakeUpdate(text=case.text)

    run(david.handle_message(update, FakeContext()))

    assert router.chain == case.handler, (
        f"{case.text!r} routed to {router.chain}, expected {case.handler}"
    )

    if case.handler == NO_HANDLER:
        needle = case.args["reply_contains"]
        assert update.message.replied_with(needle), (
            f"{case.text!r} should have replied containing {needle!r}, "
            f"got {update.message.reply_texts}"
        )
    else:
        assert router.parsed_args == case.args, (
            f"{case.text!r} parsed as {router.parsed_args}, expected {case.args}"
        )


def test_table_covers_every_command():
    """Every handler the router can dispatch to has at least one row."""
    dispatched = {name for route in ROUTES for name in route.handler.split(" → ")}
    expected = {attr for attr, *_ in SPY_TARGETS}
    assert expected - dispatched == set(), f"commands with no route test: {expected - dispatched}"


def test_no_route_is_duplicated():
    """A duplicated input string means one of the two rows is dead weight."""
    seen = [route.text for route in ROUTES]
    assert len(seen) == len(set(seen)), "duplicate input strings in ROUTES"


def test_known_bug_rows_are_documented():
    """A known_bug row without a note is unmaintainable once it starts failing."""
    for route in ROUTES:
        if route.known_bug:
            assert route.note, f"known_bug row {route.text!r} needs a note explaining it"


# ─── FAILURE BRANCHES ──────────────────────────────────────────────────────────
# The table asserts routing. These assert what the user is told when the Notion
# call behind a route fails — the other half of "did this command work?".

def test_quote_reports_a_book_that_is_not_in_the_library(router, monkeypatch):
    monkeypatch.setattr(david, "find_Book_Page", lambda book_name: None)
    update = FakeUpdate(text="Add q Missing - Ch 1 - some text")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("I didn't find 'Missing'")
    assert "add_Quote" not in router.chain


def test_book_reports_a_failed_notion_write(router, monkeypatch):
    monkeypatch.setattr(david, "add_New_Book", lambda name, author, genre: None)
    update = FakeUpdate(text="Add b Dune - Herbert - s")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("Could not connect to Notion")


def test_expense_reports_a_failed_notion_write(router, monkeypatch):
    monkeypatch.setattr(david, "add_Expenses", lambda name, amount, category: False)
    update = FakeUpdate(text="Add e Carrefour 2.20")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("Could not connect to Notion")


def test_update_expense_distinguishes_not_found_from_api_failure(router, monkeypatch):
    monkeypatch.setattr(david, "update_Expense", lambda name, amount, category: (False, None))
    update = FakeUpdate(text="U e Ghost 1.00")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Expense 'Ghost' not found")

    monkeypatch.setattr(david, "update_Expense", lambda name, amount, category: (False, "page-1"))
    update = FakeUpdate(text="U e Ghost 1.00")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Could not update 'Ghost'")


def test_delete_expense_distinguishes_not_found_from_api_failure(router, monkeypatch):
    monkeypatch.setattr(david, "delete_Expense", lambda name: (False, None))
    update = FakeUpdate(text="D e Ghost")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Expense 'Ghost' not found")

    monkeypatch.setattr(david, "delete_Expense", lambda name: (False, "page-1"))
    update = FakeUpdate(text="D e Ghost")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Could not delete 'Ghost'")


def test_budget_reports_a_notion_failure(router, monkeypatch):
    monkeypatch.setattr(david, "budget", lambda: None)
    update = FakeUpdate(text="B")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("Could not calculate budget")


def test_budget_reply_is_the_budget_text(router):
    update = FakeUpdate(text="B")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with(BUDGET_TEXT)
