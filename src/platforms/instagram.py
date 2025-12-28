import json
from typing import Tuple, Dict, Any
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired, 
    LoginRequired, 
    TwoFactorRequired, 
    ClientError
)
from src.logging_utils import init_logging
from src.database import get_config, set_account_state, set_config, set_json_config

SESSION_KEY = "insta_session"
SESSION_ID_KEY = "insta_sessionid"
logger = init_logging("instagram")

def _credentials() -> Tuple[str, str]:
    return get_config("insta_user"), get_config("insta_pass")

def _format_error(exc: Exception) -> str:
    # Safe error formatting to extract HTTP details if available
    try:
        status = getattr(exc, "response", None)
        if status is not None:
            code = getattr(status, "status_code", None)
            text = getattr(status, "text", None)
            if code or text:
                details = f"HTTP {code}" if code else "HTTP error"
                if text:
                    # Truncate long HTML/JSON responses in logs
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

def _load_settings(cl: Client) -> bool:
    """Load settings and return True if session was loaded."""
    session_data = get_config(SESSION_KEY)
    if session_data:
        try:
            cl.set_settings(json.loads(session_data))
            return True
        except Exception:
            logger.warning("Failed to load stored Instagram session settings.")
    return False

def _store_settings(cl: Client) -> None:
    try:
        set_config(SESSION_KEY, json.dumps(cl.get_settings()))
        if getattr(cl, "sessionid", None):
            set_config(SESSION_ID_KEY, cl.sessionid)
    except Exception:
        logger.warning("Could not persist Instagram settings/session.")

def _login(cl: Client) -> Tuple[bool, str]:
    """
    Attempts to ensure the client is authenticated.
    Priority:
    1. Existing session ID (if not expired)
    2. Username/Password login
    """
    username, password = _credentials()
    sessionid = _extract_sessionid(get_config(SESSION_ID_KEY, ""))
    
    # 1. Try Session ID first (stateless check, relying on library validation during action)
    if sessionid:
        try:
            cl.login_by_sessionid(sessionid)
            _store_settings(cl)
            set_account_state("instagram", True, None)
            return True, "Session login successful."
        except Exception as exc:
            logger.warning("Instagram sessionid login failed: %s", exc)

    # 2. Fallback to Credentials
    if not username or not password:
        msg = "Instagram credentials missing."
        set_account_state("instagram", False, msg)
        return False, msg

    try:
        cl.login(username, password)
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

def upload(video_path: str, caption: str):
    cl = Client()
    cl.delay_range = [1, 3]
    
    # Attempt to load settings/session first
    using_session = _load_settings(cl)
    
    # Ensure we are logged in (via session or creds)
    ok, msg = _login(cl)
    if not ok:
        return False, msg

    def attempt_upload(client):
        return client.clip_upload(
            video_path,
            caption=(caption or "")[:2200],
            extra_data={"share_to_feed": 0},
        )

    try:
        # First Attempt
        media = attempt_upload(cl)
        
        if getattr(media, "product_type", "").lower() != "clips":
            err = "Upload completed but returned non-Reel media."
            set_account_state("instagram", False, err)
            return False, err

        _store_settings(cl)
        return True, f"Uploaded PK: {media.pk}"

    except Exception as exc:
        # If the first attempt fails, we check if we should retry.
        # We retry if we were using a cached session (which might be stale)
        # OR if the error specifically indicates login requirements (even via HTTP 200).
        
        err_str = _format_error(exc)
        
        # Check for keywords that imply auth failure, or that "HTTP 200" weirdness
        is_auth_error = any(x in err_str.lower() for x in ["login", "challenge", "unauthorized", "http 200"])
        
        if using_session or is_auth_error:
            logger.warning(f"Instagram upload failed with potential session issue ({err_str}). Retrying with fresh login...")
            
            try:
                username, password = _credentials()
                if not username or not password:
                    raise Exception("No credentials for retry.")
                
                # Create FRESH client (clear settings)
                cl = Client()
                cl.login(username, password)
                _store_settings(cl)
                
                # Retry Upload
                media = attempt_upload(cl)
                set_account_state("instagram", True, None)
                return True, f"Uploaded PK: {media.pk} (Retry)"
            
            except Exception as retry_exc:
                final_err = f"Retry failed: {_format_error(retry_exc)}"
                set_account_state("instagram", False, final_err)
                return False, final_err
        
        # If it wasn't an auth error (e.g. file not found, API down), fail immediately.
        if "ffmpeg" in err_str.lower() or "no such file" in err_str.lower():
            err_str += " (Ensure FFMPEG is installed)"
            
        set_account_state("instagram", False, err_str)
        return False, err_str

def verify_login() -> Tuple[bool, str]:
    cl = Client()
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