"""
Reminder system for David — replaces the old Task functionality.

Command:  Remind [Appointment Name] [Date] [Time]
Examples: Remind Dentist 12.06 - 14.30
          Remind Dentist td 10         (today at 10:00)
          Remind Dentist tr 10         (tomorrow at 10:00)

Google Calendar is the single source of truth. Creating a reminder makes a
calendar event (with native Google popup alerts). A daily polling job reads the
calendar each morning and sends Telegram pings for today's and tomorrow's
events — so reminders survive Railway restarts (nothing is held in memory).
"""

import asyncio
import re

from clients.calendar_client import (
    parse_date_time, create_event, now_local,
    find_conflicts, CALENDAR_ID, DEFAULT_EVENT_MINUTES,
)
from page_lock import WRITE_LOCK_TIMEOUT_SECONDS, PageBusy, page_lock
from telegram_text import escape_md, reply
from datetime import timedelta

# Remind [Name] [Date] [- ][Time]
#
#   Remind Dentist 12.06 - 14.30      the long form
#   Remind Dentist 12.06.2027 - 14.30 with the year spelled out
#   Remind Dentist td 10              today at 10:00
#   Remind Dentist tr 10              tomorrow at 10:00
#
# Name is non-greedy so it stops at the date.
#
# The year is OPTIONAL and is what makes the past-date refusal actionable: with
# no year, a date inside the last day is ambiguous and calendar_client refuses to
# guess (see PAST_GRACE), so spelling the year out is how you answer it.
#
# THE ALTERNATION IS ORDERED LONGEST-FIRST FOR LEGIBILITY, AND THAT IS ALL IT IS.
# It was written as a guard and is not one: reversing it to `t|td|tr|today|
# tomorrow` leaves every token resolving to the same day, and
# `test_every_date_token_resolves_to_the_day_it_names` stays green. Recorded
# because a line that reads like protection and is not is worse than no line.
#
# The lookahead below is what actually separates them. Ordered first, `t` matches
# the head of "today", fails `(?![\w.])` on the `o`, and the engine backtracks to
# the branch that fits — so every order works, and the order chosen is the one
# that reads the way it behaves. The test still earns its place: it pins the DAY
# each token resolves to, which is the property that must hold however the
# pattern is later rearranged.
#
# `t` IS STILL LEGAL HERE, and it resolves to nothing: calendar_client refuses it
# by name (RETIRED_TOKENS). This module decides which tokens are legal, that one
# decides what they mean, and "used to mean tomorrow, now means neither day" is a
# meaning. Dropping it here instead would give the generic usage message, which
# does not say what changed.
#
# THE TWO LOOKAHEADS ARE LOAD-BEARING, not decoration. Both were checked by
# removing them and watching a test go red, because a lookahead that guards
# nothing is worse than none — it reads like protection.
#
#   after the date — the token has to END where the word does. A shorthand
#     otherwise matches the leading letters of ANY word that starts with them,
#     and the rest of that word is skipped as separator noise:
#     `Remind Bus td4 to town 10` silently becomes "Bus", today, 04:00. It costs
#     the run-together form — `td10` is refused and `td 10` is required — and
#     that is the right trade: a space is one keystroke, a wrong booking is
#     invisible.
#
#   after the time — a bare hour must not match the leading digits of a typo,
#     so `1030` fails the command rather than quietly booking 10:00.
#
# The separator is optional because `tr 10` reads naturally without one; the
# dash still works everywhere it did before.
REMIND_PATTERN = (r"(?i)remind\s+(?P<name>.+?)\s+"
                  r"(?P<date>tomorrow|today|tr|td|t"
                  r"|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)(?![\w.])"
                  r"\s*(?:-\s*)?"
                  r"(?P<time>\d{1,2}(?:\.\d{1,2})?)(?!\d)")


def _format_conflict_warning(new_name: str, start_dt, end_dt, conflicts: list) -> str:
    """Plain-text heads-up about overlapping events.

    Sent as a SEPARATE message from the (Markdown) confirmation so arbitrary
    event titles can't break Markdown rendering, and only when a conflict exists.
    """
    def _range(s, e):
        if e is None:
            return s.strftime("%H:%M")
        return f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"

    new_slot = _range(start_dt, end_dt)

    if len(conflicts) == 1:
        c = conflicts[0]
        return (f'⚠️ Heads up: "{new_name} {new_slot}" overlaps '
                f'"{c["summary"]} {_range(c["start_dt"], c["end_dt"])}".')

    lines = [f'⚠️ Heads up: "{new_name} {new_slot}" overlaps {len(conflicts)} events:']
    for c in conflicts:
        lines.append(f'  • {c["summary"]} {_range(c["start_dt"], c["end_dt"])}')
    return "\n".join(lines)


async def handle_remind(update, user_text: str):
    """Parse a Remind command, create the calendar event, confirm, and flag conflicts."""
    match = re.match(REMIND_PATTERN, user_text.strip())
    if not match:
        await reply(
            update,
            "📅 *Reminder usage:*\n"
            "`Remind [Name] [Date] [Time]`\n\n"
            "Examples:\n"
            "`Remind Dentist 12.06 - 14.30`\n"
            "`Remind Dentist td 10`  _(today at 10:00)_\n"
            "`Remind Dentist tr 10`  _(tomorrow at 10:00)_\n\n"
            "_Date is DD.MM, or `td` for today and `tr` for tomorrow. Time is "
            "HH.MM in 24-hour format, or a bare hour._\n"
            "_Add the year — `12.06.2027` — to book a specific one._",
        )
        return

    name      = match["name"].strip()
    date_str  = match["date"].strip()
    time_str  = match["time"].strip()

    # Validate + parse into a Europe/Rome datetime
    start_dt, err = parse_date_time(date_str, time_str)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    await reply(update, f"⏳ Adding *{escape_md(name)}* to your calendar…")

    # Detect overlaps against the proposed slot BEFORE creating the event, so the
    # new event itself can't show up in the results. A failed check degrades
    # gracefully (no warning) rather than blocking the reminder.
    # Both calls below are blocking Google Calendar round trips, so they run on
    # worker threads: on the event loop they would freeze every other command and
    # every scheduled job until Google answered.
    #
    # They also run under one lock on the calendar, because together they are a
    # check-then-act: "does anything overlap this slot?" then "create it". Two
    # overlapping reminders would each check against a calendar that did not yet
    # contain the other, so both would report a clear slot and neither would warn
    # about the collision they just created. The conflict check silently stops
    # working for exactly the pair of events it exists to catch.
    end_dt = start_dt + timedelta(minutes=DEFAULT_EVENT_MINUTES)
    try:
        async with page_lock(CALENDAR_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            conflicts, _ = await asyncio.to_thread(find_conflicts, start_dt, end_dt)
            link, err = await asyncio.to_thread(
                create_event, name, start_dt, DEFAULT_EVENT_MINUTES)
    except PageBusy:
        await update.message.reply_text(
            "⏳ Another reminder is still being created. Try again in a moment.")
        return

    if err:
        await update.message.reply_text(f"❌ Could not create the event: {err}")
        return

    # Confirmation — include a heads-up if the appointment is before the morning poll
    #
    # THE YEAR IS SPELLED OUT, in a weekday-first long form. It used to render as
    # "06.08.2027 at 09:00", where the one wrong digit sat mid-string between two
    # correct ones and read as normal — which is how a reminder silently a year
    # out was confirmed and never questioned. calendar_client no longer guesses
    # the year, and this makes whatever it did decide impossible to skim past.
    when = start_dt.strftime("%A %d %B %Y at %H:%M")
    msg = (
        f"✅ Reminder set!\n\n"
        f"📅 *{escape_md(name)}*\n"
        f"🕐 *{when}*\n"
    )
    if start_dt.year != now_local().year:
        msg += f"\n📆 Note: that is *{start_dt.year}*, not this year.\n"
    msg += (
        "\nYou'll get a Telegram ping the day before and on the day, "
        "plus Google's own alerts (1 day + 1 hour before)."
    )
    if start_dt.hour < 8:
        msg += "\n\n⚠️ This is before 08:00 — the morning ping arrives at 07:30, so for very early events rely on Google's 1-hour alert."

    await reply(update, msg)

    # Conflict heads-up — separate, plain text, only when there's a real overlap.
    if conflicts:
        await update.message.reply_text(_format_conflict_warning(name, start_dt, end_dt, conflicts))


# build_today_message, build_tomorrow_message and _format_event_line used to live
# here. They were deleted, not fixed.
#
# They had had zero callers since proactive/briefing.py took over the scheduled
# pings — nothing imported them, no job ran them, no test covered them. They also
# carried the `if err or not events: return None` collapse this branch set out to
# fix, which made them look like the bug's home. They were not: the live copy of
# that collapse was in briefing.py, and fixing these two would have changed
# nothing about the reminders going quiet.
#
# Deleting them is what makes that unambiguous for the next reader.
