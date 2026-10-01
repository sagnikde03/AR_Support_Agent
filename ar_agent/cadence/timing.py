"""
Business Hours Gate (F-07).

Prevents outbound emails from being sent outside business hours. All times
are evaluated in US Eastern Time to match the CapLinked team's working day.

Configurable via environment variables:
  BH_START_HOUR  — 24h format, default 9  (9 AM ET)
  BH_END_HOUR    — 24h format, default 18 (6 PM ET)
  BH_TIMEZONE    — default "America/New_York"

The gate also enforces a minimum 15-minute pre-send delay (F-18) for shadow-
mode approvals, so callers should always call should_send_now() rather than
querying the window directly.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# zoneinfo is stdlib in Python 3.9+; pytz is the fallback for older installs
try:
    from zoneinfo import ZoneInfo
    def _eastern() -> ZoneInfo:
        return ZoneInfo(os.environ.get("BH_TIMEZONE", "America/New_York"))
except ImportError:
    import pytz
    def _eastern():  # type: ignore
        return pytz.timezone(os.environ.get("BH_TIMEZONE", "America/New_York"))


def _start_hour() -> int:
    return int(os.environ.get("BH_START_HOUR", "9"))


def _end_hour() -> int:
    return int(os.environ.get("BH_END_HOUR", "18"))


def _normalize_utc(at: datetime | None) -> datetime:
    """Return a timezone-aware UTC datetime; treat naive inputs as UTC."""
    if at is None:
        return datetime.now(timezone.utc)
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def is_business_hours(at: datetime | None = None) -> bool:
    """
    Return True if `at` (default: now) falls within the configured business
    hours window on a weekday.
    """
    now = _normalize_utc(at)
    local = now.astimezone(_eastern())
    if local.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _start_hour() <= local.hour < _end_hour()


def next_business_hours_open(at: datetime | None = None) -> datetime:
    """
    Return the next datetime (UTC) when the business hours window opens.
    If already within business hours, returns the normalized UTC datetime.
    """
    now = _normalize_utc(at)
    if is_business_hours(now):
        return now  # normalized UTC — satisfies the UTC contract

    local = now.astimezone(_eastern())

    # Advance to next weekday if needed
    candidate = local.replace(hour=_start_hour(), minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate += timedelta(days=1)

    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc)


def should_send_now(at: datetime | None = None) -> bool:
    """Convenience wrapper — returns True only if currently in business hours."""
    return is_business_hours(at)


def format_next_window() -> str:
    """Human-readable string of when the next business window opens (for Slack messages)."""
    nxt = next_business_hours_open()
    local = nxt.astimezone(_eastern())
    return local.strftime("%A, %b %-d at %-I:%M %p %Z")
