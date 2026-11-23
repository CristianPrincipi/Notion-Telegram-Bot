"""The button menu, driven through the real router.

The menu's whole claim is that it COMPOSES a command and runs nothing itself: a
walk through its buttons must end in exactly the handler call the typed command
makes, with exactly the arguments that were entered. So every walkthrough below
sends its taps one message at a time through `david.handle_message` — the same
entry point a phone uses — with the router test's spies in place of Notion and
Google, and asserts on what those spies received.

The other half is what the menu must REFUSE to do: send text that a command
would read differently from how it was entered (a book title holding " - ",
a reminder name holding a date), answer a question that has lapsed, or treat a
command word as anything but an answer while a question is open.
"""

import re

import pytest

import bot.menu
import config
import david
import pending_choice
from bot import menu, sentences
from conftest import FakeContext, FakeUpdate, run
from test_router import NO_HANDLER, _matching, router  # noqa: F401 — router is a fixture

# The real show_menu, captured before the router fixture swaps it for a spy: the
# walkthroughs need the menu itself, and a spy for everything it leads to.
REAL_SHOW_MENU = bot.menu.show_menu


@pytest.fixture
def spies(router, monkeypatch):  # noqa: F811 — the imported fixture, by name
    monkeypatch.setattr(bot.menu, "show_menu", REAL_SHOW_MENU)
    return router


def tap(context, *texts):
    """Send each text as its own message, in one conversation. Returns the updates."""
    updates = []
    for text in texts:
        update = FakeUpdate(text=text)
        run(david.handle_message(update, context))
        updates.append(update)
    return updates


def replies(updates):
    return [text for update in updates for text in update.message.reply_texts]


# ─── EVERY FLOW, END TO END ────────────────────────────────────────────────────
# (flow label, answers, handler chain, parsed args, the command it composes)

WALKTHROUGHS = [
    ("💵 Expense", ["Carrefour", "2.20", "Food"], "add_Expenses",
     {"name": "Carrefour", "amount": 2.20, "category": "Food"}, "Add e Carrefour 2.20 f"),
    ("💰 Budget", [], "budget", {}, "B"),
    ("✏️ Change expense", ["Carrefour", "12,50", "Gift"],
     "find_expense_matches → update_Expense",
     {"name": "Carrefour", "amount": 12.50, "category": "Gift"}, "U e Carrefour 12,50 g"),
    ("🗑️ Delete expense", ["Carrefour"], "find_expense_matches → delete_Expense",
     {"name": "Carrefour"}, "D e Carrefour"),
    ("📅 Reminder", ["Dentist", "Tomorrow", "14:30"], "handle_remind",
     {"user_text": "Remind Dentist tr - 14.30"}, "Remind Dentist tr - 14.30"),
    ("📆 Agenda", ["Tomorrow"], "handle_agenda", {"day": "tr"}, "Agenda tr"),
    ("🚫 Cancel reminder", ["Dentist"], "find_event_matches → delete_event",
     {"name": "Dentist"}, "Cancel Dentist"),
    ("↩️ Undo", [], NO_HANDLER, {"reply_contains": "Nothing to undo"}, "undo"),
    ("📖 Book", ["Dune", "Frank Herbert", "Adventure"], "add_New_Book",
     {"name": "Dune", "author": "Frank Herbert", "genre": "Adventure"},
     "Add b Dune - Frank Herbert - a"),
    ("🖋️ Quote", ["Dune", "On Fear", "I must not fear.\nFear is the mind-killer."],
     "find_Book_Page → add_Quote",
     {"book_name": "Dune", "quote_title": "On Fear",
      "quote_text": "I must not fear.\nFear is the mind-killer."},
     "Add q Dune / On Fear / I must not fear.\nFear is the mind-killer."),
    ("🧠 Learn", ["video", "https://youtu.be/abc"], "handle_learn",
     {"user_text": "Learn video https://youtu.be/abc"}, "Learn video https://youtu.be/abc"),
    ("🔎 Get", ["Active Recall", "Brain"], "handle_get",
     {"user_text": "Get Active Recall - Brain"}, "Get Active Recall - Brain"),
    ("🔧 Implement", ["Memory Techniques", "Brain"], "handle_implement",
     {"user_text": "Implement Memory Techniques - Brain"},
     "Implement Memory Techniques - Brain"),
]


@pytest.mark.parametrize("label, answers, chain, args, composed", WALKTHROUGHS,
                         ids=[w[0] for w in WALKTHROUGHS])
def test_each_flow_reaches_its_command_with_what_was_entered(
        spies, label, answers, chain, args, composed):
    updates = tap(FakeContext(), menu.MENU_BUTTON, label, *answers)

    assert spies.chain == chain
    if chain == NO_HANDLER:
        assert any(args["reply_contains"] in text for text in replies(updates))
    else:
        assert spies.parsed_args == args

    # The command is shown, so the menu teaches the syntax it saves you typing,
    # and the persistent 📋 Menu keyboard comes back with it.
    last = updates[-1].message.replies
    echo = [(text, kwargs) for text, kwargs in last if text == f"▶️ {composed}"]
    assert echo, f"expected the composed command to be shown, got {[t for t, _ in last]}"
    assert echo[0][1]["reply_markup"] == menu.MAIN_KEYBOARD


def test_every_flow_has_a_walkthrough():
    """A flow added without one is a flow nothing proves reaches its command."""
    assert {flow.label for flow in menu.FLOWS} == {w[0] for w in WALKTHROUGHS}


def test_every_command_is_in_the_menu_or_left_out_on_purpose():
    """A new command has to be put in the menu, or named here, deliberately."""
    left_out = {"Menu", "Month", "Diag", "Find", "DBs", "Giornale", "v", "h"}
    in_menu = {flow.command for flow in menu.FLOWS}

    assert {c.name for c in david.COMMANDS} - in_menu == left_out


def test_no_button_is_itself_a_command():
    """A tap that lapsed must not run something: every label the menu shows
    matches no command, except 📋 Menu, which is the Menu command."""
    labels = ([flow.label for flow in menu.FLOWS] + [menu.CANCEL_BUTTON]
              + [button for flow in menu.FLOWS for f in flow.fields for button in f.buttons])

    assert {label: [c.name for c in _matching(label)]
            for label in labels if _matching(label)} == {}
    assert [c.name for c in _matching(menu.MENU_BUTTON)] == ["Menu"]


def test_the_area_buttons_are_areas_david_knows():
    for area in config.AREA_BUTTONS:
        assert f"{area.upper()}_ID" in config.OPTIONAL_ENV


# ─── ONE QUESTION AT A TIME, IN THE ONE SLOT ───────────────────────────────────

def test_a_bad_amount_is_asked_again(spies):
    context = FakeContext()
    updates = tap(context, menu.MENU_BUTTON, "💵 Expense", "Carrefour", "two euros")

    assert spies.chain == NO_HANDLER
    assert "How much" in updates[-1].message.reply_texts[-1]

    tap(context, "2", "Food")

    assert spies.parsed_args == {"name": "Carrefour", "amount": 2.0, "category": "Food"}


def test_an_amount_the_command_would_not_read_is_asked_again(spies):
    """`2.` is a float to parse_amount but not an amount to the `Add e` pattern,
    so it is refused at the question — not built into a command that misses."""
    context = FakeContext()
    updates = tap(context, "m", "💵 Expense", "Carrefour", "2.")

    assert "How much" in updates[-1].message.reply_texts[-1]
    assert pending_choice.peek(context.user_data, menu.KIND).values == {"name": "Carrefour"}


def test_an_unknown_category_is_asked_again(spies):
    context = FakeContext()
    updates = tap(context, "m", "💵 Expense", "Carrefour", "2", "Groceries")

    assert spies.chain == NO_HANDLER
    assert "Which category" in updates[-1].message.reply_texts[-1]


def test_while_a_question_is_open_a_command_word_is_an_answer(spies):
    """An expense CAN be called "B". Once a question is asked, the next message
    answers it — it is not the budget command."""
    context = FakeContext()
    tap(context, "m", "💵 Expense", "B")

    assert spies.chain == NO_HANDLER
    assert pending_choice.kind_of(context.user_data) == menu.KIND

    tap(context, "3", "Food")

    assert spies.parsed_args == {"name": "B", "amount": 3.0, "category": "Food"}


def test_cancel_closes_the_menu(spies):
    context = FakeContext()
    updates = tap(context, "m", "💵 Expense", menu.CANCEL_BUTTON)

    assert not pending_choice.has_pending(context.user_data)
    assert updates[-1].message.replies[-1][1]["reply_markup"] == menu.MAIN_KEYBOARD

    tap(context, "Carrefour")

    assert spies.chain == NO_HANDLER


def test_a_lapsed_question_is_not_answered(spies):
    """The two-minute expiry of the shared slot applies: an answer typed after it
    is an ordinary message again, never the last field of a command."""
    context = FakeContext()
    tap(context, "m", "🗑️ Delete expense")
    kind, session, items = context.user_data[pending_choice.PENDING_KEY]
    context.user_data[pending_choice.PENDING_KEY] = (
        kind, type(session)(session.flow, session.values, expires_at=0.0), items)

    updates = tap(context, "Carrefour")

    assert spies.chain == NO_HANDLER
    assert updates[-1].message.replied_with("I didn't get that")


def test_a_tap_on_a_lapsed_menu_says_so(spies):
    context = FakeContext()
    updates = tap(context, "💵 Expense")

    assert spies.chain == NO_HANDLER
    assert updates[-1].message.replied_with("Tap 📋 Menu")


def test_the_menu_takes_over_a_live_numbered_list(spies, monkeypatch):
    """One slot: the menu, opened while a `D e` list waits for a number, replaces
    it — so a `2` then answers the menu, and is never read as "delete the second"."""
    from services import expenses
    two = [{"id": f"exp-{n}", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Carrefour"}]},
        "Amount": {"number": n}, "Date": {"date": {"start": "2026-11-0%d" % n}},
        "Category": {"multi_select": [{"name": "Food"}]}}} for n in (1, 2)]
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: (two, None))
    context = FakeContext()
    tap(context, "D e Carrefour")
    assert pending_choice.kind_of(context.user_data) != menu.KIND

    tap(context, menu.MENU_BUTTON, "2")

    assert pending_choice.kind_of(context.user_data) == menu.KIND
    assert "delete_Expense" not in spies.chain


# ─── WHAT THE MENU REFUSES TO SEND ─────────────────────────────────────────────

def test_a_book_title_holding_the_separator_is_asked_again(spies):
    context = FakeContext()
    updates = tap(context, "m", "📖 Book", "Dune - Part One")

    assert spies.chain == NO_HANDLER
    assert updates[-1].message.replied_with('" - "')
    assert pending_choice.peek(context.user_data, menu.KIND).values == {}


def test_a_quote_body_with_pdf_markers_is_asked_again(spies):
    context = FakeContext()
    updates = tap(context, "m", "🖋️ Quote", "Dune", "Ch 1", "Fear is | the mind-killer")

    assert spies.chain == NO_HANDLER
    assert updates[-1].message.replied_with("PDF")


def test_a_reminder_name_that_would_be_read_as_a_date_is_asked_again(spies):
    """`Remind Meet td 12 tr - 10` books "Meet", today at 12:00 — the name is cut at
    the first date. Each field passes on its own; only reading the whole text back
    the way the reminder service will catches it."""
    context = FakeContext()
    updates = tap(context, "m", "📅 Reminder", "Meet td 12", "Tomorrow", "10")

    assert spies.chain == NO_HANDLER
    assert updates[-1].message.replied_with("would read the name as")
    assert pending_choice.peek(context.user_data, menu.KIND).values == {}


def test_an_implement_page_name_with_a_dash_is_asked_again(spies):
    context = FakeContext()
    updates = tap(context, "m", "🔧 Implement", "Step-by-step", "Brain")

    assert spies.chain == NO_HANDLER
    assert updates[-1].message.replied_with("would read the page as")


def test_run_composed_refuses_text_that_reaches_another_reading(spies):
    """The router's own check, under every field check: a composed command runs
    only if it fullmatches the command it names, with the groups it names."""
    update = FakeUpdate(text="")
    composed = menu.Composed("Add b Dune - Part One - Herbert - s", "Add b",
                             {"name": "Dune - Part One", "author": "Herbert", "genre": "s"})

    run(david.run_composed(composed, update, FakeContext()))

    assert spies.chain == NO_HANDLER
    assert update.message.replied_with("did not send it")


def test_run_composed_refuses_text_that_reaches_another_command(spies):
    update = FakeUpdate(text="")
    composed = menu.Composed("B", "Add e", {})

    run(david.run_composed(composed, update, FakeContext()))

    assert spies.chain == NO_HANDLER


# ─── THE SAME DISPATCH FOR EVERY CHANNEL ───────────────────────────────────────

def test_handle_message_and_dispatch_text_are_one_path(spies):
    run(david.dispatch_text("Add e Carrefour 2.20 f", FakeUpdate(text=""), FakeContext()))

    assert spies.parsed_args == {"name": "Carrefour", "amount": 2.20, "category": "Food"}


def test_question_handlers_answer_any_text_and_selections_only_numbers():
    assert set(david.QUESTION_HANDLERS) == {menu.KIND, sentences.CONFIRM, sentences.CLARIFY}
    assert not set(david.QUESTION_HANDLERS) & set(david.SELECTION_HANDLERS)


def test_no_flow_label_looks_like_a_selection():
    assert not [f.label for f in menu.FLOWS if re.fullmatch(r"\d+", f.label)]
