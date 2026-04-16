import time
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By


def dismiss_shadow_cookies(driver) -> bool:
    """
    Dismiss TikTok's cookie/consent banner.

    Key fixes vs the old version:
    - Removed `offsetParent !== null` check.  TikTok's banner is position:fixed
      so offsetParent is ALWAYS null for fixed elements — the old check silently
      prevented every click.
    - Targets `tiktok-cookie-banner` custom element directly before doing the
      generic shadow-DOM walk (faster, more reliable).
    - Uses getComputedStyle for visibility instead of the broken offsetParent heuristic.
    - Extended button-text list to match current TikTok copy.
    """
    try:
        clicked = driver.execute_script("""
            const ACCEPT_PHRASES = [
                'allow all cookies',
                'allow all',
                'accept all cookies',
                'accept all',
                'agree to all',
                'decline optional cookies',
                'decline optional',
                'reject optional',
            ];

            function isVisible(el) {
                const s = window.getComputedStyle(el);
                return s.display !== 'none'
                    && s.visibility !== 'hidden'
                    && s.opacity !== '0'
                    && parseFloat(s.opacity) > 0;
            }

            function tryClickIn(root) {
                const candidates = root.querySelectorAll(
                    'button, [role="button"], input[type="button"], a[role="button"]'
                );
                for (const el of candidates) {
                    const txt = (el.textContent || el.innerText || '').toLowerCase().trim();
                    if (ACCEPT_PHRASES.some(p => txt === p || txt.includes(p))) {
                        if (isVisible(el)) {
                            el.click();
                            console.log('[TIKTOK_BOT] Cookie clicked:', txt);
                            return true;
                        }
                    }
                }
                // Recurse into shadow roots
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot && tryClickIn(el.shadowRoot)) return true;
                }
                return false;
            }

            // 1. Check TikTok's specific custom element first (fastest path)
            const banner = document.querySelector('tiktok-cookie-banner');
            if (banner) {
                if (banner.shadowRoot && tryClickIn(banner.shadowRoot)) return true;
                if (tryClickIn(banner)) return true;
            }

            // 2. Generic document walk
            return tryClickIn(document);
        """)
        if clicked:
            time.sleep(0.5)
            return True
    except WebDriverException:
        pass
    return False


def handle_standard_popups(driver) -> bool:
    """
    Dismiss common TikTok interstitial popups in one JS round-trip.

    Targets (case-insensitive, visible elements only):
      - "Got it" / "OK" / "Okay" buttons
      - "Confirm" buttons / divs
      - Generic close (×) icons on floating dialogs
    """
    try:
        dismissed = driver.execute_script("""
            const TARGETS = [
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'got it')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'okay')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), ' ok')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm')]",
                "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'continue')]",
                "//div[@role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'confirm')]",
            ];

            let dismissed = false;
            for (const xp of TARGETS) {
                try {
                    const result = document.evaluate(
                        xp, document, null,
                        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
                    );
                    for (let i = 0; i < result.snapshotLength; i++) {
                        const el = result.snapshotItem(i);
                        const s = window.getComputedStyle(el);
                        if (s.display !== 'none' && s.visibility !== 'hidden') {
                            el.click();
                            dismissed = true;
                        }
                    }
                } catch(e) {}
            }
            return dismissed;
        """)
        if dismissed:
            time.sleep(0.5)
            return True
    except WebDriverException:
        pass
    return False


def handle_continue_to_post(driver, log_fn) -> bool:
    """
    Click the "Post now" / "Continue to post" confirmation modal if visible.
    Single JS round-trip with race-condition protection.
    """
    try:
        result = driver.execute_script("""
            const xpaths = [
                "//button[contains(normalize-space(.), 'Post now')]",
                "//button[contains(normalize-space(.), 'Continue to post')]",
            ];
            for (const xp of xpaths) {
                const snap = document.evaluate(
                    xp, document, null,
                    XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
                );
                for (let i = 0; i < snap.snapshotLength; i++) {
                    const btn = snap.snapshotItem(i);
                    if (btn && btn.isConnected && btn.offsetParent !== null) {
                        try { btn.click(); return "clicked"; } catch(e) {}
                    }
                }
            }
            return null;
        """)
        if result == "clicked":
            if log_fn:
                log_fn(driver, "Found 'Continue to post?' modal — clicking 'Post now'")
            time.sleep(2)
            return True
    except WebDriverException:
        pass
    return False


def handle_are_you_sure_exit(driver, log_fn) -> bool:
    """
    Dismiss the "Are you sure you want to exit?" / "Discard post" modal
    by clicking Cancel / Keep editing.
    """
    try:
        dismissed = driver.execute_script("""
            const headers = document.querySelectorAll('h1, h2, h3, [role="heading"]');
            for (const h of headers) {
                const txt = (h.textContent || h.innerText || '').toLowerCase();
                if (txt.includes('sure you want to exit') || txt.includes('discard post')) {
                    const dialog = h.closest('[role="dialog"]')
                                || h.closest('.modal')
                                || h.parentNode.parentNode;
                    if (!dialog) continue;
                    for (const btn of dialog.querySelectorAll('button')) {
                        const bTxt = (btn.textContent || btn.innerText || '').toLowerCase();
                        if (bTxt.includes('cancel') || bTxt.includes('keep editing')) {
                            btn.click();
                            return true;
                        }
                    }
                }
            }
            return false;
        """)
        if dismissed:
            if log_fn:
                log_fn(driver, "Dismissed 'Exit' modal")
            time.sleep(1)
            return True
    except WebDriverException:
        pass
    return False


def find_file_input(driver):
    """
    Locate the file input on TikTok's upload page.

    TikTok embeds its upload form inside an <iframe>.  This function:
      1. Always resets to default content first so repeated calls in the retry
         loop never start from a stale iframe context.
      2. Tries the main document first (handles any future layout changes).
      3. Scans all top-level iframes, staying inside the one that has the input
         so the caller can immediately send_keys to it.

    Returns (input_element, in_iframe: bool) or (None, False) on failure.
    On failure the driver is always in default content.
    Uses a single JS call per iframe to minimise Pi round-trips.
    """
    # Always start from a clean context — critical when called in a retry loop.
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # 1. Main document (quick check — free, handles future TikTok layout changes)
    try:
        el = driver.find_element(By.XPATH, "//input[@type='file']")
        if el:
            return el, False
    except Exception:
        pass

    # 2. Top-level iframe scan.
    # iframes are fetched from default content, so they reference the right frames.
    try:
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
    except Exception:
        return None, False

    for frame in iframes:
        try:
            driver.switch_to.frame(frame)
            el = driver.execute_script(
                "return document.querySelector('input[type=\"file\"]');"
            )
            if el:
                # Leave driver inside this iframe so the caller can send_keys.
                return el, True
            # Input not in this frame — go back to default before trying the next.
            driver.switch_to.default_content()
        except Exception:
            # Frame switch or JS failed — reset and continue.
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    # Nothing found — driver is in default content.
    return None, False
