"""parse_date_time — which YEAR a bare DD.MM means, and the two hours that aren't.

THE BUG THIS LOCKS DOWN
-----------------------
The rule used to be "if the parsed datetime is in the past, roll to year + 1".
That is right for `12.06` sent in December and catastrophic for `06.08 - 09.00`
sent at 10:00 on the 6th of August: an appointment an hour ago became an event in
AUGUST NEXT YEAR. Nothing errored, the confirmation read normally, and the ping
never came — the failure surfaces as silence, twelve months later.

The rule is now: more than a day past, roll it; inside a day, ASK. The year is
optional in the command precisely so there is a way to answer.

The second half is DST. `TIMEZONE.localize()` defaults to is_dst=False, so the
hour that does not exist at the start of summer time was silently shifted, and
the hour that happens twice at the end was silently resolved to one of them. Both
produce an event an hour from where it was asked for, which is indistinguishable
from a typo.

Europe/Rome transitions, verified against pytz rather than assumed:
  2026-03-29  02:00 -> 03:00   (02:00-02:59 does not exist)
  2026-10-25  03:00 -> 02:00   (02:00-02:59 happens twice)
"""

from datetime import datetime

import pytest

import calendar_client
import reminder
from conftest import FakeUpdate, run


def freeze(monkeypatch, *, year, month, day, hour, minute=0):
    """Pin calendar_client's clock to a Europe/Rome wall-clock instant."""
    pinned = calendar_client.TIMEZONE.localize(
        datetime(year, month, day, hour, minute))

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned.astimezone(tz) if tz else pinned.replace(tzinfo=None)

    monkeypatch.setattr(calendar_client, "datetime", FrozenClock)
    return pinned


# ─── WHICH YEAR A BARE DD.MM MEANS ─────────────────────────────────────────────

def test_an_appointment_earlier_today_is_refused_not_booked_next_year(monkeypatch):
    """THE BUG, exactly as reported.

    10:00 on the 6th, a reminder for 09:00 on the 6th. The old rule read that as
    August NEXT year and said "Reminder set!". One hour past is not evidence that
    someone meant twelve months ahead.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("06.08", "09.00")

    assert dt is None, f"booked {dt} instead of asking"
    assert "already past" in err
    assert "06.08.2027" in err, "the refusal must say how to confirm next year"


def test_a_date_comfortably_in_the_past_still_rolls_to_next_year(monkeypatch):
    """The useful half of the old behaviour, kept: in December, `12.06` obviously
    means next June. That is months past, not hours."""
    freeze(monkeypatch, year=2026, month=12, day=20, hour=10)

    dt, err = calendar_client.parse_date_time("12.06", "14.30")

    assert err is None
    assert (dt.year, dt.month, dt.day) == (2027, 6, 12)


def test_a_future_date_this_year_is_left_alone(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("06.08", "14.00")

    assert err is None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 8, 6, 14)


@pytest.mark.parametrize("hours_ago, rolls", [
    (23, False),   # inside the grace window — ambiguous, so it asks
    (25, True),    # outside it — "next year" is the only sensible reading
])
def test_the_grace_window_is_where_the_behaviour_changes(monkeypatch, hours_ago, rolls):
    """PAST_GRACE is 24h. Either side of it the answer is different, and the
    boundary is the whole point of the fix — so it is asserted, not assumed."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=12)
    target = datetime(2026, 8, 6, 12) - calendar_client.timedelta(hours=hours_ago)

    dt, err = calendar_client.parse_date_time(
        f"{target.day:02d}.{target.month:02d}", f"{target.hour:02d}.{target.minute:02d}")

    if rolls:
        assert err is None and dt.year == 2027
    else:
        assert dt is None and "already past" in err


# ─── AN EXPLICIT YEAR IS THE CONFIRMATION ──────────────────────────────────────

def test_an_explicit_year_is_taken_at_face_value(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("06.08.2027", "09.00")

    assert err is None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2027, 8, 6, 9)


def test_an_explicit_year_in_the_past_is_not_second_guessed(monkeypatch):
    """Spelling out a year is an answer, not a question. Whatever it says goes —
    the refusal exists for the case where nobody said."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("06.08.2025", "09.00")

    assert err is None
    assert dt.year == 2025


def test_a_two_digit_year_is_read_as_this_century(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("06.08.27", "09.00")

    assert err is None and dt.year == 2027


def test_a_date_with_too_many_parts_is_rejected(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("06.08.2027.1", "09.00")

    assert dt is None and "Invalid date format" in err


# ─── THE TWO HOURS THAT AREN'T ─────────────────────────────────────────────────

def test_a_time_that_does_not_exist_is_refused(monkeypatch):
    """29.03.2026: the clocks go forward, 02:00 becomes 03:00, and 02:30 never
    happens. pytz's default quietly shifted it and booked 01:30 UTC instead."""
    freeze(monkeypatch, year=2026, month=1, day=15, hour=10)

    dt, err = calendar_client.parse_date_time("29.03", "02.30")

    assert dt is None, f"silently shifted a nonexistent time to {dt}"
    assert "doesn't exist" in err
    assert "02:00" in err and "03:00" in err, "the message must say which hour"


def test_a_time_that_happens_twice_is_refused(monkeypatch):
    """25.10.2026: the clocks go back, so 02:30 names two instants an hour apart
    and picking one silently is a coin flip the user never sees."""
    freeze(monkeypatch, year=2026, month=1, day=15, hour=10)

    dt, err = calendar_client.parse_date_time("25.10", "02.30")

    assert dt is None, f"silently resolved an ambiguous time to {dt}"
    assert "happens twice" in err


def test_an_ordinary_time_is_unaffected_by_the_dst_check(monkeypatch):
    """The guard must not make normal days refuse. 03:30 on the same night is a
    single real instant."""
    freeze(monkeypatch, year=2026, month=1, day=15, hour=10)

    dt, err = calendar_client.parse_date_time("29.03", "03.30")

    assert err is None
    assert (dt.month, dt.day, dt.hour) == (3, 29, 3)


def test_the_next_year_rollover_is_dst_checked_too(monkeypatch):
    """The rollover builds a SECOND datetime, and it needs the same guard — a
    year+1 date landing in the skipped hour would otherwise be shifted silently
    on the very path this module exists to make trustworthy."""
    freeze(monkeypatch, year=2026, month=12, day=20, hour=10)

    dt, err = calendar_client.parse_date_time("28.03", "02.30")   # 28.03.2027 is the switch

    assert dt is None
    assert "doesn't exist" in err


# ─── IMPOSSIBLE DATES ──────────────────────────────────────────────────────────

def test_a_date_that_does_not_exist_at_all_is_rejected(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("31.02", "09.00")

    assert dt is None and "not a real date" in err


def test_29_february_rolling_into_a_non_leap_year_is_reported(monkeypatch):
    """2028 is a leap year, 2029 is not. The rollover has to say so rather than
    raise ValueError out of a handler."""
    freeze(monkeypatch, year=2028, month=3, day=1, hour=10)

    dt, err = calendar_client.parse_date_time("29.02", "09.00")

    assert dt is None
    assert "2029" in err


# ─── THE CONFIRMATION ──────────────────────────────────────────────────────────

@pytest.fixture
def calendar(monkeypatch):
    monkeypatch.setattr(reminder, "find_conflicts", lambda start, end: ([], None))
    monkeypatch.setattr(reminder, "create_event",
                        lambda name, start, minutes: ("https://cal/link", None))


def test_the_confirmation_spells_the_year_out(monkeypatch, calendar):
    """It used to read "06.08.2027 at 09:00" — one wrong digit mid-string between
    two correct ones, which is exactly the shape the eye skips. A weekday-first
    long form cannot be skimmed past."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    update = FakeUpdate(text="Remind Dentist 06.08.2027 - 09.00")

    run(reminder.handle_remind(update, update.message.text))

    assert update.message.replied_with("Friday 06 August 2027 at 09:00")


def test_the_confirmation_calls_out_a_different_year(monkeypatch, calendar):
    """When the reminder is not for this year — whether rolled or asked for — say
    so in its own line. This is the check that would have caught the bug."""
    freeze(monkeypatch, year=2026, month=12, day=20, hour=10)
    update = FakeUpdate(text="Remind Dentist 12.06 - 14.30")

    run(reminder.handle_remind(update, update.message.text))

    assert update.message.replied_with("that is *2027*, not this year")


def test_a_reminder_this_year_does_not_get_the_year_warning(monkeypatch, calendar):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    update = FakeUpdate(text="Remind Dentist 06.08 - 14.00")

    run(reminder.handle_remind(update, update.message.text))

    assert update.message.replied_with("Reminder set!")
    assert not update.message.replied_with("not this year")


def test_the_refusal_reaches_the_user_instead_of_an_event(monkeypatch, calendar):
    """End to end: the ambiguous case must stop before create_event."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    created = []
    monkeypatch.setattr(reminder, "create_event",
                        lambda name, start, minutes: (created.append(start), "")[1])
    update = FakeUpdate(text="Remind Dentist 06.08 - 09.00")

    run(reminder.handle_remind(update, update.message.text))

    assert created == [], "created an event for a date it was supposed to query"
    assert update.message.replied_with("already past")


def test_the_command_still_accepts_a_bare_date(monkeypatch, calendar):
    """The optional year must not have broken the common form."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    update = FakeUpdate(text="Remind Dentist 12.09 - 14.30")

    run(reminder.handle_remind(update, update.message.text))

    assert update.message.replied_with("Reminder set!")
