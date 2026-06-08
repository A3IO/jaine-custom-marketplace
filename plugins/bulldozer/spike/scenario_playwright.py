# spike/scenario_playwright.py — same scenario via Playwright connect_over_cdp.
# No blind sleeps: expect() auto-waits; click() waits for actionability.
# Usage: scenario_playwright.py <CDP_URL> <URL> [--expect-console-error]
import sys
from playwright.sync_api import sync_playwright, expect

cdp_url = sys.argv[1]                               # http://127.0.0.1:9360
url = sys.argv[2]                                   # http://127.0.0.1:9402/async-page.html
EXPECT_ERR = "--expect-console-error" in sys.argv   # R1-F3 parity

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(cdp_url)
    ctx = browser.contexts[0]
    pages = ctx.pages
    page = next((pg for pg in pages if "async-page" in pg.url), None) or (pages[0] if pages else ctx.new_page())  # R1-F4 pin
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url, wait_until="load")
    page.click("#load")
    expect(page.locator("#result")).to_have_text("loaded")    # auto-waits
    page.click("#delayed")                                    # auto-waits enabled (actionability)
    expect(page.locator("#delayed-result")).to_have_text("clicked")
    if EXPECT_ERR:
        page.click("#break")                                  # trigger ReferenceError
        page.wait_for_timeout(300)                            # same observation window as cdp poll tick
        detected = bool(errors)
        browser.close()
        print("PW_CONSOLE " + ("DETECTED" if detected else "MISSED")); sys.exit(0)  # measured (R2-F2)
    browser.close()
    if errors:
        print("PW_FAIL console=" + repr(errors)); sys.exit(1)
    print("PW_PASS"); sys.exit(0)
