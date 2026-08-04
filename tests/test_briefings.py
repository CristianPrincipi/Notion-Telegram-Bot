"""Briefing and pacing text composition.

These are the messages the scheduled jobs actually send. They were written but
never ran, so nothing had ever exercised them. Google Calendar and the budget
are both stubbed — the composition is what is under test, not the fetching.
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

    text = briefing.build_morning_briefing()

    assert text == "☀️ Good morning. Today: Dentist 14:30, Gym 19:00. 💰 €182/300 (on pace)"


def test_morning_flags_being_over_pace(calendar, notion):
    notion["budget"] = budget(total=250.0, on_pace=False)

    assert "(over pace)" in briefing.build_morning_briefing()


def test_morning_says_so_when_the_day_is_empty(calendar, notion):
    text = briefing.build_morning_briefing()

    assert "nothing scheduled" in text
    assert "💰" in text, "an empty day should still report the budget"


def test_morning_marks_all_day_events(calendar, notion):
    calendar["events"] = [event("Holiday", all_day=True)]

    assert "Holiday (all day)" in briefing.build_morning_briefing()


def test_morning_still_sends_the_budget_when_the_calendar_is_down(calendar, notion):
    calendar["err"] = "Google Calendar unavailable"

    text = briefing.build_morning_briefing()

    assert text is not None
    assert "💰" in text


def test_morning_still_sends_events_when_notion_is_down(calendar, notion):
    calendar["events"] = [event("Dentist", 14, 30)]
    notion["budget"] = None

    text = briefing.build_morning_briefing()

    assert "Dentist 14:30" in text
    assert "💰" not in text


def test_morning_stays_silent_in_a_total_outage(calendar, notion):
    """Both sources down means there is nothing to say — don't ping noise."""
    calendar["err"] = "down"
    notion["budget"] = None

    assert briefing.build_morning_briefing() is None


# ─── EVENING BRIEFING ──────────────────────────────────────────────────────────

def test_evening_lists_tomorrows_events(calendar, notion):
    calendar["events"] = [event("Standup", 9), event("Dentist", 14, 30)]

    assert briefing.build_evening_briefing() == "🌙 Tomorrow: Standup 09:00, Dentist 14:30."


def test_evening_asks_the_calendar_for_tomorrow_not_today(calendar, monkeypatch):
    """An off-by-one here would quietly re-send today's events every night."""
    asked = []
    monkeypatch.setattr(briefing, "get_events_for_day",
                        lambda day: (asked.append(day), ([event("X", 9)], None))[1])

    briefing.build_evening_briefing()

    assert asked[0].date() == datetime(2025, 6, 11).date()


def test_evening_stays_silent_when_tomorrow_is_empty(calendar, notion):
    """No nightly "nothing scheduled" ping — matches the old behaviour."""
    assert briefing.build_evening_briefing() is None


def test_evening_stays_silent_when_the_calendar_is_down(calendar, notion):
    calendar["err"] = "down"
    calendar["events"] = [event("Standup", 9)]

    assert briefing.build_evening_briefing() is None


# ─── BUDGET PACING ─────────────────────────────────────────────────────────────

def test_pacing_warns_with_the_projection_and_the_driver(notion):
    notion["budget"] = budget(total=245.0, day=18, on_pace=False,
                              projected_total=408.0, projected_over=108.0,
                              top_category=("Food", 150.0))

    text = budget_watch.build_pacing_warning()

    assert "Day 18" in text
    assert "€245 spent" in text
    assert "€408" in text and "€108 over" in text
    assert "Food is the driver (€150)" in text


def test_pacing_is_silent_early_in_the_month(notion):
    """Days 1-4 project wildly off one coffee — too noisy to be useful."""
    notion["budget"] = budget(total=200.0, day=3, projected_over=200.0)

    assert budget_watch.build_pacing_warning() is None


def test_pacing_is_silent_for_a_trivial_overshoot(notion):
    """A few euros over the ceiling is not worth a push message."""
    notion["budget"] = budget(total=150.0, day=20, projected_over=5.0)

    assert budget_watch.build_pacing_warning() is None


def test_pacing_fires_at_the_threshold(notion):
    """5% of a €300 ceiling = €15."""
    notion["budget"] = budget(total=150.0, day=20, projected_over=15.0)

    assert budget_watch.build_pacing_warning() is not None


def test_pacing_is_silent_when_notion_is_down(notion):
    notion["budget"] = None

    assert budget_watch.build_pacing_warning() is None


def test_pacing_omits_the_driver_when_there_is_no_spend_category(notion):
    notion["budget"] = budget(total=245.0, day=18, projected_over=108.0,
                              top_category=None)

    text = budget_watch.build_pacing_warning()

    assert text is not None
    assert "driver" not in text
