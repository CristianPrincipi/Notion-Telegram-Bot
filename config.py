"""
Centralized configuration for David.

Everything that used to be a magic number or an inline dict scattered across
the codebase now lives here. Change a category name, the budget ceiling, or a
shortcut once — and it applies everywhere.
"""

import logging
import os

logger = logging.getLogger(__name__)

# ─── BUDGET ────────────────────────────────────────────────────────────────────
# Monthly budget ceiling, in euros. Override on Railway with BUDGET_CEILING.
BUDGET_CEILING = float(os.environ.get("BUDGET_CEILING", "300"))


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

ANTHROPIC_READ_TIMEOUT = 300   # handed to requests — a 2-hour transcript is slow to summarise
ANTHROPIC_TIMEOUT      = 330   # outer cap; must stay above ANTHROPIC_READ_TIMEOUT
SOURCE_FETCH_TIMEOUT   = 90    # YouTube transcript or article scrape
PDF_PARSE_TIMEOUT      = 120   # PyPDF2 text extraction: CPU-bound and unbounded by itself


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
    "MONTH_ID":          "Notion page ID of the current month; expenses relate to it.",
    "LETTI_ID":          "Notion Books ('Letti') database ID.",
    "LITERATURE_ID":     "Notion page ID of the Literature area; books relate to it.",
    "LEARN_ID":          "Notion Learn database ID for videos, articles, podcasts and PDFs.",
    "ANTHROPIC_API_KEY": "Anthropic API key used to summarise Learn and Implement content.",
}

OPTIONAL_ENV = {
    "SUPADATA_KEY":            "Supadata API key for YouTube transcripts. Without it, `Learn video` fails.",
    "GOOGLE_CREDENTIALS_JSON": "Service-account JSON for Google Calendar. Without it, reminders fail.",
    "GOOGLE_CALENDAR_ID":      "Target calendar. Defaults to 'primary'.",
    "DIET_ID":                 "Notion Diet area database ID. Needed by `Implement ... - Diet`.",
    "BRAIN_ID":                "Notion Brain area database ID. Needed by `Implement ... - Brain`.",
    "FINANCE_ID":              "Notion Finance area database ID. Needed by `Implement ... - Finance`.",
    "BUDGET_CEILING":          "Monthly budget ceiling in euros. Defaults to 300.",
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
