import os
import time
import requests
import json
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
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

logger = init_logging("tiktok")

# --- HELPER FUNCTIONS ---
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
    """Check if session is present and marked valid."""
    status = session_status()
    return bool(status["sessionid"]) and status["valid"]

def _probe_session(session_id: str) -> Tuple[bool, str, Optional[str]]:
    url = "https://www.tiktok.com/passport/web/account/info/?aid=1459"
    headers = {"User-Agent": USER_AGENT, "Cookie": f"sessionid={session_id};"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        username = data.get("data", {}).get("username") or data.get("data", {}).get("unique_id")
        is_valid = (data.get("data", {}).get("login_status") == 0 or bool(username))
        
        if is_valid: return True, f"Valid: @{username}", username
        return False, f"Invalid: {data.get('message') or 'Session expired'}", None
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
    if not ok: bundle["last_error"] = msg
    _persist_bundle(bundle)
    set_account_state("tiktok", ok, msg if not ok else None)
    return ok, session_id, msg

def verify_session(force: bool = True) -> Tuple[bool, str]:
    ok, _, message = ensure_session_valid(force=force)
    return ok, message


# --- GENTLE INTERACTION UTILS ---

def _inject_text_via_js(driver, element, text):
    """Safely injects text (including Emojis) into a contenteditable."""
    try:
        safe_text = json.dumps(text)
        driver.execute_script(f"""
            var elm = arguments[0];
            var txt = {safe_text};
            elm.focus();
            if (document.execCommand('insertText', false, txt)) return;
            elm.textContent = txt;
            elm.dispatchEvent(new Event('input', {{ bubbles: true }}));
        """, element)
    except Exception as e:
        logger.warning(f"JS Inject failed: {e}")

def _handle_popups_lightweight(driver):
    """
    Lightweight popup clicker using direct XPath. 
    Does NOT use heavy JS scanning to save CPU on Pi 5.
    """
    # Specific buttons we know exist
    targets = [
        "//button[contains(text(), 'Turn on')]", # Content check
        "//button[contains(text(), 'Allow all')]", # Cookies
        "//button[contains(text(), 'Got it')]", # Feature tour
        "//button[contains(text(), 'Retry')]", # Error page
        "//div[contains(text(), 'Turn on')]" # Sometimes it's a div
    ]
    
    did_click = False
    for xpath in targets:
        try:
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        did_click = True
                        logger.info(f"Clicked popup: {xpath}")
                    except:
                        pass
        except:
            pass
            
    # Escape key is very cheap on CPU and closes most modals
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except:
        pass
        
    return did_click

def _debug_dump(driver, queue_name="error"):
    try:
        ts = datetime.now().strftime("%H%M%S")
        debug_dir = os.path.join("data", "logs")
        os.makedirs(debug_dir, exist_ok=True)
        
        screen_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.png")
        driver.save_screenshot(screen_path)
        logger.error(f"Debug artifacts saved: {screen_path}")
    except Exception:
        pass

def _find_chromedriver():
    paths = [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/local/bin/chromedriver"
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return "chromedriver"

# --- UPLOAD FUNCTION ---

def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok or not session_id:
        return False, info

    if not os.path.exists(video_path):
        return False, f"File not found: {video_path}"

    logger.info("Starting TikTok upload for %s...", os.path.basename(video_path))
    
    # --- DRIVER SETUP ---
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    # Standard 1080p to ensure buttons aren't hidden in mobile view
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")
    # Fix Timezone crash
    options.add_argument("--timezone=Europe/Berlin") 
    
    service = Service(_find_chromedriver())
    driver = None
    
    try:
        driver = webdriver.Chrome(service=service, options=options)
        
        # 1. Authenticate
        driver.get("https://www.tiktok.com")
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expiry": int(time.time()) + 31536000
        })
        
        # 2. Navigate
        driver.get("https://www.tiktok.com/upload?lang=en")
        
        # 3. Simple File Input Locator
        logger.debug("Locating file input...")
        try:
            # We wait for the input OR the iframe to appear
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='file'] | //iframe"))
            )
        except TimeoutException:
            # If we timed out, check for Login redirect
            if "login" in driver.current_url:
                return False, "Session expired (Login Redirect)"
            raise Exception("Page failed to load input")

        # Handle Iframe logic simply
        file_input = None
        try:
            file_input = driver.find_element(By.XPATH, "//input[@type='file']")
        except NoSuchElementException:
            # Check frames
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for frame in iframes:
                driver.switch_to.frame(frame)
                if len(driver.find_elements(By.XPATH, "//input[@type='file']")) > 0:
                    file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                    break
                driver.switch_to.default_content()
        
        if not file_input:
            raise Exception("File input not found")

        # 4. Upload
        driver.execute_script("arguments[0].style.display = 'block';", file_input)
        file_input.send_keys(os.path.abspath(video_path))
        
        # Wait for file processing (Important for Pi speed)
        time.sleep(5)
        
        # Reset context
        driver.switch_to.default_content()

        # 5. Handle Popups & Wait for Success
        # We wait for "Edit cover" which appears when video is ready.
        logger.info("Waiting for upload success (Edit cover)...")
        
        upload_success = False
        post_btn = None
        
        # Wait up to 120s
        for _ in range(60):
            # A. Check for Crash Page (Sad Robot)
            if len(driver.find_elements(By.XPATH, "//div[contains(text(), 'Something went wrong')]")) > 0:
                # Try clicking retry
                _handle_popups_lightweight(driver)
                time.sleep(2)
                # If still there, abort
                if len(driver.find_elements(By.XPATH, "//div[contains(text(), 'Something went wrong')]")) > 0:
                    raise Exception("TikTok crashed (Something went wrong)")

            # B. Clear Popups (Cookies, Content Check)
            _handle_popups_lightweight(driver)
            
            # C. Check Success
            if len(driver.find_elements(By.XPATH, "//div[contains(text(), 'Edit cover')]")) > 0:
                upload_success = True
                logger.info("Upload Verified.")
                break
                
            time.sleep(2) # 2s sleep to save CPU

        if not upload_success:
            _debug_dump(driver, "upload_timeout")
            raise Exception("Upload timed out (Video processing stuck)")

        # 6. Caption
        if description:
            try:
                # Find editor
                caption_box = driver.find_element(By.CSS_SELECTOR, ".public-DraftEditor-content")
                driver.execute_script("arguments[0].click();", caption_box)
                time.sleep(0.5)
                _inject_text_via_js(driver, caption_box, description)
            except:
                pass

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

        # 7. Click Post
        # We assume if "Edit cover" is visible, we are ready to post after handling copyright popup
        logger.info("Finalizing post...")
        
        for _ in range(30):
            _handle_popups_lightweight(driver) # Kill "Turn on" button
            
            try:
                # Try to find Post button
                btns = driver.find_elements(By.XPATH, "//button[contains(text(), 'Post')]")
                if btns:
                    btn = btns[0]
                    # If enabled, click
                    if btn.is_enabled():
                        driver.execute_script("arguments[0].click();", btn)
                        break
            except:
                pass
            time.sleep(2)

        # 8. Verify
        try:
            WebDriverWait(driver, 30).until(
                lambda d: "upload" not in d.current_url or 
                len(d.find_elements(By.XPATH, "//div[contains(., 'Manage your posts')]")) > 0
            )
            set_account_state("tiktok", True, None)
            logger.info("Upload Successful!")
            return True, "Upload Successful"
        except TimeoutException:
            # If we are still on upload page, assume detection failed but check if button is gone
            if len(driver.find_elements(By.XPATH, "//button[contains(text(), 'Post')]")) == 0:
                 return True, "Upload likely success (Post button gone)"
            else:
                 _debug_dump(driver, "final_fail")
                 return False, "Post button clicked but page didn't change"

    except Exception as exc:
        err_msg = str(exc).split("\n")[0]
        if driver: _debug_dump(driver, "upload_failure")
        logger.error(f"TikTok Upload Failed: {exc}")
        set_account_state("tiktok", False, err_msg)
        return False, err_msg
        
    finally:
        if driver:
            try: driver.quit()
            except: pass