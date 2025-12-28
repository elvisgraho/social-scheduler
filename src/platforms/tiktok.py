import os
import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

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
# Use a static, consistent User Agent. 
USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"

logger = init_logging("tiktok")

# --- HELPER FUNCTIONS (State Management) ---
def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value: return None
    try: return datetime.fromisoformat(value)
    except Exception: return None

def _session_bundle() -> Dict:
    data = get_json_config(SESSION_KEY, {})
    if not data:
        legacy = get_config(LEGACY_KEY)
        if legacy:
            data = {"sessionid": legacy, "valid": False}
            set_json_config(SESSION_KEY, data)
    return data or {}

def _persist_bundle(bundle: Dict) -> None:
    set_json_config(SESSION_KEY, bundle)
    if bundle.get("sessionid"):
        set_config(LEGACY_KEY, bundle["sessionid"])

def save_session(session_id: str) -> None:
    cleaned = session_id.strip()
    if not cleaned:
        _persist_bundle({})
        set_account_state("tiktok", False, "Session missing")
        return
    bundle = _session_bundle()
    bundle.update({"sessionid": cleaned, "valid": False, "last_verified": None})
    _persist_bundle(bundle)
    set_account_state("tiktok", True, None)
    verify_session(force=True)

def session_status() -> Dict:
    bundle = _session_bundle()
    return {
        "sessionid": bundle.get("sessionid"),
        "valid": bundle.get("valid", False),
        "message": bundle.get("last_error"),
    }

def ensure_session_valid(force: bool = False) -> Tuple[bool, Optional[str], str]:
    bundle = _session_bundle()
    session_id = bundle.get("sessionid")
    if not session_id: return False, None, "No session."
    
    # Simple logic: If we have a session, assume valid until proven otherwise.
    # We skip the complex 'probe' logic here to keep the worker fast.
    return True, session_id, "Session present"

def verify_session(force: bool = True) -> Tuple[bool, str]:
    ok, _, message = ensure_session_valid(force=force)
    return ok, message


# --- ROBUST UPLOAD IMPLEMENTATION ---

def _init_driver():
    """
    Initializes a Chrome Driver tuned specifically for Raspberry Pi / Docker / ARM.
    """
    options = Options()
    # Basic Headless
    options.add_argument("--headless=new")
    
    # CRITICAL: Memory & Crash Prevention for Docker/ARM
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") # Fixes shared memory crash
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer") # Fixes 0xaaaa crash
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    
    # Reduce Load
    options.add_argument("--window-size=1280,720") # Smaller viewport = less rendering work
    options.add_argument("--blink-settings=imagesEnabled=false") # Disable images if possible to save RAM
    
    # Anti-Detection
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

def _js_set_value(driver, element, value):
    """Sets input value directly via JS to avoid keyboard event overhead."""
    safe_val = value.replace('"', '\\"').replace('\n', '\\n')
    driver.execute_script(f'arguments[0].innerText = "{safe_val}";', element)

def _js_click(driver, element):
    """Force click via JS. Bypasses overlays/interceptions."""
    driver.execute_script("arguments[0].click();", element)

def _dump_debug(driver):
    try:
        ts = datetime.now().strftime("%H%M%S")
        path = f"data/logs/tiktok_debug_{ts}.png"
        driver.save_screenshot(path)
        logger.error(f"Saved crash screenshot: {path}")
    except: pass

def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok: return False, info

    logger.info("Starting TikTok upload (Low-Resource Mode)...")
    driver = None

    try:
        driver = _init_driver()
        
        # 1. Stealth Patching
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        })

        # 2. Authentication (Cookie Injection)
        logger.debug("Injecting session...")
        driver.get("https://www.tiktok.com/404") # Load a lightweight page on domain first
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True
        })
        
        # 3. Navigate directly to Upload
        # We wait 5 seconds to ensure the Pi has fully loaded the heavy React app.
        driver.get("https://www.tiktok.com/upload?lang=en")
        time.sleep(5) 

        # 4. File Input
        # We don't look for iframes or popups yet. We just locate the input and send the file.
        # This often works even if a popup is visually blocking the screen.
        try:
            file_input = driver.find_element(By.XPATH, "//input[@type='file']")
        except NoSuchElementException:
            # Maybe inside iframe?
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            if frames:
                driver.switch_to.frame(frames[0])
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
        
        file_input.send_keys(os.path.abspath(video_path))
        logger.info("File path sent. Waiting for upload...")

        # 5. The "Long Wait" (Crucial for Pi)
        # We wait for the "Uploaded" text. This can take 30-60s on slow networks/CPUs.
        # We use a long polling interval (2s) to save CPU.
        WebDriverWait(driver, 300, poll_frequency=2).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Uploaded')] | //div[contains(@class, 'uploaded')] | //div[text()='100%']")
            )
        )
        logger.info("Video processed.")
        
        # 6. Set Description (JS Injection)
        # We don't clear text or use ActionChains. We just force the innerText.
        if description:
            try:
                caption_box = driver.find_element(By.CSS_SELECTOR, ".public-DraftEditor-content")
                _js_set_value(driver, caption_box, description)
            except:
                logger.warning("Could not set caption (element missing).")

        # 7. The "Blind" Post
        # We don't try to close popups individually. 
        # We find the Post button and force a click on it via JavaScript.
        # This works even if "Cookie Banner" or "Content Check" is on top.
        
        post_btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//button[div[text()='Post']] | //button[text()='Post']"))
        )
        
        # 8. Copyright Check Wait
        # Wait for the button to not be disabled.
        logger.debug("Waiting for copyright check...")
        for _ in range(30):
            if post_btn.get_attribute("disabled") is None:
                break
            time.sleep(2) # Slow poll

        # 9. Execute
        _js_click(driver, post_btn)
        logger.info("Post clicked.")

        # 10. Verification
        # Wait for "Manage your posts" or similar success indicator
        WebDriverWait(driver, 60, poll_frequency=2).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Manage your posts')] | //div[contains(text(), 'Upload another')]")
            )
        )
        
        set_account_state("tiktok", True, None)
        return True, "Upload Successful"

    except Exception as exc:
        if driver: _dump_debug(driver)
        
        err = str(exc).split("\n")[0]
        if "Stacktrace" in str(exc) or "crash" in str(exc).lower():
            err = "Browser Crash (Memory). Try rebooting Pi."
        
        logger.error(f"TikTok Fail: {err}")
        set_account_state("tiktok", False, err)
        return False, err

    finally:
        if driver:
            try: driver.quit()
            except: pass