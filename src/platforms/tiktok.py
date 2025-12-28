import os
import time
import json
import requests
import random
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, 
    NoSuchElementException, 
    ElementClickInterceptedException,
    WebDriverException
)

from src.logging_utils import init_logging
from src.database import (
    get_config,
    get_json_config,
    set_account_state,
    set_config,
    set_json_config,
)

# --- CONFIGURATION ---
SESSION_KEY = "tiktok_session_bundle"
LEGACY_KEY = "tiktok_session_id"
VERIFICATION_INTERVAL_HOURS = 6
REFRESH_WARNING_DAYS = 25
# Use a very standard, real desktop User Agent to avoid fingerprinting
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

logger = init_logging("tiktok")

# --- HELPER FUNCTIONS (Preserved) ---
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


# --- ROBUST UPLOAD IMPLEMENTATION ---

def _nuke_overlays(driver):
    """Aggressively removes modal backdrops, banners, and overlays via JS."""
    try:
        driver.execute_script("""
            const selectors = [
                'div[class*="modal"]', 'div[class*="mask"]', 'div[class*="overlay"]',
                'div[class*="banner"]', 'div[id*="cookie"]'
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
        """)
    except Exception:
        pass

def _debug_dump(driver, queue_name="error"):
    """Saves screenshot and HTML to debug empty error messages."""
    try:
        ts = datetime.now().strftime("%H%M%S")
        debug_dir = os.path.join("data", "logs")
        os.makedirs(debug_dir, exist_ok=True)
        
        screen_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.png")
        html_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.html")
        
        driver.save_screenshot(screen_path)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
            
        logger.error(f"Debug saved: {screen_path}")
    except Exception as e:
        logger.error(f"Failed to save debug info: {e}")

def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok or not session_id:
        return False, info

    logger.info("Starting TikTok upload for %s...", os.path.basename(video_path))

    # 1. Chromium Options for Headless / Anti-Detect
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") # Critical for Docker
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-infobars")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")
    
    # Hide Selenium Signature
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Use System Driver (ARM/Pi compatible)
    service = Service("/usr/bin/chromedriver")
    
    driver = None
    try:
        driver = webdriver.Chrome(service=service, options=options)
        
        # 2. Advanced CDP Stealth (Mask webdriver & plugins)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            """
        })

        # 3. Authenticate
        logger.debug("Navigating to TikTok...")
        driver.get("https://www.tiktok.com")
        
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True
        })
        
        # 4. Navigate to Upload
        driver.refresh()
        time.sleep(2)
        driver.get("https://www.tiktok.com/upload?lang=en")

        # 5. Iframe / Context Detection
        # TikTok sometimes wraps the upload tool in an iframe.
        wait = WebDriverWait(driver, 20)
        try:
            iframe = wait.until(EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src, 'upload')]")))
            driver.switch_to.frame(iframe)
            logger.debug("Switched to upload iframe.")
        except TimeoutException:
            logger.debug("No upload iframe found, assuming main frame.")

        # 6. File Input
        logger.debug("Locating file input...")
        file_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@type='file']")))
        file_input.send_keys(os.path.abspath(video_path))

        # 7. Wait for Upload Verification
        # Critical: Wait until the "Uploading" percent indicator DISAPPEARS or "Uploaded" text APPEARS.
        logger.debug("Waiting for video processing...")
        # Check for success indicators
        WebDriverWait(driver, 180).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Uploaded')] | //div[contains(@class, 'uploaded')] | //div[text()='100%']")
            )
        )
        
        # 8. Caption Input
        # TikTok uses a DraftEditor, usually div[contenteditable="true"]
        try:
            caption_box = driver.find_element(By.CSS_SELECTOR, ".public-DraftEditor-content")
        except NoSuchElementException:
            caption_box = driver.find_element(By.XPATH, "//div[@contenteditable='true']")
            
        if caption_box and description:
            # Click to focus
            driver.execute_script("arguments[0].click();", caption_box)
            time.sleep(0.5)
            # Use JS to clear and set text to avoid modifier key issues in headless
            # Note: TikTok DraftEditor is complex, sending keys is safer than innerHTML replacement
            ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
            time.sleep(0.5)
            ActionChains(driver).send_keys(description).perform()

        # 9. Handle "Post" Button
        # Scroll down
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        _nuke_overlays(driver) # Remove 'Get App' banners

        # Find the button
        post_btn = wait.until(EC.presence_of_element_located(
            (By.XPATH, "//button[div[text()='Post']] | //button[text()='Post']")
        ))
        
        # 10. Wait for "Copyright Check" / Processing
        # The button is disabled while TikTok checks the video.
        logger.debug("Waiting for Post button to enable...")
        for i in range(30):
            # Check for disabled attribute or class
            classes = post_btn.get_attribute("class") or ""
            disabled_attr = post_btn.get_attribute("disabled")
            
            if disabled_attr is None and "disabled" not in classes:
                break
            time.sleep(1)
            
        # 11. Click
        logger.info("Clicking Post...")
        try:
            post_btn.click()
        except ElementClickInterceptedException:
            # Fallback for overlays
            driver.execute_script("arguments[0].click();", post_btn)

        # 12. Confirm Success
        # Wait for redirect to profile or "Manage your posts" modal
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Manage your posts')] | //div[contains(text(), 'Your video is being uploaded')] | //div[contains(text(), 'Upload another')]")
            )
        )
        
        set_account_state("tiktok", True, None)
        return True, "Upload Successful"

    except Exception as exc:
        # Debugging for headless failures
        if driver:
            _debug_dump(driver, "upload_failure")
            
        err_msg = str(exc)
        # Simplify error for DB
        if "Timeout" in err_msg:
            err_msg = "Timeout waiting for TikTok elements (Check screenshot in logs)"
        elif "NoSuchElement" in err_msg:
            err_msg = "Could not find upload elements (Check screenshot in logs)"
            
        logger.error(f"TikTok Upload Failed: {err_msg}")
        set_account_state("tiktok", False, err_msg)
        return False, err_msg
        
    finally:
        if driver:
            driver.quit()