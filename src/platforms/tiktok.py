import os
import time
import requests
import json
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

# Selenium Imports
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

# --- HYBRID IMPORT SYSTEM (Server vs Local) ---
try:
    from src.logging_utils import init_logging
    from src.database import (
        get_config,
        get_json_config,
        set_account_state,
        set_config,
        set_json_config,
    )
    logger = init_logging("tiktok")
    IS_LOCAL = False
except ImportError:
    # Fallback for Local Testing
    print("!!! RUNNING IN LOCAL / VISIBLE MODE !!!")
    IS_LOCAL = True
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    logger = logging.getLogger("tiktok_local")
    
    # Mock database functions
    def get_config(key, default=None): return default
    def get_json_config(key, default=None): return default
    def set_config(key, value): pass
    def set_json_config(key, value): pass
    def set_account_state(platform, status, msg): print(f"SET STATE: {platform} -> {status} ({msg})")

# --- CONFIGURATION ---
SESSION_KEY = "tiktok_session_bundle"
LEGACY_KEY = "tiktok_session_id"
VERIFICATION_INTERVAL_HOURS = 6
REFRESH_WARNING_DAYS = 25
# Standard User Agent (Identical to Desktop to avoid detection)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

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

def ensure_session_valid(force: bool = False, local_session: str = None) -> Tuple[bool, Optional[str], str]:
    if local_session:
        return True, local_session, "Local Session Provided"

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

# --- BROWSER UTILS ---

def _browser_log(driver, message):
    """Writes a distinct log to the Browser Console for debugging."""
    try:
        safe_msg = message.replace("'", "\\'")
        driver.execute_script(f"console.log('[TIKTOK_BOT] {safe_msg}');")
        logger.info(message)
    except:
        # In case driver is closed or script fails
        logger.info(message)

def _dismiss_blocking_elements(driver, check_shadow=False) -> bool:
    """
    Clears popups. Returns True if something was dismissed.
    Includes logic for: Exit Modal, Shadow Cookies, and 'Post now' Confirmation.
    Optimized to minimize CPU usage on Pi.
    """
    did_dismiss = False

    # 1. EXIT MODAL HANDLER (High Priority, Fast JS Check)
    try:
        dismissed = driver.execute_script("""
            var h1s = document.getElementsByTagName('h1');
            for (var i = 0; i < h1s.length; i++) {
                if (h1s[i].innerText.indexOf('Are you sure you want to exit') !== -1) {
                    var dialog = h1s[i].closest('div[role="dialog"]');
                    if (dialog) {
                        var buttons = dialog.getElementsByTagName('button');
                        for (var j = 0; j < buttons.length; j++) {
                            if (buttons[j].innerText.indexOf('Cancel') !== -1) {
                                buttons[j].click();
                                return true;
                            }
                        }
                    }
                }
            }
            return false;
        """)
        if dismissed:
            _browser_log(driver, "Dismissed 'Exit' modal")
            did_dismiss = True
            time.sleep(1) # Wait for animation
    except: pass

    # 2. "CONTINUE TO POST?" MODAL (High Priority, Specific XPath)
    try:
        # Only search if we suspect we are at the end
        post_now_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Post now')]")
        for btn in post_now_btns:
            if btn.is_displayed():
                _browser_log(driver, "Found 'Continue to post?' modal - Clicking 'Post now'")
                driver.execute_script("arguments[0].click();", btn)
                did_dismiss = True
                time.sleep(2) # Allow redirect
    except: pass

    # 3. SHADOW COOKIES (Heavy JS - Only runs if needed, or periodically)
    # We rely on the calling loop to not call this excessively
    if check_shadow:
        try:
            driver.execute_script("""
                function clickShadowCookies(root) {
                    try {
                        // Use querySelectorAll which is faster than iterating everything
                        let buttons = root.querySelectorAll('button');
                        buttons.forEach(b => {
                            let txt = b.innerText.toLowerCase();
                            if (txt.includes('allow all') || txt.includes('decline optional')) {
                                if (b.offsetParent !== null) b.click();
                            }
                        });
                    } catch(e) {}
                    try {
                        // Only traverse open shadow roots
                        let all = root.querySelectorAll('*');
                        all.forEach(el => {
                            if (el.shadowRoot) clickShadowCookies(el.shadowRoot);
                        });
                    } catch(e) {}
                }
                clickShadowCookies(document);
            """)
        except: pass

    # 4. STANDARD POPUPS (Fast XPath)
    xpath_targets = [
        "//button[contains(., 'Confirm')]", 
        "//button[contains(., 'Got it')]",
    ]
    for xp in xpath_targets:
        try:
            elements = driver.find_elements(By.XPATH, xp)
            for el in elements:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(0.5)
                    did_dismiss = True
        except: pass
            
    return did_dismiss

def _debug_dump(driver, queue_name="error"):
    """Saves screenshot and logs on failure."""
    try:
        ts = datetime.now().strftime("%H%M%S")
        debug_dir = os.path.join("data", "logs")
        os.makedirs(debug_dir, exist_ok=True)
        
        screen_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.png")
        driver.save_screenshot(screen_path)
        
        # Save browser console logs
        log_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.log")
        try:
            logs = driver.get_log('browser')
            with open(log_path, "w", encoding="utf-8") as f:
                for entry in logs:
                    f.write(f"{entry['level']}: {entry['message']}\n")
        except: pass
                
        logger.error(f"Debug artifacts saved: {screen_path}")
    except Exception:
        pass

def _find_chromedriver():
    # Helper to find driver on different systems
    import shutil
    if shutil.which("chromedriver"):
        return shutil.which("chromedriver")
    paths = ["/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver", "/usr/local/bin/chromedriver"]
    for p in paths:
        if os.path.exists(p): return p
    return "chromedriver"

# --- UPLOAD FUNCTION ---

def upload(video_path: str, description: str, local_session_key: str = None):
    ok, session_id, info = ensure_session_valid(local_session=local_session_key)
    if not ok or not session_id:
        return False, info

    if not os.path.exists(video_path):
        return False, f"File not found: {video_path}"

    logger.info("Starting TikTok upload for %s...", os.path.basename(video_path))
    
    options = Options()
    
    # --- RASPBERRY PI OPTIMIZED SETTINGS ---
    if IS_LOCAL:
        logger.info("Setting up VISIBLE Chrome window...")
        options.add_argument("--window-size=1920,1080")
    else:
        # Critical for Pi Stability
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage") # Fixes crash on low /dev/shm
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-extensions")
        options.add_argument("--window-size=1920,1080")

    options.add_argument(f"user-agent={USER_AGENT}")
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

        _browser_log(driver, "Navigating to TikTok...")
        driver.get("https://www.tiktok.com")
        
        # Add Session Cookie
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expiry": int(time.time()) + 31536000
        })
        
        driver.get("https://www.tiktok.com/upload?lang=en")
        
        # --- 1. INPUT RADAR ---
        _browser_log(driver, "Scanning for file input...")
        file_input = None
        for i in range(30):
            try:
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                break
            except NoSuchElementException:
                pass
            
            # Check Iframes
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            found_in_frame = False
            for frame in iframes:
                try:
                    driver.switch_to.frame(frame)
                    if len(driver.find_elements(By.XPATH, "//input[@type='file']")) > 0:
                        file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                        found_in_frame = True
                        break 
                except: pass
                finally:
                    if not found_in_frame: driver.switch_to.default_content()
            if found_in_frame: break
            
            # Periodically clear popups
            if i % 3 == 0: _dismiss_blocking_elements(driver)
            time.sleep(1)

        if not file_input:
            raise Exception("Could not locate file input")

        _browser_log(driver, "Uploading file...")
        driver.execute_script("arguments[0].style.display = 'block';", file_input)
        file_input.send_keys(os.path.abspath(video_path))
        time.sleep(5)
        driver.switch_to.default_content()

        # --- 2. WAIT LOOP ---
        _browser_log(driver, "Waiting for upload completion...")
        upload_complete = False
        
        # Pi optimization: Check less frequently (every 3s) to save CPU
        for i in range(120): # Max 6 minutes
            # Only run heavy JS dismissal every few cycles to save Pi CPU
            if i % 2 == 0: 
                _dismiss_blocking_elements(driver)
            
            try:
                replace_btns = driver.find_elements(By.XPATH, "//button[@aria-label='Replace' or contains(., 'Replace')]")
                success_status = driver.find_elements(By.XPATH, "//div[contains(@class, 'info-status') and contains(@class, 'success')]")
                cancel_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Cancel')]")
                
                # Completion Logic: (Replace OR Success) AND No Cancel button
                if (len(replace_btns) > 0 or len(success_status) > 0) and len(cancel_btns) == 0:
                    _browser_log(driver, "Upload confirmed complete.")
                    upload_complete = True
                    break
                
                if i % 10 == 0:
                     _browser_log(driver, f"Still uploading... (Attempt {i})")
            except Exception: pass
            time.sleep(3)

        if not upload_complete:
            raise Exception("Upload timed out - 'Replace' button never appeared")

        # --- 3. DESCRIPTION ---
        if description:
            try:
                _browser_log(driver, "Entering description...")
                _dismiss_blocking_elements(driver)
                
                caption_box = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".public-DraftEditor-content"))
                )
                
                # Center scroll to avoid 'Exit' triggers on top/bottom
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", caption_box)
                time.sleep(1)
                
                actions = ActionChains(driver)
                actions.move_to_element(caption_box).click().pause(0.5)
                actions.key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).pause(0.5)
                actions.send_keys(Keys.BACKSPACE).pause(0.5)
                actions.send_keys(description)
                actions.perform()
                
                _browser_log(driver, "Description entered. Waiting 3s for save...")
                time.sleep(3) # Wait for auto-save
            except Exception as e:
                logger.warning(f"Caption failed: {e}")

        # --- 4. POST ---
        _browser_log(driver, "Looking for Post button...")
        
        for _ in range(30):
            try:
                if _dismiss_blocking_elements(driver, check_shadow=False):
                    time.sleep(1) 
                    continue

                # Use Robust Selector (data-e2e)
                btns = driver.find_elements(By.XPATH, "//button[@data-e2e='post_video_button']")
                if not btns:
                    btns = driver.find_elements(By.XPATH, "//button[normalize-space()='Post']")

                if btns:
                    post_btn = btns[0]
                    if post_btn.is_enabled() and "disabled" not in post_btn.get_attribute("class"):
                        
                        # Scroll Center (Safe)
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", post_btn)
                        time.sleep(1.5) 
                        
                        # Last check for modals before clicking
                        if _dismiss_blocking_elements(driver, check_shadow=False):
                            _browser_log(driver, "Modal appeared during scroll - dismissed, retrying...")
                            continue

                        driver.execute_script("arguments[0].click();", post_btn)
                        _browser_log(driver, "Post button clicked. Moving to verification...")
                        break
            except: pass
            time.sleep(2)

        # --- 5. VERIFICATION ---
        _browser_log(driver, "Verifying upload...")
        
        # Loop to catch "Continue to post?" modal or Success
        for _ in range(120): # 60 seconds
            # A. Success Indicators
            if "upload" not in driver.current_url or \
               len(driver.find_elements(By.XPATH, "//div[contains(., 'Manage your posts')]")) > 0 or \
               len(driver.find_elements(By.XPATH, "//div[contains(., 'Upload another video')]")) > 0:
                
                set_account_state("tiktok", True, None)
                _browser_log(driver, "Upload Successful!")
                return True, "Upload Successful"
            
            # B. "Post Now" Modal Check
            if _dismiss_blocking_elements(driver):
                _browser_log(driver, "Handled modal during verification phase.")
                time.sleep(2)
                continue
                
            time.sleep(0.5)
            
        raise Exception("Verification failed - Success indicators not found")

    except Exception as e:
        if driver:
            _debug_dump(driver, "upload_failure")
            if IS_LOCAL:
                logger.error("Error! Leaving window open for 60s...")
                time.sleep(60)
        logger.error(f"TikTok Upload Failed: {e}")
        set_account_state("tiktok", False, str(e))
        return False, str(e)
    finally:
        if driver: 
            try: driver.quit()
            except: pass

# --- LOCAL TESTING BLOCK ---
if __name__ == "__main__":
    # Paste your Session ID to test
    LOCAL_SESSION_ID = "YOUR_SESSION_ID_HERE" 
    LOCAL_VIDEO_PATH = "test_video.mp4" 
    LOCAL_DESC = "Test upload #pi #optimization"

    if LOCAL_SESSION_ID == "YOUR_SESSION_ID_HERE":
        print("ERROR: Paste your sessionid below the if __name__ block.")
    else:
        print("--- STARTING VISIBLE LOCAL TEST ---")
        if not os.path.exists(LOCAL_VIDEO_PATH):
            with open(LOCAL_VIDEO_PATH, 'wb') as f: f.write(b'0'*1024*1024) # Create dummy if missing
        
        upload(LOCAL_VIDEO_PATH, LOCAL_DESC, local_session_key=LOCAL_SESSION_ID)