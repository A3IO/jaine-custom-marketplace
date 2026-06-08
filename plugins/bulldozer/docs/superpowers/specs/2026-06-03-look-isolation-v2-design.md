# /look-v2 — Robust Parallel Headless Localhost Testing — Design Spec

**Status:** draft (brainstorm output, pre-`bulldozer:check`)
**Date:** 2026-06-03
**Issues:** #141 (parallelism + headless), #93 (LAN fetch), + two new sub-issues to file (window-over-CDP, tab-target pinning)

**Goal:** Make `/look` reliable for running multiple isolated browser lanes in parallel, optionally headless, driven pure-CDP-first — so several projects/sessions can test localhost sites at once without colliding, and headless verification works end-to-end.

**Architecture:** A *lane* = an isolated browser instance = a set of env vars (`CDP_PORT` + derived/overridable profile, headless, window, binary). `launch.sh` is parameterized to honor them; `cdp.py` already speaks `CDP_PORT` and is extended to (a) query window geometry over CDP instead of AppleScript and (b) pin which tab a command drives. The macOS-GUI/AppleScript hybrid is retained only for the default headful "human watches" browser and for inherently-desktop ergonomics; headless lanes are pure CDP.

**Tech stack:** bash (`launch.sh`), Python 3 + bundled `websocket-client` (`cdp.py`), pytest + the `jaine_browser` conftest fixture, Chrome DevTools Protocol (Browser/Page/Emulation/Runtime/Input domains), Chrome `--headless=new`.

---

## 1. Problem cluster (what "close everything" means)

| # | Problem | Closed by |
|---|---------|-----------|
| 1 | No parallelism — one shared Chrome on fixed port 9333, shared tabs, `get_tab()` = active tab → cross-session collision | **A** (lanes) |
| 2 | No headless option | **A** (`--headless=new`) |
| 3 | `cmd_window` is AppleScript-only → breaks headless; the channel is macOS-GUI cruft | **B** (window-over-CDP) |
| 4 | Intra-lane tab drift — `get_tab()` returns the first page; an agent that opened 2 tabs in ONE lane drives the wrong one | **C** (tab-target pinning) |
| 5 | #93 LAN fetch — `fetch('http://<LAN>')` from a `file://` page fails (cross-origin from a null/file origin) | **D** (opt-in web-security lane flag) |

**Two axes, deliberately:** 1–4 are one coherent axis — "per-lane, pure-CDP-first control." 5 (#93) is a *different* axis — web-origin/CORS — folded in here only because it shares `launch.sh` as the change-point; it is gated, opt-in, and never the default.

**Out of scope (documented, not fixed):**
- The default headful daily browser (port 9333) keeps the AppleScript/Local-State/osascript apparatus by design — a human watches it; that is its purpose.
- Persistent cross-invocation lane state, lane registries, auto-port-allocation. A lane is just env vars; the caller owns them. YAGNI.

---

## 2. Core architecture: the lane model

A **lane** is fully described by env vars; **the default invocation (no env, no flag) stays byte-identical to today.**

| Knob | Source | Default |
|------|--------|---------|
| Port | `CDP_PORT` env | `9333` |
| Profile dir | `LOOK_PROFILE_DIR` env → used verbatim; else derive from port | `9333 → /0/.jaine/.browser/profile` (unchanged); other → `/0/.jaine/.browser/profile-<port>` |
| Headless | arg `--headless`/`--headful` (arg wins) → else `LOOK_HEADLESS` truthy | headful |
| Window position (headful only) | derived from port, capped | `9333 → 100,100` (unchanged); else `100+((port-9333)*40 mod CAP)` on both axes |
| Chrome binary | `CHROME_BIN` env | unescaped macOS path (see A.7) |
| chrome.log | derived; follows the profile when overridden | `9333 → /0/.jaine/.browser/chrome.log` (unchanged); else next to the profile dir |
| Web-security relax (D-owned; **reserved & rejected until D flag-shipped**) | `LOOK_INSECURE=1` env / `--insecure` arg | off (fail-loud if set pre-D) |

`CDP_PORT=9334 LOOK_HEADLESS=1 ./launch.sh url` + `CDP_PORT=9334 cdp.py click …` = one fully-isolated headless lane. `cdp.py` already honors `CDP_PORT`.

**Headless channel implications** (documented in SKILL.md, enforced by nothing — it is a capability fact): headless ⇒ websocket-only. The AppleScript DOM channel and the macOS-native screenshot fallback both need a GUI and are unavailable; with bundled `websocket-client` present, every content command (`navigate`/`screenshot`/`js`/`click`/`fill`/`wait`/`console`/`network`) uses the CDP path and works headless. Window *ergonomics* (`window upper/lower/activate`) are headful-only (B). Audio: a trusted click (shipped in #140) satisfies the user-activation gate so `AudioContext.resume()` succeeds, but a headless browser has no output device → functional verification yes, audible no.

---

## 3. Decomposition & build order

Four sub-projects, each its own plan→TDD→PR. Recommended order:

1. **A — Lane infrastructure** (`launch.sh` + `conftest.py`). Foundation. = #141 core. Largest.
2. **B — Window query over CDP** (`cdp.py::cmd_window`). Small; jointly with A delivers "headless e2e fully green" (no skipped window test). Pairs naturally with A in one PR or directly after.
3. **C — Tab-target pinning** (`cdp.py` global `--target`). Independent of A/B.
4. **D — LAN web-security lane flag** (`launch.sh` opt-in + a spike). = #93. Independent; do last (carries a confirm-the-flag spike).

B and C are pure `cdp.py` and work against any lane (including 9333), so they do not strictly depend on A; A and D share `launch.sh`. Each sub-project produces working, testable software on its own.

---

## 4. Sub-project A — Lane infrastructure (`launch.sh` + `conftest.py`)

### A.0 Invariant (backward-compat)
With no env and no flag, the resolved Chrome argv, profile, window, log path, osascript blocks, and Local-State patch are **byte-for-byte today's**. A characterization test pins this.

### A.1 Resolve config (env with defaults)
Read `CDP_PORT`, `LOOK_PROFILE_DIR`, `LOOK_HEADLESS`, `CHROME_BIN` with the defaults above. (`LOOK_INSECURE` / `--insecure` are **owned by D** — see D.2 — A never *uses* them as config. A's launch.sh refuses both until D flag-ships: the `--insecure` **arg** via A.2's unknown-flag fail-loud, and a set `LOOK_INSECURE` **env** via an explicit reserved-env guard — `[ -n "$LOOK_INSECURE" ]` → fail-loud — because unknown-flag handling catches only argv, not env.)

### A.2 Argument parsing (panel must-fix #2)
Replace `URL="${1:-about:blank}"` with a real parser: loop over args; recognize `--headless`, `--headful` (NOT `--insecure` — that flag is D's, see D.2; until D's flag-shipped branch it is an unknown flag → fail-loud); a `--` terminator forces the next token to be the URL; the first non-flag token is the URL (default `about:blank`); an **unknown `--flag` is a fail-loud error** to stderr + non-zero exit (cdp.py "no silent fallbacks" principle). This lets a URL that legitimately starts with `--` be passed after `--`. Flags may appear before or after the URL.

### A.3 Headless precedence (judgment-call #2)
`--headful` arg → headful; `--headless` arg → headless (arg overrides env, both directions); else `LOOK_HEADLESS` truthy (`1`/`true`/`yes`, case-insensitive) → headless; else headful. Headless ⇒ add `--headless=new` AND **skip** the Local-State pre-patch + both osascript blocks (no GUI → AppleScript is meaningless). Headful ⇒ today's behavior.

### A.4 Profile derivation (judgment-call: derive + override)
`LOOK_PROFILE_DIR` set → verbatim. Else `9333 → /0/.jaine/.browser/profile`; other port → `/0/.jaine/.browser/profile-<port>`. The override is the seam `conftest` uses for an ephemeral `mkdtemp` profile.

### A.5 `pkill` lane-scoping (panel must-fix #1 — CRITICAL)
Today: `pkill -f "user-data-dir=$PROFILE_DIR"`. `…/profile` is a **substring** of `…/profile-9334` → launching the 9333 default would kill other lanes. Fix: anchor the match to an argument boundary and regex-escape the profile path. Concretely, match `--user-data-dir=<escaped-profile>` up to a whitespace/end boundary (e.g. `pkill -f -- "user-data-dir=<escaped>($|[[:space:]])"`), where `<escaped>` escapes regex metacharacters (arbitrary `LOOK_PROFILE_DIR` is untrusted regex input). A spike validates the exact `pkill`/`pgrep` form on macOS before locking it.

### A.6 Window-offset validation + cap (panel must-fix #3)
Validate `CDP_PORT` is an integer in `1..65535` **before** any arithmetic or path derivation (mirror `cdp.py`'s int-guard; non-numeric / out-of-range → fail-loud, never silently `0`). The headful window offset must be **non-negative**: bash `%` keeps the dividend's sign, so a port *below* 9333 (e.g. `9222`, `9304`) yields a negative coordinate (`port 9304 → x=-1060`). Normalize: `off=(port-9333)*40; x = 100 + ((off % 1200) + 1200) % 1200` (same for y). `9333 → 100,100` exactly. Tests cover ports below **and** above 9333.

### A.7 `CHROME_BIN` (panel must-fix #7)
Default to the **unescaped** path `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` (today's `launch.sh` works only because the path is backslash-escaped *and* unquoted). Always invoke as `"$CHROME_BIN"`. `conftest`'s existing `CHROME` const is already the unescaped form — it becomes the shared reference and `conftest` stops launching Chrome directly (A.10).

### A.8 Directory creation (panel must-fix #6)
`mkdir -p` the profile parent and the log directory before the Local-State read and the log redirection; otherwise a first-run lane fails in the shell before Chrome starts.

### A.9 chrome.log follows the profile (panel must-fix #5)
`9333 → /0/.jaine/.browser/chrome.log` (unchanged). When `LOOK_PROFILE_DIR` is overridden (tests), the log goes **inside the profile dir** at exactly `$LOOK_PROFILE_DIR/chrome.log` — under the temp profile so the fixture's `rmtree(temp_profile)` removes it; not into the global `/0/.jaine/.browser/` and not a sibling. Otherwise (derived per-port profile) `chrome-<port>.log` next to that profile. This prevents concurrent same-port writers interleaving in one global file and guarantees test logs are cleaned with the profile.

### A.10 Single argv array + `LOOK_DRY_RUN` seam (panel must-fix #4 + judgment-call #3)
Build the Chrome invocation as **one bash array** `CHROME_ARGV=(…)`; both the dry-run printer and the real launch use that same array (no second code path, no quoting drift). The real launch is a **background invocation** — `"${CHROME_ARGV[@]}" >> "$LOG" 2>&1 &` then `CHROME_PID=$!` — **NOT** a shell-replacing `exec` (the headful default still needs its post-launch blocks: the two `osascript` enablement steps + the `kill -0 "$CHROME_PID"` startup check). `LOOK_DRY_RUN=1 ./launch.sh url` prints the resolved config (port, profile, headless, window, chrome_bin, log path, and the full `CHROME_ARGV`) and exits 0 **without launching Chrome**. Unit tests assert that dry-run and the real path build the **identical** `CHROME_ARGV`; a slow e2e test covers a real headless launch.

### A.11 conftest collapse (judgment-call #1, panel-corrected)
Fold the `if CDP_PORT==9333: launch.sh else: direct-Chrome` branch into one path: always `launch.sh`. Non-9333 lanes pass `LOOK_PROFILE_DIR=<mkdtemp>` + `LOOK_HEADLESS=1` → ephemeral isolated headless test browser (skips osascript, no windows pop). **The e2e default is itself an isolated lane:** `conftest` defaults `CDP_PORT` to a dedicated test port (NOT 9333) + `mkdtemp` profile + headless, so a bare `pytest` (no env) never reaches the user's daily 9333 browser; driving the daily browser is explicit opt-in (`CDP_PORT=9333`). The `CHROME` const stays only as the `CHROME_BIN` reference (A.7); direct `Popen([CHROME,…])` is removed.
- **Monitors (from the panel, verified in TDD, not blockers):**
  - *Headful-only window test:* `cmd_window bounds` is AppleScript today; B ports it to CDP so it passes headless. **Until B lands**, mark `test_window_bounds_returns_coords` headful-only (skip when the lane is headless). After B, the skip is removed.
  - *20s startup deadline:* a fresh temp profile + `--headless=new` still needs profile-init + debug-port bind; confirm headless starts within the fixture's 20s deadline, bump if flaky.
  - *reuse-if-online (MANDATORY guard, not optional):* `if _cdp_is_online(): yield "reused"` may reuse ONLY a browser the fixture owns on its dedicated non-9333 test port. An **unexpected** pre-existing CDP listener on that test port is a **fail-loud setup error**, never silent reuse — otherwise a stale browser bypasses isolation and the §8 "no test reaches 9333" guarantee is false.
  - *no early-crash signal:* `launch.sh` backgrounds Chrome and exits 0, so a post-launch crash surfaces only at the 20s timeout. Acceptable — `conftest` already polls HTTP + cleans via `pkill -f <profile>`, not via a `Popen` handle.

### A.12 SKILL.md
Document the lane model (`CDP_PORT=N [LOOK_HEADLESS=1] ./launch.sh url` + `CDP_PORT=N cdp.py …`), the headless channel implications (§2), and `LOOK_DRY_RUN`. Default-invocation docs unchanged.

### A — acceptance
Default lane byte-identical (characterization test green); `CDP_PORT=9334` → `profile-9334` + capped offset + port in argv; `LOOK_PROFILE_DIR` verbatim + log inside it; headless (env and `--headless`) → `--headless=new`, osascript/Local-State skipped; `--headful` overrides `LOOK_HEADLESS=1`; `CHROME_BIN` honored + quoted; fail-loud on unknown `--flag` / non-numeric port; `pkill` does not cross lanes; existing e2e green via the unified launch path (headless), `test_window_bounds_returns_coords` skipped-headful pending B.

---

## 5. Sub-project B — Window query over CDP (`cdp.py::cmd_window`)

**Why:** drop the AppleScript dependency for the window *query* so it works headless (Chrome 142+ supports CDP window bounds; machine runs 148; `ws_send`/`ws_send_seq` already exist from #140).

### B.1 `window bounds` → CDP
When `has_websocket()`: resolve the target id from the active/pinned tab (`get_tab()["id"]`), call `Browser.getWindowForTarget({targetId})` → `{windowId, bounds:{left, top, width, height, windowState}}`, print the bounds. AppleScript stays as the `else` fallback (consistent with every other `cmd_*`). A spike confirms the exact response shape + headless behavior before locking the printed format.

### B.2 Output format (explicit — do not silently change semantics)
AppleScript returned `x1, y1, x2, y2` (Chrome window `bounds`); CDP returns `left, top, width, height` (different last two fields). **Fixed stdout contract:** `window bounds` prints exactly `left,top,width,height` (comma-separated, no spaces) on the CDP path. The AppleScript **fallback is normalized to the SAME `left,top,width,height`** (convert `x2,y2 → width=x2-x1, height=y2-y1`) so the contract is identical across channels — not two formats for one command. The e2e assertion checks the four labeled fields (not merely "contains a comma"). Document the exact format in SKILL.md.

### B.3 `upper` / `lower` / `activate` stay headful
These move the visible window between physical monitors / focus the GUI — meaningless headless. Keep them AppleScript, document as headful-only ergonomics. (No e2e test exercises them; `bounds` is the only tested action.)

### B.4 conftest
Remove the A.11 headful-only skip on `test_window_bounds_returns_coords`; it now passes headless via CDP. Keep/add a headful-marked test for `activate` if desired (optional).

### B — acceptance
`window bounds` returns coordinates against a **headless** lane; AppleScript fallback still works when websocket is absent; `upper/lower/activate` documented headful-only; the window e2e test is green headless.

---

## 6. Sub-project C — Tab-target pinning (`cdp.py` global `--target`)

**Why:** kill intra-lane tab drift. `get_tab(url_filter=None)` already supports a substring url filter but no command exposes it; every `cmd_*` calls bare `get_tab()` → first page.

### C.1 Global `--target <selector>`
Parse a global `--target <selector>` (and/or `--tab`) in `main()` before command dispatch; thread the selector into `get_tab`. Selector resolution: a target whose full `id` **equals OR is uniquely prefixed by** the selector (`cmd_tabs`/`cmd_status`/`cmd_open` print `id[:12]` — grep `[:12]` — so the copy-pasteable displayed id is a 12-char *prefix*, not the full id — and the selector must be **≥12 chars** to count as an id-prefix (a shorter string is NOT treated as an id-prefix; it falls through to the url match, so a 1–3 char string can't accidentally pin a tab — plan-review R1-F4); a prefix matching ≥2 targets is **ambiguous → fail-loud**), else substring URL match (reuse `url_filter`). No `--target` → first page (today's behavior; backward-compat). Unknown / no-match / ambiguous selector → fail-loud error, never silent fall-back to the wrong tab. **`--target` requires the CDP/websocket channel:** the AppleScript/native fallback paths bind to the *active* tab/window and cannot honor a CDP target id, so any command given `--target` while `has_websocket()` is false **fails loud** rather than silently driving the active tab.

### C.2 Plumbing
`get_tab` gains a `target=` parameter (id-or-url); the existing `url_filter` path is subsumed. **The selector must reach EVERY tab resolution, including the indirect ones.** `cdp_js()` itself calls bare `get_tab()` (grep `def cdp_js`), and `cmd_title`/`cmd_html`/`cmd_wait`/`cmd_fill`/`cmd_screenshot --full-page` resolve their tab THROUGH `cdp_js` (grep `cdp_js(` for the call sites) — so pinning only the direct `get_tab()` callers leaves those drifting. Therefore: resolve the tab ONCE per command from the global selector and thread the captured target/`ws_url` into `cdp_js()` (give `cdp_js` an explicit `target`/`ws_url` parameter). No command may re-resolve `get_tab()` without the selector. Preserves the #140 same-`ws_url`-pinning pattern.

### C.3 Discoverability
`cmd_tabs`/`cmd_status`/`cmd_open` print `id[:12]`; since C.1 accepts a unique id-**prefix**, that 12-char display is directly copy-pasteable into `--target` (no need to widen the display). SKILL.md documents the parallel-tabs workflow.

### C — acceptance
With two pages open in one lane, `cdp.py --target <id> js …`, the indirect-`cdp_js` commands (`title`/`html`/`wait`/`fill`/`screenshot --full-page`), **AND the direct-`get_tab()` side-effect commands (`navigate`/`reload`/`click`/`console`/`network`/`pdf`/`viewport`/ viewport-or-clip `screenshot`)** all drive the pinned tab, not the first; **a structural test asserts no `cmd_*` resolves a tab without the selector** (every `get_tab(` / `cdp_js(` call site is selector-threaded); the 12-char id printed by `tabs` is accepted as a unique prefix; `--target <url-substring>` works; `--target` without websocket fails loud; no `--target` is unchanged; bad / ambiguous selector errors loudly.

---

## 7. Sub-project D — LAN web-security lane flag (`launch.sh`, opt-in) — #93

**Why:** `fetch('http://<LAN>')` from a `file://` page fails (cross-origin from a file/null origin). #93's own workaround (serve the page over http) confirms it is an origin problem, not network isolation.

### D.1 Spike first
Empirically confirm which flag actually unblocks the #93 case on the current Chrome: `--disable-web-security` (disables same-origin/CORS; **requires** a non-default `--user-data-dir`, which lanes have) vs `--allow-file-access-from-files` vs serving over http. The spec does not guess — the plan starts with a spike against a reproduction (a `file://` page fetching a local http server).

### D.2 Opt-in flag, never default
**D is the sole owner of `LOOK_INSECURE` / `--insecure`.** Until D's *flag-shipped* branch lands — and permanently if D goes *docs-only* — both are **unsupported and fail-loud** by TWO distinct mechanisms (a recognized no-op is never acceptable): the `--insecure` **arg** is rejected by A.2's unknown-flag handling; a set `LOOK_INSECURE` **env** is rejected by an explicit reserved-env guard in launch.sh (`[ -n "$LOOK_INSECURE" ]` → fail-loud) — *not* "via A.2", which only sees argv. D's flag-shipped branch then REPLACES the env guard with the gated `LOOK_INSECURE` read AND extends A.2 to recognize `--insecure`. When shipped: `LOOK_INSECURE=1` env / `--insecure` arg adds the confirmed flag(s) to `CHROME_ARGV`. Off by default. **Refusal is mandatory, not advisory:** `--insecure` is REJECTED (fail-loud, non-zero exit) unless the lane is provably isolated — non-9333 `CDP_PORT` AND an explicit non-default `LOOK_PROFILE_DIR` (ideally headless). The SAME gate runs in `LOOK_DRY_RUN` and the real launch (one code path), so the daily 9333 browser — and its argv-inheriting child processes — can never be launched web-security-relaxed. Loud stderr warning on the permitted isolated path ("web security relaxed for THIS lane — never use for untrusted content"). Documented in SKILL.md as "isolated trusted-LAN testing only."

### D.3 Alternative documented
If the spike shows the flags are fragile/unreliable, D ships **docs-only** (no `--insecure` implementation). The documented workaround is NOT merely "serve over http" — a plain `http://localhost` page fetching `http://<LAN>` is **still cross-origin**. The supported docs-only path is: serve the page from the **same origin** as the target service, OR front the LAN service with a **CORS-enabled local proxy** (origin sends `Access-Control-Allow-Origin`). Honest either-way.

### D — acceptance
**Two acceptance branches by spike outcome.** *Flag-shipped:* `--insecure` on the default/9333 lane is REJECTED (fail-loud; dry-run and real share the gate); on an isolated lane (non-9333 + non-default `LOOK_PROFILE_DIR`) it warns, adds only the spike-confirmed flag, and the #93 reproduction fetch succeeds. *Docs-only* (spike rejected the flags): NO `--insecure` ships; SKILL.md documents the same-origin / CORS-proxy workaround and the #93 reproduction succeeds via that path. Either branch: default lanes are unchanged and unweakened; SKILL.md documents the safety boundary.

---

## 8. Testing strategy

- **A:** `LOOK_DRY_RUN` unit tests (bash via subprocess) assert resolved argv for every knob + the byte-identical default (characterization). Slow e2e: a real headless lane starts, `cdp.py` drives it, screenshot works. conftest unified path keeps the existing e2e green headless.
- **B:** e2e `window bounds` against a headless lane; unit/fallback for the AppleScript branch.
- **C:** e2e with two tabs + `--target` covering BOTH the indirect-`cdp_js` callers (`title`/`html`/`wait`/`fill`/`screenshot --full-page`) AND representative direct-`get_tab()` side-effect commands (`navigate`/`click`/`console`/`network`), not just `js`; a **structural test that every `get_tab(` / `cdp_js(` call site is selector-threaded** (no un-pinned resolution); unit for the selector resolver (exact id, unique prefix, ambiguous prefix, url, miss) + `--target` without websocket fails loud.
- **D:** a reproduction fixture (file:// page + local http server) gated behind the spike; e2e proves the flag unblocks fetch.
- Cross-cutting: every new `launch.sh` path has a `LOOK_DRY_RUN` unit test; the e2e default is an isolated non-9333 headless lane, so no test reaches the user's daily 9333 browser by default, and an unexpected listener on the test port fails loud (A.11) rather than silently reusing it.

## 9. Risks

- `pkill`/`pgrep` exact anchoring form on macOS (A.5) — spike.
- CDP window-bounds response shape + headless semantics (B.1) — spike.
- #93 flag efficacy (D.1) — spike; may degrade to docs-only.
- Headless first-run vs 20s fixture deadline (A.11) — verify, bump if flaky.

## 10. Acceptance (epic)
All five cluster rows closed (1–4 functionally; 5 via opt-in flag or, if the spike fails, the documented same-origin/CORS-proxy path); default `/look` unchanged; every sub-project independently tested; `bulldozer:check` GO on this spec before plans.
