"""`Agenda` — reading the calendar on demand, and the three answers it can give.

WHAT THIS LOCKS DOWN
--------------------
One thing above all others: **a failed read, an empty day and a full day are
three different messages.** That is not a style preference, it is the bug
`proactive/briefing.py` was rewritten for. The morning briefing used to set
`events = []` on an error, and `[]` renders as "nothing scheduled" — so during a
calendar outage David stated the day was clear. A missing message you eventually
notice; a confident wrong answer you act on by not showing up.

`Agenda` is that same read, on demand, and it would have inherited the same
collapse for free. `test_the_three_outcomes_are_three_different_messages` is the
one test in this file that would matter if the rest were deleted.

THE SECOND SUBJECT is which DAY a token names, and how that differs from
`Remind`. `parse_day` deliberately does NOT roll a past `DD.MM` forward the way
`parse_date_time` does: that rule exists because a reminder in the past never
pings, and reading a past day is an ordinary thing to want. What makes taking it
at face value safe is that the reply NAMES the resolved day in full — so the
tests assert both halves, because the permissiveness is only defensible with the
naming in place.
"""

from datetime import datetime, timedelta

import pytest

from clients import calendar_client
from services import agenda
from proactive import briefing
from conftest import FakeUpdate, run, with_update


def freeze(monkeypatch, *, year, month, day, hour, minute=0):
    """Pin calendar_client's clock to a Europe/Rome wall-clock instant.

    Same helper as tests/test_reminder_dates.py, and deliberately a copy rather
    than an import: it patches `calendar_client.datetime`, so the two files
    installing it independently is what lets either run alone.
    """
    pinned = calendar_client.TIMEZONE.localize(
        datetime(year, month, day, hour, minute))

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned.astimezone(tz) if tz else pinned.replace(tzinfo=None)

    monkeypatch.setattr(calendar_client, "datetime", FrozenClock)
    monkeypatch.setattr(agenda, "now_local", lambda: pinned)
    return pinned


def at(hour, minute=0, *, day=15, month=8, year=2026):
    return calendar_client.TIMEZONE.localize(datetime(year, month, day, hour, minute))


def event(summary, start, end=None, all_day=False):
    return {"id": f"id-{summary}", "summary": summary, "start_dt": start,
            "end_dt": end, "all_day": all_day, "raw": {}}


def calendar(monkeypatch, result):
    """Install a fake get_events_for_day and record which DAY it was asked for."""
    asked = []

    def get_events_for_day(day):
        asked.append(day)
        return result

    monkeypatch.setattr(agenda, "get_events_for_day", get_events_for_day)
    return asked


def send(token=None):
    update = FakeUpdate(text="Agenda")
    run(agenda.run_agenda(token, **with_update(update)))
    return update


# ─── THE THREE OUTCOMES ────────────────────────────────────────────────────────

def test_the_three_outcomes_are_three_different_messages(monkeypatch):
    """THE test in this file.

    A calendar outage must not be answered with "nothing scheduled". The two
    states are kept apart all the way to the text — `events` is `[]` on failure,
    so a single fall-through here restores the exact statement the briefings were
    fixed for.
    """
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)

    calendar(monkeypatch, ([event("Dentist", at(14, 30), at(15, 30))], None))
    full = send()

    calendar(monkeypatch, ([], None))
    empty = send()

    calendar(monkeypatch, ([], "Could not fetch events: 403 forbidden"))
    failed = send()

    assert full.message.replied_with("Dentist")
    assert empty.message.replied_with("Nothing scheduled")

    assert failed.message.replied_with("could not read your calendar")
    assert failed.message.replied_with("403 forbidden"), (
        "the failure named no cause — a report you cannot act on")
    assert not failed.message.replied_with("Nothing scheduled"), (
        "a calendar outage was reported as an empty day, which is an "
        "affirmative false statement about your day")


def test_an_empty_day_still_names_the_day(monkeypatch):
    """"Nothing scheduled" alone cannot be checked against what you asked for."""
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    calendar(monkeypatch, ([], None))

    update = send("tr")

    assert update.message.replied_with("Sunday 16 August 2026")


def test_the_failure_names_the_day_it_could_not_read(monkeypatch):
    """Otherwise a failed `Agenda tr` and a failed `Agenda` read identically, and
    you cannot tell which question went unanswered."""
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    calendar(monkeypatch, ([], "boom"))

    update = send("tr")

    assert update.message.replied_with("Sunday 16 August 2026")


def test_every_event_on_a_busy_day_is_listed(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    calendar(monkeypatch, ([
        event("Standup", at(9, 30), at(9, 45)),
        event("Dentist", at(14, 30), at(15, 30)),
        event("Gym", at(19, 0)),
        event("Bank holiday", at(0, 0), all_day=True),
    ], None))

    update = send()
    sent = "\n".join(update.message.reply_texts)

    for summary in ("Standup", "Dentist", "Gym", "Bank holiday"):
        assert summary in sent, f"{summary!r} was dropped from a four-event day"
    assert "09:30" in sent and "14:30" in sent and "19:00" in sent
    assert "all day" in sent, "an all-day event was rendered with a meaningless 00:00"


# ─── WHICH DAY ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("token, offset", [
    (None,       0),
    ("td",       0),
    ("today",    0),
    ("tr",       1),
    ("tomorrow", 1),
])
def test_the_day_tokens_resolve_to_the_day_they_name(monkeypatch, token, offset):
    """`Agenda tr` reads TOMORROW, not today.

    Asserted on the date handed to the calendar rather than on the message: the
    message could name the right day while the read asked for another, and that
    is the failure — you would be shown yesterday's events under today's
    heading.
    """
    pinned = freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    asked = calendar(monkeypatch, ([], None))

    send(token)

    assert asked == [(pinned + timedelta(days=offset)).date()]


def test_a_bare_date_is_read_in_the_current_year_and_not_rolled_forward(monkeypatch):
    """THE DELIBERATE DIFFERENCE FROM `Remind`, asserted in both directions.

    `parse_date_time` rolls a comfortably-past `DD.MM` to next year, because a
    reminder in the past never pings. `Agenda` must not: "what did I have on
    12.06?" is an ordinary question, and answering it with next June's empty day
    is a wrong answer that looks exactly like a right one.
    """
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    asked = calendar(monkeypatch, ([], None))

    send("12.06")

    assert asked == [datetime(2026, 6, 12).date()], (
        "a past date was rolled to next year — that is Remind's rule, not this one")

    # The same token through the reminder parser, so the difference is visible
    # here rather than only in two docstrings that could drift apart.
    rolled, err = calendar_client.parse_date_time("12.06", "10")
    assert err is None and rolled.year == 2027, (
        "parse_date_time stopped rolling — this test's premise is stale")


def test_the_reply_names_the_full_date_including_the_year(monkeypatch):
    """WHAT MAKES THE FACE-VALUE READING SAFE.

    Taking a bare `DD.MM` at face value is only defensible because the answer
    says which day it took. Remove this line and a wrong reading becomes
    invisible — which is exactly how a reminder silently booked a year out was
    confirmed and never questioned.
    """
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    calendar(monkeypatch, ([event("Dentist", at(14, 30, day=12, month=6))], None))

    update = send("12.06")

    assert update.message.replied_with("Friday 12 June 2026"), (
        f"the reply did not name the day it read: {update.message.reply_texts}")


def test_an_explicit_year_is_taken_at_face_value(monkeypatch):
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    asked = calendar(monkeypatch, ([], None))

    send("12.06.2027")

    assert asked == [datetime(2027, 6, 12).date()]


# ─── REFUSALS ──────────────────────────────────────────────────────────────────

def test_a_bare_t_is_refused_by_name(monkeypatch):
    """`t` used to mean tomorrow and now names neither day.

    Refused here for the same reason `Remind` refuses it: it sits one letter from
    both replacements, so any silent reading is a coin flip between two adjacent
    days. A read is a cheaper place to be wrong than a write, which is an
    argument for keeping the message, not for dropping it.
    """
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    asked = calendar(monkeypatch, ([], None))

    update = send("t")

    assert asked == [], "a refused token still read the calendar"
    sent = "\n".join(update.message.reply_texts)
    assert "td" in sent and "tr" in sent, (
        "the refusal must name BOTH replacements — naming one nudges toward a "
        "day David has no idea was meant")


@pytest.mark.parametrize("token", ["someday", "32.13", "31.02", "12/06", "yesterday"])
def test_an_unreadable_day_is_refused_and_says_what_works(monkeypatch, token):
    """REFUSED, not resolved. A guess here answers a question you did not ask,
    and the answer is indistinguishable from a correct one."""
    freeze(monkeypatch, year=2026, month=8, day=15, hour=9)
    asked = calendar(monkeypatch, ([], None))

    update = send(token)

    assert asked == [], f"{token!r} still reached the calendar"
    assert update.message.replied_with("❌")
    assert update.message.replied_with("Agenda tr"), (
        "the refusal did not say which forms work")


# ─── ONE RENDERER ──────────────────────────────────────────────────────────────

def test_the_briefings_and_the_agenda_share_one_renderer():
    """Two copies of this drift a wording at a time, and nothing fails when they
    do — the same argument that put TAKEAWAYS_HEADING in config.py and left one
    message splitter in bot/long_messages.py.

    Asserted as identity, not as equal output: two functions that agree today are
    exactly what a shared constant is meant to replace.
    """
    assert briefing.format_events_inline is agenda.format_events_inline


def test_the_inline_renderer_still_says_nothing_scheduled_for_an_empty_list():
    """The behaviour the briefings depend on, pinned where it now lives.

    It is also the reason every caller must check the error FIRST: this is what
    `[]` renders as, and `[]` is what a failed read hands back.
    """
    assert agenda.format_events_inline([]) == "nothing scheduled"
