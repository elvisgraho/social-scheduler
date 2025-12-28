from typing import Callable, Dict

from src import platforms
from src.auth_utils import youtube_connected
from src.database import get_account_state, get_all_account_states, get_config, set_account_state

PlatformConfig = Dict[str, Callable]


def _instagram_connected() -> bool:
    try:
        return platforms.instagram.session_connected()
    except Exception:
        return False


def _tiktok_connected() -> bool:
    return platforms.tiktok.session_connected()


PLATFORMS: Dict[str, Dict] = {
    "youtube": {
        "label": "YouTube",
        "uploader": platforms.youtube.upload,
        "connected": youtube_connected,
    },
    "instagram": {
        "label": "Instagram",
        "uploader": platforms.instagram.upload,
        "connected": _instagram_connected,
    },
    "tiktok": {
        "label": "TikTok",
        "uploader": platforms.tiktok.upload,
        "connected": _tiktok_connected,
    },
}


def get_platforms() -> Dict[str, Dict]:
    return PLATFORMS


def platform_status(platform_key: str) -> Dict:
    state = get_account_state(platform_key)
    config = PLATFORMS.get(platform_key, {})
    connected = config.get("connected", lambda: False)()

    # Always refresh account_state to reflect current connection truth.
    if connected:
        if not state.get("connected") or state.get("last_error"):
            set_account_state(platform_key, True, None)
            state = get_account_state(platform_key)
    else:
        # Keep last_error if already recorded; otherwise mark as disconnected without a specific error.
        if state.get("connected"):
            set_account_state(platform_key, False, state.get("last_error"))
            state = get_account_state(platform_key)

    return state


def all_platform_statuses() -> Dict[str, Dict]:
    statuses = {}
    for key in PLATFORMS:
        statuses[key] = platform_status(key)
    return statuses
