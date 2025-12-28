import os
import time
import requests
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException

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

# --- Helper Functions ---
def _utcnow() -> datetime:
    return datetime.utcnow()

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value: return None
    try: return datetime.fromisoformat(value)
    except Exception: return None

def _session_bundle() -> Dict:
    data = get_json_config(SESSION_KEY, {})
    if not data:
        legacy = get_config(LEGACY_KEY)
        if legacy:
            data = {"sessionid": legacy, "stored_at": _utcnow().isoformat(), "valid": False, "last_verified": None}
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
        return
    bundle = _session_bundle()
    bundle.update({"sessionid": cleaned, "stored_at": _utcnow().isoformat(), "valid": False, "last_verified": None, "account_name": None})
    _persist_bundle(bundle)
    set_config("tiktok_refresh_warned", "")
    set_account_state("tiktok", bool(cleaned), None)
    verify_session(force=True)

def _session_age_days(bundle: Dict) -> Optional[int]:
    stored = bundle.get("stored_at")
    stored_dt = _parse_iso(stored)
    if not stored_dt: return None
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
    headers = {"User-Agent": USER_AGENT, "Cookie": f"sessionid={session_id};"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        username = data.get("data", {}).get("username") or data.get("data", {}).get("unique_id")
        is_valid = (data.get("status_code") == 0 or data.get("status_code") is None) and bool(username)
        if is_valid: return True, f"Valid: @{username}", username
        return False, f"Invalid: {data.get('status_msg')}", None
    except Exception as exc:
        return False, str(exc), None

def ensure_session_valid(force: bool = False) -> Tuple[bool, Optional[str], str]:
    bundle = _session_bundle()
    session_id = bundle.get("sessionid")
    if not session_id: return False, None, "No session."
    
    last = _parse_iso(bundle.get("last_verified"))
    if not force and bundle.get("valid") and last and _utcnow() - last < timedelta(hours=VERIFICATION_INTERVAL_HOURS):
        return True, session_id, "Valid (Cached)"

    ok, msg, user = _probe_session(session_id)
    bundle["valid"] = ok
    bundle["last_verified"] = _utcnow().isoformat()
    if user: bundle["account_name"] = user
    _persist_bundle(bundle)
    set_account_state("tiktok", ok, msg if not ok else None)
    return ok, session_id, msg

def verify_session(force: bool = True) -> Tuple[bool, str]:
    ok, _, message = ensure_session_valid(force=force)
    return ok, message


# --- ROBUST SELENIUM UPLOAD FUNCTION ---

def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok or not session_id:
        return False, info

    logger.info("Starting TikTok upload for %s (ARM Native Mode)...", os.path.basename(video_path))

    # 1. Setup Options for Headless Docker environment
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(f"user-agent={USER_AGENT}")
    
    # 2. Point to the system-installed driver on the Pi
    service = Service("/usr/bin/chromedriver")
    
    driver = None
    try:
        driver = webdriver.Chrome(service=service, options=options)
        
        # 3. CDP Magic to remove 'navigator.webdriver' flag (Anti-Detection)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })
        
        # 4. Set Domain Context & Cookies
        driver.get("https://www.tiktok.com")
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True
        })
        
        # 5. Refresh to apply cookie and navigate to Upload
        driver.refresh()
        time.sleep(1) # Brief pause to let cookies settle
        driver.get("https://www.tiktok.com/upload?lang=en")

        # 6. Iframe Handling (TikTok sometimes puts upload in an iframe)
        try:
            iframe = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src, 'upload')]"))
            )
            driver.switch_to.frame(iframe)
            logger.debug("Switched to upload iframe.")
        except TimeoutException:
            pass # Interface is likely main frame, proceed

        # 7. File Input
        # Find hidden input, no need to click it, just send keys
        file_input = WebDriverWait(driver, 45).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
        file_input.send_keys(os.path.abspath(video_path))

        # 8. Wait for Processing to Finish
        # We wait for the "Uploaded" text or the "Change video" button which indicates success
        WebDriverWait(driver, 120).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Uploaded')] | //div[contains(@class, 'uploaded')] | //div[text()='100%']")
            )
        )

        # 9. Handle Caption
        try:
            # Try specific editor class first, fall back to generic contenteditable
            caption_input = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'public-DraftEditor-content')] | //div[@contenteditable='true']"))
            )
            # Clear existing text (like filename) logic
            caption_input.send_keys(Keys.CONTROL + "a")
            caption_input.send_keys(Keys.DELETE)
            # Type new description
            caption_input.send_keys(description or "")
        except TimeoutException:
            logger.warning("Could not find caption input, skipping description.")

        # 10. Click Post (Robust Method)
        # Scroll to bottom to ensure button is in viewport
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        
        # Aggressively remove overlays that might intercept the click
        driver.execute_script("""
            const overlays = document.querySelectorAll('[class*="modal"], [class*="banner"], [class*="overlay"]');
            overlays.forEach(el => el.remove());
        """)

        # Wait for button to be clickable AND not disabled (processing check)
        post_btn = WebDriverWait(driver, 45).until(
            EC.element_to_be_clickable((By.XPATH, "//button[div[text()='Post']] | //button[text()='Post']"))
        )
        
        # Verification check: Ensure button isn't disabled (TikTok disables it during copyright check)
        # We loop briefly to wait for 'disabled' attribute to disappear
        for _ in range(10):
            if post_btn.get_attribute("disabled") is None:
                break
            time.sleep(1)

        try:
            post_btn.click()
        except ElementClickInterceptedException:
            # Fallback: JavaScript Click if blocked
            logger.warning("Post click intercepted, trying JS click.")
            driver.execute_script("arguments[0].click();", post_btn)

        # 11. Final Success Verification
        # Wait for redirect to profile or "Manage your posts" modal
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Manage your posts')] | //div[contains(text(), 'Your video is being uploaded')] | //div[contains(text(), 'Upload another')]")
            )
        )
        
        set_account_state("tiktok", True, None)
        return True, "Upload Successful"

    except Exception as exc:
        # Capture short error for DB
        err_msg = str(exc).split("\n")[0]
        # Log full trace for debugging
        logger.error("TikTok upload error details: %s", exc)
        set_account_state("tiktok", False, err_msg)
        return False, err_msg
    finally:
        if driver:
            driver.quit()