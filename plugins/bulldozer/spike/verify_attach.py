# spike/verify_attach.py — attach to a running lane, pin a tab by url, print which one.
import sys
from playwright.sync_api import sync_playwright

cdp_url = sys.argv[1]                              # e.g. http://127.0.0.1:9360
want = sys.argv[2] if len(sys.argv) > 2 else ""    # url substring to pin (e.g. async-page); "" = any
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(cdp_url)
    ctx = browser.contexts[0]
    pages = ctx.pages
    page = next((pg for pg in pages if want and want in pg.url), None) or (pages[0] if pages else ctx.new_page())
    print("ATTACH_OK target=" + repr(page.url) + " title=" + repr(page.title()))
    browser.close()
