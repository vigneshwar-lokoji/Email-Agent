"""Google Calendar — find free slots for scheduling email replies."""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

TIMEZONE   = os.environ.get("USER_TIMEZONE", "Asia/Kolkata")
WORK_START = 9   # 9 AM
WORK_END   = 18  # 6 PM
SLOT_MIN   = 30  # minutes


def get_calendar_service():
    creds = Credentials.from_authorized_user_file("token.json")
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def get_free_slots(service, days_ahead: int = 5, count: int = 4) -> list[dict]:
    """Return up to `count` free 30-min slots across the next N weekdays."""
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()

    candidates = []
    day = today
    weekdays = 0
    while weekdays < days_ahead:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        weekdays += 1
        for hour in range(WORK_START, WORK_END):
            start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=tz)
            candidates.append({"start": start, "end": start + timedelta(minutes=SLOT_MIN)})

    if not candidates:
        return []

    fb = service.freebusy().query(body={
        "timeMin": candidates[0]["start"].isoformat(),
        "timeMax": candidates[-1]["end"].isoformat(),
        "items":   [{"id": "primary"}],
    }).execute()

    busy = [
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
        for b in fb.get("calendars", {}).get("primary", {}).get("busy", [])
    ]

    free = []
    for slot in candidates:
        if not any(slot["start"] < b_end and slot["end"] > b_start for b_start, b_end in busy):
            free.append(slot)
            if len(free) >= count:
                break
    return free


def format_slot(slot: dict) -> str:
    s = slot["start"].astimezone(ZoneInfo(TIMEZONE))
    return s.strftime("%a %b %d, %I:%M %p %Z")


def slots_to_text(slots: list[dict]) -> str:
    if not slots:
        return ""
    lines = ["Your available time slots (from Google Calendar):"]
    lines += [f"  - {format_slot(s)}" for s in slots]
    return "\n".join(lines)


def get_todays_events(service=None) -> list[dict]:
    """Return all events for today."""
    if service is None:
        service = get_calendar_service()

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    events_result = service.events().list(
        calendarId="primary",
        timeMin=start_of_day.isoformat(),
        timeMax=end_of_day.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    events = []
    for event in events_result.get("items", []):
        start = event.get("start", {})
        start_time = start.get("dateTime", start.get("date", ""))
        summary = event.get("summary", "No title")

        # Format time
        if "dateTime" in start:
            dt = datetime.fromisoformat(start_time).astimezone(tz)
            time_str = dt.strftime("%I:%M %p")
        else:
            time_str = "All day"

        events.append({
            "summary": summary,
            "time": time_str,
            "link": event.get("hangoutLink", ""),
        })

    return events
