import time
from selenium.common.exceptions import WebDriverException

def dismiss_shadow_cookies(driver):
    """
    FIXED: 
    1. Removed strict 'offsetWidth' checks that caused failures on loading elements.
    2. Uses 'textContent' fallback to ensure text is read even if hidden.
    3. Traverses deep Shadow DOMs without early aborting.
    """
    try:
        driver.execute_script("""
            function clickShadowCookies(root) {
                // 1. Try to find and click buttons in the current root
                try {
                    // Look for standard buttons and elements acting as buttons
                    let buttons = root.querySelectorAll('button, div[role="button"], input[type="button"], a[role="button"]');
                    
                    buttons.forEach(b => {
                        // Use textContent as fallback if innerText is empty (hidden elements)
                        let txt = (b.innerText || b.textContent || "").toLowerCase().trim();
                        
                        // Check for common keywords
                        if (txt.includes('allow all') || 
                            txt.includes('accept all') || 
                            txt.includes('accept cookies') || 
                            txt.includes('agree') || 
                            txt.includes('decline optional') || 
                            txt.includes('reject optional')) {
                            
                            // Check visibility: offsetParent is the standard check for 'is reachable'
                            // We do NOT check offsetWidth/Height as it fails on animating elements
                            if (b.offsetParent !== null) {
                                b.click();
                                console.log('Shadow cookie clicked:', txt);
                            }
                        }
                    });
                } catch(e) { console.error(e); }

                // 2. Traverse into Shadow Roots
                try {
                    // Optimized: specific query is impossible for shadow roots, so we must iterate
                    // heavily optimized for the Pi by not creating new variables inside the loop
                    let all = root.querySelectorAll('*');
                    for (let i = 0; i < all.length; i++) {
                        if (all[i].shadowRoot) {
                            clickShadowCookies(all[i].shadowRoot);
                        }
                    }
                } catch(e) {}
            }
            
            // Start the process
            clickShadowCookies(document);
        """)
    except WebDriverException: pass

def handle_standard_popups(driver) -> bool:
    """
    Optimized: Moves the Loop and XPath search entirely to JS.
    RPi Benefit: Replaces multiple HTTP requests with 1 single request.
    """
    try:
        did_dismiss = driver.execute_script("""
            var xpathTargets = [
                "//button[contains(translate(., 'C', 'c'), 'confirm')]", 
                "//button[contains(translate(., 'G', 'g'), 'got it')]",
                "//button[contains(translate(., 'O', 'o'), 'okay')]",
                "//div[@role='button'][contains(., 'Confirm')]" // Edge Case: Divs acting as buttons
            ];
            
            var dismissed = false;
            
            xpathTargets.forEach(xp => {
                try {
                    var result = document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                    for (var i = 0; i < result.snapshotLength; i++) {
                        var el = result.snapshotItem(i);
                        // Edge Case: Check visibility via offsetParent or computed style
                        var style = window.getComputedStyle(el);
                        if (el.offsetParent !== null && style.display !== 'none' && style.visibility !== 'hidden') {
                            el.click();
                            dismissed = true;
                        }
                    }
                } catch(e) {}
            });
            return dismissed;
        """)
        if did_dismiss:
            time.sleep(0.5)
            return True
    except WebDriverException: pass
    return False

def handle_continue_to_post(driver, logFunction) -> bool:
    """
    Optimized: Single JS call to check, log (via return), and click.
    """
    try:
        # Returns string "clicked" if successful, else null
        result = driver.execute_script("""
            var btns = document.evaluate("//button[contains(., 'Post now')]", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (var i = 0; i < btns.snapshotLength; i++) {
                var btn = btns.snapshotItem(i);
                if (btn.offsetParent !== null) {
                    btn.click();
                    return "clicked";
                }
            }
            return null;
        """)
        
        if result == "clicked":
            if logFunction:
                logFunction(driver, "Found 'Continue to post?' modal - Clicking 'Post now'")
            time.sleep(2)
            return True
    except WebDriverException: pass
    return False

def handle_are_you_sure_exit(driver, logFunction) -> bool:
    """
    Optimized: Broadened search to include H2/H3 and aria-labels.
    """
    did_dismiss = False
    try:
        dismissed = driver.execute_script("""
            // RPi Opt: Get headers by tag is fast
            var headers = document.querySelectorAll('h1, h2, h3, [role="heading"]');
            for (var i = 0; i < headers.length; i++) {
                var txt = headers[i].innerText.toLowerCase();
                // Edge Case: Text variations
                if (txt.includes('sure you want to exit') || txt.includes('discard post')) {
                    var dialog = headers[i].closest('div[role="dialog"]') || headers[i].closest('.modal') || headers[i].parentNode.parentNode;
                    if (dialog) {
                        var buttons = dialog.querySelectorAll('button');
                        for (var j = 0; j < buttons.length; j++) {
                            var bTxt = buttons[j].innerText.toLowerCase();
                            // Edge Case: "Keep editing" vs "Cancel"
                            if (bTxt.includes('cancel') || bTxt.includes('keep editing')) {
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
            if logFunction:
                logFunction(driver, "Dismissed 'Exit' modal")
            did_dismiss = True
            time.sleep(1)
    except WebDriverException: pass
    return did_dismiss



