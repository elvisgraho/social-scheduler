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
    WebDriverException,
    ElementClickInterceptedException
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
# Use a standard, lightweight user agent
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


# --- UTILS ---

def _dismiss_popups(driver):
    """
    Tries to CLICK known popups/cookies regularly.
    Does NOT remove elements from DOM (Safe for React).
    """
    # Common button text for dialogs
    xpath_targets = [
        "//button[contains(text(), 'Decline optional cookies')]",
        "//button[contains(text(), 'Allow all')]",
        "//button[contains(text(), 'Got it')]",
        "//button[contains(text(), 'Retry')]",
        "//div[contains(@class, 'cookie-banner')]//button"
    ]
    
    for xp in xpath_targets:
        try:
            # Only find elements that are actually visible
            elements = driver.find_elements(By.XPATH, xp)
            for el in elements:
                if el.is_displayed() and el.is_enabled():
                    # Try standard click first
                    try:
                        el.click()
                    except:
                        # Fallback to JS click if blocked, but still clicking the correct element
                        driver.execute_script("arguments[0].click();", el)
                    time.sleep(0.5)
        except:
            pass
            
    # Escape key is a cheap way to close many modals
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except:
        pass

def _debug_dump(driver, queue_name="error"):
    try:
        ts = datetime.now().strftime("%H%M%S")
        debug_dir = os.path.join("data", "logs")
        os.makedirs(debug_dir, exist_ok=True)
        
        screen_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.png")
        driver.save_screenshot(screen_path)
        
        log_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.log")
        # Only get browser logs if available/supported
        try:
            logs = driver.get_log('browser')
            with open(log_path, "w", encoding="utf-8") as f:
                for entry in logs:
                    f.write(f"{entry['level']}: {entry['message']}\n")
        except:
            pass
                
        logger.error(f"Debug artifacts saved: {screen_path}")
    except Exception:
        pass

def _find_chromedriver():
    paths = ["/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver", "/usr/local/bin/chromedriver"]
    for p in paths:
        if os.path.exists(p): return p
    return "chromedriver"

# --- UPLOAD FUNCTION ---

def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok or not session_id:
        return False, info

    if not os.path.exists(video_path):
        return False, f"File not found: {video_path}"

    logger.info("Starting TikTok upload for %s...", os.path.basename(video_path))
    
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--timezone=Europe/Berlin") 
    options.page_load_strategy = 'normal'
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    
    service = Service(_find_chromedriver())
    driver = None
    
    try:
        driver = webdriver.Chrome(service=service, options=options)
        
        # Stealth JS
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        })

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
        
        # 3. Radar Mode (Optimized for Pi)
        # We loop slower (every 2s) to save CPU
        logger.debug("Scanning for file input...")
        file_input = None
        
        for i in range(30): # 60 seconds total (30 * 2)
            try:
                # Check Main DOM
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                logger.info("Found input in Main DOM.")
                break
            except NoSuchElementException:
                pass
            
            # Check Iframes (Safe iteration)
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            found_in_frame = False
            for index, frame in enumerate(iframes):
                try:
                    driver.switch_to.frame(frame)
                    if len(driver.find_elements(By.XPATH, "//input[@type='file']")) > 0:
                        file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                        logger.info(f"Found input in Iframe #{index}.")
                        found_in_frame = True
                        break 
                except:
                    pass
                finally:
                    if not found_in_frame:
                        driver.switch_to.default_content()
            
            if found_in_frame:
                break
                
            # Periodically try to dismiss popups, but not too often
            if i % 3 == 0:
                _dismiss_popups(driver)
                
            time.sleep(2) # Relaxed polling for Pi
        
        if not file_input:
            _debug_dump(driver, "input_missing")
            raise Exception("Could not locate file input (Scanned Main + All Iframes)")

        # 4. Upload
        # Force display block so Selenium can interact with hidden inputs
        driver.execute_script("arguments[0].style.display = 'block';", file_input)
        file_input.send_keys(os.path.abspath(video_path))
        
        # Wait for file to be ingested
        time.sleep(5)
        
        # Ensure we are back in main context
        driver.switch_to.default_content()

        # 5. Wait for Processing
        logger.info("Waiting for upload processing...")
        upload_success = False
        
        # Poll every 2 seconds, max 120s
        for _ in range(60): 
            success_indicators = [
                "//div[contains(text(), 'Edit cover')]", 
                "//div[contains(text(), 'Uploaded')]",
                "//button[contains(text(), 'Replace')]"
            ]
            
            for path in success_indicators:
                if len(driver.find_elements(By.XPATH, path)) > 0:
                    upload_success = True
                    break
            
            if upload_success:
                logger.info("Upload processed.")
                break
            
            if _ % 5 == 0:
                _dismiss_popups(driver)
                
            time.sleep(2) # Low CPU wait

        if not upload_success:
            _debug_dump(driver, "upload_timeout")
            raise Exception("Upload timed out")

        # 6. Description / Caption (React-Safe Method)
        if description:
            try:
                # Wait for editor
                caption_box = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".public-DraftEditor-content"))
                )
                
                # Scroll just enough to see it
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", caption_box)
                time.sleep(1)
                
                # Use ActionChains to simulate REAL user behavior.
                # This works with React's state management.
                actions = ActionChains(driver)
                actions.move_to_element(caption_box).click()
                actions.pause(0.5)
                # Select All -> Delete (Cleanest way to clear DraftJS)
                actions.key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL)
                actions.pause(0.2)
                actions.send_keys(Keys.BACKSPACE)
                actions.pause(0.2)
                # Type content
                actions.send_keys(description)
                actions.perform()
                
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Caption entry failed: {e}")

        # 7. Post Button Logic (Overlay-Safe)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        logger.info("Waiting for Post button...")
        
        for _ in range(30): # 60 seconds
            try:
                # Try to dismiss cookies gracefully first
                _dismiss_popups(driver)
                
                btns = driver.find_elements(By.XPATH, "//button[contains(text(), 'Post') or contains(text(), 'Schedule')]")
                
                if btns:
                    post_btn = btns[0]
                    classes = post_btn.get_attribute("class")
                    
                    if post_btn.is_enabled() and "disabled" not in classes:
                        # Attempt 1: Standard Click (Best for React)
                        try:
                            # Scroll center to hopefully move it away from bottom banners
                            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_btn)
                            time.sleep(1) 
                            post_btn.click()
                        except (ElementClickInterceptedException, WebDriverException):
                            # Attempt 2: JS Click Bypass
                            # If the cookie banner blocks the click, this executes the click 
                            # directly on the button element, ignoring the overlay.
                            # NO DOM deletion required.
                            logger.info("Standard click blocked, using JS bypass...")
                            driver.execute_script("arguments[0].click();", post_btn)
                        
                        logger.info("Post button clicked.")
                        break
            except Exception:
                pass
                
            time.sleep(2) # Wait before retry

        # 8. Final Verification
        try:
            WebDriverWait(driver, 60).until(
                lambda d: "upload" not in d.current_url or 
                          len(d.find_elements(By.XPATH, "//div[contains(., 'Manage your posts')]")) > 0 or
                          len(d.find_elements(By.XPATH, "//div[contains(., 'Upload another video')]")) > 0
            )
            set_account_state("tiktok", True, None)
            logger.info("Upload Successful!")
            return True, "Upload Successful"
        except TimeoutException:
            # Check one last time if we are redirected
            if "upload" not in driver.current_url:
                set_account_state("tiktok", True, None)
                return True, "Upload Successful (Url Changed)"
            
            _debug_dump(driver, "post_verification_timeout")
            raise Exception("Post button clicked but verification timed out")

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