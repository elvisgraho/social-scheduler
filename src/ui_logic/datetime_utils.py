from datetime import datetime
from typing import Optional
import pytz
from src.scheduling import get_schedule


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parses an ISO datetime string safely, returning None if invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_datetime_for_ui(value: Optional[str]) -> str:
    """Formats an ISO string into a human-readable local time string."""
    dt = parse_iso(value)
    if not dt:
        return "Not scheduled"
    
    tz_name = get_schedule().get("timezone", "UTC")
    try:
        local_tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        local_tz = pytz.UTC

    # Ensure dt is aware before converting
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
        
    local_dt = dt.astimezone(local_tz)
    return local_dt.strftime("%b %d, %Y %H:%M")


def format_uploaded_time(value: Optional[str]) -> str:
    """
    Formats uploaded_at which may be ISO or SQLite-style 'YYYY-MM-DD HH:MM:SS'.
    """
    if not value:
        return "Unknown"
    dt = parse_iso(value)
    if not dt:
        try:
            dt = datetime.fromisoformat(value.replace(" ", "T"))
        except Exception:
            return value
    tz_name = get_schedule().get("timezone", "UTC")
    try:
        local_tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        local_tz = pytz.UTC
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(local_tz).strftime("%b %d, %Y %H:%M")
