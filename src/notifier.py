from typing import Optional
import requests
from src.database import get_config
from src.logging_utils import init_logging

logger = init_logging("notifier")

def _telegram_endpoint() -> Optional[str]:
    token = get_config("telegram_bot_token")
    chat_id = get_config("telegram_chat_id")
    if not token or not chat_id:
        return None
    return f"https://api.telegram.org/bot{token}/sendMessage", chat_id


def telegram_enabled() -> bool:
    return _telegram_endpoint() is not None


def send_telegram_message(message: str, silent: bool = False) -> None:
    endpoint = _telegram_endpoint()
    if not endpoint:
        return
    url, chat_id = endpoint
    try:
        requests.post(
            url,
            timeout=10,
            data={
                "chat_id": chat_id,
                "text": message,
                "disable_notification": silent,
                "parse_mode": "HTML",
            },
        )
    except Exception as exc:
        logger.warning("Failed to send Telegram message: %s", exc)
