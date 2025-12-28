import math
from datetime import datetime, timedelta, time
from typing import Dict, List, Optional

import pytz

from src.database import get_json_config, set_json_config

DEFAULT_SCHEDULE = {
    "days": list(range(7)),  # 0=Mon
    "times": ["11:00"],
    "timezone": "UTC",
}


def _normalize_schedule(config: Optional[Dict]) -> Dict[str, List]:
    if not config:
        return DEFAULT_SCHEDULE.copy()
    days = config.get("days", DEFAULT_SCHEDULE["days"])
    times = config.get("times", DEFAULT_SCHEDULE["times"])
    tz_name = config.get("timezone", DEFAULT_SCHEDULE["timezone"])
    valid_days = sorted({d for d in days if isinstance(d, int) and 0 <= d <= 6})
    valid_times = []
    for raw in times:
        try:
            clean = raw.strip()
            datetime.strptime(clean, "%H:%M")
            valid_times.append(clean)
        except Exception:
            continue
    if not valid_times:
        valid_times = DEFAULT_SCHEDULE["times"]
    if not valid_days:
        valid_days = DEFAULT_SCHEDULE["days"]
    try:
        pytz.timezone(tz_name)
    except Exception:
        tz_name = DEFAULT_SCHEDULE["timezone"]
    return {"days": valid_days, "times": sorted(valid_times), "timezone": tz_name}


def get_schedule() -> Dict[str, List]:
    cfg = get_json_config("publish_schedule", DEFAULT_SCHEDULE)
    normalized = _normalize_schedule(cfg)
    if normalized != cfg:
        set_json_config("publish_schedule", normalized)
    return normalized


def save_schedule(days: List[int], times: List[str], timezone: str) -> None:
    payload = _normalize_schedule(
        {"days": days, "times": times, "timezone": timezone or DEFAULT_SCHEDULE["timezone"]}
    )
    if timezone:
        payload["timezone"] = timezone
    set_json_config("publish_schedule", payload)


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def next_slots(count: int, start: Optional[datetime] = None) -> List[datetime]:
    cfg = get_schedule()
    tz = pytz.timezone(cfg["timezone"])
    if start:
        now = start if start.tzinfo else tz.localize(start)
        now = now.astimezone(tz)
    else:
        now = datetime.now(tz)
    slots: List[datetime] = []

    # limit search to next 90 days to avoid infinite loops
    day_offset = 0
    while len(slots) < count and day_offset < 90:
        candidate_date = (now + timedelta(days=day_offset)).date()
        weekday = candidate_date.weekday()
        if weekday in cfg["days"]:
            for ts in cfg["times"]:
                dt = tz.localize(datetime.combine(candidate_date, _parse_time(ts)))
                if dt <= now:
                    continue
                slots.append(dt)
                if len(slots) >= count:
                    break
        day_offset += 1
    return slots


def next_daily_slots(
    count: int,
    start: Optional[datetime] = None,
    occupied_dates: Optional[set] = None,
) -> List[datetime]:
    """
    Like next_slots but enforces at most one slot per calendar day by skipping dates
    already occupied (YYYY-MM-DD in the configured timezone).
    """
    occupied_dates = occupied_dates or set()
    cfg = get_schedule()
    tz = pytz.timezone(cfg["timezone"])
    now = datetime.now(tz) if start is None else (start if start.tzinfo else tz.localize(start)).astimezone(tz)

    slots: List[datetime] = []
    day_offset = 0
    while len(slots) < count and day_offset < 90:
        candidate_date = (now + timedelta(days=day_offset)).date()
        weekday = candidate_date.weekday()
        day_key = candidate_date.isoformat()
        if weekday in cfg["days"] and day_key not in occupied_dates:
            for ts in cfg["times"]:
                dt = tz.localize(datetime.combine(candidate_date, _parse_time(ts)))
                if dt <= now:
                    continue
                slots.append(dt)
                break
        day_offset += 1
    return slots


def human_readable_schedule() -> str:
    cfg = get_schedule()
    days_map = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = ", ".join(days_map[d] for d in cfg["days"])
    times = ", ".join(cfg["times"])
    return f"{days} @ {times} ({cfg['timezone']})"
