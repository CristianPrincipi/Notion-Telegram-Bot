"""
Daily briefings.

Step 1: Morning Briefing — today's calendar events + a one-line budget pace.
Step 2: Evening Briefing — tomorrow's calendar events (so you can prep tonight).

These functions only *compose text*; the JobQueue wiring and Telegram sending
live in proactive/scheduler.py. Both briefings share one event formatter so the
two messages can never drift apart in style.

BOTH RETURN (message, error). The error is NOT the absence of a message — the
morning briefing returns both, because a calendar outage still leaves the budget
half worth sending. The scheduler reports the error and sends whatever text came
back with it.

This is the fix for the worst silent failure David had. Before it:

  - the evening briefing collapsed the two cases into one — `if err or not
    events: return None` — so a revoked calendar share and an empty tomorrow were
    indistinguishable, and the briefing simply stopped arriving;
  - the morning briefing was worse than silent. On an error it set `events = []`,
    and an empty list renders as "nothing scheduled", so during an outage David
    said the day was clear. That is not a missing message, it is an affirmative
    false statement, and it is the kind you act on by not showing up.

A calendar error must therefore never reach _format_events_inline. The two states
are kept apart all the way to the text.
"""

from datetime import timedelta

from calendar_client import get_events_for_day, now_local
from budget import compute_budget


def _format_events_inline(events: list) -> str:
    """Compact, comma-separated: 'Dentist 14:30, Gym 19:00'."""
    if not events:
        return "nothing scheduled"
    parts = []
    for e in events:
        if e.get("all_day"):
            parts.append(f"{e['summary']} (all day)")
        else:
            parts.append(f"{e['summary']} {e['start_dt'].strftime('%H:%M')}")
    return ", ".join(parts)


def _budget_line(b: dict | None) -> str | None:
    """'💰 €182/300 (on pace)' — or None if the budget query failed."""
    if not b:
        return None
    pace = "on pace" if b["on_pace"] else "over pace"
    return f"💰 €{b['total']:.0f}/{b['ceiling']:.0f} ({pace})"


def _events_for(day) -> tuple:
    """get_events_for_day, with a raised exception turned into a returned error.

    calendar_client already returns (events, error) for the failures it predicts,
    but an unforeseen one — a googleapiclient change, an SSL failure, a None where
    a dict was expected — propagates. Letting it escape costs the whole briefing,
    including the budget half that had nothing to do with the calendar, and the
    morning message is the one that must arrive. Caught here, it takes the same
    route as any other calendar error: degraded message, error reported.
    """
    try:
        return get_events_for_day(day)
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def build_morning_briefing() -> tuple:
    """Compose the Morning Briefing (today's events + budget pace).

    Returns (message, error).

    On a calendar error it still sends — the budget half is independently useful,
    and morning silence is easy to miss — but it says the calendar is unreachable
    instead of claiming the day is clear. The error is returned alongside so the
    scheduler reports it too: the message tells you today is unknown, the error
    report tells you why and that it needs fixing.

    (None, None) only when the calendar is genuinely empty AND the budget query
    returned nothing — there is then nothing to say and nothing wrong.
    """
    events, err = _events_for(now_local())
    budget_line = _budget_line(compute_budget())

    if err:
        # NEVER fall through to _format_events_inline here. `events` is [] on
        # error, and [] renders as "nothing scheduled".
        parts = ["☀️ Good morning.",
                 "⚠️ I could not read your calendar, so I don't know what's on today."]
        if budget_line:
            parts.append(budget_line)
        return " ".join(parts), err

    if not events and budget_line is None:
        return None, None

    parts = ["☀️ Good morning.", f"Today: {_format_events_inline(events)}."]
    if budget_line:
        parts.append(budget_line)
    return " ".join(parts), None


def build_evening_briefing() -> tuple:
    """Compose the Evening Briefing (tomorrow's events only).

    Returns (message, error).

    (None, None) on a genuinely empty tomorrow — no nightly empty pings, which is
    the behaviour worth keeping. (None, error) when the calendar failed: nothing
    useful to say about tomorrow, but the scheduler still reports the failure
    rather than letting the evening pass in the same silence as an empty one.
    """
    events, err = _events_for(now_local() + timedelta(days=1))
    if err:
        return None, err
    if not events:
        return None, None
    return f"🌙 Tomorrow: {_format_events_inline(events)}.", None
