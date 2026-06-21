---
name: look
description: Use when you need to see a web page, verify UI visually, take a screenshot, capture a region of the page, execute JS in a real browser, click elements, fill forms, check console errors, or monitor network requests. Triggers on "open in browser", "take screenshot", "capture region", "check if X is aligned", "what does this look like", "захватить область", "run JS", "click button", "fill form", visual verification, UI detail check.
argument-hint: "[URL] [task description]"
allowed-tools: ["Bash", "Read"]
---

# JAINE Browser — Multi-Channel Browser Automation

**Core principle:** See what the user sees. Three channels — CDP WebSocket (primary), AppleScript + DOM injection (fallback), macOS native (screenshot). No extensions needed.

## Quick Invoke (`/bulldozer:look [URL [task description]]`)

**Parse `$ARGUMENTS` first.** Extract the first URL-shaped token — that is the URL passed to scripts. The remaining text is task description you keep as your own brief; do **not** pass it to `launch.sh` or `cdp.py navigate`. A URL-shaped token starts with `http://`, `https://`, `file://`, or matches a `host:port/...` form. A **bare absolute filesystem path** (starts with `/`) that **exists** on disk is also URL-shaped — pass it through **as-is** (do NOT hand-prefix `file://` yourself). `cdp.py navigate`/`open` and `launch.sh` normalize a bare absolute path to a correctly **percent-encoded** `file://` URI via `pathlib.as_uri()` (so spaces and reserved chars like `#`, `?`, `%` are handled). Hand-prefixing `file://` yourself would bypass that encoding and break such paths. Viewing a just-created local file is a common reason to call this skill; a bare path passed straight to the scripts now works, whereas CDP otherwise rejects it (`Cannot navigate to invalid URL`) and leaves the browser on the previous page. If `$ARGUMENTS` contains no URL token, treat the whole string as task description and skip steps 2–3 (browser opens at `about:blank`).

Example: `/bulldozer:look file:///tmp/page.html — проверить рендеринг таблицы` →
- URL = `file:///tmp/page.html`
- Task description (your own note, not passed to scripts) = `проверить рендеринг таблицы`

Why: `launch.sh` reads `$1` verbatim as the URL and `cdp.py navigate` does the same. Passing the full `$ARGUMENTS` produces a malformed URL whenever the user adds a description.

1. Resolve the scripts, then check browser status. `$CLAUDE_PLUGIN_ROOT` is **not** exported to the Bash tool (#221), so resolve the plugin dir from the cache (honoring the var if it IS set). Shell state doesn't persist across Bash calls — re-run this resolver in any later Bash call that uses `$CDP`/`$LAUNCH` (it's a cheap filesystem lookup):
```bash
BULLDOZER_DIR=$( { [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "$CLAUDE_PLUGIN_ROOT/skills/look" ] \
  && printf '%s\n' "$CLAUDE_PLUGIN_ROOT"; } || ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null | head -1 )
CDP="$BULLDOZER_DIR/skills/look/scripts/cdp.py"
LAUNCH="$BULLDOZER_DIR/skills/look/scripts/launch.sh"
python3 "$CDP" status
```

2. If OFFLINE, launch with the parsed URL (or `about:blank` if none) — re-run the resolver above first if this is a separate Bash call:
```bash
"$LAUNCH" "<parsed URL or about:blank>" &
sleep 5
```

3. If browser was already ONLINE and a URL was parsed, navigate:
```bash
python3 "$CDP" navigate "<parsed URL>"
sleep 2
```
Skip step 3 if you just launched the browser in step 2 (it already opened the URL).

4. Screenshot + show:
```bash
python3 "$CDP" screenshot /tmp/jaine-look.jpg
```
Then Read the screenshot file to see it.

5. Report what you see to the user.

## Channels

| Channel | When | Capabilities |
|---------|------|-------------|
| **CDP WebSocket** | websocket-client installed | Everything: screenshot, JS, DOM, network, console, PDF |
| **AppleScript + DOM injection** | websocket-client missing | JS in main world, navigate, reload, click, fill, wait |
| **macOS native** | screenshot without websocket | screencapture via window ID |
| **AppleScript window** | macOS + Google Chrome | move between monitors, activate (+ bounds fallback) |

Auto-detected: `cdp.py status` shows active channel. Fallback is transparent — same commands work in both modes.

**No pip installs needed:** websocket-client is bundled in `scripts/vendor/`. Native screenshot fallback uses Quartz (PyObjC) — available in system/homebrew Python on macOS.

**Known Chrome behavior (Chromium #543437):** AppleScript `execute javascript` runs in isolated world — page variables invisible. Our DOM injection bridge solves this: injects `<script>` tag (runs in main world), writes result to `dataset`, reads back. Automatic in `cdp.py js`.

## Shared or isolated — decide BEFORE your first command

**Port 9333** = the user's live browser (cookies, logins, co-browsing). Use `open` + `--target` to work in a new tab; do NOT `navigate` the active tab away from the user's page.

**Your own task** (file://, localhost preview, UI iteration) → launch an **isolated lane** (`CDP_PORT=0 --automation` for ephemeral, or a named port). This keeps you from touching the user's session.

## Quick Reference

```bash
# Resolve scripts ($CLAUDE_PLUGIN_ROOT is NOT in the Bash env — #221; re-run per Bash call):
BULLDOZER_DIR=$( { [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "$CLAUDE_PLUGIN_ROOT/skills/look" ] \
  && printf '%s\n' "$CLAUDE_PLUGIN_ROOT"; } || ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null | head -1 )
CDP="$BULLDOZER_DIR/skills/look/scripts/cdp.py"
LAUNCH="$BULLDOZER_DIR/skills/look/scripts/launch.sh"

# Global flag — pin every command in the call to one tab (CDP/websocket only):
#   python3 "$CDP" --target SEL CMD ...   SEL = full target id, its 12-char prefix
#   (as shown by `tabs`/`status`), or a url substring. Ambiguous/unknown → fail loud.

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
python3 "$CDP" ax [--max-nodes N] [--raw] [--ref N]  # accessibility tree snapshot (CDP only)
  # Playwright-parity format: roles, states, [ref=N] for interactive elements
  # AX_OK header + tree body; --ref N for scoped subtree; --raw disables filters

# Execute
python3 "$CDP" js 'EXPRESSION'           # JS in main world
python3 "$CDP" js --ref N 'EXPR'         # EXPR with `el` bound to ref element (CDP only)
python3 "$CDP" wait [--js] SELECTOR_OR_EXPR [TIMEOUT]  # CSS selector; --js for JS expression
python3 "$CDP" click SELECTOR            # click element (trusted gesture on CDP channel)
python3 "$CDP" click --ref N             # click by AX ref (always trusted, no fallback, CDP only)
python3 "$CDP" fill SELECTOR VALUE       # fill input + dispatch events
python3 "$CDP" fill --ref N VALUE        # fill by AX ref (CDP only)
python3 "$CDP" hover SELECTOR            # hover element, triggers CSS :hover (CDP only)
python3 "$CDP" hover --ref N             # hover by AX ref (CDP only)
python3 "$CDP" key --ref N KEY           # send key to ref element (CDP only; Enter/Escape/Tab/ArrowDown/ArrowUp)
python3 "$CDP" drag SRC DST [--html5|--cancel]  # drag (CDP only; mouse default, --html5 for DnD, --cancel for Esc)
python3 "$CDP" drag --ref N --to-ref M [--html5|--cancel]  # drag by AX ref pair (CDP only)

# Verify
python3 "$CDP" assert [--js] EXPR_OR_SEL [--visible|--actionable] [--stable MS] [--timeout S]
python3 "$CDP" assert --ref N [--visible|--actionable] [--stable MS] [--timeout S]  # (CDP only)

# Debug
python3 "$CDP" console                   # console messages + uncaught exceptions (CDP only)
python3 "$CDP" network                   # network requests (CDP only)

# Generate
python3 "$CDP" pdf [FILE]                # save as PDF (CDP only)
python3 "$CDP" viewport WIDTH HEIGHT     # change viewport size

# Window management
python3 "$CDP" window bounds             # → "left,top,width,height" (CDP; works headless)
python3 "$CDP" window upper              # move to upper monitor (headful only)
python3 "$CDP" window lower              # move to lower monitor (headful only)
python3 "$CDP" window activate           # bring to front (headful only)
```

## Browser Setup

JAINE Browser = separate Chrome instance, no extensions:
- **CDP port:** 9333
- **Profile:** `/0/.jaine/.browser/profile/`
- **Launcher:** `$LAUNCH [URL]` (resolve `LAUNCH` via the Quick Reference snippet — `$CLAUDE_PLUGIN_ROOT` is not exported to the Bash tool, #221)
- **Rendering fidelity:** Chrome's Auto Dark Mode (`WebContentsForceDark`) is **off** — screenshots reflect the page as a normal user sees it. The browser UI (window/tabs/menus) follows the OS appearance setting; the CDP capture path never includes browser UI anyway.

### Lanes (parallel + headless)

A *lane* is an isolated browser = a set of env vars; the default invocation (no
env, no flag) is unchanged. The example blocks below use `$LAUNCH`/`$CDP` from the
Quick Reference resolver — set them in the same Bash call.

```bash
# Isolated headless lane on port 9334 (own temp profile):
CDP_PORT=9334 LOOK_PROFILE_DIR=/tmp/lane-a LOOK_HEADLESS=1 \
  "$LAUNCH" "http://localhost:9401"
CDP_PORT=9334 python3 "$CDP" screenshot /tmp/a.jpg
```

| Knob | Source | Default |
|------|--------|---------|
| Port | `CDP_PORT` env | `9333` |
| Profile | `LOOK_PROFILE_DIR` env, else derived from port | `9333 → /0/.jaine/.browser/profile`; else `…/profile-<port>` |
| Headless | `--headless`/`--headful` arg (wins), else `LOOK_HEADLESS` truthy | headful |
| Window pos (headful) | derived from port | `9333 → 100,100` |
| Chrome binary | `CHROME_BIN` env | macOS Chrome path |

**Headless ⇒ websocket-only.** The AppleScript DOM channel and the macOS-native
screenshot fallback both need a GUI and are unavailable headless; with bundled
`websocket-client` present, every content command (navigate/screenshot/js/click/
fill/wait/console/network) works over CDP. `window bounds` works headless too
(CDP `Browser.getWindowForTarget` → `left,top,width,height`); only `window
upper/lower/activate` are headful-only ergonomics (they move/focus the visible
GUI window). Audio: a trusted click satisfies user-activation but a headless
browser has no output device → functional verification yes, audible no.

**Parallel tabs in one lane:** `tabs`/`status` print each tab's 12-char id; pass it
(or a url substring) to `--target` to pin commands to one tab without it drifting —
e.g. drive two dashboards in the same lane: `--target <idA> screenshot a.jpg` then
`--target <idB> screenshot b.jpg`.

**Dry run:** `LOOK_DRY_RUN=1 …/launch.sh url` prints the resolved config + full
Chrome argv and exits without launching — useful to confirm a lane's flags.

If `status` shows OFFLINE:
```bash
"$LAUNCH" "http://localhost:9401"
```

### Web-security lane (`--insecure` / `LOOK_INSECURE`) — isolated trusted-LAN testing only

By default a `file://` page cannot `fetch('http://<host>')` — it is cross-origin from
the null `file://` origin (this is #93). For trusted local/LAN testing, an **isolated**
lane may opt into Chrome's `--disable-web-security`:

```bash
CDP_PORT=9334 LOOK_PROFILE_DIR=/tmp/look-lan LOOK_HEADLESS=1 \
  "$LAUNCH" --insecure http://localhost:8080/page
```

`--insecure` (or `LOOK_INSECURE=1`/`true`/`yes`, case-insensitive; any other value = off) is
**refused fail-loud** unless the lane is provably isolated: a **non-9333 `CDP_PORT`** AND an
**explicit, non-default `LOOK_PROFILE_DIR`** (the path is `realpath`-canonicalized — a
trailing-slash or symlink alias of the daily profile is rejected too). The
default lane and the daily 9333 browser can never be launched web-security-relaxed. On
the permitted path `launch.sh` prints a loud stderr warning.

**Safety boundary:** a relaxed lane disables the same-origin policy for *all* content it
loads — use it ONLY for trusted local/LAN pages, never for untrusted/remote content.

### Cert-pin lane (`--cert-spki=<PIN>` / `LOOK_CERT_SPKI`) — self-signed HTTPS targets

A self-signed LAN target (typical for home-lab deploys) hits a cert interstitial that
blocks navigation. An **isolated** lane may opt into Chrome's
`--ignore-certificate-errors-spki-list=<PIN>`: cert errors are ignored **only** for
certs whose SPKI SHA-256 matches a listed pin — all other TLS validation stays strict
(this is NOT the blanket `--ignore-certificate-errors`; same pin-don't-disable doctrine
as `curl --cacert`).

```bash
# Standard drive recipe — --automation's temp profile satisfies the gate:
CDP_PORT=9341 LOOK_HEADLESS=1 \
  "$LAUNCH" --automation \
  --cert-spki="BASE64PIN=" https://192.168.1.50:8443/

# Compute a target's pin:
openssl s_client -connect HOST:PORT </dev/null | openssl x509 -pubkey -noout \
  | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64
```

Value: comma-separated base64 SHA-256 SPKI fingerprints (each 43 chars + `=`);
malformed pins are rejected fail-loud at launch (a bad pin would otherwise surface as a
silent interstitial at navigate time). Arg wins over env. Same fail-closed gate as
`--insecure`: **non-9333 `CDP_PORT`** AND a provably-isolated profile (explicit
non-default `LOOK_PROFILE_DIR`, realpath-canonicalized — or `--automation`'s auto temp
profile). The daily 9333 browser can never get cert-bypass. Loud stderr warning on the
permitted path.

### Automation lane (Chrome for Testing — SP1, #164)

`launch.sh --automation` (or `LOOK_AUTOMATION=1`) starts the lane on the **pinned
Chrome for Testing** (`/0/.jaine/.browser/cft/current` — install/refresh via
`skills/look/scripts/update-cft.sh`; launching never auto-updates) with
`--enable-automation` (suppresses the bad-flags infobar; CfT alone does not) and
`--use-mock-keychain` (no macOS keychain prompts). Fail-closed gate: requires a
non-9333 `CDP_PORT` AND a profile that does not resolve to the daily profile;
without an explicit `LOOK_PROFILE_DIR` the lane gets a temp per-port profile
(`$TMPDIR/jaine-drive-<port>`). **Lane contract for cdp.py:** every `cdp.py` call
against a CfT lane carries BOTH `CDP_PORT=<port>` and
`CHROME_APP_NAME="Google Chrome for Testing"` — launch.sh's automation default
does not propagate to separate cdp.py processes, and without it the
AppleScript/native paths target stock Chrome. Headful CfT always shows its own
built-in "for automated testing only" banner (−56 CSS px viewport height; absent
headless; never in CDP screenshots) — cosmetic, not flag-suppressible. This is
the engine lane `/drive` (SP2) builds on; `/look` on 9333 is structurally
unaffected.

## Decision Rules (for JAINE)

**ax vs screenshot — choose by what you need:**
- **Text/states/structure** (what's on the page, is button disabled, what's in the table, form values) → `ax`. Cheapest structured channel: ~200-600 tokens vs 1300-1600 for screenshot. Absence of `[disabled]` = element is enabled.
- **Layout/color/visual/pixels** (alignment, overlapping, canvas content, CSS styling) → `screenshot`. Do NOT replace screenshot with ax for visual checks — ax has no geometry.
- **Interaction after reading** → `ax` then `click/fill/key --ref N` (ref from snapshot). No CSS selectors needed.

**screenshot:**
- `--full-page` — verify below-fold content (long pages, tables, grids). Omit for quick viewport checks.
- `--clip X Y W H` — capture a CSS-pixel region (mutually exclusive with `--full-page`). Use for UI-detail verification. Output dimensions are W × H × native DPR by default — pair with `--scale 1` for exact W×H CSS-pixel output.
  - **Origin (empirically verified):** `X Y` are **document/page coordinates** (CSS px from the page origin, *not* viewport-relative). Because `--clip` does **not** set `captureBeyondViewport` (unlike `--full-page`, which does), only the part of the region currently **within the viewport** renders — a below-fold region (its document-`Y` past the current scroll) captures **blank**. To clip below-fold content, **scroll it into the viewport first** (`js "window.scrollTo(0, <Y>)"`) then clip at its document-`Y`. For whole below-fold pages prefer `--full-page`.
- `--scale N` — opt-in output resolution. Default = native DPR (Retina ≈ 2× preserves UI detail — that is the skill's main use case). `--scale 1` produces CSS-pixel output (1440×900 instead of 2880×1800 on Retina) via `clip.scale = N / window.devicePixelRatio`. `Emulation.setDeviceMetricsOverride{deviceScaleFactor:1}` was tried first — it does **not** affect capture output size, only `window.devicePixelRatio` for the page's JS.
- `stdout` — every screenshot prints `PATH  W×H` (e.g. `/tmp/x.jpg  2880×1626`). Read those dimensions before computing any external crop — on Retina the captured image is wider than the logical viewport.
- If WARNING appears on stderr — page dimensions unavailable, retry after `wait`.

**wait:** Default = CSS selector (`wait ".results" 10`). Use `--js` only for JS expressions that aren't DOM selectors (`wait --js "DATA !== null" 10`, `wait --js "document.readyState === 'complete'" 5`). Never use `--js` with CSS selectors — it will fail.

**console:** Run `console` FIRST when a page loads blank or broken — it shows uncaught exceptions (TypeError, ReferenceError) that `js` cannot see. Output format: `[error] message` for console.error, `[exception] description — file:line:col` for uncaught exceptions.

**click vs js:** Prefer `click SELECTOR` over `js "querySelector(...).click()"` — click reports the element tag and handles NOT_FOUND with clear error. Use `js` only for complex multi-step DOM manipulation.

**Trusted gesture:** on the CDP (websocket) channel, `click` dispatches a real `Input.dispatchMouseEvent` (press+release at the element's box center), so the click is `isTrusted` and grants browser **user activation** — it can unblock `AudioContext.resume()`, autoplay, clipboard write, pointer lock, and fullscreen (verify audio by clicking the play control). Note: the trusted path scrolls the target to center before clicking, so a `click` can change the page's scroll position — if you `click` then `screenshot` and rely on the prior scroll offset, re-`navigate` or account for the shift. If the element is not hittable (hidden / `display:none` / occluded by an overlay / off-viewport after scroll), `click` automatically falls back to an untrusted `el.click()`, prints `(fallback: … untrusted)`, and WARNs on stderr that user activation was not granted. The AppleScript fallback channel (no CDP Input) is always untrusted. There is no autoplay flag — a real trusted click is the faithful path (headless + parallel-session isolation are tracked separately in #141).

**Active tab / tab pinning:** by default all commands act on the browser's first/active tab. With two or more tabs open in one lane that tab can drift (another task navigated it). To pin a specific tab for the whole call, pass `--target SEL` (a full target id, its 12-char prefix as shown by `tabs`/`status`, or a url substring): `python3 "$CDP" --target <id12> js …`. An ambiguous or unknown selector fails loud (it never silently drives the wrong tab). `--target` needs the CDP/websocket channel — the AppleScript fallback can only reach the active tab.

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
Note: `channel=` is present on commands with CDP/AppleScript fallback (screenshot, js, navigate, reload, click, fill, wait). Commands that don't record a channel (open, console, network, pdf, viewport, window) omit it.

Review: `column -t -s'|' ~/.claude/hooks/bulldozer-look.log`

## Fallback Matrix

| Command | CDP (websocket) | AppleScript fallback | macOS native |
|---------|:-:|:-:|:-:|
| status, tabs, open | HTTP only | — | — |
| js, title, click, fill, wait | WebSocket | DOM injection | — |
| navigate, reload | WebSocket | AppleScript | — |
| screenshot | WebSocket | — | screencapture |
| html, console, network, pdf | WebSocket | unavailable | — |
| ax, hover, key, drag | WebSocket | unavailable (CDP only) | — |
| assert, `--ref` variants | WebSocket | unavailable (CDP only) | — |
| viewport | WebSocket | window bounds (approximate) | — |
| window | WebSocket (bounds) | AppleScript (bounds fallback; upper/lower/activate) | — |
| `--target` | WebSocket (required) | — (active tab only) | — |

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
- Plugin version: $(jq -r .version "$(ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json 2>/dev/null | head -1)" 2>/dev/null || echo unknown)
- Skill: look
- Project: $(pwd)
ISSUE
)"
```

**For new-skill requests (trigger #5):** use title prefix `[feedback/new-skill]`, labels `feedback,bulldozer` (omit `look`).

After creating the issue, tell the user:
> "I created a feedback issue about the look skill: {URL}. Want me to continue with a workaround, or would you like to get this fixed first?"
