import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Any, Tuple, Optional, Dict

import pandas as pd

from src.database import add_to_queue, get_config
from src.scheduling import next_daily_slots

logger = logging.getLogger("ui_logic")


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
    import random
    
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
                            return str(cookie.get("value", "")).strip()
            
            # Format: [{"name": "sessionid", ...}]
            if isinstance(data, list):
                for cookie in data:
                    if isinstance(cookie, dict) and cookie.get("name") == "sessionid":
                        return str(cookie.get("value", "")).strip()
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


def format_queue_dataframe(queue_rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Converts raw DB rows into a clean Pandas DataFrame for the UI."""
    from src.ui_logic.datetime_utils import format_datetime_for_ui
    
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
