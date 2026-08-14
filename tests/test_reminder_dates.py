"""parse_date_time — which DAY a shorthand means, which YEAR a bare DD.MM means,
and the two hours that aren't.

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

THE THIRD SUBJECT, added later: which DAY a shorthand names. There was one token,
`t`, and it meant tomorrow — a single letter for one of two adjacent days, with no
room for today beside it and nothing in the letter itself to say which it had
picked. It is now a matched pair, `td` and `tr`, and `t` is refused by name rather
than quietly reassigned. `td` also brings the one thing `t` never could: a time
that has already passed, which books an event nothing can ping for.
"""

import re
from datetime import datetime

import pytest

from clients import calendar_client
import david
from conftest import FakeUpdate, run, with_update
from services import reminder


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

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert update.message.replied_with("Friday 06 August 2027 at 09:00")


def test_the_confirmation_calls_out_a_different_year(monkeypatch, calendar):
    """When the reminder is not for this year — whether rolled or asked for — say
    so in its own line. This is the check that would have caught the bug."""
    freeze(monkeypatch, year=2026, month=12, day=20, hour=10)
    update = FakeUpdate(text="Remind Dentist 12.06 - 14.30")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert update.message.replied_with("that is *2027*, not this year")


def test_a_reminder_this_year_does_not_get_the_year_warning(monkeypatch, calendar):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    update = FakeUpdate(text="Remind Dentist 06.08 - 14.00")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert update.message.replied_with("Reminder set!")
    assert not update.message.replied_with("not this year")


def test_the_refusal_reaches_the_user_instead_of_an_event(monkeypatch, calendar):
    """End to end: the ambiguous case must stop before create_event."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    created = []
    monkeypatch.setattr(reminder, "create_event",
                        lambda name, start, minutes: (created.append(start), "")[1])
    update = FakeUpdate(text="Remind Dentist 06.08 - 09.00")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert created == [], "created an event for a date it was supposed to query"
    assert update.message.replied_with("already past")


def test_the_command_still_accepts_a_bare_date(monkeypatch, calendar):
    """The optional year must not have broken the common form."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    update = FakeUpdate(text="Remind Dentist 12.09 - 14.30")

    run(reminder.run_remind(update.message.text, **with_update(update)))

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

    THE CLOCK IS 06:00, and that is not arbitrary. `[HH]` fills as 10, so with
    `td [HH]` advertised a 10:00 clock puts the example exactly ON now — which
    passes, because the past check is strictly-before, but passes on the boundary
    for a reason that has nothing to do with the help agreeing with the parser.
    An early clock makes every advertised form genuinely future.
    """
    freeze(monkeypatch, year=2026, month=1, day=15, hour=6)
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


# ─── `td` FOR TODAY, `tr` FOR TOMORROW, AND A BARE HOUR ────────────────────────
# `Remind Dentist tr 10` — the shorthand that gets typed in practice. Three
# things have to give at once for it: the date token, the bare hour, and the dash.
#
# There used to be one token, `t`, and it meant tomorrow. A single letter for one
# of two adjacent days left no room for today and did not say which day it had
# picked, so it became a matched pair: `td` today, `tr` tomorrow. `t` itself is
# retired, and its own section below covers what happens when it is typed.

def parse_command(text):
    """Route a whole Remind command the way run_remind does."""
    match = re.match(reminder.REMIND_PATTERN, text.strip())
    if not match:
        return None, None, "no match"
    dt, err = calendar_client.parse_date_time(match["date"], match["time"])
    return match["name"], dt, err


def test_tr_and_a_bare_hour_book_tomorrow_at_oclock(monkeypatch):
    """THE COMMON FORM, end to end through the real pattern and parser."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    name, dt, err = parse_command("Remind Dentist tr 10")

    assert err is None
    assert name == "Dentist"
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 7, 10, 0)


def test_td_and_a_bare_hour_book_today_at_oclock(monkeypatch):
    """The other half of the pair, and the day the command could not name before."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    name, dt, err = parse_command("Remind Dentist td 18")

    assert err is None
    assert name == "Dentist"
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 8, 6, 18, 0)


@pytest.mark.parametrize("token, expected_day", [
    ("td",       6),
    ("today",    6),
    ("tr",       7),
    ("tomorrow", 7),
])
def test_every_date_token_resolves_to_the_day_it_names(monkeypatch, token, expected_day):
    """The one thing this change must not get wrong: four tokens, two of them one
    letter apart, three starting with the same letter as a fourth that is retired.

    NOT an alternation-order guard, though it was written as one. Reversing
    `REMIND_PATTERN`'s alternation to `t|td|tr|today|tomorrow` leaves this green —
    the lookahead after the date group makes the order irrelevant, because `t`
    matching the head of "today" fails it and the engine backtracks. Verified,
    and recorded in the pattern's own comment so the next reader does not take
    the ordering for protection.

    It earns its place regardless: it drives every live token through the real
    pattern and the real parser and asserts the DAY that came out. That property
    has to hold however the pattern is later rearranged, and this is what says so.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    name, dt, err = parse_command(f"Remind Dentist {token} 18")

    assert err is None, f"{token!r} was rejected: {err}"
    assert name == "Dentist", f"{token!r} left {name!r} as the name"
    assert (dt.month, dt.day, dt.hour) == (8, expected_day, 18), (
        f"{token!r} resolved to {dt}, not day {expected_day}")


@pytest.mark.parametrize("text, expected_day, expected_hour, expected_minute", [
    ("Remind Dentist tr 10",        7, 10, 0),   # the short form
    ("Remind Dentist tomorrow 10",  7, 10, 0),   # the long one, for anyone who forgets
    ("Remind Dentist td 18",        6, 18, 0),   # today, short
    ("Remind Dentist today 18",     6, 18, 0),   # today, long
    ("Remind Dentist tr 10.30",     7, 10, 30),  # shorthand date, full time
    ("Remind Dentist tr - 10",      7, 10, 0),   # the dash still works if you type it
    ("Remind Dentist TR 10",        7, 10, 0),   # case-insensitive like every command
    ("Remind Dentist Td 18",        6, 18, 0),
])
def test_the_accepted_shorthand_forms(monkeypatch, text, expected_day,
                                      expected_hour, expected_minute):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    _, dt, err = parse_command(text)

    assert err is None, f"{text!r} was rejected: {err}"
    assert (dt.day, dt.hour, dt.minute) == (expected_day, expected_hour, expected_minute)


def test_a_multi_word_name_still_stops_at_the_shorthand(monkeypatch):
    """The name is non-greedy, so it has to give up exactly at the date token —
    not at the first space, and not swallow the date."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    name, dt, err = parse_command("Remind Call the plumber tr 9")

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


# ─── `t` IS RETIRED, AND SAYS SO ───────────────────────────────────────────────
# It meant tomorrow. It now sits one letter from BOTH replacements, so any silent
# reading of it is a coin flip between two adjacent days — the exact failure this
# module's whole history is about. It stays a legal TOKEN so the refusal can name
# it; what it MEANS is "neither day", decided in calendar_client like every other
# date rule.

def test_a_bare_t_is_refused_by_name(monkeypatch):
    """Not the generic usage message. `t` is what changed, so `t` is what the
    reply is about — and it names BOTH replacements, because naming only `tr`
    ("it used to mean tomorrow") is a nudge toward one of two days at the moment
    David has no idea which was meant."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command("Remind Dentist t 10")

    assert dt is None, f"`t` booked {dt} instead of asking"
    assert name == "Dentist", "the pattern must still parse it, so it can be refused"
    assert "no longer a date" in err
    assert "`tr 10`" in err, f"the refusal did not offer tomorrow: {err}"
    assert "`td 10`" in err, f"the refusal did not offer today: {err}"


def test_a_bare_t_is_refused_before_the_time_is_even_looked_at(monkeypatch):
    """Ordering, and it is a decision. With a bad time as well, `t` is the thing
    to fix either way — an "Invalid time" message would send the reader after the
    wrong problem and leave them to discover the token separately."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("t", "99")

    assert dt is None
    assert "no longer a date" in err, f"reported the time instead of the token: {err}"


def test_a_bare_t_never_reaches_create_event(monkeypatch, calendar):
    """End to end through the real handler: the refusal has to stop before the
    calendar, not merely be worded well."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    created = []
    monkeypatch.setattr(reminder, "create_event",
                        lambda name, start, minutes: (created.append(start), "")[1])
    update = FakeUpdate(text="Remind Dentist t 10")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert created == [], "created an event for a token that names no day"
    assert update.message.replied_with("no longer a date")


def test_retiring_t_did_not_retire_the_word_tomorrow(monkeypatch):
    """The obvious over-correction: `t` goes, `tomorrow` stays. A shorthand
    nobody remembers is worse than none, which is why the long forms exist."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)

    _, dt, err = parse_command("Remind Dentist tomorrow 10")

    assert err is None, f"'tomorrow' was caught by the `t` refusal: {err}"
    assert (dt.day, dt.hour) == (7, 10)


# ─── WHAT THE SHORTHANDS MUST NOT SWALLOW ──────────────────────────────────────

def test_today_and_td_mean_today_not_tomorrow(monkeypatch):
    """THE ONE-DAY ERROR, pinned from the other side.

    This test used to assert that "today" did not parse at all — it was rejected
    because the time group could not match the "oday" the old `t` token left
    behind, which the docstring recorded as luck rather than design. "today" is a
    legal token now, so the anxiety it was written for is the same and the
    assertion is stronger: it must resolve to TODAY.

    A one-day silent error is the same shape as the next-year bug the top of this
    module exists for, and it is now the alternation order that stands between
    the two, not an accident of what the time group could not match.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    for token in ("today", "td"):
        name, dt, err = parse_command(f"Remind Dentist {token} 18")

        assert err is None, f"{token!r} was rejected: {err}"
        assert name == "Dentist"
        assert dt.day == 6, f"{token!r} resolved to day {dt.day}, not today"


@pytest.mark.parametrize("text, token", [
    ("Remind Bus t4 to town 10",  "t"),
    ("Remind Bus td4 to town 10", "td"),
    ("Remind Bus tr4 to town 10", "tr"),
])
def test_a_shorthand_inside_the_name_does_not_become_the_date(monkeypatch, text, token):
    """THE LOOKAHEAD AFTER THE DATE, and the case that actually needs it.

    Without it, a shorthand matches the leading letters of any word starting the
    same way and the remainder becomes the TIME. `Remind Bus td4 to town 10`
    parses as name "Bus", today, 04:00 — wrong name AND wrong time, confirmed as
    though it were what was asked for.

    The guard predates the new tokens and covers them unchanged, which is exactly
    why they are listed here: a lookahead whose only test names the token it was
    written against is half a guard, and the half it is missing is the half that
    ships next. Verified by deleting `(?![\\w.])` and watching all three go red.

    Refusing costs the run-together form (`td10` is not accepted, `td 10` is).
    A space is one keystroke; a wrong booking is invisible.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command(text)

    assert (name, dt) == (None, None)
    assert err == "no match", f"a `{token}` inside the name was taken as the date"


@pytest.mark.parametrize("token", ["td", "tr"])
def test_the_shorthand_needs_its_space(monkeypatch, token):
    """The accepted cost of the rule above, pinned so it is a decision rather
    than a surprise."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    assert parse_command(f"Remind Dentist {token}18")[2] == "no match"
    assert parse_command(f"Remind Dentist {token} 18")[2] is None


def test_a_run_together_time_is_refused_rather_than_truncated(monkeypatch):
    """THE LOOKAHEAD AFTER THE TIME. `1030` is a typo for 10.30, and a bare-hour
    pattern will happily match its first two digits and book 10:00 — an event
    half an hour early, confirmed as though it were asked for."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command("Remind Dentist tr 1030")

    assert (name, dt) == (None, None)
    assert err == "no match", "a bare hour matched the leading digits of a typo"


@pytest.mark.parametrize("token", ["t", "td", "tr"])
def test_a_shorthand_with_no_time_is_not_a_command(monkeypatch, token):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    assert parse_command(f"Remind Dentist {token}")[2] == "no match"


def test_an_out_of_range_bare_hour_is_reported(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    dt, err = calendar_client.parse_date_time("tr", "25")

    assert dt is None
    assert "Hour 0-23" in err


# ─── `td` AND A MOMENT ALREADY GONE ────────────────────────────────────────────
# The one thing the new pair can do that the old `t` could not: name a time that
# has already passed. `t` was tomorrow, a future calendar day by construction, so
# the question never came up.
#
# A calendar event in the past pings NOTHING. Google's alerts are 1 day and 1
# hour before — both gone — and the morning poll ran at 07:30. Booking it is a
# silent no-op confirmed as "Reminder set!", which is the same shape as every
# other bug this module is built around: a confident reply for a thing that did
# not happen.

def test_td_is_refused_when_the_time_has_already_passed(monkeypatch):
    """10:00, and a reminder for 09:00 today. Nothing can ping for it."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    name, dt, err = parse_command("Remind Dentist td 09.00")

    assert dt is None, f"booked {dt}, which can never ping"
    assert name == "Dentist"
    assert "already past" in err


def test_the_past_today_refusal_says_what_time_it_is(monkeypatch):
    """"Already past" invites "past what?". The message answers it — the date it
    resolved to and the current time — so the refusal can be checked rather than
    taken on trust.

    THE DATE IS ASSERTED ATTACHED TO THE TIME, not on its own, and the revert
    pass is why. The message names 06.08.2026 twice: once in the clause saying
    what is past, and once in the `spell the date out` escape at the end. A bare
    `"06.08.2026" in err` was satisfied by the SECOND one, so rewording the first
    to "09.00 today is already past" — dropping exactly the date this test claims
    to guard — left it green.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10, minute=23)

    _, _, err = parse_command("Remind Dentist td 09.00")

    assert "09.00 on 06.08.2026" in err, (
        f"the refusal did not name the moment it refused: {err}")
    assert "10:23" in err, f"the refusal did not say what time it is: {err}"


def test_the_past_today_refusal_offers_both_ways_out(monkeypatch):
    """Two readings are plausible — the same time tomorrow, or a record of
    something that already happened — and the message offers both rather than
    picking. The second works because an explicit year is taken at face value."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    _, _, err = parse_command("Remind Dentist td 09.00")

    # Both asserted in the form the message OFFERS them — backticked — for the
    # reason the test above records: the date also appears in the clause naming
    # what was refused, so a bare substring check would pass on that instead.
    assert "`tr 09.00`" in err, f"the refusal did not offer tomorrow: {err}"
    assert "(`06.08.2026`)" in err, (
        f"the refusal did not offer the explicit date: {err}")

    # And the escape it offers has to actually work.
    dt, err = calendar_client.parse_date_time("06.08.2026", "09.00")
    assert err is None, f"the refusal offered a form the parser rejects: {err}"
    assert (dt.day, dt.hour) == (6, 9)


def test_td_is_accepted_for_a_time_still_ahead_today(monkeypatch):
    """The guard must not make today unbookable — that is the whole feature."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    _, dt, err = parse_command("Remind Dentist td 18")

    assert err is None
    assert (dt.day, dt.hour) == (6, 18)


def test_td_at_the_current_minute_is_not_past(monkeypatch):
    """THE BOUNDARY, asserted rather than left to luck. Strictly-before is the
    rule: this minute has not finished happening, and refusing it would be
    refusing a moment still ahead."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10, minute=0)

    _, dt, err = parse_command("Remind Dentist td 10.00")

    assert err is None, f"refused a time that has not passed: {err}"
    assert (dt.day, dt.hour, dt.minute) == (6, 10, 0)


def test_a_past_td_does_not_roll_to_next_year(monkeypatch):
    """PAST_GRACE must not reach this path. It exists because a bare `DD.MM` in
    the recent past is ambiguous between this year and next — and `td` answers
    that question outright, so there is nothing to roll. Rolling it would produce
    the original bug with a new token: a reminder twelve months out, confirmed."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)

    _, dt, err = parse_command("Remind Dentist td 09.00")

    assert dt is None
    assert "2027" not in err, f"the refusal suggested next year: {err}"


def test_a_past_td_never_reaches_create_event(monkeypatch, calendar):
    """End to end: the refusal stops before the calendar."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    created = []
    monkeypatch.setattr(reminder, "create_event",
                        lambda name, start, minutes: (created.append(start), "")[1])
    update = FakeUpdate(text="Remind Dentist td 09.00")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert created == [], "created an event that can never ping"
    assert update.message.replied_with("already past")


# ─── THE SHORTHANDS GO THROUGH THE SAME GUARDS ─────────────────────────────────

def test_tomorrow_is_dst_checked_like_any_other_date(monkeypatch):
    """29.03.2026 is the spring-forward day, so on the 28th `tr 02.30` names an
    hour that does not exist. The shorthand is a shortcut for typing a date, not
    for skipping the checks that make a date trustworthy."""
    freeze(monkeypatch, year=2026, month=3, day=28, hour=10)

    _, dt, err = parse_command("Remind Dentist tr 02.30")

    assert dt is None
    assert "doesn't exist" in err


def test_today_is_dst_checked_like_any_other_date(monkeypatch):
    """The same, one day later and through the other token. Asserted separately
    because `td` reaches _localize down its own branch — a branch that forgot the
    call would book an hour that does not exist and say nothing."""
    freeze(monkeypatch, year=2026, month=3, day=29, hour=1)

    _, dt, err = parse_command("Remind Dentist td 02.30")

    assert dt is None
    assert "doesn't exist" in err


def test_the_dst_refusal_wins_when_the_hour_is_also_past(monkeypatch):
    """ORDERING, and it is a decision worth pinning.

    At 10:00 on the spring-forward day, `td 02.30` is both nonexistent and past.
    The DST message is the useful one: it says the hour cannot be booked on any
    day. "Already past" would send you to try the same time tomorrow, where it
    exists — a correct reply that teaches the wrong thing.
    """
    freeze(monkeypatch, year=2026, month=3, day=29, hour=10)

    _, dt, err = parse_command("Remind Dentist td 02.30")

    assert dt is None
    assert "doesn't exist" in err, f"reported the past check instead: {err}"
    assert "already past" not in err


def test_tomorrow_is_never_in_the_past_so_it_is_never_queried(monkeypatch):
    """Late at night, `tr 00.30` is barely half an hour away — still tomorrow,
    and still future, so the past question never arises for this form."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=23, minute=50)

    _, dt, err = parse_command("Remind Dentist tr 00.30")

    assert err is None
    assert (dt.day, dt.hour, dt.minute) == (7, 0, 30)


def test_the_confirmation_spells_out_the_day_the_shorthand_resolved_to(monkeypatch, calendar):
    """`tr` is terse enough to mistype or misremember, so the safeguard is the
    confirmation the year work already added: it names the weekday and the full
    date, which is exactly what makes a wrong day obvious at a glance."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)
    update = FakeUpdate(text="Remind Dentist tr 10")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert update.message.replied_with("Friday 07 August 2026 at 10:00")


def test_the_confirmation_spells_out_today_too(monkeypatch, calendar):
    """And for the token one letter away, which is the pair the confirmation now
    has to tell apart. `td` and `tr` differ by a keystroke; "Thursday 06" and
    "Friday 07" do not."""
    freeze(monkeypatch, year=2026, month=8, day=6, hour=15)
    update = FakeUpdate(text="Remind Dentist td 18")

    run(reminder.run_remind(update.message.text, **with_update(update)))

    assert update.message.replied_with("Thursday 06 August 2026 at 18:00")


def test_a_dst_refusal_names_the_date_not_the_token(monkeypatch):
    """`tr` is a legal date token, so the message must not echo it back.

    "02.30 doesn't exist on tr" says nothing about WHICH night is the problem,
    which is the only thing the message is for. It names the date the token
    resolved to instead — which is also what makes the refusal checkable against
    a calendar.
    """
    freeze(monkeypatch, year=2026, month=3, day=28, hour=10)

    _, dt, err = parse_command("Remind Dentist tr 02.30")

    assert dt is None
    assert "29.03.2026" in err, f"the refusal did not name the date: {err}"
    assert "on tr " not in err, f"the refusal echoed the raw token: {err}"


# ─── THE SPLIT ─────────────────────────────────────────────────────────────────
# reminder.py used to take `update` and reply through telegram_text itself, so it
# could only ever be driven by a bot handler. Every other test in this file still
# passes an Update, because they assert on what the USER is told and
# with_update() binds the real bot-layer channels. This one asserts the property
# that made the split worth doing.

def test_the_service_runs_with_no_update_at_all(monkeypatch, calendar):
    """THE PROOF THE SPLIT WORKED.

    A list's `append` is a complete implementation of the notify interface —
    `notify_md` defaults to `notify`, so one function is the whole contract. If
    this ever needs a fake Update again, something has been welded back to
    Telegram and tests/test_layering.py should already have said so.
    """
    freeze(monkeypatch, year=2026, month=8, day=6, hour=10)
    said = []

    run(reminder.run_remind("Remind Dentist tr 10", notify=_async(said.append)))

    assert any("Reminder set!" in message for message in said)
    assert any("Friday 07 August 2026 at 10:00" in message for message in said)


def _async(fn):
    """`notify` is awaited, and a list's append is not a coroutine function.

    Kept here rather than in conftest: this is the only test that needs it, and
    the point being made is that the interface is one callable — wrapping it is
    a detail of THIS caller, exactly as a scheduled job's logger would be.
    """
    async def call(text):
        return fn(text)
    return call
