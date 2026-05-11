---
name: look
description: Use when you need to see a web page, verify UI visually, take a screenshot, execute JS in browser, click elements, fill forms, check console errors, monitor network, or interact with the JAINE Browser. Triggers on "open in browser", "take screenshot", "check if page works", "what does this look like", "run JS", "click button", "fill form", visual verification.
---

# JAINE Browser — Multi-Channel Browser Automation

**Core principle:** See what the user sees. Three channels — CDP WebSocket (primary), AppleScript + DOM injection (fallback), macOS native (screenshot). No extensions needed.

## Channels

| Channel | When | Capabilities |
|---------|------|-------------|
| **CDP WebSocket** | websocket-client installed | Everything: screenshot, JS, DOM, network, console, PDF |
| **AppleScript + DOM injection** | websocket-client missing | JS in main world, navigate, reload, click, fill, wait |
| **macOS native** | screenshot without websocket | screencapture via window ID |
| **AppleScript window** | macOS + Google Chrome | bounds, move between monitors, activate |

Auto-detected: `cdp.py status` shows active channel. Fallback is transparent — same commands work in both modes.

**No pip installs needed:** websocket-client is bundled in `scripts/vendor/`. Native screenshot fallback uses Quartz (PyObjC) — available in system/homebrew Python on macOS.

**Known Chrome behavior (Chromium #543437):** AppleScript `execute javascript` runs in isolated world — page variables invisible. Our DOM injection bridge solves this: injects `<script>` tag (runs in main world), writes result to `dataset`, reads back. Automatic in `cdp.py js`.

## Quick Reference — 17 Commands

```bash
CDP="${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py"

# Status & tabs
python3 "$CDP" status                    # ONLINE/OFFLINE + channel info
python3 "$CDP" tabs                      # list all tabs

# Navigation
python3 "$CDP" navigate URL              # go to URL
python3 "$CDP" open URL                  # new tab with URL
python3 "$CDP" reload                    # reload (cache bypass)

# See
python3 "$CDP" screenshot [FILE]         # screenshot → /tmp/jaine-screenshot.png
python3 "$CDP" title                     # page title
python3 "$CDP" html                      # full HTML (CDP only)

# Execute
python3 "$CDP" js 'EXPRESSION'           # JS in main world
python3 "$CDP" wait SELECTOR [TIMEOUT]   # wait for CSS selector
python3 "$CDP" click SELECTOR            # click element
python3 "$CDP" fill SELECTOR VALUE       # fill input + dispatch events

# Debug
python3 "$CDP" console                   # console messages (CDP only)
python3 "$CDP" network                   # network requests (CDP only)

# Generate
python3 "$CDP" pdf [FILE]                # save as PDF (CDP only)
python3 "$CDP" viewport WIDTH HEIGHT     # change viewport size

# Window management
python3 "$CDP" window bounds             # get current bounds
python3 "$CDP" window upper              # move to upper monitor
python3 "$CDP" window lower              # move to lower monitor
python3 "$CDP" window activate           # bring to front
```

## Browser Setup

JAINE Browser = separate Chrome instance, dark mode, no extensions:
- **CDP port:** 9333
- **Profile:** `/0/.jaine/.browser/profile/`
- **Launcher:** `${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh [URL]`

If `status` shows OFFLINE:
```bash
"${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh" "http://localhost:9401"
```

## Workflows

### Screenshot → Read → See
```bash
python3 "$CDP" screenshot /tmp/check.png
Read /tmp/check.png
```

### Click through UI
```bash
python3 "$CDP" navigate "http://localhost:9401/dashboard.html"
python3 "$CDP" wait ".tab" 5
python3 "$CDP" click ".tab[data-tab='sessions']"
python3 "$CDP" screenshot /tmp/sessions.png
```

### Fill form and submit
```bash
python3 "$CDP" fill "#search-input" "Харли память"
python3 "$CDP" click "#search-button"
python3 "$CDP" wait ".results" 10
```

### Debug page errors
```bash
python3 "$CDP" console
python3 "$CDP" network
python3 "$CDP" js "document.querySelectorAll('.error').length"
```

### Responsive testing
```bash
python3 "$CDP" viewport 375 812    # iPhone
python3 "$CDP" screenshot /tmp/mobile.png
python3 "$CDP" viewport 1440 900   # desktop
python3 "$CDP" screenshot /tmp/desktop.png
```

## Remote Machines

For Vivaldi on kosm4 (CDP :9222):
```bash
ssh kosm4 'CDP_PORT=9222 python3.13 cdp.py screenshot /tmp/shot.png'
```
Or SSH tunnel (run cdp.py locally): `ssh -L 9222:localhost:9222 kosm4`

## Logging

All actions → `~/.claude/hooks/bulldozer-look.log`:
```
2026-05-11T03:30:00+0700 | event=screenshot | channel=cdp | path=/tmp/page.png | size=339006
2026-05-11T03:30:05+0700 | event=js | channel=applescript | expr=document.title
2026-05-11T03:30:10+0700 | event=open | url=http://localhost:9401
```
Note: `channel=` is present on commands with CDP/AppleScript fallback (screenshot, js, navigate, reload, click, fill). Commands that use a single channel (open, wait, console, network, pdf, viewport, window) omit it.

Review: `column -t -s'|' ~/.claude/hooks/bulldozer-look.log`

## Fallback Matrix

| Command | CDP (websocket) | AppleScript fallback | macOS native |
|---------|:-:|:-:|:-:|
| status, tabs, open | HTTP only | — | — |
| js, title, click, fill, wait | WebSocket | DOM injection | — |
| navigate, reload | WebSocket | AppleScript | — |
| screenshot | WebSocket | — | screencapture |
| html, console, network, pdf | WebSocket | unavailable | — |
| viewport | WebSocket | window bounds (approximate) | — |
| window | — | AppleScript | — |
