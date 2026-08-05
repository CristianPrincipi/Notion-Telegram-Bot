"""Briefing and pacing text composition.

These are the messages the scheduled jobs actually send. Google Calendar and the
budget are both stubbed — the composition is what is under test, not the fetching.

Every builder returns (message, error). The distinction these tests exist to
protect: an error is NOT the same as an empty result. Collapsing the two is what
made a revoked calendar share look exactly like a quiet week, and made the morning
briefing announce "nothing scheduled" on a day that was full.
"""

from datetime import datetime

import pytest

import proactive.briefing as briefing
import proactive.budget_watch as budget_watch


def event(summary, hour=0, minute=0, all_day=False):
    return {
        "summary": summary,
        "all_day": all_day,
        "start_dt": datetime(2025, 6, 10, hour, minute),
    }


def budget(total=100.0, ceiling=300.0, on_pace=True, day=10,
           projected_total=300.0, projected_over=0.0, top_category=None):
    return {
        "total": total, "ceiling": ceiling, "on_pace": on_pace, "day": day,
        "projected_total": projected_total, "projected_over": projected_over,
        "top_category": top_category, "per_category": {}, "remaining": ceiling - total,
        "days_in_month": 30, "expected_to_date": 100.0,
    }


@pytest.fixture
def calendar(monkeypatch):
    """Control what the calendar returns. Defaults to (no events, no error)."""
    state = {"events": [], "err": None}
    monkeypatch.setattr(briefing, "get_events_for_day",
                        lambda day: (state["events"], state["err"]))
    monkeypatch.setattr(briefing, "now_local", lambda: datetime(2025, 6, 10, 7, 30))
    return state


@pytest.fixture
def notion(monkeypatch):
    """Control what compute_budget returns, for both modules that call it."""
    state = {"budget": budget()}
    monkeypatch.setattr(briefing, "compute_budget", lambda: state["budget"])
    monkeypatch.setattr(budget_watch, "compute_budget", lambda: state["budget"])
    return state


# ─── MORNING BRIEFING ──────────────────────────────────────────────────────────

def test_morning_lists_todays_events_and_the_budget_pace(calendar, notion):
    calendar["events"] = [event("Dentist", 14, 30), event("Gym", 19)]
    notion["budget"] = budget(total=182.0, on_pace=True)

    text, err = briefing.build_morning_briefing()

    assert text == "☀️ Good morning. Today: Dentist 14:30, Gym 19:00. 💰 €182/300 (on pace)"
    assert err is None


def test_morning_flags_being_over_pace(calendar, notion):
    notion["budget"] = budget(total=250.0, on_pace=False)

    text, _ = briefing.build_morning_briefing()

    assert "(over pace)" in text


def test_morning_says_so_when_the_day_is_empty(calendar, notion):
    text, err = briefing.build_morning_briefing()

    assert "nothing scheduled" in text
    assert "💰" in text, "an empty day should still report the budget"
    assert err is None, "an empty day is not an error"


def test_morning_marks_all_day_events(calendar, notion):
    calendar["events"] = [event("Holiday", all_day=True)]

    text, _ = briefing.build_morning_briefing()

    assert "Holiday (all day)" in text


# ─── THE BUG THIS BRANCH EXISTS FOR ────────────────────────────────────────────

def test_morning_never_claims_nothing_is_scheduled_when_the_calendar_failed(calendar, notion):
    """The worst failure David had.

    On a calendar error the old code set events = [], and an empty list renders as
    "nothing scheduled" — so during an outage David affirmatively said the day was
    clear. Silence you eventually notice; a confident wrong answer you act on, by
    not showing up.
    """
    calendar["err"] = "Could not fetch events: <HttpError 401 invalid_grant>"

    text, err = briefing.build_morning_briefing()

    assert text is not None, "an outage must not make the morning silent"
    assert "nothing scheduled" not in text, (
        "the calendar failed — saying the day is clear is an affirmative lie"
    )
    assert "could not read your calendar" in text.lower()
    assert err == "Could not fetch events: <HttpError 401 invalid_grant>"


def test_morning_degrades_rather_than_dying_when_the_calendar_client_raises(
        calendar, notion, monkeypatch):
    """A RAISED exception must take the same route as a returned error.

    Letting it propagate would cost the whole briefing — including the budget
    half, which had nothing to do with the calendar — and the morning message is
    the one that has to arrive.
    """
    def boom(day):
        raise RuntimeError("service account key revoked")

    monkeypatch.setattr(briefing, "get_events_for_day", boom)
    notion["budget"] = budget(total=182.0)

    text, err = briefing.build_morning_briefing()

    assert text is not None, "an exception must not silence the briefing"
    assert "nothing scheduled" not in text
    assert "could not read your calendar" in text.lower()
    assert "💰" in text, "the budget half survives a calendar failure"
    assert "RuntimeError" in err and "revoked" in err


def test_morning_still_sends_the_budget_when_the_calendar_is_down(calendar, notion):
    calendar["err"] = "Google Calendar unavailable"

    text, err = briefing.build_morning_briefing()

    assert text is not None
    assert "💰" in text, "the budget half is independently useful"
    assert err == "Google Calendar unavailable"


def test_morning_still_sends_events_when_notion_is_down(calendar, notion):
    calendar["events"] = [event("Dentist", 14, 30)]
    notion["budget"] = None

    text, err = briefing.build_morning_briefing()

    assert "Dentist 14:30" in text
    assert "💰" not in text
    assert err is None


def test_morning_reports_rather_than_hides_a_total_outage(calendar, notion):
    """Both sources down. The old code returned None here — total silence.

    Now the calendar failure is still reported, because "I could not check" and
    "there was nothing to say" are different facts.
    """
    calendar["err"] = "down"
    notion["budget"] = None

    text, err = briefing.build_morning_briefing()

    assert err == "down"
    assert "nothing scheduled" not in (text or "")


# ─── EVENING BRIEFING ──────────────────────────────────────────────────────────

def test_evening_lists_tomorrows_events(calendar, notion):
    calendar["events"] = [event("Standup", 9), event("Dentist", 14, 30)]

    text, err = briefing.build_evening_briefing()

    assert text == "🌙 Tomorrow: Standup 09:00, Dentist 14:30."
    assert err is None


def test_evening_asks_the_calendar_for_tomorrow_not_today(calendar, monkeypatch):
    """An off-by-one here would quietly re-send today's events every night."""
    asked = []
    monkeypatch.setattr(briefing, "get_events_for_day",
                        lambda day: (asked.append(day), ([event("X", 9)], None))[1])

    briefing.build_evening_briefing()

    assert asked[0].date() == datetime(2025, 6, 11).date()


def test_evening_stays_silent_when_tomorrow_is_empty(calendar, notion):
    """No nightly "nothing scheduled" ping — this behaviour is worth keeping."""
    text, err = briefing.build_evening_briefing()

    assert text is None
    assert err is None, "an empty tomorrow is not a failure"


def test_evening_reports_instead_of_going_quiet_when_the_calendar_is_down(calendar, notion):
    """The silent-reminders bug, in the place it actually lived.

    `if err or not events: return None` meant a revoked calendar share and a free
    evening produced byte-identical output: nothing. The reminders stopped and
    there was no way to tell.
    """
    calendar["err"] = "Could not fetch events: <HttpError 403 forbidden>"
    calendar["events"] = [event("Standup", 9)]

    text, err = briefing.build_evening_briefing()

    assert text is None, "nothing trustworthy to say about tomorrow"
    assert err == "Could not fetch events: <HttpError 403 forbidden>", (
        "but the failure must be reported, not swallowed"
    )


def test_evening_degrades_rather_than_dying_when_the_calendar_client_raises(
        calendar, notion, monkeypatch):
    def boom(day):
        raise ConnectionError("SSL handshake failed")

    monkeypatch.setattr(briefing, "get_events_for_day", boom)

    text, err = briefing.build_evening_briefing()

    assert text is None
    assert "ConnectionError" in err


# ─── BUDGET PACING ─────────────────────────────────────────────────────────────

def test_pacing_warns_with_the_projection_and_the_driver(notion):
    notion["budget"] = budget(total=245.0, day=18, on_pace=False,
                              projected_total=408.0, projected_over=108.0,
                              top_category=("Food", 150.0))

    text, err = budget_watch.build_pacing_warning()

    assert "Day 18" in text
    assert "€245 spent" in text
    assert "€408" in text and "€108 over" in text
    assert "Food is the driver (€150)" in text
    assert err is None


def test_pacing_is_silent_early_in_the_month(notion):
    """Days 1-4 project wildly off one coffee — too noisy to be useful."""
    notion["budget"] = budget(total=200.0, day=3, projected_over=200.0)

    assert budget_watch.build_pacing_warning() == (None, None)


def test_pacing_is_silent_for_a_trivial_overshoot(notion):
    """A few euros over the ceiling is not worth a push message."""
    notion["budget"] = budget(total=150.0, day=20, projected_over=5.0)

    assert budget_watch.build_pacing_warning() == (None, None)


def test_pacing_fires_at_the_threshold(notion):
    """5% of a €300 ceiling = €15."""
    notion["budget"] = budget(total=150.0, day=20, projected_over=15.0)

    text, _ = budget_watch.build_pacing_warning()

    assert text is not None


def test_pacing_is_silent_when_notion_is_down(notion):
    """KNOWN GAP, asserted so it is visible rather than forgotten.

    compute_budget() returns None for both "Notion failed" and "nothing to
    report", so this builder cannot tell them apart and reports no error. It is
    the same collapse the briefings just had, one layer down — see CLAUDE.md.
    """
    notion["budget"] = None

    assert budget_watch.build_pacing_warning() == (None, None)


def test_pacing_omits_the_driver_when_there_is_no_spend_category(notion):
    notion["budget"] = budget(total=245.0, day=18, projected_over=108.0,
                              top_category=None)

    text, _ = budget_watch.build_pacing_warning()

    assert text is not None
    assert "driver" not in text
