import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Tuple
from zoneinfo import ZoneInfo

import imageio_ffmpeg
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    LoginRequired,
    TwoFactorRequired,
)
from pydantic import ValidationError

from src.logging_utils import init_logging
from src.database import get_config, set_account_state, set_config
from src.upload_outcomes import NEEDS_REVIEW

SESSION_KEY = "insta_session"
SESSION_ID_KEY = "insta_sessionid"

logger = init_logging("instagram")

def _credentials() -> Tuple[str, str]:
    return get_config("insta_user"), get_config("insta_pass")

def _format_error(exc: Exception) -> str:
    try:
        status = getattr(exc, "response", None)
        if status is not None:
            code = getattr(status, "status_code", None)
            text = getattr(status, "text", None)
            if code or text:
                details = f"HTTP {code}" if code else "HTTP error"
                if text:
                    details = f"{details}: {text[:200]}"
                return details
    except Exception:
        pass
    return str(exc)

def _extract_sessionid(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if "sessionid=" in raw and ";" in raw:
        for part in raw.split(";"):
            if "sessionid=" in part:
                return part.split("sessionid=")[-1].strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        if isinstance(data, dict):
            if "sessionid" in data:
                return str(data["sessionid"])
            cookies = data.get("cookies")
            if isinstance(cookies, list):
                for cookie in cookies:
                    if cookie.get("name") == "sessionid":
                        return str(cookie.get("value", "")).strip()
    return raw

def _apply_german_settings(cl: Client) -> None:
    """Keep the configured locale and use instagrapi's supported app profile."""
    cl.set_country("DE")
    cl.set_country_code(49)
    cl.set_locale("de_DE")
    offset = datetime.now(ZoneInfo("Europe/Berlin")).utcoffset()
    cl.set_timezone_offset(int(offset.total_seconds()), timezone_name="Europe/Berlin")
    cl.set_user_agent()

def _load_settings(cl: Client) -> bool:
    """Load saved session settings including device UUIDs."""
    session_data = get_config(SESSION_KEY)
    if session_data:
        try:
            settings = json.loads(session_data)
            cl.set_settings(settings)
            # Overwrite locale settings to ensure they match current location (Germany)
            _apply_german_settings(cl)
            logger.debug("Loaded saved Instagram session settings")
            return True
        except Exception:
            logger.warning("Failed to load stored Instagram session settings.")
    return False

def _initialize_client() -> Client:
    """Initialize client with proper delays and location settings."""
    # Refresh app version/Bloks hash while retaining restored hardware and UUIDs.
    cl = Client(override_app_version=True)
    # Delay between requests (1-3 seconds as recommended by instagrapi docs)
    cl.delay_range = [1, 3]
    _apply_german_settings(cl)
    return cl

def _store_settings(cl: Client) -> None:
    """Save full session settings including device UUIDs - critical for avoiding re-login issues."""
    try:
        set_config(SESSION_KEY, json.dumps(cl.get_settings()))
        if getattr(cl, "sessionid", None):
            set_config(SESSION_ID_KEY, cl.sessionid)
        logger.debug("Saved Instagram session settings")
    except Exception:
        logger.warning("Could not persist Instagram settings/session.")

def _login(cl: Client, relogin: bool = False) -> Tuple[bool, str]:
    """
    Login to Instagram, reusing session when possible.

    Key anti-detection: When relogin=True, we preserve device UUIDs from the
    previous session so Instagram sees the same "device" logging in again.
    """
    username, password = _credentials()

    # If we have saved settings loaded, try to use existing session first
    if not relogin:
        sessionid = _extract_sessionid(get_config(SESSION_ID_KEY, ""))
        if sessionid:
            try:
                cl.login_by_sessionid(sessionid)
                _store_settings(cl)
                set_account_state("instagram", True, None)
                return True, "Session login successful."
            except KeyError as exc:
                if 'pinned_channels_info' in str(exc) or 'threads' in str(exc):
                    logger.debug("Instagram response parsing issue (non-critical): %s", exc)
                    try:
                        cl.account_info()
                        _store_settings(cl)
                        set_account_state("instagram", True, None)
                        logger.info("Session login successful (despite parsing warning)")
                        return True, "Session login successful."
                    except Exception:
                        logger.warning("Session verification failed, will try password login")
                else:
                    logger.warning("Instagram sessionid login failed: %s", exc)
            except Exception as exc:
                logger.warning("Instagram sessionid login failed: %s", exc)

    if not username or not password:
        msg = "Instagram credentials missing."
        set_account_state("instagram", False, msg)
        return False, msg

    try:
        # IMPORTANT: When re-logging in, preserve the device UUIDs from settings
        # This makes Instagram think it's the same device, not a new one
        cl.login(username, password, relogin=relogin)
        _store_settings(cl)
        set_account_state("instagram", True, None)
        return True, f"Login successful for @{username}"
    except (ChallengeRequired, TwoFactorRequired):
        msg = "Instagram challenge/2FA required. Approve on your device, then retry."
        set_account_state("instagram", False, msg)
        return False, msg
    except Exception as exc:
        err_str = _format_error(exc)
        set_account_state("instagram", False, err_str)
        return False, err_str

def _upload_reel(client: Client, video_path: str, caption: str):
    """Supply an FFmpeg thumbnail so standard MP4 uploads do not need MoviePy."""
    video = Path(video_path).resolve()
    ffmpeg = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="scheduler-reel-") as directory:
        thumbnail = Path(directory) / "thumbnail.jpg"
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(video), "-frames:v", "1", str(thumbnail)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if result.returncode or not thumbnail.is_file():
            raise RuntimeError(f"Could not generate Reel thumbnail: {result.stderr[-300:]}")
        return client.clip_upload(
            video, caption=(caption or "")[:2200], thumbnail=thumbnail,
            show_preview_in_feed=False,
        )


def upload(video_path: str, caption: str) -> Tuple[bool, str]:
    cl = _initialize_client()
    _load_settings(cl)
    ok, msg = _login(cl)
    if not ok:
        return False, msg

    for attempt in range(2):
        try:
            media = _upload_reel(cl, video_path, caption)
            if getattr(media, "product_type", "").lower() != "clips" or not getattr(media, "pk", None):
                return False, f"{NEEDS_REVIEW} Instagram did not return a confirmed Reel ID. Check the account before retrying."
            _store_settings(cl)
            set_account_state("instagram", True, None)
            return True, f"Uploaded PK: {media.pk}"
        except LoginRequired:
            # Only an explicit expired-login error permits a second upload attempt.
            if attempt:
                msg = "Instagram login expired again. Reconnect the account before retrying."
                set_account_state("instagram", False, msg)
                return False, msg
            cl = _initialize_client()
            _load_settings(cl)
            ok, msg = _login(cl, relogin=True)
            if not ok:
                return False, msg
        except (ChallengeRequired, TwoFactorRequired):
            msg = "Instagram challenge/2FA required. Approve on your device, then retry."
            set_account_state("instagram", False, msg)
            return False, msg
        except ValidationError:
            # Response parsing cannot establish whether Instagram published the Reel.
            logger.warning("Instagram returned an unreadable upload response; manual review required.")
            return False, f"{NEEDS_REVIEW} Instagram returned an unreadable upload response. Check the account before retrying."
        except Exception as exc:
            # A generic error is not evidence of an expired session. Do not re-upload.
            msg = _format_error(exc)
            logger.error("Instagram upload stopped: %s", msg)
            return False, msg
    raise AssertionError("Unreachable upload retry state")


def verify_login() -> Tuple[bool, str]:
    cl = _initialize_client()
    _load_settings(cl)
    ok, msg = _login(cl)
    if ok:
        logger.info("Instagram session verified.")
        return True, msg
    logger.warning("Instagram verification failed: %s", msg)
    return False, msg

def save_sessionid(raw: str) -> Tuple[bool, str]:
    sessionid = _extract_sessionid(raw)
    if not sessionid:
        set_config(SESSION_ID_KEY, "")
        set_account_state("instagram", False, "Session cleared.")
        return False, "No sessionid detected."
    set_config(SESSION_ID_KEY, sessionid)
    set_account_state("instagram", True, None)
    logger.info("Instagram sessionid stored (len=%s).", len(sessionid))
    return True, "Instagram session stored. Use Verify to confirm."

def session_connected() -> bool:
    return bool(get_config(SESSION_ID_KEY, "") or get_config(SESSION_KEY, ""))
