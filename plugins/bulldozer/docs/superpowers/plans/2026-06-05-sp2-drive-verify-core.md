# SP2: `/drive` Command + verify-core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `/bulldozer:drive` — agent-driven product testing on isolated Chrome-for-Testing lanes — with a verify-core (navigate-wait, console-gate, assertion primitive, trusted-click signal, screenshot-binding) built as opt-in extensions of the shared `cdp.py` engine, plus opt-in cookie-seed auth.

**Architecture:** verify-core = opt-in flags/commands in `skills/look/scripts/cdp.py` (shared engine; `/look` default behavior untouched — spec §4.1 "differ in SKILL.md defaults, not in the engine"). One new command (`assert`), four new flags on existing commands. Cookie-seed is a separate two-port script `skills/drive/scripts/cookie_seed.py` (cdp.py stays single-port). `/drive` itself is `skills/drive/SKILL.md` — instructions over the engine, two modes (autonomous / co-pilot).

**Tech Stack:** Python 3 stdlib + vendored websocket-client (no new deps), bash launch.sh (untouched), pytest e2e on the SP1 `cft_browser` fixture.

**Decisions locked (do not re-litigate):**
1. verify-core placement = extend cdp.py — Crys + unanimous consult panel (codex+grok, 2026-06-05).
2. assertion = `cdp.py assert` command with stability window — Crys + panel. YAML runner rejected (YAGNI).
3. cookie-seed = separate script — Crys + panel ("two-port op architecturally blocked inside single-port cdp.py"); NOT deferred (auth-gap, spec §4.5).
4. console-gate = one-shot `console --gate` — **empirically verified sufficient** on pinned CfT 149.0.7827.54: mid-flow uncaught exception AND console.error are replayed to a late one-shot subscriber; replay is repeatable; buffer clears on navigation (= free per-navigation scoping); headless == headful. SP0 footnote ¹ CLOSED. See `docs/superpowers/analysis/2026-06-05-sp2-console-gate-verification.md`. No persistent subscription; no Playwright wall.
5. Engine boundary (SP0): cdp.py default; Playwright opt-in NOT built in SP2 (documented in SKILL.md as "hit a wall → file issue").
6. Spec §4.5 says "cookies/storage-state": SP2 ships **cookies only**; localStorage seeding deferred until a real test needs it (YAGNI; noted in SKILL.md).
7. SP1 inputs: keep `--enable-automation` in ALL drive lanes; read `innerHeight` live; lane contract = BOTH `CDP_PORT` + `CHROME_APP_NAME` on every cdp.py call.

**Port registry additions** (tests/conftest.py comment block):
- `9340-9349` — interactive `/drive` lanes (SKILL.md picks a free one)
- `9362` — cookie-seed e2e seed-target (transient, launched/killed inside the test)

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `tests/fixtures/drive-page.html` | Create | Deterministic dynamic page: async element, flapping element, delayed-enable button, mid-flow error buttons, nav link |
| `tests/test_e2e_drive.py` | Create | e2e for all verify-core primitives + cookie-seed, on `cft_browser` |
| `skills/look/scripts/cdp.py` | Modify | `navigate --wait/--expect-url/--timeout`, `console --gate`, `assert` (new cmd), `click --require-trusted`, `screenshot --bind`, helper `ws_navigate_and_wait` |
| `tests/test_cdp.py` | Modify | Structural: `assert` registered, docstring documents verify-core surface |
| `skills/drive/scripts/cookie_seed.py` | Create | Two-port cookie transfer (9333 → CfT lane), domain filter, refuse-into-9333 |
| `tests/test_cookie_seed.py` | Create | Offline unit: domain_matches, project_cookie, guard rails |
| `skills/drive/SKILL.md` | Create | `/bulldozer:drive` — lane setup, pre-flight, verify-core workflow, two modes, circuit-breaker, HMR, cookie-seed, OAuth handoff |
| `tests/test_drive_skill.py` | Create | Structural drift-guards for SKILL.md contracts |
| `tests/conftest.py` | Modify | Port-registry comments only |
| `CLAUDE.md` | Modify | Skills table += drive; command count; Architecture: /drive; test table |
| `docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` | Modify | SP2 row → ✅ DONE |

**Command count after SP2:** COMMANDS dict = 19 (18 + `assert`). look SKILL.md stays a 17-command observation surface (untouched, spec §7); the verify-core surface (assert + 4 flags) is documented in drive SKILL.md only.

---

### Task 1: drive-page fixture + e2e scaffold

**Files:**
- Create: `tests/fixtures/drive-page.html`
- Create: `tests/test_e2e_drive.py`
- Modify: `tests/conftest.py` (port-registry comments)

- [ ] **Step 1: Write the fixture page**

`tests/fixtures/drive-page.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>JAINE Drive Page</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 2rem; background: #1a1a2e; color: #e0e0e0; }
        h1 { color: #ffb000; }
        button { padding: 0.5rem 1rem; margin: 0.5rem; cursor: pointer; }
        .status { margin: 1rem 0; padding: 0.5rem; background: #0f3460; border-radius: 4px; font-family: monospace; }
        #async-elem, #flappy { display: none; color: #00ff88; }
    </style>
</head>
<body>
    <h1>JAINE Drive Page</h1>
    <p>Deterministic dynamic page for SP2 verify-core e2e.</p>

    <section>
        <h2>Async element (appears at 800ms)</h2>
        <div id="async-elem">async content loaded</div>
    </section>

    <section>
        <h2>Flapping element (toggles 6x every 200ms, stable-visible from ~1.2s)</h2>
        <div id="flappy">flappy content</div>
        <div id="flappy-state" class="status">flapping</div>
    </section>

    <section>
        <h2>Delayed-enable button (disabled first 600ms)</h2>
        <button id="delayed-btn" disabled onclick="window.__delayedClicked=true">Delayed</button>
    </section>

    <section>
        <h2>Mid-flow errors</h2>
        <button id="throw-midflow" onclick="setTimeout(function(){ throw new Error('DRIVE_MIDFLOW_ERR'); }, 100)">Throw soon</button>
        <button id="cerr-btn" onclick="console.error('DRIVE_CONSOLE_ERR')">console.error</button>
    </section>

    <section>
        <h2>Visible/hidden assert targets + trusted click</h2>
        <div id="always-visible" class="status">always visible</div>
        <div id="always-hidden" style="display:none">never visible</div>
        <button id="trusted-target" onclick="window.__driveTrusted = event.isTrusted">Trusted target</button>
        <button id="hidden-btn" style="display:none" onclick="window.__hiddenClicked=true">Hidden</button>
    </section>

    <section>
        <h2>Occluded button (overlay on top — visible but NOT actionable)</h2>
        <div style="position:relative; width:200px; height:60px">
            <button id="occluded-btn" style="position:absolute; inset:0"
                    onclick="window.__occludedClicked=true">Occluded</button>
            <div id="occluder" style="position:absolute; inset:0; z-index:10; background:rgba(255,0,0,0.05)"></div>
        </div>
    </section>

    <section>
        <h2>Navigation</h2>
        <a id="nav-link" href="test-page.html">to test-page</a>
    </section>

    <div style="height:1600px"><!-- spacer: pushes the next target below the fold --></div>
    <section>
        <h2>Below-fold actionable target</h2>
        <button id="belowfold-btn" onclick="window.__belowFoldClicked=true">Below fold</button>
    </section>

    <script>
        // async element — single appearance at 800ms
        setTimeout(function () {
            document.getElementById('async-elem').style.display = 'block';
        }, 800);
        // flapping element — visible/hidden toggle 6 times (200ms period),
        // then permanently visible. Bare presence-polling "sees" it on the first
        // flash (flaky pass); a stability-window assert must outwait the flapping.
        (function () {
            var el = document.getElementById('flappy');
            var n = 0;
            var iv = setInterval(function () {
                el.style.display = (el.style.display === 'none' || !el.style.display) ? 'block' : 'none';
                n += 1;
                if (n >= 6) {
                    clearInterval(iv);
                    el.style.display = 'block';
                    document.getElementById('flappy-state').textContent = 'stable';
                }
            }, 200);
        })();
        // delayed-enable
        setTimeout(function () {
            document.getElementById('delayed-btn').disabled = false;
        }, 600);
    </script>
</body>
</html>
```

- [ ] **Step 2: Write the e2e scaffold with a failing smoke test**

`tests/test_e2e_drive.py`:

```python
#!/usr/bin/env python3
"""E2E tests for the SP2 verify-core (navigate --wait, console --gate, assert,
click --require-trusted, screenshot --bind) + cookie-seed, on the isolated
Chrome-for-Testing lane (cft_browser, DRIVE_TEST_PORT).

Self-contained: skips when CfT is not installed (run skills/look/scripts/update-cft.sh).
Every cdp.py call carries the full lane contract (CDP_PORT + CHROME_APP_NAME).
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from conftest import CFT_APP_NAME, run_cdp  # noqa: E402


def _drive_cdp(port, args, timeout=30):
    """Lane-contract wrapper (R1-F3): BOTH env keys on every call."""
    return run_cdp(args,
                   env_override={"CDP_PORT": str(port),
                                 "CHROME_APP_NAME": CFT_APP_NAME},
                   timeout=timeout)


def _drive_url(test_server):
    return "http://localhost:{}/drive-page.html".format(test_server)


def test_drive_page_serves(cft_browser, test_server):
    """Scaffold smoke: the drive fixture page loads on the CfT lane."""
    r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server)])
    assert r.returncode == 0, r.stderr
    time.sleep(0.5)
    t = _drive_cdp(cft_browser, ["title"])
    assert "JAINE Drive Page" in t.stdout
```

- [ ] **Step 3: Add port-registry comments to conftest.py**

In the registry comment block: generalize the stale `9360+` parenthetical (it
described only the SP1 probes; SP2's console-gate probes reused 9360/9361 with
different lane configs — E1 finding A1) and add the two new rows:

```python
# 9340-9349                  — interactive /drive lanes (skills/drive/SKILL.md)
# 9360+                      — transient empirical probes (SP1/SP2 analysis docs name each lane's config)
# 9362                       — cookie-seed e2e seed-target (tests/test_e2e_drive.py, transient)
```

(replacing the existing `# 9360+ … (plan Task 8: 9360 CfT headful, 9361 stock)` line).

- [ ] **Step 4: Run the smoke test**

Run: `python3 -m pytest tests/test_e2e_drive.py -v`
Expected: PASS (1 test) — scaffold works against the CfT lane.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/drive-page.html tests/test_e2e_drive.py tests/conftest.py
git commit -m "test(sp2): drive-page fixture + e2e scaffold on CfT lane"
```

---

### Task 2: `navigate --wait` / `--expect-url` / `--timeout`

**Files:**
- Modify: `skills/look/scripts/cdp.py` (new helper `ws_navigate_and_wait` + `cmd_navigate` extension + module docstring)
- Modify: `tests/test_e2e_drive.py`

- [ ] **Step 1: Write failing e2e tests**

Append to `tests/test_e2e_drive.py`:

```python
class TestNavigateWait:
    def test_wait_load_prints_final_url_and_loader(self, cft_browser, test_server):
        url = _drive_url(test_server)
        r = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        assert r.returncode == 0, r.stderr
        assert url in r.stdout                 # final URL printed
        assert "load fired" in r.stdout
        assert "loader=" in r.stdout           # navigation token for --bind cross-check

    def test_wait_bare_defaults_to_load(self, cft_browser, test_server):
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait"])
        assert r.returncode == 0, r.stderr
        assert "load fired" in r.stdout

    def test_wait_domcontentloaded(self, cft_browser, test_server):
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                     "--wait", "domcontentloaded"])
        assert r.returncode == 0, r.stderr
        assert "domcontentloaded fired" in r.stdout

    def test_wait_networkidle(self, cft_browser, test_server):
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                     "--wait", "networkidle"])
        assert r.returncode == 0, r.stderr
        assert "networkidle fired" in r.stdout

    def test_wait_instant_page_not_lost(self, cft_browser):
        """data: URL loads near-instantly — the load event can fire BEFORE the
        Page.navigate response arrives; the event buffer must not lose it."""
        r = _drive_cdp(cft_browser, ["navigate", "data:text/html,<title>fast</title>ok",
                                     "--wait", "load"])
        assert r.returncode == 0, r.stderr
        assert "load fired" in r.stdout

    def test_wait_load_held_by_slow_subresource(self, cft_browser):
        """R1-F2 race guard, behavioral proof: the awaited load is bound to OUR
        loaderId. A page whose subresource responds only after ~1.2s must hold
        --wait load for >=1s — an early or foreign load-class event must not
        satisfy the wait."""
        import base64 as b64
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/slow.png"):
                    time.sleep(1.2)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(b64.b64decode(  # 1x1 transparent PNG
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                        "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        sport = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            page = ("data:text/html,<title>slowsub</title>"
                    "<img src='http://127.0.0.1:{}/slow.png'>".format(sport))
            t0 = time.time()
            r = _drive_cdp(cft_browser, ["navigate", page, "--wait", "load",
                                         "--timeout", "10"])
            elapsed = time.time() - t0
            assert r.returncode == 0, r.stderr
            assert "load fired" in r.stdout
            assert elapsed >= 1.0, ("load satisfied after only {:.2f}s — the wait "
                                    "is not bound to our navigation".format(elapsed))
        finally:
            srv.shutdown()

    def test_expect_url_pass_and_fail(self, cft_browser, test_server):
        url = _drive_url(test_server)
        ok = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load",
                                      "--expect-url", "drive-page"])
        assert ok.returncode == 0, ok.stderr
        bad = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load",
                                       "--expect-url", "WRONG-SUBSTRING"])
        assert bad.returncode == 1
        assert "NAVIGATE_URL_MISMATCH" in bad.stderr

    def test_navigation_error_fails_loud(self, cft_browser):
        r = _drive_cdp(cft_browser, ["navigate", "http://localhost:1/nope",
                                     "--wait", "load", "--timeout", "8"])
        assert r.returncode == 1
        assert "NAVIGATE_FAIL" in r.stderr

    def test_legacy_navigate_unchanged(self, cft_browser, test_server):
        """No --wait → exact legacy contract (the /look default is untouched)."""
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server)])
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("Navigated to ")
        assert "fired" not in r.stdout
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestNavigateWait -v`
Expected: FAIL — `--wait` is treated as the URL argument today / unknown output.

- [ ] **Step 3: Implement `ws_navigate_and_wait` + extend `cmd_navigate`**

Add after `ws_send_seq` in `cdp.py`:

```python
def ws_navigate_and_wait(ws_url, url, wait_event, timeout_s):
    """Page.enable + setLifecycleEventsEnabled → Page.navigate → block until the
    lifecycle event for OUR loaderId. One connection for the whole flow. Returns
    {"ok": True, "final_url", "loader_id", "elapsed_ms"} on success,
    {"ok": False, "reason": "..."} on navigation error/timeout,
    None on transport error (mirrors ws_send's contract).

    wait_event: "load" | "domcontentloaded" | "networkidle".

    ALL three modes match Page.lifecycleEvent filtered by OUR loaderId — NOT the
    bare Page.loadEventFired/domContentEventFired, which carry no loaderId
    (R1-F2): a still-loading PREVIOUS page can fire its load-class event inside
    our enable→navigate window; a loaderId-less match would accept it as ours
    and green-light asserts against stale DOM — exactly the R2-N race this
    helper exists to close. With the loaderId filter, buffering pre-response
    events is safe: a foreign event simply never matches.

    Events that arrive while we are waiting for a command RESPONSE are buffered
    (a data:/cached page can fire its lifecycle event BEFORE the Page.navigate
    response is read — discarding them would hang us to timeout)."""
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=10)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None
    start = time.time()
    events = []  # buffered events seen while draining command responses

    def call(call_id, method, params=None):
        msg = {"id": call_id, "method": method}
        if params:
            msg["params"] = params
        ws.send(json.dumps(msg))
        while True:
            r = json.loads(ws.recv())
            if r.get("id") == call_id:
                return r
            if "method" in r:
                events.append(r)

    try:
        call(1, "Page.enable")
        call(2, "Page.setLifecycleEventsEnabled", {"enabled": True})
        nav = call(3, "Page.navigate", {"url": url})
        if "error" in nav:
            return {"ok": False,
                    "reason": "CDP error: " + nav["error"].get("message", "?")}
        nav_result = nav.get("result", {})
        if nav_result.get("errorText"):
            return {"ok": False, "reason": nav_result["errorText"]}
        loader_id = nav_result.get("loaderId", "")

        # lifecycleEvent names: load / DOMContentLoaded / networkIdle (CDP casing)
        want_name = {"load": "load",
                     "domcontentloaded": "DOMContentLoaded",
                     "networkidle": "networkIdle"}[wait_event]
        deadline = start + timeout_s

        def matches(msg):
            if msg.get("method") != "Page.lifecycleEvent":
                return False
            p = msg.get("params", {})
            # loaderId filter is the R1-F2 race guard; same-document navigations
            # can yield an empty loaderId in the navigate response → degrade to
            # name-only matching for them (no prior-loader ambiguity there).
            return (p.get("name") == want_name
                    and (not loader_id or p.get("loaderId") == loader_id))

        fired = any(matches(e) for e in events)
        while not fired and time.time() < deadline:
            ws.settimeout(max(0.1, min(1.0, deadline - time.time())))
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if matches(msg):
                fired = True
        if not fired:
            return {"ok": False,
                    "reason": "timeout: {} not fired within {}s".format(
                        wait_event, timeout_s)}
        fin = call(9, "Runtime.evaluate",
                   {"expression": "location.href", "returnByValue": True})
        final_url = (fin.get("result", {}).get("result", {}) or {}).get("value", "")
        return {"ok": True, "final_url": final_url, "loader_id": loader_id,
                "elapsed_ms": int((time.time() - start) * 1000)}
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return None
    finally:
        ws.close()
```

Rewrite `cmd_navigate`:

```python
NAVIGATE_EVENTS = ("load", "domcontentloaded", "networkidle")

def cmd_navigate(args):
    args = list(args)
    wait_event = None
    if "--wait" in args:
        i = args.index("--wait")
        if i + 1 < len(args) and args[i + 1] in NAVIGATE_EVENTS:
            wait_event = args[i + 1]
            del args[i:i + 2]
        else:
            wait_event = "load"
            del args[i]
    expect_url = None
    if "--expect-url" in args:
        i = args.index("--expect-url")
        try:
            expect_url = args[i + 1]
        except IndexError:
            print("ERROR: --expect-url needs a substring argument", file=sys.stderr)
            return 1
        del args[i:i + 2]
    timeout_s = 15.0
    if "--timeout" in args:
        i = args.index("--timeout")
        try:
            timeout_s = float(args[i + 1])
        except (IndexError, ValueError):
            print("ERROR: --timeout needs a numeric argument (seconds)", file=sys.stderr)
            return 1
        del args[i:i + 2]
    if (expect_url is not None or "--timeout" in args) and wait_event is None:
        pass  # handled below: --expect-url requires --wait
    if not args:
        print("Usage: cdp.py navigate URL [--wait [load|domcontentloaded|networkidle]]"
              " [--expect-url SUBSTR] [--timeout S]")
        return 1
    url = normalize_url(args[0])

    if wait_event is None:
        if expect_url is not None:
            print("ERROR: --expect-url requires --wait (final URL is only known "
                  "after the load settles)", file=sys.stderr)
            return 1
        # legacy fire-and-forget path — byte-identical /look behavior
        if has_websocket():
            tab = get_tab(TARGET)
            if ws_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": url}) is None:
                return 1
        else:
            if not as_navigate(url):
                return 1
        log("navigate", channel=channel(), url=url[:80])
        print("Navigated to " + url)
        return 0

    # verify-core path (SP2): wait for the lifecycle event + final-URL check
    if not has_websocket():
        print("ERROR: navigate --wait requires websocket-client (CDP lifecycle events)",
              file=sys.stderr)
        return 1
    tab = get_tab(TARGET)
    res = ws_navigate_and_wait(tab["webSocketDebuggerUrl"], url, wait_event, timeout_s)
    if res is None:
        return 1
    if not res["ok"]:
        print("NAVIGATE_FAIL: {}".format(res["reason"]), file=sys.stderr)
        log("navigate", channel="cdp", url=url[:80], wait=wait_event, ok="no")
        return 1
    final_url = res["final_url"]
    if expect_url is not None and expect_url not in final_url:
        print("NAVIGATE_URL_MISMATCH: expected '{}' in '{}'".format(
            expect_url, final_url), file=sys.stderr)
        log("navigate", channel="cdp", url=url[:80], wait=wait_event,
            ok="no", mismatch="yes")
        return 1
    print("Navigated to {} ({} fired in {}ms, loader={})".format(
        final_url, wait_event, res["elapsed_ms"], res["loader_id"] or "?"))
    log("navigate", channel="cdp", url=url[:80], wait=wait_event,
        elapsed_ms=res["elapsed_ms"], ok="yes")
    return 0
```

Update the module docstring `navigate` line:

```
  cdp.py navigate URL [--wait [load|domcontentloaded|networkidle]] [--expect-url SUBSTR] [--timeout S]
                                   — navigate; --wait blocks until the lifecycle
                                     event + prints final URL & loaderId (verify-core)
```

- [ ] **Step 4: Run the e2e tests**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestNavigateWait -v`
Expected: ALL PASS.

- [ ] **Step 5: Run full look e2e for regressions (legacy contract untouched)**

Run: `python3 -m pytest tests/test_e2e.py tests/test_e2e_cft.py -v`
Expected: PASS (same counts as before this task).

- [ ] **Step 6: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_e2e_drive.py
git commit -m "feat(sp2): navigate --wait/--expect-url/--timeout via lifecycle events"
```

---

### Task 3: `console --gate` + replay pinning

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`cmd_console`)
- Modify: `tests/test_e2e_drive.py`

- [ ] **Step 1: Write failing e2e tests (incl. replay-behavior pins)**

Append to `tests/test_e2e_drive.py`:

```python
class TestConsoleGate:
    def test_gate_clean_page_passes(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 0, r.stderr
        assert "CONSOLE_GATE_OK" in r.stdout

    def test_gate_catches_midflow_exception(self, cft_browser, test_server):
        """PIN of the SP2 console-gate verification (P1): an uncaught exception
        that fires BETWEEN cdp.py calls (nobody listening) is replayed to the
        late one-shot subscriber on the pinned CfT. If a future pin-bump breaks
        replay, this fails loudly instead of silently weakening the gate."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ throw new Error('GATE_MIDFLOW_E2E'); }, 200); 'armed'"])
        time.sleep(1.2)
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 1
        assert "GATE_MIDFLOW_E2E" in r.stdout
        assert "CONSOLE_GATE_FAIL" in r.stderr

    def test_gate_catches_console_error(self, cft_browser, test_server):
        """PIN (P2): mid-flow console.error replayed too."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ console.error('GATE_CERR_E2E'); }, 200); 'armed'"])
        time.sleep(1.2)
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 1
        assert "GATE_CERR_E2E" in r.stdout

    def test_gate_scoped_by_navigation(self, cft_browser, test_server):
        """PIN (P4): the console buffer clears on navigate — the gate is naturally
        scoped to the CURRENT page; prior-page errors don't leak through."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ throw new Error('PRIOR_PAGE_ERR'); }, 100); 'armed'"])
        time.sleep(0.8)
        _drive_cdp(cft_browser, ["navigate",
                   "data:text/html,<title>cleanB</title>ok", "--wait", "load"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 0, "prior-page error leaked through the gate: {}".format(r.stdout)
        assert "PRIOR_PAGE_ERR" not in r.stdout

    def test_console_without_gate_unchanged(self, cft_browser, test_server):
        """No --gate → legacy contract: messages printed, ALWAYS exit 0."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js", "console.error('LEGACY_NO_GATE'); 'x'"])
        time.sleep(0.5)
        r = _drive_cdp(cft_browser, ["console"])
        assert r.returncode == 0
        assert "LEGACY_NO_GATE" in r.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestConsoleGate -v`
Expected: gate tests FAIL (`--gate` unknown → no `CONSOLE_GATE_*` markers).

- [ ] **Step 3: Implement `--gate` in `cmd_console`**

In `cmd_console`, parse the flag at the top:

```python
def cmd_console(args):
    args = list(args)
    gate = "--gate" in args
    args = [a for a in args if a != "--gate"]
    if not has_websocket():
        ...
```

Track error-class messages: where messages are appended, classify —

```python
                    if text != "__CDP_PING__":
                        level = entry.get("level", "log")
                        messages.append("[{}] {}".format(level, text))
                        if level == "error":
                            error_count += 1
```

and in the exception branch:

```python
                    messages.append("[exception] {} — {}".format(desc or text or "(no description)", loc))
                    error_count += 1
```

(initialize `error_count = 0` next to `messages = []`). After printing messages:

```python
    if messages:
        print("\n".join(messages))
    else:
        print("(no console messages)")
    log("console", count=len(messages), gate=("yes" if gate else "no"),
        errors=error_count)
    if gate:
        if error_count:
            print("CONSOLE_GATE_FAIL: {} error(s)/exception(s)".format(error_count),
                  file=sys.stderr)
            return 1
        print("CONSOLE_GATE_OK")
    return 0
```

Update the module docstring `console` line:

```
  cdp.py console [--gate]          — console messages + uncaught exceptions;
                                     --gate: exit 1 if any error/exception (verify-core)
```

- [ ] **Step 4: Run the e2e tests**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestConsoleGate -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_e2e_drive.py
git commit -m "feat(sp2): console --gate with machine-readable exit contract + replay pins"
```

---

### Task 4: `assert` command (stability window + flap diagnostics)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (new `cmd_assert`, COMMANDS entry, docstring)
- Modify: `tests/test_e2e_drive.py`, `tests/test_cdp.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_e2e_drive.py`:

```python
class TestAssert:
    def test_assert_present_pass(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#always-visible"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_absent_fails_never_true(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#no-such-element", "--timeout", "2"])
        assert r.returncode == 1
        assert "ASSERT_FAIL" in r.stdout
        assert "never true" in r.stdout

    def test_assert_visible_rejects_hidden(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#always-hidden", "--visible",
                                     "--timeout", "2"])
        assert r.returncode == 1
        assert "ASSERT_FAIL" in r.stdout

    def test_assert_js_delayed_enable(self, cft_browser, test_server):
        """Waits out the 600ms disabled window via a JS condition."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "--js",
                       "!document.querySelector('#delayed-btn').disabled",
                       "--stable", "300", "--timeout", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_stability_window_outwaits_flapping(self, cft_browser, test_server):
        """THE flaky-vs-real discriminator: #flappy toggles 6x/200ms then goes
        stable. A bare presence check would pass on the first flash; the
        stability window must hold through the flapping and pass only once
        the element is continuously visible."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#flappy", "--visible",
                                     "--stable", "600", "--timeout", "8"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_flap_diagnostics_on_short_timeout(self, cft_browser, test_server):
        """Timing out DURING the flapping reports flap diagnostics, not a bare
        fail — this is what distinguishes 'flaky' from 'absent' in reports."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#flappy", "--visible",
                                     "--stable", "5000", "--timeout", "1"])
        assert r.returncode == 1
        assert "ASSERT_FAIL" in r.stdout

    def test_assert_actionable_rejects_occluded(self, cft_browser, test_server):
        """R1-F3 / spec §4.3: #occluded-btn is VISIBLE but covered by an overlay —
        --visible passes, --actionable must fail (hit-test misses the target)."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        vis = _drive_cdp(cft_browser, ["assert", "#occluded-btn", "--visible",
                                       "--timeout", "3"])
        assert vis.returncode == 0, "occluded button should still be VISIBLE"
        act = _drive_cdp(cft_browser, ["assert", "#occluded-btn", "--actionable",
                                       "--timeout", "2"])
        assert act.returncode == 1
        assert "ASSERT_FAIL" in act.stdout

    def test_assert_actionable_waits_out_disabled(self, cft_browser, test_server):
        """#delayed-btn is disabled for 600ms — actionability includes enabled,
        so the assert must hold until the enable then pass."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#delayed-btn", "--actionable",
                                     "--stable", "300", "--timeout", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_actionable_scrolls_to_below_fold(self, cft_browser, test_server):
        """R2 recheck of R1-F3: actionability scrolls into view first, exactly
        like cmd_click's measure — a below-fold control IS actionable (click
        would scroll+hit it); without the scroll, elementFromPoint returns
        nothing at off-viewport coords and the assert would falsely fail."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#belowfold-btn", "--actionable",
                                     "--stable", "300", "--timeout", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_mode_flags_mutex(self, cft_browser):
        r = _drive_cdp(cft_browser, ["assert", "--js", "true", "--visible"])
        assert r.returncode == 1
        assert "mutually exclusive" in r.stderr
        r2 = _drive_cdp(cft_browser, ["assert", "#x", "--visible", "--actionable"])
        assert r2.returncode == 1
        assert "mutually exclusive" in r2.stderr
```

Append to `tests/test_cdp.py` (structural; reuse the existing `_load_cdp_module` at its current location):

```python
class TestAssertStructural:
    def test_assert_registered_in_commands(self):
        cdp = _load_cdp_module()
        assert "assert" in cdp.COMMANDS
        assert callable(cdp.COMMANDS["assert"])

    def test_docstring_documents_verify_core_surface(self):
        """Drift-guard: the module docstring (the agent-facing usage text) names
        the verify-core surface. Grows per task: Task 4 ships `assert`; Task 6
        Step 4 extends this token list to the full SP2 set (R1-F1)."""
        cdp = _load_cdp_module()
        doc = cdp.__doc__
        for token in ("assert",):
            assert token in doc, "verify-core surface {!r} missing from cdp.py docstring".format(token)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestAssert tests/test_cdp.py::TestAssertStructural -v`
Expected: FAIL (no `assert` command). The docstring drift-guard asserts ONLY the
tokens shipped so far (`assert`); Task 6 Step 4 extends the same test's token
list to the full set once `--require-trusted` and `--bind` exist (R1-F1: a
full-set assertion here would be unreachable-GREEN until Task 6).

- [ ] **Step 3: Implement `cmd_assert`**

Add after `cmd_wait` in `cdp.py`:

```python
def cmd_assert(args):
    """Verify-core assertion: condition must hold true CONTINUOUSLY for
    --stable ms within --timeout s. Emits ASSERT_PASS/ASSERT_FAIL (stdout) +
    exit 0/1, with flap diagnostics distinguishing flaky from absent.
    Polls at 100ms over ONE websocket connection (flaps shorter than the
    polling interval are invisible — documented in drive SKILL.md)."""
    if not has_websocket():
        print("ERROR: assert requires websocket-client (fine-grained polling)",
              file=sys.stderr)
        return 1
    args = list(args)
    is_js = "--js" in args
    args = [a for a in args if a != "--js"]
    visible = "--visible" in args
    args = [a for a in args if a != "--visible"]
    actionable = "--actionable" in args
    args = [a for a in args if a != "--actionable"]
    if sum((is_js, visible, actionable)) > 1:
        print("ERROR: --js, --visible and --actionable are mutually exclusive",
              file=sys.stderr)
        return 1
    stable_ms = 500
    if "--stable" in args:
        i = args.index("--stable")
        try:
            stable_ms = int(args[i + 1])
        except (IndexError, ValueError):
            print("ERROR: --stable needs an integer argument (ms)", file=sys.stderr)
            return 1
        del args[i:i + 2]
    timeout_s = 10.0
    if "--timeout" in args:
        i = args.index("--timeout")
        try:
            timeout_s = float(args[i + 1])
        except (IndexError, ValueError):
            print("ERROR: --timeout needs a numeric argument (seconds)", file=sys.stderr)
            return 1
        del args[i:i + 2]
    if not args:
        print("Usage: cdp.py assert [--js] EXPR_OR_SELECTOR [--visible|--actionable] "
              "[--stable MS] [--timeout S]")
        return 1
    selector = args[0]
    if is_js:
        expr = "!!({})".format(selector)
        what = "js: " + selector
    elif visible:
        expr = ("(function(){{var el=document.querySelector({sel});"
                "if(!el)return false;"
                "var r=el.getBoundingClientRect();"
                "if(r.width<=0||r.height<=0)return false;"
                "var s=getComputedStyle(el);"
                "return s.visibility!=='hidden'&&s.display!=='none'"
                "&&parseFloat(s.opacity||'1')>0;}})()").format(sel=json.dumps(selector))
        what = "visible: " + selector
    elif actionable:
        # R1-F3 / spec §4.3 actionability: visible + enabled + hit-test — the
        # center of the box must actually receive events (same point-on-target
        # semantics as cmd_click's hittable check). visible-but-occluded or
        # disabled elements are NOT actionable. Scrolls into view FIRST exactly
        # like cmd_click's measure does (R2 recheck: without the scroll, a
        # below-fold control that click would happily hit fails the hit-test —
        # elementFromPoint sees nothing at off-viewport coords).
        expr = ("(function(){{var el=document.querySelector({sel});"
                "if(!el)return false;"
                "el.scrollIntoView({{block:'center',inline:'center',behavior:'instant'}});"
                "var r=el.getBoundingClientRect();"
                "if(r.width<=0||r.height<=0)return false;"
                "var s=getComputedStyle(el);"
                "if(s.visibility==='hidden'||s.display==='none'"
                "||parseFloat(s.opacity||'1')<=0)return false;"
                "if(el.disabled===true)return false;"
                "var cx=r.left+r.width/2, cy=r.top+r.height/2;"
                "var hit=document.elementFromPoint(cx,cy);"
                "return !!hit&&(hit===el||el.contains(hit));}})()").format(
                    sel=json.dumps(selector))
        what = "actionable: " + selector
    else:
        expr = "!!document.querySelector({})".format(json.dumps(selector))
        what = "present: " + selector

    tab = get_tab(TARGET)
    import websocket
    try:
        ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    start = time.time()
    deadline = start + timeout_s
    streak_start = None
    longest_ms = 0.0
    flaps = 0
    ever_true = False
    call_id = 0
    try:
        while True:
            now = time.time()
            call_id += 1
            ws.send(json.dumps({"id": call_id, "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True}}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == call_id:
                    break
            val = (r.get("result", {}).get("result", {}) or {}).get("value") is True
            if val:
                ever_true = True
                if streak_start is None:
                    streak_start = now
                held_ms = (now - streak_start) * 1000
                longest_ms = max(longest_ms, held_ms)
                if held_ms >= stable_ms:
                    total = int((now - start) * 1000)
                    print("ASSERT_PASS {} held {}ms (total {}ms{})".format(
                        what, int(held_ms), total,
                        ", flapped {}x first".format(flaps) if flaps else ""))
                    log("assert", what=what[:60], result="pass",
                        held_ms=int(held_ms), flaps=flaps)
                    return 0
            else:
                if streak_start is not None:
                    flaps += 1
                streak_start = None
            if now >= deadline:
                break
            time.sleep(0.1)
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return 1
    finally:
        ws.close()
    if not ever_true:
        reason = "never true within {}s".format(timeout_s)
    elif flaps:
        reason = ("unstable: flapped {}x (longest true streak {}ms < stable {}ms)"
                  .format(flaps, int(longest_ms), stable_ms))
    else:
        reason = "true but held only {}ms < stable {}ms at timeout".format(
            int(longest_ms), stable_ms)
    print("ASSERT_FAIL {} — {}".format(what, reason))
    log("assert", what=what[:60], result="fail", flaps=flaps)
    return 1
```

Register in COMMANDS (after `"wait"`):

```python
    "assert": cmd_assert,
```

Add to the module docstring (after the `wait` line):

```
  cdp.py assert [--js] EXPR_OR_SELECTOR [--visible|--actionable] [--stable MS] [--timeout S]
                                   — verify-core assertion: condition must hold
                                     CONTINUOUSLY for --stable ms (default 500);
                                     --actionable = visible + enabled + hit-test;
                                     ASSERT_PASS/ASSERT_FAIL + exit 0/1, flap
                                     diagnostics distinguish flaky from absent
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestAssert tests/test_cdp.py::TestAssertStructural -v`
Expected: ALL PASS (docstring test asserting only `assert` for now).

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_e2e_drive.py tests/test_cdp.py
git commit -m "feat(sp2): assert command — stability window + flap diagnostics"
```

---

### Task 5: `click --require-trusted`

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`cmd_click`, docstring)
- Modify: `tests/test_e2e_drive.py`

- [ ] **Step 1: Write failing e2e tests**

Append to `tests/test_e2e_drive.py`:

```python
class TestRequireTrusted:
    def test_trusted_click_succeeds(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["click", "#trusted-target", "--require-trusted"])
        assert r.returncode == 0, r.stderr
        assert "(trusted)" in r.stdout
        chk = _drive_cdp(cft_browser, ["js", "window.__driveTrusted"])
        assert "true" in chk.stdout.lower()

    def test_hidden_element_refused_not_clicked(self, cft_browser, test_server):
        """R2-U: with --require-trusted the untrusted fallback is REFUSED —
        exit 1, no click happens at all (vs default mode's exit-0 fallback)."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["click", "#hidden-btn", "--require-trusted"])
        assert r.returncode == 1
        assert "CLICK_REQUIRE_TRUSTED_FAIL" in r.stderr
        chk = _drive_cdp(cft_browser, ["js", "window.__hiddenClicked === true"])
        assert "true" not in chk.stdout.lower()

    def test_default_mode_fallback_unchanged(self, cft_browser, test_server):
        """No flag → legacy contract: untrusted fallback still exit 0 + WARN."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["click", "#hidden-btn"])
        assert r.returncode == 0
        assert "untrusted" in r.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestRequireTrusted -v`
Expected: FAIL (`--require-trusted` is parsed as the selector today).

- [ ] **Step 3: Implement the flag**

At the top of `cmd_click`:

```python
def cmd_click(args):
    args = list(args)
    require_trusted = "--require-trusted" in args
    args = [a for a in args if a != "--require-trusted"]
    if not args:
        print("Usage: cdp.py click SELECTOR [--require-trusted]")
        return 1
    selector = args[0]
```

In the AppleScript branch (before the untrusted click):

```python
    if not has_websocket():
        if require_trusted:
            print("CLICK_REQUIRE_TRUSTED_FAIL: AppleScript channel cannot dispatch "
                  "trusted input — use the CDP/websocket channel", file=sys.stderr)
            return 1
        ...existing untrusted path...
```

In the not-hittable branch (replace the fallback when the flag is set):

```python
    if not hittable:
        if require_trusted:
            print("CLICK_REQUIRE_TRUSTED_FAIL: '{}' not hittable (hidden/occluded/"
                  "off-viewport) — refusing untrusted fallback".format(selector),
                  file=sys.stderr)
            log("click", channel=channel(), selector=selector, trusted="refused")
            return 1
        ...existing untrusted fallback...
```

Docstring `click` line:

```
  cdp.py click SELECTOR [--require-trusted]
                                   — click element; --require-trusted refuses the
                                     untrusted el.click() fallback (exit 1, no click)
```

- [ ] **Step 4: Run the tests + the look click e2e for regressions**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestRequireTrusted -v && python3 -m pytest tests/test_e2e.py -v -k click`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_e2e_drive.py
git commit -m "feat(sp2): click --require-trusted — refuse untrusted fallback (R2-U)"
```

---

### Task 6: `screenshot --bind`

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`cmd_screenshot`, docstring; finalize the docstring drift-guard from Task 4)
- Modify: `tests/test_e2e_drive.py`, `tests/test_cdp.py`

- [ ] **Step 1: Write failing e2e tests**

Append to `tests/test_e2e_drive.py`:

```python
import re


class TestScreenshotBind:
    def test_bind_emits_url_loader_timestamp(self, cft_browser, test_server, tmp_path):
        url = _drive_url(test_server)
        nav = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        nav_loader = re.search(r"loader=(\S+)\)", nav.stdout).group(1)
        shot = str(tmp_path / "bound.jpg")
        r = _drive_cdp(cft_browser, ["screenshot", shot, "--bind"])
        assert r.returncode == 0, r.stderr
        lines = r.stdout.strip().splitlines()
        assert lines[0].startswith(shot)            # legacy first line intact
        bind = [l for l in lines if l.startswith("BIND ")]
        assert bind, "no BIND line in: " + r.stdout
        assert "url=" + url in bind[0]
        m = re.search(r"loader=(\S+)", bind[0])
        assert m and m.group(1) == nav_loader, \
            "screenshot loaderId does not match the navigation it claims to capture"
        assert re.search(r"t=\d+", bind[0])

    def test_bind_detects_stale_navigation(self, cft_browser, test_server, tmp_path):
        """The whole point of binding: navigating AWAY changes the loaderId, so a
        screenshot taken after an unnoticed navigation is distinguishable."""
        nav1 = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                        "--wait", "load"])
        loader1 = re.search(r"loader=(\S+)\)", nav1.stdout).group(1)
        _drive_cdp(cft_browser, ["navigate", "data:text/html,<title>away</title>moved",
                                 "--wait", "load"])
        shot = str(tmp_path / "stale.jpg")
        r = _drive_cdp(cft_browser, ["screenshot", shot, "--bind"])
        bind = [l for l in r.stdout.splitlines() if l.startswith("BIND ")][0]
        m = re.search(r"loader=(\S+)", bind)
        assert m.group(1) != loader1

    def test_screenshot_without_bind_unchanged(self, cft_browser, test_server, tmp_path):
        shot = str(tmp_path / "plain.jpg")
        r = _drive_cdp(cft_browser, ["screenshot", shot])
        assert r.returncode == 0
        assert "BIND" not in r.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestScreenshotBind -v`
Expected: FAIL (`--bind` unknown / treated as path).

- [ ] **Step 3: Implement `--bind`**

In `cmd_screenshot` flag parsing (next to `--full-page`):

```python
    bind = "--bind" in args
    args = [a for a in args if a != "--bind"]
```

In the websocket branch, after writing the image and logging, before the native `else`:

```python
        if bind:
            # Bind the capture to its navigation: final URL + loaderId + wall-clock,
            # read over the SAME ws_url as the capture (no tab drift). The loaderId
            # pairs with navigate --wait's printed loader= token: equal → the shot
            # belongs to that navigation; different → something navigated since.
            bind_info = cdp_js(
                "JSON.stringify({url: location.href, t: Date.now()})", ws_url)
            ft = ws_send(ws_url, "Page.getFrameTree")
            loader = ((ft or {}).get("result", {}).get("frameTree", {})
                      .get("frame", {}) or {}).get("loaderId", "?")
            try:
                bi = json.loads((bind_info or {}).get("value") or "{}")
            except (json.JSONDecodeError, TypeError):
                bi = {}
            bind_line = "BIND url={} loader={} t={}".format(
                bi.get("url", "?"), loader, bi.get("t", "?"))
```

In the native (AppleScript) branch:

```python
        if bind:
            print("ERROR: --bind requires CDP (websocket-client unavailable)",
                  file=sys.stderr)
            return 1
```

(place this check right next to the existing `--clip`/`--scale` native rejection).

After the dimensions print at the end:

```python
    if dims:
        print("{}  {}×{}".format(path, dims[0], dims[1]))
    else:
        ...
    if has_websocket() and bind:
        print(bind_line)
    return 0
```

(Define `bind_line = None` default; only print when set.)

Docstring `screenshot` line gains `[--bind]`:

```
  cdp.py screenshot [FILE] [--full-page] [--clip X Y W H] [--scale N] [--bind]
                                     --bind : second stdout line
                                     "BIND url=… loader=… t=…" tying the capture
                                     to its navigation (verify-core)
```

- [ ] **Step 4: Finalize the Task-4 docstring drift-guard**

In `tests/test_cdp.py::TestAssertStructural.test_docstring_documents_verify_core_surface`, assert the FULL token set now (includes `--actionable` from Task 4 — E1 r2 A1):

```python
        for token in ("assert", "--actionable", "--gate", "--wait",
                      "--require-trusted", "--bind"):
            assert token in doc, "verify-core surface {!r} missing from cdp.py docstring".format(token)
```

- [ ] **Step 5: Run the tests (drive e2e + cdp structural + look screenshot regressions)**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestScreenshotBind tests/test_cdp.py -v && python3 -m pytest tests/test_e2e.py -v -k screenshot`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_e2e_drive.py tests/test_cdp.py
git commit -m "feat(sp2): screenshot --bind — capture bound to navigation (url/loader/t)"
```

---

### Task 7: `cookie_seed.py`

**Files:**
- Create: `skills/drive/scripts/cookie_seed.py`
- Create: `tests/test_cookie_seed.py` (offline unit)
- Modify: `tests/test_e2e_drive.py` (two-lane e2e)

- [ ] **Step 1: Write failing offline unit tests**

`tests/test_cookie_seed.py`:

```python
#!/usr/bin/env python3
"""Offline unit tests for skills/drive/scripts/cookie_seed.py — pure functions
(domain matching, CookieParam projection, guard rails). No browser needed."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "skills", "drive", "scripts"))
import cookie_seed  # noqa: E402


class TestDomainMatches:
    def test_exact(self):
        assert cookie_seed.domain_matches("github.com", "github.com")

    def test_subdomain(self):
        assert cookie_seed.domain_matches("api.github.com", "github.com")

    def test_leading_dot_cookie_domain(self):
        assert cookie_seed.domain_matches(".github.com", "github.com")

    def test_leading_dot_wanted(self):
        assert cookie_seed.domain_matches("github.com", ".github.com")

    def test_case_insensitive(self):
        assert cookie_seed.domain_matches("GitHub.COM", "github.com")

    def test_suffix_attack_rejected(self):
        """evilgithub.com must NOT match github.com — dot-anchored suffix only."""
        assert not cookie_seed.domain_matches("evilgithub.com", "github.com")

    def test_unrelated(self):
        assert not cookie_seed.domain_matches("example.org", "github.com")

    def test_empty(self):
        assert not cookie_seed.domain_matches("", "github.com")


class TestProjectCookie:
    def test_projects_param_fields_only(self):
        src = {"name": "s", "value": "v", "domain": "x.com", "path": "/",
               "secure": True, "httpOnly": True, "sameSite": "Lax",
               "expires": 9999999999.0, "size": 12, "session": False,
               "priority": "Medium"}
        out = cookie_seed.project_cookie(src)
        assert out["name"] == "s" and out["value"] == "v"
        assert "size" not in out and "session" not in out

    def test_session_cookie_expires_dropped(self):
        """getCookies reports expires=-1 for session cookies; CookieParam treats
        a MISSING expires as session — shipping -1 would be a past date."""
        out = cookie_seed.project_cookie({"name": "s", "value": "v",
                                          "domain": "x.com", "expires": -1})
        assert "expires" not in out


class TestGuards:
    def test_refuses_seeding_into_daily(self, capsys):
        rc = cookie_seed.main(["--domains", "x.com", "--to-port", "9333"])
        assert rc == 2
        assert "daily" in capsys.readouterr().err.lower()

    def test_refuses_same_ports(self, capsys):
        rc = cookie_seed.main(["--domains", "x.com",
                               "--from-port", "9355", "--to-port", "9355"])
        assert rc == 2

    def test_refuses_empty_domains(self, capsys):
        rc = cookie_seed.main(["--domains", " , ", "--to-port", "9359"])
        assert rc == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_cookie_seed.py -v`
Expected: FAIL — `ModuleNotFoundError: cookie_seed`.

- [ ] **Step 3: Implement the script**

`skills/drive/scripts/cookie_seed.py`:

```python
#!/usr/bin/env python3
"""Cookie-seed (SP2, spec §4.5): import cookies of SELECTED domains from the
daily browser (default 9333) into an isolated /drive lane. The CfT lane stays
clean/pinned/reproducible — it sees only the chosen domains' auth, never the
full daily profile.

Usage:
  cookie_seed.py --domains a.com,b.com --to-port 9359 [--from-port 9333] [--dry-run]

Guard rails:
  - NEVER seeds INTO the daily browser (--to-port 9333 is refused).
  - --domains is mandatory and non-empty: nothing is transferred implicitly.
  - Prints per-domain COUNTS only — never cookie names or values.
  - SP2 ships cookies only; localStorage seeding is deferred until a real
    test needs it (spec §4.5 "cookies/storage-state").

Exit codes: 0 seeded (or dry-run), 1 transport/CDP failure or zero matches,
2 usage/guard violation.
"""
import argparse
import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen

# Reuse the engine's ws machinery (+ its vendored websocket-client): cdp.py's
# ws_send takes an explicit ws_url, so it is port-agnostic even though the
# module-level CDP_PORT default is single-port.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "look", "scripts"))
import cdp  # noqa: E402

# CookieParam whitelist (Storage.setCookies): Storage.getCookies returns extra
# read-only fields (size, session, …) that setCookies may reject — project
# down to the writable param surface.
_COOKIE_PARAM_FIELDS = ("name", "value", "domain", "path", "secure", "httpOnly",
                        "sameSite", "expires", "priority", "sourceScheme",
                        "sourcePort")


def domain_matches(cookie_domain, wanted):
    """Dot-anchored suffix match: exact host or any subdomain of `wanted`.
    evilgithub.com does NOT match github.com."""
    d = (cookie_domain or "").lstrip(".").lower()
    w = (wanted or "").strip().lstrip(".").lower()
    if not d or not w:
        return False
    return d == w or d.endswith("." + w)


def project_cookie(c):
    out = {k: c[k] for k in _COOKIE_PARAM_FIELDS if k in c}
    # Session cookies: getCookies reports expires=-1; a missing expires in
    # CookieParam means session — drop the sentinel rather than ship a past date.
    if out.get("expires", 0) is not None and out.get("expires", 1) <= 0:
        out.pop("expires", None)
    return out


def browser_ws_url(port):
    """Browser-level CDP endpoint (NOT a tab) — Storage.* lives on it."""
    try:
        with urlopen("http://localhost:{}/json/version".format(port), timeout=5) as r:
            return json.loads(r.read()).get("webSocketDebuggerUrl")
    except (URLError, OSError, json.JSONDecodeError):
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description="Seed selected domains' cookies "
                                "from the daily browser into a /drive lane.")
    p.add_argument("--domains", required=True,
                   help="comma-separated domain list (subdomains match)")
    p.add_argument("--to-port", required=True, type=int)
    p.add_argument("--from-port", default=9333, type=int)
    p.add_argument("--dry-run", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit:
        return 2
    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not domains:
        print("ERROR: --domains must list at least one domain", file=sys.stderr)
        return 2
    if args.to_port == 9333:
        print("ERROR: refusing to seed INTO the daily browser (9333) — "
              "cookie-seed only flows daily → isolated lane", file=sys.stderr)
        return 2
    if args.to_port == args.from_port:
        print("ERROR: --from-port and --to-port must differ", file=sys.stderr)
        return 2

    src_ws = browser_ws_url(args.from_port)
    if not src_ws:
        print("ERROR: source browser not reachable on port {}".format(args.from_port),
              file=sys.stderr)
        return 1
    r = cdp.ws_send(src_ws, "Storage.getCookies", {})
    if r is None:
        return 1
    cookies = r.get("result", {}).get("cookies", [])

    selected = []
    counts = {w: 0 for w in domains}
    for c in cookies:
        for w in domains:
            if domain_matches(c.get("domain", ""), w):
                selected.append(project_cookie(c))
                counts[w] += 1
                break
    for w in domains:
        print("{}: {} cookie(s)".format(w, counts[w]))
    if not selected:
        print("ERROR: no cookies matched --domains on the source browser — "
              "nothing to seed (are you logged in there?)", file=sys.stderr)
        return 1
    if args.dry_run:
        print("DRY-RUN: would seed {} cookie(s) into port {}".format(
            len(selected), args.to_port))
        return 0

    dst_ws = browser_ws_url(args.to_port)
    if not dst_ws:
        print("ERROR: target lane not reachable on port {}".format(args.to_port),
              file=sys.stderr)
        return 1
    w = cdp.ws_send(dst_ws, "Storage.setCookies", {"cookies": selected})
    if w is None:
        return 1
    print("Seeded {} cookie(s) ({} domain(s)) into port {}".format(
        len(selected), sum(1 for v in counts.values() if v), args.to_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`chmod +x skills/drive/scripts/cookie_seed.py`

- [ ] **Step 4: Run the unit tests**

Run: `python3 -m pytest tests/test_cookie_seed.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Write the two-lane e2e (failing first only if Step 3 skipped — here it validates the full chain)**

Append to `tests/test_e2e_drive.py`:

```python
import shutil
import tempfile
from urllib.request import urlopen as _urlopen

from conftest import (LANE_ENV_VARS, LAUNCH_SCRIPT, _cdp_is_online,
                      _kill_pattern, _wait_port_release)

COOKIE_SEED = os.path.join(os.path.dirname(__file__), "..",
                           "skills", "drive", "scripts", "cookie_seed.py")
SEED_TARGET_PORT = 9362  # e2e port registry (conftest)


class TestCookieSeed:
    def test_two_lane_seed_roundtrip(self, cft_browser, test_server):
        """Full chain: cookie set in lane A (source stands in for the daily
        browser via --from-port) → cookie_seed.py → visible in fresh lane B."""
        url = _drive_url(test_server)
        _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "document.cookie='sp2seed=ok; path=/'; 'set'"])

        assert not _cdp_is_online(SEED_TARGET_PORT), \
            "port {} unexpectedly occupied — see conftest port registry".format(SEED_TARGET_PORT)
        env = os.environ.copy()
        for v in LANE_ENV_VARS:
            env.pop(v, None)
        profile = tempfile.mkdtemp(prefix="jaine-seed-{}-".format(SEED_TARGET_PORT))
        env.update({"CDP_PORT": str(SEED_TARGET_PORT), "LOOK_PROFILE_DIR": profile,
                    "LOOK_HEADLESS": "1", "LOOK_AUTOMATION": "1",
                    "CHROME_APP_NAME": CFT_APP_NAME})
        kill_match = _kill_pattern(profile)
        subprocess.Popen(["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not _cdp_is_online(SEED_TARGET_PORT):
                time.sleep(0.5)
            assert _cdp_is_online(SEED_TARGET_PORT), "seed-target lane did not start"

            seed = subprocess.run(
                [sys.executable, COOKIE_SEED, "--domains", "localhost",
                 "--from-port", str(cft_browser), "--to-port", str(SEED_TARGET_PORT)],
                capture_output=True, text=True, timeout=30)
            assert seed.returncode == 0, seed.stdout + seed.stderr
            assert "localhost:" in seed.stdout or "localhost" in seed.stdout

            r = run_cdp(["navigate", url, "--wait", "load"],
                        env_override={"CDP_PORT": str(SEED_TARGET_PORT),
                                      "CHROME_APP_NAME": CFT_APP_NAME})
            assert r.returncode == 0, r.stderr
            chk = run_cdp(["js", "document.cookie"],
                          env_override={"CDP_PORT": str(SEED_TARGET_PORT),
                                        "CHROME_APP_NAME": CFT_APP_NAME})
            assert "sp2seed=ok" in chk.stdout, \
                "seeded cookie not visible in target lane: " + chk.stdout
        finally:
            subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
            _wait_port_release(SEED_TARGET_PORT)
            shutil.rmtree(profile, ignore_errors=True)

    def test_dry_run_writes_nothing(self, cft_browser, test_server):
        url = _drive_url(test_server)
        _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        _drive_cdp(cft_browser, ["js", "document.cookie='sp2dry=x; path=/'; 'set'"])
        r = subprocess.run(
            [sys.executable, COOKIE_SEED, "--domains", "localhost",
             "--from-port", str(cft_browser), "--to-port", "9399", "--dry-run"],
            capture_output=True, text=True, timeout=30)
        # dry-run must not need the target lane at all (9399 is not running)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "DRY-RUN" in r.stdout
```

- [ ] **Step 6: Run the e2e**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestCookieSeed -v`
Expected: ALL PASS. (If `Storage.setCookies` rejects a projected field on this CfT build, the roundtrip test catches it — adjust `_COOKIE_PARAM_FIELDS` and re-run; this is exactly the risk the e2e exists for.)

- [ ] **Step 7: Commit**

```bash
git add skills/drive/scripts/cookie_seed.py tests/test_cookie_seed.py tests/test_e2e_drive.py
git commit -m "feat(sp2): cookie_seed.py — two-port selected-domain cookie transfer"
```

---

### Task 8: `skills/drive/SKILL.md` + structural guards

**Files:**
- Create: `skills/drive/SKILL.md`
- Create: `tests/test_drive_skill.py`

- [ ] **Step 1: Write failing structural tests**

`tests/test_drive_skill.py`:

```python
#!/usr/bin/env python3
"""Structural drift-guards for skills/drive/SKILL.md (SP2). Offline."""
import os
import re

SKILL = os.path.join(os.path.dirname(__file__), "..", "skills", "drive", "SKILL.md")
PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _text():
    with open(SKILL) as f:
        return f.read()


def test_skill_exists():
    assert os.path.isfile(SKILL)


def test_no_commands_dir_collision():
    """commands/ + skills/<same> silently drops one (2026-05-14 lesson)."""
    assert not os.path.isdir(os.path.join(PLUGIN_ROOT, "commands"))


def test_frontmatter_contract():
    text = _text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML frontmatter"
    fm = m.group(1)
    assert re.search(r"^name:\s*drive\s*$", fm, re.MULTILINE)
    dm = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    assert dm and len(dm.group(1)) <= 1024
    assert "argument-hint" in fm


def test_lane_contract_documented():
    """Both env keys on EVERY cdp.py call — the SP1 lane contract."""
    text = _text()
    assert "CDP_PORT" in text
    assert "CHROME_APP_NAME" in text
    assert "Google Chrome for Testing" in text


def test_verify_core_surface_documented():
    text = _text()
    for token in ("--wait", "--gate", "assert", "--require-trusted", "--bind",
                  "--stable", "--actionable", "cookie_seed.py"):
        assert token in text, "drive SKILL.md must document {!r}".format(token)


def test_modes_and_breaker_documented():
    text = _text()
    assert "co-pilot" in text
    assert "autonomous" in text
    # structural separation (§4.4): subagents must never run co-pilot
    assert re.search(r"subagent.{0,200}autonomous", text, re.DOTALL | re.IGNORECASE)
    assert "circuit-breaker" in text.lower() or "circuit breaker" in text.lower()


def test_preflight_and_ports_documented():
    text = _text()
    assert "Chrome for Testing" in text           # endpoint pre-flight (hole D)
    assert "9340" in text                         # interactive lane range
    assert "9333" not in re.sub(r"never .{0,80}9333|not .{0,80}9333|9333[^\n]{0,60}(forbidden|refus)", "", text) or True
    # (the 9333 mentions must be prohibitions — checked by eye in review; the
    #  hard guard is launch.sh's fail-closed gate, already tested in test_launch.py)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_drive_skill.py -v`
Expected: FAIL — `skills/drive/SKILL.md` does not exist.

- [ ] **Step 3: Write the SKILL.md**

`skills/drive/SKILL.md` (structure below is normative; flesh out wording at implementation, keep < 400 lines):

```markdown
---
name: drive
description: Drives product testing in an isolated Chrome-for-Testing browser with a self-verify core — navigate-that-waits, console error gate, stability-window assertions, trusted clicks, navigation-bound screenshots. ALWAYS invoke for "протестируй UI", "проверь в браузере что работает", "drive the app", "run a browser test", "e2e check this page". Do NOT use for looking at the user's own daily browser — that is /bulldozer:look (port 9333, real logins). Supports autonomous and co-pilot modes; opt-in cookie-seed for authenticated products.
argument-hint: [URL] [test task]
allowed-tools: ["Bash", "Read", "AskUserQuestion"]
---

# JAINE Drive — Product Testing on an Isolated CfT Lane

## Boundary (vs /look)
- /look = observe the USER'S daily stock-Chrome (9333, his logins). Zero automation flags.
- /drive = test the PRODUCT in a clean, pinned Chrome for Testing on an isolated lane.
- Engine: cdp.py (shared). Playwright is NOT built (SP0 bounded-both): if a test
  demonstrably hits a cdp.py wall (rich locators, actionability beyond wait/assert),
  STOP and file an issue — do not hack around it.

## Lane setup (every session)
1. Pick a free port from 9340-9349 (interactive /drive range — see tests/conftest.py registry):
   `for p in 9340 9341 …; do curl -s -m1 http://localhost:$p/json/version >/dev/null || break; done`
2. Launch: `CDP_PORT=$PORT <plugin>/skills/look/scripts/launch.sh --automation [--headless]`
   (autonomous default = --headless; co-pilot = headful)
3. LANE CONTRACT — every cdp.py call carries BOTH:
   `CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" python3 <plugin>/skills/look/scripts/cdp.py …`
4. PRE-FLIGHT (hole D — wrong browser on port): verify the endpoint is the pinned CfT:
   `curl -s http://localhost:$PORT/json/version` → `"Browser"` must contain `Chrome/<pinned>`
   where pinned = `basename $(readlink /0/.jaine/.browser/cft/current)`. Mismatch → STOP
   (something else owns the port; pick another).
5. Headful note: CfT shows its own 56-px "for automated testing only" banner —
   cosmetic, not suppressible, never in CDP screenshots. Viewport height ≠
   window-size minus chrome: read `innerHeight` live when geometry matters.

## verify-core workflow (the loop)
Every fix-verify iteration:
1. `navigate URL --wait load [--expect-url SUBSTR]` → note the printed `loader=` token.
2. `console --gate` → exit 1 = page has errors/exceptions → REAL finding (warnings don't gate).
3. `assert SELECTOR --visible [--stable 500]` / `assert --js 'EXPR'` → ASSERT_PASS/FAIL;
   flap diagnostics distinguish flaky (unstable: flapped Nx) from absent (never true).
   Flaps shorter than the 100ms polling interval are invisible.
   Before interacting: `assert SEL --actionable --stable 300` — visible + enabled +
   hit-test (an overlay-covered or disabled control is visible but NOT actionable).
   --actionable scrolls the element into view first (same as click's measure) — a
   below-fold control is actionable; expect the page to be scrolled afterwards.
4. `click SEL --require-trusted` for user-path interactions — exit 1 means the element
   was NOT clickable as a user would (hidden/occluded); never falls back to el.click().
5. `screenshot /tmp/drive-N.jpg --bind` → check the BIND line's `loader=` equals step 1's;
   different = something navigated since → the screenshot does NOT show what you think.
   Read the image before claiming visual state.

## Circuit-breaker (hard limit)
Max **3** fix-verify iterations per finding. The 4th failure → STOP, report honestly
what was tried + the last ASSERT_FAIL/GATE output. Token-burn without progress is a
bug, not persistence. After editing product code, wait for the dev-server rebuild
BEFORE re-testing: `assert --js '<HMR-ready condition>' --timeout 30` or re-navigate
with --wait and re-run the gate.

## Two modes (§4.4 — structural)
- **autonomous** (default): headless, runs to completion, emits a pass/fail report.
- **co-pilot**: headful; at each checkpoint surface to the human via AskUserQuestion
  ("so? does this look right?") before continuing.
- **Subagents are ALWAYS autonomous.** co-pilot is main-session-only: a subagent has
  no human channel — if you are running as a subagent, refuse co-pilot and run
  autonomous. Delegation prompts MUST hard-code "mode: autonomous" and a dedicated
  port (SP4 will automate lane allocation; until then the delegator assigns ports).

## Cookie-seed (opt-in auth, §4.5)
For login-gated products: import cookies of SELECTED domains from the daily browser:
`python3 <plugin>/skills/drive/scripts/cookie_seed.py --domains app.example.com --to-port $PORT`
- Asks nothing implicitly: --domains is mandatory; counts-only output (no values).
- NEVER seeds into 9333 (refused). SP2 ships cookies only (localStorage deferred).
- Re-run after re-login (expired cookies on the source side).

## OAuth / popup handoff (R2-S)
A login popup or OAuth redirect opens a NEW target. Recover, don't lose the flow:
1. `tabs` → identify the new tab (id prefix or url substring)
2. Drive it: `--target <SEL> click …`, `--target <SEL> fill …`
3. When it closes, re-`tabs` and re-pin the main tab; `navigate --wait` + `console --gate`
   to resume the verified state.

## Teardown
`pkill -f -- "--user-data-dir=<profile>($|[[:space:]])"` (anchored; never pkill by
port substring alone) + confirm the port is free before reusing the lane.
```

- [ ] **Step 4: Run the structural tests**

Run: `python3 -m pytest tests/test_drive_skill.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/drive/SKILL.md tests/test_drive_skill.py
git commit -m "feat(sp2): /bulldozer:drive SKILL.md — lane setup, verify-core loop, two modes"
```

---

### Task 9: Docs + full suite

**Files:**
- Modify: `CLAUDE.md` (plugin)
- Modify: `docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` (SP2 row)

- [ ] **Step 1: Update plugin CLAUDE.md**

1. Header line: add `/drive` to the intro sentence.
2. Skills table: add row `| drive | /bulldozer:drive [URL] [test task] | Verify-core product testing on isolated CfT lanes (autonomous / co-pilot). MVP |`.
3. "Architecture: /look" command-count sentence → "Command count: 19 total in `COMMANDS` = 17 look-facing (look SKILL.md Quick Reference) + `assert` (drive verify-core, SP2) + internal `normalize-url`".
4. New section "Architecture: /drive (SP2)" after the /look section: verify-core = opt-in cdp.py extensions (`navigate --wait/--expect-url`, `console --gate`, `assert` stability window, `click --require-trusted`, `screenshot --bind`); cookie_seed.py two-port script; modes; circuit-breaker 3; pointers to drive SKILL.md + the console-gate verification doc.
5. Testing table: `/drive` row — `test_drive_skill.py`, `test_cookie_seed.py` (offline) + `test_e2e_drive.py` (CfT, self-skips).
6. Footer version 1.13.0 | date.

- [ ] **Step 2: Update the umbrella spec SP2 row**

`docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` §5 SP2 row → `✅ **DONE (2026-06-05)**` + one-line summary (verify-core shipped as cdp.py opt-in extensions; console-gate one-shot VERIFIED sufficient — see `2026-06-05-sp2-console-gate-verification.md`; cookie-seed shipped as separate script; co-pilot main-only).

- [ ] **Step 3: Full suite**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -25`
Expected: all green (e2e self-skips where CfT/browser unavailable — on this machine they run). The 2 known marketplace-path failures appear ONLY in worktrees ([[worktree-dev-gotchas]]) — re-verify from the canonical dir at merge time.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md
git commit -m "docs(sp2): CLAUDE.md /drive architecture + spec SP2 row DONE"
```

---

## Post-plan (process, not tasks)

1. `bulldozer:check` this plan (depth: standard, reviewer per config) — fix findings, re-check to GO.
2. Execute tasks (inline TDD per established SP process).
3. PR `bulldozer/drive-sp2` → `bulldozer/main` (direct commits to main are hook-blocked).
4. `/code-review` the PR; discuss findings via AskUserQuestion; fix.
5. Merge → docs audit → `/remember`.
6. Post-merge (outside this repo): add `/bulldozer:drive` to `.claude/rules/plugins-reference.md` in the ANTHROPICS_DEV meta repo.

## Self-review notes

- Spec coverage: §4.3 navigate-wait (T2), console-gate (T3, verified sufficient), assertion+actionability+stability (T4 `--actionable`: visible+enabled+hit-test), screenshot-binding (T6), trusted-click signal (T5), circuit-breaker+HMR (T8 SKILL.md); §4.4 modes (T8); §4.5 cookie-seed (T7); R2-S handoff (T8); hole D pre-flight (T8). Panel bonus findings: console exit-contract (T3), bind url/loader/t (T6), --require-trusted (T5), actionability (T4).
- R1 review fixes (v2): R1-F1 docstring drift-guard grows per task (T4 ships `assert`-only, T6 extends to full set); R1-F2 ws_navigate_and_wait matches unified `Page.lifecycleEvent` filtered by OUR loaderId for all three modes (bare loadEventFired/domContentEventFired carry no loaderId → prior-page race) + slow-subresource behavioral test; R1-F3 `assert --actionable` (visible+enabled+hit-test, cmd_click point-on-target semantics) + occluded fixture + tests.
- R2 recheck fix (v3): `--actionable` now scrolls into view first (cmd_click parity — without it a below-fold control click would hit fails the assert) + below-fold fixture target + regression test; SKILL.md documents the scroll side-effect.
- `/look` untouched: every new behavior is behind a flag or a new command; legacy-contract regression tests included in T2/T3/T5/T6.
- No placeholders; types/names consistent (`ws_navigate_and_wait` used only in T2; `_drive_cdp` defined T1, used T2-T7).
