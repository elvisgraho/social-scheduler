import json
import os
import time
import random 
from datetime import datetime, time as dtime, timedelta

import pytz
import schedule

from src.database import (
    get_config,
    get_due_queue,
    get_queue,
    increment_attempts,
    init_db,
    set_config,
    update_queue_status,
    reschedule_queue_item,
    archive_uploaded_item,
)
from src.logging_utils import init_logging, log_once
from src.notifier import send_telegram_message
from src.platform_registry import get_platforms
from src.auth_utils import verify_youtube_credentials
from src.platforms import instagram as instagram_platform
from src.platforms import tiktok as tiktok_platform
from src.scheduling import get_schedule, next_daily_slots

logger = init_logging("worker")

WORKER_BUSY = False
PAUSE_KEY = "queue_paused"
FORCE_KEY = "queue_force_run"
FORCE_PLATFORM_KEY = "queue_force_platform"
TOKEN_CHECK_KEY = "last_token_check_date"
TOKEN_CHECK_TIME = dtime(hour=8, minute=0)


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
    """
    ok, msg = verify_youtube_credentials(probe_api=False)
    if ok:
        logger.info("Daily YouTube token verification passed.")
        set_config("last_youtube_ok", now.isoformat())
    else:
        _notify(f"YouTube token check failed: {msg}")

    ig_ok, ig_msg = instagram_platform.verify_login()
    if ig_ok:
        logger.info("Daily Instagram session verification passed.")
        set_config("last_instagram_ok", now.isoformat())
    else:
        _notify(f"Instagram session check failed: {ig_msg}")

    tt_ok, tt_msg = tiktok_platform.verify_session(force=True)
    if tt_ok:
        logger.info("Daily TikTok session verification passed.")
        set_config("last_tiktok_ok", now.isoformat())
    else:
        _notify(f"TikTok session check failed: {tt_msg}")

    set_config(TOKEN_CHECK_KEY, now.date().isoformat())


def _maybe_verify_tokens(now: datetime) -> None:
    """
    Run token checks once per day after the configured morning time.
    """
    try:
        last_run = get_config(TOKEN_CHECK_KEY)
        if last_run == now.date().isoformat():
            return
        if now.time() < TOKEN_CHECK_TIME:
            return
        _run_token_checks(now)
    except Exception as exc:
        logger.warning("Skipping daily token check: %s", exc)


def _pull_queue_forward(now: datetime) -> None:
    """
    When a force run occurs, shift the remaining pending/retry items to the earliest
    available daily slots starting now (preserving one-per-day constraint).
    """
    try:
        pending = [row for row in get_queue(limit=200) if row.get("status") in ("pending", "retry")]
        if not pending:
            return

        slots = next_daily_slots(len(pending), start=now, occupied_dates=set())
        if len(slots) < len(pending):
            logger.warning("Not enough slots to pull queue forward (%s needed, %s available).", len(pending), len(slots))
        for row, slot in zip(pending, slots):
            reschedule_queue_item(row["id"], slot.isoformat())
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
            logger.warning(f"Found {len(stale_tasks)} stale 'processing' tasks on startup. Resetting to 'pending'.")
            for task in stale_tasks:
                # Keep logs, just reset status so it tries again
                update_queue_status(task["id"], "pending", None, task.get("platform_logs"))
    except Exception as e:
        logger.error(f"Failed to reset stale tasks: {e}")


def process_video(video: dict, forced_platforms: set[str] | None = None) -> None:
    queue_id = video["id"]
    file_path = video["file_path"]
    forced_platforms = set(forced_platforms or [])

    paused = bool(int(get_config(PAUSE_KEY, 0) or 0))
    if paused:
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

        # Add Jitter (wait 10-30 seconds between platforms to avoid bot detection)
        time.sleep(random.uniform(10, 30))

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
    total_platforms_to_try = 0
    for key, cfg in platforms.items():
        if enabled_platforms_for_video is not None and key not in enabled_platforms_for_video:
            continue
        if cfg["connected"]():
            total_platforms_to_try += 1

    # Calculate pending queue count
    pending_count = len([r for r in get_queue(limit=500) if r.get("status") in ("pending", "retry")])

    # Determine if this upload should be considered successful
    has_any_success = len(successes) > 0
    all_platforms_succeeded = (len(successes) == total_platforms_to_try and len(failures) == 0)

    if all_platforms_succeeded:
        # Complete success
        archive_uploaded_item(video, current_logs)
        platform_list = ", ".join(successes)
        _notify(
            f"Queue #{queue_id} uploaded successfully to: {platform_list}\n"
            f"Remaining in queue: {pending_count}"
        )
    elif has_any_success and len(failures) > 0:
        # Partial success - some platforms succeeded, some failed
        archive_uploaded_item(video, current_logs)
        success_list = ", ".join(successes)
        failure_list = ", ".join([f"{label} ({msg[:30]}...)" for label, msg in failures])

        # Pause queue on partial failure to allow user to investigate
        set_config(PAUSE_KEY, 1)

        logger.warning("Partial upload for #%s. Success: %s. Failures: %s", queue_id, success_list, failure_list)
        _notify(
            f"Queue #{queue_id} partially uploaded\n"
            f"✓ Success: {success_list}\n"
            f"✗ Failed: {failure_list}\n"
            f"Queue paused. Remaining: {pending_count}"
        )
    else:
        # Complete failure - no platforms succeeded
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
            f"Queue paused. Remaining: {pending_count}"
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
    if force_platform and force_platform not in get_platforms():
        force_platform = ""
    if paused and not force:
        logger.debug("Queue paused; skipping tick.")
        return

    WORKER_BUSY = True
    try:
        due = get_due_queue(now.isoformat())
        if not due and force:
            # If forcing and nothing is strictly due, pick the earliest pending/retry
            pending = [row for row in get_queue(limit=200) if row.get("status") in ("pending", "retry")]
            due = pending[:1] if pending else []
        if force:
            set_config(FORCE_KEY, 0)
            set_config(FORCE_PLATFORM_KEY, "")

        if not due:
            logger.debug("No videos due at %s", now.isoformat())
            return

        for video in due:
            # Check if status is still pending (in case of race conditions if multiple workers exist)
            if video.get('status') not in ('pending', 'retry'):
                continue

            logger.info("Processing queue item %s.", video["id"])
            platforms_to_run = {force_platform} if force_platform else None
            process_video(video, platforms_to_run)
            
            # Add delay between different videos too
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
