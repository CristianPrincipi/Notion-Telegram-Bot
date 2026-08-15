"""`Agenda [day]` — what is on the calendar, on demand.

Until this existed, nothing in the command registry read the calendar at all:
`clients/calendar_client.py` had `get_events_for_day` and the only callers were
the 07:30 and 20:00 jobs. If you wanted to know what was on at 3pm you waited
until the next morning.

THE THREE OUTCOMES ARE THREE MESSAGES, and that is the whole of the care needed
here. A read failure, an empty day and a populated day must not collapse into
each other — `proactive/briefing.py` carries the scar tissue from the version of
this that did: on an error it set `events = []`, and an empty list renders as
"nothing scheduled", so during an outage David stated the day was clear. A
missing message you eventually notice; a confident wrong answer you act on by
not showing up.

WHY THE RENDERER LIVES HERE. `_format_events_inline` was private to
`proactive/briefing.py`, and this command has to produce the same line or the two
drift a wording at a time. It cannot stay there — `services/` importing
`proactive/` is sideways-and-upward — so it moved down and the briefing imports
it. One renderer, two callers, the same rule that put `split_for_telegram` in one
place and `TAKEAWAYS_HEADING` in `config.py`.
"""

import asyncio

from clients.calendar_client import get_events_for_day, now_local, parse_day
from telegram_text import escape_md

# What to send when a day token means nothing. Appended to `parse_day`'s own
# message rather than replacing it: that one says why the token failed, this one
# says what would have worked, and a refusal that does only the first sends you
# guessing.
#
# PLAIN, no Markdown, because it is appended to `parse_day`'s message and that
# one already carries backticks around the tokens it names. Escaping a client's
# string would show the backslashes; not escaping it would let a stray character
# in it reject the whole reply. `run_remind` sends parse_date_time's refusals the
# same way, for the same reason.
USAGE = "Send `Agenda` for today, `Agenda tr` for tomorrow, or a date: `Agenda 12.06`."


def format_events_inline(events: list) -> str:
    """Compact, comma-separated: 'Dentist 14:30, Gym 19:00'.

    MOVED FROM proactive/briefing.py, unchanged. Both briefings and this command
    render through it, so the three messages cannot drift apart in style.

    Never called with the result of a FAILED read. `[]` renders as "nothing
    scheduled", which is a statement about the day rather than about the
    request, and handing it an empty list that only means "the call errored" is
    the exact bug the briefings were fixed for. Every caller checks the error
    first.
    """
    if not events:
        return "nothing scheduled"
    parts = []
    for e in events:
        if e.get("all_day"):
            parts.append(f"{e['summary']} (all day)")
        else:
            parts.append(f"{e['summary']} {e['start_dt'].strftime('%H:%M')}")
    return ", ".join(parts)


def _format_events_listed(events: list) -> str:
    """One event per line, for a reply that is read rather than skimmed.

    The briefings are one-liners because they arrive unasked; an agenda you
    typed for is worth a line each. Plain text: an event title is arbitrary data
    from Google and a stray `*` in one must not cost the whole message.
    """
    lines = []
    for e in events:
        if e.get("all_day"):
            lines.append(f"  • {e['summary']} (all day)")
        elif e.get("end_dt") is not None:
            lines.append(f"  • {e['start_dt'].strftime('%H:%M')}–"
                         f"{e['end_dt'].strftime('%H:%M')}  {e['summary']}")
        else:
            lines.append(f"  • {e['start_dt'].strftime('%H:%M')}  {e['summary']}")
    return "\n".join(lines)


async def run_agenda(day_token: str | None = None, *, notify, notify_md=None) -> None:
    """Read one day's events back out of the calendar.

    Takes the day TOKEN, not the whole message: the registry pattern captures it
    in one group, so there is nothing left for this module to parse out. What it
    still owns is what a token means, which it asks `parse_day` — the same split
    `Remind` keeps between REMIND_PATTERN and parse_date_time, and for the same
    reason. A date rule resolved in a regex is a date rule nothing can unit-test.

    Runs INLINE (see bot/agenda.py): one read, already off the event loop here,
    and read-only work cannot reorder against a write the way a detached command
    could.
    """
    notify_md = notify_md or notify

    token = (day_token or "").strip()
    if token:
        day, err = parse_day(token)
        if err:
            # REFUSED, not resolved. Guessing which day an unrecognised token
            # meant would answer a question you did not ask, and the answer
            # would look exactly like a correct one.
            await notify(f"❌ {err}\n{USAGE}")
            return
    else:
        day = now_local().date()

    events, err = await asyncio.to_thread(get_events_for_day, day)

    # THE ERROR BRANCH RETURNS BEFORE ANY RENDERING. `events` is [] on failure,
    # and [] means "nothing scheduled" — so falling through here would answer a
    # calendar outage with an affirmative statement that the day is clear.
    if err:
        await notify(f"❌ I could not read your calendar, so I don't know what is "
                     f"on {day.strftime('%A %d %B %Y')}:\n{err}")
        return

    # THE DAY IS NAMED IN FULL, weekday and year included, and that is what makes
    # `parse_day` safe to keep permissive. It takes a bare `DD.MM` at face value
    # in the current year rather than rolling a past one forward the way `Remind`
    # does, and the only thing standing between that and a silently wrong answer
    # is this line saying which day it actually read.
    heading = f"📆 *{escape_md(day.strftime('%A %d %B %Y'))}*"

    if not events:
        await notify_md(f"{heading}\n\nNothing scheduled.")
        return

    await notify_md(heading)
    await notify(_format_events_listed(events))
