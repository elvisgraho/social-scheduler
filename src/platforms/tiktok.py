"""
TikTok platform upload module using Selenium automation.

Supports both headless (server) and visible (local) modes with
session-based authentication and robust error handling.
"""
import requests
import logging
import mimetypes
import os
import sys
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Dict
from typing import Optional
from typing import Tuple
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Add current directory to path for local imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from tiktok_selenium_utils import (
    dismiss_shadow_cookies,
    find_file_input,
    handle_are_you_sure_exit,
    handle_continue_to_post,
    handle_standard_popups,
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
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    logger = logging.getLogger("tiktok_local")

    # Mock database functions
    def get_config(key, default=None):  # noqa: ARG001
        return default

    def get_json_config(key, default=None):  # noqa: ARG001
        return default

    def set_config(key, value):  # noqa: ARG001
        pass

    def set_json_config(key, value):  # noqa: ARG001
        pass

    def set_account_state(platform, status, msg):  # noqa: ARG001
        print(f"SET STATE: {platform} -> {status} ({msg})")

# --- CONFIGURATION ---
SESSION_KEY = "tiktok_session_bundle"
LEGACY_KEY = "tiktok_session_id"
VERIFICATION_INTERVAL_HOURS = 6
REFRESH_WARNING_DAYS = 25
# Standard User Agent (Identical to Desktop to avoid detection)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"

# Upload timeout constants (in iterations)
FILE_INPUT_SEARCH_TIMEOUT = 30  # 30 seconds  (30 iters × 1s)
UPLOAD_COMPLETE_TIMEOUT = 120   # 6 minutes   (120 iters × 3s)
POST_BUTTON_TIMEOUT = 30        # 60 seconds  (30 iters × 2s)
VERIFICATION_TIMEOUT = 120      # 60 seconds  (120 iters × 0.5s)

# Supported video formats
SUPPORTED_VIDEO_FORMATS = {'.mp4', '.mov', '.avi', '.webm', '.mkv', '.flv', '.wmv', '.m4v'}

# --- HELPER FUNCTIONS ---
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO string to timezone-aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        # If naive datetime, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

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
    else:
        set_config(LEGACY_KEY, "")

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
    except Exception:
        # In case driver is closed or script fails
        logger.info(message)

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
        except Exception:
            pass
                
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

def _validate_video_file(video_path: str) -> Tuple[bool, str]:
    """Validates that the file exists and is a supported video format."""
    path = Path(video_path)

    # Check file exists
    if not path.exists():
        return False, f"File not found: {video_path}"

    # Check it's a file, not directory
    if not path.is_file():
        return False, f"Path is not a file: {video_path}"

    # Check file extension
    if path.suffix.lower() not in SUPPORTED_VIDEO_FORMATS:
        return False, f"Unsupported format: {path.suffix}. Supported: {', '.join(SUPPORTED_VIDEO_FORMATS)}"

    # Check MIME type
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type and not mime_type.startswith('video/'):
        return False, f"File is not a video (detected: {mime_type})"

    # Check file size (TikTok limit: ~4GB)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > 4000:
        return False, f"File too large: {size_mb:.1f}MB (max 4000MB)"
    if size_mb < 0.1:
        return False, f"File too small: {size_mb:.2f}MB (likely corrupt)"

    return True, "Valid video file"

def upload(video_path: str, description: str, local_session_key: str = None):
    ok, session_id, info = ensure_session_valid(local_session=local_session_key)
    if not ok or not session_id:
        return False, info

    # Validate video file
    valid, msg = _validate_video_file(video_path)
    if not valid:
        return False, msg

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

        time.sleep(2)
        
        # --- 1. LOCATE FILE INPUT ---
        # Dismiss cookie banner immediately — it blocks the upload form on first load.
        # Run twice: once right after navigation and once more after a short wait in
        # case TikTok injects the banner asynchronously.
        dismiss_shadow_cookies(driver)
        handle_standard_popups(driver)

        _browser_log(driver, "Scanning for file input...")
        file_input = None
        in_iframe = False

        for i in range(FILE_INPUT_SEARCH_TIMEOUT):
            file_input, _ = find_file_input(driver)
            if file_input:
                break

            # Re-dismiss on every 3rd attempt in case banners reappear
            if i % 3 == 0:
                handle_standard_popups(driver)
                dismiss_shadow_cookies(driver)

            time.sleep(1)

        if not file_input:
            raise Exception("Could not locate file input after %d seconds" % FILE_INPUT_SEARCH_TIMEOUT)

        _browser_log(driver, "Uploading file...")
        driver.execute_script("arguments[0].style.display = 'block';", file_input)
        file_input.send_keys(str(Path(video_path).resolve()))
        time.sleep(5)

        # Return to main document regardless of whether we're inside an iframe
        driver.switch_to.default_content()

        # --- 2. WAIT LOOP ---
        _browser_log(driver, "Waiting for upload completion...")
        upload_complete = False

        # Pi optimization: Check less frequently (every 3s) to save CPU
        for i in range(UPLOAD_COMPLETE_TIMEOUT):
            # Only run heavy JS dismissal every few cycles to save Pi CPU
            if i % 2 == 0: 
                handle_are_you_sure_exit(driver, _browser_log)
                handle_standard_popups(driver)
            
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
            except Exception:
                pass
            time.sleep(3)

        if not upload_complete:
            raise Exception("Upload timed out - 'Replace' button never appeared")

        # --- 3. DESCRIPTION ---
        if description:
            try:
                _browser_log(driver, "Entering description...")
                handle_standard_popups(driver)
                
                caption_box = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".public-DraftEditor-content"))
                )
                
                # Center scroll to avoid 'Exit' triggers
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'instant'});", caption_box)
                time.sleep(1)
                
                # 1. Clear existing text safely
                actions = ActionChains(driver)
                actions.move_to_element(caption_box).click().pause(0.5)
                actions.key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).pause(0.5)
                actions.send_keys(Keys.BACKSPACE).pause(0.5)
                actions.perform()

                # 2. Type description with improved hashtag handling
                parts = description.split(' ')
                for part in parts:
                    actions = ActionChains(driver)

                    if part.startswith('#'):
                        # Extract the clean tag name (without #)
                        clean_tag = part[1:].lower()
                        _browser_log(driver, f"Processing hashtag: #{clean_tag}")
                        
                        # Type the hashtag character by character for better autocomplete triggering
                        actions.send_keys('#')
                        actions.pause(0.3)
                        
                        # Type tag characters one by one (triggers better autocomplete)
                        for char in clean_tag:
                            actions.send_keys(char)
                            actions.pause(0.1)
                        
                        actions.pause(2)  # Wait for TikTok autocomplete dropdown
                        
                        # Smart tag selection: Try to find exact match in dropdown
                        # Press DOWN multiple times to find better matches (TikTok often puts
                        # trending/irrelevant tags first, relevant ones lower)
                        tag_found = _select_best_hashtag(driver, clean_tag, actions)
                        
                        if tag_found:
                            _browser_log(driver, f"Hashtag #{clean_tag} selected successfully")
                        else:
                            _browser_log(driver, f"Hashtag #{clean_tag} - using best available suggestion")
                        
                        actions.pause(0.5)
                        actions.send_keys(' ')  # Add space after tag
                        actions.perform()
                    else:
                        # Normal word logic
                        actions.send_keys(part + " ")
                        actions.perform()
                    
                    time.sleep(0.15)  # Human-like typing speed

                _browser_log(driver, "Description entered. Waiting 3s for save...")
                time.sleep(3)  # Critical wait for Auto-Save
                    
            except Exception as e:
                logger.warning(f"Caption failed: {e}")

        # --- 4. POST ---
        _browser_log(driver, "Looking for Post button...")

        post_clicked = False
        for _ in range(POST_BUTTON_TIMEOUT):
            try:
                if handle_continue_to_post(driver, _browser_log):
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
                        if handle_continue_to_post(driver, _browser_log):
                            _browser_log(driver, "Modal appeared during scroll - dismissed, retrying...")
                            continue

                        if IS_LOCAL:
                            time.sleep(9999)

                        _browser_log(driver, "Clicking Post Button")
                        driver.execute_script("arguments[0].click();", post_btn)
                        post_clicked = True
                        _browser_log(driver, "Post button clicked. Moving to verification...")
                        break
            except Exception:
                pass
            time.sleep(2)

        if not post_clicked:
            raise Exception("Post button not found or could not be clicked")

        # --- 5. VERIFICATION ---
        _browser_log(driver, "Verifying upload...")

        # Success text patterns TikTok uses across UI variants
        _SUCCESS_TEXTS = [
            "Manage your posts",
            "Upload another video",
            "Post published",
            "Video uploaded",
            "Your video has been",
        ]

        def _check_success(drv) -> bool:
            """Return True if any success indicator is visible or the page has left the upload flow."""
            try:
                current_url = drv.current_url
                # URL left the /upload path — TikTok redirected after successful post
                if "upload" not in current_url:
                    return True
                page_text = drv.find_element(By.TAG_NAME, "body").text
                return any(phrase in page_text for phrase in _SUCCESS_TEXTS)
            except Exception:
                return False

        for _ in range(VERIFICATION_TIMEOUT):
            if _check_success(driver):
                set_account_state("tiktok", True, None)
                _browser_log(driver, "Upload Successful!")
                return True, "Upload Successful"

            # Handle any remaining "Post Now?" modals
            if handle_continue_to_post(driver, _browser_log):
                _browser_log(driver, "Handled modal during verification phase.")
                time.sleep(2)
                continue

            time.sleep(0.5)

        # Verification timed out, but the Post button WAS clicked.
        # Treat as probable success to prevent a retry that would double-post.
        # The video most likely went through — TikTok's UI was just slow/different.
        logger.warning(
            "TikTok verification timed out after Post was clicked. "
            "Treating as probable success to avoid double-post. "
            "Check TikTok manually to confirm."
        )
        set_account_state("tiktok", True, None)
        return True, "Upload Successful (verified by post click; confirmation page not detected — check TikTok manually)"

    except Exception as e:
        logger.error(f"TikTok Upload Failed: {e}")
        set_account_state("tiktok", False, str(e))

        if driver:
            try:
                _debug_dump(driver, "upload_failure")
            except Exception as dump_err:
                logger.warning(f"Failed to save debug dump: {dump_err}")

            if IS_LOCAL:
                logger.error("Error! Leaving window open for 60s...")
                try:
                    time.sleep(60)
                except KeyboardInterrupt:
                    logger.info("Debug wait interrupted by user")

        return False, str(e)

    finally:
        # Ensure browser is always cleaned up, even if errors occur
        if driver:
            try:
                driver.quit()
                logger.debug("WebDriver closed successfully")
            except Exception as quit_err:
                logger.warning(f"Failed to quit WebDriver cleanly: {quit_err}")
                # Force kill if normal quit fails - try multiple approaches
                try:
                    driver.service.stop()
                except Exception:
                    pass
                try:
                    if hasattr(driver, 'service') and hasattr(driver.service, 'process'):
                        driver.service.process.kill()
                except Exception:
                    pass


def _select_best_hashtag(driver, target_tag: str, actions: ActionChains, max_attempts: int = 5) -> bool:
    """
    Intelligently select the best matching hashtag from TikTok's dropdown.
    
    Strategy:
    1. First try to find an exact match by cycling through options
    2. If no exact match, accept the closest partial match
    3. Fall back to first suggestion if nothing matches
    
    Args:
        driver: Selenium WebDriver instance
        target_tag: The hashtag text without # prefix
        actions: ActionChains instance for keyboard input
        max_attempts: Maximum dropdown items to check
        
    Returns:
        True if a good match was found, False otherwise
    """
    try:
        # Wait for dropdown to appear
        time.sleep(1.5)
        
        # Try to find dropdown items
        dropdown_items = driver.find_elements(
            By.XPATH,
            "//div[contains(@class, 'tiktok-tag') or contains(@data-e2e, 'suggest') or contains(@class, 'suggest')]"
        )
        
        if not dropdown_items:
            # Fallback: Just press DOWN and ENTER (original behavior)
            actions.send_keys(Keys.DOWN)
            actions.pause(0.3)
            actions.send_keys(Keys.ENTER)
            return False
        
        # Search for best match in dropdown
        best_match_index = 0
        found_exact = False
        
        for i, item in enumerate(dropdown_items[:max_attempts]):
            try:
                item_text = item.text.lower().strip()
                # Check for exact match (tag text without #)
                if item_text == target_tag or item_text == f'#{target_tag}':
                    best_match_index = i
                    found_exact = True
                    break
                # Check for partial match (contains target)
                elif target_tag in item_text and not found_exact:
                    best_match_index = i
            except Exception:
                continue
        
        # Navigate to the best match
        for _ in range(best_match_index):
            actions.send_keys(Keys.DOWN)
            actions.pause(0.2)
        
        actions.send_keys(Keys.ENTER)
        return found_exact
        
    except Exception as e:
        logger.debug(f"Hashtag dropdown selection failed: {e}, using fallback")
        # Fallback: Simple DOWN + ENTER
        actions.send_keys(Keys.DOWN)
        actions.pause(0.3)
        actions.send_keys(Keys.ENTER)
        return False

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
