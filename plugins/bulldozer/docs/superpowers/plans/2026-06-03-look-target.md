# /look-v2 sub-C — Tab-Target Pinning (`cdp.py --target`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a global `cdp.py --target <selector>` flag that pins every command in a call to one specific tab (by id, 12-char id-prefix, or url substring), killing intra-lane tab drift.

**Architecture:** `cdp.py` already has `get_tab(url_filter=None)` but no command exposes it — every `cmd_*` calls bare `get_tab()` → "first page". `main()` (extracted from the `__main__` block so it is unit-testable) parses a global `--target`/`--tab <selector>` from anywhere in argv and stores it in a module global `TARGET`. `get_tab` gains a selector resolver (exact id → unique 12-char id-prefix → url substring → fail-loud on ambiguous/miss). **Every** tab resolution then threads `TARGET` *explicitly*: direct callers become `get_tab(TARGET)`, and the `cdp_js` callers (`cmd_js`/`cmd_title`/`html`/`wait`/`fill`/`screenshot --full-page`) resolve the tab ONCE per command and pass the captured `ws_url` into `cdp_js(expr, ws_url)` — `cdp_js`'s self-`get_tab()` is removed and `ws_url` becomes a required arg. An AST structural test asserts no `get_tab(`/`cdp_js(` call site is left un-threaded, so a future command that forgets to pin fails the build.

**Why a module global + explicit threading (not a hidden default):** `cdp.py` dispatches uniformly via `COMMANDS[cmd](args)`; threading the selector through every `cmd_*` signature would be invasive and break that contract. A module global set by `main()` is the idiomatic argparse-free transport. We still read it *explicitly* at each call site (`get_tab(TARGET)`, `cdp_js(expr, ws_url)`) — NOT via a silent `get_tab()` default — precisely so the structural test can see the threading and a forgetful new caller is caught. This preserves the #140 same-`ws_url`-pinning discipline (resolve once, reuse the connection).

**Tech Stack:** Python 3 (`cdp.py`), bundled `websocket-client` (`skills/look/scripts/vendor/`), pytest (offline structural/unit via `_load_cdp_module()` + stubbed `cdp_get`/`ws_send`; real-browser e2e via the session `jaine_browser` headless lane).

**Source:** spec `docs/superpowers/specs/2026-06-03-look-isolation-v2-design.md` §6 (C.1–C.3 + acceptance) and §8 (testing). Issue: #153. Exemplar: sub-B plan `docs/superpowers/plans/2026-06-03-look-window-cdp.md`.

### Selector resolution contract (C.1)

Given `--target SEL` and the page targets from `/json/list`:

1. **exact id** — a target whose full `id == SEL`.
2. **unique id-prefix** — exactly one target whose `id.startswith(SEL)`, where `SEL` is **≥12 chars** (`tabs`/`status`/`open` print `id[:12]` — the copy-pasteable granularity; a shorter `SEL` is NOT treated as an id-prefix, so a 1–3 char string can't accidentally pin a tab — it falls through to the url check). ≥2 matches → **ambiguous, fail-loud**.
3. **url substring** — exactly one target whose `url` contains `SEL` (subsumes the old `url_filter`). ≥2 matches → **ambiguous, fail-loud**.
4. no match → **fail-loud**. `SEL is None` (no `--target`) → first page (today's behavior, backward-compat).

"Fail-loud" = `print(ERROR…, file=sys.stderr)` + `sys.exit(1)` — never silently drive the wrong tab. **`--target` requires the CDP/websocket channel:** `main()` rejects `--target` when `has_websocket()` is false (the AppleScript/native fallback binds to the active tab and cannot honor a CDP target id).

### Backward-compat invariant

No caller passes `url_filter` today (every `get_tab()` is bare). With no `--target`, `TARGET is None` → `get_tab(None)` returns the first page — byte-identical to current behavior. All existing single-tab e2e/unit tests stay green; the change is inert until `--target` is used.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `skills/look/scripts/cdp.py` | the `/look` CLI | module global `TARGET`; `get_tab(target=None)` resolver; `cdp_js(expr, ws_url)` (required `ws_url`); extract `main(argv)` with `--target`/`--tab` parse + websocket gate; thread `TARGET`/`ws_url` into all 14 tab-resolution sites; docstring note. |
| `tests/test_cdp.py` | offline structural + unit | resolver unit tests; `main(argv)` parse/gate unit tests; `cdp_js` arity + per-command pin units (stub `cdp_get`/`ws_send`); the AST "no un-pinned resolution" structural test. All via existing `_load_cdp_module()` (no pytest fixtures, so the `__main__` runner stays clean). |
| `tests/test_e2e.py` | real-browser behavioral | two-tab `--target` acceptance: the cdp_js family (`js`/`fill`/`wait`/`title`/`html`/`screenshot --full-page`) + direct (`navigate`/`click`) pin the right tab; url-substring + id-prefix selectors; ambiguous/miss fail loud; no-`--target` unchanged. |
| `skills/look/SKILL.md` | agent-facing API docs | document the global `--target` flag (command-ref + the "Active tab" drift note it fixes + parallel-tabs workflow + fallback-matrix note). |

`conftest.py`, `launch.sh` — **not** touched (sub-C is pure `cdp.py`; `--target` is a runtime flag, no launcher change).

---

## Task 1: `get_tab` selector resolver

**Files:**
- Modify: `skills/look/scripts/cdp.py` (rewrite `get_tab`; add module global `TARGET`)
- Test: `tests/test_cdp.py`

- [ ] **Step 1: Write the failing resolver unit tests**

Append to `tests/test_cdp.py` (uses the existing `_load_cdp_module()` at the bottom of the file; no pytest fixtures):

```python
# ── sub-C: --target tab pinning ──

TWO_TABS = [
    {"id": "AAAaaa111222333", "type": "page", "url": "http://localhost/a?look=tgtA",
     "webSocketDebuggerUrl": "ws://x/A"},
    {"id": "BBBbbb444555666", "type": "page", "url": "http://localhost/b?look=tgtB",
     "webSocketDebuggerUrl": "ws://x/B"},
]


def _stub_tabs(mod, tabs):
    """Point a loaded cdp.py module's cdp_get at a fixed /json/list (no browser)."""
    mod.cdp_get = lambda path: tabs


def _expect_systemexit(fn):
    """Run fn(); return True iff it raised SystemExit with a non-zero code."""
    try:
        fn()
    except SystemExit as e:
        return e.code not in (0, None)
    return False


def test_get_tab_none_returns_first_page():
    """No selector → first page (backward-compat lock; passes pre-impl too)."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab()["id"] == "AAAaaa111222333"


def test_get_tab_exact_id():
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab("BBBbbb444555666")["id"] == "BBBbbb444555666"


def test_get_tab_unique_id_prefix():
    """A 12-char prefix (what tabs/status/open display) resolves to that tab."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab("BBBbbb444555")["id"] == "BBBbbb444555666"  # 12-char prefix


def test_get_tab_url_substring():
    """Backward-compat lock: a url substring resolves (subsumes old url_filter)."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab("look=tgtB")["id"] == "BBBbbb444555666"


def test_get_tab_ambiguous_prefix_fails_loud():
    """A ≥12-char prefix matching ≥2 tabs exits non-zero (never silently picks one)."""
    mod = _load_cdp_module()
    _stub_tabs(mod, [
        {"id": "SHAREDPREFIX01aaaa", "type": "page", "url": "http://x/1", "webSocketDebuggerUrl": "ws://1"},
        {"id": "SHAREDPREFIX01bbbb", "type": "page", "url": "http://x/2", "webSocketDebuggerUrl": "ws://2"},
    ])
    assert _expect_systemexit(lambda: mod.get_tab("SHAREDPREFIX01")), \
        "ambiguous id-prefix must fail loud (SystemExit non-zero)"


def test_get_tab_short_prefix_not_an_id_match():
    """A <12-char selector is NOT treated as an id prefix (the displayed id is 12 chars)
    — it falls through to url-substring, so a 1-3 char string can't silently pin a tab
    by a too-short unique id prefix (R1-F4). "BBB" uniquely prefixes BBBbbb444555666's
    id but is <12 chars and not a url substring → fail loud (no match)."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert _expect_systemexit(lambda: mod.get_tab("BBB")), \
        "a <12-char id-prefix must NOT resolve as an id — fall through to url, then fail loud"


def test_get_tab_ambiguous_url_fails_loud():
    """A url substring matching ≥2 tabs is ambiguous → fail loud (never pick first)."""
    mod = _load_cdp_module()
    _stub_tabs(mod, [
        {"id": "id000000000001", "type": "page", "url": "http://x/p?dup=Z", "webSocketDebuggerUrl": "ws://1"},
        {"id": "id000000000002", "type": "page", "url": "http://x/q?dup=Z", "webSocketDebuggerUrl": "ws://2"},
    ])
    assert _expect_systemexit(lambda: mod.get_tab("dup=Z")), \
        "ambiguous url substring must fail loud"


def test_get_tab_no_match_fails_loud():
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert _expect_systemexit(lambda: mod.get_tab("zzz-no-such")), \
        "unknown selector must fail loud (SystemExit non-zero)"
```

- [ ] **Step 2: Run the resolver tests to verify they fail**

Run: `pytest tests/test_cdp.py -k get_tab -v`
Expected: the **new-behavior** tests FAIL (current `get_tab(url_filter)` treats the arg as a url substring, so an id/prefix selector matches nothing → it returns `tabs[0]`, and ambiguous/miss never raise):
- `test_get_tab_exact_id`, `test_get_tab_unique_id_prefix` → return the first tab `AAA…`, assert wants `BBB…` → FAIL.
- `test_get_tab_ambiguous_prefix_fails_loud`, `test_get_tab_ambiguous_url_fails_loud`, `test_get_tab_no_match_fails_loud`, `test_get_tab_short_prefix_not_an_id_match` → no `SystemExit` → FAIL (current code returns `tabs[0]` for all four).
- `test_get_tab_none_returns_first_page`, `test_get_tab_url_substring` already PASS (backward-compat locks — expected).

- [ ] **Step 3: Add the module global `TARGET`**

In `skills/look/scripts/cdp.py`, immediately after `CHROME_APP = "Google Chrome"`, add:

```python
# Global tab selector, set by main() from --target/--tab. None → first page (default).
TARGET = None
```

- [ ] **Step 4: Rewrite `get_tab` with the resolver**

Replace the entire current `get_tab`:

```python
def get_tab(url_filter=None):
    tabs = cdp_get("/json/list")
    if not tabs:
        print("ERROR: Browser not running on CDP port " + str(CDP_PORT), file=sys.stderr)
        sys.exit(1)
    for t in tabs:
        if t.get("type") != "page":
            continue
        if url_filter and url_filter not in t.get("url", ""):
            continue
        return t
    return tabs[0] if tabs else None
```

with:

```python
def get_tab(target=None):
    """Resolve a single tab. target=None → first page (backward-compat). Otherwise
    SEL resolves by: exact id → unique 12-char id-prefix → unique url substring.
    Ambiguous (≥2) or no match → fail loud (sys.exit) — never drive the wrong tab."""
    tabs = cdp_get("/json/list")
    if not tabs:
        print("ERROR: Browser not running on CDP port " + str(CDP_PORT), file=sys.stderr)
        sys.exit(1)
    pages = [t for t in tabs if t.get("type") == "page"]
    if target is None:
        return pages[0] if pages else tabs[0]
    # 1. exact id
    for t in pages:
        if t.get("id") == target:
            return t
    # 2. unique id-prefix — only at/above the 12-char displayed granularity
    #    (cmd_tabs/status/open print id[:12]); a <12-char selector is NOT an id
    #    prefix (it would collide too easily) → falls through to the url check.
    pref = ([t for t in pages if t.get("id", "").startswith(target)]
            if len(target) >= 12 else [])
    if len(pref) == 1:
        return pref[0]
    if len(pref) > 1:
        print("ERROR: --target {!r} is an ambiguous id prefix — {} tabs match ({}). "
              "Use more characters of the id.".format(
                  target, len(pref), ", ".join(t.get("id", "?")[:12] for t in pref)),
              file=sys.stderr)
        sys.exit(1)
    # 3. unique url substring (subsumes the old url_filter)
    urls = [t for t in pages if target in t.get("url", "")]
    if len(urls) == 1:
        return urls[0]
    if len(urls) > 1:
        print("ERROR: --target {!r} matches {} tab URLs ({}). Be more specific.".format(
                  target, len(urls), ", ".join(t.get("url", "?")[:50] for t in urls)),
              file=sys.stderr)
        sys.exit(1)
    # 4. no match → fail loud
    print("ERROR: --target {!r} matched no tab (not an id, id-prefix, or url substring). "
          "Run `tabs` to list open tabs.".format(target), file=sys.stderr)
    sys.exit(1)
```

- [ ] **Step 5: Run the resolver tests to verify they pass**

Run: `pytest tests/test_cdp.py -k get_tab -v`
Expected: PASS (8 passed).

- [ ] **Step 6: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat(look): get_tab selector resolver (id / id-prefix / url) for --target (sub-C)"
```

---

## Task 2: `main(argv)` — parse `--target`/`--tab` + websocket gate

**Files:**
- Modify: `skills/look/scripts/cdp.py` (extract `main(argv)`; docstring note)
- Test: `tests/test_cdp.py`

- [ ] **Step 1: Write the failing `main(argv)` unit tests**

Append to `tests/test_cdp.py`:

```python
def test_main_target_requires_websocket():
    """--target on the AppleScript fallback (no websocket) fails loud BEFORE dispatch."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.main(["--target", "abc", "title"])
    assert rc == 1, "expected rc 1, got {}".format(rc)
    assert "--target requires" in err.getvalue(), \
        "must explain --target needs websocket, got: {!r}".format(err.getvalue())


def test_main_target_missing_selector():
    """`--target` with no following value fails loud (not an IndexError)."""
    import io, contextlib
    mod = _load_cdp_module()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.main(["--target"])
    assert rc == 1
    assert "requires a selector" in err.getvalue()


def test_main_target_strips_to_help():
    """--target is global: `--target X --help` consumes the flag and still prints help
    (the flag is NOT mistaken for the command). Named with the `main_target` prefix so
    `-k "main_target or main_tab"` selects it WITHOUT also matching the existing
    `test_*_as_js_main_world_*` tests (a bare `-k main_` would — R1-F3)."""
    import io, contextlib
    mod = _load_cdp_module()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(["--target", "X", "--help"])
    assert rc == 0
    assert "cdp.py" in out.getvalue(), "help text should print"


def test_main_tab_is_alias_for_target():
    """--tab is an accepted alias (the websocket gate proves it was parsed)."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.main(["--tab", "abc", "title"])
    assert rc == 1 and "--target requires" in err.getvalue(), \
        "--tab must be parsed like --target"
```

- [ ] **Step 2: Run the `main` tests to verify they fail**

Run: `pytest tests/test_cdp.py -k "main_target or main_tab" -v`
Expected: all 4 FAIL — `AttributeError: module '_cdp_under_test' has no attribute 'main'` (there is no `main(argv)` yet; logic lives inline in the `__main__` block). **Use the explicit `main_target or main_tab` selector, NOT a bare `-k main_`** — `main_` also matches the existing `test_b9_as_js_main_world_clears_stale_result` / `test_b1_as_js_main_world_escapes_single_quotes` (substring `main_world`), which would dilute the "all FAIL" signal (R1-F3). The four new tests are named to satisfy this selector.

- [ ] **Step 3: Extract `main(argv)` with `--target`/`--tab` parsing + the gate**

In `skills/look/scripts/cdp.py`, replace the entire current `__main__` block:

```python
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print("Unknown: {}. Available: {}".format(cmd, ", ".join(sorted(COMMANDS))), file=sys.stderr)
        sys.exit(1)
    sys.exit(COMMANDS[cmd](sys.argv[2:]) or 0)
```

with:

```python
def main(argv):
    """Parse the global --target/--tab selector (from anywhere in argv) into the
    module global TARGET, then dispatch the command. --target requires the
    CDP/websocket channel (fail loud otherwise)."""
    global TARGET
    TARGET = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--target", "--tab"):
            if i + 1 >= len(argv):
                print("ERROR: {} requires a selector argument".format(a), file=sys.stderr)
                return 1
            TARGET = argv[i + 1]
            i += 2
            continue
        rest.append(a)
        i += 1
    if not rest or rest[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = rest[0]
    if cmd not in COMMANDS:
        print("Unknown: {}. Available: {}".format(cmd, ", ".join(sorted(COMMANDS))), file=sys.stderr)
        return 1
    if TARGET is not None and not has_websocket():
        print("ERROR: --target requires the CDP/websocket channel (the AppleScript/native "
              "fallback drives the active tab and cannot honor a target id)", file=sys.stderr)
        return 1
    return COMMANDS[cmd](rest[1:]) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Document the flag in the module docstring**

In the top docstring, add a `--target` line to the command list (after the `cdp.py window …` line) and a note after the `CDP_PORT env var…` line. Grep anchor: `CDP_PORT env var overrides default 9333.`

Add this command-list line (immediately above the `Channels:` line):

```
  cdp.py [--target SEL] CMD ...    — pin every tab resolution in the call to SEL
```

And append after `CDP_PORT env var overrides default 9333.`:

```
--target SEL (alias --tab) pins all of a call's commands to one tab: a full
target id, its 12-char prefix (as shown by `tabs`/`status`), or a url substring.
Requires the CDP/websocket channel (the AppleScript fallback drives the active tab).
```

- [ ] **Step 5: Run the `main` tests + the offline suite to verify GREEN + no regressions**

Run: `pytest tests/test_cdp.py -k "main_target or main_tab" -v`
Expected: PASS (4 passed).

Run: `pytest tests/test_cdp.py -v`
Expected: PASS for every cdp.py test (the `--help`/no-arg paths still work through `main`; the 2 marketplace tests `test_*_cache_path_uses_correct_marketplace` may fail in a worktree that lacks `../../.claude-plugin/marketplace.json` — pre-existing, unrelated to sub-C).

- [ ] **Step 6: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat(look): main(argv) parses global --target/--tab + websocket gate (sub-C, #153)"
```

---

## Task 3: `cdp_js(expr, ws_url)` + thread the indirect (`cdp_js`) callers

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`cdp_js`; `cmd_js`/`cmd_title`/`cmd_html`/`cmd_wait`/`cmd_fill`/`cmd_screenshot`)
- Test: `tests/test_cdp.py`

`cmd_js`/`cmd_title`/`html`/`wait`/`fill`/`screenshot --full-page` call `cdp_js`, which today self-resolves a bare `get_tab()`. Make `cdp_js` take a required `ws_url` (drop its self-`get_tab()`); each caller resolves `get_tab(TARGET)` ONCE and threads the `ws_url`.

- [ ] **Step 1: Write the failing arity + pin unit tests + the cdp_js-family e2e**

Append to `tests/test_cdp.py`:

```python
def _capture_ws_url(mod):
    """Stub ws_send to record the ws_url it was called with; return a benign OK result."""
    seen = {}
    def fake_ws_send(ws_url, method, params=None):
        seen["ws_url"] = ws_url
        return {"result": {"result": {"value": "ok"}}}
    mod.ws_send = fake_ws_send
    return seen


def test_cdp_js_requires_ws_url():
    """cdp_js no longer self-resolves get_tab(); ws_url is a REQUIRED param (C.2 pin).
    Signature-based so the RED is offline-deterministic — a call-based check
    (`mod.cdp_js("1+1")`) would execute the OLD 1-arg body's `get_tab()`→`cdp_get()`,
    which hits the live CDP port and may `sys.exit(1)`, making the RED env-dependent
    (R1-F2). Inspecting the signature touches no network."""
    import inspect
    mod = _load_cdp_module()
    params = inspect.signature(mod.cdp_js).parameters
    assert "ws_url" in params, "cdp_js must take an explicit ws_url param"
    assert params["ws_url"].default is inspect.Parameter.empty, \
        "ws_url must be a REQUIRED arg (no default) so a caller can't forget to thread it"


def test_cmd_title_pins_target_tab():
    """cmd_title (an indirect cdp_js caller) must drive the PINNED tab, not the first."""
    import io, contextlib
    mod = _load_cdp_module()
    _stub_tabs(mod, TWO_TABS)
    mod.has_websocket = lambda: True
    mod.TARGET = "BBBbbb444555666"
    mod.log = lambda *a, **k: None
    seen = _capture_ws_url(mod)
    with contextlib.redirect_stdout(io.StringIO()):
        mod.cmd_title([])
    assert seen.get("ws_url") == "ws://x/B", \
        "cmd_title must use the pinned tab's ws_url, got {!r}".format(seen.get("ws_url"))
```

Then append the cdp_js-family two-tab e2e to `tests/test_e2e.py` (this also defines the
shared `_open_lane` helper that Task 4 reuses; `_read_jpeg_dimensions` is already
defined at module scope in `test_e2e.py`):

```python
# ── sub-C: --target tab pinning ──


def _open_lane(token, test_server):
    """Open test-page.html?<token> in a new tab; return its 12-char id from `tabs`."""
    url = "http://localhost:{}/test-page.html?{}".format(test_server, token)
    r = run_cdp(["open", url])
    assert r.returncode == 0, "open failed: {}".format(r.stderr)
    t = run_cdp(["tabs"])
    for line in t.stdout.splitlines():
        if token in line:
            return line.split()[0]  # id[:12] is the first column
    raise AssertionError("opened tab not in `tabs` for {}: {}".format(token, t.stdout))


def test_target_pins_cdp_js_family(jaine_browser, test_server, tmp_path):
    """--target drives ONLY the selected tab for the cdp_js family, value-distinguished:
    js (location.search), fill (input value), wait (--js predicate), title (per-tab
    document.title), html (per-tab marker), screenshot --full-page (per-tab height).
    Per-tab markers are injected with the js command (proven-pinned first)."""
    base = "http://localhost:{}/test-page.html".format(test_server)
    id_a = _open_lane("look=cjA", test_server)
    id_b = _open_lane("look=cjB", test_server)

    # js (cdp_js) honors the id pin
    assert run_cdp(["--target", id_a, "js", "location.search"]).stdout.strip() == "?look=cjA"
    assert run_cdp(["--target", id_b, "js", "location.search"]).stdout.strip() == "?look=cjB"
    # url-substring selector resolves the same tab (end-to-end)
    assert run_cdp(["--target", "look=cjB", "js", "location.search"]).stdout.strip() == "?look=cjB"

    # fill (cdp_js) drives only the pinned tab
    run_cdp(["--target", id_b, "fill", "#test-input", "valB"])
    assert run_cdp(["--target", id_b, "js",
                    "document.getElementById('test-input').value"]).stdout.strip() == "valB"
    assert run_cdp(["--target", id_a, "js",
                    "document.getElementById('test-input').value"]).stdout.strip() == ""

    # wait (cdp_js) polls the pinned tab
    assert run_cdp(["--target", id_b, "wait", "--js",
                    "location.search === '?look=cjB'", "5"]).returncode == 0

    # Inject per-tab markers via the proven-pinned js (A also gets a 4000px pad).
    run_cdp(["--target", id_a, "js",
             "document.title='TTL_A'; document.body.setAttribute('data-lane','LANE_A');"
             " var d=document.createElement('div'); d.style.height='4000px';"
             " document.body.appendChild(d); 'ok'"])
    run_cdp(["--target", id_b, "js",
             "document.title='TTL_B'; document.body.setAttribute('data-lane','LANE_B'); 'ok'"])

    # title (cdp_js) pins
    assert run_cdp(["--target", id_a, "title"]).stdout.strip() == "TTL_A"
    assert run_cdp(["--target", id_b, "title"]).stdout.strip() == "TTL_B"

    # html (cdp_js) pins — each tab's marker present only in its own DOM
    html_a = run_cdp(["--target", id_a, "html"]).stdout
    html_b = run_cdp(["--target", id_b, "html"]).stdout
    assert "LANE_A" in html_a and "LANE_B" not in html_a
    assert "LANE_B" in html_b and "LANE_A" not in html_b

    # screenshot --full-page (cdp_js metrics) pins — A's 4000px pad ⇒ taller capture
    pa, pb = str(tmp_path / "fpA.jpg"), str(tmp_path / "fpB.jpg")
    run_cdp(["--target", id_a, "screenshot", "--full-page", pa])
    run_cdp(["--target", id_b, "screenshot", "--full-page", pb])
    dim_a, dim_b = _read_jpeg_dimensions(pa), _read_jpeg_dimensions(pb)
    assert dim_a and dim_b, "could not read screenshot dims: {} {}".format(dim_a, dim_b)
    assert dim_a[1] > dim_b[1] + 1000, (
        "A's full-page capture (4000px pad) must be much taller than B's — proves "
        "--full-page metrics read the PINNED tab; got A={} B={}".format(dim_a, dim_b))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_cdp.py -k "cdp_js_requires_ws_url or cmd_title_pins" -v`
Expected: both FAIL —
- `test_cdp_js_requires_ws_url`: current `cdp_js` signature is `(expr)`, so `'ws_url' not in inspect.signature(cdp_js).parameters` → the assert fails (signature-based, offline — no browser dependency).
- `test_cmd_title_pins_target_tab`: current `cmd_title` → `cdp_js("document.title")` → bare `get_tab()` → first tab `AAA…` → `ws://x/A`; assert wants `ws://x/B`.

Run: `pytest tests/test_e2e.py -k cdp_js_family -v`
Expected: `test_target_pins_cdp_js_family` FAILS — before threading, every cdp_js-family command (`js`/`fill`/`wait`/`title`/`html`/`screenshot --full-page`) ignores `TARGET` and drives the first/active tab, so the per-tab readbacks (`?look=cjA` vs `?look=cjB`, `TTL_A` vs `TTL_B`, the taller full-page capture) land on the wrong tab. (The fixture auto-launches the headless lane; no manual setup.)

- [ ] **Step 3: Make `ws_url` a required arg of `cdp_js`**

Replace the current `cdp_js`:

```python
def cdp_js(expr):
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
    })
    if r is None:
        return None
    return r.get("result", {}).get("result", {})
```

with:

```python
def cdp_js(expr, ws_url):
    """Evaluate expr in the tab at ws_url. The caller resolves the tab ONCE via
    get_tab(TARGET) and threads ws_url here — cdp_js never re-resolves get_tab()
    (so a --target pin can't drift between a command's measure and follow-up calls)."""
    r = ws_send(ws_url, "Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
    })
    if r is None:
        return None
    return r.get("result", {}).get("result", {})
```

- [ ] **Step 4: Thread `TARGET`/`ws_url` through the indirect callers**

In `cmd_js`, replace the websocket branch:

```python
    if has_websocket():
        result = cdp_js(expr)
        if result is None:
            return 1
```

with:

```python
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js(expr, tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
```

In `cmd_title`, replace the websocket branch:

```python
    if has_websocket():
        result = cdp_js("document.title")
        if result is None:
            return 1
        print(result.get("value", "?"))
```

with:

```python
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js("document.title", tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        print(result.get("value", "?"))
```

In `cmd_html`, replace the websocket branch:

```python
    if has_websocket():
        result = cdp_js("document.documentElement.outerHTML")
        if result is None:
            return 1
        print(result.get("value", ""))
```

with:

```python
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js("document.documentElement.outerHTML", tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        print(result.get("value", ""))
```

In `cmd_wait`, resolve the tab ONCE before the polling loop. Replace:

```python
    start = time.time()
    while time.time() - start < timeout:
        if has_websocket():
            r = cdp_js(expr)
            if r is None:
                return 1
            found = r.get("value") is True
```

with:

```python
    ws_url = get_tab(TARGET)["webSocketDebuggerUrl"] if has_websocket() else None
    start = time.time()
    while time.time() - start < timeout:
        if has_websocket():
            r = cdp_js(expr, ws_url)
            if r is None:
                return 1
            found = r.get("value") is True
```

In `cmd_fill`, replace the websocket branch:

```python
    if has_websocket():
        result = cdp_js(expr)
        if result is None:
            return 1
        val = result.get("value", "?")
```

with:

```python
    if has_websocket():
        tab = get_tab(TARGET)
        result = cdp_js(expr, tab["webSocketDebuggerUrl"])
        if result is None:
            return 1
        val = result.get("value", "?")
```

In `cmd_screenshot`, the tab is already resolved once (`tab = get_tab()`) and its `ws_url` reused. Thread `TARGET` into that resolution and feed the captured `ws_url` to the `--full-page` metrics `cdp_js`. Replace:

```python
    if has_websocket():
        tab = get_tab()
        ws_url = tab["webSocketDebuggerUrl"]
        params = {"format": "jpeg", "quality": 80}

        if clip:
            params["clip"] = clip
        elif full_page:
            metrics = cdp_js("JSON.stringify({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})")
```

with:

```python
    if has_websocket():
        tab = get_tab(TARGET)
        ws_url = tab["webSocketDebuggerUrl"]
        params = {"format": "jpeg", "quality": 80}

        if clip:
            params["clip"] = clip
        elif full_page:
            metrics = cdp_js("JSON.stringify({w: document.documentElement.scrollWidth, h: document.documentElement.scrollHeight})", ws_url)
```

- [ ] **Step 5: Run the pin tests + offline suite + a slice of e2e to verify GREEN**

Run: `pytest tests/test_cdp.py -k "cdp_js_requires_ws_url or cmd_title_pins" -v`
Expected: PASS (2 passed).

Run: `pytest tests/test_cdp.py -v`
Expected: PASS (modulo the 2 pre-existing marketplace tests). In particular `test_scale_reads_devicepixelratio_via_same_ws_connection` stays green — the `--full-page` `cdp_js` call is *before* the `scale_override is not None` block, so the `--scale` section the test inspects still contains no `cdp_js`.

Run: `pytest tests/test_e2e.py -k "cdp_js_family or title or html or js or fill or wait or screenshot" -v`
Expected: PASS — `test_target_pins_cdp_js_family` now drives the pinned tab for every cdp_js-family command (js/fill/wait/title/html/full-page); the existing single-tab tests stay green (no `--target` → `TARGET=None` → first/active tab = today).

- [ ] **Step 6: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py tests/test_e2e.py
git commit -m "feat(look): cdp_js requires explicit ws_url; thread --target through indirect callers + cdp_js-family e2e (sub-C)"
```

---

## Task 4: thread the direct callers + AST structural enforcement + two-tab e2e

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`cmd_navigate`/`reload`/`click`/`console`/`network`/`pdf`/`viewport`/`window`)
- Test: `tests/test_cdp.py` (structural + a direct-caller pin unit), `tests/test_e2e.py` (two-tab acceptance)

- [ ] **Step 1: Write the failing structural test + direct-caller pin unit**

Append to `tests/test_cdp.py`:

```python
def test_no_unpinned_tab_resolution():
    """C-acceptance: every get_tab( CALL passes the global TARGET selector, and every
    cdp_js( CALL threads an explicit ws_url — no cmd_* resolves a tab without the pin.
    Arity alone is too weak: get_tab(None) or get_tab(<literal>) would satisfy a count
    check yet still drift to the wrong tab (R1-F1), so get_tab must be called with the
    Name `TARGET`. AST-based (not text) so a comment can't self-defeat it."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "get_tab":
                pinned = (len(node.args) >= 1
                          and isinstance(node.args[0], ast.Name)
                          and node.args[0].id == "TARGET")
                if not pinned:
                    offenders.append("get_tab() not threaded with TARGET @L{}".format(node.lineno))
            if node.func.id == "cdp_js" and (len(node.args) + len(node.keywords)) < 2:
                offenders.append("cdp_js(...) missing explicit ws_url @L{}".format(node.lineno))
    assert not offenders, "un-pinned tab resolution (selector not threaded): {}".format(offenders)


def test_cmd_navigate_pins_target_tab():
    """cmd_navigate (a direct get_tab caller) must drive the PINNED tab."""
    import io, contextlib
    mod = _load_cdp_module()
    _stub_tabs(mod, TWO_TABS)
    mod.has_websocket = lambda: True
    mod.TARGET = "BBBbbb444555666"
    mod.log = lambda *a, **k: None
    seen = _capture_ws_url(mod)
    with contextlib.redirect_stdout(io.StringIO()):
        mod.cmd_navigate(["http://localhost/x"])
    assert seen.get("ws_url") == "ws://x/B", \
        "cmd_navigate must use the pinned tab's ws_url, got {!r}".format(seen.get("ws_url"))
```

- [ ] **Step 2: Write the direct-caller e2e acceptance**

Append to `tests/test_e2e.py` (the `_open_lane` helper and the `# ── sub-C` section
header were added in Task 3 — reuse `_open_lane`, do NOT redefine it):

```python
def test_target_pins_direct_commands(jaine_browser, test_server):
    """--target drives ONLY the selected tab for representative DIRECT get_tab
    side-effect commands (navigate, click, network); the sibling tab is untouched and
    a bare call (no --target) is unchanged. The remaining direct callers
    (reload/console/pdf/viewport/window) share the identical get_tab(TARGET) pattern,
    guaranteed by test_no_unpinned_tab_resolution. (cdp_js-family + selector-form
    coverage lives in test_target_pins_cdp_js_family.)"""
    base = "http://localhost:{}/test-page.html".format(test_server)
    id_a = _open_lane("look=dirA", test_server)
    id_b = _open_lane("look=dirB", test_server)

    # direct (navigate) drives only the pinned tab; id is stable across navigation
    run_cdp(["--target", id_a, "navigate", base + "?look=dirA2"])
    run_cdp(["--target", id_a, "wait", "--js", "location.search === '?look=dirA2'", "5"])
    assert run_cdp(["--target", id_a, "js", "location.search"]).stdout.strip() == "?look=dirA2"
    assert run_cdp(["--target", id_b, "js", "location.search"]).stdout.strip() == "?look=dirB"

    # direct (click) drives only the pinned tab
    run_cdp(["--target", id_b, "click", "#test-btn"])
    assert "true" in run_cdp(["--target", id_b, "js",
                              "document.getElementById('test-btn').dataset.clicked"]).stdout
    a_clicked = run_cdp(["--target", id_a, "js",
                         "String(document.getElementById('test-btn').dataset.clicked)"]).stdout.strip()
    assert a_clicked in ("undefined", "null", ""), \
        "tab A's button must NOT be clicked, got {!r}".format(a_clicked)

    # direct (network) reloads + captures the PINNED tab → its own URL is in the log
    net_b = run_cdp(["--target", id_b, "network"])
    assert net_b.returncode == 0 and "look=dirB" in net_b.stdout, (
        "network must reload+capture the pinned tab B (its ?look=dirB request), "
        "got rc={} out={!r}".format(net_b.returncode, net_b.stdout))

    # no --target is unchanged (drives some tab, rc 0)
    assert run_cdp(["js", "location.search"]).returncode == 0


def test_target_ambiguous_and_miss_fail_loud(jaine_browser, test_server):
    """An ambiguous url selector and an unknown selector both fail loud (non-zero)."""
    base = "http://localhost:{}/test-page.html".format(test_server)
    run_cdp(["open", base + "?dup=AMB"])
    run_cdp(["open", base + "?dup=AMB"])  # two tabs share the token → ambiguous

    amb = run_cdp(["--target", "dup=AMB", "js", "1"])
    assert amb.returncode != 0, "ambiguous url selector must fail loud, got rc=0"
    assert "ambiguous" in amb.stderr.lower() or "match" in amb.stderr.lower()

    miss = run_cdp(["--target", "zzz-no-such-tab", "js", "1"])
    assert miss.returncode != 0, "unknown selector must fail loud"
    assert "no tab" in miss.stderr.lower() or "matched no" in miss.stderr.lower()
```

- [ ] **Step 3: Run the new tests to verify they fail (visible RED)**

Run: `pytest tests/test_cdp.py -k "no_unpinned or navigate_pins" -v`
Expected: both FAIL —
- `test_no_unpinned_tab_resolution`: Task 3 fixed the `cdp_js` callers, but the 8 direct `get_tab()` calls (`navigate`/`reload`/`click`/`console`/`network`/`pdf`/`viewport`/`window`) are still bare → `offenders` non-empty.
- `test_cmd_navigate_pins_target_tab`: current `cmd_navigate` → bare `get_tab()` → first tab `AAA…` → `ws://x/A`; assert wants `ws://x/B`.

Run: `pytest tests/test_e2e.py -k "target_pins_direct or ambiguous_and_miss" -v`
Expected: `test_target_pins_direct_commands` FAILS at the navigate readback — `--target id_a navigate …?look=dirA2` is NOT pinned (direct `get_tab()` ignores `TARGET`), so it navigates the first/active tab and `--target id_a js location.search` won't read `?look=dirA2`. `test_target_ambiguous_and_miss_fail_loud` already passes (the gate + resolver from Tasks 1-2 reject those before any command). (`test_target_pins_cdp_js_family` from Task 3 stays green — its commands were threaded in Task 3.)

- [ ] **Step 4: Thread `TARGET` through the 8 direct callers**

Each is a one-line change: `tab = get_tab()` → `tab = get_tab(TARGET)`. Apply in every function below.

In `cmd_navigate`:

```python
    if has_websocket():
        tab = get_tab()
        if ws_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": url}) is None:
```
→
```python
    if has_websocket():
        tab = get_tab(TARGET)
        if ws_send(tab["webSocketDebuggerUrl"], "Page.navigate", {"url": url}) is None:
```

In `cmd_reload`:

```python
    if has_websocket():
        tab = get_tab()
        if ws_send(tab["webSocketDebuggerUrl"], "Page.reload", {"ignoreCache": True}) is None:
```
→
```python
    if has_websocket():
        tab = get_tab(TARGET)
        if ws_send(tab["webSocketDebuggerUrl"], "Page.reload", {"ignoreCache": True}) is None:
```

In `cmd_click` (the comment above it already explains the same-`ws_url` capture; only the resolution gains `TARGET`):

```python
    tab = get_tab()
    ws_url = tab["webSocketDebuggerUrl"]
```
→
```python
    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]
```

In `cmd_console`:

```python
    tab = get_tab()
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Console.enable"}))
```
→
```python
    tab = get_tab(TARGET)
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Console.enable"}))
```

In `cmd_network`:

```python
    tab = get_tab()
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
```
→
```python
    tab = get_tab(TARGET)
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
```

In `cmd_pdf`:

```python
    path = args[0] if args else "/tmp/jaine-page.pdf"
    tab = get_tab()
    r = ws_send(tab["webSocketDebuggerUrl"], "Page.printToPDF", {
```
→
```python
    path = args[0] if args else "/tmp/jaine-page.pdf"
    tab = get_tab(TARGET)
    r = ws_send(tab["webSocketDebuggerUrl"], "Page.printToPDF", {
```

In `cmd_viewport`:

```python
    if has_websocket():
        tab = get_tab()
        if ws_send(tab["webSocketDebuggerUrl"], "Emulation.setDeviceMetricsOverride", {
```
→
```python
    if has_websocket():
        tab = get_tab(TARGET)
        if ws_send(tab["webSocketDebuggerUrl"], "Emulation.setDeviceMetricsOverride", {
```

In `cmd_window` (the `bounds` action's CDP branch):

```python
        if has_websocket():
            tab = get_tab()
            r = ws_send(tab["webSocketDebuggerUrl"], "Browser.getWindowForTarget",
                        {"targetId": tab["id"]})
```
→
```python
        if has_websocket():
            tab = get_tab(TARGET)
            r = ws_send(tab["webSocketDebuggerUrl"], "Browser.getWindowForTarget",
                        {"targetId": tab["id"]})
```

- [ ] **Step 5: Run structural + unit + e2e + full offline to verify GREEN**

Run: `pytest tests/test_cdp.py -k "no_unpinned or navigate_pins" -v`
Expected: PASS (2 passed) — `offenders` is now empty; `cmd_navigate` uses `ws://x/B`.

Run: `pytest tests/test_e2e.py -k target -v`
Expected: PASS (3 passed) — `test_target_pins_cdp_js_family` (Task 3) + `test_target_pins_direct_commands` (navigate/click pin) + `test_target_ambiguous_and_miss_fail_loud` (fail loud).

Run: `pytest tests/test_e2e.py -v`
Expected: PASS — every pre-existing e2e still green (single-tab paths unchanged).

Run: `pytest tests/ -v --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py`
Expected: PASS (modulo the 2 pre-existing marketplace `test_*_cache_path_uses_correct_marketplace` failures, identical at baseline).

- [ ] **Step 6: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py tests/test_e2e.py
git commit -m "feat(look): thread --target through direct callers + structural pin guard + two-tab e2e (sub-C, #153)"
```

---

## Task 5: SKILL.md — document the `--target` flag

**Files:**
- Modify: `skills/look/SKILL.md`

No test (docs). Edit by grep anchor (not line number — CLAUDE.md), reading the surrounding lines first to keep table columns and code fences aligned.

- [ ] **Step 1: Quick Reference — add the global flag**

Grep anchor: `# Status & tabs`. Immediately **above** that comment line (just under the `CDP="…cdp.py"` line and its blank line), insert:

```
# Global flag — pin every command in the call to one tab (CDP/websocket only):
#   python3 "$CDP" --target SEL CMD ...   SEL = full target id, its 12-char prefix
#   (as shown by `tabs`/`status`), or a url substring. Ambiguous/unknown → fail loud.
```

- [ ] **Step 2: Fix the "Active tab" drift note — `--target` is the fix**

Grep anchor: `**Active tab:** all commands act on the browser's active tab.` Replace that whole paragraph:

```
**Active tab:** all commands act on the browser's active tab. If it has drifted (another task navigated it), issue an explicit `navigate` (or select the tab) before `click`/`js` so you operate on the page you think you do.
```

with:

```
**Active tab / tab pinning:** by default all commands act on the browser's first/active tab. With two or more tabs open in one lane that tab can drift (another task navigated it). To pin a specific tab for the whole call, pass `--target SEL` (a full target id, its 12-char prefix as shown by `tabs`/`status`, or a url substring): `python3 "$CDP" --target <id12> js …`. An ambiguous or unknown selector fails loud (it never silently drives the wrong tab). `--target` needs the CDP/websocket channel — the AppleScript fallback can only reach the active tab.
```

- [ ] **Step 3: Lanes section — cross-link the parallel-tabs workflow (C.3)**

Grep anchor: `**Dry run:**` (in the "Lanes (parallel + headless)" section). Immediately **above** the `**Dry run:**` line, insert:

```
**Parallel tabs in one lane:** `tabs`/`status` print each tab's 12-char id; pass it
(or a url substring) to `--target` to pin commands to one tab without it drifting —
e.g. drive two dashboards in the same lane: `--target <idA> screenshot a.jpg` then
`--target <idB> screenshot b.jpg`.

```

- [ ] **Step 4: Fallback Matrix — note the websocket requirement**

Grep anchor: `## Fallback Matrix`. Read the table (its header + the `| window |` row). Immediately **below** the `| window |` row, add a row documenting the flag (match the existing 4-column `| Command | WebSocket | AppleScript | Native |` shape):

```
| `--target` | WebSocket (required) | — (active tab only) | — |
```

- [ ] **Step 5: Verify + commit**

Run: `grep -n -- "--target" skills/look/SKILL.md`
Expected: at least the Quick-Reference flag block, the Active-tab note, the Lanes parallel-tabs note, and the Fallback-Matrix row all match.

```bash
git add skills/look/SKILL.md
git commit -m "docs(look): document global --target tab-pinning flag (sub-C, #153)"
```

---

## Self-Review

**1. Spec coverage (§6 C.1–C.3 + acceptance):**
- **C.1** global `--target`/`--tab` parsed in `main()` before dispatch; resolution = exact id → unique 12-char prefix → url substring; ambiguous/miss → fail-loud; `None` → first page; `--target` requires websocket → Task 2 (`main` + gate) + Task 1 (resolver). ✓
- **C.2** `get_tab` gains `target=`; `url_filter` subsumed; selector reaches EVERY resolution incl. indirect `cdp_js` (now `cdp_js(expr, ws_url)`, resolved once per command); "no command may re-resolve `get_tab()` without the selector" → Tasks 1/3/4; the #140 same-`ws_url` pattern preserved (screenshot/click resolve once, reuse). ✓
- **C.3** discoverability — `tabs`/`status`/`open` already print `id[:12]` (a copy-pasteable prefix accepted by C.1); SKILL.md documents the parallel-tabs workflow → Task 5 Steps 1/3. ✓
- **C — acceptance:** two tabs, `--target` drives the `cdp_js` family (`js`/`fill`/`wait`/`title`/`html`/`screenshot --full-page` all e2e-verified via per-tab markers — `test_target_pins_cdp_js_family`, Task 3) AND direct (`navigate`/`click` e2e-verified — `test_target_pins_direct_commands`, Task 4; `reload`/`console`/`network`/`pdf`/`viewport`/`window` threaded identically, structurally guaranteed) → Task 3+4 e2e + the structural test guaranteeing ALL sites; 12-char id-prefix accepted (Task 1 unit + e2e); url-substring works (Task 1 unit + e2e); `--target` without websocket fails loud (Task 2 unit); no-`--target` unchanged (backward-compat locks + e2e); bad/ambiguous selector errors loudly (Task 1 unit + Task 4 e2e). The **structural test** (`test_no_unpinned_tab_resolution`, AST) is the durable enforcement: a future `cmd_*` with a bare `get_tab()`/`cdp_js(expr)` fails the build. ✓

**2. Placeholder scan:** none — every code step shows complete before/after code; every run step shows the exact command + expected pass/fail with the reason.

**3. Type/name consistency:** `TARGET` (module global, set in `main`, read as `get_tab(TARGET)` at all 14 sites, set as `mod.TARGET` in unit tests); `get_tab(target=None)` (Task 1; called positionally everywhere — the sub-B stubs `lambda url_filter=None:` still accept it positionally, no breakage); `cdp_js(expr, ws_url)` (Task 3; required 2nd arg, asserted by `test_cdp_js_requires_ws_url` and the structural test); `main(argv)` returns an int rc (Task 2; `__main__` does `sys.exit(main(...))`); `_load_cdp_module`/`_stub_tabs`/`_capture_ws_url`/`_expect_systemexit`/`_open_lane` (test helpers, defined once, reused). The selector contract strings (`ambiguous`, `matched no tab`) match what the e2e asserts on stderr. ✓

---

## Next step (project flow, before execution)

Per the /look-v2 workflow (matches sub-A/sub-B): run **`bulldozer:check`** on THIS plan first (catch ordering/logic gaps before code), then execute under **strict TDD with visible RED**, then **`/code-review`**, then open a **PR** (do NOT merge until Chris reviews; PR base is the orphan `bulldozer/main` — "Closes #153" won't auto-close, close manually; auto-calver bumps `plugin.json` on merge — never bump manually).
