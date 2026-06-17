"""
Centralized configuration for David.

Everything that used to be a magic number or an inline dict scattered across
the codebase now lives here. Change a category name, the budget ceiling, or a
shortcut once — and it applies everywhere.
"""

import os

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
