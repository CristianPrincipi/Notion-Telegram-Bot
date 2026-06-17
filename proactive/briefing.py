"""
Daily briefings.

Step 1: Morning Briefing — today's calendar events + a one-line budget pace.
Step 2: Evening Briefing — tomorrow's calendar events (so you can prep tonight).

These functions only *compose text*; the JobQueue wiring and Telegram sending
live in proactive/scheduler.py. Both briefings share one event formatter so the
two messages can never drift apart in style.
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


def build_morning_briefing() -> str | None:
    """Compose the Morning Briefing (today's events + budget pace).

    Returns None when there is nothing worth sending (no events AND the budget
    query failed) — so a total outage stays silent.
    """
    events, err = get_events_for_day(now_local())
    if err:
        events = []  # calendar down → still send the budget half

    budget_line = _budget_line(compute_budget())

    if not events and budget_line is None:
        return None

    parts = ["☀️ Good morning.", f"Today: {_format_events_inline(events)}."]
    if budget_line:
        parts.append(budget_line)
    return " ".join(parts)


def build_evening_briefing() -> str | None:
    """Compose the Evening Briefing (tomorrow's events only).

    Returns None when tomorrow has no events (or the calendar errored), matching
    the old build_tomorrow_message() behaviour — no nightly empty pings.
    """
    events, err = get_events_for_day(now_local() + timedelta(days=1))
    if err or not events:
        return None
    return f"🌙 Tomorrow: {_format_events_inline(events)}."
