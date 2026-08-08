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

import re
from datetime import datetime

import pytest

import calendar_client
import david
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


# ─── THE HELP AND THE PARSER MUST AGREE ────────────────────────────────────────

def test_the_help_advertises_only_date_forms_the_parser_accepts(monkeypatch):
    """`h` is generated from david.COMMANDS, so the reminder's advertised forms
    are structured data — which means they can be filled in and RUN rather than
    read. A help entry offering a syntax the parser rejects is the same drift
    class as `Learn recipe`, one module over, and this is the check that has it.

    It is also the test that was missing when the optional year landed: the
    command grew a format and the help entry did not mention it, which nothing
    caught because nothing ran the help against the parser.
    """
    freeze(monkeypatch, year=2026, month=1, day=15, hour=10)
    remind = next(c for c in david.COMMANDS if c.name == "Remind")
    values = {"[Name]": "Dentist", "[DD.MM.YYYY]": "12.06.2027",
              "[DD.MM]": "12.06", "[HH.MM]": "14.30", "[HH]": "10"}

    for usage in remind.help.usage:
        example = usage
        for placeholder, value in values.items():
            example = example.replace(placeholder, value)
        assert "[" not in example, (
            f"{usage!r} uses a placeholder this test does not know how to fill")

        assert remind.pattern.fullmatch(example), (
            f"the help advertises {example!r}, which the router does not route")

        parsed = re.match(reminder.REMIND_PATTERN, example)
        assert parsed, (
            f"the help advertises {example!r}, which the reminder pattern rejects")

        _, err = calendar_client.parse_date_time(parsed["date"], parsed["time"])
        assert err is None, (
            f"the help advertises {example!r}, which the parser rejects: {err}")


# ─── `t` FOR TOMORROW, AND A BARE HOUR ─────────────────────────────────────────
# `Remind Dentist t 10` — the shorthand that gets typed in practice. Three things
# have to give at once for it: the date token, the bare hour, and the dash.

def parse_command(text):
    """Route a whole Remind command the way handle_remind does."""
    match = re.match(reminder.REMIND_PATTERN, text.strip())
    if not match:
        return None, None, "no match"
    dt, err = calendar_client.parse_date_time(match["date"], match["time"])
    return match["name"], dt, err


def test_t_and_a_bare_hour_book_tomorrow_at_oclock(monkeypatch):
    """THE REQUESTED FORM, end to end through the real pattern and parser."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    name, dt, err = parse_command("Remind Dentist t 10")

    assert err is None
    assert name == "Dentist"
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 7, 10, 0)


@pytest.mark.parametrize("text, expected_hour, expected_minute", [
    ("Remind Dentist t 10",        10, 0),    # the short form
    ("Remind Dentist tomorrow 10", 10, 0),    # the long one, for anyone who forgets `t`
    ("Remind Dentist t 10.30",     10, 30),   # shorthand date, full time
    ("Remind Dentist t - 10",      10, 0),    # the dash still works if you type it
    ("Remind Dentist T 10",        10, 0),    # case-insensitive like every command
])
def test_the_accepted_tomorrow_forms(monkeypatch, text, expected_hour, expected_minute):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    _, dt, err = parse_command(text)

    assert err is None, f"{text!r} was rejected: {err}"
    assert (dt.day, dt.hour, dt.minute) == (7, expected_hour, expected_minute)


def test_a_multi_word_name_still_stops_at_the_shorthand(monkeypatch):
    """The name is non-greedy, so it has to give up exactly at the date token —
    not at the first space, and not swallow the date."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    name, dt, err = parse_command("Remind Call the plumber t 9")

    assert err is None
    assert name == "Call the plumber"
    assert (dt.day, dt.hour) == (7, 9)


def test_a_bare_hour_works_with_a_full_date_too(monkeypatch):
    """The two shorthands are independent — neither requires the other."""
    freeze(monkeypatch, year=2026, month=1, day=15, hour=10)

    _, dt, err = parse_command("Remind Dentist 12.06 14")

    assert err is None
    assert (dt.month, dt.day, dt.hour, dt.minute) == (6, 12, 14, 0)


def test_the_long_form_is_untouched(monkeypatch):
    """The regression that matters: the shorthand must not cost the old syntax."""
    freeze(monkeypatch, year=2026, month=1, day=15, hour=10)

    for text, expected in [("Remind Dentist 12.06 - 14.30", (2026, 6, 12, 14, 30)),
                           ("Remind Dentist 12.06.2027 - 14.30", (2027, 6, 12, 14, 30))]:
        _, dt, err = parse_command(text)
        assert err is None, f"{text!r} broke: {err}"
        assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == expected


# ─── WHAT THE SHORTHAND MUST NOT SWALLOW ───────────────────────────────────────

def test_today_does_not_silently_become_tomorrow(monkeypatch):
    """"today" starts with a `t`, and must not be read as the shorthand.

    It is rejected because the time group cannot match the "oday" the token
    leaves behind — not by the lookahead, which was verified by removing the
    lookahead and watching this test still pass. The case is worth pinning
    anyway: a one-day silent error is the same shape as the year bug this
    module was just fixed for, and the next person to touch the pattern should
    find out here rather than in the calendar.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command("Remind Dentist today 10")

    assert (name, dt) == (None, None)
    assert err == "no match", "the `t` shorthand swallowed the start of 'today'"


def test_a_t_inside_the_name_does_not_become_the_date(monkeypatch):
    """THE LOOKAHEAD AFTER THE DATE, and the case that actually needs it.

    Without it, `t` matches the leading letter of any t-word and the remainder
    is skipped as separator noise. `Remind Bus t4 to town 10` then parses as
    name "Bus", tomorrow, 04:00 — a reminder with the wrong name AND the wrong
    time, confirmed as though it were what was asked for.

    Refusing costs the run-together form (`t10` is not accepted, `t 10` is).
    A space is one keystroke; a wrong booking is invisible.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command("Remind Bus t4 to town 10")

    assert (name, dt) == (None, None)
    assert err == "no match", "a `t` inside the name was taken as the date"


def test_the_shorthand_needs_its_space(monkeypatch):
    """The accepted cost of the rule above, pinned so it is a decision rather
    than a surprise."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    assert parse_command("Remind Dentist t10")[2] == "no match"
    assert parse_command("Remind Dentist t 10")[2] is None


def test_a_run_together_time_is_refused_rather_than_truncated(monkeypatch):
    """THE LOOKAHEAD AFTER THE TIME. `1030` is a typo for 10.30, and a bare-hour
    pattern will happily match its first two digits and book 10:00 — an event
    half an hour early, confirmed as though it were asked for."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command("Remind Dentist t 1030")

    assert (name, dt) == (None, None)
    assert err == "no match", "a bare hour matched the leading digits of a typo"


def test_a_shorthand_with_no_time_is_not_a_command(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    assert parse_command("Remind Dentist t")[2] == "no match"


def test_an_out_of_range_bare_hour_is_reported(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("t", "25")

    assert dt is None
    assert "Hour 0-23" in err


# ─── THE SHORTHAND GOES THROUGH THE SAME GUARDS ────────────────────────────────

def test_tomorrow_is_dst_checked_like_any_other_date(monkeypatch):
    """29.03.2026 is the spring-forward day, so on the 28th `t 02.30` names an
    hour that does not exist. The shorthand is a shortcut for typing a date, not
    for skipping the checks that make a date trustworthy."""
    freeze(monkeypatch, year=2026, month=3, day=28, hour=10)

    _, dt, err = parse_command("Remind Dentist t 02.30")

    assert dt is None
    assert "doesn't exist" in err


def test_tomorrow_is_never_in_the_past_so_it_is_never_queried(monkeypatch):
    """Late at night, `t 00.30` is barely half an hour away — still tomorrow, and
    still future, so the PAST_GRACE question never arises for this form."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=23, minute=50)

    _, dt, err = parse_command("Remind Dentist t 00.30")

    assert err is None
    assert (dt.day, dt.hour, dt.minute) == (7, 0, 30)


def test_the_confirmation_spells_out_the_day_the_shorthand_resolved_to(monkeypatch, calendar):
    """`t` is terse enough to mistype or misremember, so the safeguard is the
    confirmation the year work already added: it names the weekday and the full
    date, which is exactly what makes a wrong day obvious at a glance."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)
    update = FakeUpdate(text="Remind Dentist t 10")

    run(reminder.handle_remind(update, update.message.text))

    assert update.message.replied_with("Friday 07 August 2026 at 10:00")
