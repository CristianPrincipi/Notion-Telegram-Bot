"""Weekly liveness proof.

WHY IT ALWAYS SPEAKS. A probe that only messages on failure cannot be trusted: it
is equally silent when the bot is dead, when the JobQueue never registered, when
the Telegram token was revoked, and when everything is fine. Those are exactly
the outages worth catching, and a failure-only design reports none of them.

So the message itself is the signal. It arrives Sunday evening against a weekly
rhythm, which makes a MISSING one easy to notice — and a missing heartbeat means
something is wrong with David itself, not with what David is watching.

WHY THE PROBES ARE REAL CALLS. Checking configuration proves nothing.
calendar_client._get_service caches a service per thread and builds it happily
from a rotated key — from_service_account_info parses locally and build() uses a
bundled discovery document, so neither contacts Google. The credential only fails
later, at .execute(). Only a real round trip detects a revoked share, and because
the bad service stays cached for the life of the thread, a restart is part of the
fix. The same reasoning applies to Notion: the key is only proven by a request.

The report is PLAIN TEXT. It is diagnostics, and it exists to be readable when
things are broken — it must not be able to fail on formatting.
"""

from clients.calendar_client import get_events_for_day, now_local
from services.month import canonical_title, current_month_id
from clients.notion_client import get_database
from observability import snapshot

EXPENSES_ID_MISSING = "EXPENSES_ID is not set"


def _probe_calendar() -> tuple:
    """(ok, detail). A real Google round trip — see the module docstring."""
    try:
        events, err = get_events_for_day(now_local())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if err:
        return False, err
    return True, f"{len(events)} event(s) today"


def _probe_notion() -> tuple:
    """(ok, detail). Reads the Expenses database — the one David cannot work without."""
    import os

    expenses_id = os.environ.get("EXPENSES_ID")
    if not expenses_id:
        return False, EXPENSES_ID_MISSING
    try:
        db, err = get_database(expenses_id)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if err:
        return False, err
    return True, "Expenses database reachable"


def _probe_month() -> tuple:
    """(ok, detail). Which page this month's expenses are landing on."""
    try:
        page_id = current_month_id()
        expected = canonical_title()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if not page_id:
        return False, f"no month page resolved (expected “{expected}”)"
    return True, f"“{expected}” → {page_id}"


def build_heartbeat() -> tuple:
    """Compose the weekly report. Returns (message, error).

    Never returns (None, ...): the message IS the liveness proof, so there is no
    path on which this stays silent. `error` is set when any probe failed, which
    is what makes the scheduler log it at ERROR — the message already says so in
    words, the error makes it greppable.
    """
    lines = ["💓 David weekly check-in", ""]
    failures = []

    for label, probe in (("Calendar", _probe_calendar),
                         ("Notion",   _probe_notion),
                         ("Month",    _probe_month)):
        try:
            ok, detail = probe()
        except Exception as e:
            # A probe is not allowed to take the report down with it — the whole
            # point is that this message arrives.
            ok, detail = False, f"probe crashed: {type(e).__name__}: {e}"
        lines.append(f"{'✅' if ok else '❌'} {label}: {detail}")
        if not ok:
            failures.append(f"{label}: {detail}")

    counts = snapshot()
    lines += [
        "",
        f"📊 Since restart: {counts['commands']} command(s), {counts['errors']} error(s).",
    ]

    if failures:
        lines += ["", "Something needs attention — see the ❌ lines above."]

    return "\n".join(lines), ("; ".join(failures) if failures else None)
