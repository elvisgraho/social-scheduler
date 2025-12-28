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


# --- SAFE SELENIUM UTILS ---

def _wait_for_spinner(driver):
    """Waits for the loading spinner (dots) to disappear."""
    try:
        # Generic loader detection
        WebDriverWait(driver, 5).until(
            EC.invisibility_of_element_located((By.XPATH, "//div[contains(@class, 'loader')] | //div[contains(@class, 'loading')]"))
        )
    except Exception:
        pass # If check fails, assume loaded or spinner not found

def _safe_click(driver, xpath, timeout=10):
    """
    Tries to click an element. If StaleElementReference occurs, 
    it re-finds the element and retries up to 3 times.
    """
    for attempt in range(3):
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            element.click()
            return True
        except StaleElementReferenceException:
            logger.debug(f"Stale element on click {xpath}, retrying ({attempt+1})...")
            time.sleep(1)
        except ElementClickInterceptedException:
            # Fallback to JS click if overlapped
            try:
                element = driver.find_element(By.XPATH, xpath)
                driver.execute_script("arguments[0].click();", element)
                return True
            except:
                pass
            time.sleep(1)
        except Exception:
            return False
    return False

def _dismiss_popups_safely(driver):
    """
    Targeted removal of known obstructions without destroying app state.
    """
    logger.debug("Checking for blocking dialogs...")
    
    # 1. Try Clicking "Cancel" or "Decline" buttons first (Best Practice)
    buttons = [
        "//button[contains(text(), 'Decline optional cookies')]",
        "//button[contains(text(), 'Allow all')]", # Sometimes easier to just allow to clear it
        "//button[contains(text(), 'Cancel')]",
        "//button[contains(text(), 'Got it')]"
    ]
    
    for btn_xpath in buttons:
        _safe_click(driver, btn_xpath, timeout=2)

    # 2. Gentle DOM Cleanup (Only remove masks/overlays, not dialog containers)
    try:
        driver.execute_script("""
            // Remove dark backdrops that capture clicks
            document.querySelectorAll('div[class*="mask"], div[class*="overlay"]').forEach(el => el.remove());
            
            // Remove specific cookie banners if buttons failed
            document.querySelectorAll('div[id*="cookie-banner"], div[class*="cookie-banner"]').forEach(el => el.remove());
        """)
    except Exception:
        pass


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

    # 1. Options
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
        _wait_for_spinner(driver)

        # 5. Handle Initial Popups
        _dismiss_popups_safely(driver)

        # 6. Iframe Handling
        try:
            iframe = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src, 'upload')]"))
            )
            driver.switch_to.frame(iframe)
            logger.debug("Switched to upload iframe.")
        except TimeoutException:
            pass

        # 7. File Input (Retry Logic)
        time.sleep(2)
        
        # Find input (retry loop to handle dynamic loading)
        file_input = None
        for _ in range(3):
            try:
                file_input = driver.find_element(By.XPATH, "//input[@type='file']")
                break
            except Exception:
                time.sleep(1)
        
        if not file_input:
            raise Exception("Could not find file input element.")

        file_input.send_keys(os.path.abspath(video_path))

        # 8. Wait for Upload Verification
        logger.debug("Waiting for upload verification...")
        WebDriverWait(driver, 180).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Uploaded')] | //div[contains(@class, 'uploaded')] | //div[text()='100%']")
            )
        )
        
        # 9. Handle Post-Upload Dialogs
        _dismiss_popups_safely(driver)

        # 10. Caption
        try:
            caption_box = driver.find_element(By.CSS_SELECTOR, ".public-DraftEditor-content")
        except NoSuchElementException:
            try:
                caption_box = driver.find_element(By.XPATH, "//div[@contenteditable='true']")
            except NoSuchElementException:
                caption_box = None
            
        if caption_box and description:
            try:
                driver.execute_script("arguments[0].click();", caption_box)
                time.sleep(0.5)
                ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
                time.sleep(0.5)
                ActionChains(driver).send_keys(description).perform()
            except Exception:
                logger.warning("Caption entry failed, skipping.")

        # 11. Final Scroll & Popup Check
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        _dismiss_popups_safely(driver)

        # 12. Robust Post Click
        # We define the xpath for the post button
        post_btn_xpath = "//button[div[text()='Post']] | //button[text()='Post']"
        
        # Wait for it to be visible
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, post_btn_xpath)))
        
        # Wait for "Disabled" state to clear (Copyright checks)
        logger.debug("Waiting for button enablement...")
        for _ in range(40):
            try:
                btn = driver.find_element(By.XPATH, post_btn_xpath)
                if btn.get_attribute("disabled") is None:
                    break
            except StaleElementReferenceException:
                pass # Refetch on next loop
            time.sleep(1)

        # Use safe click wrapper which handles StaleElement internally
        logger.info("Clicking Post...")
        if not _safe_click(driver, post_btn_xpath):
            raise Exception("Failed to click Post button after multiple attempts.")

        # 13. Verify Success
        WebDriverWait(driver, 60).until(
            EC.presence_of_element_located(
                (By.XPATH, "//div[contains(text(), 'Manage your posts')] | //div[contains(text(), 'Your video is being uploaded')] | //div[contains(text(), 'Upload another')]")
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
            err_msg = "UI updated unexpectedly (Stale Element). Retrying next cycle."
            
        logger.error(f"TikTok Upload Failed: {exc}")
        set_account_state("tiktok", False, err_msg)
        return False, err_msg
        
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass