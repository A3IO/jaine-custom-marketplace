---
name: look
description: Use when you need to see a web page, verify UI visually, take a screenshot, execute JS in browser, check if a page loads correctly, or interact with the JAINE Browser via CDP. Triggers on "open this in browser", "take a screenshot", "check if this page works", "what does this look like", "run JS in browser", visual verification of HTML/dashboard.
---

# JAINE Browser — Visual Verification

**Core principle:** See what the user sees. CDP :9333 is your eyes — screenshot, JS execution, DOM inspection, navigation. No extensions needed, no manual setup.

## When to Use

- Verify HTML/dashboard renders correctly after changes
- Screenshot a page for visual comparison
- Execute JS to check page state (DOM, variables, errors)
- Navigate to URL and inspect result
- Wait for element to appear after async load
- Debug why a page looks wrong
- Visual QA before showing to user

## Quick Start

```bash
# Check browser is running
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" status

# Not running? Launch it:
"${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh" "http://localhost:9401"

# Screenshot current page
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" screenshot /tmp/page.png
# Then: Read /tmp/page.png (you see the image)

# Execute JS
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" js "document.title"
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" js "document.querySelectorAll('.error').length"

# Navigate
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" navigate "http://localhost:9401/page.html"

# Open in new tab
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" open "http://example.com"

# Wait for element
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" wait ".content-loaded" 15

# Reload (cache bypass)
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" reload

# List tabs
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" tabs

# Get full HTML
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" html
```

## Browser Setup

JAINE Browser = separate Chrome instance with dedicated profile:
- **CDP port:** 9333 (always)
- **Profile:** `/0/.jaine/.browser/profile/` (no extensions, no sync)
- **Launcher:** `${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh [URL]`
- Dark mode, force-dark for all pages

If `cdp.py status` shows OFFLINE — run `launch.sh` once. Browser persists until killed.

## Workflow: Screenshot → Read → See

```bash
# 1. Take screenshot
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" screenshot /tmp/check.png
# 2. See the screenshot (Claude Code multimodal)
Read /tmp/check.png
# 3. Analyze what you see, report to user
```

This is the primary visual verification loop. Use it after ANY UI change.

## Workflow: JS State Check

```bash
# Check page data
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" js "(function(){ return JSON.stringify({title: document.title, errors: document.querySelectorAll('.error').length, ready: typeof DATA !== 'undefined'}) })()"
```

## Window Placement

Move browser to specific monitor via AppleScript (if enabled):
```bash
osascript -e 'tell application "Google Chrome" to set bounds of window 1 to {0, -1080, 3840, 0}'
```
Upper Odyssey monitor: `{0, -1080, 3840, 0}`. Main (lower): `{0, 0, 3840, 1080}`.

## Remote Machines (kosm4)

For Vivaldi on kosm4 (CDP :9222):
```bash
ssh kosm4 'python3.13 /tmp/cdp_script.py'
```
Or use CDP via SSH tunnel: `ssh -L 9222:localhost:9222 kosm4`

## Logging

All actions logged to `~/.claude/hooks/bulldozer-look.log`:
```
2026-05-10T12:30:00+0700 | event=screenshot | path=/tmp/page.png | size=339006 | url=http://localhost:9401
2026-05-10T12:30:05+0700 | event=js | expr=document.title | type=string
2026-05-10T12:30:10+0700 | event=navigate | url=http://localhost:9401/new-page.html
```

Review: `column -t -s'|' ~/.claude/hooks/bulldozer-look.log`

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Browser not running | `cdp.py status` first, then `launch.sh` if OFFLINE |
| Wrong browser (Yandex/Safari) | JAINE Browser = Chrome on :9333. User's browser is separate |
| Screenshot too fast after navigate | Add `sleep 2` or use `cdp.py wait SELECTOR` |
| CORS on fetch in page | Asset server must have `Access-Control-Allow-Origin: *` |
| AppleScript "disabled" error | AppleScript needs manual one-time enable. Use CDP instead |
