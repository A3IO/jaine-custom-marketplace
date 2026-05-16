---
name: look
description: Use when you need to see a web page, verify UI visually, take a screenshot, capture a region of the page, execute JS in a real browser, click elements, fill forms, check console errors, or monitor network requests. Triggers on "open in browser", "take screenshot", "capture region", "check if X is aligned", "what does this look like", "захватить область", "run JS", "click button", "fill form", visual verification, UI detail check.
argument-hint: "[URL] [task description]"
allowed-tools: ["Bash", "Read"]
---

# JAINE Browser — Multi-Channel Browser Automation

**Core principle:** See what the user sees. Three channels — CDP WebSocket (primary), AppleScript + DOM injection (fallback), macOS native (screenshot). No extensions needed.

## Quick Invoke (`/bulldozer:look [URL [task description]]`)

**Parse `$ARGUMENTS` first.** Extract the first URL-shaped token — that is the URL passed to scripts. The remaining text is task description you keep as your own brief; do **not** pass it to `launch.sh` or `cdp.py navigate`. A URL-shaped token starts with `http://`, `https://`, `file://`, or matches a `host:port/...` form. If `$ARGUMENTS` contains no URL token, treat the whole string as task description and skip steps 2–3 (browser opens at `about:blank`).

Example: `/bulldozer:look file:///tmp/page.html — проверить рендеринг таблицы` →
- URL = `file:///tmp/page.html`
- Task description (your own note, not passed to scripts) = `проверить рендеринг таблицы`

Why: `launch.sh` reads `$1` verbatim as the URL and `cdp.py navigate` does the same. Passing the full `$ARGUMENTS` produces a malformed URL whenever the user adds a description.

1. Check browser status:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" status
```

2. If OFFLINE, launch with the parsed URL (or `about:blank` if none):
```bash
"${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh" "<parsed URL or about:blank>" &
sleep 5
```

3. If browser was already ONLINE and a URL was parsed, navigate:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" navigate "<parsed URL>"
sleep 2
```
Skip step 3 if you just launched the browser in step 2 (it already opened the URL).

4. Screenshot + show:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" screenshot /tmp/jaine-look.jpg
```
Then Read the screenshot file to see it.

5. Report what you see to the user.

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
python3 "$CDP" screenshot [FILE] [--full-page] [--clip X Y W H] [--scale N]
  # screenshot (--full-page for below-fold; --clip for region; --scale 1 for CSS-pixel output)
  # Always prints "PATH  W×H" on stdout — verify actual dimensions before any cropping.
python3 "$CDP" title                     # page title
python3 "$CDP" html                      # full HTML (CDP only)

# Execute
python3 "$CDP" js 'EXPRESSION'           # JS in main world
python3 "$CDP" wait [--js] SELECTOR_OR_EXPR [TIMEOUT]  # CSS selector; --js for JS expression
python3 "$CDP" click SELECTOR            # click element
python3 "$CDP" fill SELECTOR VALUE       # fill input + dispatch events

# Debug
python3 "$CDP" console                   # console messages + uncaught exceptions (CDP only)
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

JAINE Browser = separate Chrome instance, no extensions:
- **CDP port:** 9333
- **Profile:** `/0/.jaine/.browser/profile/`
- **Launcher:** `${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh [URL]`
- **Rendering fidelity:** Chrome's Auto Dark Mode (`WebContentsForceDark`) is **off** — screenshots reflect the page as a normal user sees it. The browser UI (window/tabs/menus) follows the OS appearance setting; the CDP capture path never includes browser UI anyway.

If `status` shows OFFLINE:
```bash
"${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh" "http://localhost:9401"
```

## Decision Rules (for JAINE)

**screenshot:**
- `--full-page` — verify below-fold content (long pages, tables, grids). Omit for quick viewport checks.
- `--clip X Y W H` — capture a CSS-pixel region (mutually exclusive with `--full-page`). Use for UI-detail verification. Output dimensions are W × H × native DPR by default — pair with `--scale 1` for exact W×H CSS-pixel output.
- `--scale N` — opt-in output resolution. Default = native DPR (Retina ≈ 2× preserves UI detail — that is the skill's main use case). `--scale 1` produces CSS-pixel output (1440×900 instead of 2880×1800 on Retina) via `clip.scale = N / window.devicePixelRatio`. `Emulation.setDeviceMetricsOverride{deviceScaleFactor:1}` was tried first — it does **not** affect capture output size, only `window.devicePixelRatio` for the page's JS.
- `stdout` — every screenshot prints `PATH  W×H` (e.g. `/tmp/x.jpg  2880×1626`). Read those dimensions before computing any external crop — on Retina the captured image is wider than the logical viewport.
- If WARNING appears on stderr — page dimensions unavailable, retry after `wait`.

**wait:** Default = CSS selector (`wait ".results" 10`). Use `--js` only for JS expressions that aren't DOM selectors (`wait --js "DATA !== null" 10`, `wait --js "document.readyState === 'complete'" 5`). Never use `--js` with CSS selectors — it will fail.

**console:** Run `console` FIRST when a page loads blank or broken — it shows uncaught exceptions (TypeError, ReferenceError) that `js` cannot see. Output format: `[error] message` for console.error, `[exception] description — file:line:col` for uncaught exceptions.

**click vs js:** Prefer `click SELECTOR` over `js "querySelector(...).click()"` — click reports the element tag and handles NOT_FOUND with clear error. Use `js` only for complex multi-step DOM manipulation.

**wait + screenshot pattern:** After `navigate`, always `wait` before `screenshot` — pages with async data need time to render:
```bash
python3 "$CDP" navigate URL
python3 "$CDP" wait --js "DATA !== null" 10
python3 "$CDP" screenshot /tmp/result.jpg
```

## Workflows

### Screenshot → Read → See
```bash
python3 "$CDP" screenshot /tmp/check.jpg
Read /tmp/check.jpg
```

### Click through UI
```bash
python3 "$CDP" navigate "http://localhost:9401/dashboard.html"
python3 "$CDP" wait ".tab" 5
python3 "$CDP" click ".tab[data-tab='sessions']"
python3 "$CDP" screenshot /tmp/sessions.jpg
```

### Fill form and submit
```bash
python3 "$CDP" fill "#search-input" "Харли память"
python3 "$CDP" click "#search-button"
python3 "$CDP" wait ".results" 10
```

### Wait for async data
```bash
python3 "$CDP" wait --js "DATA !== null" 10
python3 "$CDP" wait --js "document.readyState === 'complete'" 5
```

### Debug page errors
```bash
python3 "$CDP" console
python3 "$CDP" network
python3 "$CDP" js "document.querySelectorAll('.error').length"
```

### Full-page screenshot
```bash
python3 "$CDP" screenshot /tmp/full.jpg --full-page
```

### Region capture for UI-detail checks
```bash
python3 "$CDP" viewport 1440 900               # normalize DPR to 1
python3 "$CDP" screenshot /tmp/region.jpg --clip 100 200 240 160
# stdout: /tmp/region.jpg  240×160
```

### CSS-pixel screenshot (opt-in 1:1 output)
```bash
python3 "$CDP" screenshot /tmp/css.jpg --scale 1
# stdout: /tmp/css.jpg  1440×900   (instead of 2880×1800 on Retina)
# Implementation: clip.scale = N / window.devicePixelRatio. No side effects
# on subsequent commands — each screenshot computes scale independently.
```

### Responsive testing
```bash
python3 "$CDP" viewport 375 812    # iPhone
python3 "$CDP" screenshot /tmp/mobile.jpg
python3 "$CDP" viewport 1440 900   # desktop
python3 "$CDP" screenshot /tmp/desktop.jpg
```

## Remote Machines

For Vivaldi on kosm4 (CDP :9222):
```bash
ssh kosm4 'CDP_PORT=9222 python3.13 cdp.py screenshot /tmp/shot.jpg'
```
Or SSH tunnel (run cdp.py locally): `ssh -L 9222:localhost:9222 kosm4`

## Logging

All actions → `~/.claude/hooks/bulldozer-look.log`:
```
2026-05-11T03:30:00+0700 | event=screenshot | channel=cdp | path=/tmp/page.jpg | size=92847
2026-05-11T03:30:05+0700 | event=js | channel=applescript | expr=document.title
2026-05-11T03:30:10+0700 | event=open | url=http://localhost:9401
```
Note: `channel=` is present on commands with CDP/AppleScript fallback (screenshot, js, navigate, reload, click, fill, wait). Commands that use a single channel (open, console, network, pdf, viewport, window) omit it.

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

## Feedback

If you encounter friction while using this skill — documentation mismatch, missing capability, unclear error, or need a workaround — create a GitHub issue so JAINE-developer can fix it in real-time.

**Create issue when:**
1. SKILL.md describes behavior X, reality is Y
2. Had to use a workaround instead of the standard path
3. Need a feature that doesn't exist
4. Script failed with an unhelpful error message
5. No existing bulldozer skill covers the use case (use `[feedback/new-skill]` prefix)

**Do NOT create issue when:** own mistake in arguments, external problem (browser not running), or behavior documented as a known limitation.

**Command:**

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,look" \
  --title "[feedback/look] short description" \
  --body "$(cat <<ISSUE
## What I was doing
{task description}

## What I expected
{expected behavior}

## What happened
{actual behavior, errors}

## Workaround used
{what was done instead, or "none — blocked"}

## Environment
- Plugin version: $(jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json")
- Skill: look
- Project: $(pwd)
ISSUE
)"
```

**For new-skill requests (trigger #5):** use title prefix `[feedback/new-skill]`, labels `feedback,bulldozer` (omit `look`).

After creating the issue, tell the user:
> "I created a feedback issue about the look skill: {URL}. Want me to continue with a workaround, or would you like to get this fixed first?"
