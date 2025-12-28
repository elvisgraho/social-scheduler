import os
import time
import requests
import json
import shutil
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
    StaleElementReferenceException,
    ElementClickInterceptedException,
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
# Updated User Agent to match a modern Desktop (Chrome 123)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

logger = init_logging("tiktok")

# --- HELPER FUNCTIONS (DO NOT REMOVE OR RENAME) ---
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
    # Using mobile API info endpoint for lightweight validation
    url = "https://www.tiktok.com/passport/web/account/info/?aid=1459"
    headers = {"User-Agent": USER_AGENT, "Cookie": f"sessionid={session_id};"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        # "login_status" is often 0 for success in this endpoint
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


# --- ROBUST INTERACTION UTILS ---

def _inject_text_via_js(driver, element, text):
    """
    Safely injects text (including Emojis) into a contenteditable or input
    using Clipboard API simulation. This fixes 'broken emoji' issues on Linux.
    """
    try:
        # Escape backslashes and quotes for JS
        safe_text = json.dumps(text)
        driver.execute_script(f"""
            var elm = arguments[0];
            var txt = {safe_text};
            elm.focus();
            
            // Try standard insert first
            if (document.execCommand('insertText', false, txt)) return;
            
            // Fallback for newer React apps
            var val = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
            if (val && val.set) {{
                val.set.call(elm, txt);
            }} else {{
                elm.value = txt;
            }}
            elm.dispatchEvent(new Event('input', {{ bubbles: true }}));
            elm.dispatchEvent(new Event('change', {{ bubbles: true }}));
        """, element)
    except Exception as e:
        logger.warning(f"JS Inject failed, falling back to send_keys: {e}")
        element.send_keys(text)

def _nuke_overlays(driver):
    """
    Removes generic overlay masks that intercept clicks.
    """
    try:
        driver.execute_script("""
            document.querySelectorAll("div[class*='mask'], div[class*='overlay'], div[id*='modal-root']").forEach(el => {
                const rect = el.getBoundingClientRect();
                // Only nuke if it covers a significant area
                if (rect.width > 300 && rect.height > 300 && window.getComputedStyle(el).position === 'fixed') {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                }
            });
        """)
    except Exception:
        pass

def _dismiss_popups_aggressively(driver):
    """
    Eagle Eye: Scans for text buttons, close icons, and hits ESCAPE.
    """
    # 1. Hit Escape (Standard accessibility close)
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except:
        pass

    # 2. Click specific text buttons or Close SVGs
    keywords = ["Turn on", "Allow all", "Decline", "Got it", "Reload", "Upload", "Close", "No thanks"]
    script = """
        const keywords = arguments[0];
        const buttons = document.querySelectorAll('button, div[role="button"], div[class*="btn"], svg');
        let clicked = false;
        
        buttons.forEach(el => {
            if (el.offsetParent === null) return; // Hidden
            
            // Check text content
            if (el.innerText && keywords.some(k => el.innerText.includes(k))) {
                el.click();
                clicked = true;
                return;
            }
            // Check if it's a close icon (common SVG path patterns or class names)
            if (el.tagName === 'svg' && (el.innerHTML.includes('path') || el.getAttribute('class')?.includes('close'))) {
                // Click the parent button if possible
                let parent = el.closest('button') || el;
                parent.click();
                clicked = true;
            }
        });
        return clicked;
    """
    try:
        driver.execute_script(script, keywords)
    except Exception:
        pass

def _debug_dump(driver, queue_name="error"):
    try:
        ts = datetime.now().strftime("%H%M%S")
        debug_dir = os.path.join("data", "logs")
        os.makedirs(debug_dir, exist_ok=True)
        
        # 1. Save Screenshot
        screen_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.png")
        driver.save_screenshot(screen_path)
        
        # 2. Save Browser Console Logs (Vital for headless debugging)
        log_path = os.path.join(debug_dir, f"tiktok_{queue_name}_{ts}.log")
        logs = driver.get_log('browser')
        with open(log_path, "w", encoding="utf-8") as f:
            for entry in logs:
                f.write(f"{entry['level']}: {entry['message']}\n")
                
        logger.error(f"Debug artifacts saved: {screen_path}")
    except Exception:
        pass

def _find_chromedriver():
    """Auto-detect chromedriver path on Pi/Docker systems."""
    paths = [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/local/bin/chromedriver"
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    # Fallback to whatever is in PATH
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
    options.add_argument("--disable-dev-shm-usage") # Critical for Docker memory
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer") # Pi Optimization
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1280,720")
    options.add_argument(f"user-agent={USER_AGENT}")
    
    # Pi/Network Optimization: Eager strategy + blocking images
    options.page_load_strategy = 'eager'
    options.add_argument("--disk-cache-dir=/dev/null") 
    options.add_argument("--disable-application-cache")
    prefs = {
        "profile.managed_default_content_settings.images": 2, # Block images
        "profile.default_content_setting_values.notifications": 2, # Block notifications
        "profile.default_content_settings.popups": 0
    }
    options.add_experimental_option("prefs", prefs)
    
    # Stealth
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Enable browser logging
    options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})

    service_path = _find_chromedriver()
    service = Service(service_path)
    
    driver = None
    
    # Retry Loop for the entire process (resilience against random network blips)
    MAX_RETRIES = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt > 1:
                logger.warning(f"Retrying upload (Attempt {attempt})...")
                if driver: 
                    try: driver.quit() 
                    except: pass
            
            driver = webdriver.Chrome(service=service, options=options)
            
            # Stealth JS
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            })

            # 1. Authenticate
            driver.get("https://www.tiktok.com/404")
            driver.add_cookie({
                "name": "sessionid",
                "value": session_id,
                "domain": ".tiktok.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expiry": int(time.time()) + 31536000
            })
            
            # 2. Go to Upload
            logger.debug("Navigating to upload page...")
            driver.get("https://www.tiktok.com/upload?lang=en")
            
            # Wait for Title or Login Redirect
            try:
                WebDriverWait(driver, 20).until(
                    lambda d: "upload" in d.current_url or "login" in d.current_url
                )
            except TimeoutException:
                pass # Continue to check logic

            # 3. Validation Check
            if "login" in driver.current_url:
                logger.error("Session invalid: Redirected to Login.")
                set_account_state("tiktok", False, "Session expired (Redirected)")
                return False, "Session expired (Login Redirect)"

            # 4. Handle Popups
            _dismiss_popups_aggressively(driver)

            # 5. File Input (Robust finding)
            # Find input even if nested or hidden
            file_input = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
            )
            driver.execute_script("arguments[0].style.display = 'block';", file_input)
            file_input.send_keys(os.path.abspath(video_path))

            # 6. Wait for Upload Processing (Eagle Eye)
            logger.debug("Waiting for upload processing...")
            # We wait for the 'Change video' button, 'Uploaded' text, or 'Replace'
            WebDriverWait(driver, 300).until(
                EC.presence_of_element_located((By.XPATH, 
                    "//div[contains(@class, 'uploaded')] | //div[contains(text(), 'Uploaded')] | //div[contains(text(), '100%')] | //button[contains(text(), 'Replace')]"
                ))
            )
            
            # 7. Post-Upload Popup Clearing
            _dismiss_popups_aggressively(driver)

            # 8. Caption
            if description:
                try:
                    caption_box = WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, ".public-DraftEditor-content, [contenteditable='true']"))
                    )
                    # Click to focus
                    driver.execute_script("arguments[0].click();", caption_box)
                    time.sleep(0.5)
                    # Use robust injector for emojis
                    _inject_text_via_js(driver, caption_box, description)
                except Exception as e:
                    logger.warning(f"Caption minor error: {e}")

            # 9. Scroll Down
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")

            # 10. Wait for Post Button Enablement (The critical part)
            # We look for the button NOT having 'disabled' attribute or class
            logger.info("Waiting for copyright checks...")
            
            post_btn_xpath = "//button[contains(@class, 'btn-post') or contains(text(), 'Post') or @data-e2e='post_video_button']"
            can_click = False
            post_btn = None
            
            # Polling loop (Max 90s)
            for _ in range(90):
                _dismiss_popups_aggressively(driver)
                _nuke_overlays(driver)
                
                try:
                    btns = driver.find_elements(By.XPATH, post_btn_xpath)
                    if btns:
                        post_btn = btns[0]
                        # Check disabled state
                        is_disabled = post_btn.get_attribute("disabled")
                        aria_disabled = post_btn.get_attribute("aria-disabled")
                        classes = post_btn.get_attribute("class")
                        
                        if is_disabled is None and aria_disabled != "true" and "disabled" not in classes:
                            can_click = True
                            break
                except:
                    pass
                time.sleep(1)

            if not can_click:
                raise Exception("Post button never enabled (Copyright check stuck?)")

            # 11. Click Post
            logger.info("Clicking Post...")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", post_btn)
            time.sleep(1)
            try:
                post_btn.click()
            except:
                driver.execute_script("arguments[0].click();", post_btn)

            # 12. Verify Success
            # Wait for redirection to profile OR "Upload another" OR "Manage posts"
            WebDriverWait(driver, 45).until(
                lambda d: "upload" not in d.current_url or 
                          len(d.find_elements(By.XPATH, "//div[contains(., 'Manage your posts')]")) > 0 or
                          len(d.find_elements(By.XPATH, "//div[contains(., 'Upload another')]")) > 0
            )
            
            set_account_state("tiktok", True, None)
            logger.info("Upload Successful!")
            return True, "Upload Successful"

        except Exception as exc:
            err_msg = str(exc).split("\n")[0]
            if attempt == MAX_RETRIES:
                if driver: _debug_dump(driver, "upload_fatal")
                logger.error(f"TikTok Upload Failed after retries: {exc}")
                set_account_state("tiktok", False, err_msg)
                return False, err_msg
            else:
                logger.warning(f"Attempt {attempt} failed: {err_msg}. Retrying...")
                time.sleep(5)
        
        finally:
            if driver:
                try: driver.quit()
                except: pass