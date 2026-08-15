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

The morning briefing has two sources and treats them identically: either can fail
without costing the other, a half that failed says so in the message instead of
vanishing, and both errors are reported. That symmetry arrived late —
compute_budget used to return a bare None for both "Notion failed" and "nothing
to report", so the budget half degraded silently while the calendar half did not.

This is the fix for the worst silent failure David had. Before it:

  - the evening briefing collapsed the two cases into one — `if err or not
    events: return None` — so a revoked calendar share and an empty tomorrow were
    indistinguishable, and the briefing simply stopped arriving;
  - the morning briefing was worse than silent. On an error it set `events = []`,
    and an empty list renders as "nothing scheduled", so during an outage David
    said the day was clear. That is not a missing message, it is an affirmative
    false statement, and it is the kind you act on by not showing up.

A calendar error must therefore never reach format_events_inline. The two states
are kept apart all the way to the text.
"""

from datetime import timedelta

from clients.calendar_client import get_events_for_day, now_local
from budget import compute_budget
from services.agenda import format_events_inline

# IMPORTED, NOT DEFINED. This renderer used to be `_format_events_inline` here,
# private to the briefings. The `Agenda` command has to produce the same line, so
# leaving it here meant either a second copy or `services/` importing `proactive/`
# — sideways and upward, which the layering rule does not allow. It moved down to
# services/agenda.py and both callers read the one implementation.


def _budget_line(b: dict | None) -> str | None:
    """'💰 €182/300 (on pace)' — or None if there is no budget to render.

    A pure formatter: it is given the dict compute_budget returned and knows
    nothing about why it might be missing. The caller holds the error and decides
    what to say about it, which is the split that stops "no line" from meaning
    two different things again.
    """
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


def _join(*errors) -> str | None:
    """The one error the scheduler takes, from the two sources this briefing has.

    Unlabelled on purpose: with a single failure the client's own string is
    passed through VERBATIM, which is what the tests assert and what makes a
    report worth reading. `_report_error` already prefixes the job name, and a
    Notion 401 does not look like a Google 403, so a double outage joined with a
    separator is still legible — and reporting both beats picking one.
    """
    return " · ".join(e for e in errors if e) or None


def build_morning_briefing() -> tuple:
    """Compose the Morning Briefing (today's events + budget pace).

    Returns (message, error).

    BOTH HALVES DEGRADE THE SAME WAY, and that symmetry is the point. Either
    source can fail without costing the other, and a half that could not be read
    SAYS SO in the message rather than quietly disappearing — a vanished budget
    line is indistinguishable from a month with nothing in it, which is the exact
    bug this milestone came from. The error is returned alongside so the
    scheduler reports it too: the message tells you a half is unknown, the report
    tells you why and that it needs fixing.

    It no longer returns (None, None). That branch was reachable only when the
    calendar was empty AND compute_budget returned a bare None, i.e. only through
    the collapsed error that has now been removed. A total outage used to be
    silence plus one error; it is now a message saying both halves are unknown,
    plus both errors.
    """
    events, cal_err = _events_for(now_local())
    b, budget_err   = compute_budget()
    budget_line     = _budget_line(b)

    parts = ["☀️ Good morning."]
    if cal_err:
        # NEVER fall through to format_events_inline here. `events` is [] on
        # error, and [] renders as "nothing scheduled".
        parts.append("⚠️ I could not read your calendar, so I don't know what's on today.")
    else:
        parts.append(f"Today: {format_events_inline(events)}.")

    if budget_line:
        parts.append(budget_line)
    elif budget_err:
        parts.append("⚠️ I could not read your budget.")

    return " ".join(parts), _join(cal_err, budget_err)


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
    return f"🌙 Tomorrow: {format_events_inline(events)}.", None
