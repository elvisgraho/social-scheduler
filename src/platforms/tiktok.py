"""TikTok session management and uploads through Playwright-managed Chromium."""
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import requests
from playwright.sync_api import sync_playwright

from src.database import get_config, get_json_config, set_account_state, set_config, set_json_config
from src.logging_utils import init_logging
from src.tiktok_browser import TikTokUpload
from src.upload_outcomes import PUBLISH_PENDING

logger = init_logging("tiktok")
SESSION_KEY = "tiktok_session_bundle"
LEGACY_KEY = "tiktok_session_id"
VERIFICATION_INTERVAL_HOURS = 6
REFRESH_WARNING_DAYS = 25
# Preserve the existing HTTP account probe; the browser uses its native user agent.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
SUPPORTED_VIDEO_FORMATS = {'.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m4v'}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO string to timezone-aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        # If naive datetime, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def _session_bundle() -> Dict:
    data = get_json_config(SESSION_KEY, {})
    if not data:
        legacy = get_config(LEGACY_KEY)
        if legacy:
            data = {"sessionid": legacy, "stored_at": _utcnow().isoformat(), "valid": False, "last_verified": None}
            set_json_config(SESSION_KEY, data)
    return data or {}

def _persist_bundle(bundle: Dict) -> None:
    set_json_config(SESSION_KEY, bundle)
    if bundle.get("sessionid"):
        set_config(LEGACY_KEY, bundle["sessionid"])
    else:
        set_config(LEGACY_KEY, "")

def save_session(session_id: str) -> None:
    cleaned = session_id.strip()
    if not cleaned:
        bundle = {}
        _persist_bundle(bundle)
        set_account_state("tiktok", False, "Session missing")
        set_config("tiktok_refresh_warned", "")
        return
    bundle = _session_bundle()
    bundle.update({"sessionid": cleaned, "stored_at": _utcnow().isoformat(), "valid": False, "last_verified": None, "account_name": None})
    _persist_bundle(bundle)
    set_config("tiktok_refresh_warned", "")
    set_account_state("tiktok", bool(cleaned), None)
    verify_session(force=True)

def _session_age_days(bundle: Dict) -> Optional[int]:
    stored = bundle.get("stored_at")
    stored_dt = _parse_iso(stored)
    if not stored_dt: return None
    return (_utcnow() - stored_dt).days

def session_status() -> Dict:
    bundle = _session_bundle()
    age = _session_age_days(bundle)
    return {
        "sessionid": bundle.get("sessionid"),
        "valid": bundle.get("valid", False),
        "last_verified": bundle.get("last_verified"),
        "account_name": bundle.get("account_name"),
        "stored_at": bundle.get("stored_at"),
        "needs_refresh": (age is not None and age >= REFRESH_WARNING_DAYS),
        "age_days": age,
        "message": bundle.get("last_error"),
    }

def session_connected() -> bool:
    status = session_status()
    return bool(status["sessionid"]) and status["valid"]

def _probe_session(session_id: str) -> Tuple[bool, str, Optional[str]]:
    url = "https://www.tiktok.com/passport/web/account/info/?aid=1459"
    headers = {"User-Agent": USER_AGENT, "Cookie": f"sessionid={session_id};"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        username = data.get("data", {}).get("username") or data.get("data", {}).get("unique_id")
        is_valid = (data.get("data", {}).get("login_status") == 0 or bool(username))
        
        if is_valid: return True, f"Valid: @{username}", username
        return False, f"Invalid: {data.get('message') or 'Session expired'}", None
    except Exception as exc:
        return False, str(exc), None

def ensure_session_valid(force: bool = False, local_session: str = None) -> Tuple[bool, Optional[str], str]:
    if local_session:
        return True, local_session, "Local Session Provided"

    bundle = _session_bundle()
    session_id = bundle.get("sessionid")
    if not session_id: return False, None, "No session."
    
    last = _parse_iso(bundle.get("last_verified"))
    if not force and bundle.get("valid") and last and _utcnow() - last < timedelta(hours=VERIFICATION_INTERVAL_HOURS):
        return True, session_id, "Valid (Cached)"

    ok, msg, user = _probe_session(session_id)
    bundle["valid"] = ok
    bundle["last_verified"] = _utcnow().isoformat()
    if user: bundle["account_name"] = user
    if not ok: bundle["last_error"] = msg
    _persist_bundle(bundle)
    set_account_state("tiktok", ok, msg if not ok else None)
    return ok, session_id, msg

def verify_session(force: bool = True) -> Tuple[bool, str]:
    ok, _, message = ensure_session_valid(force=force)
    return ok, message

def _validate_video_file(video_path: str) -> Tuple[bool, str]:
    """Validates that the file exists and is a supported video format."""
    path = Path(video_path)

    # Check file exists
    if not path.exists():
        return False, f"File not found: {video_path}"

    # Check it's a file, not directory
    if not path.is_file():
        return False, f"Path is not a file: {video_path}"

    # Check file extension
    if path.suffix.lower() not in SUPPORTED_VIDEO_FORMATS:
        return False, f"Unsupported format: {path.suffix}. Supported: {', '.join(SUPPORTED_VIDEO_FORMATS)}"

    # Check MIME type
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type and not mime_type.startswith('video/'):
        return False, f"File is not a video (detected: {mime_type})"

    # Check file size (TikTok limit: ~4GB)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > 4000:
        return False, f"File too large: {size_mb:.1f}MB (max 4000MB)"
    if size_mb < 0.1:
        return False, f"File too small: {size_mb:.2f}MB (likely corrupt)"

    return True, "Valid video file"

def upload(video_path: str, description: str, local_session_key: str = None,
           before_publish: Optional[Callable[[], None]] = None) -> Tuple[bool, str]:
    """Upload once; ambiguous submissions require manual review before retrying."""
    valid, message = _validate_video_file(video_path)
    if not valid:
        return False, message
    ok, session_id, info = ensure_session_valid(local_session=local_session_key)
    if not ok or not session_id:
        return False, info

    headless = os.getenv("TIKTOK_HEADLESS", "true").lower() not in {"0", "false", "no"}
    browser = None
    context = None
    flow = None
    with sync_playwright() as playwright:
        try:
            # Uses Playwright's matching browser, no Chrome install or WebDriver.
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="en-US")
            context.add_cookies([{
                "name": "sessionid", "value": session_id, "domain": ".tiktok.com",
                "path": "/", "secure": True, "httpOnly": True,
            }])
            page = context.new_page()
            page.goto("https://www.tiktok.com/tiktokstudio/upload?lang=en", wait_until="domcontentloaded")
            flow = TikTokUpload(page)
            flow.prepare(video_path, description)
            flow.publish(before_publish or (lambda: None))
            set_account_state("tiktok", True, None)
            return True, "Upload Successful (TikTok confirmed publication)"
        except Exception as exc:
            logger.exception("TikTok upload stopped")
            if context and context.pages:
                try:
                    path = Path("data/logs") / f"tiktok_failure_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
                    context.pages[0].screenshot(path=str(path))
                    logger.error("TikTok screenshot saved to %s", path)
                except Exception:
                    logger.warning("Could not capture TikTok screenshot")
            if flow and flow.publish_started:
                # The worker already persisted PUBLISH_PENDING before the click.
                return False, PUBLISH_PENDING
            return False, f"TikTok upload failed before publishing: {exc}"
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    logger.warning("Could not close TikTok browser cleanly")
