import json
from typing import Tuple, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from src.logging_utils import init_logging
from src.auth_utils import get_youtube_credentials, describe_youtube_http_error
from src.database import set_account_state, set_config

YOUTUBE_KEY = "youtube_credentials"
logger = init_logging("youtube")

def _load_credentials() -> Tuple[bool, str, Optional[Credentials]]:
    token_json = get_youtube_credentials()
    if not token_json:
        return False, "YouTube account not linked.", None
    try:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info)
        
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                set_config(YOUTUBE_KEY, creds.to_json())
            except Exception as refresh_err:
                # CRITICAL FIX: If refresh fails (token revoked/expired), wipe it.
                # This ensures the UI shows "Not Linked" so you can fix it.
                logger.error(f"YouTube token refresh failed: {refresh_err}. Deleting invalid credentials.")
                set_config(YOUTUBE_KEY, "")
                set_account_state("youtube", False, "Token expired/revoked. Please re-link.")
                return False, "Token expired. Please re-link account.", None
                
        return True, "", creds
    except Exception as exc:
        return False, f"Credential error: {exc}", None


def upload(video_path: str, title: str, description: str):
    ok, msg, creds = _load_credentials()
    if not ok:
        set_account_state("youtube", False, msg)
        return False, msg

    try:
        service = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {
                "title": title[:100] if title else "Short",
                "description": (description or "")[:5000],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
                "notifySubscribers": False,
            },
        }
        
        # Robust Chunking (4MB)
        media = MediaFileUpload(video_path, chunksize=4 * 1024 * 1024, resumable=True)
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)

        logger.info("Starting YouTube upload...")
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                # Log progress occasionally to know it's not frozen
                logger.debug(f"YouTube Upload Progress: {int(status.progress() * 100)}%")
            
        video_id = response.get("id")
        set_account_state("youtube", True, None)
        return True, f"Uploaded ID: {video_id}"

    except HttpError as e:
        # CRITICAL FIX: Explicitly catch the Scope error so you know exactly what to do.
        if e.resp.status == 403:
            # Check if it's strictly a scope issue
            content = str(e)
            if "insufficient" in content.lower() and "scope" in content.lower():
                err_msg = "Permission Denied: Missing 'youtube.upload' scope. You must re-authenticate with the setup script."
            else:
                err_msg = describe_youtube_http_error(e)
        else:
            err_msg = describe_youtube_http_error(e)
            
        set_account_state("youtube", False, err_msg)
        return False, err_msg

    except Exception as exc:
        set_account_state("youtube", False, str(exc))
        return False, str(exc)