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
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, 
    NoSuchElementException, 
    StaleElementReferenceException,
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
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

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


# --- SMART JS INTERACTION UTILS ---

def _js_click_text(driver, text_signature):
    """
    Scans ALL buttons and clickable divs for specific text and clicks via JS.
    This bypasses complex nested HTML structures.
    """
    try:
        driver.execute_script(f"""
            const keywords = ["{text_signature}"];
            const elements = document.querySelectorAll('button, div[role="button"], div[class*="btn"]');
            
            for (let el of elements) {{
                if (el.innerText.includes("{text_signature}") && el.offsetParent !== null) {{
                    el.click();
                    console.log("JS Clicked:", el);
                    return;
                }}
            }}
        """)
        return True
    except Exception:
        return False

def _dismiss_popups_aggressively(driver):
    """
    Attempts to click known 'Compliance' buttons using both XPath and JS.
    """
    logger.debug("Scanning for popups to accept...")
    
    # 1. XPath approach (Broad match using '.')
    targets = [
        "//button[contains(., 'Turn on')]",   # Content checks
        "//button[contains(., 'Allow all')]", # Cookies
        "//button[contains(., 'Decline')]",   # Cookies Alt
        "//button[contains(., 'Got it')]",    # Feature tour
        "//button[contains(., 'Reload')]",    # Network error
        "//div[contains(., 'Upload') and contains(@class, 'modal')]" # Confirmation
    ]
    
    for xpath in targets:
        try:
            # Find all matching
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    time.sleep(1)
        except Exception:
            pass

    # 2. JS Text Search approach (Backup)
    _js_click_text(driver, "Turn on")
    _js_click_text(driver, "Allow all")
    _js_click_text(driver, "Got it")

def _debug_dump(driver, queue_name="error"):
    try:
        ts = datetime.now().strftime("%H%M%S")
        debug_dir = os.path.join("data", "logs")
        os.makedirs(debug_dir, exist_ok=True)
        screen_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.png")
        driver.save_screenshot(screen_path)
        logger.error(f"Debug screenshot saved: {screen_path}")
    except Exception:
        pass

# --- UPLOAD FUNCTION ---

def upload(video_path: str, description: str):
    ok, session_id, info = ensure_session_valid()
    if not ok or not session_id:
        return False, info

    logger.info("Starting TikTok upload for %s...", os.path.basename(video_path))

    # 1. Options (Optimized for Pi Stability)
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={USER_AGENT}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service("/usr/bin/chromedriver")
    
    driver = None
    try:
        driver = webdriver.Chrome(service=service, options=options)
        
        # 2. Stealth
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        })

        # 3. Authenticate
        driver.get("https://www.tiktok.com")
        driver.add_cookie({
            "name": "sessionid",
            "value": session_id,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": True
        })
        
        driver.refresh()
        time.sleep(3)
        
        # 4. Navigate to Upload
        driver.get("https://www.tiktok.com/upload?lang=en")
        
        # 5. Handle Initial Popups
        _dismiss_popups_aggressively(driver)

        # 6. File Input
        # Wait for input to exist
        file_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
        file_input.send_keys(os.path.abspath(video_path))

        # 7. Wait for Upload Verification
        logger.debug("Waiting for upload...")
        # Wait for success indicator
        WebDriverWait(driver, 180).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Uploaded')] | //div[contains(@class, 'uploaded')] | //div[text()='100%']")
            )
        )
        
        # 8. Mid-Process Popups (Content Checks)
        _dismiss_popups_aggressively(driver)

        # 9. Caption
        try:
            # Locate editor
            caption_box = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".public-DraftEditor-content"))
            )
            # Focus
            driver.execute_script("arguments[0].click();", caption_box)
            time.sleep(0.5)
            # Select All -> Backspace (Robust clearing)
            ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
            time.sleep(0.5)
            # Type text
            if description:
                ActionChains(driver).send_keys(description).perform()
        except TimeoutException:
            logger.warning("Caption box not reachable, skipping.")

        # 10. Scroll
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

        # 11. Find Post Button & Wait for Enablement
        # Use simple text matching for robustness
        post_btn_xpath = "//button[contains(., 'Post')]"
        
        post_btn = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, post_btn_xpath))
        )
        
        logger.debug("Waiting for Post button enablement...")
        # Loop for up to 60 seconds waiting for button to enable AND clearing popups
        for _ in range(60):
            # 1. Clear any blocking popups
            _dismiss_popups_aggressively(driver)
            
            # 2. Check if button is enabled
            if post_btn.get_attribute("disabled") is None:
                logger.info("Button enabled.")
                break
            time.sleep(1)

        # 12. Click Post (JS Force)
        logger.info("Clicking Post...")
        driver.execute_script("arguments[0].click();", post_btn)

        # 13. Verify Success
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(., 'Manage your posts')] | //div[contains(., 'uploaded')] | //div[contains(., 'Upload another')]")
            )
        )
        
        set_account_state("tiktok", True, None)
        return True, "Upload Successful"

    except Exception as exc:
        if driver:
            _debug_dump(driver, "upload_failure")
            
        err_msg = str(exc).split("\n")[0]
        if "Stacktrace" in str(exc) or "crash" in str(exc).lower():
            err_msg = "Browser Crash (Memory/Driver). Check debug screenshot."
        elif "StaleElementReference" in str(exc):
            err_msg = "UI updated unexpectedly. Retrying next cycle."
            
        logger.error(f"TikTok Upload Failed: {exc}")
        set_account_state("tiktok", False, err_msg)
        return False, err_msg
        
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass