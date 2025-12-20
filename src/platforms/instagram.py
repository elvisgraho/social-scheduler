import json
from typing import Tuple
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired
from src.logging_utils import init_logging
from src.database import get_config, set_account_state, set_config

SESSION_KEY = "insta_session"
logger = init_logging("instagram")

def _credentials() -> Tuple[str, str]:
    return get_config("insta_user"), get_config("insta_pass")

def upload(video_path: str, caption: str):
    username, password = _credentials()
    if not username or not password:
        msg = "Instagram credentials missing."
        set_account_state("instagram", False, msg)
        return False, msg

    cl = Client()
    cl.delay_range = [1, 3]
    
    session_data = get_config(SESSION_KEY)

    try:
        if session_data:
            try:
                cl.set_settings(json.loads(session_data))
            except Exception:
                logger.warning("Failed to load stored Instagram session.")

        # cl.login() automatically validates the session if settings are loaded.
        # It only performs a full login if the session is invalid.
        cl.login(username, password)
        
        # Fix: Save session IMMEDIATELY after login/validation, before upload
        # This prevents losing a valid session if the video upload itself fails.
        set_config(SESSION_KEY, json.dumps(cl.get_settings()))
        set_account_state("instagram", True, None)

        media = cl.clip_upload(video_path, caption=(caption or "")[:2200])
        return True, f"Uploaded PK: {media.pk}"

    except ChallengeRequired:
        msg = "Instagram 2FA/Challenge required. Log in manually on a phone."
        set_account_state("instagram", False, msg)
        return False, msg
    except Exception as exc:
        err_str = str(exc)
        if "ffmpeg" in err_str.lower() or "No such file" in err_str:
            err_str += " (Ensure FFMPEG is installed on the system)"
            
        set_account_state("instagram", False, err_str)
        return False, err_str


def verify_login() -> Tuple[bool, str]:
    """
    Lightweight validation used by the UI to confirm credentials/session are still good.
    """
    username, password = _credentials()
    if not username or not password:
        msg = "Instagram credentials missing."
        set_account_state("instagram", False, msg)
        return False, msg

    cl = Client()
    session_data = get_config(SESSION_KEY)
    if session_data:
        try:
            cl.set_settings(json.loads(session_data))
        except Exception:
            logger.warning("Failed to load stored Instagram session.")

    try:
        cl.login(username, password)
        set_config(SESSION_KEY, json.dumps(cl.get_settings()))
        set_account_state("instagram", True, None)
        return True, f"Session valid for @{username}"
    except ChallengeRequired:
        msg = "Instagram 2FA/Challenge required. Log in manually on a phone."
        set_account_state("instagram", False, msg)
        return False, msg
    except Exception as exc:
        set_account_state("instagram", False, str(exc))
        return False, str(exc)
