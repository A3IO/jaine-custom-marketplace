# /look-v2 sub-B — Window Query over CDP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `cdp.py window bounds` work against a headless lane by querying window bounds over CDP, with a single normalized stdout contract across both channels.

**Architecture:** `cmd_window`'s `bounds` action currently calls AppleScript only (`get bounds of window 1`), so it returns 1 against a headless lane (no GUI). Add a `has_websocket()` branch that calls `Browser.getWindowForTarget({targetId})` over the page target's existing websocket (the same `tab["webSocketDebuggerUrl"]` every other `cmd_*` uses) and prints `left,top,width,height`. The AppleScript path stays as the `else` fallback but is normalized — via a new pure helper `_applescript_bounds` — to the identical `left,top,width,height` contract (AppleScript returns `x1, y1, x2, y2`; width=`x2-x1`, height=`y2-y1`). `upper`/`lower`/`activate` are untouched (headful-only GUI ergonomics). No changes to `conftest.py`, `launch.sh`, or any other command.

**Tech Stack:** Python 3 (`cdp.py`), bundled `websocket-client` (`skills/look/scripts/vendor/`), pytest (offline structural/unit + real-browser e2e via the `jaine_browser` headless lane from sub-A).

**Source:** spec `docs/superpowers/specs/2026-06-03-look-isolation-v2-design.md` §5 (B.1–B.4). Issue: #152.

### Spike findings (run during planning, 2026-06-03 — Chrome 148.0.7778.216, headless lane)

Confirmed empirically against a live `--headless=new` lane (so the printed format below is locked, not assumed):

- `Browser.getWindowForTarget({"targetId": <page target id>})` **works on the page target's `webSocketDebuggerUrl`** (identical result on the browser-level endpoint) → use `tab["webSocketDebuggerUrl"]`; no need to fetch `/json/version` for the browser ws.
- Response shape: `{"id":1,"result":{"windowId":N,"bounds":{"left":L,"top":T,"width":W,"height":H,"windowState":"normal"}}}`.
- A headless lane **does** have a window with real bounds (observed `left=1020,top=1020,width=1440,height=900`) — so `bounds` is meaningful headless.
- `targetId` = `get_tab()["id"]` (the `/json/list` page entry's `id`).

### Backward-compat invariant

On a normal machine the bundled `websocket-client` makes `has_websocket()` true, so `window bounds` takes the **CDP path in both headful and headless** — consistent with `navigate`/`reload`/`click`. The AppleScript path runs only when websocket is absent. The printed format changes from the old raw AppleScript `x1, y1, x2, y2` (comma-space) to `left,top,width,height` (comma, **no spaces**); this is the deliberate B.2 contract change, asserted by the e2e test.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `skills/look/scripts/cdp.py` | the `/look` CLI | add `_applescript_bounds` helper; rewrite `cmd_window`'s `bounds` action (CDP branch + normalized AppleScript fallback). `upper/lower/activate` unchanged. |
| `tests/test_cdp.py` | offline structural + unit | add `_load_cdp_module` loader; 2 unit tests for `_applescript_bounds`; 1 structural pin for the CDP branch; 2 `cmd_window` unit tests (AppleScript-fallback normalize end-to-end, malformed-CDP fail-loud). |
| `tests/test_e2e.py` | real-browser behavioral | rewrite `test_window_bounds_returns_coords` (un-skip headless; assert the 4-field `left,top,width,height` contract). Drop the now-unused `LANE_IS_HEADLESS` import. |
| `skills/look/SKILL.md` | agent-facing API docs | document the `left,top,width,height` contract + that `bounds` works headless while `upper/lower/activate` stay headful-only. |

`conftest.py` is intentionally **not** modified: `LANE_IS_HEADLESS` stays defined there (a meaningful lane property; harmless if only the import in `test_e2e.py` is dropped).

---

## Task 1: `_applescript_bounds` helper (pure, offline unit)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add helper immediately above `def cmd_window`)
- Test: `tests/test_cdp.py`

- [ ] **Step 1: Write the failing unit tests**

Append to `tests/test_cdp.py`:

```python
def _load_cdp_module():
    """Load cdp.py as a throwaway module (mirrors test_e2e.py's _load_image_dimensions
    — keeps unit tests in sync with the source of truth, and lets tests monkeypatch
    module globals like has_websocket/osascript/ws_send to drive cmd_* offline)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_cdp_under_test", CDP_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_applescript_bounds_normalizes_to_contract():
    """AppleScript `x1, y1, x2, y2` → CDP contract `left,top,width,height`."""
    fn = _load_cdp_module()._applescript_bounds
    assert fn("100, 100, 1540, 1000") == "100,100,1440,900"


def test_applescript_bounds_rejects_malformed():
    """Malformed AppleScript output fails loud (ValueError), no silent fallback."""
    fn = _load_cdp_module()._applescript_bounds
    raised = False
    try:
        fn("not bounds at all")
    except ValueError:
        raised = True
    assert raised, "_applescript_bounds must raise ValueError on malformed input"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cdp.py::test_applescript_bounds_normalizes_to_contract tests/test_cdp.py::test_applescript_bounds_rejects_malformed -v`
Expected: FAIL — `AttributeError: module '_cdp_under_test' has no attribute '_applescript_bounds'`.

- [ ] **Step 3: Implement the helper**

In `skills/look/scripts/cdp.py`, immediately **above** `def cmd_window(args):`, add:

```python
def _applescript_bounds(raw):
    """Normalize AppleScript `get bounds of window 1` (x1, y1, x2, y2) to the CDP
    stdout contract `left,top,width,height`. Raises ValueError on malformed input
    (fail-loud — cdp.py's no-silent-fallback principle)."""
    parts = [int(p.strip()) for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("expected 4 bounds values, got {}: {!r}".format(len(parts), raw))
    x1, y1, x2, y2 = parts
    return "{},{},{},{}".format(x1, y1, x2 - x1, y2 - y1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cdp.py::test_applescript_bounds_normalizes_to_contract tests/test_cdp.py::test_applescript_bounds_rejects_malformed -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat(look): _applescript_bounds — normalize AppleScript window bounds to CDP contract (sub-B)"
```

---

## Task 2: `cmd_window bounds` over CDP (e2e RED → CDP implementation → GREEN)

**Files:**
- Modify: `tests/test_e2e.py` (rewrite the window test)
- Modify: `tests/test_cdp.py` (add structural pin)
- Modify: `skills/look/scripts/cdp.py` (rewrite the `bounds` action)

- [ ] **Step 1: Rewrite the e2e test (un-skip headless, assert the 4-field contract)**

In `tests/test_e2e.py`, change the import line from:

```python
from conftest import run_cdp, LANE_IS_HEADLESS  # noqa: E402
```

to:

```python
from conftest import run_cdp  # noqa: E402
```

Then replace the entire skip-decorated test:

```python
@pytest.mark.skipif(
    LANE_IS_HEADLESS,
    reason="window bounds is AppleScript-only until sub-project B ports it to CDP "
           "(headless has no GUI)",
)
def test_window_bounds_returns_coords(jaine_browser):
    r = run_cdp(["window", "bounds"])
    assert r.returncode == 0, "window bounds failed: {}".format(r.stderr)
    assert "," in r.stdout, "Expected comma-separated bounds, got: {}".format(r.stdout)
```

with (no decorator — runs against the headless lane too):

```python
def test_window_bounds_returns_coords(jaine_browser):
    r = run_cdp(["window", "bounds"])
    assert r.returncode == 0, "window bounds failed: {}".format(r.stderr)
    out = r.stdout.strip()
    # B.2 contract: exactly `left,top,width,height` — comma-separated, no spaces.
    assert " " not in out, "contract is left,top,width,height (no spaces), got: {!r}".format(out)
    parts = out.split(",")
    assert len(parts) == 4, "expected 4 fields left,top,width,height, got: {!r}".format(out)
    left, top, width, height = (int(p) for p in parts)
    assert width > 0 and height > 0, "width/height must be positive, got {}x{}".format(width, height)
```

- [ ] **Step 2: Run the e2e test to verify it fails (visible RED, real headless lane)**

Run: `pytest tests/test_e2e.py::test_window_bounds_returns_coords -v`
Expected: FAIL — the fixture launches a headless lane; current `cmd_window` calls `osascript` (no GUI) → `osascript` returns None → `cmd_window` returns 1 → assertion `r.returncode == 0` fails with `"window bounds failed: ..."`.
(The fixture auto-launches an isolated headless test browser; no manual setup.)

- [ ] **Step 3: Add the structural pin + cmd_window unit tests in test_cdp.py**

Append to `tests/test_cdp.py`:

```python
def test_cmd_window_bounds_uses_cdp():
    """window bounds must use CDP Browser.getWindowForTarget (headless-capable),
    with the AppleScript fallback normalized via _applescript_bounds (sub-project B)."""
    source = Path(CDP_SCRIPT).read_text()
    assert "Browser.getWindowForTarget" in source, (
        "cmd_window bounds must call CDP Browser.getWindowForTarget"
    )
    assert "_applescript_bounds" in source, (
        "cmd_window AppleScript fallback must normalize bounds via _applescript_bounds"
    )


def test_cmd_window_applescript_fallback_normalizes():
    """cmd_window's AppleScript fallback (websocket absent) must ITSELF print the
    normalized left,top,width,height contract — not raw AppleScript output (B.2:
    both channels share one contract). The structural test above only proves the
    symbols exist; this drives cmd_window end-to-end with a stubbed osascript."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: False
    mod.osascript = lambda script: "100, 100, 1540, 1000"
    mod.log = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cmd_window(["bounds"])
    assert rc == 0, "fallback should succeed, got rc={}".format(rc)
    assert buf.getvalue().strip() == "100,100,1440,900", (
        "AppleScript fallback must print normalized contract, got: {!r}".format(buf.getvalue())
    )


def test_cmd_window_bounds_rejects_unexpected_cdp_response_shape():
    """A malformed CDP response (e.g. {"result": None}) must fail loud (rc 1), not
    raise an AttributeError traceback — the shape guard must be isinstance-safe."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: True
    mod.get_tab = lambda url_filter=None: {"webSocketDebuggerUrl": "ws://x", "id": "T"}
    mod.ws_send = lambda ws, method, params=None: {"result": None}
    mod.osascript = lambda script: "0, 0, 1, 1"  # pre-fix code falls here → rc 0 → RED
    mod.log = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cmd_window(["bounds"])
    assert rc == 1, "malformed CDP response must fail loud (rc 1), got rc={}".format(rc)
```

- [ ] **Step 4: Run the new test_cdp.py tests to verify they fail (RED)**

Run: `pytest tests/test_cdp.py::test_cmd_window_bounds_uses_cdp tests/test_cdp.py::test_cmd_window_applescript_fallback_normalizes tests/test_cdp.py::test_cmd_window_bounds_rejects_unexpected_cdp_response_shape -v`
Expected: all THREE FAIL —
- `test_cmd_window_bounds_uses_cdp`: `Browser.getWindowForTarget` not yet in `cdp.py`.
- `test_cmd_window_applescript_fallback_normalizes`: current `cmd_window` prints the raw stubbed `100, 100, 1540, 1000` via `print(r)`, not `100,100,1440,900`.
- `test_cmd_window_bounds_rejects_unexpected_cdp_response_shape`: current `cmd_window` ignores `has_websocket` and falls to the stubbed osascript → prints `0, 0, 1, 1` and returns 0, so `rc == 1` fails (post-fix it routes to CDP, hits the isinstance-safe guard, returns 1).

- [ ] **Step 5: Implement the CDP branch in `cmd_window`**

In `skills/look/scripts/cdp.py`, replace the current `bounds` action:

```python
    if action == "bounds":
        r = osascript('tell application "{}" to get bounds of window 1'.format(CHROME_APP))
        if r is None:
            return 1
        print(r)
```

with:

```python
    if action == "bounds":
        if has_websocket():
            tab = get_tab()
            r = ws_send(tab["webSocketDebuggerUrl"], "Browser.getWindowForTarget",
                        {"targetId": tab["id"]})
            if r is None:
                return 1
            result = r.get("result") if isinstance(r, dict) else None
            b = result.get("bounds") if isinstance(result, dict) else None
            if not isinstance(b, dict) or not all(k in b for k in ("left", "top", "width", "height")):
                print("ERROR: unexpected getWindowForTarget response: {}".format(r), file=sys.stderr)
                return 1
            print("{},{},{},{}".format(b["left"], b["top"], b["width"], b["height"]))
        else:
            r = osascript('tell application "{}" to get bounds of window 1'.format(CHROME_APP))
            if r is None:
                return 1
            try:
                print(_applescript_bounds(r))
            except ValueError as e:
                print("ERROR: cannot parse AppleScript window bounds: {}".format(e), file=sys.stderr)
                return 1
```

Leave `upper`, `lower`, `activate`, the `else`/`Usage` branch, the `log("window", action=action)` call, and `return 0` exactly as they are.

- [ ] **Step 6: Run e2e + structural + full offline suite to verify GREEN**

Run: `pytest tests/test_e2e.py::test_window_bounds_returns_coords tests/test_cdp.py::test_cmd_window_bounds_uses_cdp tests/test_cdp.py::test_cmd_window_applescript_fallback_normalizes tests/test_cdp.py::test_cmd_window_bounds_rejects_unexpected_cdp_response_shape tests/test_cdp.py::test_applescript_bounds_normalizes_to_contract tests/test_cdp.py::test_applescript_bounds_rejects_malformed -v`
Expected: PASS (6 passed) — `window bounds` returns e.g. `1020,1020,1440,900` against the headless lane; helper + AppleScript-fallback + malformed-shape unit tests all green.

Run: `pytest tests/ -v --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py -q`
Expected: PASS — no offline regressions (the new unit/structural tests included).

- [ ] **Step 7: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py tests/test_e2e.py
git commit -m "feat(look): window bounds over CDP — headless-capable, left,top,width,height contract (sub-B, #152)"
```

---

## Task 3: SKILL.md — document the contract + headless capability

**Files:**
- Modify: `skills/look/SKILL.md`

No test (docs). Edit by grep anchor (not line number — CLAUDE.md), reading the surrounding lines first to keep table columns aligned.

- [ ] **Step 1: Window-management command block**

Grep anchor: `# Window management`. Replace the four `window` command lines with:

```
# Window management
python3 "$CDP" window bounds   # → "left,top,width,height" (CDP; works headless)
python3 "$CDP" window upper    # move to upper monitor (headful only)
python3 "$CDP" window lower    # move to lower monitor (headful only)
python3 "$CDP" window activate # bring to front (headful only)
```

- [ ] **Step 2: Channel summary table (top)**

Grep anchor: `**AppleScript window**`. That row currently lists `bounds, move between monitors, activate` as the AppleScript-window capabilities. Since `bounds` is now CDP-first, change the capability cell to: `move between monitors, activate (+ bounds fallback)`. (Read the row first; keep the other two columns intact.)

- [ ] **Step 3: Headless implications section**

Grep anchor: `window upper/lower/activate` (in the "Headless ⇒ websocket-only" paragraph). Ensure the text states that **`window bounds` works headless over CDP**, while **`upper/lower/activate` stay headful-only**. Adjust the existing sentence so it no longer implies the whole `window` command is headful-only — only the three GUI-move actions are.

- [ ] **Step 4: Bottom channel table**

Grep anchor: `| window |`. The row currently reads `| window | — | AppleScript | — |`. Update it to reflect bounds-over-CDP with an AppleScript fallback, e.g.: `| window | WebSocket (bounds) | AppleScript (bounds fallback; upper/lower/activate) | — |`. (Read the header row first to match column count/order.)

- [ ] **Step 5: Verify + commit**

Run: `grep -n "left,top,width,height" skills/look/SKILL.md`
Expected: at least the command-block line matches (the contract is documented).

```bash
git add skills/look/SKILL.md
git commit -m "docs(look): window bounds CDP contract + headless capability (sub-B)"
```

---

## Self-Review

**1. Spec coverage (§5 B.1–B.4):**
- B.1 (CDP `Browser.getWindowForTarget` via `get_tab()["id"]`, AppleScript `else`) → Task 2 Step 5. ✓
- B.2 (fixed `left,top,width,height` on **both** channels; AppleScript normalized `x2-x1`/`y2-y1`; e2e asserts 4 labeled fields) → Task 1 (helper) + Task 2 Steps 1/5 (e2e + impl) + Task 3 (doc). ✓
- B.3 (`upper`/`lower`/`activate` stay AppleScript headful-only, documented) → unchanged in impl + Task 3 Steps 1/3. ✓
- B.4 (remove the headful-only skip on `test_window_bounds_returns_coords`; green headless) → Task 2 Steps 1/6. ✓
- B — acceptance (coords against headless; AppleScript fallback still works; headful-only documented; window e2e green headless) → covered. The AppleScript fallback is covered at **two** levels: `_applescript_bounds` in isolation, AND `cmd_window`'s fallback branch end-to-end (`test_cmd_window_applescript_fallback_normalizes` stubs `has_websocket→False` + `osascript`, asserting cmd_window itself prints the normalized contract — so a regression to raw `print(r)` is caught offline, not only on a websocket-absent Mac GUI). The CDP shape guard's fail-loud is pinned by `test_cmd_window_bounds_rejects_unexpected_cdp_response_shape` (isinstance-safe; no traceback on `{"result": None}`).

**2. Placeholder scan:** none — every code step shows complete code; every run step shows the exact command + expected result.

**3. Type/name consistency:** `_applescript_bounds` (defined Task 1, asserted Task 2 structural, called Task 2 impl + fallback test), `_load_cdp_module` (Task 1; reused by Task 2's `cmd_window` tests), `Browser.getWindowForTarget` + `targetId` + `tab["webSocketDebuggerUrl"]` + `tab["id"]` + `result.bounds.{left,top,width,height}` (consistent Task 2 ↔ spike findings). `LANE_IS_HEADLESS` import dropped in Task 2 Step 1 (the only consumer). ✓

---

## Next step (project flow, before execution)

Per the /look-v2 workflow (matches sub-A): run **`bulldozer:check`** on THIS plan first (catch ordering/logic gaps before code), then execute under **strict TDD with visible RED**, then **`/code-review`**, then open a **PR** (do NOT merge until Chris reviews; auto-calver bumps `plugin.json` on merge — never bump manually).
