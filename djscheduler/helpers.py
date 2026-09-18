"""Date / time parsing and rendering helpers for the DJScheduler cog."""

import re
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from zoneinfo import ZoneInfo

# Every slot is always rendered in these three zones, in this order.
DISPLAY_ZONES = ("America/New_York", "Europe/Berlin", "America/Los_Angeles")

# Winter (standard time) abbreviation of each display zone, used for the
# "summer time" explainer line.
STANDARD_ABBR = {
    "America/New_York": "EST",
    "Europe/Berlin": "CET",
    "America/Los_Angeles": "PST",
}

# What an admin may type for the server's anchor timezone.
TZ_ALIASES = {
    "ET": "America/New_York",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "EASTERN": "America/New_York",
    "CET": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "CE": "Europe/Berlin",
    "EU": "Europe/Berlin",
    "EUROPE": "Europe/Berlin",
    "PT": "America/Los_Angeles",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "UTC": "UTC",
    "GMT": "UTC",
}

WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)


def resolve_tz(raw: str) -> str:
    """Turn user input into an IANA timezone name, or raise ValueError."""
    candidate = TZ_ALIASES.get(raw.strip().upper(), raw.strip())
    try:
        ZoneInfo(candidate)
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"`{raw}` is not a timezone I know. Try `EST`, `CET`, `PST`, `UTC`, "
            f"or a full name like `Europe/Athens`."
        ) from exc
    return candidate


def parse_day(raw: str, anchor_tz: str) -> Date:
    """Parse `today`, `tomorrow`, a weekday name, or an ISO `YYYY-MM-DD` date."""
    text = raw.strip().lower()
    today = datetime.now(ZoneInfo(anchor_tz)).date()

    if text in ("today", "tonight"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)

    weekday = WEEKDAYS.get(text)
    if weekday is not None:
        # Next occurrence of that weekday; "friday" on a Friday means next week.
        return today + timedelta(days=(weekday - today.weekday()) % 7 or 7)

    try:
        return Date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"`{raw}` is not a day I understand. Use `2026-09-25`, `today`, "
            f"`tomorrow`, or a weekday like `friday`."
        ) from exc


def parse_hour(raw: str) -> int:
    """Parse `20:00`, `20` or `8pm` into an hour 0-23."""
    match = TIME_RE.match(raw.strip())
    if not match:
        raise ValueError(f"`{raw}` is not a time I understand. Try `20:00`, `20` or `8pm`.")

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()

    if minute:
        raise ValueError("Slots start on the hour, so the minutes have to be `00`.")
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"`{raw}` is not a valid 12-hour time.")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not 0 <= hour <= 23:
        raise ValueError(f"`{raw}` is not a valid hour.")
    return hour


def zone_line(ts: int, base_day: Date) -> str:
    """Render one moment in all three display zones, marking day rollovers."""
    parts = []
    for zone in DISPLAY_ZONES:
        moment = datetime.fromtimestamp(ts, ZoneInfo(zone))
        offset = (moment.date() - base_day).days
        rollover = f" ({offset:+d}d)" if offset else ""
        parts.append(f"{moment:%H:%M} {moment.tzname()}{rollover}")
    return " · ".join(parts)


def summer_note(ts: int) -> Optional[str]:
    """Explain the summer-time abbreviations, if any zone is on DST that day."""
    active = []
    for zone in DISPLAY_ZONES:
        moment = datetime.fromtimestamp(ts, ZoneInfo(zone))
        if moment.dst():
            active.append(f"{moment.tzname()} = {STANDARD_ABBR[zone]}+1h")
    if not active:
        return None
    return "☀️ Summer time in effect: " + " · ".join(active)


def build_slots(
    day: Date, anchor_tz: str, start_hour: int, end_hour: int, per_hour: int, first_id: int
) -> dict:
    """Create real one-hour slots between local clock boundaries.

    The end is exclusive. A repeated boundary uses its first occurrence;
    nonexistent boundaries are rejected instead of silently shifting them.
    """
    tz = ZoneInfo(anchor_tz)
    start = datetime(day.year, day.month, day.day, start_hour, tzinfo=tz)
    end_day = day + timedelta(days=1) if end_hour <= start_hour else day
    end = datetime(end_day.year, end_day.month, end_day.day, end_hour, tzinfo=tz)
    for boundary in (start, end):
        restored = boundary.astimezone(timezone.utc).astimezone(tz)
        if restored.replace(tzinfo=None) != boundary.replace(tzinfo=None):
            raise ValueError(
                f"{boundary:%Y-%m-%d %H:%M} does not exist in {anchor_tz} because "
                "the clocks move forward. Choose another start or end hour."
            )
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    seconds = int((end_utc - start_utc).total_seconds())
    if seconds <= 0 or seconds % 3600:
        raise ValueError(
            "This clock change does not fit whole one-hour slots. Choose another range."
        )
    span = seconds // 3600

    slots = {}
    slot_id = first_id
    for step in range(span):
        ts = int((start_utc + timedelta(hours=step)).timestamp())
        for _ in range(per_hour):
            slots[str(slot_id)] = {"ts": ts, "user": None}
            slot_id += 1
    return slots
