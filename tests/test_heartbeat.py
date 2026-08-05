"""The weekly liveness proof.

The design decision under test: it ALWAYS sends. A failure-only probe is equally
silent when the bot is dead, when the JobQueue never registered, and when
everything is fine — so silence carries no information. Here the message is the
signal, and a missing Sunday message is itself the alarm.

Which means the one thing that must never happen is build_heartbeat returning
nothing, whatever the probes do.
"""

import pytest

import observability
import proactive.heartbeat as heartbeat


@pytest.fixture(autouse=True)
def healthy(monkeypatch):
    """All three probes green by default; each test breaks the one it cares about."""
    monkeypatch.setattr(heartbeat, "get_events_for_day", lambda day: ([{"summary": "Gym"}], None))
    monkeypatch.setattr(heartbeat, "now_local", lambda: "2026-08-09")
    monkeypatch.setattr(heartbeat, "get_database", lambda db_id: ({"id": db_id}, None))
    monkeypatch.setattr(heartbeat, "current_month_id", lambda: "page-august-2026")
    monkeypatch.setattr(heartbeat, "canonical_title", lambda: "August 2026")


@pytest.fixture(autouse=True)
def clean_counters():
    observability.reset_counters()
    yield
    observability.reset_counters()


# ─── THE HEALTHY CASE ──────────────────────────────────────────────────────────

def test_it_sends_even_when_everything_is_fine():
    """The whole design: no news is still news."""
    text, err = heartbeat.build_heartbeat()

    assert text is not None
    assert err is None


def test_a_healthy_report_names_every_probe():
    text, _ = heartbeat.build_heartbeat()

    assert "Calendar" in text
    assert "Notion" in text
    assert "Month" in text
    assert "❌" not in text


def test_it_reports_the_counters_since_restart():
    observability.record_command()
    observability.record_command()
    observability.record_command()
    observability.record_error()

    text, _ = heartbeat.build_heartbeat()

    assert "3 command(s)" in text
    assert "1 error(s)" in text


def test_it_names_the_month_page_expenses_are_landing_on():
    text, _ = heartbeat.build_heartbeat()

    assert "August 2026" in text
    assert "page-august-2026" in text


# ─── FAILURES ──────────────────────────────────────────────────────────────────

def test_a_calendar_outage_is_named_and_still_sent(monkeypatch):
    """The scenario that started all of this: a rotated service-account key."""
    monkeypatch.setattr(heartbeat, "get_events_for_day",
                        lambda day: ([], "Could not fetch events: <HttpError 401>"))

    text, err = heartbeat.build_heartbeat()

    assert text is not None, "a failed probe must not silence the heartbeat"
    assert "❌ Calendar" in text
    assert "HttpError 401" in text
    assert "Calendar" in err


def test_a_notion_outage_is_named_and_still_sent(monkeypatch):
    monkeypatch.setattr(heartbeat, "get_database",
                        lambda db_id: (None, "Notion 401: unauthorized"))

    text, err = heartbeat.build_heartbeat()

    assert "❌ Notion" in text
    assert "Notion 401" in err
    assert "✅ Calendar" in text, "one failed probe must not mask the healthy ones"


def test_an_unresolved_month_page_is_named(monkeypatch):
    monkeypatch.setattr(heartbeat, "current_month_id", lambda: None)

    text, err = heartbeat.build_heartbeat()

    assert "❌ Month" in text
    assert "Month" in err


def test_a_probe_that_raises_does_not_take_the_report_down(monkeypatch):
    """A crashing probe is the case where a heartbeat is most needed."""
    def boom(day):
        raise RuntimeError("SSL handshake failed")

    monkeypatch.setattr(heartbeat, "get_events_for_day", boom)

    text, err = heartbeat.build_heartbeat()

    assert text is not None
    assert "❌ Calendar" in text
    assert "RuntimeError" in text
    assert err is not None


def test_every_probe_failing_still_produces_a_report(monkeypatch):
    """Total outage — still the one message that proves David is running."""
    monkeypatch.setattr(heartbeat, "get_events_for_day", lambda day: ([], "calendar down"))
    monkeypatch.setattr(heartbeat, "get_database", lambda db_id: (None, "notion down"))
    monkeypatch.setattr(heartbeat, "current_month_id", lambda: None)

    text, err = heartbeat.build_heartbeat()

    failed = [line for line in text.splitlines() if line.startswith("❌")]

    assert text is not None
    assert len(failed) == 3, f"expected all three probes to report failure, got {failed}"
    assert "calendar down" in err and "notion down" in err
    assert "needs attention" in text


def test_the_report_is_plain_text_safe():
    """It is diagnostics — it must not be able to fail on formatting.

    No Markdown emphasis characters are used for structure, so nothing can be left
    unbalanced by an error string interpolated into it.
    """
    text, _ = heartbeat.build_heartbeat()

    assert "*" not in text
    assert "_" not in text


def test_build_heartbeat_is_synchronous():
    """It runs via asyncio.to_thread — an async builder would never be awaited."""
    import asyncio

    assert not asyncio.iscoroutinefunction(heartbeat.build_heartbeat)
