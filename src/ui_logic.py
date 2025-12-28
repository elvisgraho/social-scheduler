import json
import logging
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Set

import pandas as pd
import pytz

# Imports from your existing modules
from src.database import add_to_queue, get_config, reschedule_queue_item, update_queue_status
from src.scheduling import get_schedule, next_daily_slots

logger = logging.getLogger("ui_logic")

def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parses an ISO datetime string safely, returning None if invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

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

def format_queue_dataframe(queue_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Converts raw DB rows into a clean Pandas DataFrame for the UI."""
    data = []
    for row in queue_rows:
        data.append({
            "ID": row["id"],
            "File": Path(row["file_path"]).name,
            "Scheduled": format_datetime_for_ui(row.get("scheduled_for")),
            "Status": row.get("status"),
            "Attempts": row.get("attempts", 0),
            "Last Error": row.get("last_error") or "",
        })
    
    columns = ["ID", "File", "Scheduled", "Status", "Attempts", "Last Error"]
    if not data:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(data)

def save_files_to_queue(
    files: List[Any], 
    slots: List[datetime], 
    upload_dir: Path, 
    shuffle_order: bool = False
) -> int:
    """
    Saves uploaded Streamlit files to disk and adds them to the DB queue.
    Returns the count of successfully queued items.
    """
    if not files or not slots:
        return 0

    title = get_config("global_title", "Daily Short")
    desc = get_config("global_desc", "#shorts")
    
    # Zip stops at the shortest list, preventing index errors
    paired = list(zip(files, slots))
    
    if shuffle_order:
        random.shuffle(paired)

    success_count = 0
    base_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    sequence = 1
    
    for uploaded_file, slot in paired:
        ext = Path(uploaded_file.name).suffix or ".mp4"
        destination = upload_dir / f"{base_timestamp}_{sequence:02d}{ext}"

        # Ensure uniqueness even if multiple uploads land in the same second
        while destination.exists():
            sequence += 1
            destination = upload_dir / f"{base_timestamp}_{sequence:02d}{ext}"
        sequence += 1
        
        try:
            with destination.open("wb") as f:
                f.write(uploaded_file.getbuffer())
            
            # Add to DB
            add_to_queue(str(destination), slot.isoformat(), title, desc)
            logger.info("Queued file %s for %s", destination.name, slot.isoformat())
            success_count += 1
            
        except Exception as e:
            logger.error("Failed to save or queue file %s: %s", uploaded_file.name, e)
            # Cleanup orphan file if DB insert failed
            if destination.exists():
                try:
                    destination.unlink()
                except OSError:
                    pass

    return success_count

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

def extract_tiktok_session(raw_value: str) -> str:
    """Robustly extracts the sessionid from JSON, Cookie headers, or raw strings."""
    if not raw_value:
        return ""
    
    raw = raw_value.strip()

    # 1. Try parsing as JSON (common from browser extensions)
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                # Format: {"sessionid": "..."}
                if "sessionid" in data:
                    return str(data["sessionid"])
                # Format: {"cookies": [{"name": "sessionid", ...}]}
                cookies = data.get("cookies")
                if isinstance(cookies, list):
                    for cookie in cookies:
                        if isinstance(cookie, dict) and cookie.get("name") == "sessionid":
                            return str(cookie.get("value", ""))
            
            # Format: [{"name": "sessionid", ...}]
            if isinstance(data, list):
                for cookie in data:
                    if isinstance(cookie, dict) and cookie.get("name") == "sessionid":
                        return str(cookie.get("value", ""))
        except json.JSONDecodeError:
            pass

    # 2. Try parsing as Cookie Header (sessionid=XYZ; path=/...)
    if "sessionid" in raw:
        parts = raw.split(";")
        for part in parts:
            if "sessionid" in part:
                key, _, val = part.partition("=")
                key_clean = key.strip()
                # Remove "Cookie:" prefix if present
                if ":" in key_clean:
                    key_clean = key_clean.split(":")[-1].strip()
                if key_clean == "sessionid":
                    return val.strip()

    # 3. Fallback: return raw if it looks like a simple ID (no spaces/brackets)
    if " " not in raw and ";" not in raw and "{" not in raw:
        return raw

    return raw

def get_storage_summary(data_dir: Path) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Returns (used_gb, free_gb, total_gb, percent_used)."""
    try:
        usage = shutil.disk_usage(data_dir)
        gb = 1024**3
        used_gb = usage.used / gb
        total_gb = usage.total / gb
        free_gb = usage.free / gb
        percent = (usage.used / usage.total) * 100 if usage.total else 0
        return used_gb, free_gb, total_gb, percent
    except Exception as exc:
        logger.warning("Unable to read disk usage: %s", exc)
        return None, None, None, None


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


def reschedule_pending_items(
    queue_rows: List[Dict[str, Any]], start: Optional[datetime] = None
) -> Tuple[int, Optional[datetime]]:
    """
    Move all pending/retry/failed items to the next available schedule slots.
    Failed items are re-marked as retry so they re-enter the worker.
    Returns (count_rescheduled, first_slot_used).
    """
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