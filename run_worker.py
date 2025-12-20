import json
import os
import time
import random 
from datetime import datetime

import pytz
import schedule

from src.database import (
    get_config,
    get_due_queue,
    increment_attempts,
    init_db,
    reschedule_queue_item,
    set_config,
    update_queue_status,
)
from src.logging_utils import init_logging
from src.notifier import send_telegram_message
from src.platform_registry import get_platforms
from src.platforms import tiktok as tiktok_platform
from src.scheduling import get_schedule, next_slots

logger = init_logging("worker")
MAX_ATTEMPTS = 3


def _now_with_timezone() -> datetime:
    tz_name = get_schedule()["timezone"]
    tz = pytz.timezone(tz_name)
    return datetime.now(tz)


def _notify(message: str) -> None:
    logger.info(message)
    send_telegram_message(message)


def warn_tiktok_session_if_needed() -> None:
    status = tiktok_platform.session_status()
    if not status["sessionid"]:
        set_config("tiktok_refresh_warned", "")
        return
    if not status["needs_refresh"]:
        set_config("tiktok_refresh_warned", "")
        return
    today = datetime.utcnow().date().isoformat()
    last_warned = get_config("tiktok_refresh_warned")
    if last_warned == today:
        return
    set_config("tiktok_refresh_warned", today)
    logger.warning("TikTok session cookie older than %s days.", status.get("age_days"))
    _notify("TikTok session cookie is older than 25 days. Refresh it soon to avoid upload failures.")


def process_video(video: dict) -> None:
    queue_id = video["id"]
    file_path = video["file_path"]
    
    # 1. Parse previous logs to prevent double-uploading on retry
    raw_logs = video.get("platform_logs")
    previous_logs = {}
    if isinstance(raw_logs, str) and raw_logs:
        try:
            previous_logs = json.loads(raw_logs)
        except json.JSONDecodeError:
            pass
    elif isinstance(raw_logs, dict):
        previous_logs = raw_logs

    # 2. Increment attempts immediately
    previous_attempts = video.get("attempts", 0) or 0
    attempts = previous_attempts + 1
    increment_attempts(queue_id)
    
    # Update status to processing so other workers don't grab it (if you scale later)
    update_queue_status(queue_id, "processing", None, previous_logs)

    # 3. File Integrity Check
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        msg = f"File missing or empty for queue #{queue_id}: {file_path}"
        update_queue_status(queue_id, "failed", msg, {"error": msg})
        _notify(msg)
        return

    title = video.get("title") or get_config("global_title", "Short")
    description = video.get("description") or get_config("global_desc", "")

    current_logs = previous_logs.copy()
    failures = []
    missing_accounts = []
    platforms = get_platforms()

    # 4. Process Platforms
    for key, cfg in platforms.items():
        label = cfg["label"]
        
        # SKIP if already succeeded in a previous attempt
        prev_status = current_logs.get(key, "")
        if "success" in str(prev_status).lower() or "uploaded id" in str(prev_status).lower():
            logger.info("Skipping %s for #%s (already uploaded).", label, queue_id)
            continue

        if not cfg["connected"]():
            reason = f"{label} not connected."
            current_logs[key] = reason
            missing_accounts.append(label)
            continue

        # Add Jitter (wait 10-30 seconds between platforms to avoid bot detection)
        time.sleep(random.uniform(10, 30))

        uploader = cfg["uploader"]
        try:
            if key == "youtube":
                ok, message = uploader(file_path, title, description)
            else:
                ok, message = uploader(file_path, description)
        except Exception as e:
            ok = False
            message = str(e)

        current_logs[key] = message
        
        if ok:
            logger.info("%s upload success for queue #%s: %s", label, queue_id, message)
        else:
            failure = f"{label} failed: {message}"
            failures.append(failure)
            logger.error(failure)

    # 5. Determine Final Status
    # If there are failures, we retry. BUT we must ensure we don't retry forever.
    if failures or missing_accounts:
        status = "retry" if attempts < MAX_ATTEMPTS else "failed"
        
        # Consolidate error messages
        error_msg = "; ".join(failures) if failures else "Awaiting account connections."
        
        update_queue_status(queue_id, status, error_msg, current_logs)
        
        if status == "retry":
            # Smart Reschedule: Ensure we don't retry immediately.
            # Look for next slot, but force at least 1 hour delay for retries.
            future_slots = next_slots(1, start=_now_with_timezone())
            
            retry_time = None
            if future_slots:
                retry_time = future_slots[0]
            
            # Enforce minimum backoff of 60 mins for retries to allow API limits to reset
            # (Logic depends on your next_slots implementation, but this is safer)
            from datetime import timedelta
            min_backoff = _now_with_timezone() + timedelta(minutes=60)
            
            if not retry_time or retry_time < min_backoff:
                retry_time = min_backoff

            reschedule_queue_item(queue_id, retry_time.isoformat())
            logger.info("Rescheduled queue #%s (Attempt %s/%s) for %s due to failures.", 
                        queue_id, attempts, MAX_ATTEMPTS, retry_time.isoformat())
            
        if failures:
            _notify(f"Queue #{queue_id} partial failure: {'; '.join(failures)}")
        return

    # Success
    update_queue_status(queue_id, "uploaded", None, current_logs)
    _notify(f"Queue #{queue_id} uploaded to all connected platforms.")


def check_and_post():
    global WORKER_BUSY
    if WORKER_BUSY:
        logger.debug("Worker is busy, skipping schedule tick.")
        return

    WORKER_BUSY = True
    try:
        warn_tiktok_session_if_needed()
        now = _now_with_timezone()
        due = get_due_queue(now.isoformat())
        
        if not due:
            logger.debug("No videos due at %s", now.isoformat())
            return

        for video in due:
            # Check if status is still pending (in case of race conditions if multiple workers exist)
            if video.get('status') not in ('pending', 'retry'):
                continue
                
            logger.info("Processing queue item %s.", video["id"])
            process_video(video)
            
            # Add delay between different videos too
            time.sleep(random.uniform(5, 15))
            
    except Exception as e:
        logger.error("Error in check_and_post: %s", e)
    finally:
        WORKER_BUSY = False


def main():
    logger.info("Scheduler worker started.")
    init_db()
    schedule.every(1).minutes.do(check_and_post)
    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
