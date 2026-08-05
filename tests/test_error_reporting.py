"""The error reporters must not be able to fail on formatting.

All three used to interpolate the exception text into a parse_mode="Markdown"
message. Notion 400 bodies and tracebacks routinely carry unbalanced * _ ` and [,
so Telegram rejected the notification with BadRequest, the bare `except` swallowed
it, and the error being reported vanished — exactly the silent failure the
reporters exist to prevent.

These tests assert the reporters send PLAIN TEXT. If a future change reintroduces
parse_mode, they turn red.
"""

import david
import proactive.scheduler as sched
from conftest import FakeContext, run

CHAT_ID = "test-chat-id"

# A message shaped like the real thing that broke it: a Notion validation error
# naming snake_case properties, with unbalanced markup in every direction.
HOSTILE = (
    "body failed validation: body.properties.rich_text[0].text should be "
    "defined, instead was `undefined`. *unclosed and _unclosed"
)


def job_context():
    context = FakeContext()
    context.job = type("Job", (), {"chat_id": CHAT_ID})()
    return context


# ─── notify_error ──────────────────────────────────────────────────────────────

def test_notify_error_sends_without_parse_mode():
    context = FakeContext()

    run(david.notify_error(context, "send_budget_recap", ValueError(HOSTILE)))

    assert len(context.bot.sent_full) == 1
    _, _, kwargs = context.bot.sent_full[0]
    assert kwargs.get("parse_mode") is None, (
        f"notify_error sent parse_mode={kwargs.get('parse_mode')!r} — "
        "the report must be plain text or it can fail on the error it is reporting"
    )


def test_notify_error_delivers_markdown_hostile_text_intact():
    context = FakeContext()

    run(david.notify_error(context, "send_budget_recap", ValueError(HOSTILE)))

    _, text, _ = context.bot.sent_full[0]
    assert HOSTILE in text
    assert "ValueError" in text
    assert "send_budget_recap" in text


# ─── on_error ──────────────────────────────────────────────────────────────────

def test_on_error_is_importable_at_module_scope():
    """It used to be nested inside __main__, which made it untestable."""
    assert callable(david.on_error)


def test_on_error_sends_without_parse_mode():
    context = FakeContext()
    context.error = RuntimeError(HOSTILE)

    run(david.on_error(None, context))

    assert len(context.bot.sent_full) == 1
    _, _, kwargs = context.bot.sent_full[0]
    assert kwargs.get("parse_mode") is None


def test_on_error_delivers_markdown_hostile_text_intact():
    context = FakeContext()
    context.error = RuntimeError(HOSTILE)

    run(david.on_error(None, context))

    _, text, _ = context.bot.sent_full[0]
    assert HOSTILE in text
    assert "RuntimeError" in text


# ─── proactive._report_error ───────────────────────────────────────────────────

def test_proactive_report_error_sends_without_parse_mode():
    context = job_context()

    run(sched._report_error(context, "morning_briefing", ValueError(HOSTILE)))

    assert len(context.bot.sent_full) == 1
    chat_id, _, kwargs = context.bot.sent_full[0]
    assert chat_id == CHAT_ID
    assert kwargs.get("parse_mode") is None


def test_proactive_report_error_delivers_markdown_hostile_text_intact():
    context = job_context()

    run(sched._report_error(context, "morning_briefing", ValueError(HOSTILE)))

    _, text, _ = context.bot.sent_full[0]
    assert HOSTILE in text
    assert "ValueError" in text
    assert "morning_briefing" in text


def test_proactive_report_error_accepts_a_plain_error_string():
    """Builders return (text, error) tuples, so `err` is not always an Exception."""
    context = job_context()

    run(sched._report_error(context, "evening_briefing", "Could not fetch events: 401"))

    _, text, kwargs = context.bot.sent_full[0]
    assert "Could not fetch events: 401" in text
    assert kwargs.get("parse_mode") is None
