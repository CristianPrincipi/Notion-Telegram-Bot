"""Plain sentences, read as commands — through the real router, with Claude faked.

What is asserted is what the interpreter is NOT trusted with: a write never runs
without ✅ Yes, a proposal is run only if it is exactly one real command, a
failure of the model is never a write, and a typed command never reaches the
model at all. Claude's answers are scripted (the `claude` fixture replaces
`services.interpret.complete_json` and records every call), so these prove the
plumbing around the model, not the model's reading — that is measured on the
real thing, from the `Anthropic … in= out=` line in the Railway log.
"""

import asyncio

import pytest

import config
import david
import expense_safety
import pending_choice
from bot import menu, sentences
from conftest import FakeContext, FakeUpdate, run
from services import expenses, interpret
from test_router import NO_HANDLER, router  # noqa: F401 — router is a fixture


class Claude:
    """Scripted answers for the interpreter, and a record of what it was asked."""

    def __init__(self):
        self.answers = []           # dicts to answer with, or ("error", text)
        self.calls = []             # (system, user, kwargs)

    def says(self, command="", question=""):
        self.answers.append({"command": command, "question": question})

    def fails(self, error):
        self.answers.append(("error", error))

    def __call__(self, system, user, schema, **kwargs):
        self.calls.append((system, user, kwargs))
        answer = self.answers.pop(0) if self.answers else {"command": "", "question": ""}
        if isinstance(answer, tuple):
            return None, answer[1]
        return dict(answer), None


@pytest.fixture
def claude(monkeypatch):
    fake = Claude()
    monkeypatch.setattr(interpret, "complete_json", fake)
    return fake


@pytest.fixture
def spies(router, claude):  # noqa: F811 — the imported fixture, by name
    return router


def send(context, *texts):
    updates = []
    for text in texts:
        update = FakeUpdate(text=text)
        run(david.handle_message(update, context))
        updates.append(update)
    return updates


def last_reply(update):
    return update.message.replies[-1]


# ─── A WRITE WAITS FOR ✅ YES ──────────────────────────────────────────────────

def test_a_write_is_proposed_and_runs_only_on_yes(spies, claude):
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()

    [asked] = send(context, "ho speso 12 euro da Carrefour")

    assert spies.chain == NO_HANDLER
    text, kwargs = last_reply(asked)
    assert "Add e Carrefour 12 f" in text
    assert kwargs["reply_markup"] == sentences.CONFIRM_KEYBOARD

    [yes] = send(context, sentences.YES_BUTTON)

    assert spies.chain == "add_Expenses"
    assert spies.parsed_args == {"name": "Carrefour", "amount": 12.0, "category": "Food"}
    assert yes.message.replied_with("▶️ Add e Carrefour 12 f")


def test_a_typed_yes_counts(spies, claude):
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()

    send(context, "ho speso 12 euro da Carrefour", "sì")

    assert spies.chain == "add_Expenses"


def test_no_writes_nothing(spies, claude):
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()

    [_, no] = send(context, "ho speso 12 euro da Carrefour", sentences.NO_BUTTON)

    assert spies.chain == NO_HANDLER
    # One plain answer: ❌ No is not "some other message", which would also say
    # the proposal lapsed.
    assert no.message.reply_texts == ["OK — nothing done."]
    assert not pending_choice.has_pending(context.user_data)


def test_another_command_instead_of_an_answer_drops_the_proposal_and_runs(spies, claude):
    """A new command typed while ✅ Yes is waiting is a new command — not a "no"
    that swallows it, and never a yes."""
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()

    [_, typed] = send(context, "ho speso 12 euro da Carrefour", "B")

    assert spies.chain == "budget"
    assert typed.message.replied_with("Not confirmed")


def test_a_lapsed_proposal_is_not_run(spies, claude):
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()
    send(context, "ho speso 12 euro da Carrefour")
    kind, proposal, items = context.user_data[pending_choice.PENDING_KEY]
    context.user_data[pending_choice.PENDING_KEY] = (
        kind, sentences.Proposal(proposal.composed, expires_at=0.0), items)

    [late] = send(context, sentences.YES_BUTTON)

    assert spies.chain == NO_HANDLER
    assert late.message.replied_with("lapsed")


def test_a_confirmed_destructive_command_still_lists_its_matches(spies, claude, monkeypatch):
    """✅ Yes confirms the COMMAND, not the row: Hard Rule 4 still applies after
    it, because the command runs through the router like a typed one."""
    two = [{"id": f"exp-{n}", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Carrefour"}]},
        "Amount": {"number": n}, "Date": {"date": {"start": f"2026-11-0{n}"}},
        "Category": {"multi_select": [{"name": "Food"}]}}} for n in (1, 2)]
    monkeypatch.setattr(expenses, "find_expense_matches", lambda name: (two, None))
    claude.says("D e Carrefour")
    context = FakeContext()

    send(context, "cancella la spesa Carrefour", sentences.YES_BUTTON)

    assert "delete_Expense" not in spies.chain
    assert pending_choice.kind_of(context.user_data) == expense_safety.KIND


# ─── A READ RUNS AT ONCE ───────────────────────────────────────────────────────

def test_a_read_runs_without_asking(spies, claude):
    claude.says("Agenda tr")
    context = FakeContext()

    [update] = send(context, "cosa ho domani?")

    assert spies.chain == "handle_agenda"
    assert spies.parsed_args == {"day": "tr"}
    assert update.message.replied_with("▶️ Agenda tr")
    assert not pending_choice.has_pending(context.user_data)


def test_the_read_only_commands_are_exactly_these():
    """A command that writes and is marked read-only would run from a sentence
    unconfirmed. Adding to this set is a decision, not a diff nobody read."""
    assert {c.name for c in david.COMMANDS if c.read_only} == {
        "Menu", "Agenda", "B", "Diag", "Find", "DBs", "Get", "Giornale", "v", "h"}


# ─── WHAT IS REFUSED ───────────────────────────────────────────────────────────

def test_a_proposal_that_is_no_command_is_dropped(spies, claude):
    claude.says("Spend 12 at Carrefour")
    context = FakeContext()

    [update] = send(context, "ho speso 12 euro da Carrefour")

    assert spies.chain == NO_HANDLER
    assert update.message.replied_with("I didn't get that")
    assert not pending_choice.has_pending(context.user_data)


def test_a_proposal_matching_two_commands_is_dropped(claude):
    """Cannot happen with today's registry — tests/test_router.py proves no input
    matches two patterns — so it is driven with a `matching` that says it does."""
    claude.says("B")
    update, context = FakeUpdate(text=""), FakeContext()

    composed = run(sentences.understand(
        update, context, "quanto ho speso?", catalogue="",
        matching=lambda text: david.COMMANDS[:2]))

    assert composed is None
    assert update.message.replied_with("I didn't get that")


def test_a_failure_of_the_model_is_reported_and_writes_nothing(spies, claude):
    claude.fails("Daily Anthropic budget reached — about $5.01 of $5.00 spent")
    context = FakeContext()

    [update] = send(context, "ho speso 12 euro da Carrefour")

    assert spies.chain == NO_HANDLER
    assert update.message.replied_with("I didn't get that")
    assert update.message.replied_with("Daily Anthropic budget reached")
    assert not pending_choice.has_pending(context.user_data)


def test_a_model_that_does_not_answer_in_time_is_reported(spies, monkeypatch):
    def slow(*args, **kwargs):
        import time
        time.sleep(0.3)
        return {"command": "B", "question": ""}, None
    monkeypatch.setattr(interpret, "complete_json", slow)
    monkeypatch.setattr(sentences, "INTERPRET_TIMEOUT", 0.05)

    [update] = send(FakeContext(), "quanto ho speso?")

    assert spies.chain == NO_HANDLER
    assert update.message.replied_with("no answer within")


def test_nothing_is_understood_from_chatter(spies, claude):
    [update] = send(FakeContext(), "ciao David")

    assert spies.chain == NO_HANDLER
    assert update.message.replied_with("I didn't get that")


# ─── WHAT NEVER REACHES THE MODEL ──────────────────────────────────────────────

@pytest.mark.parametrize("text", ["Add e Carrefour 2.20 f", "B", "m"])
def test_a_typed_command_never_costs_a_claude_call(spies, claude, text):
    send(FakeContext(), text)

    assert claude.calls == []


@pytest.mark.parametrize("text", ["", "   ", "2", "x" * (config.INTERPRET_MAX_CHARS + 1)])
def test_empty_bare_numbers_and_long_pastes_are_not_sent(spies, claude, text):
    [update] = send(FakeContext(), text)

    assert claude.calls == []
    assert update.message.replied_with("I didn't get that")


def test_a_late_tap_is_not_sent_either(spies, claude):
    [update] = send(FakeContext(), sentences.YES_BUTTON)

    assert claude.calls == []
    assert update.message.replied_with("lapsed")


# ─── A MISSING PART IS ASKED FOR ───────────────────────────────────────────────

def test_a_missing_part_is_asked_and_the_reply_read_with_the_sentence(spies, claude):
    claude.says(question="Quanto hai speso?")
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()

    [asked, proposed] = send(context, "ho fatto la spesa da Carrefour", "12")

    assert asked.message.replied_with("Quanto hai speso?")
    _, second_user_message, _ = claude.calls[1]
    assert "ho fatto la spesa da Carrefour" in second_user_message
    assert "Quanto hai speso?" in second_user_message
    assert "12" in second_user_message
    assert proposed.message.replied_with("Add e Carrefour 12 f")

    send(context, sentences.YES_BUTTON)

    assert spies.chain == "add_Expenses"


def test_an_answer_that_is_also_a_command_word_is_read_as_the_answer(spies, claude):
    """`h` is the History genre AND the help command; `m` is Manga AND the menu.
    Answering "Which genre?" with one of them must answer the question — it used
    to open the help, and the book was never added."""
    claude.says(question="Che genere?")
    claude.says("Add b Sapiens - Harari - h")
    context = FakeContext()

    [_, proposed] = send(context, "aggiungi il libro Sapiens di Harari", "h")

    assert spies.chain == NO_HANDLER
    _, second_user_message, _ = claude.calls[1]
    assert "Che genere?" in second_user_message and second_user_message.endswith("h")
    assert proposed.message.replied_with("Add b Sapiens - Harari - h")

    send(context, sentences.YES_BUTTON)

    assert spies.chain == "add_New_Book"
    assert spies.parsed_args == {"name": "Sapiens", "author": "Harari", "genre": "History"}


def test_a_whole_command_given_as_the_answer_is_left_to_the_reading(spies, claude):
    """The open question takes any reply, as the menu's does; the model is told a
    reply that is a complete request on its own replaces the earlier message, and
    whatever it reads still passes the same checks."""
    claude.says(question="Quanto hai speso?")
    claude.says("B")
    context = FakeContext()

    send(context, "ho fatto la spesa da Carrefour", "B")

    assert spies.chain == "budget"
    assert len(claude.calls) == 2


# ─── WHAT THE MODEL IS GIVEN ───────────────────────────────────────────────────

def test_the_interpreter_runs_on_the_cheap_model_at_its_own_effort_and_cap(spies, claude):
    send(FakeContext(), "quanto mi resta?")

    [(system, user, kwargs)] = claude.calls
    assert kwargs == {"model": config.ANTHROPIC_INTERPRET_MODEL,
                      "effort": config.ANTHROPIC_INTERPRET_EFFORT,
                      "max_tokens": config.ANTHROPIC_INTERPRET_MAX_TOKENS}
    assert user == "quanto mi resta?"


def test_the_interpreter_is_cheaper_than_the_main_model():
    cheap = config.ANTHROPIC_RATES[config.ANTHROPIC_INTERPRET_MODEL]
    main = config.ANTHROPIC_RATES[config.ANTHROPIC_MODEL]
    assert cheap[0] < main[0] and cheap[1] < main[1]


def test_the_catalogue_is_every_command_but_the_menu():
    catalogue = david.build_catalogue()

    for command in david.COMMANDS:
        if command.help is None:
            continue
        for usage in command.help.usage:
            assert (f"- {usage}" in catalogue) == (command.name != "Menu"), usage
    for name in config.CATEGORY_MAP.values():
        assert name in catalogue


def test_the_system_prompt_carries_the_catalogue_and_today(spies, claude, monkeypatch):
    send(FakeContext(), "quanto mi resta?")

    [(system, _, _)] = claude.calls
    assert david.CATALOGUE in system
    assert interpret._today() in system


def test_the_stale_tap_labels_are_not_commands():
    for label in (sentences.YES_BUTTON, sentences.NO_BUTTON):
        assert david.matching_commands(label) == []


def test_the_interpreter_never_waits_on_the_event_loop(spies, monkeypatch):
    """It runs on a worker thread: the loop stays free for the scheduled jobs."""
    seen = {}

    def record(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return {"command": "", "question": ""}, None
    monkeypatch.setattr(interpret, "complete_json", record)

    send(FakeContext(), "ciao")

    assert seen == {"on_loop": False}


def test_menu_keyboard_returns_after_a_no(spies, claude):
    claude.says("Add e Carrefour 12 f")
    context = FakeContext()

    [_, no] = send(context, "ho speso 12 euro", sentences.NO_BUTTON)

    assert last_reply(no)[1]["reply_markup"] == menu.MAIN_KEYBOARD
