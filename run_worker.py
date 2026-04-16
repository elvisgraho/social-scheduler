"""
Background worker for processing the upload queue.

Handles scheduled video uploads to multiple platforms (YouTube, Instagram, TikTok)
with retry logic, platform shuffling, and token verification.
"""

import json
import os
import random
import time
from datetime import datetime
from datetime import time as dtime
from datetime import timedelta

import pytz
import schedule

from src.auth_utils import verify_youtube_credentials
from src.database import (
    archive_uploaded_item,
    get_config,
    get_due_queue,
    get_pending_count,
    get_queue,
    increment_attempts,
    init_db,
    reschedule_queue_item,
    set_config,
    update_queue_status,
)
from src.logging_utils import init_logging
from src.logging_utils import log_once
from src.notifier import send_telegram_message
from src.platform_registry import get_platforms
from src.platforms import instagram as instagram_platform
from src.platforms import tiktok as tiktok_platform
from src.scheduling import get_schedule
from src.scheduling import next_daily_slots

logger = init_logging("worker")

# State keys
WORKER_BUSY = False
PAUSE_KEY = "queue_paused"
FORCE_KEY = "queue_force_run"
FORCE_PLATFORM_KEY = "queue_force_platform"
FORCE_QUEUE_ID_KEY = "queue_force_id"
TOKEN_CHECK_KEY = "last_token_check_date"


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


def _platform_shuffle_enabled() -> bool:
    return True


def _run_token_checks(now: datetime) -> None:
    """
    Validate platform tokens/sessions and warn on failure.
    Only runs once per week to avoid suspicious repeated login checks.
    """
    ok, msg = verify_youtube_credentials(probe_api=False)
    if ok:
        logger.info("Weekly YouTube token verification passed.")
        set_config("last_youtube_ok", now.isoformat())
    else:
        _notify(f"YouTube token check failed: {msg}")

    # SKIP Instagram verification - it's unnecessary and suspicious
    # Instagram sessions are checked naturally during uploads
    # No need to create extra login events that could trigger bot detection
    logger.info("Skipping Instagram verification (checked during actual uploads)")

    tt_ok, tt_msg = tiktok_platform.verify_session(force=True)
    if tt_ok:
        logger.info("Weekly TikTok session verification passed.")
        set_config("last_tiktok_ok", now.isoformat())
    else:
        _notify(f"TikTok session check failed: {tt_msg}")

    set_config(TOKEN_CHECK_KEY, now.date().isoformat())


def _maybe_verify_tokens(now: datetime) -> None:
    """
    Run token checks once per week (not daily) to avoid suspicious patterns.
    Only runs in the 6-10 AM window, and only once that day.
    """
    try:
        last_run = get_config(TOKEN_CHECK_KEY)

        # Check if we already ran today - prevents multiple runs in same day
        today_str = now.date().isoformat()
        if last_run and last_run == today_str:
            return

        # Check if 7 days have passed since last check
        if last_run:
            try:
                # last_run is stored as a date string (YYYY-MM-DD), parse it directly
                last_check = datetime.strptime(last_run, "%Y-%m-%d").date()
                days_since = (now.date() - last_check).days
                # Only run once per week (7 days minimum)
                if days_since < 7:
                    return
            except (ValueError, TypeError):
                # Invalid date format, proceed with check
                logger.warning("Invalid token check date format: %s, proceeding with check", last_run)
                pass

        # Add randomness: only run checks between 6 AM and 10 AM (not exactly 8 AM)
        if not (dtime(hour=6, minute=0) <= now.time() <= dtime(hour=10, minute=0)):
            return

        # All conditions met - run the check
        _run_token_checks(now)
    except Exception as exc:
        logger.warning("Skipping token check: %s", exc)


def _pull_queue_forward(now: datetime) -> None:
    """
    When a force run occurs, shift the remaining pending/retry items to the earliest
    available daily slots starting now (preserving one-per-day constraint).
    """
    try:
        all_rows = get_queue(limit=200)
        pending = [row for row in all_rows if row.get("status") in ("pending", "retry")]
        if not pending:
            return

        # Block dates already used by non-pending items (e.g. processing, failed)
        # so the pulled items don't stack on days that are already occupied.
        occupied: set = set()
        from src.ui_logic.datetime_utils import parse_iso as _parse_iso
        cfg = get_schedule()
        import pytz as _pytz
        try:
            _tz = _pytz.timezone(cfg.get("timezone", "UTC"))
        except Exception:
            _tz = _pytz.UTC

        for row in all_rows:
            if row.get("status") in ("pending", "retry"):
                continue
            dt = _parse_iso(row.get("scheduled_for"))
            if dt:
                if dt.tzinfo:
                    dt = dt.astimezone(_tz)
                occupied.add(dt.date().isoformat())

        slots = next_daily_slots(len(pending), start=now, occupied_dates=occupied)
        if len(slots) < len(pending):
            logger.warning("Not enough slots to pull queue forward (%s needed, %s available).", len(pending), len(slots))
        for row, slot in zip(pending, slots):
            reschedule_queue_item(row["id"], slot.isoformat())
            occupied.add(slot.date().isoformat())
            logger.info("Pulled queue item %s forward to %s.", row["id"], slot.isoformat())
    except Exception as exc:
        logger.warning("Failed to pull queue forward: %s", exc)


def _preflight_platform(platform_key: str) -> tuple[bool, str]:
    """
    Quick readiness checks before attempting an upload so we fail fast with actionable errors.
    """
    if platform_key == "youtube":
        return verify_youtube_credentials(probe_api=True)
    if platform_key == "instagram":
        return instagram_platform.verify_login()
    if platform_key == "tiktok":
        return tiktok_platform.verify_session(force=True)
    return True, ""


def reset_stale_tasks():
    """
    CRITICAL FIX: Reset tasks stuck in 'processing' due to worker crash/restart.
    This prevents items from getting stuck forever if the container dies during upload.
    """
    try:
        # Get all tasks, filter manually or rely on DB status
        all_items = get_queue(limit=1000)
        stale_tasks = [r for r in all_items if r["status"] == "processing"]
        
        if stale_tasks:
            logger.warning("Found %d stale 'processing' tasks on startup. Resetting to 'pending'.", len(stale_tasks))
            for task in stale_tasks:
                # Parse existing logs to avoid double-encoding
                raw_logs = task.get("platform_logs")
                existing_logs = {}
                if raw_logs:
                    if isinstance(raw_logs, str):
                        try:
                            existing_logs = json.loads(raw_logs)
                        except json.JSONDecodeError:
                            existing_logs = {}
                    elif isinstance(raw_logs, dict):
                        existing_logs = raw_logs
                # Keep logs, just reset status so it tries again
                update_queue_status(task["id"], "pending", None, existing_logs)
    except Exception as e:
        logger.error("Failed to reset stale tasks: %s", e)


def process_video(video: dict, forced_platforms: set[str] | None = None, is_forced: bool = False) -> None:
    queue_id = video["id"]
    file_path = video["file_path"]
    forced_platforms = set(forced_platforms or [])

    # Allow processing if forced, even when paused
    paused = bool(int(get_config(PAUSE_KEY, 0) or 0))
    if paused and not is_forced:
        logger.info("Queue is paused. Skipping processing for #%s.", queue_id)
        return

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

    # 2. Parse enabled platforms for this specific video (if set)
    enabled_platforms_for_video = None
    raw_enabled = video.get("enabled_platforms")
    if raw_enabled:
        try:
            if isinstance(raw_enabled, str):
                enabled_platforms_for_video = set(json.loads(raw_enabled))
            elif isinstance(raw_enabled, list):
                enabled_platforms_for_video = set(raw_enabled)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse enabled_platforms for queue #%s", queue_id)

    # 3. Increment attempts immediately
    previous_attempts = video.get("attempts", 0) or 0
    attempts = previous_attempts + 1
    increment_attempts(queue_id)

    # Update status to processing so other workers don't grab it (if you scale later)
    update_queue_status(queue_id, "processing", None, previous_logs)

    # 4. File Integrity Check
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        msg = f"File missing or empty for queue #{queue_id}: {file_path}"
        update_queue_status(queue_id, "failed", msg, {"error": msg})
        _notify(msg)
        return

    # Get base title and description
    base_title = video.get("title") or get_config("global_title", "Short")
    base_description = video.get("description") or get_config("global_desc", "")

    # Parse platform-specific overrides from video (if any)
    video_platform_overrides = {}
    raw_overrides = video.get("platform_overrides")
    if raw_overrides:
        try:
            if isinstance(raw_overrides, str):
                video_platform_overrides = json.loads(raw_overrides)
            elif isinstance(raw_overrides, dict):
                video_platform_overrides = raw_overrides
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse platform_overrides for queue #%s", queue_id)

    current_logs = previous_logs.copy()
    failures = []
    successes = []
    missing_accounts = []
    platforms = get_platforms()
    platform_items = list(platforms.items())

    # Check if staged uploads are enabled
    staged_uploads_enabled = bool(int(get_config("staged_uploads_enabled", "0") or "0"))
    test_platform_key = get_config("staged_upload_test_platform", "youtube")

    # If staged uploads enabled, upload to test platform first
    if staged_uploads_enabled and test_platform_key in platforms:
        # Reorder: test platform first, then others
        test_platform_items = [(k, v) for k, v in platform_items if k == test_platform_key]
        other_platform_items = [(k, v) for k, v in platform_items if k != test_platform_key]

        if _platform_shuffle_enabled():
            random.shuffle(other_platform_items)

        platform_items = test_platform_items + other_platform_items
        logger.info("Staged uploads enabled: testing with %s first", test_platform_key)
    elif _platform_shuffle_enabled():
        random.shuffle(platform_items)

    # 5. Process Platforms
    staged_test_failed = False
    for idx, (key, cfg) in enumerate(platform_items):
        # If staged uploads and test platform failed, skip remaining platforms
        if staged_uploads_enabled and staged_test_failed and idx > 0:
            logger.info("Skipping %s for #%s (staged test platform failed)", cfg["label"], queue_id)
            current_logs[key] = "Skipped due to test platform failure"
            continue

        # Skip if forced platforms set and this platform not in forced list
        if forced_platforms and key not in forced_platforms:
            continue

        # Skip if this video has specific platforms enabled and this platform is not in the list
        if enabled_platforms_for_video is not None and key not in enabled_platforms_for_video:
            logger.info("Skipping %s for #%s (not enabled for this video).", cfg["label"], queue_id)
            continue

        label = cfg["label"]

        # SKIP if already succeeded in a previous attempt
        prev_status = current_logs.get(key, "")
        if "success" in str(prev_status).lower() or "uploaded id" in str(prev_status).lower():
            logger.info("Skipping %s for #%s (already uploaded).", label, queue_id)
            successes.append(label)
            continue

        if not cfg["connected"]():
            reason = f"{label} not connected."
            current_logs[key] = reason
            missing_accounts.append(label)
            continue

        preflight_ok, preflight_msg = _preflight_platform(key)
        if not preflight_ok:
            failure = f"{label} failed: {preflight_msg}"
            current_logs[key] = preflight_msg
            failures.append((label, preflight_msg))
            logger.error(failure)
            # CONTINUE to other platforms instead of stopping
            continue

        # Add delay BETWEEN platforms (not before first platform).
        # Simulates switching between apps like a human would.
        # Skipped on forced uploads — the user is waiting for immediate feedback.
        if idx > 0 and not is_forced:
            if key == "instagram":
                delay = random.uniform(60, 180)  # 1-3 minutes
                logger.info("Waiting %d seconds before Instagram upload (human-like delay)...", int(delay))
            else:
                delay = random.uniform(30, 90)  # 30 seconds to 1.5 minutes
                logger.info("Waiting %d seconds before %s upload...", int(delay), key)
            time.sleep(delay)

        # Get platform-specific title/description
        # Priority: video-specific override > global platform override > base title/description
        platform_title = base_title
        platform_description = base_description

        # Check video-specific overrides first
        if key in video_platform_overrides:
            overrides = video_platform_overrides[key]
            if "title" in overrides and overrides["title"]:
                platform_title = overrides["title"]
            if "description" in overrides and overrides["description"]:
                platform_description = overrides["description"]
        else:
            # Fall back to global platform overrides from settings
            if key == "youtube":
                yt_title_override = get_config("youtube_title_override", "")
                yt_desc_override = get_config("youtube_desc_override", "")
                if yt_title_override:
                    platform_title = yt_title_override
                if yt_desc_override:
                    platform_description = yt_desc_override
            elif key == "instagram":
                ig_desc_override = get_config("instagram_desc_override", "")
                if ig_desc_override:
                    platform_description = ig_desc_override
            elif key == "tiktok":
                tt_desc_override = get_config("tiktok_desc_override", "")
                if tt_desc_override:
                    platform_description = tt_desc_override

        uploader = cfg["uploader"]
        try:
            if key == "youtube":
                ok, message = uploader(file_path, platform_title, platform_description)
            else:
                ok, message = uploader(file_path, platform_description)
        except Exception as e:
            ok = False
            message = str(e)

        current_logs[key] = message

        if ok:
            logger.info("%s upload success for queue #%s: %s", label, queue_id, message)
            successes.append(label)
        else:
            failure = f"{label} failed: {message}"
            failures.append((label, message))
            logger.error(failure)

            # If this is the test platform in staged mode, mark as failed
            if staged_uploads_enabled and idx == 0 and key == test_platform_key:
                staged_test_failed = True
                logger.warning("Test platform %s failed in staged mode, skipping remaining platforms", label)

            # CONTINUE to other platforms instead of stopping (unless staged test failed)

    # 6. Determine Final Status
    # Count total platforms that should have been attempted
    # This includes platforms that were already successful (counted in successes list)
    total_platforms_to_try = 0
    for key, cfg in platforms.items():
        label = cfg["label"]
        # Skip if this video has specific platforms enabled and this platform is not in the list
        if enabled_platforms_for_video is not None and key not in enabled_platforms_for_video:
            continue
        # Skip if forced platforms set and this platform not in forced list
        # BUT still count already-successful platforms
        if forced_platforms and key not in forced_platforms:
            # Check if this platform already succeeded
            prev_status = current_logs.get(key, "")
            if "success" in str(prev_status).lower() or "uploaded id" in str(prev_status).lower():
                # Already successful, count it AND add to successes if not already there
                total_platforms_to_try += 1
                if label not in successes:
                    successes.append(label)
            # Otherwise skip (not forced and not already successful)
            continue
        if cfg["connected"]():
            total_platforms_to_try += 1

    # Calculate pending queue count (single COUNT query — avoids fetching all rows)
    pending_count = get_pending_count()

    # Determine if this upload should be considered successful
    has_any_success = len(successes) > 0
    all_platforms_succeeded = (len(successes) == total_platforms_to_try and len(failures) == 0)

    if all_platforms_succeeded:
        # Complete success - archive and remove from queue
        archive_uploaded_item(video, current_logs)
        platform_list = ", ".join(successes)
        _notify(
            f"Queue #{queue_id} uploaded successfully to: {platform_list}\n"
            f"Remaining in queue: {pending_count}"
        )
    elif has_any_success and len(failures) > 0:
        # Partial success - some platforms succeeded, some failed
        # KEEP in queue with 'failed' status so user can manually retry failed platforms
        success_list = ", ".join(successes)
        failure_list = ", ".join([f"{label} ({msg[:30]}...)" for label, msg in failures])
        error_msg = f"Partial: {failure_list}"

        update_queue_status(queue_id, "failed", error_msg, current_logs)

        # Pause queue on partial failure to allow user to investigate
        set_config(PAUSE_KEY, 1)

        logger.warning("Partial upload for #%s. Success: %s. Failures: %s", queue_id, success_list, failure_list)
        _notify(
            f"Queue #{queue_id} partially uploaded\n"
            f"✓ Success: {success_list}\n"
            f"✗ Failed: {failure_list}\n"
            f"Queue paused. Use Force button to retry failed platforms. Remaining: {pending_count}"
        )
    else:
        # Complete failure - no platforms succeeded
        # KEEP in queue with 'failed' status for manual retry
        parts = []
        if failures:
            failure_messages = [f"{label}: {msg}" for label, msg in failures]
            parts.append("; ".join(failure_messages))
        if missing_accounts:
            parts.append(f"Awaiting account connections: {', '.join(missing_accounts)}")
        error_msg = "; ".join(parts) if parts else "Awaiting account connections."

        update_queue_status(queue_id, "failed", error_msg, current_logs)
        # Halt the queue after a failure but keep the failed item visible for manual action.
        set_config(PAUSE_KEY, 1)

        logger.error("Upload failed; queue paused for #%s: %s", queue_id, error_msg)
        try:
            import json as _json
            logger.error("failure_detail=%s", _json.dumps({"queue_id": queue_id, "failures": failures, "missing": missing_accounts}))
        except Exception:
            pass

        failure_summary = ", ".join([label for label, _ in failures])
        _notify(
            f"Queue #{queue_id} upload failed on all platforms\n"
            f"Failed: {failure_summary}\n"
            f"Queue paused. Use Force button to retry. Remaining: {pending_count}"
        )


def check_and_post():
    global WORKER_BUSY
    now = _now_with_timezone()
    _maybe_verify_tokens(now)
    warn_tiktok_session_if_needed()

    if WORKER_BUSY:
        logger.debug("Worker is busy, skipping schedule tick.")
        return

    paused = bool(int(get_config(PAUSE_KEY, 0) or 0))
    force = bool(int(get_config(FORCE_KEY, 0) or 0))
    force_platform = (get_config(FORCE_PLATFORM_KEY, "") or "").strip()
    force_queue_id = get_config(FORCE_QUEUE_ID_KEY, "")
    if force_platform and force_platform not in get_platforms():
        force_platform = ""
    if paused and not force:
        logger.debug("Queue paused; skipping tick.")
        return

    WORKER_BUSY = True
    try:
        due = []
        
        # If forcing a specific queue item, fetch only that item
        if force and force_queue_id:
            try:
                force_id = int(force_queue_id)
                from src.database import get_queue_item
                specific_item = get_queue_item(force_id)
                if specific_item:
                    due = [specific_item]
                    logger.info("Force processing specific queue item #%s", force_id)
                else:
                    logger.warning("Force queue item #%s not found", force_id)
            except (ValueError, TypeError):
                logger.warning("Invalid force_queue_id: %s", force_queue_id)
        
        # Normal scheduling: get due items — limit to 1 per tick so a long pause
        # never causes a burst upload of every overdue item at once.
        if not due and not force:
            due = get_due_queue(now.isoformat())[:1]
        
        # If forcing but no specific item found, fall back to earliest pending
        if not due and force and not force_queue_id:
            pending = [row for row in get_queue(limit=200) if row.get("status") in ("pending", "retry")]
            due = pending[:1] if pending else []
        
        # Clear force flags
        if force:
            set_config(FORCE_KEY, 0)
            set_config(FORCE_PLATFORM_KEY, "")
            set_config(FORCE_QUEUE_ID_KEY, "")

        if not due:
            logger.debug("No videos due at %s", now.isoformat())
            return

        for video in due:
            # Check if status is still pending/retry (in case of race conditions)
            if video.get('status') not in ('pending', 'retry'):
                continue

            logger.info("Processing queue item %s.", video["id"])
            platforms_to_run = {force_platform} if force_platform and force else None
            process_video(video, platforms_to_run, is_forced=force)

            # Add a short delay between videos to avoid hammering the platforms.
            # Skip for forced runs — the user is waiting for immediate results.
            if not force:
                time.sleep(random.uniform(5, 15))
        
        # If we just forced an item and there are more pending/retry items, pull the queue forward
        if force:
            _pull_queue_forward(now)
            
    except Exception as e:
        logger.error("Error in check_and_post: %s", e)
    finally:
        WORKER_BUSY = False


def main():
    log_once(logger, "worker_started", "Scheduler worker started.")
    init_db()
    
    reset_stale_tasks()
    
    schedule.every(1).minutes.do(check_and_post)
    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
