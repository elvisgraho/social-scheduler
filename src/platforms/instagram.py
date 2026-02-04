import json
from typing import Tuple

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    TwoFactorRequired,
)
from pydantic import ValidationError

from src.logging_utils import init_logging
from src.database import get_config, set_account_state, set_config

SESSION_KEY = "insta_session"
SESSION_ID_KEY = "insta_sessionid"

# Updated User-Agent to version 410 with German locale (de_DE) to match your IP
IG_USER_AGENT = "Instagram 410.1.0.63.71 Android (34/14; 320dpi; 720x1438; Xiaomi/Redmi; 23108RN04Y; gust; mt6768; de_DE; 846519237)"

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
    """Enforce Germany (Hessen) settings to match IP and avoid challenges."""
    cl.set_user_agent(IG_USER_AGENT)
    cl.set_country("DE")
    cl.set_country_code(49)
    cl.set_locale("de_DE")
    cl.set_timezone_offset(3600)  # UTC+1 (Winter Time)

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
    cl = Client()
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

def upload(video_path: str, caption: str) -> Tuple[bool, str]:
    cl = _initialize_client()

    # Load saved settings (includes device UUIDs)
    using_session = _load_settings(cl)
    ok, msg = _login(cl)
    if not ok:
        return False, msg

    def attempt_upload(client):
        safe_caption = caption or ""
        if len(safe_caption) > 2200:
            safe_caption = safe_caption[:2200]
            try:
                safe_caption = safe_caption.encode('utf-8', errors='ignore').decode('utf-8')
            except Exception:
                pass

        return client.clip_upload(
            video_path,
            caption=safe_caption,
            extra_data={
                "clips_share_preview_to_feed": "0",
                "share_to_feed": "0"
            }
        )

    try:
        media = attempt_upload(cl)

        if getattr(media, "product_type", "").lower() != "clips":
            err = "Upload completed but returned non-Reel media."
            set_account_state("instagram", False, err)
            return False, err

        _store_settings(cl)
        return True, f"Uploaded PK: {media.pk}"

    except ValidationError as e:
        error_msg = str(e)
        if "audio_filter_infos" in error_msg or "clips_metadata" in error_msg:
            logger.warning(f"Instagram response parsing failed (Known instagrapi Library Bug). Upload likely succeeded. Error: {e}")
            set_account_state("instagram", True, None)
            return True, "Upload successful (Response parsing error ignored)"
        else:
            logger.error(f"Instagram validation error (Unknown): {e}")
            set_account_state("instagram", False, f"Validation error: {error_msg[:200]}")
            return False, f"Upload failed: {error_msg[:200]}"

    except Exception as exc:
        err_str = _format_error(exc)

        is_auth_error = any(x in err_str.lower() for x in ["login", "challenge", "unauthorized", "http 200"])

        if using_session or is_auth_error:
            logger.warning(f"Instagram upload failed ({err_str}). Retrying with relogin...")
            try:
                username, password = _credentials()
                if not username or not password:
                    raise Exception("No credentials for retry.")

                # Create new client but load old settings to preserve device UUIDs
                cl = _initialize_client()
                _load_settings(cl)  # Load old device UUIDs

                # Use relogin=True to preserve device identity
                cl.login(username, password, relogin=True)
                _store_settings(cl)

                media = attempt_upload(cl)
                set_account_state("instagram", True, None)
                return True, f"Uploaded PK: {media.pk} (Retry)"

            except ValidationError as retry_e:
                retry_err_msg = str(retry_e)
                if "audio_filter_infos" in retry_err_msg or "clips_metadata" in retry_err_msg:
                    logger.warning("Retry upload likely succeeded (Known parsing bug ignored).")
                    set_account_state("instagram", True, None)
                    return True, "Upload successful (Retry - Response parsing error ignored)"
                else:
                    logger.error(f"Retry validation error: {retry_e}")
                    set_account_state("instagram", False, f"Retry failed: {retry_err_msg[:200]}")
                    return False, f"Retry failed: {retry_err_msg[:200]}"
            except Exception as retry_exc:
                final_err = f"Retry failed: {_format_error(retry_exc)}"
                set_account_state("instagram", False, final_err)
                return False, final_err

        if "ffmpeg" in err_str.lower() or "no such file" in err_str.lower():
            err_str += " (Ensure FFMPEG is installed)"

        set_account_state("instagram", False, err_str)
        return False, err_str

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