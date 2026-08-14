"""
The Learn nudge (Step 6) — pages you saved and never merged into a Manual.

WHY IT EXISTS. Both Implement paths tick an `Implemented` checkbox on the source
page, and until now nothing read it. So the capture half of the knowledge
pipeline was automated and the act-on-it half depended on remembering which
pages you saved months ago and never came back to — which is the half that
decides whether any of it was worth summarising. This closes the loop, and as a
side effect makes the checkbox mean something.

WHAT "PENDING" MEANS, AND WHERE IT IS DECIDED. `Implemented` unchecked AND
created more than LEARN_NUDGE_STALE_DAYS ago, both in ONE Notion filter rather
than fetched and sieved here. Notion can evaluate the predicate, and a
client-side copy of it would be a second definition of the same word.

That has a consequence worth knowing before reading the tests: the boundary is
Notion's. `before` is strict, so a page created exactly N days ago is not yet
named. No test here can assert that from the OUTPUT — a fake Notion returns
whatever rows it is handed — so the test asserts the REQUEST: the cutoff and the
operator, which is the whole of what decides it.

THE MISSING-COLUMN CASE IS A REFUSAL, deliberately the opposite of the
`Source URL` check in services/learn.py. There, the safeguard is optional and
refusing would cost you the command, so it degrades and says so. Here the
checkbox IS the feature: with no column there is nothing to degrade TO, and both
alternatives are worse than reporting — listing every Learn page ever is noise,
and listing nothing is indistinguishable from "you are all caught up", which is
exactly the collapse (value, error) exists to prevent.

Returns (text, error) like every builder here, and never sends: that is
scheduler.py's job.
"""

import os
from datetime import datetime, timedelta

from clients.calendar_client import now_local
from clients.notion_client import (
    database_property_type, get_page_title, query_database,
)
from config import LEARN_NUDGE_MAX_ITEMS, LEARN_NUDGE_STALE_DAYS

LEARN_ID = os.environ.get("LEARN_ID")

# The checkbox both Implement paths write. Named once here: the writers are in
# services/, and a string spelled two ways in two layers is how a marker gets
# reworded in one of them and silently stops being detected in the other.
IMPLEMENTED_PROPERTY = "Implemented"


def _pending_filter(cutoff_iso: str) -> dict:
    """Unimplemented AND older than the cutoff, as one Notion filter."""
    return {
        "and": [
            {"property": IMPLEMENTED_PROPERTY, "checkbox": {"equals": False}},
            # `before` is strict — see the module docstring on the boundary.
            {"timestamp": "created_time", "created_time": {"before": cutoff_iso}},
        ]
    }


def _age_in_days(page: dict, now) -> int | None:
    """Whole days since the page was created, or None if Notion sent no date.

    None rather than 0: an unknown age printed as "0 days ago" would put the
    oldest debt in the list looking like the newest.
    """
    created = page.get("created_time")
    if not created:
        return None
    try:
        # Notion sends "2026-08-01T10:00:00.000Z"; fromisoformat takes the Z on
        # 3.11+, and CI pins 3.12. TypeError as well as ValueError: `now` is
        # tz-aware, so a naive stamp subtracts rather than parses badly, and the
        # age is decoration — it must never be the thing that costs you the list.
        stamp = datetime.fromisoformat(created)
        return max(0, (now - stamp).days)
    except (ValueError, TypeError):
        return None


def _format_nudge(pages: list, total: int, now) -> str:
    """The message. PLAIN TEXT — page titles are user data and carry * and _."""
    lines = [f"📚 {total} Learn page(s) you saved but never implemented:", ""]
    for page in pages:
        age = _age_in_days(page, now)
        suffix = f" ({age} days ago)" if age is not None else ""
        lines.append(f"• {get_page_title(page)}{suffix}")

    hidden = total - len(pages)
    if hidden > 0:
        lines.append(f"…and {hidden} more.")

    lines += ["", "Merge one with:  Implement [page name] - [Area]"]
    return "\n".join(lines)


def build_nudge() -> tuple:
    """Return (message, error) — the list of unimplemented Learn pages.

    FOUR outcomes, and they are four because collapsing any pair of them is a
    bug this codebase has already shipped once:

        (text, None)   there is a backlog; here it is, oldest first
        (None, None)   the database was read and you are caught up
        (None, error)  the read failed, or the schema read failed
        (None, error)  there is no `Implemented` column — its own wording,
                       because the fix is different from an outage's
    """
    if not LEARN_ID:
        return None, "LEARN_ID is not set, so I cannot check for unimplemented pages."

    # THREE outcomes from this call, all handled apart: the middle one — schema
    # read fine, no such property — is the missing column, and `if not prop_type`
    # would fold it into the failure below.
    prop_type, err = database_property_type(LEARN_ID, IMPLEMENTED_PROPERTY)
    if err:
        return None, f"Could not read the Learn database schema: {err}"
    if prop_type is None:
        return None, (
            f"The Learn database has no '{IMPLEMENTED_PROPERTY}' checkbox, so I "
            "cannot tell which pages you have merged into a Manual. Add a "
            f"checkbox column named '{IMPLEMENTED_PROPERTY}' to the database. "
            "(Implement has been trying to tick it on every run.)"
        )
    if prop_type != "checkbox":
        return None, (
            f"'{IMPLEMENTED_PROPERTY}' in the Learn database is a {prop_type}, "
            "not a checkbox, so I cannot tell which pages are still pending."
        )

    now = now_local()
    cutoff = now - timedelta(days=LEARN_NUDGE_STALE_DAYS)

    pages, err = query_database(
        LEARN_ID,
        filter_obj=_pending_filter(cutoff.isoformat()),
        # Oldest first: the page you have ignored longest is the one worth
        # naming, and it is the one a cap would otherwise cut.
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
    )
    if err:
        # NOT silence. A failed read and an empty backlog are the same value
        # only if you let them be, and only one of them means "you are done".
        return None, f"Could not check for unimplemented Learn pages: {err}"

    if not pages:
        return None, None

    return _format_nudge(pages[:LEARN_NUDGE_MAX_ITEMS], len(pages), now), None
