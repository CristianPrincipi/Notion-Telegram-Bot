"""
Shared Google Calendar client for David.

Uses a service account for authentication — no browser, no token refresh, no
expiry. This is the only auth model that runs cleanly on a headless host like
Railway. The service account's JSON key lives in the GOOGLE_CREDENTIALS_JSON
environment variable, and the target calendar is shared with the service
account's client_email (with "Make changes to events" permission).

All Google Calendar logic lives here so future calendar commands (view agenda,
delete event, etc.) can be added by importing from this module.
"""

import os
import json
import threading
from datetime import datetime, timedelta

import pytz

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TIMEZONE_NAME = "Europe/Rome"
TIMEZONE = pytz.timezone(TIMEZONE_NAME)

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Default event duration when the command gives only a start time
DEFAULT_EVENT_MINUTES = 60

# Native Google popup alerts attached to every event (minutes before start)
GOOGLE_REMINDER_MINUTES = [24 * 60, 60]  # 1 day before, 1 hour before


# ─── SERVICE (lazy, one per thread) ────────────────────────────────────────────
# ONE SERVICE PER THREAD, not one shared singleton.
#
# google-api-python-client is built on httplib2, which is NOT thread-safe:
# httplib2.Http keeps a plain dict of live connections keyed by host and reuses
# them, so two threads sharing one service can be handed the same socket and
# interleave their requests on it. Google's own guidance is that each thread
# needs its own Http. This is the same hazard notion_client.py solves for
# requests.Session, for the same reason — every calendar call now runs inside an
# asyncio.to_thread worker, and with concurrent updates the morning briefing job
# and a `Remind` command really can run at the same instant.
#
# A shared singleton was safe right up until updates stopped being sequential.
#
# Cheap to build per thread: google-api-python-client ships a static discovery
# document for calendar v3, so build() does not hit the network. The pool is
# bounded (min(32, cpu_count + 4) workers) and threads are reused.
_thread_local = threading.local()


def _get_service():
    """Build (once per thread) and return the authenticated Calendar API service.

    Returns (service, error). On any auth/config problem returns (None, error)
    so callers can surface a clean message instead of crashing.
    """
    service = getattr(_thread_local, "service", None)
    if service is not None:
        return service, None

    if not _CREDS_JSON:
        return None, "GOOGLE_CREDENTIALS_JSON is not set in environment."

    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account

        info = json.loads(_CREDS_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        _thread_local.service = service
        return service, None
    except json.JSONDecodeError as e:
        return None, f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}"
    except Exception as e:
        return None, f"Calendar auth failed: {e}"


# ─── DATE / TIME PARSING ───────────────────────────────────────────────────────

# How far into the past a date may fall before it is read as "you meant next year".
#
# THE BUG THIS EXISTS FOR: the rule used to be "any past datetime rolls to
# year + 1". So at 10:00, `Remind Dentist 06.08 - 09.00` — an appointment an hour
# ago, most likely a typo or a note-to-self about something happening today —
# silently created an event on the same day in AUGUST NEXT YEAR. The confirmation
# read normally, the event was a year away, and nothing ever pinged.
#
# A day's grace keeps the useful half of the behaviour: in December, `12.06`
# obviously means next June, and that is months past, not hours. Inside the
# window the two readings are genuinely ambiguous, so it asks instead of guessing.
PAST_GRACE = timedelta(hours=24)


def _localize(naive: datetime, time_str: str):
    """Attach Europe/Rome to a naive datetime. Returns (datetime, error).

    is_dst=None, so pytz RAISES on the two local times that are not a single
    real instant, instead of quietly picking one:

      - NONEXISTENT: the hour skipped at the start of summer time. Europe/Rome
        jumps 02:00 -> 03:00, so 02:30 that night never happens. pytz's default
        (is_dst=False) shifted it silently, booking an event an hour from where
        it was asked for.
      - AMBIGUOUS: the hour repeated at the end of summer time. 03:00 -> 02:00,
        so 02:30 happens twice and "02.30" names two instants an hour apart.

    Both land on 02:00-02:59 in this timezone, once a year each. Rare — and
    exactly the kind of rare that is impossible to diagnose from a calendar entry
    that is simply an hour out.

    ON MIGRATING TO stdlib zoneinfo: it would remove the localize() step
    altogether — tzinfo attaches at construction (`datetime(..., tzinfo=ZoneInfo(
    "Europe/Rome"))`) and arithmetic is DST-aware, so the whole class of
    "forgot to localize" bugs goes away. It would NOT give this behaviour for
    free, though: zoneinfo does not raise on either case, it resolves them
    through the `fold` attribute, picking one silently the way pytz's default
    did. Detecting them there means comparing the utcoffset at fold=0 against
    fold=1 explicitly. Worth knowing before anyone starts that migration
    expecting an exception. Not doing it here.
    """
    # The message names the DATE THIS RESOLVED TO, not the token that was typed.
    # `tr` is a legal date token, and "02.30 doesn't exist on tr" tells you
    # nothing about which night is the problem — the whole point of the message.
    on = naive.strftime("%d.%m.%Y")
    try:
        return TIMEZONE.localize(naive, is_dst=None), None
    except pytz.exceptions.NonExistentTimeError:
        return None, (f"{time_str} doesn't exist on {on} — the clocks go forward "
                      f"that night and the hour from 02:00 to 03:00 is skipped. "
                      f"Pick a time before 02:00 or from 03:00.")
    except pytz.exceptions.AmbiguousTimeError:
        return None, (f"{time_str} happens twice on {on} — the clocks go back "
                      f"that night, so 02:00 to 03:00 runs through a second time. "
                      f"Pick a time before 02:00 or from 03:00.")


# The two day shorthands, as a MATCHED PAIR. The long forms are accepted beside
# them because a shorthand nobody remembers is worse than none.
#
# `td` and `tr` are two letters rather than one, and that is the point. The old
# scheme had a single `t` meaning tomorrow, which left no room for today next to
# it and did not say which of the two it had picked — a one-letter token for one
# of two adjacent days is a one-day error waiting for a typo. Two letters, chosen
# so neither is a prefix of the other, cost one keystroke and remove the class.
TODAY_TOKENS    = {"td", "today"}
TOMORROW_TOKENS = {"tr", "tomorrow"}

# `t` used to mean tomorrow, and it is REFUSED rather than removed.
#
# Removing it from reminder.REMIND_PATTERN would work — the command would simply
# not match and the usage message would come back. That is worse for the one
# reason that matters here: the usage message does not say why the thing you have
# typed for months stopped working, and the reader is left to spot the difference
# between two lists. Refusing it by name says it.
#
# What it must never do is keep working. `t` sits one letter from BOTH new
# tokens, so any silent reading of it is a coin flip between today and tomorrow —
# and a reminder on the wrong day of two adjacent ones is exactly the failure this
# command's history is made of. Kept legal at the pattern level and refused here,
# which is the same split every other date rule in this module lives on.
RETIRED_TOKENS  = {"t"}


def _parse_clock(time_str: str):
    """'HH.MM' or a bare 'HH'. Returns (hour, minute, error).

    A bare hour means o'clock, which is how times are said out loud: `t 10` is
    tomorrow at ten, and typing `10.00` for that is three characters of ceremony.
    """
    parts = time_str.split(".")
    try:
        if len(parts) == 1:
            hour, minute = int(parts[0]), 0
        elif len(parts) == 2:
            hour, minute = (int(p) for p in parts)
        else:
            raise ValueError
    except (ValueError, TypeError):
        return None, None, (f"Invalid time format '{time_str}'. Use HH.MM 24h "
                            f"(e.g. 14.30) or a bare hour (e.g. 10).")
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None, None, f"Invalid time '{time_str}'. Hour 0-23, minute 0-59."
    return hour, minute, None


def parse_date_time(date_str: str, time_str: str):
    """Parse a date + time into an aware Europe/Rome datetime. Returns (dt, error).

    Accepted date forms:
      DD.MM          the current year, with the rollover rules below
      DD.MM.YYYY     that year, taken at face value
      td / today     today, provided the time has not already passed
      tr / tomorrow  the day after today

    `t` on its own is refused: it used to mean tomorrow and now names neither day.

    Accepted time forms: HH.MM, or a bare HH meaning o'clock.

    With no year, the current one is assumed and a date more than PAST_GRACE in
    the past rolls to next year. A date inside that window is REFUSED rather than
    guessed at — see PAST_GRACE. Spelling the year out (`06.08.2027`) is how you
    say which one you meant; an explicit year is taken at face value, past or not,
    because it is no longer a guess at that point.
    """
    token = date_str.strip().lower()

    # ── `t`, which no longer names a day ───────────────────────────────────────
    # Refused BEFORE the clock is parsed, so the answer does not depend on
    # whether the time was also wrong: `t` is the thing to fix either way, and a
    # message about the time would send the reader after the wrong problem.
    #
    # It names BOTH replacements. Naming only `tr` — "it used to mean tomorrow,
    # so say tomorrow" — would be a nudge toward one of two adjacent days at the
    # exact moment David has no idea which was meant.
    if token in RETIRED_TOKENS:
        return None, (
            f"`{date_str.strip()}` on its own is no longer a date — it used to "
            f"mean tomorrow, and there are now two days to tell apart. Send "
            f"`tr {time_str}` for tomorrow or `td {time_str}` for today."
        )

    hour, minute, err = _parse_clock(time_str)
    if err:
        return None, err

    now = datetime.now(TIMEZONE)

    # ── `tr` / `tomorrow` ──────────────────────────────────────────────────────
    # No rollover question to answer and no past-date question either: tomorrow
    # is a future calendar day by construction, so the earliest instant this can
    # produce is still ahead of `now`. It goes through _localize like everything
    # else, so asking for 02.30 on the night the clocks change is still refused.
    if token in TOMORROW_TOKENS:
        tomorrow = (now + timedelta(days=1)).date()
        return _localize(datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                                  hour, minute), time_str)

    # ── `td` / `today` ─────────────────────────────────────────────────────────
    # The one token that can name a moment already gone, which is why it is the
    # only shorthand with a check after it.
    #
    # _localize runs FIRST, deliberately. On the night the clocks go forward,
    # `td 02.30` is both nonexistent and (later that day) past, and the DST
    # message is the useful one: it names an hour that cannot be booked on any
    # day, while "already past" would send you to try the same time tomorrow.
    if token in TODAY_TOKENS:
        today = now.date()
        dt, err = _localize(datetime(today.year, today.month, today.day,
                                     hour, minute), time_str)
        if err:
            return None, err

        # STRICTLY before. A reminder for this exact minute is odd but not wrong,
        # and refusing it would be refusing a moment that has not happened yet.
        if dt < now:
            # PAST_GRACE does not apply and must not: it exists because a bare
            # `DD.MM` in the recent past is ambiguous between this year and next,
            # and `td` answers that question by construction. There is nothing to
            # roll forward — the day was named outright.
            #
            # A past event pings nothing. Google's alerts are 1 day and 1 hour
            # before, both gone, and the morning poll ran at 07:30 — so booking it
            # is a silent no-op confirmed as "Reminder set!". Both ways out are
            # offered, because one of them is usually what was meant.
            return None, (
                f"{time_str} on {dt.strftime('%d.%m.%Y')} is already past — it "
                f"is {now.strftime('%H:%M')} now, and a reminder in the past "
                f"never pings. Send `tr {time_str}` for tomorrow, or spell the "
                f"date out (`{dt.strftime('%d.%m.%Y')}`) to record it anyway."
            )
        return dt, None

    # ── DD.MM, optionally with a year ──────────────────────────────────────────
    parts = date_str.split(".")
    try:
        if len(parts) == 3:
            day, month, year_given = (int(p) for p in parts)
            if year_given < 100:                      # '27' -> 2027
                year_given += 2000
        elif len(parts) == 2:
            day, month = (int(p) for p in parts)
            year_given = None
        else:
            raise ValueError
    except (ValueError, TypeError):
        return None, (f"Invalid date format '{date_str}'. Use DD.MM (e.g. 12.06), "
                      f"DD.MM.YYYY (e.g. 12.06.2027), `td` for today or `tr` "
                      f"for tomorrow.")
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None, f"Invalid date '{date_str}'. Day 1-31, month 1-12."

    # Build the datetime; catch impossible dates like 31.02
    try:
        naive = datetime(year_given or now.year, month, day, hour, minute)
    except ValueError:
        return None, f"'{date_str}' is not a real date."

    dt, err = _localize(naive, time_str)
    if err:
        return None, err

    # An explicit year is the answer to the question below, so it is not asked.
    if year_given is not None or dt >= now:
        return dt, None

    # ── In the past, with no year given. Do not guess. ─────────────────────────
    if dt >= now - PAST_GRACE:
        return None, (
            f"{date_str} at {time_str} was earlier today — that is already past. "
            f"If you meant next year, send it with the year: "
            f"`{day:02d}.{month:02d}.{now.year + 1}`."
        )

    # Comfortably past, so "next year" is the only reading that makes sense.
    try:
        naive_next = datetime(now.year + 1, month, day, hour, minute)
    except ValueError:
        # 29.02 in a non-leap next year.
        return None, (f"'{date_str}' does not exist in {now.year + 1}. "
                      f"Send it with the year you meant.")

    return _localize(naive_next, time_str)


# ─── EVENT CREATION ────────────────────────────────────────────────────────────

def create_event(summary: str, start_dt: datetime, duration_minutes: int = DEFAULT_EVENT_MINUTES):
    """Create a calendar event with native Google popup reminders.

    Returns (event_link, error). event_link is the htmlLink to the event.
    """
    service, err = _get_service()
    if err:
        return None, err

    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body = {
        "summary": summary,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE_NAME},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE_NAME},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": m} for m in GOOGLE_REMINDER_MINUTES],
        },
    }

    try:
        event = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
        return event.get("htmlLink", ""), None
    except Exception as e:
        return None, f"Could not create event: {e}"


# ─── EVENT QUERIES (for the daily reminder poll + conflict detection) ───────────

def _to_local(iso_str: str) -> datetime:
    """Parse a Google ISO datetime string into a Europe/Rome-aware datetime.

    Plain localize() here, NOT the is_dst=None of _localize, and deliberately:
    this is a READ path over data Google owns. Raising on an ambiguous timestamp
    somebody else stored would break the morning briefing over an event David did
    not create and cannot fix. Refusing to guess is right when the user is at the
    keyboard to answer; it is not right in a scheduled job at 07:30.
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return TIMEZONE.localize(dt)
    return dt.astimezone(TIMEZONE)


def _list_events_between(start_dt: datetime, end_dt: datetime):
    """List events that OVERLAP [start_dt, end_dt). Returns (events, error).

    Note the API semantics: timeMin filters by an event's end time and timeMax by
    its start time, so this returns every event overlapping the window (not just
    those that start inside it) — which is exactly what conflict detection needs.

    Each returned item: {"summary", "start_dt" (tz-aware), "end_dt" (tz-aware or
    None for all-day), "all_day" (bool)}.
    """
    service, err = _get_service()
    if err:
        return [], err

    try:
        resp = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start_dt.isoformat(),
            timeMax=end_dt.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except Exception as e:
        return [], f"Could not fetch events: {e}"

    out = []
    for ev in resp.get("items", []):
        start = ev.get("start", {})
        end = ev.get("end", {})
        summary = ev.get("summary", "(no title)")

        if "dateTime" in start:
            # Timed event
            start_local = _to_local(start["dateTime"])
            end_local = _to_local(end["dateTime"]) if end.get("dateTime") else None
            out.append({"summary": summary, "start_dt": start_local,
                        "end_dt": end_local, "all_day": False})
        elif "date" in start:
            # All-day event
            d = datetime.fromisoformat(start["date"])
            start_local = TIMEZONE.localize(datetime(d.year, d.month, d.day, 0, 0))
            out.append({"summary": summary, "start_dt": start_local,
                        "end_dt": None, "all_day": True})

    return out, None


def find_conflicts(start_dt: datetime, end_dt: datetime):
    """Return (conflicts, error): TIMED events overlapping [start_dt, end_dt).

    All-day events are ignored (they'd "overlap" everything that day). Relies on
    the overlap semantics of _list_events_between, so two back-to-back events —
    one ending exactly when the next begins — are NOT flagged.

    Each conflict: {"summary", "start_dt", "end_dt"}.
    """
    events, err = _list_events_between(start_dt, end_dt)
    if err:
        return [], err
    return [e for e in events if not e["all_day"]], None


def get_events_for_day(target_date: datetime):
    """Return (events, error) for all events on the calendar date of target_date."""
    day_start = TIMEZONE.localize(datetime(target_date.year, target_date.month, target_date.day, 0, 0))
    day_end = day_start + timedelta(days=1)
    return _list_events_between(day_start, day_end)


def now_local() -> datetime:
    """Current time in Europe/Rome — convenience for handlers/jobs."""
    return datetime.now(TIMEZONE)
