"""
Budget computation for David.

Extracted from david.py so that BOTH the bot's existing `B` / weekly-recap
commands AND the new proactive jobs (Morning Briefing, and Budget Pacing in
Step 4) read from one source of truth.

  compute_budget() -> (dict, error)   raw numbers + month pacing
  format_budget(b) -> str             the recap string, built from the dict
  budget()         -> (str, error)    convenience = format_budget(compute_budget())

BOTH FALLIBLE FUNCTIONS RETURN (value, error), and neither ever returns a bare
None. They used to: `None` meant "Notion failed" AND "there is nothing to
report", which is the collapse CLAUDE.md names under "An error is never the same
value as an empty result". It cost the same thing here it cost one layer up —
`briefing._budget_line` dropped the pace line silently during an outage, and
`budget_watch._should_warn` could not fire at all, so a month you were
overspending went unreported for as long as Notion was unhappy.

There is no third state to worry about: a month with no expenses is a perfectly
good dict whose total is 0.0. That is the whole point — the empty month and the
failed read are no longer the same value.

`format_budget` is NOT fallible: it takes a dict and renders it.
"""

import logging
import os
import calendar as _calendar
from datetime import datetime

import pytz

from config import BUDGET_CEILING, EXPENSE_MONTH_RELATION
from month import current_month_id
from clients.notion_client import query_database
from telegram_text import escape_md

logger = logging.getLogger(__name__)

EXPENSES_ID = os.environ.get("EXPENSES_ID")

_TIMEZONE = pytz.timezone("Europe/Rome")


def compute_budget() -> tuple[dict | None, str | None]:
    """Aggregate the current month's expenses and compute month pacing.

    Returns (budget, error) — `(None, error)` if the Notion query fails, and
    `(dict, None)` otherwise. Never `(None, None)`: a month with no expenses is
    a dict whose total is 0.0, so a caller that gets no dict has a real failure
    to report and cannot mistake it for a quiet month.

    On success the dict is:

      {
        "per_category":     {name: amount, ...},
        "top_category":     (name, amount) | None,   # biggest spend category
        "total":            float,   # spent so far this month
        "ceiling":          float,   # monthly budget ceiling
        "remaining":        float,   # ceiling - total
        "day":              int,     # today's day-of-month (Europe/Rome)
        "days_in_month":    int,
        "expected_to_date": float,   # linear pace target = ceiling * day/days_in_month
        "on_pace":          bool,    # total <= expected_to_date
        "projected_total":  float,   # total / day * days_in_month
        "projected_over":   float,   # max(0, projected_total - ceiling)
      }
    """
    # Paginated: Notion caps a query at 100 rows. This feeds the morning briefing
    # and the pacing warning, so an unpaginated query understated both.
    #
    # The month is resolved per call, not read once at import: on the 1st this
    # process keeps running across the boundary, and a module-level constant
    # would have the recap reporting last month's total all of the new month.
    results, err = query_database(
        EXPENSES_ID,
        filter_obj={"property": EXPENSE_MONTH_RELATION,
                    "relation": {"contains": current_month_id()}},
    )
    if err:
        logger.error("compute_budget could not read the expenses: %s", err)
        return None, err

    per_category: dict[str, float] = {}
    total = 0.0
    for page in results:
        props = page.get("properties", {})
        amount = props.get("Amount", {}).get("number", 0) or 0
        total += amount
        cat_multi = props.get("Category", {}).get("multi_select", [])
        category = cat_multi[0].get("name", "Other") if cat_multi else "Other"
        per_category[category] = per_category.get(category, 0) + amount

    top_category = None
    if per_category:
        name = max(per_category, key=per_category.get)
        top_category = (name, per_category[name])

    now = datetime.now(_TIMEZONE)
    day = now.day
    days_in_month = _calendar.monthrange(now.year, now.month)[1]

    expected_to_date = BUDGET_CEILING * day / days_in_month
    projected_total  = (total / day * days_in_month) if day else total

    return {
        "per_category":     per_category,
        "top_category":     top_category,
        "total":            total,
        "ceiling":          BUDGET_CEILING,
        "remaining":        BUDGET_CEILING - total,
        "day":              day,
        "days_in_month":    days_in_month,
        "expected_to_date": expected_to_date,
        "on_pace":          total <= expected_to_date,
        "projected_total":  projected_total,
        "projected_over":   max(0.0, projected_total - BUDGET_CEILING),
    }, None


def format_budget(b: dict) -> str:
    """Render the full monthly-budget recap — identical to the old `B` output.

    Sent with parse_mode="Markdown" by both callers. The category names are
    Notion multi_select values, so renaming one to "Food_2024" in Notion used to
    break the entire recap — every command reporting a total, not just that row.
    """
    lines = ["💰 **Monthly Budget**", "━━━━━━━━━━━━━━━"]
    for cat in sorted(b["per_category"], key=lambda c: b["per_category"][c], reverse=True):
        lines.append(f"**{escape_md(cat)}: €{b['per_category'][cat]:.2f}**")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"**Spent: €{b['total']:.2f}**")
    lines.append(f"**Remaining: €{b['remaining']:.2f}** (of €{b['ceiling']:.0f})")
    return "\n".join(lines)


def budget() -> tuple[str | None, str | None]:
    """compute + format. Returns (recap, error).

    The error is passed through rather than swallowed so the two callers — the
    `B` command and the Sunday recap — can print WHICH failure happened. Both
    used to say "Could not fetch budget from Notion" for every cause, which is
    the one sentence that fits a 401, a 429 and a renamed property equally badly.
    """
    b, err = compute_budget()
    return (format_budget(b), None) if b is not None else (None, err)
