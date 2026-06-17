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


# ─── SERVICE (lazy singleton) ──────────────────────────────────────────────────
_service = None


def _get_service():
    """Build (once) and return the authenticated Calendar API service.

    Returns (service, error). On any auth/config problem returns (None, error)
    so callers can surface a clean message instead of crashing.
    """
    global _service
    if _service is not None:
        return _service, None

    if not _CREDS_JSON:
        return None, "GOOGLE_CREDENTIALS_JSON is not set in environment."

    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account

        info = json.loads(_CREDS_JSON)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _service, None
    except json.JSONDecodeError as e:
        return None, f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}"
    except Exception as e:
        return None, f"Calendar auth failed: {e}"


# ─── DATE / TIME PARSING ───────────────────────────────────────────────────────

def parse_date_time(date_str: str, time_str: str):
    """Parse 'DD.MM' + 'HH.MM' (24h) into a timezone-aware datetime.

    - Assumes the current year; if that date/time has already passed, rolls to
      next year (so '12.06' entered in December means next June, not the past).
    - Returns (datetime, error). datetime is localized to Europe/Rome.
    """
    # Date
    try:
        day, month = (int(p) for p in date_str.split("."))
    except (ValueError, TypeError):
        return None, f"Invalid date format '{date_str}'. Use DD.MM (e.g. 12.06)."
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None, f"Invalid date '{date_str}'. Day 1-31, month 1-12."

    # Time
    try:
        hour, minute = (int(p) for p in time_str.split("."))
    except (ValueError, TypeError):
        return None, f"Invalid time format '{time_str}'. Use HH.MM 24h (e.g. 14.30)."
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None, f"Invalid time '{time_str}'. Hour 0-23, minute 0-59."

    now = datetime.now(TIMEZONE)
    year = now.year

    # Build the datetime; catch impossible dates like 31.02
    try:
        naive = datetime(year, month, day, hour, minute)
    except ValueError:
        return None, f"'{date_str}' is not a real date."

    dt = TIMEZONE.localize(naive)

    # If already in the past, roll to next year
    if dt < now:
        try:
            dt = TIMEZONE.localize(datetime(year + 1, month, day, hour, minute))
        except ValueError:
            return None, f"'{date_str}' is not a real date."

    return dt, None


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
    """Parse a Google ISO datetime string into a Europe/Rome-aware datetime."""
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
