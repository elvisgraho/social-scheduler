import json
import os
import logging
from typing import Optional, Tuple

from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.database import get_config, set_account_state, set_config
from src.logging_utils import init_logging

CLIENT_SECRETS_FILE = "client_secret.json"

# UPDATED SCOPES: 
# 'youtube.upload' is for uploading videos.
# 'youtube.readonly' is required for the 'verify' button to check channel status without 403 errors.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly"
]

# Require loopback redirect for desktop apps; allow override via env/config.
DEFAULT_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:8080")
REDIRECT_URI_KEY = "google_redirect_uri"
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
    redirect_uri = get_config(REDIRECT_URI_KEY, DEFAULT_REDIRECT_URI) or DEFAULT_REDIRECT_URI
    return Flow.from_client_config(
        config,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def get_google_auth_url() -> Tuple[Optional[str], Optional[str]]:
    try:
        flow = _build_flow()
    except FileNotFoundError as exc:
        return None, str(exc)
    
    # Generate URL
    auth_url, _ = flow.authorization_url(
        prompt="consent",
        access_type="offline",
        include_granted_scopes="true",
    )
    return auth_url, None


def finish_google_auth(auth_code: str) -> Tuple[bool, str]:
    try:
        flow = _build_flow()
        flow.fetch_token(code=auth_code)
        creds = flow.credentials
        
        # Save credentials to DB
        set_config(YOUTUBE_KEY, creds.to_json())
        set_account_state("youtube", True, None)
        return True, "YouTube channel linked successfully."
    except Exception as exc:
        msg = str(exc)
        logger.error(f"Google Auth Finish Error: {msg}")
        set_account_state("youtube", False, msg)
        return False, f"Auth failed: {msg}"


def get_youtube_credentials() -> Optional[str]:
    creds = get_config(YOUTUBE_KEY)
    if creds:
        return creds
    # Legacy key migration
    for legacy_key in LEGACY_KEYS:
        legacy_value = get_config(legacy_key)
        if legacy_value:
            set_config(YOUTUBE_KEY, legacy_value)
            return legacy_value
    return None


def youtube_connected() -> bool:
    return bool(get_youtube_credentials())


def describe_youtube_http_error(err: HttpError) -> str:
    """
    Convert a YouTube HttpError into a user-friendly message with hints.
    """
    status = getattr(err, "resp", None)
    status_code = getattr(status, "status", None)
    
    # Try to decode content
    raw = ""
    try:
        raw = err.content.decode() if isinstance(err.content, (bytes, bytearray)) else str(err.content)
    except Exception:
        raw = str(err)

    message = getattr(err, "reason", "") or raw
    
    # Try parsing JSON error response
    try:
        payload = json.loads(raw or "{}")
        message = payload.get("error", {}).get("message", message)
    except Exception:
        pass

    # Add Hints based on error text
    lower = (message or "").lower()
    hint = ""
    if "has not been used in project" in lower or "accessnotconfigured" in lower:
        hint = "Enable YouTube Data API v3 for the selected Google Cloud project in Google Cloud Console."
    elif "quota" in lower:
        hint = "Quota exceeded; wait for reset or request higher quota."
    elif "forbidden" in lower and "permission" in lower:
        hint = "Check that the OAuth client belongs to a project with YouTube Data API enabled."
    elif "upload" in lower and "scope" in lower:
        hint = "Missing upload permissions. Re-link account in Accounts tab."

    base = f"API Error {status_code}: {message}" if status_code else f"API Error: {message}"
    return f"{base} ({hint})" if hint else base


def verify_youtube_credentials(probe_api: bool = False) -> tuple[bool, str]:
    """
    Attempt to refresh/validate the stored YouTube credentials.
    If probe_api is True, performs a lightweight public call to confirm API availability.
    """
    token_json = get_youtube_credentials()
    if not token_json:
        msg = "YouTube account not linked."
        set_account_state("youtube", False, msg)
        return False, msg

    try:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info)
        
        # 1. Refresh Token if needed
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                set_config(YOUTUBE_KEY, creds.to_json())
            except Exception as refresh_err:
                # If refresh fails (revoked/expired), wipe credentials to force re-login
                logger.error(f"YouTube refresh failed: {refresh_err}. Wiping credentials.")
                set_config(YOUTUBE_KEY, "")
                msg = "Token expired or revoked. Please re-link account."
                set_account_state("youtube", False, msg)
                return False, msg

        # 2. Probe API (Optional)
        if probe_api:
            try:
                # Cache discovery=False speeds up simple calls
                service = build("youtube", "v3", credentials=creds, cache_discovery=False)
                
                # We use channels.list(mine=True) to verify we can identify the user.
                # This requires 'youtube.readonly' scope.
                service.channels().list(part="id", mine=True, maxResults=1).execute()
                
            except HttpError as http_err:
                msg = describe_youtube_http_error(http_err)
                set_account_state("youtube", False, msg)
                logger.warning("YouTube API probe failed: %s", msg)
                return False, msg
            except Exception as exc:
                msg = f"API probe failed: {exc}"
                set_account_state("youtube", False, msg)
                return False, msg

        set_account_state("youtube", True, None)
        logger.info("YouTube credentials verified%s.", " with API probe" if probe_api else "")
        return True, "YouTube token and API access look good."

    except Exception as exc:
        msg = f"Credential error: {exc}"
        set_account_state("youtube", False, msg)
        logger.warning("YouTube credential verification failed: %s", msg)
        return False, msg


def save_google_client_config(raw_json: str) -> Tuple[bool, str]:
    try:
        data = json.loads(raw_json)
        # Basic validation of JSON structure
        if "installed" not in data and "web" not in data:
            return False, "Invalid OAuth client JSON. Must contain 'installed' or 'web' key."
        set_config(GOOGLE_CLIENT_CONFIG_KEY, json.dumps(data))
        return True, "Google OAuth client saved."
    except json.JSONDecodeError as exc:
        return False, f"JSON error: {exc}"


def get_google_client_config(pretty: bool = False) -> str:
    data = get_config(GOOGLE_CLIENT_CONFIG_KEY)
    
    # Fallback to file if not in DB
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