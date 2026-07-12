---
name: drive
description: Drives product testing in an isolated Chrome-for-Testing browser with a self-verify core — navigate-that-waits, console error gate, stability-window assertions, trusted clicks, navigation-bound screenshots, opt-in cookie-seed auth. ALWAYS invoke for "протестируй UI", "проверь в браузере что работает", "прогони e2e по странице", "drive the app", "run a browser test", "verify this page works". Do NOT use for looking at the user's own daily browser or his real logins — that is /bulldozer:look (stock Chrome, port 9333). Supports autonomous (headless, runs to completion) and co-pilot (headful, human checkpoints) modes.
argument-hint: [URL] [test task]
allowed-tools: ["Bash", "Read", "Write", "AskUserQuestion"]
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
  issue — do not hack around it. "Might be nicer" is not a wall. The recipe is in
  **Feedback** at the bottom of this file — use it; do not guess the labels.

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

When the breaker trips, also write one durable line (#322 A1 — a tripped breaker
was previously invisible to log mining). Shell state does not persist between
Bash calls — resolve the plugin dir IN the same call:

```bash
BULLDOZER_DIR=$( { [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "$CLAUDE_PLUGIN_ROOT/lib" ] \
  && printf '%s\n' "$CLAUDE_PLUGIN_ROOT"; } || ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null | head -1 )
python3 "$BULLDOZER_DIR/lib/bulldozer_log.py" \
    "${BULLDOZER_DRIVE_LOG:-$HOME/.claude/hooks/bulldozer-drive.log}" \
    circuit-breaker "port=<lane port>" "finding=<short label>" "iterations=3"
```

Substitute the CONCRETE lane port (`$PORT` from the setup call does not survive
into a later Bash call — same non-persistent-shell reason as above).
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
# $CLAUDE_PLUGIN_ROOT is NOT exported to the Bash tool (#221) — resolve the plugin dir from
# the cache (honor the var if set). PLUGIN feeds the launch.sh + cdp.py calls below.
PLUGIN=$( { [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "$CLAUDE_PLUGIN_ROOT/skills/look" ] \
  && printf '%s\n' "$CLAUDE_PLUGIN_ROOT"; } || ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null | head -1 )
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
# lane-stop ONLY after the port is confirmed free — a delivered SIGTERM is not a
# terminated process (headless Chrome can serve CDP for seconds; #328 r8)
for i in 1 2 3 4 5; do
  curl -s -m1 "http://localhost:$PORT/json/version" >/dev/null 2>&1 || break; sleep 0.5
done
curl -s -m1 "http://localhost:$PORT/json/version" >/dev/null 2>&1 || \
  python3 "$PLUGIN/lib/bulldozer_log.py" \
    "${BULLDOZER_DRIVE_LOG:-$HOME/.claude/hooks/bulldozer-drive.log}" \
    lane-stop "port=$PORT" "profile=$LANE_PROFILE"   # #322: lifecycle completes (start→stop)
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
BULLDOZER_DIR=$( { [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "$CLAUDE_PLUGIN_ROOT/lib" ] \
  && printf '%s\n' "$CLAUDE_PLUGIN_ROOT"; } || ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null | head -1 )
python3 "$BULLDOZER_DIR/lib/bulldozer_log.py" \
    "${BULLDOZER_DRIVE_LOG:-$HOME/.claude/hooks/bulldozer-drive.log}" \
    lane-stop "port=<lane port>" "profile=<profile>"   # #322: lane lifetime becomes computable (start→stop)
```

Confirm the port is actually free before reusing the lane (headless Chrome can
serve CDP for a few seconds after SIGTERM):
`curl -s -m1 http://localhost:$PORT/json/version || echo free`.

Write the `lane-stop` line ONLY after the port-free confirm succeeds — an
unconditional stop event would report a still-running lane as cleaned (#328 r7).

## Logging (#322 PR5)

`/drive` has its own stable channel: `~/.claude/hooks/bulldozer-drive.log` (env
override `BULLDOZER_DRIVE_LOG`; canonical grammar via `lib/bulldozer_log.py` —
sanitized values, `session=`, 5MB rotation):

- `event=drive-invoke` — start marker from the UserPromptSubmit hook.
- `event=lane-start` / `event=lane-fail` — written by `launch.sh` for every
  non-9333 lane LAUNCH ATTEMPT (port/profile/headless/automation/ephemeral/
  insecure/pid; fail lines carry `reason=` — missing binary, DevToolsActivePort
  timeout/garble, CDP silence, Chrome death). Pre-launch env-validation
  rejections (malformed CDP_PORT/profile) exit without a line — caller bugs,
  not lane events. The daily 9333 browser never logs here.
- `event=cookie-seed` — every `cookie_seed.py` invocation, success AND failure
  (`from_port`/`to_port`/`domains`/`cookies=N`/`ok=`/`reason=` — counts only,
  never cookie names or values).
- `event=circuit-breaker` — written per the Circuit-breaker section above.

cdp.py traffic itself lands in `bulldozer-look.log` with `port=` on every line —
`/drive` lane activity is separable from the daily browser by port.

## Assert patterns for modern frameworks (dogfood #172)

Selector-based `assert` uses `document.querySelector` — two DOM structures need
`--js` instead:

**Shadow DOM — three-route routing** (wavesurfer, Shoelace, web components):

The `ax` snapshot sees through shadow DOM including **closed** roots (the only
channel that can — `.shadowRoot` returns null for closed). Shadow hosts appear with
`[shadow=open]` or `[shadow=closed]` markers in the snapshot.

| What's inside shadow | Route | Example |
|---|---|---|
| **Semantic elements** (buttons, inputs, headings) | `ax` → `assert/click --ref N` | Button in closed Shoelace component — `ax` shows it with `[ref=N]`, click it directly |
| **Canvas / non-semantic** (open shadow) | `--js` with `.shadowRoot` | `assert --js "!!document.querySelector('waveform-element')?.shadowRoot?.querySelector('canvas')"` |
| **Canvas / non-semantic** (closed shadow) | `screenshot` | No AX node, no JS access — visual channel only |

**Reactive frameworks** (Alpine `x-if`, Vue `v-if`, React conditional render): the
element may be removed and re-inserted during a reactive cycle → flap → stability
window resets. Assert on **reactive state**, not DOM presence (ref also stales on
re-insert — honest `REF_STALE`, not a solution for this class):
```bash
# Alpine: check the data property, not the DOM node
assert --js "Alpine.\$data(document.querySelector('[x-data]')).showPopup === true" --stable 300
```

When neither pattern applies and you see `ASSERT_FAIL never true`, use
`screenshot` as ground truth — if the screenshot shows the element, the selector
is wrong for the DOM structure (not an assert bug).

**ax as default text ground truth:** For text/state verification (what's on the page,
button states, table contents), use `ax` before `screenshot` — it's 2-6× cheaper in
tokens and more accurate for cheap models. Always `wait` or `assert` before `ax` to
avoid snapshotting intermediate state. The chain `ax` → `click/fill/key --ref` replaces
the old `js querySelector → click SELECTOR` pattern with zero CSS selectors.

## Red flags — STOP and reassess

- Pre-flight shows a non-CfT browser on your chosen port → wrong lane, pick another.
- `screenshot --bind` loader ≠ navigate loader → stale capture; re-navigate.
- ASSERT_FAIL with `flapped Nx` → the UI is unstable (flaky class), not absent —
  raise `--stable`, investigate the flapping, don't just retry.
- The same gate error after 3 fix iterations → circuit-breaker: report, don't loop.
- You are about to point cdp.py at port 9333 from this skill → that is /look's
  daily browser; drive NEVER touches it.

## Feedback

If you hit friction while driving — a documented behavior that isn't real, a missing
capability, an unhelpful error, or the **engine wall** — file a GitHub issue so it gets
fixed. Use the command below verbatim: the labels are part of the convention, not a
guess (a consumer session once filed with `bulldozer` alone and the rest had to be added
by hand afterwards — #186).

**Create issue when:**
1. **Engine wall (the SP0 doctrine step)** — a real test demonstrably needs something
   `cdp.py` cannot do (rich locators, actionability beyond `assert --actionable`, a
   Playwright-only feature). Show the test that is blocked. "Might be nicer" is NOT a wall.
2. SKILL.md describes behavior X, the lane does Y.
3. A verify-core primitive is wrong: a gate that misses a real error, an `assert` that
   flaps on a stable element, a `screenshot --bind` loader that never matches.
4. The lane itself misbehaves — launch fails, teardown leaves a browser, cookie-seed
   lands nothing.
5. You had to work around the standard path to finish the task.

**Do NOT create issue when:** your own bad selector/arguments; a genuinely broken product
under test (that is the FINDING, report it to the user); a limitation this file documents
as known.

**Command — two steps. Do NOT inline the body into the shell.**

The body carries text you copied from the page under test: a FAIL line, a console error,
a stack trace. That text is UNTRUSTED — a page can put `` `…` `` or `$(…)` in an error
message, and a shell-expanded heredoc would execute it as you. So the body goes to a file
via the **Write tool** (no shell involved), and only then to `gh`.

**Step 0 — take a private path** (two drive sessions can file at once; a fixed
`/tmp/drive-issue.md` would let one overwrite the other's evidence mid-flight):

```bash
mktemp -d /tmp/drive-issue-XXXXXX    # prints e.g. /tmp/drive-issue-v8vdhC
```

A **directory**, not a file, and the X's must be the LAST characters — two BSD-`mktemp`
traps, both silent: `mktemp /tmp/x-XXXXXX.md` exits 0 and prints the template *verbatim*
(BSD substitutes trailing X's only — so every session would collide on the same path
again), and the file form CREATES the file, which makes the Write tool refuse it as an
existing file it has not read.

**Step 1 — Write the body to `<printed-dir>/body.md`** (Write tool, verbatim text, no
shell; the file does not exist yet, so Write is clean):

```markdown
## What I was doing
{test task + target URL}

## What I expected
{expected behavior}

## What happened
{actual behavior, the exact FAIL line / gate output — paste it, do not paraphrase}

## Empirics
- Lane: port {CDP_PORT}, profile {LANE_PROFILE}, mode {autonomous|co-pilot}
- Reproducing command: {the cdp.py invocation}
- Log: {relevant lines from ~/.claude/hooks/bulldozer-drive.log}

## Workaround used
{what was done instead, or "none — blocked"}

## Environment
- Skill: drive
```

**Step 2 — append the environment facts and file it** (only trusted, self-generated values
are expanded here):

```bash
BODY=<printed-dir>/body.md               # ← the dir mktemp printed in step 0
{ printf -- '- Plugin version: %s\n' \
    "$(jq -r .version "$(ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json 2>/dev/null | head -1)" 2>/dev/null || echo unknown)"
  printf -- '- Project: %s\n' "$(pwd)"; } >> "$BODY"     # BODY = the step-0 path

gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,drive" \
  --title "[feedback/drive] short description" \
  --body-file "$BODY"
```

**For an engine wall (trigger #1)** the label set and title differ — use this invocation,
not the one above:

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "enhancement,bulldozer,drive" \
  --title "feat(drive): <capability> — cdp.py wall" \
  --body-file "$BODY"
```

The body MUST carry the blocked test — a wall is a demonstrated failure, not an opinion.
Delete the temp dir once `gh` has accepted the issue.

After creating the issue, tell the user:
> "Я завела issue по drive: {URL}. Продолжить с workaround или сначала пофиксим?"
