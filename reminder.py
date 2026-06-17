"""
Reminder system for David — replaces the old Task functionality.

Command:  Remind [Appointment Name] [Date] - [Time]
Example:  Remind Dentist 12.06 - 14.30

Google Calendar is the single source of truth. Creating a reminder makes a
calendar event (with native Google popup alerts). A daily polling job reads the
calendar each morning and sends Telegram pings for today's and tomorrow's
events — so reminders survive Railway restarts (nothing is held in memory).
"""

import re

from calendar_client import (
    parse_date_time, create_event, get_events_for_day, now_local,
    find_conflicts, DEFAULT_EVENT_MINUTES, TIMEZONE,
)
from datetime import timedelta

# Remind [Name] [DD.MM] - [HH.MM]
# Name is non-greedy so it stops at the date; date and time are dot-separated.
REMIND_PATTERN = r"(?i)remind\s+(.+?)\s+(\d{1,2}\.\d{1,2})\s*-\s*(\d{1,2}\.\d{1,2})"


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
        await update.message.reply_text(
            "📅 *Reminder usage:*\n"
            "`Remind [Name] [Date] - [Time]`\n\n"
            "Example: `Remind Dentist 12.06 - 14.30`\n"
            "_Date is DD.MM, time is HH.MM in 24-hour format._",
            parse_mode="Markdown",
        )
        return

    name      = match.group(1).strip()
    date_str  = match.group(2).strip()
    time_str  = match.group(3).strip()

    # Validate + parse into a Europe/Rome datetime
    start_dt, err = parse_date_time(date_str, time_str)
    if err:
        await update.message.reply_text(f"❌ {err}")
        return

    await update.message.reply_text(f"⏳ Adding *{name}* to your calendar…", parse_mode="Markdown")

    # Detect overlaps against the proposed slot BEFORE creating the event, so the
    # new event itself can't show up in the results. A failed check degrades
    # gracefully (no warning) rather than blocking the reminder.
    end_dt = start_dt + timedelta(minutes=DEFAULT_EVENT_MINUTES)
    conflicts, _ = find_conflicts(start_dt, end_dt)

    link, err = create_event(name, start_dt, DEFAULT_EVENT_MINUTES)
    if err:
        await update.message.reply_text(f"❌ Could not create the event: {err}")
        return

    # Confirmation — include a heads-up if the appointment is before the morning poll
    when = start_dt.strftime("%d.%m.%Y at %H:%M")
    msg = (
        f"✅ Reminder set!\n\n"
        f"📅 *{name}*\n"
        f"🕐 {when}\n\n"
        f"You'll get a Telegram ping the day before and on the day, "
        f"plus Google's own alerts (1 day + 1 hour before)."
    )
    if start_dt.hour < 8:
        msg += "\n\n⚠️ This is before 08:00 — the morning ping arrives at 07:30, so for very early events rely on Google's 1-hour alert."

    await update.message.reply_text(msg, parse_mode="Markdown")

    # Conflict heads-up — separate, plain text, only when there's a real overlap.
    if conflicts:
        await update.message.reply_text(_format_conflict_warning(name, start_dt, end_dt, conflicts))


def _format_event_line(ev: dict) -> str:
    """One line per event for the reminder messages."""
    if ev["all_day"]:
        return f"•  {ev['summary']} (all day)"
    return f"•  {ev['summary']} at {ev['start_dt'].strftime('%H:%M')}"


def build_today_message() -> str | None:
    """Build the 'today' reminder message, or None if there are no events / on error."""
    today = now_local()
    events, err = get_events_for_day(today)
    if err or not events:
        return None
    lines = "\n".join(_format_event_line(e) for e in events)
    return f"🔔 *Today's reminders*\n━━━━━━━━━━━━━━━\n{lines}"


def build_tomorrow_message() -> str | None:
    """Build the 'tomorrow' reminder message, or None if there are no events / on error."""
    tomorrow = now_local() + timedelta(days=1)
    events, err = get_events_for_day(tomorrow)
    if err or not events:
        return None
    lines = "\n".join(_format_event_line(e) for e in events)
    return f"📅 *Tomorrow's reminders*\n━━━━━━━━━━━━━━━\n{lines}"
