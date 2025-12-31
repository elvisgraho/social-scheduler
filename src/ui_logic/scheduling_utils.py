import random
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Set, Tuple
import pytz
from src.database import reschedule_queue_item, update_queue_status
from src.scheduling import get_schedule, next_daily_slots
from src.ui_logic.datetime_utils import parse_iso


def get_schedule_start_time(queue_rows: List[Dict[str, Any]]) -> datetime:
    """
    Determines the anchor time for scheduling new uploads.
    Returns the later of 'now' or the 'latest scheduled item'.
    """
    cfg = get_schedule()
    try:
        tz = pytz.timezone(cfg.get("timezone", "UTC"))
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC
        
    now = datetime.now(tz)
    
    scheduled_times = []
    for row in queue_rows:
        if row.get("status") in ("uploaded", "failed"):
            continue # Ignore completed/failed items for future scheduling anchor
            
        dt = parse_iso(row.get("scheduled_for"))
        if dt:
            # Ensure the parsed time is timezone-aware for comparison
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            scheduled_times.append(dt)

    if not scheduled_times:
        return now

    latest_scheduled = max(scheduled_times)
    # If the latest scheduled item is in the past, start from now
    return max(latest_scheduled, now)


def occupied_schedule_dates(queue_rows: List[Dict[str, Any]]) -> Set[str]:
    """
    Return set of YYYY-MM-DD strings already scheduled for active items.
    Includes failed rows so new/rescheduled items don't land on the same day.
    """
    dates = set()
    for row in queue_rows:
        if row.get("status") not in ("pending", "retry", "processing", "failed"):
            continue
        dt = parse_iso(row.get("scheduled_for"))
        if not dt:
            continue
        dt = dt.date()
        dates.add(dt.isoformat())
    return dates


def shuffle_queue(queue_rows: List[Dict[str, Any]]) -> Tuple[int, Optional[datetime]]:
    """
    Randomly reassign schedule slots for pending/retry items starting from NOW.
    This shuffles the order without pushing items further into the future.
    Returns (count_shuffled, first_slot_used).
    """
    pending_items = [row for row in queue_rows if row.get("status") in ("pending", "retry")]
    if not pending_items:
        return 0, None

    cfg = get_schedule()
    try:
        tz = pytz.timezone(cfg.get("timezone", "UTC"))
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    # Start from NOW, not from the latest scheduled time (which pushes items further)
    anchor = datetime.now(tz)
    if anchor.tzinfo is None:
        anchor = tz.localize(anchor)
    else:
        anchor = anchor.astimezone(tz)

    # Block dates used by non-pending items (so shuffled items don't land on same day)
    occupied: Set[str] = set()
    for row in queue_rows:
        if row.get("status") not in ("pending", "retry"):
            dt = parse_iso(row.get("scheduled_for"))
            if dt:
                occupied.add(dt.date().isoformat())

    slots = next_daily_slots(len(pending_items), start=anchor, occupied_dates=occupied)
    if not slots:
        return 0, None

    random.shuffle(pending_items)
    shuffled = 0
    for row, slot in zip(pending_items, slots):
        reschedule_queue_item(row["id"], slot.isoformat())
        shuffled += 1
        occupied.add(slot.date().isoformat())

    return shuffled, slots[0] if shuffled else None


def reschedule_pending_items(
    queue_rows: List[Dict[str, Any]], start: Optional[datetime] = None
) -> Tuple[int, Optional[datetime]]:
    """
    Move all pending/retry/failed items to the next available schedule slots.
    Failed items are re-marked as retry so they re-enter the worker.
    Returns (count_rescheduled, first_slot_used).
    """
    import json
    import logging
    logger = logging.getLogger("ui_logic")
    
    pending_items = [row for row in queue_rows if row.get("status") in ("pending", "retry", "failed")]
    if not pending_items:
        return 0, None

    cfg = get_schedule()
    try:
        tz = pytz.timezone(cfg.get("timezone", "UTC"))
    except pytz.UnknownTimeZoneError:
        tz = pytz.UTC

    anchor = start or datetime.now(tz)
    if anchor.tzinfo is None:
        anchor = tz.localize(anchor)
    else:
        anchor = anchor.astimezone(tz)

    # Block any dates belonging to items that are not being rescheduled (e.g., processing).
    occupied: Set[str] = set()
    rescheduled_statuses = {"pending", "retry", "failed"}
    for row in queue_rows:
        if row.get("status") in rescheduled_statuses:
            continue
        dt = parse_iso(row.get("scheduled_for"))
        if dt:
            occupied.add(dt.date().isoformat())

    slots = next_daily_slots(len(pending_items), start=anchor, occupied_dates=occupied)
    if not slots:
        return 0, None

    def _scheduled_key(item: Dict[str, Any]) -> datetime:
        dt = parse_iso(item.get("scheduled_for"))
        if dt and dt.tzinfo is None:
            dt = tz.localize(dt)
        return dt or datetime.min.replace(tzinfo=pytz.UTC)

    pending_sorted = sorted(pending_items, key=_scheduled_key)
    rescheduled = 0
    
    def _parse_logs(log_value: Any) -> Dict[str, Any]:
        """
        Normalize platform_logs into a dict so we can re-store without double-encoding.
        """
        if not log_value:
            return {}
        if isinstance(log_value, dict):
            return log_value
        if isinstance(log_value, str):
            try:
                parsed = json.loads(log_value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}
    
    for row, slot in zip(pending_sorted, slots):
        # Reactivate failed items by marking them as retry while preserving logs context.
        # We handle logs parsing here to avoid double-stringification in update_queue_status
        current_logs = _parse_logs(row.get("platform_logs"))
        
        status_to_set = "retry" if row.get("status") == "failed" else row.get("status")
        
        # 1. Update status if changing from failed -> retry
        if row.get("status") == "failed":
            update_queue_status(
                row["id"],
                "retry",
                row.get("last_error"),
                current_logs,
            )
            
        # 2. Update time
        reschedule_queue_item(row["id"], slot.isoformat())
        rescheduled += 1
        occupied.add(slot.date().isoformat())

    if rescheduled < len(pending_sorted):
        logger.warning("Only rescheduled %s/%s items due to limited slots.", rescheduled, len(pending_sorted))
    return rescheduled, slots[0] if rescheduled else None
