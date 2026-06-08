---
name: drive
description: Drives product testing in an isolated Chrome-for-Testing browser with a self-verify core — navigate-that-waits, console error gate, stability-window assertions, trusted clicks, navigation-bound screenshots, opt-in cookie-seed auth. ALWAYS invoke for "протестируй UI", "проверь в браузере что работает", "прогони e2e по странице", "drive the app", "run a browser test", "verify this page works". Do NOT use for looking at the user's own daily browser or his real logins — that is /bulldozer:look (stock Chrome, port 9333). Supports autonomous (headless, runs to completion) and co-pilot (headful, human checkpoints) modes.
argument-hint: [URL] [test task]
allowed-tools: ["Bash", "Read", "AskUserQuestion"]
---

# JAINE Drive — Product Testing on an Isolated CfT Lane

## Boundary (vs /look)

| | `/bulldozer:look` | `/bulldozer:drive` (this skill) |
|---|---|---|
| Browser | user's daily stock Chrome | pinned Chrome for Testing |
| Port/profile | 9333, his real logins | isolated lane (9340-9349 main session / ephemeral `CDP_PORT=0` subagents), temp profile |
| Intent | observe HIS browser state | test the PRODUCT in a clean env |
| Flags | none | `--enable-automation --use-mock-keychain` |

- The cut is **environment**, not human-vs-automation (spec §2). A human can watch
  a drive session (co-pilot); an agent can run it end-to-end (autonomous).
- Engine: the same `cdp.py` as /look (`skills/look/scripts/cdp.py`) — verify-core
  features are opt-in flags that never change /look's defaults.
- **Playwright is NOT built** (SP0 bounded-both verdict): cdp.py is the default
  engine. If a real test demonstrably hits a cdp.py wall (rich locators,
  actionability beyond `assert --actionable`, PW-only features), STOP and file an
  issue — do not hack around it. "Might be nicer" is not a wall.

## Parse `$ARGUMENTS`

Split into: first token that looks like a URL/path → the target; the rest → the
test task description. No URL → ask what to test.

## Lane setup (main session; subagents use "Subagent delegation" below instead)

1. **Pick a free port from 9340-9349** (interactive /drive range — registry in
   `tests/conftest.py`):
   ```bash
   for p in 9340 9341 9342 9343 9344 9345 9346 9347 9348 9349; do
     curl -s -m1 "http://localhost:$p/json/version" >/dev/null 2>&1 || { PORT=$p; break; }
   done
   ```
2. **Launch** (autonomous default = headless; co-pilot = headful, drop LOOK_HEADLESS):
   ```bash
   CDP_PORT=$PORT LOOK_HEADLESS=1 "<plugin>/skills/look/scripts/launch.sh" --automation
   ```
   The lane gets a temp per-port profile automatically (`$TMPDIR/jaine-drive-<port>`).
   **Self-signed HTTPS target** (typical LAN deploy) → add `--cert-spki=<PIN>` to the
   launch (pin-only TLS bypass, gated to isolated lanes; pin computation + contract:
   look SKILL.md → "Cert-pin lane"). Without it, navigate hits a cert interstitial.
3. **LANE CONTRACT — every cdp.py call carries BOTH env keys** (launch.sh's
   defaults do NOT propagate to separate cdp.py processes):
   ```bash
   CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" \
     python3 "<plugin>/skills/look/scripts/cdp.py" <command> …
   ```
4. **PRE-FLIGHT** (hole D — wrong browser on the port): verify the endpoint is the
   pinned CfT BEFORE trusting any result:
   ```bash
   curl -s "http://localhost:$PORT/json/version"   # "Browser" must end with /<pinned>
   basename "$(readlink /0/.jaine/.browser/cft/current)"   # the pinned version
   ```
   Mismatch → STOP: something else owns the port; pick another from the range.
   (CfT install/refresh: `skills/look/scripts/update-cft.sh` — launching never
   auto-updates.)
5. **Headful note:** CfT always shows its own 56-px "for automated testing only"
   banner — cosmetic, not flag-suppressible, absent headless, never in CDP
   screenshots. Headful viewport height ≠ window-size minus chrome: read
   `innerHeight` live (`cdp.py js "window.innerHeight"`) when geometry matters.

## verify-core workflow (the loop)

Every fix-verify iteration runs this sequence — each primitive emits a
machine-readable verdict (exit code + marker), never trust prose impressions.
**Verdict grammar:** ALL verdict markers (`*_PASS`/`*_OK`/`*_FAIL`/`*_MISMATCH`)
are on **stdout**; stderr carries only tool errors (bad flags, transport
failures):

1. `navigate URL --wait load [--expect-url SUBSTR]` — blocks until OUR
   navigation's lifecycle event (loaderId-bound; a prior page's events can't
   satisfy it). Note the printed `loader=` token. `--wait networkidle` for
   XHR-heavy pages. Failure: `NAVIGATE_FAIL`/`NAVIGATE_URL_MISMATCH` + exit 1.
2. `console --gate` — exit 1 = the page has errors → REAL finding. The gate
   listens on THREE channels and its FAIL line names which leg fired
   (`CONSOLE_GATE_FAIL: N (X exception(s), Y console, Z log)`):
   - **exceptions — caught retroactively** (replayed even when nobody was
     listening when they fired);
   - **`console.error` — caught in the gate's live 3s window** — call the gate
     IMMEDIATELY after the action it checks; retroactive console.* replay is NOT
     guaranteed (fragile storage-activation quirk — see
     `docs/superpowers/analysis/2026-06-05-sp2-console-gate-verification.md`);
   - **browser-generated errors (CORS blocks, CSP violations, net::ERR_*) — via
     the Log domain**, live window (these NEVER appear via Console/Runtime —
     without this leg a CORS-rejected auth fetch would green-light).
   Warnings do not gate. The buffer clears on navigation, so the gate is scoped
   to the current page for free.
3. `assert SELECTOR --visible [--stable 500]` / `assert --js 'EXPR'` —
   ASSERT_PASS/ASSERT_FAIL + exit 0/1. The stability window (condition must hold
   true CONTINUOUSLY for --stable ms) is the flaky-vs-real discriminator: flap
   diagnostics (`unstable: flapped Nx`) distinguish flaky from absent
   (`never true`). Flaps shorter than the 100ms polling interval are invisible.
   Before interacting: `assert SEL --actionable --stable 300` — visible + enabled +
   hit-test (an overlay-covered or disabled control is visible but NOT actionable).
   `--actionable` scrolls the element into view first (same as click's measure) — a
   below-fold control is actionable; expect the page to be scrolled afterwards.
4. `click SEL --require-trusted` for user-path interactions — exit 1
   (`CLICK_REQUIRE_TRUSTED_FAIL`) means the element was NOT clickable as a user
   would click it (hidden/occluded/off-viewport); it never falls back to the
   untrusted `el.click()`. Exit 0 ⇒ the click was a trusted Input event.
5. `screenshot /tmp/drive-N.jpg --bind` — the second stdout line
   `BIND url=… loader=… t=…` ties the capture to its navigation: compare
   `loader=` with step 1's token — different = something navigated since, the
   screenshot does NOT show what you think. Read the image before claiming
   visual state.

## Circuit-breaker (hard limit)

Max **3** fix-verify iterations per finding. The 4th failure → STOP and report
honestly: what was tried, the last ASSERT_FAIL/CONSOLE_GATE_FAIL output, your
hypothesis. Token-burn without progress is a bug, not persistence.
(The limit of 3 is now **empirically validated** by the SP4 calibration: across
30 fix-verify runs the complete-cycle distribution was {1:10, 2:19, 3:1}, 0
censored — one cell needed all 3 cycles and succeeded on the 3rd, so a floor of 2
would have cut off a real repair. Keep 3. See
`docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md`.)

After editing product code, wait for the dev-server rebuild BEFORE re-testing:
`assert --js '<HMR-ready condition>' --timeout 30`, or re-`navigate --wait` and
re-run the gate. Testing a stale build wastes an iteration.

## Two modes (spec §4.4 — structural)

- **autonomous** (default): headless, runs the whole loop to completion, emits a
  pass/fail report with the machine-readable evidence (gate/assert outputs).
- **co-pilot**: headful; at each confirmation checkpoint surface to the human via
  AskUserQuestion ("so? does this look right?") before continuing.
- **Subagents are ALWAYS autonomous.** co-pilot is main-session-only: a subagent
  has no human channel — a co-pilot checkpoint inside one would hang forever. If
  you are running as a subagent, refuse co-pilot and run autonomous. Delegation
  prompts MUST hard-code "mode: autonomous".

## Subagent delegation (SP4 — ephemeral lanes)

The main session NEVER picks ports for subagents (that was the collision source —
hole H). Each subagent provisions its OWN ephemeral lane:

The whole lifecycle is ONE runnable block — launch with the clean-env guard,
bind the contract, pre-flight, drive, tear down. Strip every lane env var first
(the conftest `LANE_ENV_VARS` hermeticity canon: an inherited `LOOK_DRY_RUN=1`
would prevent the launch, `LOOK_INSECURE`/`LOOK_CERT_SPKI` silently alter flags,
a stray `CHROME_APP_NAME` pollutes later cdp.py calls — `launch.sh` deliberately
honors env-provided values, hermeticity is the caller's job):

```bash
PLUGIN="<plugin root>"                    # e.g. $CLAUDE_PLUGIN_ROOT
# 1. Launch ONCE, capturing stdout — the contract arrives on it.
out=$(env -u LOOK_PROFILE_DIR -u LOOK_INSECURE -u LOOK_DRY_RUN -u CHROME_BIN \
          -u LOOK_AUTOMATION -u CHROME_APP_NAME -u LOOK_CERT_SPKI \
          CDP_PORT=0 LOOK_HEADLESS=1 "$PLUGIN/skills/look/scripts/launch.sh" --automation) \
  || { echo "lane never came up (LANE_FAIL above)"; exit 1; }

# 2. Bind the contract — every later command uses these, nothing is implicit.
PORT=$(printf '%s\n' "$out" | sed -n 's/^CDP_PORT=//p' | tail -1)
LANE_PROFILE=$(printf '%s\n' "$out" | sed -n 's/^LANE_PROFILE=//p' | tail -1)
LANE_KILL_MATCH=$(printf '%s\n' "$out" | sed -n 's/^LANE_KILL_MATCH=//p' | tail -1)
LANE_BROWSER_BIN=$(printf '%s\n' "$out" | sed -n 's/^LANE_BROWSER_BIN=//p' | tail -1)
# Validate ALL four before ANY use: a partial contract must never reach the
# wrong-browser branch or teardown (an empty $LANE_KILL_MATCH would make pkill
# match far too much). Four explicit checks — NO eval / bash-only indirect
# expansion: this block runs in the AGENT'S shell, which may be zsh (CC Bash
# tool), where bash indirection dies with "bad substitution" (pilot wf_c33de294).
[ -n "$PORT" ]             || { echo "lane contract missing PORT — refusing"; exit 1; }
[ -n "$LANE_PROFILE" ]     || { echo "lane contract missing LANE_PROFILE — refusing"; exit 1; }
[ -n "$LANE_KILL_MATCH" ]  || { echo "lane contract missing LANE_KILL_MATCH — refusing"; exit 1; }
[ -n "$LANE_BROWSER_BIN" ] || { echo "lane contract missing LANE_BROWSER_BIN — refusing"; exit 1; }

# 3. Pre-flight (hole D, binary identity): /json/version's Browser string CANNOT
#    distinguish CfT from stock Chrome at the same version — binary PATH is the
#    only reliable check. Mismatch → STOP, never proceed on the wrong browser.
case "$LANE_BROWSER_BIN" in
  /0/.jaine/.browser/cft/*) : ;;
  *) echo "WRONG BROWSER: $LANE_BROWSER_BIN — refusing"; pkill -f -- "$LANE_KILL_MATCH"; exit 1 ;;
esac
# Graded runs ONLY: capture the liveness curl as the run's first log. $RUN_DIR is
# the directory the orchestrator gave you — ALWAYS path-prefix (a bare cmd-00.log
# lands in your cwd and grades 0). Generic (ungraded) delegations have no RUN_DIR —
# the guard makes this line a no-op for them instead of writing to "/cmd-00.log".
[ -n "${RUN_DIR:-}" ] && { curl -s -m 2 "http://localhost:$PORT/json/version"; echo "EXIT=$?"; } > "$RUN_DIR/cmd-00.log" 2>&1

# 4. Drive — lane contract unchanged: BOTH env keys on every cdp.py call.
CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" \
  python3 "$PLUGIN/skills/look/scripts/cdp.py" navigate "$TARGET_URL" --wait load

# 5. Teardown by the launcher-escaped pattern — verbatim, never hand-rolled.
#    The unique mktemp profile IS the ownership token: this pattern can only
#    ever kill your own browser, so parallel subagents cannot interfere.
pkill -f -- "$LANE_KILL_MATCH"
```

When a run must be graded externally (calibration, CI), wrap every command in
the capture form so the logs — not your retelling — carry the verdict. `$RUN_DIR`
is the directory the orchestrator gave you; path-prefix EVERY log (bare names
land in your cwd and the grader reads only `$RUN_DIR`):
```bash
{ CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" \
    python3 "<plugin>/skills/look/scripts/cdp.py" <command…>; echo "EXIT=$?"; } > "$RUN_DIR/cmd-NN.log" 2>&1
cat "$RUN_DIR/cmd-NN.log"
```

Two graded-run layout rules the grader enforces beyond the capture form:
- **Teardown evidence** (tasks with `teardown_check`): after the pkill, capture the
  port-free proof as `cmd-99.log` —
  `{ sleep 1; curl -s -m1 "http://localhost:$PORT/json/version" >/dev/null 2>&1 && echo PORT_STILL_ALIVE || echo PORT_FREE; echo "EXIT=0"; } > "$RUN_DIR/cmd-99.log" 2>&1`
- **Fix-verify tasks**: each fix-verify CYCLE writes its command logs into its own
  subdirectory — `mkdir -p "$RUN_DIR/iter-$K"` (K=1,2,3) and use
  `"$RUN_DIR/iter-$K/cmd-NN.log"` paths; only `cmd-00.log` (and `cmd-99.log`) stay at
  the `$RUN_DIR` root. The grader counts iterations from these directories and grades
  the highest-K **complete** cycle (one carrying the full command-log set) — flat logs
  grade 0 (`log-set-mismatch`/`no-iterations`). Do NOT `mkdir` an `iter-$K` you don't
  fill: once you go green, stop — an empty trailing dir is not an attempt (it used to
  false-fail the run; now skipped, but it still muddies the iteration count).

## Model routing (SP4 calibration, 2026-06-05)

Empirical (111-run calibration, freeze `75bac59`; full analysis:
`docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md`):

| Drive workload | Route to | Why |
|---|---|---|
| verify-core + ANY graded/calibration run | **sonnet** | 27/27 verify; correct defect classification; ~5× cheaper than opus at identical accuracy |
| fix-verify (iterative repair) | **sonnet** | reliable capture protocol + correct iteration discipline |

- **opus buys nothing over sonnet** here — identical verify accuracy (27/27), no speed
  gain, ~5× the cost. Don't reach for it on drive work.
- **haiku is NOT recommended for any graded/trusted drive run**: verify 15/27, a
  systematic "pass"-bias that rubber-stamps defects as success, lost the `cmd-00`
  capture form on 7 runs, and **overclaimed `self_success` on 15 of 37 runs**. Reserve
  it for throwaway, human-verified exploration only.
- **Validity:** calibrated at freeze `75bac59` against the model generation current
  then. Re-run the calibration (`grade_run.py` + the matrix workflow) before trusting
  these routes on a new model generation — a future haiku may close the gap.

## Cookie-seed (opt-in auth, spec §4.5)

For login-gated products: import cookies of SELECTED domains from the daily
browser into the lane:

```bash
python3 "<plugin>/skills/drive/scripts/cookie_seed.py" \
  --domains app.example.com --to-port $PORT [--from-port 9333] [--dry-run]
```

- Nothing is transferred implicitly: `--domains` is mandatory; subdomains match
  (dot-anchored — `evilgithub.com` never matches `github.com`).
- Output is per-domain COUNTS only — never cookie names or values.
- NEVER seeds into the daily browser — port 9333 AND a `CDP_PORT`-overridden
  daily are both refused by the script. SP2 ships cookies only; localStorage
  seeding is deferred until a real test needs it.
- Already-expired cookies (epoch-or-earlier expiry) are skipped, not resurrected.
- Re-run after re-login on the daily side (expired cookies re-import).

## OAuth / popup handoff (R2-S)

A login popup or OAuth redirect opens a NEW target — recover instead of losing
the flow:

1. `tabs` → identify the new tab (12-char id prefix or url substring).
2. Drive it pinned: `--target <SEL> fill …`, `--target <SEL> click … --require-trusted`.
3. When it closes, re-run `tabs`, re-pin the main tab, then `navigate --wait` +
   `console --gate` to re-establish a verified state before asserting anything.

## Teardown

```bash
pkill -f -- "--user-data-dir=<profile>($|[[:space:]])"   # anchored — never by port substring
```

Confirm the port is actually free before reusing the lane (headless Chrome can
serve CDP for a few seconds after SIGTERM):
`curl -s -m1 http://localhost:$PORT/json/version || echo free`.

## Assert patterns for modern frameworks (dogfood #172)

Selector-based `assert` uses `document.querySelector` — two DOM structures need
`--js` instead:

**Shadow DOM** (wavesurfer, Shoelace, web components): querySelector doesn't pierce
shadow boundaries. Use `--js` to traverse manually:
```bash
assert --js "!!document.querySelector('waveform-element')?.shadowRoot?.querySelector('canvas')"
```

**Reactive frameworks** (Alpine `x-if`, Vue `v-if`, React conditional render): the
element may be removed and re-inserted during a reactive cycle → flap → stability
window resets. Assert on **reactive state**, not DOM presence:
```bash
# Alpine: check the data property, not the DOM node
assert --js "Alpine.\$data(document.querySelector('[x-data]')).showPopup === true" --stable 300
```

When neither pattern applies and you see `ASSERT_FAIL never true`, use
`screenshot` as ground truth — if the screenshot shows the element, the selector
is wrong for the DOM structure (not an assert bug).

## Red flags — STOP and reassess

- Pre-flight shows a non-CfT browser on your chosen port → wrong lane, pick another.
- `screenshot --bind` loader ≠ navigate loader → stale capture; re-navigate.
- ASSERT_FAIL with `flapped Nx` → the UI is unstable (flaky class), not absent —
  raise `--stable`, investigate the flapping, don't just retry.
- The same gate error after 3 fix iterations → circuit-breaker: report, don't loop.
- You are about to point cdp.py at port 9333 from this skill → that is /look's
  daily browser; drive NEVER touches it.
