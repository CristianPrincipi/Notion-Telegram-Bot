"""Table-driven router tests — the pre-deploy gate.

WHAT THIS LOCKS DOWN
--------------------
`david.handle_message` dispatches by walking `david.COMMANDS` and taking the
first pattern that fullmatches. Which command wins therefore depends on the ORDER
of that list, and what each handler receives depends on the NAMES of the capture
groups in its pattern — neither of which is visible from reading one entry.

The table below drives the REAL `handle_message` with every Notion/Telegram call
replaced by a spy, then asserts:

  input string  →  which handler(s) fired, in order  →  with which parsed args

Tests run against the shipping code path, not a re-implementation of it, so a
route can never silently drift away from what the bot actually does.

The registry tests at the bottom close the loop the table cannot: every command
in `COMMANDS` has a row, no two patterns can claim the same input (which is what
makes the list's order safe to arrange for reading), `destructive` means what it
says, and the generated help describes exactly the commands that exist.

HOW TO ADD A COMMAND
--------------------
1. Add a `Command` to `david.COMMANDS`, in the position you want it read.
2. Add its handler to `SPY_TARGETS` (name in david's namespace, its arg names).
3. Add a happy-path row plus its edge cases to `ROUTES`. The per-command tests
   below fail until at least one row matches the new pattern.

`known_bug` rows assert TODAY'S behaviour, which is wrong — each says so and why.
Fixing the bug makes that row fail; update the row in the same commit as the fix.
"""

import pathlib
import re
from dataclasses import dataclass, field

import pytest

import david
from services import learn
from services import books, expenses
from conftest import FakeContext, FakeUpdate, run

# ─── SENTINELS ─────────────────────────────────────────────────────────────────

NO_HANDLER   = "(no handler)"      # command answered with a reply only
BOOK_PAGE_ID = "book-page-id"      # what the stubbed find_Book_Page returns
BUDGET_TEXT  = "MOCK BUDGET RECAP"

# What the stubbed find_expense_matches returns: EXACTLY ONE match, so the
# destructive commands run straight through to their write. Ambiguity is a
# behaviour, not a route — it is driven end to end in test_expense_safety.py.
ONE_MATCH = [{
    "id": "exp-page-id",
    "properties": {
        "Name":     {"type": "title", "title": [{"plain_text": "Carrefour"}]},
        "Amount":   {"number": 12.5},
        "Date":     {"date": {"start": "2026-08-06"}},
        "Category": {"multi_select": [{"name": "Food"}]},
    },
}]

# Plumbing values that are not parsed out of the user's text, so they are not
# part of what a route test is asserting. `page_id` is one: the destructive
# commands are routed by NAME and the ID is whatever the lookup resolved it to.
UNINTERESTING_ARGS = {"_update", "page_id"}


# ─── SPY LAYER ─────────────────────────────────────────────────────────────────
# (attribute name, positional arg names, return value, is_async)
# Arg names starting with "_" are recorded then dropped — see UNINTERESTING_ARGS.
#
# WHERE EACH ONE IS PATCHED. A spy only works if it is installed on the module
# whose namespace the caller resolves the name through at CALL time. The
# handlers that used to do the work themselves live in services/ now, so the
# book and expense calls are patched there; SPY_HOMES is that mapping, and
# anything absent from it is still reached through david.

SPY_TARGETS = [
    ("budget",           (),                                        BUDGET_TEXT,          False),
    ("handle_diag",      ("_update",),                              None,                 True),
    ("handle_dbs",       ("_update",),                              None,                 True),
    ("handle_month",     ("_update",),                              None,                 True),
    ("handle_find",      ("_update", "query"),                      None,                 True),
    ("handle_remind",    ("_update", "user_text"),                  None,                 True),
    ("handle_learn",     ("_update", "user_text", "file_bytes"),    None,                 True),
    ("handle_implement", ("_update", "user_text"),                  None,                 True),
    ("handle_get",       ("_update", "user_text"),                  None,                 True),
    ("add_New_Book",     ("name", "author", "genre"),               BOOK_PAGE_ID,         False),
    ("find_Book_Page",   ("book_name",),                            BOOK_PAGE_ID,         False),
    ("add_Quote",        ("page_id", "quote_title", "quote_text"),  True,                 False),
    ("add_Expenses",     ("name", "amount", "category"),            True,                 False),
    # The destructive pair is find-then-write, so both halves are spied: the
    # finder carries the parsed NAME, the writer the parsed amount/category.
    ("find_expense_matches", ("name",),                             (ONE_MATCH, None),    False),
    ("update_Expense",   ("page_id", "amount", "category"),         (True, None),         False),
    ("delete_Expense",   ("page_id",),                              (True, None),         False),
]

SPY_HOMES = {
    "add_New_Book":         books,
    "find_Book_Page":       books,
    "add_Quote":            books,
    "add_Expenses":         expenses,
    "find_expense_matches": expenses,
    "update_Expense":       expenses,
    "delete_Expense":       expenses,
}


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
    """Replace every side-effecting call a command makes with a recorder."""
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
        home = SPY_HOMES.get(attr, david)
        if is_async:
            def as_async(record=record):
                async def wrapper(*args, **kwargs):
                    return record(*args, **kwargs)
                return wrapper
            monkeypatch.setattr(home, attr, as_async())
        else:
            monkeypatch.setattr(home, attr, record)

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
    # Supported all along, and missing from the hand-written help for just as
    # long — the generated one lists it because learn.SUPPORTED_TYPES does.
    Route("Learn podcast https://show.com/episode", "handle_learn",
          {"user_text": "Learn podcast https://show.com/episode"}, "learn podcast"),
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

    # ── GET (PKM retrieval) ────────────────────────────────────────────────────
    Route("Get Perfect Process - Brain", "handle_get",
          {"user_text": "Get Perfect Process - Brain"}, "get a topic"),
    Route("get active recall - brain", "handle_get",
          {"user_text": "get active recall - brain"}, "get, lowercase"),
    Route("Get ? - Brain", "handle_get", {"user_text": "Get ? - Brain"},
          "'?' is discovery mode, not a topic"),
    # The separator is a SPACE-hyphen-SPACE precisely so this keeps working.
    Route("Get Step-by-Step Breakdown - Brain", "handle_get",
          {"user_text": "Get Step-by-Step Breakdown - Brain"},
          "a hyphen inside the topic is not the separator"),
    Route("Get Perfect Process", NO_HANDLER, reply("I didn't get that"),
          "get without an area is not a command"),
    Route("Get Perfect Process-Brain", NO_HANDLER, reply("I didn't get that"),
          "the separator needs its spaces"),

    # ── UPDATE EXPENSE ─────────────────────────────────────────────────────────
    # The chain is find-then-write: the lookup is a separate call precisely so a
    # multi-match can stop between the two. These rows all resolve to one match.
    Route("U e Carrefour 12.50 s", "find_expense_matches → update_Expense",
          {"name": "Carrefour", "amount": 12.50, "category": "Shopping"}, "update expense"),
    Route("U e Carrefour 12.50", "find_expense_matches → update_Expense",
          {"name": "Carrefour", "amount": 12.50, "category": "Food"},
          "update expense defaults to Food"),
    Route("u e Carrefour 12.50 G", "find_expense_matches → update_Expense",
          {"name": "Carrefour", "amount": 12.50, "category": "Gift"},
          "update expense, lowercase command + uppercase category"),
    Route("U e Carrefour 12", "find_expense_matches → update_Expense",
          {"name": "Carrefour", "amount": 12.0, "category": "Food"},
          "amount without decimals"),
    # `U e` uses re.fullmatch while `Add e` uses re.search, so trailing junk that
    # Add e would silently swallow makes U e fall through entirely.
    Route("U e Carrefour 12.50 s please", NO_HANDLER, reply("I didn't get that"),
          "U e is anchored: trailing words make the whole command miss"),

    # ── DELETE EXPENSE ─────────────────────────────────────────────────────────
    Route("D e Carrefour", "find_expense_matches → delete_Expense",
          {"name": "Carrefour"}, "delete expense"),
    Route("d e Carrefour", "find_expense_matches → delete_Expense",
          {"name": "Carrefour"}, "delete expense, lowercase"),
    # `D e (.+)` is greedy and unvalidated, so anything after the name is taken
    # as part of the name — and then simply won't be found in Notion.
    Route("D e Carrefour 5", "find_expense_matches → delete_Expense",
          {"name": "Carrefour 5"},
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

    # ── UNDO ───────────────────────────────────────────────────────────────────
    # Reached with an empty user_data, so it reports having nothing to reverse.
    # The reversal itself is driven in test_expense_safety.py.
    Route("undo", NO_HANDLER, reply("Nothing to undo"), "undo with nothing recorded"),
    Route("UNDO", NO_HANDLER, reply("Nothing to undo"), "undo is case-insensitive"),
    Route("undo that", NO_HANDLER, reply("I didn't get that"), "undo takes no argument"),

    # ── UNRECOGNISED ───────────────────────────────────────────────────────────
    # A bare number is a selection ONLY while a list of matches is live. With
    # nothing pending it must stay unrecognised, or every stray digit becomes a
    # command with no visible effect.
    Route("2", NO_HANDLER, reply("I didn't get that"),
          "a bare number with nothing pending is not a selection"),
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


# ─── THE REGISTRY ──────────────────────────────────────────────────────────────
# The table above proves the routes it lists. These prove things about the WHOLE
# registry, so a command added without a row — or with a pattern that overlaps
# one already there — cannot slip through.

# handle_message strips before matching, so the registry must be probed the same
# way or a row with phone-keyboard whitespace looks like it matches nothing.
def _matching(text):
    return [c for c in david.COMMANDS if c.pattern.fullmatch(text.strip())]


def _example_row(command):
    """The first ROUTES row that reaches `command`, or None if it has no row."""
    for route in ROUTES:
        if command.pattern.fullmatch(route.text.strip()):
            return route
    return None


@pytest.mark.parametrize("command", david.COMMANDS, ids=lambda c: c.name)
def test_every_registered_command_has_a_route_row(command):
    """A command with no row is a command nothing checks before a deploy."""
    assert _example_row(command) is not None, (
        f"{command.name!r} is in COMMANDS but no ROUTES row matches its pattern"
    )


def test_no_input_can_be_claimed_by_two_commands():
    """THIS IS WHAT MAKES THE LIST'S ORDER SAFE TO READ.

    Dispatch stops at the first pattern that matches, so ordering is precedence.
    COMMANDS is arranged for reading (and for the help it generates) rather than
    to resolve conflicts — which is only sound while there are no conflicts to
    resolve. Every pattern is anchored on a distinct literal prefix (`add b`,
    `add q`, `add e`, `u e`, `d e`, `undo`, `learn`, …), so under fullmatch no
    string satisfies two.

    Verified over every input the table drives, plus every complete example the
    help advertises. A new command that overlaps an existing one turns this red
    and has to be positioned deliberately.
    """
    probes = [route.text for route in ROUTES] + [
        usage for command in david.COMMANDS if command.help
        for usage in command.help.usage if "[" not in usage
    ]

    collisions = {text: [c.name for c in _matching(text)]
                  for text in probes if len(_matching(text)) > 1}

    assert collisions == {}, f"these inputs match more than one pattern: {collisions}"


@pytest.mark.parametrize("command", david.COMMANDS, ids=lambda c: c.name)
def test_destructive_is_exactly_what_goes_through_the_expense_guard(command, router):
    """`destructive=True` is a claim about the code path, not a label.

    A destructive command finds its target through `find_expense_matches` — the
    month-scoped, CREATED_DESC-sorted lookup that Hard Rule 4 is built on — and
    only then writes. If one ever wrote without it, or a harmless command started
    reaching it, the flag and the behaviour would disagree here.
    """
    route = _example_row(command)

    run(david.handle_message(FakeUpdate(text=route.text), FakeContext()))

    assert ("find_expense_matches" in router.chain) == command.destructive, (
        f"{command.name!r} declares destructive={command.destructive} but routed "
        f"to {router.chain}"
    )


def test_the_destructive_commands_are_the_two_expected_ones():
    """A new destructive command must be a deliberate act, not a diff nobody read."""
    assert {c.name for c in david.COMMANDS if c.destructive} == {"U e", "D e"}


# ─── THE GENERATED HELP ────────────────────────────────────────────────────────
# The help used to be a hand-written string, and it had drifted: it advertised
# `Learn recipe`, which is not in learn.SUPPORTED_TYPES and answers "Unknown type
# recipe", and it said nothing about `Implement … - Diet` taking a different path
# from every other area. It is generated from COMMANDS now, and these keep it
# honest about what that means.

BOLD = re.compile(r"\*([^*\n]+)\*")


def test_help_documents_every_registered_command():
    help_text = david.build_help()

    for command in david.COMMANDS:
        assert f"`{command.name}" in help_text, (
            f"{command.name!r} is a command but the help never mentions it"
        )


def test_help_documents_nothing_that_is_not_a_command():
    """Every section heading in the help belongs to a command that exists.

    The "and nothing else" half: a section can no longer outlive the command it
    describes, because there is nowhere to write one by hand.
    """
    declared = {label for c in david.COMMANDS if c.help and c.help.label
                for label in BOLD.findall(c.help.label)}
    declared |= {label for g in david.HELP_GROUPS.values() if g.label
                 for label in BOLD.findall(g.label)}

    assert set(BOLD.findall(david.build_help())) == declared


def test_every_complete_example_in_the_help_actually_runs(router):
    """An advertised example with no placeholder in it must route somewhere.

    This is the shape of the `Learn recipe` bug: help promising something the
    router or the handler then rejects. Examples containing a `[placeholder]`
    are skipped — they are shapes, not commands.
    """
    help_text = david.build_help()

    for command in david.COMMANDS:
        for usage in command.help.usage:
            assert f"`{usage}`" in help_text, f"{usage!r} is declared but never rendered"
            if "[" in usage:
                continue

            update = FakeUpdate(text=usage)
            run(david.handle_message(update, FakeContext()))

            assert not update.message.replied_with("I didn't get that"), (
                f"the help advertises {usage!r}, which David does not understand"
            )


def test_help_advertises_exactly_the_learn_types_learn_supports():
    """The drift this registry exists to prevent, asserted directly.

    `Learn recipe` was in the help for as long as the help was written by hand.
    The usage lines are built from learn.SUPPORTED_TYPES now, so adding a type
    documents it and removing one un-documents it.
    """
    learn_command = next(c for c in david.COMMANDS if c.name == "Learn")
    advertised = {usage.split()[1] for usage in learn_command.help.usage}

    assert advertised == set(learn.SUPPORTED_TYPES)
    assert "recipe" not in david.build_help()


def test_help_explains_that_diet_is_implemented_differently():
    """Implement routes Diet to implement_diet.py, which merges into a toggle
    tree instead of a flat Manual. The hand-written help never said so."""
    assert "Diet" in david.build_help()


# ─── THE README COMMANDS TABLE ─────────────────────────────────────────────────
# With `h` generated, the README's Commands table is the LAST hand-written copy
# of the command set — and it carries the same shape the bug had, a row reading
# `Learn video|article|podcast|book|pdf [source]` with the types spelled out by
# hand. It cannot be generated (it is prose, with a description per command that
# is worth writing), so it is checked instead.

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"
TABLE_ROW = re.compile(r"^\|(.+?)\|", re.M)      # first column of a table row
PIPE_IN_SPAN = r"\|"                             # escaped pipe inside a code span


def _commands_section() -> str:
    """The README's `## Commands` section, including its subsections."""
    text = README.read_text(encoding="utf-8")
    start = text.index("## Commands")
    rest = text[start + len("## Commands"):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _documented_commands() -> list[str]:
    """Every code span in the first column of the Commands table."""
    section = _commands_section().replace(PIPE_IN_SPAN, "\x00")
    spans = []
    for first_column in TABLE_ROW.findall(section):
        spans += [s.replace("\x00", "|") for s in re.findall(r"`([^`]+)`", first_column)]
    return spans


def test_the_readme_table_documents_every_command():
    """A command missing from the README is undiscoverable outside the chat."""
    section = _commands_section()

    for command in david.COMMANDS:
        assert f"`{command.name}" in section, (
            f"{command.name!r} is a command but the README's Commands section omits it"
        )


def test_the_readme_table_documents_nothing_that_is_not_a_command():
    """The other direction: a row cannot outlive the command it describes."""
    names = sorted((c.name for c in david.COMMANDS), key=len, reverse=True)

    for documented in _documented_commands():
        assert any(documented == name or documented.startswith(f"{name} ")
                   for name in names), (
            f"the README documents {documented!r}, which is not a command"
        )


def test_the_readme_lists_exactly_the_learn_types_learn_supports():
    """The `Learn recipe` bug's last hiding place.

    The README spells the types out by hand — `Learn video|article|…` — so it is
    the one copy left that can advertise a type handle_learn rejects, or omit one
    it accepts.
    """
    learn_row = next(d for d in _documented_commands() if d.startswith("Learn "))
    documented = set(learn_row.removeprefix("Learn ").split(" ")[0].split("|"))

    assert documented == set(learn.SUPPORTED_TYPES), (
        f"README lists {sorted(documented)}, learn supports {sorted(learn.SUPPORTED_TYPES)}"
    )


# ─── FAILURE BRANCHES ──────────────────────────────────────────────────────────
# The table asserts routing. These assert what the user is told when the Notion
# call behind a route fails — the other half of "did this command work?".

def test_quote_reports_a_book_that_is_not_in_the_library(router, monkeypatch):
    monkeypatch.setattr(books, "find_Book_Page", lambda book_name: None)
    update = FakeUpdate(text="Add q Missing - Ch 1 - some text")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("I didn't find 'Missing'")
    assert "add_Quote" not in router.chain


def test_book_reports_a_failed_notion_write(router, monkeypatch):
    monkeypatch.setattr(books, "add_New_Book", lambda name, author, genre: None)
    update = FakeUpdate(text="Add b Dune - Herbert - s")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("Could not connect to Notion")


def test_expense_reports_a_failed_notion_write(router, monkeypatch):
    monkeypatch.setattr(expenses, "add_Expenses", lambda name, amount, category: False)
    update = FakeUpdate(text="Add e Carrefour 2.20")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("Could not connect to Notion")


def test_update_expense_distinguishes_the_three_lookup_outcomes(router, monkeypatch):
    """Not found, lookup failed, and write failed are three different answers.

    The middle one is the one worth having: a Notion outage reported as "not
    found" reads as "there was nothing there anyway", which is the collapse of
    error into empty that this codebase keeps paying for.
    """
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: ([], None))
    update = FakeUpdate(text="U e Ghost 1.00")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("no expense matching 'Ghost'")

    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: ([], "Notion 502: bad gateway"))
    update = FakeUpdate(text="U e Ghost 1.00")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Could not look up")
    assert not update.message.replied_with("no expense matching")

    # Lookup restored to one match, so the failure under test is the WRITE.
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: (ONE_MATCH, None))
    monkeypatch.setattr(expenses, "update_Expense",
                        lambda page_id, amount, category: (False, "Notion 400: bad request"))
    update = FakeUpdate(text="U e Ghost 1.00")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Could not update")


def test_delete_expense_distinguishes_the_three_lookup_outcomes(router, monkeypatch):
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: ([], None))
    update = FakeUpdate(text="D e Ghost")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("no expense matching 'Ghost'")

    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: ([], "Notion 502: bad gateway"))
    update = FakeUpdate(text="D e Ghost")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Could not look up")
    assert not update.message.replied_with("no expense matching")

    # Lookup restored to one match, so the failure under test is the WRITE.
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: (ONE_MATCH, None))
    monkeypatch.setattr(expenses, "delete_Expense",
                        lambda page_id: (False, "Notion 400: bad request"))
    update = FakeUpdate(text="D e Ghost")
    run(david.handle_message(update, FakeContext()))
    assert update.message.replied_with("Could not delete")


def test_budget_reports_a_notion_failure(router, monkeypatch):
    monkeypatch.setattr(david, "budget", lambda: None)
    update = FakeUpdate(text="B")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with("Could not calculate budget")


def test_budget_reply_is_the_budget_text(router):
    update = FakeUpdate(text="B")

    run(david.handle_message(update, FakeContext()))

    assert update.message.replied_with(BUDGET_TEXT)
