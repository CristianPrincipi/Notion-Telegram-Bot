"""Scheduled-job wiring.

The proactive package was fully written but never called: david.py's __main__
registered send_daily_reminders and send_weekly_budget directly, so the morning
briefing, evening briefing and budget pacing never ran. Nothing failed loudly —
the jobs simply did not exist.

These tests drive the real register_jobs() against a real Application, so a job
that is never attached fails here instead of silently not firing at 07:30.
"""

import pytest
from telegram.ext import ApplicationBuilder

import david
from conftest import FakeContext, run

MORNING   = "morning_briefing"
EVENING   = "evening_briefing"
PACING    = "budget_pacing"
WEEKLY    = "budget_recap"
ROLLOVER  = "month_rollover"
HEARTBEAT = "heartbeat"
NUDGE     = "learn_nudge"

CHAT_ID = "-1001234567"


@pytest.fixture
def scheduled():
    """A real Application with the real jobs attached. No network at build time."""
    application = ApplicationBuilder().token("123456:test-token").build()
    assert david.register_jobs(application, CHAT_ID) is True
    return application


def jobs_by_name(application):
    return {job.name: job for job in application.job_queue.jobs()}


def trigger_fields(job):
    """(hour, minute) the APScheduler cron trigger will fire at."""
    fields = {f.name: str(f) for f in job.job.trigger.fields}
    return int(fields["hour"]), int(fields["minute"])


# ─── EVERY JOB IS ATTACHED ─────────────────────────────────────────────────────

def test_every_job_is_registered(scheduled):
    """The regression this fixes: three of these were previously never attached."""
    assert set(jobs_by_name(scheduled)) == {
        MORNING, EVENING, PACING, WEEKLY, ROLLOVER, HEARTBEAT, NUDGE}


@pytest.mark.parametrize("name, hour, minute", [
    (ROLLOVER, 0, 5),
    (MORNING, 7, 30),
    (PACING, 13, 0),
    (EVENING, 20, 0),
    (WEEKLY, 9, 30),
    (HEARTBEAT, 20, 30),
    (NUDGE, 10, 0),
])
def test_each_job_fires_at_its_configured_time(scheduled, name, hour, minute):
    assert trigger_fields(jobs_by_name(scheduled)[name]) == (hour, minute)


def test_every_job_targets_the_owner_chat(scheduled):
    for name, job in jobs_by_name(scheduled).items():
        assert job.chat_id == CHAT_ID or name == WEEKLY, (
            f"{name} would send to {job.chat_id!r}")


def test_the_budget_recap_runs_on_sunday_only(scheduled):
    """python-telegram-bot v20 renumbered days from monday-sunday to SUNDAY-saturday.

    The original days=(5, 6) carried a "0=Mon ... 6=Sun" comment written for v13,
    so under v21+ this fired Friday and Saturday. It is now Sunday only, written
    as config.SUNDAY rather than a bare integer.

    Asserts the weekday NAMES the trigger resolves to, not the integers passed
    in, so a future renumbering fails here too.
    """
    days = str(jobs_by_name(scheduled)[WEEKLY].job.trigger.fields[4])

    assert set(days.split(",")) == {"sun"}, f"budget recap fires on {days}"


def test_the_daily_jobs_run_every_day(scheduled):
    """month_rollover included: it only has work to do on the 1st, but it is
    scheduled daily so a missed or failed rollover retries the next night rather
    than a month later."""
    for name in (MORNING, EVENING, PACING, ROLLOVER):
        days = str(jobs_by_name(scheduled)[name].job.trigger.fields[4])
        assert "mon" in days and "sun" in days, f"{name} does not run daily"


# ─── NO DOUBLE-MESSAGING ───────────────────────────────────────────────────────

def test_only_one_job_owns_the_0730_slot(scheduled):
    """The old send_daily_reminders also fired at 07:30 and also sent today's
    events. Running both would send them twice, every morning."""
    at_0730 = [name for name, job in jobs_by_name(scheduled).items()
               if trigger_fields(job) == (7, 30)]

    assert at_0730 == [MORNING]


def test_the_superseded_daily_reminder_job_is_gone():
    """Its content is now split across the morning and evening briefings."""
    assert not hasattr(david, "send_daily_reminders")


def test_no_two_jobs_share_a_slot(scheduled):
    slots = [trigger_fields(job) for job in scheduled.job_queue.jobs()]

    assert len(slots) == len(set(slots)), f"two jobs fire at the same time: {slots}"


# ─── DEGRADED STARTUP ──────────────────────────────────────────────────────────

def test_a_missing_job_queue_does_not_stop_the_bot(monkeypatch):
    """Without the [job-queue] extra, commands must still work."""
    application = ApplicationBuilder().token("123456:test-token").build()
    monkeypatch.setattr(type(application), "job_queue", property(lambda self: None))

    assert david.register_jobs(application, CHAT_ID) is False


def test_a_scheduling_failure_does_not_stop_the_bot(monkeypatch):
    class Boom:
        def run_daily(self, *a, **kw):
            raise RuntimeError("scheduler exploded")

    application = ApplicationBuilder().token("123456:test-token").build()
    monkeypatch.setattr(type(application), "job_queue", property(lambda self: Boom()))

    assert david.register_jobs(application, CHAT_ID) is False


# ─── THE JOB CALLBACKS ─────────────────────────────────────────────────────────
# register_jobs proves the jobs exist; these prove they send what they compose.

@pytest.fixture
def job_context():
    context = FakeContext()
    context.job = type("Job", (), {"chat_id": CHAT_ID})()
    return context


# Every builder returns (text, error). The three states the send path must keep
# apart, asserted for all five jobs.
BUILDERS = [
    (MORNING,   "build_morning_briefing"),
    (EVENING,   "build_evening_briefing"),
    (PACING,    "build_pacing_warning"),
    (ROLLOVER,  "build_rollover_message"),
    (HEARTBEAT, "build_heartbeat"),
]


@pytest.mark.parametrize("job_name, builder", BUILDERS)
def test_job_sends_the_text_it_composed(scheduled, job_context, monkeypatch,
                                        job_name, builder):
    import proactive.scheduler as sched

    monkeypatch.setattr(sched, builder, lambda: ("COMPOSED TEXT", None))
    run(jobs_by_name(scheduled)[job_name].callback(job_context))

    assert job_context.bot.sent == [(CHAT_ID, "COMPOSED TEXT")]


@pytest.mark.parametrize("job_name, builder", BUILDERS)
def test_job_stays_silent_when_there_is_genuinely_nothing_to_say(
        scheduled, job_context, monkeypatch, job_name, builder):
    """(None, None) means 'no news' — no empty ping, and nothing to report."""
    import proactive.scheduler as sched

    monkeypatch.setattr(sched, builder, lambda: (None, None))
    run(jobs_by_name(scheduled)[job_name].callback(job_context))

    assert job_context.bot.sent == []


@pytest.mark.parametrize("job_name, builder", BUILDERS)
def test_job_reports_a_returned_error_instead_of_dropping_it(
        scheduled, job_context, monkeypatch, job_name, builder):
    """THE FIX. A builder that returns an error must not go quiet.

    Every callback used to be `if text: send(text)` and nothing else, so an error
    returned alongside a None was discarded — four separate places where a
    calendar or Notion failure became indistinguishable from a quiet day.
    """
    import proactive.scheduler as sched

    monkeypatch.setattr(sched, builder, lambda: (None, "Could not fetch events: 401"))
    run(jobs_by_name(scheduled)[job_name].callback(job_context))

    assert len(job_context.bot.sent) == 1, f"{job_name} swallowed a returned error"
    assert "Could not fetch events: 401" in job_context.bot.sent[0][1]


@pytest.mark.parametrize("job_name, builder", BUILDERS)
def test_job_sends_the_text_and_reports_the_error_when_it_gets_both(
        scheduled, job_context, monkeypatch, job_name, builder):
    """The degraded-but-useful case: the morning briefing during a calendar outage."""
    import proactive.scheduler as sched

    monkeypatch.setattr(sched, builder, lambda: ("DEGRADED TEXT", "calendar down"))
    run(jobs_by_name(scheduled)[job_name].callback(job_context))

    bodies = [text for _, text in job_context.bot.sent]
    assert "DEGRADED TEXT" in bodies
    assert any("calendar down" in body for body in bodies)


def test_a_crashing_job_reports_instead_of_dying_silently(scheduled, job_context,
                                                          monkeypatch):
    import proactive.scheduler as sched

    def boom():
        raise ValueError("calendar is down")

    monkeypatch.setattr(sched, "build_morning_briefing", boom)
    run(jobs_by_name(scheduled)[MORNING].callback(job_context))

    assert len(job_context.bot.sent) == 1
    assert "ValueError" in job_context.bot.sent[0][1]


def test_error_reports_from_jobs_are_plain_text(scheduled, job_context, monkeypatch):
    """The report must not be able to fail on the error it is reporting."""
    import proactive.scheduler as sched

    monkeypatch.setattr(sched, "build_morning_briefing",
                        lambda: (None, "body.properties.rich_text *broken_"))
    run(jobs_by_name(scheduled)[MORNING].callback(job_context))

    _, _, kwargs = job_context.bot.sent_full[0]
    assert kwargs.get("parse_mode") is None


# ─── THE HEARTBEAT ─────────────────────────────────────────────────────────────

def test_the_heartbeat_runs_weekly_on_sunday(scheduled):
    """Weekly, against a rhythm — so a MISSING message is itself noticeable."""
    days = str(jobs_by_name(scheduled)[HEARTBEAT].job.trigger.fields[4])

    assert set(days.split(",")) == {"sun"}, f"heartbeat fires on {days}"


def test_the_heartbeat_does_not_collide_with_the_evening_briefing(scheduled):
    """Both are evening jobs; 20:00 and 20:30 must stay distinct."""
    jobs = jobs_by_name(scheduled)

    assert trigger_fields(jobs[EVENING]) != trigger_fields(jobs[HEARTBEAT])


# ─── THE LEARN NUDGE ───────────────────────────────────────────────────────────

def test_the_learn_nudge_runs_weekly_on_saturday(scheduled):
    """Weekly, for the mirror of the heartbeat's reason: the heartbeat always
    speaks so daily would be noise, and this one's list barely changes between
    one day and the next, so daily would be the same noise."""
    days = str(jobs_by_name(scheduled)[NUDGE].job.trigger.fields[4])

    assert set(days.split(",")) == {"sat"}, f"learn nudge fires on {days}"


def test_the_learn_nudge_has_the_saturday_morning_slot_to_itself(scheduled):
    """test_no_two_jobs_share_a_slot covers collisions in general; this names the
    slot, so moving the nudge onto an occupied one fails with the reason."""
    jobs = jobs_by_name(scheduled)

    assert trigger_fields(jobs[NUDGE]) == (10, 0)
    assert [name for name, job in jobs.items()
            if trigger_fields(job) == (10, 0)] == [NUDGE]
