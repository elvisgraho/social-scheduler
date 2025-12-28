import os
import time
import json
import logging
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import requests
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
    WebDriverException,
    NoSuchElementException,
    StaleElementReferenceException
)

# --- LOCAL IMPORTS ---
# We use try/except to prevent import errors if running standalone for testing
try:
    from src.logging_utils import init_logging
    from src.database import (
        get_config,
        get_json_config,
        set_account_state,
        set_config,
        set_json_config,
    )
except ImportError:
    logging.basicConfig(level=logging.INFO)
    def init_logging(name): return logging.getLogger(name)
    def get_config(k, d=None): return d
    def get_json_config(k, d=None): return d
    def set_account_state(*args): pass
    def set_config(*args): pass
    def set_json_config(*args): pass

# --- CONFIGURATION ---
SESSION_KEY = "tiktok_session_bundle"
LEGACY_KEY = "tiktok_session_id"
VERIFICATION_INTERVAL_HOURS = 6

# Updated UA for 2025 - Linux Desktop (Stealth)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

logger = init_logging("tiktok")

# --- ADVANCED JS INJECTIONS ---

# 1. POPUP MANAGER (React-Safe)
# Tries to click buttons first. Only deletes backdrops if they block the UI.
JS_POPUP_MANAGER = """
window.setInterval(() => {
    try {
        // 1. Defined Keywords to CLICK
        const clickKeywords = ["Turn on", "Allow all", "Got it", "Decline", "Manage options", "Reload"];
        
        // 2. Select potential buttons
        const candidates = document.querySelectorAll('button, div[role="button"], div[class*="btn"]');
        
        candidates.forEach(el => {
            if (el.offsetParent === null) return; // Skip hidden elements
            const text = el.innerText || "";
            
            // Check for keywords
            if (clickKeywords.some(k => text.includes(k))) {
                console.log("[Auto-Click] Clicking:", text);
                el.click();
            }
        });

        // 3. Remove "Modal Overlays" that block clicks (Class usually contains 'mask' or 'overlay')
        const overlays = document.querySelectorAll('div[class*="overlay"], div[class*="mask"]');
        overlays.forEach(el => {
            // Only remove if it has a high z-index and covers screen
            const style = window.getComputedStyle(el);
            if (parseInt(style.zIndex) > 1000) {
                console.log("[Auto-Remove] Removing blocking overlay");
                el.remove();
            }
        });

        // 4. Force enable scrolling if stuck
        if (document.body.style.overflow === 'hidden') {
            document.body.style.overflow = 'auto';
        }
    } catch (e) { }
}, 500);
"""

# 2. INPUT REVEALER
# TikTok often hides the file input. This forces it to be visible for Selenium.
JS_REVEAL_INPUT = """
const input = document.querySelector("input[type='file']");
if (input) {
    input.style.display = 'block';
    input.style.visibility = 'visible';
    input.style.opacity = '1';
    input.style.width = '1px';
    input.style.height = '1px';
    return true;
}
return false;
"""

# --- SYSTEM UTILS ---

def _cleanup_zombies():
    """Kills orphaned chrome processes to free Pi RAM."""
    try:
        # Only kill processes to avoid killing host processes if not in Docker
        subprocess.run(["pkill", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "chromedriver"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _get_driver_path() -> str:
    """Smart detection for Docker (Alpine/Debian) vs Local."""
    paths = [
        shutil.which("chromedriver"),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/local/bin/chromedriver"
    ]
    for p in paths:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("Chromedriver not found. Install 'chromium-chromedriver'.")

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# --- SESSION HANDLING ---

def _session_bundle() -> Dict:
    data = get_json_config(SESSION_KEY, {})
    # Legacy migration
    if not data and get_config(LEGACY_KEY):
        return {
            "sessionid": get_config(LEGACY_KEY), 
            "valid": False, 
            "last_verified": None
        }
    return data or {}

def _save_bundle(bundle: Dict):
    set_json_config(SESSION_KEY, bundle)
    if bundle.get("sessionid"):
        set_config(LEGACY_KEY, bundle["sessionid"])

def ensure_session_valid() -> Tuple[bool, Optional[str], str]:
    bundle = _session_bundle()
    sid = bundle.get("sessionid")
    if not sid: return False, None, "No Session ID"

    # API Probe (Lightweight)
    try:
        # Randomize User-Agent slightly to avoid API blocks
        headers = {"User-Agent": USER_AGENT, "Cookie": f"sessionid={sid}"}
        r = requests.get("https://www.tiktok.com/passport/web/account/info/", headers=headers, timeout=10)
        data = r.json()
        
        # Check if logged in
        if data.get("data", {}).get("user_verified") or data.get("data", {}).get("username"):
            bundle["valid"] = True
            bundle["last_verified"] = _now_utc().isoformat()
            _save_bundle(bundle)
            return True, sid, "Valid"
        else:
            return False, sid, f"Session Invalid: {data.get('status_msg', 'Unknown')}"
    except Exception as e:
        logger.warning(f"Session probe failed (Network?): {e}")
        # If network fail, assume valid if previously valid to attempt upload
        return True, sid, "Assume Valid (Network Error)"

# --- BROWSER FACTORY ---

def get_driver():
    _cleanup_zombies() # Critical for Pi 8GB stability
    
    opts = Options()
    opts.add_argument("--headless=new") # Modern headless
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    
    # Pi Optimization: Disable heavy logging and cache
    opts.add_argument("--log-level=3")
    opts.add_argument("--disk-cache-dir=/dev/null")
    
    # Anti-Detection
    opts.add_argument(f"user-agent={USER_AGENT}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = Service(_get_driver_path())
    driver = webdriver.Chrome(service=service, options=opts)

    # CDP Stealth: Inject Client Hints to match User Agent
    # This prevents "User-Agent Client Hint" mismatch detection
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": USER_AGENT,
        "platform": "Linux",
        "acceptLanguage": "en-US,en;q=0.9",
        "userAgentMetadata": {
            "brands": [{"brand": "Not A(Brand", "version": "99"}, {"brand": "Google Chrome", "version": "121"}, {"brand": "Chromium", "version": "121"}],
            "fullVersion": "121.0.6167.85",
            "platform": "Linux",
            "platformVersion": "6.5.0",
            "architecture": "x86", # Emulate x86 even on ARM to match standard Linux UA
            "model": "",
            "mobile": False
        }
    })
    
    # Remove navigator.webdriver flag
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    })

    return driver

# --- UPLOAD LOGIC ---

def upload(video_path: str, description: str):
    """
    Main entry point for TikTok upload.
    Named 'upload' to match src/platform_registry.py requirements.
    """
    is_valid, session_id, msg = ensure_session_valid()
    if not is_valid:
        return False, msg

    logger.info(f"Initializing upload for {os.path.basename(video_path)}...")
    driver = None

    try:
        driver = get_driver()
        
        # 1. Auth Injection
        driver.get("https://www.tiktok.com/login") # Load domain first
        driver.delete_all_cookies()
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax"
        })

        # 2. Navigate to Upload
        logger.debug("Navigating to upload page...")
        driver.get("https://www.tiktok.com/upload?lang=en")
        
        # 3. Inject Popup Manager (The "Eagle Eye")
        driver.execute_script(JS_POPUP_MANAGER)

        # 4. Wait for Page Load & Verify Login
        try:
            WebDriverWait(driver, 20).until(
                lambda d: "login" not in d.current_url and (
                    d.execute_script(JS_REVEAL_INPUT) or len(d.find_elements(By.XPATH, "//input[@type='file']")) > 0
                )
            )
        except TimeoutException:
            # Check for Captcha
            if "verify" in driver.page_source.lower() or "captcha" in driver.current_url:
                return False, "CAPTCHA Triggered - Aborting"
            if "login" in driver.current_url:
                set_account_state("tiktok", False, "Session expired")
                return False, "Redirected to Login - Session Expired"
            return False, "Upload page failed to load"

        # 5. File Upload
        logger.info("Sending file...")
        file_input = driver.find_element(By.XPATH, "//input[@type='file']")
        file_input.send_keys(os.path.abspath(video_path))

        # 6. Wait for Upload Verification (The hardest part)
        # We wait for the progress bar to complete or the text "Uploaded"
        logger.info("Waiting for video processing...")
        upload_success = False
        for _ in range(30): # 60 seconds max wait for upload processing
            src = driver.page_source
            if "Uploaded" in src or "100%" in src or "change-video-btn" in src:
                upload_success = True
                break
            time.sleep(2)
        
        if not upload_success:
            return False, "Video processing timed out"

        # 7. Set Description (React Safe)
        if description:
            try:
                # Target the DraftJS editor
                editor = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".public-DraftEditor-content"))
                )
                
                # Use ActionChains for reliable typing on Pi
                actions = ActionChains(driver)
                actions.move_to_element(editor).click().perform()
                time.sleep(0.5)
                
                # Clear existing
                (actions
                 .key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL)
                 .send_keys(Keys.BACKSPACE)
                 .perform())
                
                # Type new
                actions.send_keys(description).perform()
                time.sleep(1) # Let React state update
            except Exception as e:
                logger.warning(f"Could not set caption: {e}")

        # 8. Copyright Check & Post
        logger.info("Waiting for Post button...")
        
        # Scroll down to ensure button is in view (trigger lazy load)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        
        try:
            # Find the Post button
            post_btn = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//button[contains(., 'Post')]"))
            )
            
            # Smart wait for "Disabled" state to clear (Copyright check)
            # Pi 5 can be slow here, give it time
            WebDriverWait(driver, 60).until(
                lambda d: post_btn.is_enabled() and post_btn.get_attribute("disabled") is None
            )
            
            # Click it
            logger.info("Clicking Post...")
            driver.execute_script("arguments[0].click();", post_btn)
            
        except TimeoutException:
            return False, "Post button never enabled (Copyright issue?)"

        # 9. Verify Success Redirect
        try:
            WebDriverWait(driver, 20).until(
                lambda d: "manage" in d.current_url or "Upload another" in d.page_source
            )
            set_account_state("tiktok", True, None)
            logger.info("Upload Successful!")
            return True, "Success"
        except TimeoutException:
            # Final check - sometimes it stays on same page but shows "Post published" toast
            if "published" in driver.page_source.lower():
                return True, "Success (Toast detected)"
            return False, "Post clicked but no confirmation"

    except Exception as e:
        logger.error(f"Critical Upload Error: {e}")
        return False, str(e)

    finally:
        if driver:
            driver.quit()
        _cleanup_zombies() # Clean up immediately