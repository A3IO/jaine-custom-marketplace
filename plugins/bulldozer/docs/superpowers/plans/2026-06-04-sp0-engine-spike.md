# SP0: `/drive` Engine Spike — Playwright vs cdp.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Empirically choose the `/drive` automation engine — Playwright (`connect_over_cdp`), the existing `cdp.py`, or a bounded both — by measuring one representative flaky-prone drive scenario on **three** configurations (naive cdp.py, best-effort cdp.py, Playwright) against fixed criteria, then recording the decision for SP2.

**Architecture:** Throwaway spike. Build a deterministic async HTML fixture that exercises the exact pains the mine surfaced (async element appearance → wait, delayed-actionable button → actionability, console error → gate). Implement the same `navigate → click → wait → assert → console-check` scenario on (a) **naive cdp.py** (blind `time.sleep` + untrusted js-click — worst-case usage), (b) **best-effort cdp.py** (real `cdp.py wait` + trusted `cdp.py click`), and (c) **Playwright** attached to the lane. Run each ×10 to measure flakiness on a **self-contained** runner that owns lane+fixture lifecycle, then write a decision doc. **Spike code lives under `spike/` and is NOT production** — it informs SP2, it does not ship.

**Tech Stack:** Python 3, existing `skills/look/scripts/cdp.py` + `launch.sh` lane (`CDP_PORT`/`LOOK_PROFILE_DIR`/`LOOK_HEADLESS`), Playwright for Python (`connect_over_cdp` — no bundled browser needed), `uv` for dependency management.

**Decision criteria (the spike measures each):**
1. **Auto-wait reliability** — flaky-vs-real over 10 runs, across all three configs (naive cdp.py is expected to flake on blind sleeps; best cdp.py and Playwright should not). The three-way split (R1-F2) separates "cdp.py the tool" from "cdp.py used naively".
2. **Actionability** — best cdp.py needs an *explicit* `wait` on `:not([disabled])` before click; Playwright `click` auto-waits enabled. Measure whether each reaches the delayed button correctly and how much ceremony it costs.
3. **Assertion ergonomics** — LOC + explicit-wait count: best cdp.py (explicit waits each step) vs Playwright (`expect` implicit).
4. **`connect_over_cdp` fidelity** — does Playwright attach cleanly to the lane and drive the right tab? (The spike runs on a stock-Chrome lane; CfT-specific attach follows trivially — CDP is identical — and is re-verified on the real CfT binary in SP1, per spec §5 SP0 criteria, which scopes CfT-specific fidelity to SP1.)
5. **Dependency cost** — install size/complexity; does it need its own browser?
6. **Trusted input** — both must reach `isTrusted=true` (cdp.py shipped PR#140 trusted press+release; Playwright native).
7. **Console-gate detection (measured)** — both trigger `#break`; record whether each DETECTS the ReferenceError. cdp.py reads via a fresh one-shot `console` subprocess that subscribes AFTER the error fired (relies on Console.enable replay — may MISS); Playwright's persistent subscription should catch. The asymmetry, if any, is a real measured outcome — not a forced pass (R1-F3 / R2-F2).
8. **Agent-parseability** — output is a stable `*_PASS`/`*_FAIL` + exit code an agent can branch on.

**Scope note:** The spike runs on the existing lane mechanism (any non-9333 port). It does NOT require SP1 (CfT) — engine choice (auto-wait/locators) is orthogonal to binary isolation. `connect_over_cdp` attaches to whatever Chrome the lane launched.

---

## File Structure

- `spike/async-page.html` (create) — deterministic fixture: a Load button that reveals `#result` after 800ms + logs to console; a second button that becomes enabled after 500ms (actionability); a "break" button that throws a console error (gate test).
- `spike/scenario_cdp.py` (create) — the scenario via `cdp.py` subcommands, with a `naive|best` mode arg (R1-F2). Port via `CDP_PORT` env (R1-F1). Tab pinned by url via `--target` (R1-F4). Optional `--expect-console-error` parity mode (R1-F3).
- `spike/scenario_playwright.py` (create) — the same scenario via Playwright `connect_over_cdp`, page pinned by url (R1-F4), same `--expect-console-error` mode (R1-F3).
- `spike/run_spike.sh` (create) — **self-contained** (R1-F5): starts the fixture server + a fresh lane, waits for CDP, runs all three configs ×10, runs the console-parity check, tallies, and tears everything down via `trap`.
- `spike/verify_attach.py` (create) — Playwright attach smoke-check that pins+prints the target tab (R1-F4).
- `docs/superpowers/analysis/2026-06-04-sp0-engine-decision.md` (create) — the decision doc (criteria table + verdict + rationale for SP2).
- `docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` (modify, §5 SP3 row + §4.6) — record the decision inline.

---

## Task 1: Install Playwright + verify `connect_over_cdp` to the lane

**Files:**
- Create: `spike/verify_attach.py`

- [ ] **Step 1: Install Playwright (Python, no bundled browser)**

Run:
```bash
cd /0/.aitemp/bulldozer-drive-sp0
uv venv .venv-spike   # dedicated venv — `uv pip --python python3` needs one
uv pip install --python .venv-spike/bin/python playwright   # Playwright scenarios run via .venv-spike/bin/python; cdp.py scenarios via system python3
```
Expected: `playwright` installed. We do NOT run `playwright install` — `connect_over_cdp` uses the lane's Chrome, not a bundled browser.

- [ ] **Step 2: Write the attach smoke-check (pins + prints the target tab — R1-F4)**

```python
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
```

- [ ] **Step 3: Launch a lane and run the smoke-check**

Run:
```bash
CDP_PORT=9360 LOOK_PROFILE_DIR=/tmp/spike-smoke LOOK_HEADLESS=1 \
  skills/look/scripts/launch.sh about:blank &
sleep 5
python3 spike/verify_attach.py http://127.0.0.1:9360
pkill -f 'user-data-dir=/tmp/spike-smoke'; rm -rf /tmp/spike-smoke   # smoke teardown
```
Expected: a line `ATTACH_OK target=... title=...`. If it errors, Playwright cannot attach to the lane — record that as a criterion-4 finding (do not abort; cdp.py remains the fallback engine).

- [ ] **Step 4: Commit**

```bash
git add spike/verify_attach.py
git commit -m "spike(sp0): playwright connect_over_cdp attach smoke-check (pins tab)"
```

---

## Task 2: Deterministic async fixture

**Files:**
- Create: `spike/async-page.html`

- [ ] **Step 1: Write the fixture**

```html
<!doctype html>
<html><head><meta charset="utf-8"><title>SP0 Async Spike</title></head>
<body>
  <button id="load">Load</button>
  <div id="result"></div>
  <button id="delayed" disabled>Delayed</button>
  <div id="delayed-result"></div>
  <button id="break">Break</button>
  <script>
    // #result appears 800ms after click — tests wait-for-element.
    document.getElementById('load').addEventListener('click', function () {
      setTimeout(function () {
        document.getElementById('result').textContent = 'loaded';
        console.log('SPIKE_RESULT_READY');
      }, 800);
      // #delayed enables 1500ms AFTER this click (not page load) — exercises the actionability
      // race (R2-F1): naive (blind 1s sleep, immediate click) hits it while still disabled →
      // no-op → fails; best (explicit wait !disabled) and Playwright (auto-wait) handle it.
      setTimeout(function () {
        document.getElementById('delayed').disabled = false;
      }, 1500);
    });
    document.getElementById('delayed').addEventListener('click', function () {
      document.getElementById('delayed-result').textContent = 'clicked';
    });
    // #break throws — tests console-error gate.
    document.getElementById('break').addEventListener('click', function () {
      undefined_function_xyz();  // ReferenceError → console
    });
  </script>
</body></html>
```

- [ ] **Step 2: Verify it serves + renders**

Run:
```bash
( cd spike && python3 -m http.server 9402 >/dev/null 2>&1 & echo $! > /tmp/spike-fix-pid )
sleep 1
curl -s http://127.0.0.1:9402/async-page.html | grep -c 'SPIKE_RESULT_READY'
kill "$(cat /tmp/spike-fix-pid)" 2>/dev/null   # teardown (run_spike owns its own server on 9401)
```
Expected: `1` (fixture served, script present).

- [ ] **Step 3: Commit**

```bash
git add spike/async-page.html
git commit -m "spike(sp0): deterministic async fixture (wait/actionability/console)"
```

---

## Task 3: Scenario on `cdp.py` (naive + best modes) + measure

**Files:**
- Create: `spike/scenario_cdp.py`

Two modes (R1-F2 fairness): `naive` = blind `time.sleep` + untrusted js-click (worst-case cdp.py usage); `best` = real `cdp.py wait` (`--js` condition) + trusted `cdp.py click` (#140). Port via `CDP_PORT` env (R1-F1). Tab pinned by url via `--target` (R1-F4). Optional `--expect-console-error` parity (R1-F3).

- [ ] **Step 1: Write the scenario**

```python
# spike/scenario_cdp.py — drive scenario via cdp.py. Two modes for a fair R1-F2 comparison.
# Usage: scenario_cdp.py <naive|best> <PORT> <URL> [--expect-console-error]
import subprocess, sys, os, time
CDP = "skills/look/scripts/cdp.py"
MODE = sys.argv[1]                                  # "naive" | "best"
PORT = sys.argv[2]
URL  = sys.argv[3]
EXPECT_ERR = "--expect-console-error" in sys.argv   # R1-F3 parity

def cdp(*args):
    # cdp.py takes the port via CDP_PORT env, NOT a --port flag (R1-F1).
    # --target pins every command to the fixture tab by url substring (R1-F4).
    env = {**os.environ, "CDP_PORT": PORT}
    return subprocess.run(["python3", CDP, "--target", "async-page", *args],
                          capture_output=True, text=True, env=env)

def fail(m): print("CDP_FAIL " + MODE + ": " + m); sys.exit(1)

cdp("navigate", URL)
if MODE == "naive":
    time.sleep(2)                                   # blind sleep — guessed load
    cdp("js", "document.getElementById('load').click(); 'ok'")          # untrusted
    time.sleep(1)                                   # blind sleep — guessed < 800ms async
    txt = cdp("js", "document.getElementById('result').textContent")
    if "loaded" not in txt.stdout: fail("result not loaded (sleep race)")
    cdp("js", "document.getElementById('delayed').click(); 'ok'")       # fires before enabled
    dr = cdp("js", "document.getElementById('delayed-result').textContent")
    if "clicked" not in dr.stdout: fail("delayed not clicked (no actionability wait)")
else:  # best
    cdp("wait", "#load")                            # auto-wait present
    cdp("click", "#load")                           # trusted click (#140)
    w = cdp("wait", "--js", "document.getElementById('result').textContent==='loaded'")
    if w.returncode != 0: fail("result wait timed out")
    cdp("wait", "--js", "!document.getElementById('delayed').disabled")  # explicit actionability
    cdp("click", "#delayed")                        # trusted click
    d = cdp("wait", "--js", "document.getElementById('delayed-result').textContent==='clicked'")
    if d.returncode != 0: fail("delayed wait timed out")
# console gate — MEASURED detection (R2-F2), NOT a forced parity-pass. The cdp.py `console`
# read is a FRESH subprocess that subscribes AFTER #break already fired, so it depends on
# Console.enable replaying the buffered error — empirically unknown, so we REPORT detected/missed
# (exit 0 either way) and let the spike resolve it. A MISS here is a real cdp.py limitation.
if EXPECT_ERR:
    cdp("click", "#break")                          # trigger ReferenceError
    cdp("wait", "--js", "true")                     # one poll tick to let it surface
    con = cdp("console")
    has_err = "ReferenceError" in con.stdout or "exception" in con.stdout
    print("CDP_CONSOLE " + MODE + " " + ("DETECTED" if has_err else "MISSED")); sys.exit(0)
con = cdp("console")
if "ReferenceError" in con.stdout or "exception" in con.stdout: fail("unexpected console error")
print("CDP_PASS " + MODE); sys.exit(0)
```

- [ ] **Step 2: Run each mode once against a lane (verifies cdp.py API assumptions)**

Run (`run_spike.sh` from Task 5 owns the long-lived lane; for a one-off check, launch one here and tear it down):
```bash
CDP_PORT=9360 LOOK_PROFILE_DIR=/tmp/spike-t3 LOOK_HEADLESS=1 skills/look/scripts/launch.sh "http://127.0.0.1:9402/async-page.html" &
( cd spike && python3 -m http.server 9402 >/dev/null 2>&1 & echo $! > /tmp/spike-t3-fix )
sleep 5
python3 spike/scenario_cdp.py best  9360 http://127.0.0.1:9402/async-page.html; echo "best exit=$?"
python3 spike/scenario_cdp.py naive 9360 http://127.0.0.1:9402/async-page.html; echo "naive exit=$?"
pkill -f 'user-data-dir=/tmp/spike-t3'; kill "$(cat /tmp/spike-t3-fix)" 2>/dev/null; rm -rf /tmp/spike-t3
```
Expected: `best` prints `CDP_PASS best`; `naive` may pass or `CDP_FAIL` (race). Either is a valid measurement. **If `best` fails, the cdp.py `wait`/`click`/`--target` API assumption is wrong — STOP and re-read `cdp.py`.**

- [ ] **Step 3: Commit**

```bash
git add spike/scenario_cdp.py
git commit -m "spike(sp0): cdp.py scenario, naive+best modes (CDP_PORT env, real wait/click, --target)"
```

---

## Task 4: Scenario on Playwright + measure

**Files:**
- Create: `spike/scenario_playwright.py`

- [ ] **Step 1: Write the Playwright scenario (auto-wait, page pinned by url — R1-F4; parity mode — R1-F3)**

```python
# spike/scenario_playwright.py — same scenario via Playwright connect_over_cdp.
# No blind sleeps: expect() auto-waits; click() waits for actionability.
# Usage: scenario_playwright.py <CDP_URL> <URL> [--expect-console-error]
import sys
from playwright.sync_api import sync_playwright, expect

cdp_url = sys.argv[1]                               # http://127.0.0.1:9360
url = sys.argv[2]                                   # http://127.0.0.1:9401/async-page.html
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
```

- [ ] **Step 2: Run it once (against the Task 3 one-off lane pattern, or Task 5 runner)**

Run:
```bash
python3 spike/scenario_playwright.py http://127.0.0.1:9360 http://127.0.0.1:9402/async-page.html; echo "exit=$?"
```
Expected: `PW_PASS` (auto-wait should make this deterministic) or `PW_FAIL ...`. Record either.

- [ ] **Step 3: Commit**

```bash
git add spike/scenario_playwright.py
git commit -m "spike(sp0): Playwright scenario (auto-wait, page-pinned, console parity)"
```

---

## Task 5: Self-contained runner — measure all 3 configs ×10 + console parity

**Files:**
- Create: `spike/run_spike.sh`

- [ ] **Step 1: Write the runner (owns lane+fixture lifecycle — R1-F5; bash 3.2 compatible — no assoc arrays)**

```bash
#!/usr/bin/env bash
# spike/run_spike.sh — self-contained: starts fixture + a fresh lane, runs 3 configs x10,
# checks console parity, tears everything down via trap. Idempotent (per-PID profile + own ports).
set -u
PORT=9360; FIXPORT=9401
FIX="http://127.0.0.1:$FIXPORT/async-page.html"; CDP_URL="http://127.0.0.1:$PORT"
PROFILE="/tmp/spike-profile-$$"
FIXPID=""
cleanup() {
  pkill -f "user-data-dir=$PROFILE" 2>/dev/null
  [ -n "$FIXPID" ] && kill "$FIXPID" 2>/dev/null
  rm -rf "$PROFILE"
}
trap cleanup EXIT
# Preflight (R3-F1): refuse to run if our fixed ports are already occupied — otherwise we'd
# attach to a stale lane on $PORT (which would pass the readiness poll) or measure a foreign
# fixture server on $FIXPORT (our own bind would fail), producing invalid data.
for p in "$PORT" "$FIXPORT"; do
  if lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "FATAL: port $p already in use — refusing to measure a stale/foreign endpoint; free it and retry." >&2
    exit 1
  fi
done
# fixture server (own PID)
( cd spike && exec python3 -m http.server "$FIXPORT" >/dev/null 2>&1 ) &
FIXPID=$!
# fresh lane, single tab = fixture (R1-F4)
CDP_PORT=$PORT LOOK_PROFILE_DIR="$PROFILE" LOOK_HEADLESS=1 \
  skills/look/scripts/launch.sh "$FIX" >/dev/null 2>&1 &
# wait for CDP up (poll cdp.py status), then ABORT if still down — no false 0/10 tallies (R2-F3)
for i in $(seq 1 20); do
  CDP_PORT=$PORT python3 skills/look/scripts/cdp.py status >/dev/null 2>&1 && break
  sleep 0.5
done
CDP_PORT=$PORT python3 skills/look/scripts/cdp.py status >/dev/null 2>&1 || {
  echo "FATAL: lane not ready on $PORT after readiness poll — aborting (no measurements)" >&2; exit 1; }
naive_pass=0; best_pass=0; pw_pass=0
for i in $(seq 1 10); do
  python3 spike/scenario_cdp.py naive "$PORT" "$FIX" >/dev/null 2>&1 && naive_pass=$((naive_pass+1))
  python3 spike/scenario_cdp.py best  "$PORT" "$FIX" >/dev/null 2>&1 && best_pass=$((best_pass+1))
  python3 spike/scenario_playwright.py "$CDP_URL" "$FIX" >/dev/null 2>&1 && pw_pass=$((pw_pass+1))
done
echo "cdp.py naive:  $naive_pass/10 passed"
echo "cdp.py best:   $best_pass/10 passed"
echo "playwright:    $pw_pass/10 passed"
echo "--- console-gate detection (MEASURED — cdp.py one-shot may MISS vs Playwright subscribe, R2-F2) ---"
python3 spike/scenario_cdp.py best "$PORT" "$FIX" --expect-console-error 2>&1 | tail -1
python3 spike/scenario_playwright.py "$CDP_URL" "$FIX" --expect-console-error 2>&1 | tail -1
echo "--- ergonomics ---"
echo "cdp.py scenario LOC:     $(wc -l < spike/scenario_cdp.py)"
echo "playwright scenario LOC: $(wc -l < spike/scenario_playwright.py)"
```

- [ ] **Step 2: Run the full spike (twice — proves idempotent teardown, R1-F5)**

Run:
```bash
chmod +x spike/run_spike.sh
./spike/run_spike.sh
echo "=== second run (must be clean, no stale lane/port) ==="
./spike/run_spike.sh
lsof -nP -iTCP:9360 -sTCP:LISTEN || echo "port 9360 free after runs (good)"
```
Expected: two tallies. Hypothesis from the mine: naive cdp.py < 10/10 (blind-sleep races), best cdp.py 10/10, Playwright 10/10; Playwright fewer LOC. **Record the ACTUAL numbers — do not assume.** Second run identical-shaped (no stale-state corruption).

- [ ] **Step 3: Commit**

```bash
git add spike/run_spike.sh
git commit -m "spike(sp0): self-contained 3-config runner + console parity + idempotent teardown"
```

---

## Task 6: Decision doc + record in spec

**Files:**
- Create: `docs/superpowers/analysis/2026-06-04-sp0-engine-decision.md`
- Modify: `docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` (§5 SP3 row + §4.6)

- [ ] **Step 1: Write the decision doc**

Fill the criteria table with the ACTUAL measurements from Task 5 (three-config flaky counts, LOC, console parity, attach result), then a verdict. Template:

```markdown
# SP0 Decision: /drive automation engine

*Date: 2026-06-04. Measured via spike/ (see plan 2026-06-04-sp0-engine-spike.md).*

| Criterion | cdp.py naive | cdp.py best | Playwright | Winner |
|---|---|---|---|---|
| Auto-wait reliability (×10) | <N>/10 | <N>/10 | <N>/10 | <…> |
| Actionability (delayed button) | <pass/fail> | <pass/fail, needs explicit wait> | <pass/fail, auto> | <…> |
| Assertion ergonomics (LOC) | — | <LOC> | <LOC> | <…> |
| connect_over_cdp fidelity | n/a | n/a | <ATTACH_OK target?> | <…> |
| Dependency cost | none | none | <install size> | <…> |
| Trusted input | untrusted js-click | trusted (#140) | native | <…> |
| Console-gate parity | <detected?> | <detected?> | <detected?> | <…> |

**Verdict:** <Playwright / cdp.py / bounded both>.

**Rationale:** <2-4 sentences grounded in the numbers — note that naive-vs-best isolates "cdp.py the tool" from "cdp.py used naively", so the verdict is about ergonomics/safety, not raw capability>.

**If "both":** <the exact boundary — which engine for which task class, so /look (cdp.py observation/fallback) and /drive (assertions) cannot diverge — per panel R2-F/dual-stack>.

**Consequence for SP2:** <what the verify-core is built on>.
```

- [ ] **Step 2: Record the decision in the spec**

Edit `§5 SP3` row and `§4.6` of the umbrella spec to state the chosen engine (replace "if SP0 says so" with the actual decision). Keep it one line + a pointer to the decision doc.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/analysis/2026-06-04-sp0-engine-decision.md docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md
git commit -m "spike(sp0): engine decision + record in umbrella spec"
```

---

## Self-Review (run after drafting, before execution)

1. **Spec coverage (SP0 row §5 + criteria §4.6):** Tasks 1–6 implement the SP0 spike (attach, fixture, naive+best cdp.py scenario, Playwright scenario, self-contained 3-config measure, decide, record). ✓
2. **Placeholder scan:** the only intentional fill-ins are the ACTUAL measurements in Task 6's table — those are outputs of execution, not plan placeholders. All code steps contain real code. ✓
3. **Type consistency:** `scenario_cdp.py` takes `(mode, port, url, [--expect-console-error])`; `scenario_playwright.py` takes `(cdp_url, url, [--expect-console-error])`; both print `*_PASS`/`*_FAIL` + exit code; `run_spike.sh` invokes exactly those signatures (naive/best for cdp, cdp_url for pw). cdp.py port is `CDP_PORT` env everywhere (R1-F1); tab pin is `--target async-page` (cdp) / url-substring (pw) everywhere (R1-F4). ✓
4. **Round-1 fixes applied:** R1-F1 (CDP_PORT env), R1-F2 (naive+best three-config), R1-F3 (--expect-console-error parity), R1-F4 (--target / page-by-url + clean single-tab lane), R1-F5 (self-contained runner + trap + idempotent). ✓
5. **Cleanup note:** `spike/` is throwaway. `run_spike.sh` owns its lane (per-PID `/tmp/spike-profile-$$`) + fixture (own `FIXPID`) and tears them down via `trap cleanup EXIT`. Task 1/2/3 one-off launches each clean up their own `/tmp/spike-*` + fixture PID inline.
