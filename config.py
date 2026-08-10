"""
Centralized configuration for David.

Everything that used to be a magic number or an inline dict scattered across
the codebase now lives here. Change a category name, the budget ceiling, or a
shortcut once — and it applies everywhere.
"""

import logging
import os
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ─── BUDGET ────────────────────────────────────────────────────────────────────
# Monthly budget ceiling, in euros. Override on Railway with BUDGET_CEILING.
BUDGET_CEILING = float(os.environ.get("BUDGET_CEILING", "300"))

# The Expenses column that relates a row to its month page. month.py follows this
# relation to discover WHICH database the month pages live in, so it is named
# once here rather than spelled out at each call site (notion_ids.py checks the
# same column when it validates the Expenses schema).
EXPENSE_MONTH_RELATION = "Account"


# ─── SHORTCUT MAPS ─────────────────────────────────────────────────────────────
# Single source of truth for every command shortcut. Previously these were
# redefined inline in handle_message AND handle_document (genre map appeared twice).

GENRE_MAP = {
    "s":  "Satira",
    "h":  "History",
    "m":  "Manga",
    "p":  "Poetry",
    "a":  "Adventure",
    "ph": "Philosophy",
}

CATEGORY_MAP = {
    "s": "Shopping",
    "f": "Food",
    "g": "Gift",
    "o": "Other",
}

PRIORITY_MAP = {
    "l": "Low",
    "m": "Mid",
    "h": "High",
}

# Default category when none is supplied on an expense
DEFAULT_CATEGORY = "Food"


# ─── LEARN CONTENT TYPES ───────────────────────────────────────────────────────
# What `Learn [type] [source]` accepts, the page icon each type gets, and which
# database it is filed in. One map rather than the two that lived in learn.py
# (TYPE_EMOJI, plus the dict inside _get_db_id) precisely because they were keyed
# identically and had to agree: a type in one and not the other is a command that
# either files nowhere or files with the wrong icon, and nothing would say so.
#
# The VALUE is the env var's NAME, not its value. config.py does not read feature
# IDs — each module reads its own os.environ (see learn._get_db_id, and
# implement.get_area_db_id, which derives its key the same way).

class LearnType(NamedTuple):
    emoji:  str
    db_env: str


LEARN_TYPES = {
    "video":   LearnType("🎬",  "LEARN_ID"),
    "article": LearnType("📰",  "LEARN_ID"),
    "book":    LearnType("📚",  "LETTI_ID"),
    "podcast": LearnType("🎙️", "LEARN_ID"),
    "pdf":     LearnType("📄",  "LEARN_ID"),
}

DEFAULT_LEARN_EMOJI = "📖"


def genre_help() -> str:
    """Human-readable list of genre shortcuts for help/error messages."""
    return " · ".join(GENRE_MAP.keys())


def category_help() -> str:
    return " · ".join(CATEGORY_MAP.keys())


def priority_help() -> str:
    return " · ".join(PRIORITY_MAP.keys())


# ─── BLOCKING-CALL TIMEOUTS ────────────────────────────────────────────────────
# Every blocking call is now run on a worker thread via asyncio.to_thread, so a
# slow one no longer freezes the bot. These are the OUTER caps (asyncio.wait_for)
# on the operations that could otherwise run forever. They are separate from, and
# larger than, the per-request timeouts handed to requests.
#
# An outer cap is NOT redundant with a requests timeout. requests' read timeout
# is per socket read and restarts on every byte, so a server trickling one byte
# at a time never trips it; wait_for bounds the whole operation regardless.
#
# ONLY READS ARE CAPPED THIS WAY. asyncio.wait_for cancels the awaiting task but
# cannot cancel the worker thread — the blocking call runs to completion in the
# background either way. Timing out a Notion WRITE would therefore tell the user
# it failed while it was still in flight. Notion calls are already bounded by
# notion_request's per-request timeout and its bounded retries, so they are
# offloaded to a thread but not wrapped in wait_for.

ANTHROPIC_READ_TIMEOUT = 300   # per request — a 2-hour transcript is slow to summarise
ANTHROPIC_TIMEOUT      = 330   # outer cap; must stay above ANTHROPIC_READ_TIMEOUT
SOURCE_FETCH_TIMEOUT   = 90    # YouTube transcript or article scrape
PDF_PARSE_TIMEOUT      = 120   # PyPDF2 text extraction: CPU-bound and unbounded by itself


# ─── ANTHROPIC ─────────────────────────────────────────────────────────────────
# One model name for the whole bot. It was hard-coded three times — in learn.py,
# implement.py and implement_diet.py — so an upgrade meant finding all three.
ANTHROPIC_MODEL = "claude-sonnet-4-5"

ANTHROPIC_MAX_TOKENS = 8192    # output cap. Stays well under the ~16k above which
                               # the SDK refuses a non-streaming request outright.

# INPUT cap: how much source text one summarisation may be given. ~100k chars
# covers a 2-hour video transcript in a single call.
#
# This constant is owned by what CONSUMES the text, not by what fetches it, and
# there is exactly one of it because the alternative already shipped: the article
# extractor capped at 12,000 while the summariser accepted 100,000, so every
# article over ~12k chars was summarised from its first eighth and nothing said
# so. Two independent numbers describing one budget drift the moment either
# moves. An extractor must NOT pre-truncate to this value either — then
# `run_learn` could not tell a source of exactly the budget from one cut down to
# it, and the warning below would be unreliable at its own boundary.
#
# services.learn.run_learn is the single place text is cut to fit, and it says so
# in the reply when it does. A partial summary you are told about is a different
# thing from a thin Manual entry you discover months later.
SUMMARY_INPUT_CHARS = 100_000

# Retries on 429 / 5xx / 529 and network errors, with exponential backoff. The
# Notion client has had this for ages; the Anthropic calls did not, so a single
# rate-limit response after two minutes of transcript fetching threw the whole
# job away and the re-run paid for the tokens a second time.
#
# NOTE the interaction with ANTHROPIC_TIMEOUT: worst case is
# ANTHROPIC_READ_TIMEOUT × (ANTHROPIC_MAX_RETRIES + 1), which is longer than the
# outer wait_for. That is accepted rather than tuned away — retries are triggered
# by 429/529, which fail in milliseconds, so the worst case needs three
# separately-stalled generations. If wait_for does fire first the user is told it
# gave up, and nothing has been written to Notion at that point either way.
ANTHROPIC_MAX_RETRIES = 3

# ─── DAILY SPEND GUARD ─────────────────────────────────────────────────────────
# A runaway loop or a habit of re-running `Learn` on long videos is invisible
# until the invoice arrives. The guard refuses new calls once the day's estimated
# spend passes the threshold, and the refusal reaches Telegram as the command's
# error — every Anthropic call in David is user-initiated, so there is always
# someone at the keyboard to read it.
#
# Rates are per MILLION tokens and MUST be updated alongside ANTHROPIC_MODEL —
# they are the published Sonnet-tier rates, not a per-model lookup. The guard
# fails toward refusing, so a stale rate over-counts rather than under-counts.
ANTHROPIC_INPUT_COST_PER_MTOK  = 3.00
ANTHROPIC_OUTPUT_COST_PER_MTOK = 15.00

ANTHROPIC_DAILY_BUDGET_USD = float(os.environ.get("ANTHROPIC_DAILY_BUDGET_USD", "5"))


# ─── WEEKDAYS ──────────────────────────────────────────────────────────────────
# python-telegram-bot's JobQueue.run_daily(days=...) numbers 0-6 as
# SUNDAY-saturday. It was monday-sunday before PTB v20, and a call site that
# kept the old "0=Mon" comment ran the budget recap on Friday and Saturday for
# months. Never write a bare integer at a run_daily call site — use these.
SUNDAY    = 0
MONDAY    = 1
TUESDAY   = 2
WEDNESDAY = 3
THURSDAY  = 4
FRIDAY    = 5
SATURDAY  = 6


# ─── PROACTIVE SYSTEM ──────────────────────────────────────────────────────────
# Timezone + schedule for proactive (push) jobs. Add new schedules here as you
# build out the features.
PROACTIVE_TIMEZONE      = "Europe/Rome"

# Morning Briefing — today's events + budget pace (the slot the old daily
# reminder job used).
MORNING_BRIEFING_HOUR   = 7
MORNING_BRIEFING_MINUTE = 30

# Evening Briefing — tomorrow's events (enough lead time to prep tonight).
EVENING_BRIEFING_HOUR   = 20
EVENING_BRIEFING_MINUTE = 0

# Budget Pacing — a "trending over" alert (Step 4). Fires only when, past the
# noisy early-month days, you're projected to blow the ceiling by a meaningful
# margin. The Morning Briefing already shows the daily pace tag; this is the
# louder mid-day escalation with the projection + driver category.
BUDGET_PACING_HOUR          = 13     # 13:00 — a mid-day checkpoint, separate from both briefings
BUDGET_PACING_MINUTE        = 0
BUDGET_PACING_MIN_DAY       = 5      # skip days 1-4: total/day projections are too noisy that early
BUDGET_PACING_THRESHOLD_PCT = 0.05   # only warn if projected to exceed the ceiling by >= 5%

# Month Rollover — points the expense month at the new month's Notion page on the
# 1st, creating or renaming that page if needed (see month.py).
#
# 00:05 local, and DAILY rather than monthly: running it daily is what makes a
# FAILED or MISSED rollover retry tomorrow instead of waiting a month for the next
# firing. The job RUNS every night; it only SPEAKS when the run lands on a month
# David was not already on — see Rollover.rolled_over in month.py.
#
# 00:05 is also chosen to sit clear of Europe/Rome's DST switches (02:00→03:00 in
# March, 03:00→02:00 in October): a job scheduled inside that hour is either
# skipped or run twice on those two nights a year.
MONTH_ROLLOVER_HOUR   = 0
MONTH_ROLLOVER_MINUTE = 5

# Weekly Heartbeat — proof David is alive and its dependencies answer.
#
# It ALWAYS sends, which is the whole design: a probe that only speaks on failure
# is equally silent when the bot is dead, the JobQueue never registered, or the
# Telegram token was revoked — the outages most worth catching. The message is the
# liveness proof, so a MISSING Sunday message is itself the alarm.
#
# Sunday 20:30: late enough to sit clear of the 20:00 daily evening briefing, and
# on a weekday you have a weekly rhythm for, so an absence is noticeable.
HEARTBEAT_DAY    = SUNDAY
HEARTBEAT_HOUR   = 20
HEARTBEAT_MINUTE = 30


# ─── ENVIRONMENT CONTRACT ──────────────────────────────────────────────────────
# Every environment variable David reads, in one place. The descriptions are the
# same text the README table and the startup error message use, so there is only
# one copy to keep accurate.

REQUIRED_ENV = {
    "TELEGRAM_TOKEN":    "Telegram bot token from @BotFather.",
    "OWNER_ID":          "Numeric Telegram user ID allowed to use the bot. Everyone else is ignored.",
    "CHAT_ID":           "Telegram chat that receives scheduled briefings and error reports.",
    "NOTION_KEY":        "Notion internal integration secret.",
    "EXPENSES_ID":       "Notion Expenses database ID.",
    "LETTI_ID":          "Notion Books ('Letti') database ID.",
    "LITERATURE_ID":     "Notion page ID of the Literature area; books relate to it.",
    "LEARN_ID":          "Notion Learn database ID for videos, articles, podcasts and PDFs.",
    "ANTHROPIC_API_KEY": "Anthropic API key used to summarise Learn and Implement content.",
}

OPTIONAL_ENV = {
    # OPTIONAL SINCE month.py STOPPED DEPENDING ON IT. It used to be required,
    # and was contradicted by the module that reads it: month.py treats it as a
    # first-boot SEED and resolves the real page from Notion by title, so David
    # starts and runs correctly with it unset. Leaving it in REQUIRED_ENV meant a
    # deploy without it died at startup over a value nothing needs — required by
    # contract, optional in practice, which is the kind of gap that gets
    # "resolved" by pasting a stale page ID back in to make the error go away.
    "MONTH_ID":                ("Fallback Notion page ID for the current month, used only if "
                                "Notion cannot be reached. David resolves the real page itself."),
    "SUPADATA_KEY":            "Supadata API key for YouTube transcripts. Without it, `Learn video` fails.",
    "GOOGLE_CREDENTIALS_JSON": "Service-account JSON for Google Calendar. Without it, reminders fail.",
    "GOOGLE_CALENDAR_ID":      "Target calendar. Defaults to 'primary'.",
    "DIET_ID":                 "Notion Diet area database ID. Needed by `Implement ... - Diet`.",
    "BRAIN_ID":                "Notion Brain area database ID. Needed by `Implement ... - Brain`.",
    "FINANCE_ID":              "Notion Finance area database ID. Needed by `Implement ... - Finance`.",
    "BUDGET_CEILING":          "Monthly budget ceiling in euros. Defaults to 300.",
    "MONTHS_DB_ID":            ("Notion database the month pages live in. Discovered from the "
                                f"Expenses '{EXPENSE_MONTH_RELATION}' relation when unset."),
    "ANTHROPIC_DAILY_BUDGET_USD": ("Estimated Anthropic spend allowed per day before Learn and "
                                   "Implement are refused. Defaults to 5."),
    "ANTHROPIC_SPEND_FILE":       ("Where the running daily spend is recorded. "
                                   "Defaults to .anthropic_spend.json."),
    "LOG_LEVEL":                  ("Logging verbosity: DEBUG, INFO, WARNING, ERROR or "
                                   "CRITICAL. Defaults to INFO."),
}


def validate() -> None:
    """Fail fast when the environment is incomplete. Call first thing at startup.

    Reads os.environ directly rather than the module constants above, so it
    reports the real state of the process.

    WHY: every ID below is read at import time, so a missing one becomes None and
    then a confusing downstream symptom — a missing NOTION_KEY builds the header
    "Bearer None" and surfaces as a 401 hours later, against whichever command
    happened to run first. Every problem is collected and reported in ONE exit so
    a misconfigured deploy takes one fix, not one redeploy per variable.

    Raises SystemExit if anything required is missing or malformed. Optional
    variables only log a warning: the bot runs fine without them, minus the
    feature each one powers.
    """
    problems = []

    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        problems.append(f"Missing {len(missing)} required environment variable(s):")
        problems += [f"    {name} — {REQUIRED_ENV[name]}" for name in missing]

    # OWNER_ID gates every inbound message and is turned into an int to build the
    # Telegram filter, so a non-numeric value is just as fatal as a missing one —
    # and would otherwise crash with a bare ValueError during handler setup.
    owner_id = os.environ.get("OWNER_ID", "").strip()
    if owner_id and not _is_int(owner_id):
        problems.append(
            f"OWNER_ID must be a numeric Telegram user ID, got {owner_id!r}. "
            "Send /start to @userinfobot to find yours."
        )

    if problems:
        raise SystemExit(
            "David cannot start — the environment is incomplete.\n\n"
            + "\n".join(problems)
            + "\n\nSet these in the Railway service variables, then redeploy."
        )

    for name, purpose in OPTIONAL_ENV.items():
        if not os.environ.get(name, "").strip():
            logger.warning("%s is not set — %s", name, purpose)


def _is_int(value: str) -> bool:
    try:
        int(value)
    except ValueError:
        return False
    return True
