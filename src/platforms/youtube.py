import json
from typing import Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from src.logging_utils import init_logging
from src.auth_utils import get_youtube_credentials
from src.database import set_account_state, set_config

YOUTUBE_KEY = "youtube_credentials"

logger = init_logging("youtube")


def _load_credentials() -> Tuple[bool, str, Credentials]:
    token_json = get_youtube_credentials()
    if not token_json:
        return False, "YouTube account not linked.", None
    try:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            set_config(YOUTUBE_KEY, creds.to_json())
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
            "status": {"privacyStatus": "public"},
        }
        
        # Fix: Use specific chunksize (4MB) for better memory usage vs -1
        media = MediaFileUpload(video_path, chunksize=4 * 1024 * 1024, resumable=True)
        request = service.videos().insert(part="snippet,status", body=body, media_body=media)

        # Fix: Robust upload loop instead of simple .execute()
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("Uploaded %d%%", int(status.progress() * 100))
            
        video_id = response.get("id")
        set_account_state("youtube", True, None)
        return True, f"Uploaded ID: {video_id}"

    except HttpError as e:
        # Fix: Parse API error content for clearer logs (e.g., 'quotaExceeded')
        reason = e.reason
        try:
            reason = json.loads(e.content)['error']['message']
        except Exception:
            pass
        err_msg = f"API Error {e.resp.status}: {reason}"
        set_account_state("youtube", False, err_msg)
        return False, err_msg

    except Exception as exc:
        set_account_state("youtube", False, str(exc))
        return False, str(exc)