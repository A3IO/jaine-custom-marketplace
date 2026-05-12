# E2E Test Framework for /bulldozer:look

**Date:** 2026-05-11
**Status:** Approved (rev 2 — post-review fixes)
**Context:** PR review of `bulldozer/feat/multi-channel-look` found 84% of tests are source-grep (verify string presence, not behavior). Multi-channel fallback chain is never tested end-to-end.

## Problem

`cdp.py` has 17 CDP commands, 3 communication channels (WebSocket, AppleScript, macOS native), and multiple fallback paths. Existing tests verify structure (function names exist, strings appear in source) but never execute commands against a real browser. Bugs B1-B9 from the review would have been caught by behavioral tests.

## Design

### Components

**1. `tests/fixtures/test-page.html`**

Static HTML page with deterministic, testable elements:

| Element | Purpose | Selector |
|---------|---------|----------|
| Page title | `title` command | `<title>JAINE Test Page</title>` |
| Click button | `click` command | `#test-btn` with onclick → `data-clicked="true"` |
| Console button | `console` command | `#console-btn` with onclick → `console.log("TEST_MARKER")` |
| Text input | `fill` command | `#test-input` with oninput → `data-input-fired="true"` |
| Delayed element | `wait` command | `.delayed-element` appears after 2s via JS |
| Self-fetch | `network` command | `fetch("test-page.html")` on load (200, not 404) |
| Enough content | `screenshot`, `pdf`, `html` | Visible text, styled layout |

**Console timing note:** `console.log()` on page load fires BEFORE `Console.enable` — CDP Console domain only streams events after subscription. The console test must trigger a log message AFTER `cmd_console` connects. Solution: dedicated `#console-btn` that logs on click. Test sequence: `click #console-btn` → `console` → assert marker.

**Network note:** `cmd_network` does `Page.reload` after `Network.enable` (cdp.py:457), so it captures the reload's requests. `fetch("test-page.html")` ensures a 200 response in output (not a 404 from a non-existent endpoint).

**2. `tests/conftest.py`**

Session-scoped pytest fixtures:

- **`jaine_browser`** — First checks if JAINE Browser is already running (`GET http://localhost:9333/json/version`). If online — reuses it (no kill on teardown). If offline — launches `launch.sh`, polls until responsive (max 15s), marks as "we launched it" → kills on teardown only if we launched it.
- **`test_server`** — Starts `http.server.HTTPServer` on port 0 (random), serves `tests/fixtures/`, runs in daemon thread, shuts down on teardown.
- **`test_page_url(jaine_browser, test_server)`** — Returns `http://localhost:{port}/test-page.html`. Navigates browser to the page via `cdp.py navigate`.

Helper: `run_cdp(args)` — subprocess wrapper (already exists in test_cdp.py, extract to conftest).

**Browser safety:** `launch.sh` line 14 does `pkill -f "user-data-dir=$PROFILE_DIR"` — kills running JAINE Browser. The fixture avoids this by reusing an already-running browser. Only launches (and later kills) if no browser was running at test start.

**3. `tests/test_e2e.py`**

One test per command (all 17), plus edge cases:

```
# Status & tabs
test_status_shows_online         — rc=0, "ONLINE" in stdout
test_tabs_lists_test_page        — test page URL in output

# Navigation
test_navigate_to_server_root     — navigate to http://localhost:{port}/, verify URL changed via js
test_open_creates_new_tab        — open test-page.html, tabs count increases
test_reload_succeeds             — rc=0, "Reloaded" in stdout

# See
test_screenshot_creates_file     — file exists, >5KB, valid JPEG header
test_title_returns_page_title    — "JAINE Test Page"
test_html_returns_content        — "JAINE Test Page" in output

# Execute
test_js_returns_value            — js "2+2" → "4"
test_js_reads_dom                — js "document.title" → "JAINE Test Page"
test_click_triggers_handler      — click #test-btn, js reads data-clicked="true"
test_fill_sets_value             — fill #test-input "hello", js reads value
test_fill_dispatches_events      — fill, then js reads data-input-fired="true"
test_wait_finds_existing         — wait #test-btn 5 → rc=0
test_wait_timeout_missing        — wait #nonexistent 2 → rc=1

# Debug
test_console_captures_marker     — click #console-btn, then console → "TEST_MARKER" in output
test_network_captures_requests   — reload-based, test page URL + 200 in output

# Generate
test_pdf_creates_file            — file exists, starts with %PDF
test_viewport_changes_size       — viewport 375 812, js reads innerWidth

# Window
test_window_bounds_returns_coords — window bounds → rc=0, comma-separated numbers in output
```

21 tests. Each is independent (navigates back to test page if needed via fixture).

**navigate test:** Uses `http://localhost:{port}/` (server root) instead of `about:blank` — Chrome may block `about:blank` navigation via CDP in some contexts.

**4. `CLAUDE.md`** (plugin root)

Documents:
- Plugin overview (check + look skills)
- Testing doctrine: every new cdp.py command MUST have an e2e test
- How to run tests: `pytest tests/` (structural) or `pytest tests/test_e2e.py` (needs browser)
- Known issues from review (B1-B9, D1-D7, T1-T2)
- Development workflow

### Test Isolation

- E2e tests reuse running JAINE Browser if available, launch only if needed
- `test_server` uses random port → no conflicts
- Each test navigates to test page → clean state
- Temp files use `tmp_path` fixture → auto-cleanup
- Teardown only kills browser if the fixture launched it

### Existing Tests

`test_cdp.py` stays unchanged. It runs fast (0.8s), offline, no browser. The two test files serve different purposes:
- `test_cdp.py` — structural invariants (functions exist, error handling patterns)
- `test_e2e.py` — behavioral correctness (commands produce right results)

### Not In Scope

- AppleScript fallback testing (requires disabling websocket — separate concern)
- CI integration (no headed Chrome in CI)
- Performance testing

### Review Fixes (rev 2)

| # | Issue | Fix |
|---|-------|-----|
| 1 | console.log on load fires before Console.enable | Dedicated `#console-btn` with onclick trigger |
| 2 | `open` and `window` commands not tested | Added `test_open_creates_new_tab` and `test_window_bounds_returns_coords` |
| 3 | `fetch("/api/ping")` → 404 from HTTPServer | Changed to `fetch("test-page.html")` → 200 |
| 4 | `launch.sh` kills running browser | Fixture reuses running browser, launches only if offline |
| 5 | `about:blank` unreliable for navigate test | Navigate to `http://localhost:{port}/` instead |
