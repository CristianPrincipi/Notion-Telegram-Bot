"""The button menu: `Menu`, `m`, or the 📋 Menu button under the chat.

WHAT IT IS
----------
A way to run a command without remembering how it is written. Tap a command,
answer one question per part — with buttons wherever the answer is a closed list
(categories, genres, today/tomorrow, areas) — and the menu writes the command out
(`Add e Carrefour 2.20 f`) and hands it to the router, exactly as if you had
typed it. The reply shows the command it built, so using the menu teaches the
syntax it saves you typing.

IT COMPOSES; IT NEVER RUNS ANYTHING
-----------------------------------
Nothing here writes to Notion or Google, and nothing here picks a handler. The
menu's only output is a `Composed`: the text, the command that must read it, and
what that command must read out of it. `david.run_composed` routes the text
through the same `COMMANDS` registry a typed message goes through — so the
OWNER_ID gate, Hard Rule 4's numbered list for `D e` / `U e` / `Cancel`, the
locks and the router tests all hold for the menu with no second copy of any of
them. The menu cannot do anything a typed command cannot.

THE READ-BACK CHECK
-------------------
A command is cut into its parts by separators — ` - ` in `Add b`, ` / ` in
`Add q`, the first date-looking word in `Remind`. A part entered in a box of its
own can hold the separator, and the text built from it is then read differently
from how it was entered: a book called "Dune - Part One" becomes the book "Dune"
by the author "Part One". So:

  - each part is checked as it arrives, for the separator its command cuts at,
    and asked again with the reason when it holds one;
  - for the commands that hand their WHOLE text to a service which cuts it up
    itself (`Remind`, `Get`, `Implement`), the finished text is read back with
    that service's own pattern, and a part that would come back different is
    asked again — this is the only check that sees `Remind Meet td 12 tr - 10`
    for what it is, because every part is fine on its own;
  - `david.run_composed` then checks the router's reading, under all of the
    above: it refuses text that does not fullmatch the command named, with the
    groups named.

WHY REPLY-KEYBOARD BUTTONS, NOT INLINE ONES
-------------------------------------------
A reply-keyboard button sends its label as an ordinary text message, so a tap
reaches David through the same owner-only MessageHandler as anything typed, and
the menu needs no handler of its own. An inline button sends a callback query
instead, which a MessageHandler never sees: it would need a CallbackQueryHandler,
and PTB's filters — OWNER_ID's included — do not apply to one. The gate would
become an `if` inside handler code, which is exactly what it must never be.

WHERE THE STATE LIVES
---------------------
In pending_choice's ONE slot, as kind `menu`. So there is one open prompt at a
time, menu or numbered list: opening the menu while a `D e` list waits replaces
the list, and a `2` typed next answers the menu — it can never delete "the
second match" of a list that is no longer on screen.

While a question is open the next message ANSWERS it, whatever it says — an
expense can be called "B", and a name is not a command. Only ❌ Cancel, 📋 Menu
(start again) and the slot's two-minute expiry close it. Each question re-arms
the two minutes. The expiry is kept short for the reason the numbered list has
it: an answer is the last part of a command, and the last part of `Cancel` is a
delete. A message typed after it lapses is an ordinary message again.

Telegram-only: this module talks to the chat and never to a service.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from telegram import ReplyKeyboardMarkup

import pending_choice
from bot.expenses import AMOUNT, parse_amount
from config import AREA_BUTTONS, CATEGORY_MAP, GENRE_MAP
from services.implement import IMPLEMENT_PATTERN
from services.learn import SUPPORTED_TYPES
from services.pkm import GET_PATTERN
from services.reminder import REMIND_PATTERN

logger = logging.getLogger(__name__)

KIND = "menu"

MENU_BUTTON   = "📋 Menu"
CANCEL_BUTTON = "❌ Cancel"
ALL_TOPICS    = "❔ All topics"

# The keyboard David leaves under the chat: one button that opens the menu.
# `is_persistent` keeps it showing instead of collapsing behind the keyboard icon.
MAIN_KEYBOARD = ReplyKeyboardMarkup([[MENU_BUTTON]], resize_keyboard=True, is_persistent=True)


@dataclass(frozen=True)
class Composed:
    """A command built for the router: its text, the Command that must read it,
    and the named groups that Command must read out of it. A command that hands
    its whole text to a service names one group, `text`."""

    text: str
    command: str
    groups: dict


@dataclass(frozen=True)
class Field:
    """One question. `check` turns the answer into the value written into the
    command, or says why it cannot be: (value, error)."""

    key: str
    question: str
    check: Callable[[str], tuple[str | None, str | None]]
    buttons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Flow:
    """One menu entry: the button, the command it builds, and its questions.

    `reads_back` is set for a command whose service cuts the text up itself: it
    returns what that service will read, by field key, or None if it reads
    nothing at all.
    """

    label: str
    command: str
    fields: tuple[Field, ...]
    compose: Callable[[dict], Composed]
    reads_back: Callable[[str], dict | None] | None = None


@dataclass(frozen=True)
class Session:
    """The open question, parked in pending_choice's slot. `flow` is None while
    the command itself is being chosen. The slot reads only `expires_at`."""

    flow: Flow | None
    values: dict
    expires_at: float


# ─── CHECKS ────────────────────────────────────────────────────────────────────

def _one_line(text):
    value = text.strip()
    if not value:
        return None, "That was empty."
    if "\n" in value:
        return None, "Please keep it on one line."
    return value, None


def _without(separator, command):
    """A one-line part that must not hold the separator its command cuts at."""
    def check(text):
        value, err = _one_line(text)
        if err:
            return None, err
        if separator in value:
            return None, (f'It can\'t contain "{separator}": that is where {command} '
                          f"separates its parts.")
        return value, None
    return check


_AMOUNT = re.compile(AMOUNT)


def _amount(text):
    """The amount as typed — `2.20` or `2,20`, a euro sign dropped — once it is
    a number the expense commands accept and parse_amount takes as positive."""
    raw = text.replace("€", "").strip()
    if not _AMOUNT.fullmatch(raw):
        return None, f'"{text.strip()}" is not an amount.'
    _, err = parse_amount(raw)
    return (None, err) if err else (raw, None)


def _choice(mapping):
    """A shortcut from one of config's maps, given its button (the full name)
    or the shortcut itself."""
    def check(text):
        answer = text.strip().lower()
        for shortcut, name in mapping.items():
            if answer in (shortcut.lower(), name.lower()):
                return shortcut, None
        return None, "Pick one of the buttons."
    return check


_DAY_BUTTONS = {"today": "td", "tomorrow": "tr"}
_DAY = re.compile(r"td|tr|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?")


def _day(text):
    """A day the calendar commands read: a button, `td` / `tr`, or a date. What
    the date MEANS — and refusing one in the past — stays the service's."""
    answer = text.strip().lower()
    value = _DAY_BUTTONS.get(answer, answer)
    if not _DAY.fullmatch(value):
        return None, "Tap a button, or type a date like 12.06 or 12.06.2027."
    return value, None


_TIME = re.compile(r"\d{1,2}(?:\.\d{1,2})?")


def _time(text):
    value = text.strip().replace(":", ".")
    if not _TIME.fullmatch(value):
        return None, "Type a time like 14.30, or just the hour: 10."
    return value, None


# A PDF is sent as a file, with `Learn pdf` as its caption: there is nothing to
# type into a box for it.
LEARN_TYPES = tuple(t for t in SUPPORTED_TYPES if t != "pdf")


def _learn_type(text):
    answer = text.strip().lower()
    if answer in LEARN_TYPES:
        return answer, None
    return None, "Pick one of the buttons. A PDF is sent as a file, with Learn pdf as its caption."


def _topic(text):
    return ("?", None) if text.strip() == ALL_TOPICS else _one_line(text)


def _quote_body(text):
    """Several lines are fine. " | " is not: on a typed quote it means "cut this
    out of the attached PDF", which a quote typed here has none of."""
    value = text.strip()
    if not value:
        return None, "That was empty."
    if " | " in value:
        return None, ('" | " marks where a quote begins and ends in a PDF. For that, '
                      "attach the PDF with the Add q command as its caption.")
    return value, None


# ─── READING BACK ──────────────────────────────────────────────────────────────

def _whole(command):
    """compose() for a command whose handler takes the whole text."""
    def compose(text):
        return Composed(text, command, {"text": text})
    return compose


def _reads_with(pattern, *keys):
    """What a service's own pattern cuts out of the text, by field key."""
    def reads_back(text):
        match = re.match(pattern, text)
        if match is None:
            return None
        return {key: (group or "").strip() for key, group in zip(keys, match.groups())}
    return reads_back


def _remind_reads(text):
    match = re.match(REMIND_PATTERN, text)
    if match is None:
        return None
    return {"name": match["name"].strip(), "day": match["date"].strip().lower(),
            "time": match["time"].strip()}


# ─── THE FLOWS ─────────────────────────────────────────────────────────────────

def _expense(label, command, question):
    def compose(v):
        return Composed(f"{command} {v['name']} {v['amount']} {v['category']}", command,
                        {"name": v["name"], "amount": v["amount"], "category": v["category"]})
    return Flow(label, command, (
        Field("name", question, _one_line),
        Field("amount", "How much? (e.g. 2.20)", _amount),
        Field("category", "Which category?", _choice(CATEGORY_MAP),
              tuple(CATEGORY_MAP.values())),
    ), compose)


_AREA = Field("area", "Which area?", _one_line, AREA_BUTTONS)

FLOWS = (
    _expense("💵 Expense", "Add e", "What did you buy? (e.g. Carrefour)"),
    Flow("💰 Budget", "B", (), lambda v: Composed("B", "B", {})),
    _expense("✏️ Change expense", "U e", "Which expense? (its name)"),
    Flow("🗑️ Delete expense", "D e",
         (Field("name", "Which expense? (its name)", _one_line),),
         lambda v: Composed(f"D e {v['name']}", "D e", {"name": v["name"]})),
    Flow("📅 Reminder", "Remind", (
        Field("name", "What is it? (e.g. Dentist)", _one_line),
        Field("day", "Which day? Or type a date, like 12.06", _day, ("Today", "Tomorrow")),
        Field("time", "What time? (e.g. 14.30, or just 10)", _time),
    ), lambda v: _whole("Remind")(f"Remind {v['name']} {v['day']} - {v['time']}"),
        reads_back=_remind_reads),
    Flow("📆 Agenda", "Agenda", (
        Field("day", "Which day? Or type a date, like 12.06", _day, ("Today", "Tomorrow")),
    ), lambda v: Composed(f"Agenda {v['day']}", "Agenda", {"day": v["day"]})),
    Flow("🚫 Cancel reminder", "Cancel",
         (Field("name", "Which calendar event? (its name)", _one_line),),
         lambda v: Composed(f"Cancel {v['name']}", "Cancel", {"name": v["name"]})),
    Flow("↩️ Undo", "undo", (), lambda v: Composed("undo", "undo", {})),
    Flow("📖 Book", "Add b", (
        Field("name", "Which book? (its title)", _without(" - ", "Add b")),
        Field("author", "Who wrote it?", _without(" - ", "Add b")),
        Field("genre", "Which genre?", _choice(GENRE_MAP), tuple(GENRE_MAP.values())),
    ), lambda v: Composed(f"Add b {v['name']} - {v['author']} - {v['genre']}", "Add b",
                          {"name": v["name"], "author": v["author"], "genre": v["genre"]})),
    Flow("🖋️ Quote", "Add q", (
        Field("book", "Which book is it from?", _without(" / ", "Add q")),
        Field("title", "A title for the quote?", _without(" / ", "Add q")),
        Field("body", "The quote:", _quote_body),
    ), lambda v: Composed(f"Add q {v['book']} / {v['title']} / {v['body']}", "Add q",
                          {"book": v["book"], "title": v["title"], "body": v["body"]})),
    Flow("🧠 Learn", "Learn", (
        Field("type", "What is it?", _learn_type, LEARN_TYPES),
        Field("source", "The link — or, for a book, its title:", _one_line),
    ), lambda v: _whole("Learn")(f"Learn {v['type']} {v['source']}")),
    Flow("🔎 Get", "Get", (
        Field("topic", "Which topic?", _topic, (ALL_TOPICS,)),
        _AREA,
    ), lambda v: _whole("Get")(f"Get {v['topic']} - {v['area']}"),
        reads_back=_reads_with(GET_PATTERN, "topic", "area")),
    Flow("🔧 Implement", "Implement", (
        Field("page", "Which Learn page? (its title)", _one_line),
        _AREA,
    ), lambda v: _whole("Implement")(f"Implement {v['page']} - {v['area']}"),
        reads_back=_reads_with(IMPLEMENT_PATTERN, "page", "area")),
)

FLOWS_BY_LABEL = {flow.label: flow for flow in FLOWS}


# ─── KEYBOARDS ─────────────────────────────────────────────────────────────────

def _keyboard(labels, per_row=3):
    rows = [list(labels[i:i + per_row]) for i in range(0, len(labels), per_row)]
    return ReplyKeyboardMarkup(rows + [[CANCEL_BUTTON]], resize_keyboard=True)


COMMAND_KEYBOARD = _keyboard([flow.label for flow in FLOWS], per_row=2)


# ─── THE CONVERSATION ──────────────────────────────────────────────────────────

def _park(user_data, flow, values):
    pending_choice.remember(user_data, KIND,
                            Session(flow, dict(values), pending_choice.deadline()), None)


async def show_menu(update, context):
    """Open the menu: the list of commands, as buttons."""
    _park(context.user_data, None, {})
    await update.message.reply_text("📋 What would you like to do?",
                                    reply_markup=COMMAND_KEYBOARD)


async def cmd_menu(update, context, args):
    await show_menu(update, context)


async def _ask(update, user_data, flow, values, problem=None):
    field = flow.fields[len(values)]
    _park(user_data, flow, values)
    text = field.question if problem is None else f"⚠️ {problem}\n\n{field.question}"
    await update.message.reply_text(text, reply_markup=_keyboard(field.buttons))


def _first_misread(flow, values, composed):
    """(index, what it reads as) for the first field the command would read
    differently from how it was entered, or None when every one reads back."""
    if flow.reads_back is None:
        return None
    read = flow.reads_back(composed.text)
    for index, field in enumerate(flow.fields):
        got = None if read is None else read.get(field.key)
        if got != values[field.key]:
            return index, got
    return None


async def _advance(update, user_data, flow, values):
    """Ask the next question, or — every one answered — hand back the command."""
    if len(values) < len(flow.fields):
        await _ask(update, user_data, flow, values)
        return None

    composed = flow.compose(values)
    misread = _first_misread(flow, values, composed)
    if misread is not None:
        index, got = misread
        field = flow.fields[index]
        problem = (f'{flow.command} would read the {field.key} as "{got}", not '
                   f'"{values[field.key]}", so I have not sent it. Please type the '
                   f"{field.key} again." if got is not None else
                   f"{flow.command} could not read that back, so I have not sent it.")
        logger.info("Menu held back %r: the %s reads back as %r", composed.text, field.key, got)
        kept = {f.key: values[f.key] for f in flow.fields[:index]}
        await _ask(update, user_data, flow, kept, problem)
        return None

    pending_choice.clear(user_data)
    return composed


async def answer(update, context, text):
    """The reply to an open menu question. Returns the Composed command to run
    once the last part is in, else None — the menu has replied itself."""
    user_data = context.user_data
    session = pending_choice.peek(user_data, KIND)
    if session is None:
        # Lapsed between the router's look at the slot and this one.
        await reply_stale(update)
        return None

    if text == CANCEL_BUTTON:
        pending_choice.clear(user_data)
        await update.message.reply_text("Menu closed.", reply_markup=MAIN_KEYBOARD)
        return None
    if text == MENU_BUTTON:
        await show_menu(update, context)
        return None

    if session.flow is None:
        flow = FLOWS_BY_LABEL.get(text)
        if flow is None:
            _park(user_data, None, {})
            await update.message.reply_text("Tap one of the buttons, or ❌ Cancel.",
                                            reply_markup=COMMAND_KEYBOARD)
            return None
        return await _advance(update, user_data, flow, {})

    field = session.flow.fields[len(session.values)]
    value, problem = field.check(text)
    if problem:
        await _ask(update, user_data, session.flow, session.values, problem)
        return None
    return await _advance(update, user_data, session.flow,
                          {**session.values, field.key: value})


def is_stale_tap(text):
    """A menu button arriving with no menu open: one tapped after it lapsed."""
    return text == CANCEL_BUTTON or text in FLOWS_BY_LABEL


async def reply_stale(update):
    await update.message.reply_text(
        f"That menu has closed — a question lapses after "
        f"{pending_choice.PENDING_TTL_SECONDS // 60} minutes. Tap 📋 Menu to start again.",
        reply_markup=MAIN_KEYBOARD)
