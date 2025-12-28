import os
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from src.logging_utils import init_logging
from src.database import (
    get_config,
    get_json_config,
    set_account_state,
    set_config,
    set_json_config,
)

SESSION_KEY = "tiktok_session_bundle"
LEGACY_KEY = "tiktok_session_id"
VERIFICATION_INTERVAL_HOURS = 6
REFRESH_WARNING_DAYS = 25
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

logger = init_logging("tiktok")

def _utcnow() -> datetime:
    return datetime.utcnow()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _session_bundle() -> Dict:
    data = get_json_config(SESSION_KEY, {})
    if not data:
        legacy = get_config(LEGACY_KEY)
        if legacy:
            data = {
                "sessionid": legacy,
                "stored_at": _utcnow().isoformat(),
                "valid": False,
                "last_verified": None,
            }
            set_json_config(SESSION_KEY, data)
    return data or {}


def _persist_bundle(bundle: Dict) -> None:
    set_json_config(SESSION_KEY, bundle)
    if bundle.get("sessionid"):
        set_config(LEGACY_KEY, bundle["sessionid"])


def save_session(session_id: str) -> None:
    cleaned = session_id.strip()
    if not cleaned:
        bundle = {}
        _persist_bundle(bundle)
        set_account_state("tiktok", False, "Session missing")
        set_config("tiktok_refresh_warned", "")
        logger.warning("Cleared TikTok session cookie.")
        return
    bundle = _session_bundle()
    bundle.update(
        {
            "sessionid": cleaned,
            "stored_at": _utcnow().isoformat(),
            "valid": False,
            "last_verified": None,
            "account_name": None,
        }
    )
    _persist_bundle(bundle)
    set_config("tiktok_refresh_warned", "")
    set_account_state("tiktok", bool(cleaned), None if cleaned else "Session missing")
    logger.info("TikTok session stored (length=%s).", len(cleaned))
    verify_session(force=True)


def _session_age_days(bundle: Dict) -> Optional[int]:
    stored = bundle.get("stored_at")
    stored_dt = _parse_iso(stored)
    if not stored_dt:
        return None
    return (_utcnow() - stored_dt).days


def session_status() -> Dict:
    bundle = _session_bundle()
    age = _session_age_days(bundle)
    return {
        "sessionid": bundle.get("sessionid"),
        "valid": bundle.get("valid", False),
        "last_verified": bundle.get("last_verified"),
        "account_name": bundle.get("account_name"),
        "stored_at": bundle.get("stored_at"),
        "needs_refresh": (age is not None and age >= REFRESH_WARNING_DAYS),
        "age_days": age,
        "message": bundle.get("last_error"),
    }


def session_connected() -> bool:
    status = session_status()
    return bool(status["sessionid"]) and status["valid"]


def _probe_session(session_id: str) -> Tuple[bool, str, Optional[str]]:
    url = "https://www.tiktok.com/passport/web/account/info/?aid=1459"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Cookie": f"sessionid={session_id};",
        "Referer": "https://www.tiktok.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning("TikTok probe HTTP %s", resp.status_code)
            return False, f"HTTP {resp.status_code}", None
        
        data = resp.json()
        status_code = data.get("status_code")
        status_msg = data.get("status_msg") or data.get("message") or ""
        payload = data.get("data") or {}

        # Attempt to extract username from various common keys
        username = (
            payload.get("username")
            or payload.get("unique_id")
            or payload.get("nick_name")
            or payload.get("display_name")
        )

        # FIX: The API might return status_code=0 OR omit status_code entirely (None) while returning valid data.
        # If we successfully extracted a username, we consider the session valid.
        is_valid = (status_code == 0 or status_code is None) and bool(username)

        if is_valid:
            message = status_msg or (f"Session valid for @{username}" if username else "Session verified.")
            logger.info("TikTok session verified for @%s", username or "?")
            return True, message, username

        message = status_msg or f"Session invalid (status_code={status_code})"
        logger.warning("TikTok session invalid: %s", message)
        return False, message, None
    except Exception as exc:
        logger.error("TikTok probe error: %s", exc)
        return False, str(exc), None


def ensure_session_valid(force: bool = False) -> Tuple[bool, Optional[str], str]:
    bundle = _session_bundle()
    session_id = bundle.get("sessionid")
    if not session_id:
        msg = "TikTok session missing."
        set_account_state("tiktok", False, msg)
        logger.warning("TikTok upload attempted without session cookie.")
        return False, None, msg

    last_verified = _parse_iso(bundle.get("last_verified"))
    if (
        not force
        and bundle.get("valid")
        and last_verified
        and _utcnow() - last_verified < timedelta(hours=VERIFICATION_INTERVAL_HOURS)
    ):
        return True, session_id, f"Session recently verified for @{bundle.get('account_name', '?')}"

    ok, message, username = _probe_session(session_id)
    bundle["valid"] = ok
    bundle["last_verified"] = _utcnow().isoformat()
    if username:
        bundle["account_name"] = username
    bundle["last_error"] = None if ok else message
    _persist_bundle(bundle)
    set_account_state("tiktok", ok, None if ok else message)
    if not ok:
        logger.warning("TikTok session validation failed: %s", message)
        return False, None, message
    return True, session_id, message


def verify_session(force: bool = True) -> Tuple[bool, str]:
    ok, _, message = ensure_session_valid(force=force)
    return ok, message


def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok or not session_id:
        return False, info

    logger.info("Starting TikTok upload for %s.", os.path.basename(video_path))
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.binary_location = os.environ.get("CHROME_BIN", "/usr/bin/chromium")

    driver = None

    try:
        try:
            driver_path = os.environ.get("CHROMEDRIVER_PATH")
            if not driver_path or not os.path.exists(driver_path):
                driver_path = ChromeDriverManager().install()
            driver = webdriver.Chrome(service=Service(driver_path), options=options)
        except Exception as exc:
            msg = f"ChromeDriver setup failed: {exc}"
            set_account_state("tiktok", False, msg)
            logger.error(msg)
            return False, msg

        driver.get("https://www.tiktok.com")
        driver.add_cookie(
            {
                "name": "sessionid",
                "value": session_id,
                "domain": ".tiktok.com",
                "path": "/",
            }
        )
        driver.get("https://www.tiktok.com/upload?lang=en")

        try:
            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//iframe"))
            )
            driver.switch_to.frame(iframe)
        except Exception:
            pass

        file_input = WebDriverWait(driver, 45).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
        file_input.send_keys(os.path.abspath(video_path))

        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//div[contains(text(), 'Uploaded')]|//div[contains(@class, 'uploaded')]",
                )
            )
        )

        try:
            caption_input = driver.find_element(
                By.XPATH, "//div[contains(@class, 'public-DraftEditor-content')]"
            )
            caption_input.send_keys(description or "")
        except Exception:
            pass

        post_btn = WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable(
                (By.XPATH, "//button[div[text()='Post']]|//button[text()='Post']")
            )
        )
        post_btn.click()

        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located(
                (
                    By.XPATH,
                    "//div[contains(text(), 'Manage your posts')]|//div[contains(text(), 'Your video is being uploaded')]",
                )
            )
        )
        set_account_state("tiktok", True, None)
        logger.info("TikTok upload succeeded for %s.", os.path.basename(video_path))
        return True, "Upload Successful"

    except Exception as exc:
        set_account_state("tiktok", False, str(exc))
        logger.error("TikTok upload error: %s", exc)
        return False, str(exc)
    finally:
        if driver:
            driver.quit()
