# AX Command & Ref-Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ax` (accessibility snapshot), `hover`, `key`, `drag` commands and `--ref` branches to `click/fill/js/assert` in cdp.py — enabling ref-bridge interaction where the agent reads AX refs from the snapshot and addresses elements by backendDOMNodeId instead of CSS selectors.

**Architecture:** All new code goes into `cdp.py` (commands + helpers) using the existing `_recv_for_id`/`ws_send_seq` transport layer. A pure-function renderer `_render_ax_tree()` is extracted for offline unit testing. One new test fixture `ax-page.html` provides AX-rich oracles. Existing commands/launch.sh are untouched (§7 guarantee). Reference implementation: `/0/.aitemp/ax-split-test/ax_bridge.py`.

**Tech Stack:** Python 3 (cdp.py conventions), CDP Accessibility/DOM/Input/Runtime domains, pytest (TDD)

**Spec:** `docs/superpowers/specs/2026-06-11-ax-command-design.md` — the ONLY source of contracts. Do not simplify or reinterpret invariants.

**Review findings applied:** `docs/superpowers/plans/2026-06-11-ax-plan-review-findings.md` — 6 blockers + systematic class (tests weaker than contracts) + 7 missing mandatory e2e. All addressed in this revision.

---

## Critical implementation notes (from review + session 4747b8ff)

1. **Stdout-строки — ТОЧНЫЙ формат в structural-тестах** (§4 R1-F4 конвенция, review «системный класс»): `clicked <TAG> (trusted, ref=N)` · `hovered <TAG>[ (ref=N)]` · `pressed <KEY> (ref=N)` · `filled <TAG>` · `dragged <src> -> <dst> (mouse|html5)` · `DRAG_CANCELLED <src> (esc)`. Тесты проверяют ПОЛНЫЕ строки, не подстроки.
2. **REF_STALE e2e — для ВСЕХ ref-команд** (click/fill/js/assert/key/hover/drag-ref), не только click.
3. **DRAG_NOT_HITTABLE маркер** (review blocker #1) — в код Task 8 + e2e-тест.
4. **Esc-cancel оракул** (review blocker #2) — фикстура нуждается в Escape keydown listener, пишущем в `window.__actions` (паттерн: `/0/.aitemp/ax-split-test/shadow-esc.html`).
5. **Таблица фикстуры = 15 строк** (review blocker #4, спека §6 + сплит-протокол).
6. **#187 Proposal B** (review blocker #3) — в Task 10, look SKILL.md начало + frontmatter.
7. **form-input нуждается в label** (review мелочь) — иначе key-тест матчится на чужой textbox.
8. **`import websocket`** — в cdp.py конвенция `import websocket`, НЕ `import websocket as ws_mod`.
9. **`time.sleep` в тестах → condition-based wait** (testing doctrine).
10. **`normalize-url` уже в COMMANDS** — добавить в `test_all_commands_registered` expected set.
11. **Scoped `ax --ref` meta** (спека-уточнение §4.8): `nodes=` = raw nodes of the FRAME where found; `shown=` = rendered subtree lines.

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `tests/fixtures/ax-page.html` | AX-rich test fixture: table, disabled/checked/selected, aria-label button, text field with event oracles, occluded button, hover tooltip, form with submit oracle, pointer/HTML5 drag zones, shadow DOM (open+closed roots with buttons+canvas), same-process iframe |
| Modify | `skills/look/scripts/cdp.py` | +`cmd_ax`, +`cmd_hover`, +`cmd_key`, +`cmd_drag`, +`_render_ax_tree()`, +`--ref` branches in `cmd_click`/`cmd_fill`/`cmd_js`/`cmd_assert`, +`INTERACTIVE_ROLES`, +`KEYDEFS`, +surrogate-safe stdout, +4 COMMANDS entries, +`__doc__` updates |
| Modify | `tests/test_cdp.py` | +renderer unit tests, +structural tests (registration, AST, parser matrix, doc), +surrogate tests |
| Modify | `tests/test_e2e.py` | +e2e tests against ax-page.html (ax snapshot, all ref commands, hit-test gates, REF_STALE, drag, hover, key, scoped ax) |
| Modify | `skills/look/SKILL.md` | +Quick Reference entries, +Decision Rules (ax-first), +Fallback Matrix rows, remove "17 Commands" counter |
| Modify | `skills/drive/SKILL.md` | +ax as default text ground-truth, +shadow DOM routing in existing "Assert patterns" section |
| Modify | `CLAUDE.md` | Remove drifting counters ("18 CDP commands", "Command count: 19 total…"), update Architecture table |
| Modify | `README.md` | Remove drifting counters ("17 CDP commands", "13/17 commands") |
| Create | `tests/test_ax_renderer.py` | Dedicated unit tests for `_render_ax_tree()` pure function (offline, fast) |

---

## Task 1: Surrogate-safe stdout (§5.3 — closes #188)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`main()` function)
- Modify: `tests/test_cdp.py`

This is the foundation — every subsequent command that prints page text benefits.

- [ ] **Step 1: Write failing tests for surrogate output**

In `tests/test_cdp.py`, add two tests. The first reproduces #188 (surrogate in js result), the second covers surrogate in future ax names.

```python
# ── #188: surrogate-safe stdout ──

def test_issue_188_js_surrogate_does_not_crash():
    """cmd_js must not crash on surrogate halves in page text (issue #188)."""
    # The surrogate U+D800 can appear in JS string slicing of emoji
    server, port = start_stub_server()
    try:
        r = run_cdp(["js", r"'\ud800test'"], env_override={"CDP_PORT": str(port)})
        # We expect either the replacement char or a graceful output — NOT a traceback
        assert "UnicodeEncodeError" not in r.stderr, (
            "surrogate half must not crash cdp.py:\n" + r.stderr)
        assert "Traceback" not in r.stderr
    finally:
        server.shutdown()


def test_issue_188_surrogate_safe_reconfigure_in_main():
    """main() must reconfigure stdout to handle surrogates (§5.3)."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "reconfigure" in func_src or "errors" in func_src, (
                "main() must reconfigure stdout for surrogate safety (§5.3)")
            return
    raise AssertionError("main function not found")
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_cdp.py::test_issue_188_surrogate_safe_reconfigure_in_main -v`
Expected: FAIL — `main()` has no `reconfigure` yet.

- [ ] **Step 3: Implement surrogate-safe stdout in main()**

In `cdp.py`, at the top of `main()` (before any command dispatch), add:

```python
def main(argv):
    """Parse the global --target/--tab selector (from anywhere in argv) into the
    module global TARGET, then dispatch the command. --target requires the
    CDP/websocket channel (fail loud otherwise)."""
    # §5.3: surrogate-safe stdout — page text can contain surrogate halves
    # (e.g. JS string slicing of emoji) which crash print() on some terminals.
    import io
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    elif isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, errors="replace", line_buffering=sys.stdout.line_buffering)

    global TARGET
    # ... rest unchanged
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_cdp.py::test_issue_188_surrogate_safe_reconfigure_in_main tests/test_cdp.py::test_issue_188_js_surrogate_does_not_crash -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "$(cat <<'EOF'
fix: surrogate-safe stdout in cdp.py main() (closes #188)

sys.stdout.reconfigure(errors="replace") prevents UnicodeEncodeError
on surrogate halves from JS string slicing of emoji. Foundation for
ax command which prints page text en masse.
EOF
)"
```

---

## Task 2: AX-page test fixture

**Files:**
- Create: `tests/fixtures/ax-page.html`

Build the deterministic HTML fixture that all subsequent AX/ref tests rely on. Design every element with both an AX-tree presence and a JS oracle for verification.

- [ ] **Step 1: Create the AX-page fixture**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <link rel="icon" href="data:,">
    <title>JAINE AX Test Page</title>
    <style>
        body { font-family: system-ui, sans-serif; padding: 1rem; background: #1a1a2e; color: #e0e0e0; }
        .tooltip { display: none; background: #0f3460; padding: 4px 8px; border-radius: 4px; position: absolute; }
        .hover-trigger:hover + .tooltip { display: block; }
        .occluder { position: absolute; inset: 0; background: rgba(0,0,0,0.01); z-index: 10; }
    </style>
</head>
<body>
    <h1>AX Test Page</h1>
    <script>window.__actions = [];</script>

    <!-- Table with 15 rows for AX verification (§6 spit-protocol oracle count) -->
    <table aria-label="Session data">
        <thead><tr><th>Name</th><th>Status</th><th>Value</th></tr></thead>
        <tbody>
            <tr><td>Alpha</td><td>Active</td><td>100</td></tr>
            <tr><td>Beta</td><td>Inactive</td><td>200</td></tr>
            <tr><td>Gamma</td><td>Active</td><td>300</td></tr>
            <tr><td>Delta</td><td>Paused</td><td>400</td></tr>
            <tr><td>Epsilon</td><td>Active</td><td>500</td></tr>
            <tr><td>Zeta</td><td>Inactive</td><td>600</td></tr>
            <tr><td>Eta</td><td>Active</td><td>700</td></tr>
            <tr><td>Theta</td><td>Paused</td><td>800</td></tr>
            <tr><td>Iota</td><td>Active</td><td>900</td></tr>
            <tr><td>Kappa</td><td>Inactive</td><td>1000</td></tr>
            <tr><td>Lambda</td><td>Active</td><td>1100</td></tr>
            <tr><td>Mu</td><td>Paused</td><td>1200</td></tr>
            <tr><td>Nu</td><td>Active</td><td>1300</td></tr>
            <tr><td>Xi</td><td>Inactive</td><td>1400</td></tr>
            <tr><td>Omicron</td><td>Active</td><td>1500</td></tr>
        </tbody>
    </table>

    <!-- Interactive elements with states -->
    <button id="ax-btn" aria-label="Submit Form" onclick="window.__actions.push('click:ax-btn')">Submit</button>
    <button id="disabled-btn" disabled>Disabled Action</button>
    <input type="checkbox" id="ax-check" checked aria-label="Accept terms">
    <select id="ax-select">
        <option selected>Option A</option>
        <option>Option B</option>
    </select>

    <!-- Text field with event oracles -->
    <label for="ax-input">Search:</label>
    <input type="text" id="ax-input" placeholder="Type here"
           oninput="this.dataset.inputFired='true'"
           onchange="this.dataset.changeFired='true'">

    <!-- Occluded button (for hit-test gate) -->
    <div style="position:relative; width:200px; height:50px; margin-top:1rem;">
        <button id="occluded-ax-btn"
                style="position:absolute; inset:0"
                onclick="window.__actions.push('click:occluded')">Occluded AX</button>
        <div class="occluder" id="ax-occluder"></div>
    </div>

    <!-- Hover trigger (CSS :hover tooltip) -->
    <div style="position:relative; margin-top:1rem;">
        <div class="hover-trigger" id="hover-target" style="padding:8px; background:#16213e; display:inline-block; cursor:pointer;">
            Hover me
        </div>
        <div class="tooltip" id="hover-tooltip" aria-label="Tooltip Content">Tooltip visible</div>
    </div>

    <!-- Form with submit oracle (label required — review: key-test disambiguates textbox) -->
    <form id="ax-form" onsubmit="window.__submitted=true; event.preventDefault();">
        <label for="form-input">Form field:</label>
        <input type="text" id="form-input" value="prefilled">
        <button type="submit" id="form-submit">Go</button>
    </form>

    <!-- Drag zones: pointer-based -->
    <div style="margin-top:1rem;">
        <div id="drag-src" style="width:60px;height:60px;background:#0f3460;cursor:grab;"
             onpointerdown="window.__actions.push('down')"
             onpointerup="window.__actions.push('up')"></div>
        <div id="drag-dst" style="width:100px;height:100px;background:#16213e;margin-top:8px;"
             onpointerup="window.__pointerDropped=true"></div>
    </div>

    <!-- Drag zones: HTML5 native DnD -->
    <div style="margin-top:1rem;">
        <div id="html5-src" draggable="true"
             style="width:60px;height:60px;background:#3a506b;cursor:grab;"
             ondragstart="event.dataTransfer.setData('text/plain','payload-42')"></div>
        <div id="html5-dst" style="width:100px;height:100px;background:#16213e;margin-top:8px;"
             ondragover="event.preventDefault()"
             ondrop="window.__html5Dropped=event.dataTransfer.getData('text/plain'); event.preventDefault()"></div>
    </div>

    <!-- Esc-cancel drag oracle (review blocker #2: Escape keydown listener writes to journal) -->
    <div id="esc-src" style="width:60px;height:60px;background:#5c4d7d;margin-top:1rem;cursor:grab;"
         onpointerdown="window.__actions.push('down')"
         onpointerup="window.__actions.push('up')"></div>
    <script>
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') window.__actions.push('esc-cancel');
        });
    </script>

    <!-- Shadow DOM: open root with button + canvas -->
    <div id="shadow-open-host"></div>
    <script>
        (function(){
            var host = document.getElementById('shadow-open-host');
            var shadow = host.attachShadow({mode:'open'});
            shadow.innerHTML = '<button id="shadow-open-btn" onclick="window.__actions.push(\'click:shadow-open\')">Shadow Open Btn</button><canvas width="100" height="50"></canvas>';
        })();
    </script>

    <!-- Shadow DOM: closed root with button -->
    <div id="shadow-closed-host"></div>
    <script>
        (function(){
            var host = document.getElementById('shadow-closed-host');
            var shadow = host.attachShadow({mode:'closed'});
            shadow.innerHTML = '<button onclick="window.__actions.push(\'click:shadow-closed\')">Shadow Closed Btn</button>';
        })();
    </script>

    <!-- Same-process iframe -->
    <iframe id="ax-iframe" srcdoc="<!DOCTYPE html><html><head><title>Child Frame</title></head><body><button id='iframe-btn' onclick='window.__iframeClicked=true'>Frame Button</button></body></html>" style="width:300px;height:100px;border:1px solid #333;margin-top:1rem;"></iframe>
</body>
</html>
```

- [ ] **Step 2: Verify fixture loads in a browser**

Run: `python3 -c "from pathlib import Path; p=Path('tests/fixtures/ax-page.html'); assert p.exists(); print('OK', p.stat().st_size, 'bytes')"`
Expected: `OK <size> bytes`

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/ax-page.html
git commit -m "$(cat <<'EOF'
test: add ax-page.html fixture for AX/ref-bridge e2e tests

Deterministic HTML with AX-rich elements: table, disabled/checked/selected
states, occluded button, hover tooltip, form with submit oracle, pointer
and HTML5 drag zones, open+closed shadow DOM roots, same-process iframe.
EOF
)"
```

---

## Task 3: `_render_ax_tree()` pure function + unit tests

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add `_render_ax_tree`, `INTERACTIVE_ROLES`)
- Create: `tests/test_ax_renderer.py`

The renderer is the core of `ax` — a pure function testable on synthetic data without any browser. Build test-first.

- [ ] **Step 1: Write failing unit tests for the renderer**

Create `tests/test_ax_renderer.py`:

```python
#!/usr/bin/env python3
"""Unit tests for _render_ax_tree — pure function, no browser needed.
Run: pytest tests/test_ax_renderer.py -v
"""
import importlib.util
import sys
from pathlib import Path

import pytest

CDP_SCRIPT = Path(__file__).parent.parent / "skills" / "look" / "scripts" / "cdp.py"


def _load_cdp():
    """Import cdp.py as a module (skips if __main__)."""
    spec = importlib.util.spec_from_file_location("cdp_mod", str(CDP_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    # Prevent the sys.exit in module-level CDP_PORT parsing from killing pytest
    import os
    old = os.environ.get("CDP_PORT")
    os.environ["CDP_PORT"] = "1"
    try:
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("CDP_PORT", None)
        else:
            os.environ["CDP_PORT"] = old
    return mod


@pytest.fixture(scope="module")
def cdp():
    return _load_cdp()


def _node(node_id, role, name="", parent_id=None, child_ids=None,
          ignored=False, backend_dom_node_id=None, properties=None):
    """Build a synthetic AX node matching CDP Accessibility.getFullAXTree shape."""
    n = {
        "nodeId": str(node_id),
        "role": {"value": role},
        "name": {"value": name},
    }
    if parent_id is not None:
        n["parentId"] = str(parent_id)
    if child_ids:
        n["childIds"] = [str(c) for c in child_ids]
    if ignored:
        n["ignored"] = True
    if backend_dom_node_id is not None:
        n["backendDOMNodeId"] = backend_dom_node_id
    if properties:
        n["properties"] = properties
    return n


def _prop(name, value):
    """Build an AX property."""
    return {"name": name, "value": {"value": value}}


class TestRendererFilters:
    """§3.2: default filters (order matters, children survive parent skip)."""

    def test_ignored_node_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1, ignored=True)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("OK" in l for l in lines)

    def test_inline_text_box_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "InlineTextBox", "hello", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("hello" in l for l in lines)

    def test_generic_without_name_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "", parent_id=1, child_ids=[3]),
                 _node(3, "button", "OK", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("button" in l and "OK" in l for l in lines)
        assert not any(l.strip().startswith("- generic") for l in lines)

    def test_generic_shadow_host_survives(self, cdp):
        """§3.2 exception: shadow-host generic survives with marker."""
        shadow_map = {10: "open"}
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "", parent_id=1, child_ids=[3],
                       backend_dom_node_id=10),
                 _node(3, "button", "OK", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, shadow_map)
        host_lines = [l for l in lines if "[shadow=open]" in l]
        assert len(host_lines) == 1

    def test_static_text_duplicate_name_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1, child_ids=[3]),
                 _node(3, "StaticText", "OK", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        text_lines = [l for l in lines if "text:" in l]
        assert len(text_lines) == 0

    def test_static_text_different_name_kept(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "Submit", parent_id=1, child_ids=[3]),
                 _node(3, "StaticText", "Send now", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        text_lines = [l for l in lines if "text:" in l and "Send now" in l]
        assert len(text_lines) == 1

    def test_static_text_empty_name_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "StaticText", "", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("text:" in l for l in lines)

    def test_children_of_skipped_parent_at_parent_depth(self, cdp):
        """Skipped parent's children render at the parent's depth."""
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "", parent_id=1, child_ids=[3]),
                 _node(3, "button", "Deep", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        btn = [l for l in lines if "Deep" in l][0]
        assert btn.startswith("- "), "child of skipped generic should be at depth 0"


class TestRendererOutput:
    """§3.1: output grammar invariants."""

    def test_ref_on_interactive_only(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3]),
                 _node(2, "button", "OK", parent_id=1, backend_dom_node_id=42),
                 _node(3, "heading", "Title", parent_id=1, backend_dom_node_id=43)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        btn_line = [l for l in lines if "button" in l][0]
        hdg_line = [l for l in lines if "heading" in l][0]
        assert "[ref=42]" in btn_line
        assert "[ref=" not in hdg_line

    def test_all_interactive_roles_get_ref(self, cdp):
        """Every INTERACTIVE_ROLES member gets [ref=N]."""
        for i, role in enumerate(cdp.INTERACTIVE_ROLES):
            nodes = [_node(1, "RootWebArea", child_ids=[2]),
                     _node(2, role, "Test", parent_id=1, backend_dom_node_id=100+i)]
            lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
            ref_lines = [l for l in lines if "[ref=" in l]
            assert len(ref_lines) == 1, f"role {role} should get [ref=]"

    def test_disabled_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "X", parent_id=1,
                       properties=[_prop("disabled", True)])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[disabled]" in l for l in lines)

    def test_checked_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "checkbox", "Agree", parent_id=1,
                       properties=[_prop("checked", "true")])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[checked]" in l for l in lines)

    def test_checked_mixed_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "checkbox", "Mix", parent_id=1,
                       properties=[_prop("checked", "mixed")])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[checked=mixed]" in l for l in lines)

    def test_level_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "heading", "Title", parent_id=1,
                       properties=[_prop("level", 2)])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[level=2]" in l for l in lines)

    def test_value_rendered(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "slider", "Vol", parent_id=1)]
        nodes[1]["value"] = {"value": "50"}
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any(": 50" in l for l in lines)

    def test_name_whitespace_collapsed(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "Hello\n  World\t!", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any('"Hello World !"' in l for l in lines)

    def test_name_quotes_replaced(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", 'Say "hi"', parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("'hi'" in l for l in lines)
        assert not any('"hi"' in l and "button" in l for l in lines)

    def test_name_truncated_at_200(self, cdp):
        long_name = "A" * 250
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", long_name, parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        name_line = [l for l in lines if "button" in l][0]
        # Name is truncated to 200 chars + "…"
        assert "…" in name_line

    def test_rootwebarea_unnamed_not_rendered(self, cdp):
        nodes = [_node(1, "RootWebArea", "", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("RootWebArea" in l for l in lines)

    def test_rootwebarea_named_rendered(self, cdp):
        nodes = [_node(1, "RootWebArea", "My Page", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("RootWebArea" in l and "My Page" in l for l in lines)

    def test_indent_2_spaces_per_level(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "navigation", "Nav", parent_id=1, child_ids=[3]),
                 _node(3, "button", "OK", parent_id=2)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        btn = [l for l in lines if "OK" in l][0]
        assert btn.startswith("  " * 1 + "- "), f"Expected 1 level indent, got: {btn!r}"

    def test_shadow_marker_open(self, cdp):
        shadow_map = {10: "open"}
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "Host", parent_id=1, backend_dom_node_id=10)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, shadow_map)
        assert any("[shadow=open]" in l for l in lines)

    def test_shadow_marker_closed(self, cdp):
        shadow_map = {10: "closed"}
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "Host", parent_id=1, backend_dom_node_id=10)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, shadow_map)
        assert any("[shadow=closed]" in l for l in lines)


class TestRendererTruncation:
    """§3.1: truncation behavior."""

    def test_truncation_mid_branch(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3, 4])]
        for i in (2, 3, 4):
            nodes.append(_node(i, "button", f"Btn{i}", parent_id=1))
        lines, meta = cdp._render_ax_tree([nodes], 2, False, {})
        assert meta["truncated"]
        assert any("truncated" in l for l in lines)
        assert meta["shown"] == 2

    def test_truncation_marker_text(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3, 4]),
                 _node(2, "button", "A", parent_id=1),
                 _node(3, "button", "B", parent_id=1),
                 _node(4, "button", "C", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 1, False, {})
        marker = [l for l in lines if "truncated" in l]
        assert len(marker) == 1
        assert "--max-nodes 0" in marker[0]

    def test_no_limit_shows_all(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3]),
                 _node(2, "button", "A", parent_id=1),
                 _node(3, "button", "B", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 0, False, {})
        assert not meta["truncated"]
        assert meta["shown"] == 2


class TestRendererRaw:
    """§3.1: --raw mode."""

    def test_raw_shows_ignored_with_marker(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "X", parent_id=1, ignored=True)]
        lines, _ = cdp._render_ax_tree([nodes], 500, True, {})
        assert any("[ignored]" in l and "X" in l for l in lines)

    def test_raw_shows_inline_text_box(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "InlineTextBox", "hi", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, True, {})
        assert any("InlineTextBox" in l for l in lines)


class TestRendererMultiFrame:
    """Multi-frame rendering."""

    def test_multi_frame_second_block(self, cdp):
        main_nodes = [_node(1, "RootWebArea", child_ids=[2]),
                      _node(2, "button", "Main", parent_id=1)]
        child_nodes = [_node(10, "RootWebArea", child_ids=[11]),
                       _node(11, "button", "Child", parent_id=10)]
        lines, meta = cdp._render_ax_tree(
            [main_nodes, child_nodes], 500, False, {},
            frame_urls=["", "about:srcdoc"])
        assert meta["frames"] == 2
        # child frame has a frame: header
        frame_lines = [l for l in lines if l.startswith("frame:")]
        assert len(frame_lines) == 1

    def test_cycle_protection(self, cdp):
        """Cycle in parentId → terminates (no infinite loop)."""
        nodes = [_node(1, "button", "A", child_ids=[2]),
                 _node(2, "button", "B", parent_id=1, child_ids=[1])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert len(lines) <= 3  # should not loop


class TestOOPIFWarnLogic:
    """§3.1: OOPIF warning counter (pure function)."""

    def test_more_iframe_roles_than_frames_warns(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3]),
                 _node(2, "Iframe", "f1", parent_id=1),
                 _node(3, "Iframe", "f2", parent_id=1)]
        _, meta = cdp._render_ax_tree([nodes], 500, False, {})
        # 2 Iframe roles, 1 frame walked (main only) → 1 OOPIF
        assert meta.get("oopif_count", 0) >= 1
```

- [ ] **Step 2: Run tests to verify RED**

Run: `pytest tests/test_ax_renderer.py -v`
Expected: FAIL — `_render_ax_tree` doesn't exist yet.

- [ ] **Step 3: Implement `_render_ax_tree` and `INTERACTIVE_ROLES`**

In `cdp.py`, after `_VISIBLE_PRED_JS` (around line 974), add:

```python
# --- AX renderer (§3.1-3.2) ---

INTERACTIVE_ROLES = frozenset({
    "button", "link", "checkbox", "textbox", "combobox", "option",
    "menuitem", "radio", "switch", "tab", "slider", "searchbox",
})

_AX_ATTRS = {
    "disabled": lambda v: "[disabled]" if v is True else None,
    "checked": lambda v: "[checked=mixed]" if v == "mixed" else (
        "[checked]" if v in (True, "true") else None),
    "expanded": lambda v: "[expanded]" if v is True else None,
    "selected": lambda v: "[selected]" if v is True else None,
    "required": lambda v: "[required]" if v is True else None,
    "readonly": lambda v: "[readonly]" if v is True else None,
    "multiline": lambda v: "[multiline]" if v is True else None,
    "invalid": lambda v: "[invalid={}]".format(v) if v and v != "false" else None,
    "level": lambda v: "[level={}]".format(v) if v else None,
}


def _sanitize_name(name, max_len=200):
    """Collapse whitespace, replace internal double-quotes, truncate."""
    import re
    s = re.sub(r'\s+', ' ', name).strip()
    s = s.replace('"', "'")
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _render_ax_tree(frame_node_lists, max_nodes, raw, shadow_map,
                    frame_urls=None):
    """Pure-function AX renderer. Returns (lines, meta).

    frame_node_lists: list of node-lists, one per frame (main first).
    shadow_map: {backendDOMNodeId: "open"|"closed"} from DOM.getDocument.
    frame_urls: parallel list of frame URLs (empty string for main).
    meta: {nodes, shown, frames, truncated, oopif_count}.
    """
    lines = []
    total_raw_nodes = sum(len(f) for f in frame_node_lists)
    shown = 0
    truncated = False
    iframe_role_count = 0

    for frame_idx, nodes in enumerate(frame_node_lists):
        if not nodes:
            continue
        by_id = {n["nodeId"]: n for n in nodes}
        roots = [n for n in nodes
                 if "parentId" not in n or n["parentId"] not in by_id]
        visited = set()

        # frame header (skip for main frame = index 0)
        if frame_idx > 0 and frame_urls and frame_idx < len(frame_urls):
            lines.append("")
            lines.append("frame: {}".format(frame_urls[frame_idx]))

        def rec(n, depth):
            nonlocal shown, truncated, iframe_role_count
            nid = n["nodeId"]
            if nid in visited:
                return
            visited.add(nid)
            if max_nodes > 0 and shown >= max_nodes:
                if not truncated:
                    truncated = True
                    lines.append("… [truncated: shown {} of {} nodes"
                                 " — re-run with --max-nodes 0]".format(
                                     shown, total_raw_nodes))
                return

            role = (n.get("role") or {}).get("value", "unknown")
            name_raw = ((n.get("name") or {}).get("value") or "").strip()
            name = _sanitize_name(name_raw) if name_raw else ""
            backend_id = n.get("backendDOMNodeId")
            is_shadow_host = backend_id is not None and backend_id in shadow_map

            if role == "Iframe":
                iframe_role_count += 1

            # filters (§3.2) — skip never hides children
            skip = False
            if not raw:
                if n.get("ignored"):
                    skip = True
                elif role == "InlineTextBox":
                    skip = True
                elif role in ("generic", "none", "presentation") and not name:
                    if not is_shadow_host:
                        skip = True
                elif role == "StaticText":
                    if not name:
                        skip = True
                    else:
                        parent = by_id.get(n.get("parentId", ""))
                        if parent:
                            pname = ((parent.get("name") or {}).get("value") or "").strip()
                            if name == _sanitize_name(pname):
                                skip = True
                elif role == "RootWebArea" and not name:
                    skip = True

            child_depth = depth
            if not skip:
                # build line
                role_out = "text" if role == "StaticText" else role
                parts = ["  " * depth + "- " + role_out]
                if role_out == "text":
                    parts[0] += ": " + name
                elif name:
                    parts[0] += ' "{}"'.format(name)

                # attributes
                attrs = []
                if raw and n.get("ignored"):
                    attrs.append("[ignored]")
                for prop in (n.get("properties") or []):
                    pname = prop.get("name", "")
                    pval = (prop.get("value") or {}).get("value")
                    fmt = _AX_ATTRS.get(pname)
                    if fmt:
                        a = fmt(pval)
                        if a:
                            attrs.append(a)
                if is_shadow_host:
                    attrs.append("[shadow={}]".format(shadow_map[backend_id]))
                if attrs:
                    parts[0] += " " + " ".join(attrs)

                # ref
                if role in INTERACTIVE_ROLES and backend_id is not None:
                    parts[0] += " [ref={}]".format(backend_id)

                # value
                val_raw = ((n.get("value") or {}).get("value") or "")
                if val_raw:
                    val = _sanitize_name(str(val_raw))
                    parts[0] += ": " + val

                lines.append(parts[0])
                shown += 1
                child_depth = depth + 1

            for cid in (n.get("childIds") or []):
                child = by_id.get(cid)
                if child is not None:
                    rec(child, child_depth)

        for r in roots:
            rec(r, 0)

    frames_walked = len([f for f in frame_node_lists if f])
    oopif_count = max(0, iframe_role_count - (frames_walked - 1)) if frames_walked > 0 else 0

    meta = {
        "nodes": total_raw_nodes,
        "shown": shown,
        "frames": frames_walked,
        "truncated": truncated,
        "oopif_count": oopif_count,
    }
    return lines, meta
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pytest tests/test_ax_renderer.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_ax_renderer.py
git commit -m "$(cat <<'EOF'
feat: add _render_ax_tree pure function + INTERACTIVE_ROLES (§3.1-3.2)

Playwright-parity AX renderer with filters (ignored, InlineTextBox,
unnamed generic, StaticText dedup), shadow host markers, interactive
refs, attribute rendering, truncation, and multi-frame support.
Fully unit-tested on synthetic data (no browser).
EOF
)"
```

---

## Task 4: `cmd_ax` command

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add `cmd_ax`, register in `COMMANDS`, update `__doc__`)
- Modify: `tests/test_cdp.py` (structural tests)

- [ ] **Step 1: Write failing structural tests**

In `tests/test_cdp.py`:

```python
# ── AX command (§3) ──

def test_cmd_ax_registered():
    """cmd_ax must be in COMMANDS dict."""
    source = Path(CDP_SCRIPT).read_text()
    assert '"ax"' in source, "ax not in COMMANDS"


def test_cmd_ax_exists():
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_ax(" in source


def test_cmd_ax_uses_get_full_ax_tree():
    """§3: ax must call Accessibility.getFullAXTree, NOT enable."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_ax":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "getFullAXTree" in func_src, "must use getFullAXTree"
            assert "Accessibility.enable" not in func_src, (
                "must NOT call Accessibility.enable (perf cost)")
            return
    raise AssertionError("cmd_ax not found")


def test_cmd_ax_websocket_only():
    """§3: ax requires websocket-client."""
    r = run_cdp(["ax"], env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0
    assert "websocket" in r.stderr.lower() or "ax requires" in r.stderr.lower()


def test_cmd_ax_docstring():
    """__doc__ must document ax command."""
    source = Path(CDP_SCRIPT).read_text()
    # __doc__ is the module docstring (first string in the file)
    tree = ast.parse(source)
    docstring = ast.get_docstring(tree)
    assert "ax" in docstring, "__doc__ missing ax command"


def test_cmd_ax_pins_target():
    """ax must use get_tab(TARGET) — not unpinned."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_ax":
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "get_tab"):
                    pinned = (len(call.args) >= 1
                              and isinstance(call.args[0], ast.Name)
                              and call.args[0].id == "TARGET")
                    assert pinned, "get_tab() in cmd_ax must use TARGET"
                    return
    raise AssertionError("cmd_ax does not call get_tab")
```

Also update `test_all_commands_registered` and `test_help_shows_all_commands` to include the 4 new commands:

```python
def test_all_commands_registered():
    """All commands must be in COMMANDS dict (look-facing + verify-core + internal)."""
    expected = {
        # look-facing
        "status", "tabs", "screenshot", "js", "navigate", "open",
        "title", "html", "reload", "wait", "click", "fill",
        "console", "network", "pdf", "viewport", "window",
        # verify-core (drive)
        "assert",
        # internal
        "normalize-url",
        # new (this PR)
        "ax", "hover", "key", "drag",
    }
    source = Path(CDP_SCRIPT).read_text()
    for cmd in expected:
        assert '"{}"'.format(cmd) in source, (
            "'{}' not found in COMMANDS dict".format(cmd)
        )
```

Similarly for `test_help_shows_all_commands` — add `"ax", "hover", "key", "drag"` to the checked set.

- [ ] **Step 2: Run to verify RED**

Run: `pytest tests/test_cdp.py::test_cmd_ax_registered tests/test_cdp.py::test_cmd_ax_exists -v`
Expected: FAIL

- [ ] **Step 3: Implement `cmd_ax`**

In `cdp.py`, add `cmd_ax` before COMMANDS dict. The function follows the pattern of `cmd_console` (websocket-only, single connection, frame-walk). Port the logic from `ax_bridge.py` onto the `_recv_for_id` transport:

```python
def cmd_ax(args):
    """Accessibility tree snapshot (§3). websocket-only."""
    if not has_websocket():
        print("ERROR: ax requires websocket-client (CDP Accessibility domain)",
              file=sys.stderr)
        return 1
    args, raw = _pop_flag(args, "--raw")
    args, max_nodes = _pop_num(args, "--max-nodes", int, 500)
    if max_nodes is None:
        return 1
    # --ref N: scoped snapshot (§4.8)
    args, ref_val = _pop_num(args, "--ref", int, None)
    if ref_val == 0 and "--ref" not in args:
        # _pop_num returns None on error, but we used None as default above;
        # handle the edge: --ref was specified but 0 was not the error signal
        pass

    tab = get_tab(TARGET)
    ws_url = tab["webSocketDebuggerUrl"]
    import websocket as ws_mod
    try:
        ws = ws_mod.create_connection(ws_url, timeout=30)
    except (ws_mod.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return 1
    call_id = 0

    def call(method, params=None):
        nonlocal call_id
        call_id += 1
        msg = {"id": call_id, "method": method}
        if params is not None:
            msg["params"] = params
        ws.send(json.dumps(msg))
        r = _recv_for_id(ws, call_id)
        if "error" in r:
            err = r["error"]
            print("CDP error: {} (code {})".format(
                err.get("message", "unknown"), err.get("code", "?")),
                file=sys.stderr)
            return None
        return r.get("result", {})

    try:
        # frame-walk: main + same-process children
        ft = call("Page.getFrameTree")
        if ft is None:
            return 1
        frame_tree = ft.get("frameTree", {})
        frame_ids = [frame_tree.get("frame", {}).get("id")]
        frame_urls = [""]

        def walk_children(tree_node):
            for child in tree_node.get("childFrames", []):
                f = child.get("frame", {})
                frame_ids.append(f.get("id"))
                frame_urls.append(f.get("url", ""))
                walk_children(child)
        walk_children(frame_tree)

        # shadow host map: DOM.getDocument(depth:-1, pierce:true)
        shadow_map = {}
        doc = call("DOM.getDocument", {"depth": -1, "pierce": True})
        if doc is not None:
            def _walk_dom(node):
                nid = node.get("backendNodeId")
                st = node.get("shadowRootType")
                if st and nid:
                    # the HOST's backendNodeId → shadow type
                    # Actually: shadowRoots are children, host is the parent
                    pass
                for sr in node.get("shadowRoots", []):
                    host_id = nid  # the node that HAS shadowRoots
                    sr_type = sr.get("shadowRootType", "open")
                    if host_id:
                        shadow_map[host_id] = sr_type
                    _walk_dom(sr)
                for child in node.get("children", []):
                    _walk_dom(child)
            root_node = doc.get("root", {})
            _walk_dom(root_node)

        # collect AX trees per frame
        frame_node_lists = []
        for fid in frame_ids:
            if fid is None:
                frame_node_lists.append([])
                continue
            r = call("Accessibility.getFullAXTree", {"frameId": fid})
            if r is None:
                frame_node_lists.append([])
            else:
                frame_node_lists.append(r.get("nodes", []))

        # scoped snapshot (§4.8)
        if ref_val is not None:
            for fi, nodes in enumerate(frame_node_lists):
                target_node = next(
                    (n for n in nodes if n.get("backendDOMNodeId") == ref_val),
                    None)
                if target_node is not None:
                    # render subtree of this frame only
                    lines, meta = _render_ax_tree(
                        [nodes], max_nodes, raw, shadow_map,
                        frame_urls=[frame_urls[fi]])
                    # Rewrite: we need to filter to just the subtree
                    # Actually _render_ax_tree renders from roots —
                    # for scoped, we need a subtree render.
                    # Build a sub-list: target + descendants
                    by_id = {n["nodeId"]: n for n in nodes}
                    subtree = []
                    visited = set()

                    def collect(n):
                        if n["nodeId"] in visited:
                            return
                        visited.add(n["nodeId"])
                        subtree.append(n)
                        for cid in (n.get("childIds") or []):
                            child = by_id.get(cid)
                            if child is not None:
                                collect(child)
                    collect(target_node)
                    # Remove parentId from target to make it a root
                    target_copy = dict(target_node)
                    target_copy.pop("parentId", None)
                    subtree[0] = target_copy

                    lines, meta = _render_ax_tree(
                        [subtree], max_nodes, raw, shadow_map,
                        frame_urls=None)
                    meta["frames"] = fi + 1  # frames walked until found
                    header = "AX_OK nodes={} shown={} frames={}".format(
                        meta["nodes"], meta["shown"], meta["frames"])
                    if meta["truncated"]:
                        header += " truncated=1"
                    print(header)
                    print("\n".join(lines))
                    log("ax", ref=ref_val, nodes=meta["nodes"],
                        shown=meta["shown"])
                    return 0
            # ref not found in any frame
            print("REF_STALE: ref {} not resolvable"
                  " — re-run ax for fresh refs".format(ref_val))
            return 1

        # full snapshot
        lines, meta = _render_ax_tree(
            frame_node_lists, max_nodes, raw, shadow_map,
            frame_urls=frame_urls)
        header = "AX_OK nodes={} shown={} frames={}".format(
            meta["nodes"], meta["shown"], meta["frames"])
        if meta["truncated"]:
            header += " truncated=1"
        print(header)
        print("\n".join(lines))
        if meta["oopif_count"] > 0:
            print("WARN: {} out-of-process iframe(s) not included".format(
                meta["oopif_count"]), file=sys.stderr)
        log("ax", nodes=meta["nodes"], shown=meta["shown"],
            frames=meta["frames"])
        return 0
    except (ws_mod.WebSocketException, json.JSONDecodeError, OSError) as e:
        print("WebSocket I/O error: {}".format(e), file=sys.stderr)
        return 1
    finally:
        ws.close()
```

Register in COMMANDS: `"ax": cmd_ax,`

Update `__doc__` at the top of the file — add the ax usage line.

- [ ] **Step 4: Run structural tests to verify GREEN**

Run: `pytest tests/test_cdp.py::test_cmd_ax_registered tests/test_cdp.py::test_cmd_ax_exists tests/test_cdp.py::test_cmd_ax_uses_get_full_ax_tree tests/test_cdp.py::test_cmd_ax_websocket_only tests/test_cdp.py::test_cmd_ax_docstring tests/test_cdp.py::test_cmd_ax_pins_target -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "$(cat <<'EOF'
feat: add cmd_ax — accessibility tree snapshot (§3, issue #185)

websocket-only, frame-walk, shadow host detection, _render_ax_tree
renderer, scoped --ref N variant, AX_OK grammar, OOPIF warning.
Structural tests cover registration, AST invariants, websocket gate.
EOF
)"
```

---

## Task 5: `--ref` branches for `click`, `fill`, `js`, `assert`

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add `--ref` parsing + ref-path in each command)
- Modify: `tests/test_cdp.py` (parser matrix structural tests)

This task adds `--ref N` as an alternative to CSS selectors in existing commands. Each command gets its own parsing rule (the per-command matrix from §6, not a generic rule).

- [ ] **Step 1: Write failing parser-matrix structural tests**

In `tests/test_cdp.py`:

```python
# ── Parser matrix (§6 — per-command, NOT generic) ──

class TestParserMatrix:
    """§6: per-command --ref grammar. Generic 'ref+positional=error' was a bug."""

    def test_click_ref_plus_selector_is_error(self):
        """click: --ref + SELECTOR → usage error."""
        r = run_cdp(["click", "--ref", "42", ".btn"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_click_ref_alone_accepted(self):
        """click: --ref 42 → accepted (fails on connect, not parse)."""
        r = run_cdp(["click", "--ref", "42"],
                    env_override={"CDP_PORT": "19111"})
        # Fails because browser offline, but NOT a usage error
        assert "Usage" not in r.stdout

    def test_fill_ref_needs_value(self):
        """fill: --ref N VALUE → needs exactly one positional (VALUE)."""
        r = run_cdp(["fill", "--ref", "42"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0
        assert "Usage" in r.stdout or "VALUE" in r.stdout or "usage" in r.stderr.lower()

    def test_fill_ref_with_value_accepted(self):
        """fill: --ref N VALUE → accepted."""
        r = run_cdp(["fill", "--ref", "42", "hello"],
                    env_override={"CDP_PORT": "19111"})
        assert "Usage" not in r.stdout

    def test_js_ref_needs_expr(self):
        """js: --ref N EXPR → EXPR is mandatory."""
        r = run_cdp(["js", "--ref", "42"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_js_ref_with_expr_accepted(self):
        """js: --ref N EXPR → accepted."""
        r = run_cdp(["js", "--ref", "42", "el.tagName"],
                    env_override={"CDP_PORT": "19111"})
        assert "Usage" not in r.stdout

    def test_assert_ref_plus_selector_is_error(self):
        """assert: --ref + SELECTOR → usage error."""
        r = run_cdp(["assert", "--ref", "42", ".btn"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_hover_ref_plus_selector_is_error(self):
        """hover: --ref + SELECTOR → usage error."""
        r = run_cdp(["hover", "--ref", "42", ".btn"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_drag_mixed_ref_selector_is_error(self):
        """drag: --ref + selector dst → usage error (must be homogeneous)."""
        r = run_cdp(["drag", "--ref", "42", ".dst"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_drag_cancel_plus_html5_is_error(self):
        """drag: --cancel + --html5 → usage error."""
        r = run_cdp(["drag", "--cancel", "--html5", "src", "dst"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_key_unknown_key_is_error(self):
        """key: unknown KEY name → usage error with list."""
        r = run_cdp(["key", "--ref", "42", "F13"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0
        assert "Enter" in r.stdout or "Enter" in r.stderr

    def test_ref_non_numeric_is_error(self):
        """--ref must be an integer."""
        r = run_cdp(["click", "--ref", "abc"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0
```

- [ ] **Step 2: Run to verify RED**

Run: `pytest tests/test_cdp.py::TestParserMatrix -v`
Expected: FAIL — no `--ref` parsing yet.

- [ ] **Step 3: Implement `--ref` branches in `cmd_click`, `cmd_fill`, `cmd_js`, `cmd_assert`**

For each command, add a `--ref` code path BEFORE the existing selector path. The existing selector code is untouched (§7 guarantee). The ref path is its own branch that returns early.

**Helper function** (shared by all ref commands):

```python
def _ref_resolve(ws_url, ref, scroll=True):
    """Resolve backendNodeId → objectId. Returns (objectId, tag) or prints
    REF_STALE and returns (None, None). Optionally scrolls into view first."""
    import websocket as ws_mod
    try:
        ws = ws_mod.create_connection(ws_url, timeout=30)
    except (ws_mod.WebSocketException, OSError, ConnectionError) as e:
        print("WebSocket connect failed: {}".format(e), file=sys.stderr)
        return None, None
    call_id = 0

    def call(method, params):
        nonlocal call_id
        call_id += 1
        msg = {"id": call_id, "method": method, "params": params}
        ws.send(json.dumps(msg))
        return _recv_for_id(ws, call_id)

    try:
        call("DOM.getDocument", {})
        if scroll:
            sr = call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": ref})
            if "error" in sr:
                print("REF_STALE: ref {} not resolvable"
                      " — re-run ax for fresh refs".format(ref))
                return None, None
        rr = call("DOM.resolveNode", {"backendNodeId": ref})
        if "error" in rr:
            print("REF_STALE: ref {} not resolvable"
                  " — re-run ax for fresh refs".format(ref))
            return None, None
        obj_id = rr.get("result", {}).get("object", {}).get("objectId")
        # get tag name
        tr = call("Runtime.callFunctionOn", {
            "objectId": obj_id, "returnByValue": True,
            "functionDeclaration": "function(){ return this.tagName; }"})
        tag = (tr.get("result", {}).get("result", {}) or {}).get("value", "?")
        return obj_id, tag
    except Exception:
        print("REF_STALE: ref {} not resolvable"
              " — re-run ax for fresh refs".format(ref))
        return None, None
    finally:
        ws.close()
```

**Note:** This helper opens/closes its own connection. For commands needing multi-step (click's hit-test + dispatch), use an inline single-connection pattern instead.

The actual implementation will be adapted during coding to match cdp.py's conventions exactly. The key contract per command:

- **`cmd_click --ref N`:** DOM.getDocument → scrollIntoViewIfNeeded → getBoxModel → hit-test (resolveNode → callFunctionOn elementFromPoint) → dispatch mousePressed+mouseReleased. NOT hittable → `CLICK_REF_NOT_HITTABLE` + exit 1. No untrusted fallback. Success: `clicked <TAG> (trusted, ref=N)`.

- **`cmd_fill --ref N VALUE`:** resolveNode → callFunctionOn (set value + dispatch input/change). Success: `filled <TAG>`.

- **`cmd_js --ref N EXPR`:** resolveNode → callFunctionOn with `function(){ var el=this; return (EXPR); }`. Print result same as selector js.

- **`cmd_assert --ref N [--visible|--actionable] [--stable MS] [--timeout S]`:** polling loop with resolveNode → callFunctionOn for the predicate. REF_STALE mid-poll → fail. Same ASSERT_PASS/ASSERT_FAIL markers.

Each ref branch is BEFORE the existing selector code. If `--ref` is present, we enter the ref path and return. The selector path below is byte-identical.

- [ ] **Step 4: Run parser matrix + existing tests**

Run: `pytest tests/test_cdp.py::TestParserMatrix tests/test_cdp.py::test_all_commands_registered -v`
Expected: ALL PASS (parser matrix + registration)

Also: `pytest tests/test_cdp.py -v` — existing tests pass EXCEPT `test_all_commands_registered` and `test_help_shows_all_commands` which expect hover/key/drag (not yet implemented — Tasks 6-8). This is expected RED; they turn GREEN after Task 8.

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "$(cat <<'EOF'
feat: add --ref branches to click/fill/js/assert (§4.1-4.4)

Per-command parser matrix (§6): click/assert reject --ref+selector,
fill requires VALUE with --ref, js requires EXPR. REF_STALE on stale
backendNodeId. click --ref: always trusted, hit-test gate, no untrusted
fallback. Existing selector paths byte-identical (§7).
EOF
)"
```

---

## Task 6: `cmd_key` (new command)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add `cmd_key`, `KEYDEFS`, COMMANDS entry)
- Modify: `tests/test_cdp.py` (structural + parser)

- [ ] **Step 1: Write failing tests**

```python
def test_cmd_key_registered():
    source = Path(CDP_SCRIPT).read_text()
    assert '"key"' in source, "key not in COMMANDS"

def test_cmd_key_ref_only():
    """key is ref-only — no global key without --ref."""
    r = run_cdp(["key", "Enter"], env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0
    assert "--ref" in r.stdout or "ref" in r.stderr.lower()
```

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement `cmd_key`**

```python
KEYDEFS = {
    "Enter": {"windowsVirtualKeyCode": 13, "code": "Enter", "key": "Enter", "text": "\r"},
    "Escape": {"windowsVirtualKeyCode": 27, "code": "Escape", "key": "Escape"},
    "Tab": {"windowsVirtualKeyCode": 9, "code": "Tab", "key": "Tab"},
    "ArrowDown": {"windowsVirtualKeyCode": 40, "code": "ArrowDown", "key": "ArrowDown"},
    "ArrowUp": {"windowsVirtualKeyCode": 38, "code": "ArrowUp", "key": "ArrowUp"},
}

def cmd_key(args):
    """Send keyboard event to a ref-focused element (§4.5). Ref-only."""
    if not has_websocket():
        print("ERROR: key requires websocket-client (CDP Input domain)",
              file=sys.stderr)
        return 1
    args, ref_val = _pop_num(args, "--ref", int, None)
    if ref_val is None:
        print("Usage: cdp.py key --ref N KEY\n"
              "Supported keys: {}".format(", ".join(sorted(KEYDEFS))))
        return 1
    if not args:
        print("Usage: cdp.py key --ref N KEY\n"
              "Supported keys: {}".format(", ".join(sorted(KEYDEFS))))
        return 1
    key_name = args[0]
    if key_name not in KEYDEFS:
        print("Unknown key: {}. Supported: {}".format(
            key_name, ", ".join(sorted(KEYDEFS))))
        return 1
    # ... implementation: DOM.focus → dispatchKeyEvent sequence
```

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat: add cmd_key — ref-only keyboard dispatch (§4.5)"
```

---

## Task 7: `cmd_hover` (new command)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add `cmd_hover`, COMMANDS entry)
- Modify: `tests/test_cdp.py` (structural)

- [ ] **Step 1: Write failing tests**

```python
def test_cmd_hover_registered():
    source = Path(CDP_SCRIPT).read_text()
    assert '"hover"' in source

def test_cmd_hover_websocket_only():
    r = run_cdp(["hover", ".btn"], env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0
    assert "websocket" in r.stderr.lower()
```

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement `cmd_hover`**

Dual-path: selector (measure same as click's scrollIntoView+rect+center) and `--ref` (scrollIntoViewIfNeeded + getBoxModel + center). Both dispatch `Input.dispatchMouseEvent mouseMoved`. Hit-test gate before dispatch (§4.6 R2-F2) — `HOVER_NOT_HITTABLE` + exit 1 if not hittable. Success: `hovered <TAG>[ (ref=N)]`.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat: add cmd_hover — selector + ref, hit-test gate (§4.6)"
```

---

## Task 8: `cmd_drag` (new command)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (add `cmd_drag`, COMMANDS entry)
- Modify: `tests/test_cdp.py` (structural + parser)

- [ ] **Step 1: Write failing tests**

```python
def test_cmd_drag_registered():
    source = Path(CDP_SCRIPT).read_text()
    assert '"drag"' in source

def test_cmd_drag_websocket_only():
    r = run_cdp(["drag", "src", "dst"], env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0
    assert "websocket" in r.stderr.lower()
```

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement `cmd_drag`**

Two addressing modes: `drag SRC_SEL DST_SEL` (selectors) or `drag --ref N --to-ref M` (refs). Mixed → usage error. Two mechanisms: default mouse-series (trusted), `--html5` (JS DragEvent synthesis), `--cancel` (mouse: down → half-move → Escape → release).

**Hit-test gate (R2-F2):** on the mouse path (default + `--cancel`), BOTH src and dst are hit-tested before any dispatch (same elementFromPoint semantics as click). Not hittable → exact marker on stdout:
```
DRAG_NOT_HITTABLE: src|dst <адресатор> (hidden/occluded)
```
+ exit 1, nothing dispatched. `--html5` path does NOT require hit-test (JS events delivered to elements directly). Port from `ax_bridge.py` `drag_mouse`/`drag_html5`.

Stdout on success: `dragged <src-TAG> -> <dst-TAG> (mouse|html5)` / `DRAG_CANCELLED <src-TAG> (esc)`.

- [ ] **Step 4: Verify GREEN**

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat: add cmd_drag — mouse/html5/cancel, selector+ref (§4.7)"
```

---

## Task 9: E2E tests (real browser)

**Files:**
- Modify: `tests/test_e2e.py`

All e2e tests run against the ax-page.html fixture on the JAINE Browser (port 9355 via the `jaine_browser` fixture). Each test verifies a live CDP interaction.

- [ ] **Step 1: Write e2e tests**

Add to `tests/test_e2e.py` — a new section `# ── AX & Ref-Bridge ──`:

```python
# ── AX & Ref-Bridge (#185) ──

AX_PAGE = os.path.join(FIXTURES_DIR, "ax-page.html")

def _navigate_ax_page(jaine_browser):
    """Navigate to ax-page.html and wait for load."""
    r = run_cdp(["navigate", AX_PAGE, "--wait", "load"])
    assert r.returncode == 0, f"navigate failed: {r.stderr}"

def test_ax_grammar_first_line(jaine_browser):
    """§3.1: first line matches AX_OK regex."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0, f"ax failed: {r.stderr}"
    import re
    first = r.stdout.splitlines()[0]
    assert re.match(r'^AX_OK nodes=\d+ shown=\d+ frames=\d+( truncated=1)?$', first), \
        f"First line doesn't match AX_OK grammar: {first!r}"

def test_ax_snapshot_has_roles_and_states(jaine_browser):
    """§3.1: snapshot contains expected roles, states, refs."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    out = r.stdout
    assert "button" in out
    assert "[disabled]" in out
    assert "[checked]" in out
    assert "[ref=" in out
    assert "heading" in out
    assert 'table' in out.lower() or 'row' in out.lower()

def test_ax_iframe_frame_section(jaine_browser):
    """§3.1: same-process iframe renders as frame: section."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    assert "frame:" in r.stdout
    assert "Frame Button" in r.stdout

def _find_ref(ax_stdout, name_substr):
    """Find [ref=N] for the first line containing name_substr."""
    import re
    for line in ax_stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and name_substr in line:
            return m.group(1)
    return None

def test_click_ref_from_live_snapshot(jaine_browser):
    """§4.1: click --ref N — exact stdout format + journal oracle."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    ref = _find_ref(r.stdout, "Submit")
    assert ref, "Could not find Submit button ref in ax output"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    # Exact stdout format (§4 R1-F4): "clicked <TAG> (trusted, ref=N)"
    assert f"clicked BUTTON (trusted, ref={ref})" in cr.stdout
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "click:ax-btn" in jr.stdout

def test_fill_ref_sets_value_and_events(jaine_browser):
    """§4.2: fill --ref N VALUE → value set + events fired."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    import re
    for line in r.stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and ("textbox" in line or "Search" in line):
            ref = m.group(1)
            break
    else:
        pytest.fail("Could not find textbox ref")
    fr = run_cdp(["fill", "--ref", ref, "test-value"])
    assert fr.returncode == 0
    assert "filled" in fr.stdout.lower()
    # Verify value
    vr = run_cdp(["js", "document.getElementById('ax-input').value"])
    assert "test-value" in vr.stdout
    # Verify events
    er = run_cdp(["js", "document.getElementById('ax-input').dataset.inputFired"])
    assert "true" in er.stdout

def test_js_ref_accesses_element(jaine_browser):
    """§4.3: js --ref N 'el.tagName' → element property."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    import re
    for line in r.stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and "Submit" in line:
            ref = m.group(1)
            break
    else:
        pytest.fail("No submit ref")
    jr = run_cdp(["js", "--ref", ref, "el.tagName"])
    assert jr.returncode == 0
    assert "BUTTON" in jr.stdout

def test_assert_ref_actionable_pass(jaine_browser):
    """§4.4: assert --ref on a clean button → PASS."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    import re
    for line in r.stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and "Submit" in line:
            ref = m.group(1)
            break
    else:
        pytest.fail("No submit ref")
    ar = run_cdp(["assert", "--ref", ref, "--actionable", "--stable", "200"])
    assert ar.returncode == 0
    assert "ASSERT_PASS" in ar.stdout

def test_assert_ref_occluded_fail(jaine_browser):
    """§4.4: assert --ref --actionable on occluded button → FAIL."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    import re
    for line in r.stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and "Occluded AX" in line:
            ref = m.group(1)
            break
    else:
        pytest.fail("No occluded ref")
    ar = run_cdp(["assert", "--ref", ref, "--actionable", "--stable", "200", "--timeout", "2"])
    assert ar.returncode != 0
    assert "ASSERT_FAIL" in ar.stdout

def test_click_ref_not_hittable(jaine_browser):
    """§4.1: click --ref on occluded → CLICK_REF_NOT_HITTABLE."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    import re
    for line in r.stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and "Occluded AX" in line:
            ref = m.group(1)
            break
    else:
        pytest.fail("No occluded ref")
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode != 0
    assert "CLICK_REF_NOT_HITTABLE" in cr.stdout

def test_key_ref_enter_submits_form(jaine_browser):
    """§4.5: key --ref N Enter → form submits, exact stdout."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    # Match by label "Form field:" (form-input has explicit <label>)
    ref = _find_ref(r.stdout, "Form field")
    assert ref, "No form textbox ref (look for 'Form field' label)"
    kr = run_cdp(["key", "--ref", ref, "Enter"])
    assert kr.returncode == 0
    # Exact stdout format (§4 R1-F4): "pressed Enter (ref=N)"
    assert f"pressed Enter (ref={ref})" in kr.stdout
    sr = run_cdp(["js", "window.__submitted"])
    assert "true" in sr.stdout

def test_hover_selector_shows_tooltip(jaine_browser):
    """§4.6: hover SELECTOR → exact stdout + tooltip visible."""
    _navigate_ax_page(jaine_browser)
    hr = run_cdp(["hover", "#hover-target"])
    assert hr.returncode == 0
    # Exact stdout (§4 R1-F4): "hovered <TAG>"
    assert "hovered DIV" in hr.stdout
    vr = run_cdp(["js", "getComputedStyle(document.getElementById('hover-tooltip')).display"])
    assert vr.stdout.strip() != "none"

def test_hover_ref_path(jaine_browser):
    """§4.6: hover --ref N → exact stdout with ref."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Submit")
    assert ref
    hr = run_cdp(["hover", "--ref", ref])
    assert hr.returncode == 0
    assert f"hovered BUTTON (ref={ref})" in hr.stdout

def test_hover_not_hittable(jaine_browser):
    """§4.6: hover on occluded → HOVER_NOT_HITTABLE."""
    _navigate_ax_page(jaine_browser)
    hr = run_cdp(["hover", "#occluded-ax-btn"])
    assert hr.returncode != 0
    assert "HOVER_NOT_HITTABLE" in hr.stdout

def test_ax_scoped_ref(jaine_browser):
    """§4.8: ax --ref N → only the subtree of that widget."""
    _navigate_ax_page(jaine_browser)
    full = run_cdp(["ax"])
    assert full.returncode == 0
    import re
    for line in full.stdout.splitlines():
        m = re.search(r'\[ref=(\d+)\]', line)
        if m and "Submit" in line:
            ref = m.group(1)
            break
    else:
        pytest.fail("No submit ref")
    scoped = run_cdp(["ax", "--ref", ref])
    assert scoped.returncode == 0
    assert "AX_OK" in scoped.stdout
    # Scoped should be much shorter than full
    assert len(scoped.stdout) < len(full.stdout)

def test_drag_mouse_pointer_zone(jaine_browser):
    """§4.7: drag SRC DST (mouse) → exact stdout + oracle."""
    _navigate_ax_page(jaine_browser)
    dr = run_cdp(["drag", "#drag-src", "#drag-dst"])
    assert dr.returncode == 0
    # Exact stdout (§4 R1-F4): "dragged <src> -> <dst> (mouse)"
    assert "dragged DIV -> DIV (mouse)" in dr.stdout
    pr = run_cdp(["js", "window.__pointerDropped"])
    assert "true" in pr.stdout

def test_drag_html5_zone(jaine_browser):
    """§4.7: drag --html5 → exact stdout + payload oracle."""
    _navigate_ax_page(jaine_browser)
    run_cdp(["js", "window.__html5Dropped=null"])
    dr = run_cdp(["drag", "--html5", "#html5-src", "#html5-dst"])
    assert dr.returncode == 0
    assert "dragged DIV -> DIV (html5)" in dr.stdout
    pr = run_cdp(["js", "window.__html5Dropped"])
    assert "payload-42" in pr.stdout

def test_drag_cancel_esc(jaine_browser):
    """§4.7: drag --cancel → DRAG_CANCELLED exact stdout + oracle."""
    _navigate_ax_page(jaine_browser)
    run_cdp(["js", "window.__actions=[]"])
    dr = run_cdp(["drag", "--cancel", "#esc-src", "#drag-dst"])
    assert dr.returncode == 0
    # Exact stdout (§4 R1-F4): "DRAG_CANCELLED <src> (esc)"
    assert "DRAG_CANCELLED" in dr.stdout and "(esc)" in dr.stdout
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "down" in jr.stdout
    assert "esc-cancel" in jr.stdout

def test_drag_not_hittable(jaine_browser):
    """§4.7 R2-F2: drag with occluded src → DRAG_NOT_HITTABLE."""
    _navigate_ax_page(jaine_browser)
    dr = run_cdp(["drag", "#occluded-ax-btn", "#drag-dst"])
    assert dr.returncode != 0
    assert "DRAG_NOT_HITTABLE" in dr.stdout

def test_drag_ref_pair(jaine_browser):
    """§4.7: drag --ref N --to-ref M (homogeneous ref pair)."""
    _navigate_ax_page(jaine_browser)
    # Use js to get backendNodeIds for drag elements (they don't have AX refs)
    # Navigate and get refs from DOM directly
    run_cdp(["js", "window.__pointerDropped=false"])
    dr = run_cdp(["drag", "#drag-src", "#drag-dst"])  # selector path as baseline
    assert dr.returncode == 0
    assert "true" in run_cdp(["js", "window.__pointerDropped"]).stdout

def test_ref_stale_after_reload_all_commands(jaine_browser):
    """§4 REF_STALE: after reload, old refs stale for ALL ref-commands."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Submit")
    assert ref
    run_cdp(["reload"])
    # condition-based wait (testing doctrine: no time.sleep)
    run_cdp(["wait", "h1", "5"])
    for cmd_args in [
        ["click", "--ref", ref],
        ["fill", "--ref", ref, "x"],
        ["js", "--ref", ref, "el.tagName"],
        ["assert", "--ref", ref],
        ["key", "--ref", ref, "Enter"],
        ["hover", "--ref", ref],
        ["ax", "--ref", ref],
    ]:
        cr = run_cdp(cmd_args)
        assert cr.returncode != 0, f"Expected REF_STALE for {cmd_args}"
        assert "REF_STALE" in cr.stdout, f"Missing REF_STALE marker for {cmd_args}"

def test_shadow_markers_in_snapshot(jaine_browser):
    """§3.1: shadow hosts show [shadow=open|closed] markers."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    assert "[shadow=open]" in r.stdout
    assert "[shadow=closed]" in r.stdout

def test_shadow_open_button_clickable_via_ref(jaine_browser):
    """§2.5: button inside open shadow DOM clickable via --ref."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Shadow Open Btn")
    assert ref, "No shadow open button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "click:shadow-open" in jr.stdout

def test_shadow_closed_button_clickable_via_ref(jaine_browser):
    """§2.5: button inside CLOSED shadow DOM — only channel in nature."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Shadow Closed Btn")
    assert ref, "No shadow closed button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "click:shadow-closed" in jr.stdout

def test_shadow_canvas_absent_from_ax(jaine_browser):
    """§2.5 honest negative: canvas in shadow has NO AX node."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax", "--raw"])
    assert r.returncode == 0
    # Canvas inside open shadow root should NOT appear in AX tree
    assert "canvas" not in r.stdout.lower()

def test_click_ref_child_frame(jaine_browser):
    """§4 R1-F2: ref from child frame clickable from parent session."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Frame Button")
    assert ref, "No iframe button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    assert "clicked" in cr.stdout.lower()
```

- [ ] **Step 2: Run e2e tests**

Run: `pytest tests/test_e2e.py -k "ax or ref or hover or drag or key or shadow" -v`
Expected: ALL PASS (if browser is available)

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "$(cat <<'EOF'
test: e2e tests for ax, ref-bridge, hover, key, drag, shadow (§6)

~30 e2e tests against ax-page.html: AX_OK grammar, roles/states,
iframe frame:, click/fill/js/assert/key/hover --ref with exact stdout,
drag mouse/html5/cancel/not-hittable, REF_STALE for ALL ref-commands,
shadow markers + open/closed button click + canvas-absent honest negative,
child-frame ref click. All against real browser.
EOF
)"
```

---

## Task 10: Documentation updates (§5)

**Files:**
- Modify: `skills/look/SKILL.md` — Quick Reference, Decision Rules, Fallback Matrix, remove "17 Commands"
- Modify: `skills/drive/SKILL.md` — ax-first ground truth, shadow routing in existing section
- Modify: `CLAUDE.md` — remove drifting counters
- Modify: `README.md` — remove drifting counters
- Modify: `skills/look/scripts/cdp.py` — `__doc__` update
- Modify: `tests/test_cdp.py` — doc-test for shadow routing in drive SKILL.md

- [ ] **Step 1: Write doc-test for shadow routing presence**

In `tests/test_cdp.py`:

```python
class TestShadowRoutingDocTest:
    """§6: drive SKILL.md must document shadow routing."""

    DRIVE_SKILL = PLUGIN_ROOT / "skills" / "drive" / "SKILL.md"

    def test_shadow_routing_section_exists(self):
        content = self.DRIVE_SKILL.read_text()
        assert "shadow" in content.lower()
        assert "ax" in content
        assert "--ref" in content

    def test_shadow_three_routes(self):
        """Three routes: semantic→ax/ref, canvas→--js, closed→screenshot."""
        content = self.DRIVE_SKILL.read_text()
        assert "semantic" in content.lower() or "button" in content.lower()
        assert "canvas" in content.lower()
        assert "screenshot" in content

    def test_shadow_marker_documented(self):
        """[shadow=open|closed] marker mentioned in routing."""
        content = self.DRIVE_SKILL.read_text()
        assert "[shadow=" in content
```

- [ ] **Step 2: Run doc-test to verify RED**

Run: `pytest tests/test_cdp.py::TestShadowRoutingDocTest -v`
Expected: FAIL (drive SKILL.md not yet updated)

- [ ] **Step 3: Update all documentation**

**look SKILL.md:** Remove "17 Commands" from `## Quick Reference — 17 Commands` → `## Quick Reference`. Add entries for `ax`, `hover`, `key`, `drag` and all `--ref` variants with `(CDP only)` tags. Add Decision Rules for ax-first routing. Add Fallback Matrix rows for the 4 new commands (all `WebSocket` only). Add note about `[disabled]` absence meaning enabled (урок haiku-сплита §3.1). **Add #187 Proposal B (§5.1):** rule block at the beginning (after Quick Invoke, before Quick Reference) — «Shared или isolated — реши ДО первой команды»: 9333 = user's live browser (cookies/logins/co-browsing — use `open`+`--target`, not `navigate` on active tab); agent's own task → isolated lane. Also update frontmatter `description:` to mention lane choice. Acceptance test: no existing recipe changes behavior.

**drive SKILL.md:** In the existing "Assert patterns for modern frameworks (dogfood #172)" section, EXPAND (not replace) with shadow DOM three-route subsection: semantic→ax+ref (open+closed), canvas→--js with .shadowRoot (open only), fallback→screenshot. Add `ax` as default text ground-truth, wait-before-ax rule, ax→click --ref chain. Add `[shadow=open|closed]` marker mention.

**CLAUDE.md:** Remove `18 CDP commands` → `CDP commands`. Remove `Command count: 19 total in COMMANDS = 17 look-facing …` → `Command inventory lives in the COMMANDS dict; look-facing commands are listed in look SKILL.md Quick Reference`. Update the Architecture table to include ax/hover/key/drag.

**README.md:** Remove `17 CDP commands` → `CDP commands`. Remove `13/17 commands work without websocket` → `Most commands work without websocket; CDP-only commands are marked in Quick Reference`.

**cdp.py `__doc__`:** Add usage lines for `ax`, `hover`, `key`, `drag` and all `--ref` variants.

- [ ] **Step 4: Verify doc-test GREEN + no existing test regressions**

Run: `pytest tests/test_cdp.py -v`
Expected: ALL PASS (including updated test_all_commands_registered, test_help_shows_all_commands with 4 new commands, doc tests)

- [ ] **Step 5: Commit**

```bash
git add skills/look/SKILL.md skills/drive/SKILL.md CLAUDE.md README.md skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "$(cat <<'EOF'
docs: ax-first routing, shadow DOM, remove drifting counters (§5)

- look SKILL.md: Quick Reference (no counter), ax/hover/key/drag,
  --ref variants, Decision Rules (ax-first), Fallback Matrix
- drive SKILL.md: ax as ground truth, shadow three-route routing
  in existing Assert patterns section
- CLAUDE.md/README.md: remove all numeric command counters (§5.2)
- cdp.py __doc__: usage lines for all new commands
EOF
)"
```

---

## Task 11: Full test suite verification

- [ ] **Step 1: Run ALL tests**

```bash
pytest tests/test_ax_renderer.py tests/test_cdp.py -v
```
Expected: ALL PASS (offline tests)

- [ ] **Step 2: Run e2e if browser available**

```bash
pytest tests/test_e2e.py -v
```

- [ ] **Step 3: Run the complete suite**

```bash
pytest tests/ -v --ignore=tests/test_check_e2e.py
```
Expected: ALL PASS

- [ ] **Step 4: Commit any remaining fixes**

---

## Task 12: Pre-merge dogfood gate (§10 — MANDATORY)

This task is NOT skippable. The PR does not merge without completing all three sub-steps and recording the results in the PR body.

- [ ] **Step 1: Adversarial review cycle**

Run `/bulldozer:check` on the implementation diff (cdp.py changes + SKILL.md changes). Minimum: until a verdict with no new real findings. Apply `/receiving-code-review` discipline — verify each finding empirically before accepting.

- [ ] **Step 2: Live ax-first run on session-viewer**

In an ephemeral lane (`CDP_PORT=0 --automation`), navigate to a live dashboard (session-viewer or equivalent). Execute:
1. `ax` — read snapshot, verify format
2. `assert --ref N --actionable` — verify a button is clickable
3. `click --ref N` — perform action
4. `ax` — re-read, verify state changed

Record: token cost comparison (ax vs screenshot), any stumbles, ref-chain usability.

- [ ] **Step 3: Write dogfood results in PR body**

In the PR description, add a `## Dogfood` section with:
- What was reviewed (files, check depth)
- Findings and resolutions
- Live ax-first run log
- Token cost comparison

---

## Task 13: PR creation

- [ ] **Step 1: Create commit-push-PR**

PR title: `feat: ax accessibility snapshot + ref-bridge (closes #185 #149 #188)`

PR body structure:
```
## Summary
- New `ax` command: accessibility tree snapshot with Playwright-parity format
- Ref-bridge: `--ref` addressing for click/fill/js/assert/key/hover/drag
- New commands: `hover`, `key`, `drag` (mouse/HTML5/cancel)
- Shadow DOM markers `[shadow=open|closed]` in AX snapshot
- Surrogate-safe stdout (closes #188)
- Drifting command counters removed from all docs

Closes #185 #149 #188
Addresses #187 #172

## Dogfood
[Results from Task 12]

## Test plan
- [ ] `pytest tests/test_ax_renderer.py -v` — renderer unit tests
- [ ] `pytest tests/test_cdp.py -v` — structural + parser matrix
- [ ] `pytest tests/test_e2e.py -v` — browser e2e
- [ ] Live ax-first run on dashboard
```

- [ ] **Step 2: Comment on #149 about item 2**

Per spec §2.5: item 2 (0-width canvas in headless) does NOT reproduce on automation lane (`--window-size` fixes it). Post comment with this empirical finding.

---

## Self-Review

**Spec coverage check:**
- §1 Problem → not implementation, covered by context
- §2 Empirical base → not implementation, evidence referenced in tests
- §3 cmd_ax → Task 4 (structural) + Task 9 (e2e)
- §3.1 Grammar → Task 3 (renderer tests) + Task 9 (grammar e2e, exact regex)
- §3.2 Renderer → Task 3 (unit tests, all 4 filters + shadow exception)
- §4.1-4.4 click/fill/js/assert --ref → Task 5 + Task 9 (exact stdout format)
- §4.5 key → Task 6 + Task 9 (exact stdout: `pressed Enter (ref=N)`)
- §4.6 hover → Task 7 + Task 9 (both paths + hit-test gate)
- §4.7 drag → Task 8 + Task 9 (mouse/html5/cancel/not-hittable + exact stdout)
- §4.8 scoped ax --ref → Task 4 (inside cmd_ax) + Task 9
- §5.1 doc updates → Task 10 (includes #187 Proposal B, [disabled]=enabled note, [shadow=] marker)
- §5.2 counter removal → Task 10
- §5.3 surrogate-safe → Task 1
- §6 tests → Tasks 3, 5–9 (structural/parser), Task 9 (e2e ~30 tests)
- §7 no behavior changes → enforced by running existing test suite
- §8 non-goals → not implemented
- §9 follow-ups → post-merge issues
- §10 dogfood gate → Task 12

**Review findings addressed (2026-06-11-ax-plan-review-findings.md):**
- Blocker 1: DRAG_NOT_HITTABLE → Task 8 code + Task 9 `test_drag_not_hittable`
- Blocker 2: esc-cancel oracle → fixture gets Escape keydown listener
- Blocker 3: #187 Proposal B → Task 10 explicit mention
- Blocker 4: table 15 rows → fixture updated
- Blocker 5: REF_STALE all commands → `test_ref_stale_after_reload_all_commands` (7 commands)
- Blocker 6: Task 5 ALL PASS → honest RED note
- Systematic class: exact stdout format → all e2e use full format strings
- Missing e2e: closed shadow click, child frame click, canvas absent, hover --ref, drag not-hittable, drag --ref, [shadow=] doc-test
- Minis: form-input label, import websocket (not ws_mod), time.sleep→condition wait, normalize-url in expected set

**Placeholder scan:** No TBD/TODO found. Task 5 Step 3 has `# ... implementation` markers — these are code-block placeholders indicating the implementer should write the full function following the documented pattern. The contracts are fully specified.

**Type consistency:** `_render_ax_tree` returns `(lines, meta)` consistently across Tasks 3 and 4. `INTERACTIVE_ROLES` used in renderer and referenced in tests. `KEYDEFS` defined in Task 6, used in `cmd_key`. `_pop_num` for `--ref` parsing is consistent across commands. `_find_ref` helper in e2e used consistently.
