"""
Budget computation for David.

Extracted from david.py so that BOTH the bot's existing `B` / weekly-recap
commands AND the new proactive jobs (Morning Briefing, and Budget Pacing in
Step 4) read from one source of truth.

  compute_budget() -> dict | None   raw numbers + month pacing
  format_budget(b) -> str           the existing recap string, built from the dict
  budget()         -> str | None    convenience = format_budget(compute_budget())

`budget()` keeps the exact contract the old david.py function had (returns the
recap string, or None on a Notion error), so the existing call sites — the `B`
command and send_weekly_budget — keep working unchanged.
"""

import os
import calendar as _calendar
from datetime import datetime

import pytz

from config import BUDGET_CEILING
from notion_client import notion_request, NOTION_BASE

EXPENSES_ID = os.environ.get("EXPENSES_ID")
MONTH_ID    = os.environ.get("MONTH_ID")

_TIMEZONE = pytz.timezone("Europe/Rome")


def compute_budget() -> dict | None:
    """Aggregate the current month's expenses and compute month pacing.

    Returns None if the Notion query fails (same failure signal the old
    budget() used). On success returns:

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
    # NOTE: single query (Notion returns up to 100 rows). The old budget() did
    # the same. If a month can exceed 100 expenses, switch to the paginated
    # query_database() helper introduced in Step 5.
    resp = notion_request(
        "POST",
        f"{NOTION_BASE}/databases/{EXPENSES_ID}/query",
        json={"filter": {"property": "Account", "relation": {"contains": MONTH_ID}}},
    )
    if resp.status_code != 200:
        print(f"[compute_budget] Notion {resp.status_code}: {resp.text[:200]}")
        return None

    results = resp.json().get("results", [])

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
    }


def format_budget(b: dict) -> str:
    """Render the full monthly-budget recap — identical to the old `B` output."""
    lines = ["💰 **Monthly Budget**", "━━━━━━━━━━━━━━━"]
    for cat in sorted(b["per_category"], key=lambda c: b["per_category"][c], reverse=True):
        lines.append(f"**{cat}: €{b['per_category'][cat]:.2f}**")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"**Spent: €{b['total']:.2f}**")
    lines.append(f"**Remaining: €{b['remaining']:.2f}** (of €{b['ceiling']:.0f})")
    return "\n".join(lines)


def budget() -> str | None:
    """compute + format. Returns the recap string, or None on a Notion error.
    Drop-in replacement for the old david.py budget()."""
    b = compute_budget()
    return format_budget(b) if b is not None else None
