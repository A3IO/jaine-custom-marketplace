# Trusted Click in /look — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `cdp.py cmd_click` dispatch a *trusted* CDP `Input.dispatchMouseEvent` (granting browser user-activation, so audio/autoplay/clipboard/pointer-lock/fullscreen unblock), with an automatic untrusted `el.click()` fallback for non-hittable elements so existing click use-cases never regress.

**Architecture:** On the websocket channel, `cmd_click` does ONE `Runtime.evaluate` that scrolls the element into view *instantly*, measures its box center, and hit-tests via `elementFromPoint`. If hittable → trusted press+release via a new `ws_send_seq` helper (one connection). If not hittable (zero-box / occluded / off-viewport) → untrusted `el.click()` + stderr WARN, still `return 0`. The AppleScript channel (no CDP Input domain) stays `el.click()` with an untrusted marker. Implements spec `docs/superpowers/specs/2026-06-02-look-trusted-click-design.md` (codex GO).

**Tech Stack:** Python 3 (stdlib + vendored `websocket-client`), Chrome DevTools Protocol (`Input.dispatchMouseEvent`, `Runtime.evaluate`), pytest (+ real JAINE Browser for e2e).

---

## File Structure

| File | Change | Responsibility |
|------|--------|----------------|
| `skills/look/scripts/cdp.py` | Modify | Add `ws_send_seq` helper (~line 124, after `ws_send`); rewrite `cmd_click` (currently lines 594-614) |
| `tests/fixtures/test-page.html` | Modify | Add 4 deterministic test elements (trusted probe, hidden, occluded, below-fold smooth-scroll) |
| `tests/test_cdp.py` | Modify | Add 3 structural tests (`ws_send_seq` shape, `cmd_click` uses trusted Input, `cmd_click` same-ws_url) |
| `tests/test_e2e.py` | Modify | Add 4 behavioral tests (trusted, hidden→fallback, occluded→fallback, below-fold→trusted) |
| `tests/conftest.py` | Modify | **Task 0** — `jaine_browser` fixture honors `CDP_PORT` env; non-9333 → dedicated isolated test browser (temp profile) instead of reusing the user's 9333 |
| `skills/look/SKILL.md` | Modify | Document trusted gesture + fallback + active-tab note |

**Environment notes for the implementer:**
- Work happens in worktree `bulldozer/feat/look-trusted-click` (cwd `/0/.aitemp/bulldozer-look-trusted-click`).
- **2 pre-existing test failures in the worktree are NOT regressions:** `test_claude_md_cache_path_uses_correct_marketplace` and `test_spec_cache_path_uses_correct_marketplace` fail because they resolve `PLUGIN_ROOT.parent.parent/.claude-plugin/marketplace.json`, which doesn't exist when the worktree lives under `/0/.aitemp/`. They PASS from the main checkout. Ignore them; do not "fix".
- e2e tests need a JAINE Browser. After **Task 0**, set `CDP_PORT=9334` to run them against a dedicated **isolated** test browser (temp profile) so they never touch the user's interactive browser on 9333. Default (`CDP_PORT` unset = 9333) keeps the original reuse-or-launch behavior.
- Run offline structural tests with: `python3 -m pytest tests/test_cdp.py -v`. Run e2e (isolated) with: `CDP_PORT=9334 python3 -m pytest tests/test_e2e.py -v`.

---

## Task 0: Isolate the e2e test browser (conftest port-configurable)

**Why first:** the e2e tests in Tasks 2-3 drive a real browser. Today `conftest.py` reuses the user's interactive browser on port 9333 (and navigates its active tab), so running the suite clobbers whatever the user is doing there. This task makes the e2e fixture launch its OWN dedicated browser on a configurable port + temp profile, fully isolated from 9333. **Test-hygiene only** — production `launch.sh`/`cdp.py` are untouched (user-facing multi-instance `/look` is a separate concern, #141).

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Make `CDP_PORT` env-configurable + add a Chrome constant**

In `tests/conftest.py`, add `import tempfile` alongside the existing imports, and replace the hardcoded port line:

```python
CDP_PORT = 9333
```
with:
```python
CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

- [ ] **Step 2: Rewrite the `jaine_browser` fixture to isolate non-default ports**

Replace the entire `jaine_browser` fixture with:

```python
@pytest.fixture(scope="session")
def jaine_browser():
    """Ensure a JAINE Browser is running on CDP_PORT. Reuse if already online.
    Default port 9333 → the user's production browser (via launch.sh).
    Any non-9333 CDP_PORT → a dedicated, isolated test browser with a temp
    profile (never touches the user's interactive browser)."""
    if _cdp_is_online():
        yield "reused"
        return

    if CDP_PORT == 9333:
        subprocess.Popen(
            ["bash", LAUNCH_SCRIPT, "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        kill_match = "user-data-dir=" + BROWSER_PROFILE
    else:
        profile = tempfile.mkdtemp(prefix="jaine-test-{}-".format(CDP_PORT))
        subprocess.Popen(
            [CHROME, "--user-data-dir=" + profile,
             "--remote-debugging-port={}".format(CDP_PORT),
             "--remote-allow-origins=*", "--no-first-run",
             "--no-default-browser-check", "--window-size=1440,900", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        kill_match = "user-data-dir=" + profile

    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_is_online():
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", kill_match], capture_output=True)
        pytest.fail("JAINE Browser did not start on port {} within 20s".format(CDP_PORT))

    yield "launched"

    subprocess.run(["pkill", "-f", kill_match], capture_output=True)
```

(`BROWSER_PROFILE` and `LAUNCH_SCRIPT` constants already exist; the temp profile is auto-cleaned by the OS. Headful — matches the existing fixture and keeps the window/viewport e2e tests working.)

- [ ] **Step 3: Verify — isolated launch works, 9333 untouched**

With the user's browser running on 9333, run an EXISTING e2e test against an isolated port:

Run: `CDP_PORT=9334 python3 -m pytest tests/test_e2e.py::test_click_triggers_handler -v`
Expected: PASS — a fresh isolated Chrome launches on 9334 with a temp profile, the test navigates+clicks IT (old `el.click()` behavior, unchanged at this point), and the user's 9333 browser/tab is untouched. Confirm 9333 intact: `python3 skills/look/scripts/cdp.py status` still shows its original tab.

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test(look): conftest honors CDP_PORT — isolated e2e browser, no 9333 clobber (#140)"
```

---

## Task 1: `ws_send_seq` helper (one connection for press+release)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add after `ws_send`, ~line 124)
- Test: `tests/test_cdp.py`

- [ ] **Step 1: Write the failing structural test**

Append to `tests/test_cdp.py`:

```python
def test_ws_send_seq_single_connection():
    """ws_send_seq must exist and open exactly ONE websocket connection
    (press+release share it) while iterating the call sequence."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def ws_send_seq" in source, "Missing ws_send_seq helper"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ws_send_seq":
            body = ast.get_source_segment(source, node)
            assert body.count("create_connection") == 1, \
                "ws_send_seq must open exactly one connection for the whole sequence"
            assert "for " in body, "ws_send_seq must iterate over the call sequence"
            return
    raise AssertionError("ws_send_seq not found in cdp.py")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cdp.py::test_ws_send_seq_single_connection -v`
Expected: FAIL — `AssertionError: Missing ws_send_seq helper`.

- [ ] **Step 3: Implement `ws_send_seq`**

In `skills/look/scripts/cdp.py`, immediately after the `ws_send` function (before `cdp_js`), add:

```python
def ws_send_seq(ws_url, calls):
    """Send a sequence of CDP methods over ONE connection (e.g. mousePressed +
    mouseReleased for a single trusted click). Returns the list of result dicts,
    or None on any transport/CDP error. Mirrors ws_send's error contract.

    `calls` is a list of (method, params) tuples. Responses are read in order;
    no CDP domains are enabled, so no unsolicited events interleave.
    """
    import websocket
    try:
        ws = websocket.create_connection(ws_url, timeout=30)
    except (websocket.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None
    results = []
    try:
        for i, (method, params) in enumerate(calls, start=1):
            msg = {"id": i, "method": method}
            if params:
                msg["params"] = params
            ws.send(json.dumps(msg))
            result = json.loads(ws.recv())
            if "error" in result:
                err = result["error"]
                print("CDP error: {} (code {})".format(
                    err.get("message", "unknown"), err.get("code", "?")), file=sys.stderr)
                return None
            results.append(result)
    except (websocket.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return None
    finally:
        ws.close()
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_cdp.py::test_ws_send_seq_single_connection -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat(look): ws_send_seq — one CDP connection for a method sequence (#140)"
```

---

## Task 2: test-page.html fixtures (prerequisite for e2e)

**Files:**
- Modify: `tests/fixtures/test-page.html`

- [ ] **Step 1: Add `scroll-behavior:smooth` to the page**

In the `<style>` block of `tests/fixtures/test-page.html`, add a rule so the below-fold case reproduces the smooth-scroll hazard:

```css
html { scroll-behavior: smooth; }
```

- [ ] **Step 2: Add the four test elements**

Insert these sections inside `<body>`, after the existing "Click Test" section:

```html
    <section>
        <h2>Trusted Click Test</h2>
        <button id="trusted-probe" onclick="
            window.__clickTrusted = event.isTrusted;
            window.__userActivation = (navigator.userActivation && 'isActive' in navigator.userActivation) ? navigator.userActivation.isActive : null;
            document.getElementById('trusted-status').textContent = 'isTrusted=' + window.__clickTrusted;
        ">Trusted Probe</button>
        <div id="trusted-status" class="status">no click</div>
    </section>

    <section>
        <h2>Hidden (display:none) Click Test</h2>
        <button id="hidden-btn" style="display:none" onclick="window.__hiddenClicked = true">Hidden</button>
        <div id="hidden-status" class="status">hidden, expect el.click fallback</div>
    </section>

    <section>
        <h2>Occluded Click Test</h2>
        <div style="position:relative; width:200px; height:60px">
            <button id="occluded-btn" style="position:absolute; inset:0"
                    onclick="window.__occludedClicked = true">Occluded</button>
            <div id="occluder" style="position:absolute; inset:0; z-index:10; background:rgba(255,0,0,0.05)"></div>
        </div>
    </section>
```

And add this section LAST in `<body>` (far below the fold) so `scrollIntoView` must actually scroll:

```html
    <section style="margin-top: 2200px">
        <h2>Below-Fold Smooth-Scroll Test</h2>
        <button id="belowfold-btn" onclick="
            window.__belowfoldTrusted = event.isTrusted;
            window.__belowfoldClicked = true;
        ">Below Fold</button>
    </section>
```

- [ ] **Step 3: Verify the fixtures load**

Ensure the JAINE Browser is running, then:

Run:
```bash
CDP_PORT=9334 python3 -m pytest tests/test_e2e.py::test_click_triggers_handler -v
```
Expected: PASS (the existing click test still works against the modified page — confirms the page is valid HTML and unchanged for `#test-btn`).

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/test-page.html
git commit -m "test(look): test-page elements for trusted-click + fallback cases (#140)"
```

---

## Task 3: rewrite `cmd_click` (trusted dispatch + fallback) — TDD

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`cmd_click`, currently lines 594-614)
- Test: `tests/test_cdp.py` (structural), `tests/test_e2e.py` (behavioral)

- [ ] **Step 1: Write the failing structural test**

Append to `tests/test_cdp.py`:

```python
def test_cmd_click_uses_trusted_input():
    """cmd_click must dispatch a trusted CDP Input event, force instant scroll,
    hit-test, and retain an el.click() fallback."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_click":
            body = ast.get_source_segment(source, node)
            assert "Input.dispatchMouseEvent" in body, "cmd_click must use CDP Input.dispatchMouseEvent"
            assert "ws_send_seq" in body, "cmd_click must use ws_send_seq for press+release"
            assert "behavior:'instant'" in body, "cmd_click must force instant scroll before measuring"
            assert "elementFromPoint" in body, "cmd_click must hit-test via elementFromPoint"
            assert "el.click()" in body, "cmd_click must retain the el.click() fallback"
            return
    raise AssertionError("cmd_click not found in cdp.py")


def test_cmd_click_uses_same_ws_url_for_measure_and_dispatch():
    """cmd_click's websocket path must capture ONE ws_url and use direct
    Runtime.evaluate (NOT cdp_js) for measure + fallback, so measure/dispatch
    can't drift to different tabs in a multi-tab browser. Mirrors the screenshot
    same-target guard test_scale_reads_devicepixelratio_via_same_ws_connection."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_click":
            # inspect CALLS (not raw source) so a comment mentioning cdp_js does
            # not self-defeat the assertion (R2-F1).
            called = {n.func.id for n in ast.walk(node)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "cdp_js" not in called, (
                "cmd_click must NOT call cdp_js (it re-resolves get_tab() per call → "
                "multi-tab drift); use ws_send(ws_url, 'Runtime.evaluate', ...) instead."
            )
            assert "ws_send_seq" in called, "cmd_click must dispatch via ws_send_seq"
            body = ast.get_source_segment(source, node)
            assert "ws_send_seq(ws_url" in body, \
                "cmd_click must pass the captured ws_url to ws_send_seq"
            assert "Runtime.evaluate" in body, \
                "cmd_click must use direct Runtime.evaluate for measure/fallback"
            return
    raise AssertionError("cmd_click not found in cdp.py")
```

- [ ] **Step 2: Write the failing e2e tests**

Append to `tests/test_e2e.py`:

```python
def test_click_trusted_grants_user_activation(test_page_url):
    """Visible button → trusted Input.dispatchMouseEvent → isTrusted + user activation."""
    r = run_cdp(["click", "#trusted-probe"])
    assert r.returncode == 0, "click failed: {}".format(r.stderr)
    assert "trusted" in r.stdout.lower(), "expected trusted marker, got: {}".format(r.stdout)
    assert "fallback" not in r.stdout.lower(), "visible button must not fall back: {}".format(r.stdout)
    t = run_cdp(["js", "String(window.__clickTrusted)"])
    assert "true" in t.stdout, "event.isTrusted should be true, got: {}".format(t.stdout)
    a = run_cdp(["js", "String(window.__userActivation)"])
    assert "true" in a.stdout, "userActivation.isActive should be true, got: {}".format(a.stdout)


def test_click_hidden_falls_back_untrusted(test_page_url):
    """display:none element → not hittable → untrusted el.click() fallback + stderr WARN."""
    r = run_cdp(["click", "#hidden-btn"])
    assert r.returncode == 0, "fallback click must still return 0: {}".format(r.stderr)
    assert "fallback" in r.stdout.lower(), "expected fallback marker, got: {}".format(r.stdout)
    assert "activation" in r.stderr.lower(), "expected user-activation WARN on stderr, got: {}".format(r.stderr)
    h = run_cdp(["js", "String(window.__hiddenClicked)"])
    assert "true" in h.stdout, "hidden handler should fire via el.click fallback: {}".format(h.stdout)


def test_click_occluded_falls_back(test_page_url):
    """Element under a higher-z-index overlay → elementFromPoint miss → fallback."""
    r = run_cdp(["click", "#occluded-btn"])
    assert r.returncode == 0, "occluded click must still return 0: {}".format(r.stderr)
    assert "fallback" in r.stdout.lower(), "occluded element must fall back, got: {}".format(r.stdout)


def test_click_belowfold_smooth_scroll_stays_trusted(test_page_url):
    """Below-fold button on a scroll-behavior:smooth page must STILL be trusted —
    regression guard that scrollIntoView uses behavior:'instant' (R1-F1)."""
    r = run_cdp(["click", "#belowfold-btn"])
    assert r.returncode == 0, "click failed: {}".format(r.stderr)
    assert "trusted" in r.stdout.lower() and "fallback" not in r.stdout.lower(), \
        "below-fold smooth-scroll button must be trusted (instant scroll), got: {}".format(r.stdout)
    t = run_cdp(["js", "String(window.__belowfoldTrusted)"])
    assert "true" in t.stdout, "below-fold isTrusted should be true, got: {}".format(t.stdout)
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run (structural, no `-k` — explicit nodeids are NOT subject to the e2e keyword filter): `python3 -m pytest tests/test_cdp.py::test_cmd_click_uses_trusted_input tests/test_cdp.py::test_cmd_click_uses_same_ws_url_for_measure_and_dispatch -v`
Then run (e2e, `-k`): `CDP_PORT=9334 python3 -m pytest tests/test_e2e.py -k "trusted or hidden or occluded or belowfold" -v`
Expected: FAIL — structural tests fail (`Input.dispatchMouseEvent` absent; old `cmd_click` still uses `cdp_js`); e2e `trusted`/`belowfold` fail (`__clickTrusted` is `false` / no "trusted" marker); `hidden`/`occluded` fail (no "fallback" marker). This is the RED state.

- [ ] **Step 4: Rewrite `cmd_click`**

Replace the entire `cmd_click` function (currently lines 594-614) in `skills/look/scripts/cdp.py` with:

```python
def cmd_click(args):
    if not args:
        print("Usage: cdp.py click SELECTOR")
        return 1
    selector = args[0]
    sel = json.dumps(selector)

    # AppleScript channel: no CDP Input domain → untrusted el.click() only.
    if not has_websocket():
        expr = ("(function(){ var el=document.querySelector(" + sel + ");"
                " if(!el) return 'NOT_FOUND'; el.click(); return 'clicked '+el.tagName })()")
        val = as_js_main_world(expr)
        if val is None:
            return 1
        if val == "NOT_FOUND":
            print("ERROR: '{}' not found".format(selector), file=sys.stderr)
            return 1
        print(val + " (untrusted: AppleScript channel)")
        print("WARN: AppleScript channel cannot grant user activation — "
              "trusted click needs the CDP/websocket channel", file=sys.stderr)
        log("click", channel=channel(), selector=selector, trusted="no")
        return 0

    # websocket channel: capture ONE ws_url and use it for measure, fallback,
    # AND dispatch — same-target consistency. cdp_js is NOT used here: it
    # re-resolves get_tab() on every call, so in a multi-tab browser the measure
    # could land on tab A and the trusted dispatch on tab B (R1-F1). Mirrors
    # cmd_screenshot's --scale DPR read (same-ws_url pattern).
    tab = get_tab()
    ws_url = tab["webSocketDebuggerUrl"]

    measure = ("(function(){ var el=document.querySelector(" + sel + ");"
               " if(!el) return {found:false};"
               " el.scrollIntoView({block:'center',inline:'center',behavior:'instant'});"
               " var r=el.getBoundingClientRect();"
               " var cx=r.left+r.width/2, cy=r.top+r.height/2;"
               " var hit=document.elementFromPoint(cx,cy);"
               " return {found:true,cx:cx,cy:cy,w:r.width,h:r.height,"
               " onTarget:(!!hit&&(hit===el||el.contains(hit))),tag:el.tagName}; })()")
    mr = ws_send(ws_url, "Runtime.evaluate", {"expression": measure, "returnByValue": True})
    if mr is None:
        return 1
    rr = mr.get("result", {}).get("result")
    meas = rr.get("value") if isinstance(rr, dict) else None
    if not isinstance(meas, dict):
        print("ERROR: unexpected measure result for '{}'".format(selector), file=sys.stderr)
        return 1
    if not meas.get("found"):
        print("ERROR: '{}' not found".format(selector), file=sys.stderr)
        return 1
    tag = meas.get("tag", "?")
    hittable = meas.get("w", 0) > 0 and meas.get("h", 0) > 0 and meas.get("onTarget")

    if not hittable:
        # zero-box / occluded / off-viewport → untrusted el.click() fallback (SAME ws_url).
        fb_expr = ("(function(){ var el=document.querySelector(" + sel + ");"
                   " if(!el) return 'NOT_FOUND'; el.click(); return 'clicked '+el.tagName })()")
        fr = ws_send(ws_url, "Runtime.evaluate", {"expression": fb_expr, "returnByValue": True})
        if fr is None:
            return 1
        fr_r = fr.get("result", {}).get("result")
        if isinstance(fr_r, dict) and fr_r.get("value") == "NOT_FOUND":
            print("ERROR: '{}' not found".format(selector), file=sys.stderr)
            return 1
        print("clicked {} (fallback: el.click, untrusted)".format(tag))
        print("WARN: '{}' not hittable (hidden/occluded/off-viewport) — fell back to "
              "el.click(); user activation NOT granted".format(selector), file=sys.stderr)
        log("click", channel=channel(), selector=selector, trusted="no", fallback="yes")
        return 0

    # hittable → trusted press+release at the box center, SAME ws_url (one connection).
    cx, cy = meas["cx"], meas["cy"]
    seq = ws_send_seq(ws_url, [
        ("Input.dispatchMouseEvent",
         {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "clickCount": 1}),
        ("Input.dispatchMouseEvent",
         {"type": "mouseReleased", "x": cx, "y": cy, "button": "left", "clickCount": 1}),
    ])
    if seq is None:
        return 1
    print("clicked {} (trusted)".format(tag))
    log("click", channel=channel(), selector=selector, trusted="yes")
    return 0
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run (structural, no `-k` — explicit nodeids are NOT subject to the e2e keyword filter): `python3 -m pytest tests/test_cdp.py::test_cmd_click_uses_trusted_input tests/test_cdp.py::test_cmd_click_uses_same_ws_url_for_measure_and_dispatch -v`
Then run (e2e, `-k`): `CDP_PORT=9334 python3 -m pytest tests/test_e2e.py -k "trusted or hidden or occluded or belowfold" -v`
Expected: PASS (all six — 2 structural + 4 e2e).

- [ ] **Step 6: Run the full /look suites to confirm no regression**

Run: `CDP_PORT=9334 python3 -m pytest tests/test_cdp.py tests/test_e2e.py -v`
Expected: All PASS except the 2 known worktree-path-brittle marketplace tests (see Environment notes). In particular `test_click_triggers_handler`, `test_b4_click_fails_on_cdp_error`, and `test_cmd_click_exists` (confirms `COMMANDS["click"]` still maps to `cmd_click` — covers the spec §5 "COMMANDS unchanged" check) must still PASS.

- [ ] **Step 7: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py tests/test_e2e.py
git commit -m "feat(look): trusted click via CDP Input.dispatchMouseEvent + hittability fallback (#140)"
```

---

## Task 4: SKILL.md documentation

**Files:**
- Modify: `skills/look/SKILL.md`

- [ ] **Step 1: Update the `click` command comment**

Find the line (around line 87):
```
python3 "$CDP" click SELECTOR            # click element
```
Replace with:
```
python3 "$CDP" click SELECTOR            # click element (trusted gesture on CDP channel)
```

- [ ] **Step 2: Augment the "click vs js" note**

Find the `**click vs js:**` note (around line 132) and add this paragraph immediately after it:

```markdown
**Trusted gesture:** on the CDP (websocket) channel, `click` dispatches a real `Input.dispatchMouseEvent` (press+release at the element's box center), so the click is `isTrusted` and grants browser **user activation** — it can unblock `AudioContext.resume()`, autoplay, clipboard write, pointer lock, and fullscreen (verify audio by clicking the play control). If the element is not hittable (hidden / `display:none` / occluded by an overlay / off-viewport after scroll), `click` automatically falls back to an untrusted `el.click()`, prints `(fallback: ... untrusted)`, and WARNs on stderr that user activation was not granted. The AppleScript fallback channel (no CDP Input) is always untrusted. There is no autoplay flag — a real trusted click is the faithful path (headless + parallel-session isolation are tracked separately in #141).
```

- [ ] **Step 3: Add an active-tab note**

Add this note in the same notes area (after the Trusted-gesture paragraph):

```markdown
**Active tab:** all commands act on the browser's active tab. If it has drifted (another task navigated it), issue an explicit `navigate` (or select the tab) before `click`/`js` so you operate on the page you think you do.
```

- [ ] **Step 4: Verify no skill-doc structural test breaks**

Run: `python3 -m pytest tests/test_cdp.py -k "skill or heredoc or SKILL" -v`
Expected: PASS (or "no tests ran" if none match — either is fine; this just guards the SKILL.md structural checks).

- [ ] **Step 5: Commit**

```bash
git add skills/look/SKILL.md
git commit -m "docs(look): document trusted click + fallback + active-tab note (#140)"
```

---

## Self-Review

**Spec coverage (against `2026-06-02-look-trusted-click-design.md`):**
- §3.1 trusted flow (scrollIntoView instant → measure → elementFromPoint → trusted/fallback) → Task 3 Step 4. ✓
- §3.2 `ws_send_seq` (one connection, press+release) → Task 1. ✓
- §3.2 `cmd_click` rewrite + same-target `ws_url` (direct `ws_send` for measure/fallback, no `cdp_js`) → Task 3. ✓
- §3.3 AppleScript channel stays untrusted + WARN → Task 3 Step 4 (no-websocket branch). ✓
- §4 error handling (fallback return 0 + WARN; NOT_FOUND/transport → return 1) → Task 3 Step 4. ✓
- §5 tests (test-page elements a/b/c/d; e2e trusted/hidden/occluded/smooth-scroll; structural Input.dispatchMouseEvent + ws_send_seq + fallback branch) → Tasks 2, 3. ✓
- §6 SKILL.md (trusted gesture, fallback, active-tab, no autoplay flag) → Task 4. ✓
- §7 scope (cmd_fill untouched; no autoplay flag; #141/#93 out) → respected (no tasks touch them). ✓
- R1-F1 (instant scroll) → Task 3 Step 4 (`behavior:'instant'`) + Task 2/3 below-fold smooth-scroll regression test. ✓

**Placeholder scan:** none — every code/test step contains complete code; every run step has an exact command + expected result.

**Type consistency:** `ws_send_seq(ws_url, calls)` defined Task 1, called in Task 3 with a list of `(method, params)` tuples. ✓ Measure-result keys (`found, cx, cy, w, h, onTarget, tag`) produced by the JS in Task 3 Step 4 and consumed in the same function. ✓ stdout markers (`(trusted)`, `(fallback: ... untrusted)`, `(untrusted: AppleScript channel)`) asserted by the e2e tests in Task 3 Step 2 match the prints in Step 4. ✓

---

## Execution Handoff

Empirically de-risked already: a spike this session proved `el.click()` → `isTrusted:false` / audio `suspended`, and `Input.dispatchMouseEvent` → `isTrusted:true` / `userActivation` active / audio `running` — in an isolated Chrome, in our setup. The e2e tests in this plan codify that proof.
