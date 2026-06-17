"""
Budget Pacing (Step 4).

A "you're trending over" escalation. The Morning Briefing already shows the
daily pace tag (💰 €X/Y (on/over pace)); this fires a louder, detailed alert —
with the end-of-month projection and the category driving it — only when, past
the noisy first days of the month, you're projected to blow the ceiling by a
meaningful margin.

No new aggregation lives here: it reuses the numbers compute_budget() already
returns (projected_total, projected_over, top_category).
"""

from config import BUDGET_PACING_MIN_DAY, BUDGET_PACING_THRESHOLD_PCT
from budget import compute_budget


def _should_warn(b: dict | None) -> bool:
    """Warn only past the noisy early-month days, and only for a meaningful
    projected overshoot — so a couple of euros over doesn't ping you."""
    if not b:
        return False
    if b["day"] < BUDGET_PACING_MIN_DAY:
        return False
    return b["projected_over"] >= b["ceiling"] * BUDGET_PACING_THRESHOLD_PCT


def build_pacing_warning() -> str | None:
    """Return the pacing-warning text, or None when no warning is warranted."""
    b = compute_budget()
    if not _should_warn(b):
        return None

    msg = (
        f"📉 Day {b['day']}: €{b['total']:.0f} spent. "
        f"At this pace, you'll hit €{b['projected_total']:.0f} — "
        f"€{b['projected_over']:.0f} over your €{b['ceiling']:.0f} ceiling."
    )
    if b["top_category"]:
        name, amount = b["top_category"]
        msg += f" {name} is the driver (€{amount:.0f})."
    return msg
