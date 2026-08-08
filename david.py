import os
import logging
import re
import asyncio
import pytz
from collections.abc import Callable
from dataclasses import dataclass
from datetime import time
from functools import partial
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from learn import SUPPORTED_TYPES, handle_learn
from implement import handle_implement
from reminder import handle_remind
from notion_ids import handle_diag, handle_find, handle_dbs

import config
import expense_safety
from bot.notify import for_update
from budget import budget
from clients.telegram_files import download_pdf_attachment, validate_pdf_attachment
from config import (
    CATEGORY_MAP, DEFAULT_CATEGORY,
    PROACTIVE_TIMEZONE, SUNDAY, category_help, genre_help,
)
from month import handle_month
from pkm import handle_get
from observability import record_command, record_error, set_correlation_id, setup_logging
from proactive.scheduler import register_all
from services import books, expenses
from telegram_text import reply, send

# Configured at import so config.validate() can still be the first statement in
# __main__ and have somewhere to send its warnings. Level comes from LOG_LEVEL;
# the format carries a per-update correlation ID. See observability.py.
setup_logging()
logger = logging.getLogger("david")


# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
DATABASE_ID = os.environ.get("DATABASE_ID")
CHAT_ID = os.environ.get("CHAT_ID")
LEARN_ID = os.environ.get("LEARN_ID")
DIET_ID = os.environ.get("DIET_ID")
BRAIN_ID = os.environ.get("BRAIN_ID")
FINANCE_ID = os.environ.get("FINANCE_ID")
# EXPENSES_ID, LETTI_ID and LITERATURE_ID left with the code that read them —
# services/expenses.py and services/books.py now read them from the environment
# themselves, the way every other feature module already does (config.py owns the
# contract, not the values). The four above have had no reader in this file for
# some time; they are left alone rather than swept up, along with DATABASE_ID,
# which CLAUDE.md records as deliberately unexplained.


# --- BUDGET --- #
# Imported, not defined here. budget.py owns the aggregation and the recap text,
# and proactive/ already reads its compute_budget() for the morning pace tag and
# the pacing warning. david.py used to carry a second, independent copy: same
# maths, same output, but a second place to fix a pagination or rounding bug and
# a second place to forget. budget.py was written as its replacement and says so
# in its own docstring — the `B` command simply never switched over.
#
# Still reached as david.budget, so the call sites and the spies in the tests are
# unchanged.


# --- DETACHED (BACKGROUND) COMMANDS --- #

def run_detached(context: ContextTypes.DEFAULT_TYPE, update: Update, coro, name: str):
    """Run a long command as a background task instead of awaiting it inline.

    WHY THIS EXISTS
    ---------------
    Moving blocking work onto worker threads freed the event LOOP, but
    python-telegram-bot still processes updates one at a time: it will not look
    at the next update until this handler returns. So a five-minute `Learn video`
    left every other command sitting in the queue behind it, even though the loop
    itself was idle the whole time.

    Detaching only the genuinely long commands fixes that without enabling
    concurrent_updates. Everything else — expenses, budget, quotes, books,
    reminders, diagnostics — stays strictly sequential, so a write followed by a
    read of the same data still cannot be reordered. `Add e` then `B` always
    reports the new total. That guarantee is the reason this is a per-command
    decision and not a global switch.

    WHAT THIS ALLOWS TO OVERLAP
    ---------------------------
    Two detached commands can now run at once, which is exactly what the locks
    are for: two Implements against the same area are refused by the area lock,
    and two against Diet by the Diet lock. Nothing detached here touches the
    expense or calendar paths.

    Uses Application.create_task rather than asyncio.create_task so exceptions
    reach the global error handler instead of vanishing into a dropped task, and
    so a task still in flight is awaited on shutdown rather than killed
    mid-write. Passing `update` is what gives that error handler its context.
    """
    return context.application.create_task(coro, update=update, name=name)


# --- ERROR REPORTING HELPERS --- #
#
# BOTH REPORTERS BELOW SEND PLAIN TEXT. Do not add parse_mode back.
#
# They interpolate an exception string, and Notion 400 bodies and Python
# tracebacks routinely carry unbalanced * _ ` and [. Under parse_mode="Markdown"
# that made the ERROR REPORT ITSELF raise BadRequest, the bare `except` swallowed
# it, and the error being reported was lost — the precise failure these functions
# exist to prevent.
#
# escape_md() is NOT the fix here, which is why these two are the only senders in
# the codebase exempt from telegram_text: the exception text sat inside a `code
# span`, and Markdown v1 does not honour backslash escapes inside code spans.
# Plain text is the only formatting that cannot fail.

async def notify_error(context: ContextTypes.DEFAULT_TYPE, where: str, err: Exception):
    """Send a Telegram message to the owner when something fails silently in the background."""
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ David error in {where}:\n{type(err).__name__}: {err}",
        )
    except Exception:
        # Last resort: the report itself could not be delivered. The log is the
        # only remaining channel, so this must never raise.
        logger.exception("notify_error could not report the failure in %s: %s", where, err)


async def on_error(update, context):
    """Global handler: any unhandled exception in a handler lands here.

    At module scope rather than nested inside __main__ so the suite can drive it —
    the same treatment register_jobs and register_handlers already get.
    """
    err = context.error
    record_error()
    logger.error("Unhandled %s: %s", type(err).__name__, err, exc_info=err)
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"⚠️ David hit an error:\n{type(err).__name__}: {err}",
        )
    except Exception:
        logger.exception("on_error could not report the failure.")


# --- SCHEDULED JOB: SEND BUDGET RECAP --- #
async def send_budget_recap(context: ContextTypes.DEFAULT_TYPE):
    try:
        result_text = await asyncio.to_thread(budget)
        if result_text:
            await send(context.bot, CHAT_ID, result_text)
        else:
            await context.bot.send_message(chat_id=CHAT_ID, text="❌ Could not fetch budget from Notion.")
    except Exception as e:
        await notify_error(context, "send_budget_recap", e)


# --- SCHEDULED JOB REGISTRATION --- #

def register_jobs(application, chat_id) -> bool:
    """Attach every scheduled job. Call once, at startup. Returns True if scheduling is on.

    Two families:
      - the weekly budget recap, defined above
      - the proactive briefings, which live in proactive/ and are attached by
        proactive.scheduler.register_all

    The briefings REPLACE the old send_daily_reminders job rather than joining
    it: the morning briefing owns the 07:30 slot that job used, and the evening
    briefing owns tomorrow's events. Running both would send today's events
    twice at 07:30 and tomorrow's twice (07:30 and 20:00).

    Like register_handlers, this is a function rather than inline __main__ code
    so the wiring is importable and can be tested.
    """
    try:
        job_queue = application.job_queue
        if job_queue is None:
            logger.warning("JobQueue unavailable — scheduled jobs not registered "
                           "(install python-telegram-bot[job-queue]).")
            return False

        # Budget recap — the full per-category breakdown, Sunday 09:30.
        # Distinct from the proactive one-line pace tag and the pacing warning.
        #
        # SUNDAY is a named constant on purpose: a bare integer here is exactly
        # how this call site ran on the wrong days for months (see config.py).
        job_queue.run_daily(
            send_budget_recap,
            time=time(hour=9, minute=30, tzinfo=pytz.timezone(PROACTIVE_TIMEZONE)),
            days=(SUNDAY,),
            name="budget_recap",
        )

        # Morning briefing (07:30), evening briefing (20:00), budget pacing (13:00).
        register_all(application, chat_id)
        return True
    except Exception:
        logger.exception("Scheduled jobs not available — commands still work.")
        return False


# --- ACCESS CONTROL --- #
# David is single-user: it spends the owner's Notion and Anthropic quota and can
# write to their databases, so every command is owner-only. Authorization is a
# python-telegram-bot filter rather than a check inside handle_message, so an
# unauthorized update is dropped by the dispatcher and never reaches handler
# code — a new command cannot forget to check.

def build_owner_filter(owner_id: int):
    """Filter matching only the owner's messages."""
    return filters.User(user_id=owner_id)


def register_handlers(application, owner_id: int):
    """Attach every message handler, all gated on the owner.

    Lives here rather than inline in __main__ so the authorization wiring is
    importable and can be tested against real Update objects. Testing a filter
    rebuilt inside a test would only prove the test agrees with itself.
    """
    owner_only = build_owner_filter(owner_id)

    # LISTEN FOR ANY TEXT MESSAGE... (except commands)
    # ~EDITED_MESSAGE: python-telegram-bot matches edited_message by default, so
    # without it, correcting a typo in an expense re-runs the command and creates
    # a SECOND Notion entry.
    application.add_handler(MessageHandler(
        filters.TEXT & (~filters.COMMAND) & owner_only
        & (~filters.UpdateType.EDITED_MESSAGE),
        handle_message))

    # Same for uploads: editing a file's caption would re-download the PDF and
    # append the quote to Notion a second time.
    application.add_handler(MessageHandler(
        filters.Document.ALL & owner_only & (~filters.UpdateType.EDITED_MESSAGE),
        handle_document))

    # Catch-all for everyone else. Registered LAST: within a handler group PTB
    # stops at the first match, so the owner's messages are taken by the handlers
    # above and never reach this one — and it is scoped to ~owner_only anyway.
    application.add_handler(MessageHandler(~owner_only, handle_unauthorized))


async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log an unauthorized attempt and drop it.

    Deliberately silent — replying would confirm to whoever probed the bot that
    it is live and listening. Nothing here touches Notion or the Anthropic API.
    """
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    content = (getattr(message, "text", None) or getattr(message, "caption", None) or "")

    logger.warning(
        "Unauthorized attempt — user_id=%s username=%s chat_id=%s content=%r",
        getattr(user, "id", None),
        getattr(user, "username", None),
        getattr(chat, "id", None),
        content[:80],
    )


# --- COMMAND ARGUMENT PARSING --- #
# Every command pattern below is matched with re.fullmatch, never re.search: a
# partial match must fail loudly rather than execute a command the user only
# mentioned in passing. That only holds if the incoming text is stripped first,
# which handle_message does — otherwise a trailing space from a phone keyboard
# would defeat every command instead of just `B`.

# Accepts a dot or a comma decimal separator. Italian keyboards produce commas
# by reflex, and the old \d+\.?\d* matched "2" out of "2,20" and dropped the
# rest, recording EUR 2.00 as a success.
AMOUNT = r"(?P<amount>\d+(?:[.,]\d+)?)"


def parse_amount(raw: str):
    """Parse an amount written with either separator. Returns (amount, error)."""
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None, f"❌ Error: '{raw}' is not a valid amount."
    if value <= 0:
        return None, f"❌ Error: amount must be greater than zero, got {value:g}."
    return value, None


def resolve_category(raw):
    """Map a category shortcut to its Notion name. Returns (category, error).

    An ABSENT category falls back to the default. A SUPPLIED but unrecognised one
    is an error — otherwise a typo silently files the expense under Food, which
    is indistinguishable from having meant the default. Mirrors how genre already
    behaves.
    """
    if raw is None or not raw.strip():
        return DEFAULT_CATEGORY, None
    category = CATEGORY_MAP.get(raw.strip().lower())
    if category is None:
        return None, (f"❌ Error: unknown category '{raw.strip()}'. "
                      f"Please use: {category_help()}")
    return category, None


# --- COMMAND HANDLERS --- #
#
# One per registry entry, all with the same shape: (update, context, args),
# where `args` is the match's groupdict(). They are the bodies of what used to be
# the branches of one long if/elif chain in handle_message.
#
# Every downstream call below is resolved through david's own module namespace at
# call time (`add_Expenses(...)`, not a reference captured into the registry),
# which is what keeps the spies in tests/test_router.py pointed at the code the
# bot actually runs.

async def _cmd_help(update, context, args):
    await reply(update, build_help())


async def _cmd_undo(update, context, args):
    notify, notify_md = for_update(update)
    await expenses.run_undo(context.user_data, notify=notify, notify_md=notify_md)


async def _cmd_budget(update, context, args):
    result_text = await asyncio.to_thread(budget)
    if result_text:
        # format_budget escapes the Notion category names it interpolates.
        await reply(update, result_text)
    else:
        await update.message.reply_text("❌ Error: Could not calculate budget.")


async def _cmd_diag(update, context, args):
    await handle_diag(update)


async def _cmd_dbs(update, context, args):
    await handle_dbs(update)


async def _cmd_month(update, context, args):
    await handle_month(update)


async def _cmd_find(update, context, args):
    await handle_find(update, args["query"])


async def _cmd_get(update, context, args):
    await handle_get(update, args["text"])


async def _cmd_remind(update, context, args):
    await handle_remind(update, args["text"])


async def _cmd_add_book(update, context, args):
    notify, notify_md = for_update(update)
    await books.run_add_book(
        args["name"].strip(), args["author"].strip(), args["genre"].strip(),
        notify=notify, notify_md=notify_md)


async def _cmd_add_quote(update, context, args):
    notify, notify_md = for_update(update)
    await books.run_add_quote(
        args["book"].strip(), args["title"].strip(), args["body"].strip(),
        notify=notify, notify_md=notify_md)


async def _cmd_learn(update, context, args):
    # Detached: fetch + Claude can run for minutes. See run_detached.
    run_detached(context, update, handle_learn(update, args["text"]), "learn")


async def _cmd_implement(update, context, args):
    # Detached: the Claude merge alone is tens of seconds, under a page lock.
    run_detached(context, update, handle_implement(update, args["text"]), "implement")


async def _cmd_update_expense(update, context, args):
    name = args["name"].strip()

    amount, err = parse_amount(args["amount"])
    if err:
        await update.message.reply_text(err)
        return

    category, err = resolve_category(args["category"])
    if err:
        await update.message.reply_text(err)
        return

    notify, notify_md = for_update(update)
    await expenses.run_destructive(
        context.user_data, expense_safety.UPDATE, name,
        amount=amount, category=category, notify=notify, notify_md=notify_md)


async def _cmd_delete_expense(update, context, args):
    notify, notify_md = for_update(update)
    await expenses.run_destructive(
        context.user_data, expense_safety.DELETE, args["name"].strip(),
        notify=notify, notify_md=notify_md)


async def _cmd_add_expense(update, context, args):
    name = args["name"].strip()

    amount, err = parse_amount(args["amount"])
    if err:
        await update.message.reply_text(err)
        return

    # --- IF NAME = C -> CARREFOUR (case-insensitive, like the command itself)
    if name.lower() == "c": name = "Carrefour"

    # --- CATEGORY: absent -> default, supplied but unknown -> error
    category, err = resolve_category(args["category"])
    if err:
        await update.message.reply_text(err)
        return

    notify, notify_md = for_update(update)
    await expenses.run_add(name, amount, category, notify=notify, notify_md=notify_md)


# --- THE COMMAND REGISTRY --- #
#
# WHY A REGISTRY AND NOT AN if/elif CHAIN
# ---------------------------------------
# The chain this replaces held three things that could not be read off the page:
# which pattern wins (control-flow order), what each branch parses out
# (group(1)/group(2)/group(3), counted by eye), and what the command claims to do
# (a hand-written help string somewhere else entirely). The last one had already
# drifted: help advertised `Learn recipe`, which is not in learn.SUPPORTED_TYPES
# and answers "Unknown type recipe", it left out `Learn podcast` which does work,
# and it never mentioned that `Implement … - Diet` takes a different path from
# every other area.
#
# So all three now come from one declaration per command:
#   pattern      — compiled, matched with fullmatch, NAMED groups only
#   handler      — the branch body, above
#   help         — what `h` prints, GENERATED from this list (see build_help)
#   destructive  — see below
#
# NAMED GROUPS, NOT POSITIONAL. `U e (.+?) (\d+…)(?:\s+(\w+))?` fed group(3) to
# the category and group(2) to the amount; adding an optional group anywhere to
# their left silently renumbered both, and the result would have been a wrong
# amount written to Notion, not an exception.
#
# ORDER IS STILL PRECEDENCE — dispatch takes the first pattern that fullmatches,
# so this list IS the precedence table the order of the `if`s used to be. It is
# arranged for reading (and for the help it generates) rather than to resolve
# conflicts, which is sound only while there are none to resolve: every pattern
# is anchored on a distinct literal prefix (`add b`, `add q`, `add e`, `u e`,
# `d e`, `undo`, `learn`, …), so under fullmatch no string satisfies two. That is
# not left as an assurance — tests/test_router.py drives every input in its table
# through every pattern and fails if any input matches more than one.

@dataclass(frozen=True)
class Help:
    """How one command presents itself in the generated help message."""

    label: str                            # "📖 *ADD BOOK*" — emoji + bold name
    usage: tuple[str, ...] = ()           # rendered as `code` spans
    notes: tuple[str, ...] = ()           # rendered as _italic_ lines beneath
    inline: bool = False                  # "LABEL — `usage`" on one line
    group: str = ""                       # commands sharing a key share a block


@dataclass(frozen=True)
class HelpGroup:
    """A block several commands render into together."""

    label: str | None = None              # set when the members share ONE line
    notes: tuple[str, ...] = ()           # italic lines closing the block


@dataclass(frozen=True)
class Command:
    """One thing David answers to."""

    name: str                             # the trigger, verbatim: "Add e", "U e"
    pattern: re.Pattern
    handler: Callable                     # async (update, context, args) -> None
    help: Help | None = None
    # DESTRUCTIVE = mutates or removes a row that already exists, so it must go
    # through expense_safety's find-then-choose path instead of writing straight
    # away (Hard Rule 4). It is not a second copy of that wiring: the guards are
    # driven by the expense_safety.UPDATE/DELETE action the handler passes to
    # _start_destructive_expense, and there never was a hardcoded list to
    # replace. What the flag drives is the shared warning in the generated help,
    # and tests/test_router.py asserts the flag and the guarded path agree — a
    # destructive command that skipped the lookup turns that test red.
    destructive: bool = False


# The Learn types come from learn.SUPPORTED_TYPES, not from a list written out
# here — that is the exact drift this registry exists to make impossible. The
# hints are decoration: a type with no hint still prints, a hint for a type that
# is not supported never does.
_LEARN_ARG_HINTS = {
    "video":   "https://youtu.be/...",
    "article": "https://...",
    "book":    "[Title]",
    "podcast": "https://...",
    "pdf":     "",
}

_LEARN_USAGE = tuple(
    f"Learn {content_type} {_LEARN_ARG_HINTS.get(content_type, '[source]')}".strip()
    for content_type in SUPPORTED_TYPES
)

HELP_GROUPS = {
    "notion_ids": HelpGroup(label="🩺 *FIND NOTION IDs*"),
    "expense":    HelpGroup(notes=(f"Categories: {category_help()}",)),
}

COMMANDS = [
    Command(
        name="Add b",
        pattern=re.compile(r"add b (?P<name>.+?) - (?P<author>.+?) - (?P<genre>.+)", re.I),
        handler=_cmd_add_book,
        help=Help("📖 *ADD BOOK*",
                  usage=("Add b [Name] - [Author] - [Genre]",),
                  notes=(f"Genres: {genre_help()}",)),
    ),
    Command(
        name="Add q",
        # Two shapes, one pattern: a full quote, or the "[Begin] / [End]" markers
        # that only mean something on an attached PDF — the handler tells the
        # second apart and explains the upload flow.
        pattern=re.compile(r"add q (?P<book>.+?) - (?P<title>.+?) - (?P<body>[\s\S]+)", re.I),
        handler=_cmd_add_quote,
        help=Help("🖋️ *ADD QUOTE*",
                  usage=("Add q [Book] - [Title] - [Full quote]",
                         "Add q [Book] - [Title] - [Begin text] / [End text]"),
                  notes=("The second form reads the quote out of a PDF — attach it "
                         "and send the command as the caption",)),
    ),
    Command(
        name="Remind",
        # Only the prefix is checked here; validating the date and time is
        # handle_remind's job, so a malformed one still reaches it and gets a
        # usage message instead of "I didn't get that".
        pattern=re.compile(r"(?P<text>remind\s+.+)", re.I),
        handler=_cmd_remind,
        help=Help("📅 *REMINDER*",
                  usage=("Remind [Name] [DD.MM] - [HH.MM]",
                         "Remind [Name] [DD.MM.YYYY] - [HH.MM]",
                         "Remind [Name] t [HH]"),
                  notes=("e.g. Remind Dentist 12.06 - 14.30, or Remind Dentist t 10 "
                         "for tomorrow at 10:00",
                         "t (or tomorrow) is the day after today; a bare hour means "
                         "o'clock; the dash is optional",
                         "Without a year: a date over a day past means next year, and "
                         "one inside that window is queried rather than guessed")),
    ),
    Command(
        name="Add e",
        pattern=re.compile(rf"add e (?P<name>.+?) {AMOUNT}(?:\s+(?P<category>\w+))?", re.I),
        handler=_cmd_add_expense,
        help=Help("💵 *ADD EXPENSE*",
                  usage=("Add e [Name] [Amount] [Category]",),
                  inline=True, group="expense"),
    ),
    Command(
        name="U e",
        pattern=re.compile(rf"u e (?P<name>.+?) {AMOUNT}(?:\s+(?P<category>\w+))?", re.I),
        handler=_cmd_update_expense,
        help=Help("✏️ *UPDATE EXPENSE*",
                  usage=("U e [Name] [Amount] [Category]",),
                  inline=True, group="expense"),
        destructive=True,
    ),
    Command(
        name="D e",
        pattern=re.compile(r"d e (?P<name>.+)", re.I),
        handler=_cmd_delete_expense,
        help=Help("🗑️ *DELETE EXPENSE*",
                  usage=("D e [Name]",),
                  inline=True, group="expense"),
        destructive=True,
    ),
    Command(
        name="undo",
        pattern=re.compile(r"undo", re.I),
        handler=_cmd_undo,
        help=Help("↩️ *UNDO*", usage=("undo",), inline=True,
                  notes=("Reverses the last delete or update",)),
    ),
    Command(
        name="B",
        pattern=re.compile(r"b", re.I),
        handler=_cmd_budget,
        help=Help("💰 *BUDGET*", usage=("B",), inline=True),
    ),
    Command(
        name="Month",
        # Idempotent, so sending it twice is harmless; the scheduled job runs the
        # exact same call at 00:05 every night.
        pattern=re.compile(r"month", re.I),
        handler=_cmd_month,
        help=Help("🗓️ *MONTH PAGE*", usage=("Month",), inline=True,
                  notes=("Rolls over automatically on the 1st; this forces a check",)),
    ),
    Command(
        name="Diag",
        pattern=re.compile(r"diag", re.I),
        handler=_cmd_diag,
        help=Help("", usage=("Diag",), group="notion_ids"),
    ),
    Command(
        name="Find",
        pattern=re.compile(r"find\s+(?P<query>.+)", re.I),
        handler=_cmd_find,
        help=Help("", usage=("Find [name]",), group="notion_ids"),
    ),
    Command(
        name="DBs",
        pattern=re.compile(r"dbs", re.I),
        handler=_cmd_dbs,
        help=Help("", usage=("DBs",), group="notion_ids"),
    ),
    Command(
        name="Learn",
        pattern=re.compile(r"(?P<text>learn\s+\w+[\s\S]*)", re.I),
        handler=_cmd_learn,
        help=Help("🧠 *LEARN*", usage=_LEARN_USAGE,
                  notes=("Learn pdf needs the file attached, with the command as the caption",)),
    ),
    Command(
        name="Implement",
        pattern=re.compile(r"(?P<text>implement\s+.+\s*-\s*.+)", re.I),
        handler=_cmd_implement,
        help=Help("🔧 *IMPLEMENT*",
                  usage=("Implement [Page Name] - [Area]",),
                  notes=("Merges a Learn page into an Area Manual",
                         "Area `Diet` is different: it merges into the structured Diet "
                         "page (categories > rows > attributes), not a flat Manual")),
    ),
    Command(
        name="Get",
        # The separator is a SPACE-hyphen-SPACE, matching pkm.GET_PATTERN, so a
        # topic containing a hyphen ("Step-by-Step Breakdown") is not split at
        # it. Without the " - [Area]" it is not a command, exactly like
        # `Implement`.
        #
        # Runs INLINE, not detached: it is read-only, so it cannot reorder
        # against a write the way a detached command could. The slow case is a
        # toggle manual (Diet), where build_index walks the tree one request per
        # heading.
        pattern=re.compile(r"(?P<text>get\s+.+\s+-\s+.+)", re.I),
        handler=_cmd_get,
        help=Help("🔎 *GET*",
                  usage=("Get [Topic] - [Area]", "Get ? - [Area]"),
                  notes=("? in place of the topic lists every topic in that area",)),
    ),
    Command(
        name="h",
        pattern=re.compile(r"h|help|aiuto", re.I),
        handler=_cmd_help,
        help=Help("❓ *HELP*", usage=("h", "help", "aiuto"), inline=True),
    ),
]


# --- THE GENERATED HELP MESSAGE --- #

def _render_entry(entry: Help) -> list[str]:
    """One command's own lines: label, usage, notes."""
    spans = " · ".join(f"`{usage}`" for usage in entry.usage)
    if entry.inline:
        lines = [f"{entry.label} — {spans}" if spans else entry.label]
    else:
        lines = [entry.label] + [f"`{usage}`" for usage in entry.usage]
    return lines + [f"_{note}_" for note in entry.notes]


def build_help() -> str:
    """The `h` message, built from COMMANDS.

    GENERATED rather than written out so it cannot drift from what the bot
    actually answers to, which is the whole point — the hand-written version it
    replaces advertised `Learn recipe` (rejected by handle_learn as an unknown
    type), omitted `Learn podcast`, and said nothing about `Implement … - Diet`
    taking a different path.

    Commands appear in registry order, grouped by Help.group; a group with a
    label collapses its members onto one line, and one with notes closes on them.
    """
    blocks: dict[str, list[Command]] = {}
    for command in COMMANDS:
        if command.help is None:
            continue
        blocks.setdefault(command.help.group or command.name, []).append(command)

    rendered = []
    for key, members in blocks.items():
        group = HELP_GROUPS.get(key, HelpGroup())

        if group.label:
            spans = " · ".join(f"`{usage}`"
                               for member in members for usage in member.help.usage)
            lines = [f"{group.label} — {spans}"]
        else:
            lines = [line for member in members for line in _render_entry(member.help)]

        notes = list(group.notes)
        # The destructive flag earning its keep: the guard that makes `U e` and
        # `D e` safe is only reassuring if you know it is there, and naming the
        # commands from the flag means a third one cannot be added without this
        # sentence growing to cover it.
        guarded = [member.name for member in members if member.destructive]
        if guarded:
            notes.append(" and ".join(f"`{name}`" for name in guarded)
                         + " search this month only. Several matches → I list them "
                           "and wait for a number.")
        lines += [f"_{note}_" for note in notes]

        rendered.append("\n".join(lines))

    return "\n\n".join(rendered)


# --- TELEGRAM MESSAGE HANDLER ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Tag every log line produced while handling this update — including from the
    # worker threads and detached tasks it spawns — with the update's ID, so one
    # command's whole trace is greppable even when two overlap.
    set_correlation_id(getattr(update, "update_id", None))
    record_command()

    # Stripped once, here: every pattern is a fullmatch, so stray leading or
    # trailing whitespace would otherwise miss every command.
    user_text = (update.message.text or "").strip()
    logger.info("Received: %s", user_text)

    # --- PENDING CHOICE: a bare number answering a printed list of matches ---
    # AHEAD OF THE REGISTRY, and not a Command itself, because it is the only
    # thing David answers that depends on STATE rather than on the text: while a
    # list is live the reply to it is a plain integer, which every pattern
    # rejects, so it would fall through to "I didn't get that" with the list left
    # unanswered. Guarded on there BEING a live list, so a stray "2" with nothing
    # pending stays an unrecognised message rather than a command with no visible
    # effect. See expense_safety.py.
    if expense_safety.has_pending(context.user_data):
        selection = expense_safety.parse_selection(user_text)
        if selection is not None:
            notify, notify_md = for_update(update)
            await expenses.run_selection(context.user_data, selection,
                                         notify=notify, notify_md=notify_md)
            return

    # fullmatch, never search: a partial match must fail loudly rather than
    # execute a command the user only mentioned in passing.
    for command in COMMANDS:
        match = command.pattern.fullmatch(user_text)
        if match:
            await command.handler(update, context, match.groupdict())
            return

    await update.message.reply_text("❓ I didn't get that. Try: 'Add e Carrefour 2.20'")


# --- UPLOAD WORK (run detached; see run_detached) --- #
# Split out of handle_document so the slow half — a download capped at 2 minutes,
# PyPDF2 parsing, and for Learn a full Claude summarisation — can run as a
# background task while the cheap validation stays inline and rejects a bad file
# immediately.

async def _learn_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            doc, caption: str):
    await update.message.reply_text("⏳ Downloading your PDF…")
    file_bytes, err = await download_pdf_attachment(context, doc)
    if err:
        await update.message.reply_text(err)
        return
    await handle_learn(update, caption, file_bytes=file_bytes)


# --- HANDLER FUNCTION FOR PDF ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file uploads. Dispatches based on the message caption.

    Supported captions:
      Learn pdf                                          → summarise PDF, save to Learn DB
      Add q [Book] - [Title] - [Begin text] / [End text] → extract quote from attached PDF
    """
    # Same correlation tagging as handle_message — a PDF upload is a command too,
    # and it is the one most likely to run detached and interleave with another.
    set_correlation_id(getattr(update, "update_id", None))
    record_command()

    doc     = update.message.document
    caption = (update.message.caption or "").strip()
    logger.info("Received document with caption: %s", caption)

    # ── Learn pdf ──────────────────────────────────────────────────────────────
    if re.match(r"(?i)learn\s+pdf", caption):
        # Validated inline — it is pure and network-free, so a wrong file type or
        # an oversized upload is refused immediately rather than from a task.
        # download_pdf_attachment checks again; calling it twice costs nothing.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return
        run_detached(context, update,
                     _learn_pdf_upload(update, context, doc, caption), "learn-pdf")
        return

    # ── Add q [Book] - [Title] - [Begin] / [End]  (extract quote from PDF) ────
    quote_pdf_match = re.match(r"(?i)add q (.+?) - (.+?) - (.+?) / (.+)", caption)
    if quote_pdf_match:
        # Checked before the Notion lookup so a wrong-format file costs no API call.
        err = validate_pdf_attachment(doc)
        if err:
            await update.message.reply_text(err)
            return

        # The download is BOUND, not performed: services/books.py decides when
        # (after the book is found, so a caption naming a book you do not own
        # costs no bytes) without ever seeing a PTB context of its own.
        notify, notify_md = for_update(update)
        run_detached(
            context, update,
            books.run_quote_from_pdf(
                quote_pdf_match.group(1).strip(),   # book name
                quote_pdf_match.group(2).strip(),   # quote title
                quote_pdf_match.group(3).strip(),   # begin text
                quote_pdf_match.group(4).strip(),   # end text
                download=partial(download_pdf_attachment, context, doc),
                notify=notify, notify_md=notify_md,
            ),
            "quote-pdf")
        return

    # ── Unknown caption ────────────────────────────────────────────────────────
    await reply(
        update,
        "📎 File received. Supported captions:\n\n"
        "`Learn pdf` — summarise and save to Learn DB\n"
        "`Add q [Book] - [Title] - [Begin] / [End]` — extract quote from this PDF",
    )


# --- START THE BOT ---
if __name__ == '__main__':
    config.validate()

    # UPDATES STAY SEQUENTIAL — do not add .concurrent_updates() here.
    #
    # This is a decision, not a leftover. Responsiveness is bought a different
    # way: the long commands (Learn, Implement, the PDF uploads) are dispatched
    # with run_detached, so they no longer hold up the queue, while every fast
    # command still runs to completion before the next update is looked at.
    #
    # Global concurrency would buy nothing on top of that and would cost the one
    # guarantee sequential dispatch still provides: ORDERING. `Add e Carrefour 5`
    # followed by `B` must report the new total. Locks cannot give that back —
    # they stop two cycles interleaving, they do not decide which runs first. So
    # the choice was per-command, and it is asserted in tests/test_concurrency.py
    # (test_fast_commands_stay_sequential).
    #
    # The locks are in place either way — expenses, calendar, and both Implement
    # flows — because run_detached does let two long commands overlap.
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # --- SCHEDULED JOBS ---
    register_jobs(application, CHAT_ID)

    # --- HANDLERS (owner-only) ---
    # config.validate() already proved OWNER_ID is set and numeric.
    register_handlers(application, int(OWNER_ID))

    # --- GLOBAL ERROR HANDLER ---
    # Any unhandled exception in a handler lands here and is reported to you,
    # instead of dying silently in the Railway logs. Defined at module scope.
    application.add_error_handler(on_error)

    logger.info("🤖 David online!")
    application.run_polling()
