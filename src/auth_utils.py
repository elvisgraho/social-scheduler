import json
import os
from typing import Optional, Tuple

import logging

from google_auth_oauthlib.flow import Flow

from src.database import get_config, set_account_state, set_config
from src.logging_utils import init_logging

CLIENT_SECRETS_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
YOUTUBE_KEY = "youtube_credentials"
LEGACY_KEYS = ["youtube_token"]
GOOGLE_CLIENT_CONFIG_KEY = "google_oauth_client"
logger = init_logging("youtube.auth")


def _load_client_config() -> dict:
    raw = get_config(GOOGLE_CLIENT_CONFIG_KEY)
    if raw:
        return json.loads(raw)
    if os.path.exists(CLIENT_SECRETS_FILE):
        with open(CLIENT_SECRETS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    raise FileNotFoundError(
        "Google OAuth client is missing. Paste the OAuth client JSON in the Accounts tab."
    )


def _build_flow() -> Flow:
    config = _load_client_config()
    return Flow.from_client_config(
        config,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


def get_google_auth_url() -> Tuple[Optional[str], Optional[str]]:
    try:
        flow = _build_flow()
    except FileNotFoundError as exc:
        return None, str(exc)
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", include_granted_scopes="true")
    return auth_url, None


def finish_google_auth(auth_code: str) -> Tuple[bool, str]:
    try:
        flow = _build_flow()
        flow.fetch_token(code=auth_code)
        creds = flow.credentials
        set_config(YOUTUBE_KEY, creds.to_json())
        set_account_state("youtube", True, None)
        return True, "YouTube channel linked."
    except Exception as exc:
        set_account_state("youtube", False, str(exc))
        return False, str(exc)


def get_youtube_credentials() -> Optional[str]:
    creds = get_config(YOUTUBE_KEY)
    if creds:
        return creds
    # legacy key migration
    for legacy_key in LEGACY_KEYS:
        legacy_value = get_config(legacy_key)
        if legacy_value:
            set_config(YOUTUBE_KEY, legacy_value)
            return legacy_value
    return None


def youtube_connected() -> bool:
    return bool(get_youtube_credentials())


def verify_youtube_credentials() -> tuple[bool, str]:
    """
    Attempt to refresh/validate the stored YouTube credentials.
    """
    token_json = get_youtube_credentials()
    if not token_json:
        msg = "YouTube account not linked."
        set_account_state("youtube", False, msg)
        return False, msg
    try:
        info = json.loads(token_json)
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_info(info)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            set_config(YOUTUBE_KEY, creds.to_json())
        set_account_state("youtube", True, None)
        logger.info("YouTube credentials verified/refreshed successfully.")
        return True, "YouTube token is valid."
    except Exception as exc:
        msg = f"Credential error: {exc}"
        set_account_state("youtube", False, msg)
        logger.warning("YouTube credential verification failed: %s", msg)
        return False, msg


def save_google_client_config(raw_json: str) -> Tuple[bool, str]:
    try:
        data = json.loads(raw_json)
        if "installed" not in data and "web" not in data:
            return False, "Invalid OAuth client JSON."
        set_config(GOOGLE_CLIENT_CONFIG_KEY, json.dumps(data))
        return True, "Google OAuth client saved."
    except json.JSONDecodeError as exc:
        return False, f"JSON error: {exc}"


def get_google_client_config(pretty: bool = False) -> str:
    data = get_config(GOOGLE_CLIENT_CONFIG_KEY)
    if not data and os.path.exists(CLIENT_SECRETS_FILE):
        with open(CLIENT_SECRETS_FILE, "r", encoding="utf-8") as fh:
            data = fh.read()
    if not data:
        return ""
    if not pretty:
        return data
    try:
        return json.dumps(json.loads(data), indent=2)
    except Exception:
        return data


def has_google_client_config() -> bool:
    if get_config(GOOGLE_CLIENT_CONFIG_KEY):
        return True
    return os.path.exists(CLIENT_SECRETS_FILE)
