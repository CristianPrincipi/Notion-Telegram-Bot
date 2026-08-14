"""
Budget Pacing (Step 4).

A "you're trending over" escalation. The Morning Briefing already shows the
daily pace tag (💰 €X/Y (on/over pace)); this fires a louder, detailed alert —
with the end-of-month projection and the category driving it — only when, past
the noisy first days of the month, you're projected to blow the ceiling by a
meaningful margin.

No new aggregation lives here: it reuses the numbers compute_budget() already
returns (projected_total, projected_over, top_category) — and, since M2, its
error, which this builder passes on instead of going quiet.
"""

from config import BUDGET_PACING_MIN_DAY, BUDGET_PACING_THRESHOLD_PCT
from budget import compute_budget


def _should_warn(b: dict) -> bool:
    """Warn only past the noisy early-month days, and only for a meaningful
    projected overshoot — so a couple of euros over doesn't ping you.

    Takes a dict, never None. The failure case is handled by the caller before
    this is consulted: a predicate that answered False for BOTH "the month is
    fine" and "I could not read the month" is the collapse this module was
    written around, and folding it back in here would restore it one function
    over.
    """
    if b["day"] < BUDGET_PACING_MIN_DAY:
        return False
    return b["projected_over"] >= b["ceiling"] * BUDGET_PACING_THRESHOLD_PCT


def build_pacing_warning() -> tuple:
    """Return (message, error) — the pacing warning, or (None, None) when silent.

    THE THREE STATES ARE NOW DISTINCT, which they were not until compute_budget
    stopped returning a bare None for both of its outcomes:

      (msg,  None)  you are trending over the ceiling
      (None, None)  the month was read and there is nothing to warn about
      (None, err)   the month could NOT be read — reported, not filed under
                    "nothing to warn about"

    The third is the one that matters. A silent pacing job during a Notion outage
    means a month you are overspending goes unreported for as long as the outage
    lasts, and the silence looks exactly like good news.
    """
    b, err = compute_budget()
    if err:
        return None, err
    if not _should_warn(b):
        return None, None

    msg = (
        f"📉 Day {b['day']}: €{b['total']:.0f} spent. "
        f"At this pace, you'll hit €{b['projected_total']:.0f} — "
        f"€{b['projected_over']:.0f} over your €{b['ceiling']:.0f} ceiling."
    )
    if b["top_category"]:
        name, amount = b["top_category"]
        msg += f" {name} is the driver (€{amount:.0f})."
    return msg, None
