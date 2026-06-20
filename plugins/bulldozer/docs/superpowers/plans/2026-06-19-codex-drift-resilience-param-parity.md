# codex MCP v2 — Drift-Resilience + Param-Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mcp/codex_server.py` resilient to codex upstream protocol drift (detect, degrade, log) and reach feature-parity with the stock codex MCP, without changing any happy-path output.

**Architecture:** Surgical edits to the existing v2 app-server bridge. One new logging primitive (`_drift_warn`) + a per-call accumulator threaded through the request/approval paths; a `_KNOWN_NOTIFICATIONS` allowlist in the turn pump; passive version capture at initialize (log-only); three new `thread/start` passthrough params with an isolation-key scrub. All changes are guarded by `try/except`-best-effort logging and preserve the #18268 reverse-map and `mcp_servers={}` isolation invariants.

**Tech Stack:** Python 3 stdlib only (no deps). pytest + pytest-xdist. Tests in `tests/test_codex_mcp_v2.py`; fake at `tests/fixtures/fake_appserver.py`.

**Spec:** `docs/superpowers/specs/2026-06-19-codex-drift-resilience-param-parity-design.md` (GO via brainstorming + 4 consult-panels + bulldozer:check round 2).

## Global Constraints

- **#18268 invariant:** chosen elicitation LABEL reverse-maps to the EXACT codex decision (string or dict); never the elicitation `action`; never downgrade an amendment dict. No task changes the reverse-map values.
- **Isolation invariant:** the isolated thread never loads user MCP servers. Primary guarantee = launch-level `-c mcp_servers={}` in `_spawn_appserver` (unchanged); per-thread `ISOLATION_CONFIG` always wins any caller-config merge.
- **`_drift_warn` NEVER raises** — all log I/O is best-effort `try/except`; logging never blocks or breaks the bridge.
- **Stable log path** `~/.claude/hooks/bulldozer-codex.log`; env override `BULLDOZER_CODEX_LOG` for test isolation.
- **No happy-path change:** when codex behaves as 0.141, every observable output is byte-identical to today; drift machinery activates only off-nominal.
- **`VERSION_MISMATCH` is log-only** (`acc=None`) — NEVER in the user-facing `_drift`.
- **Python stdlib only.** **TDD:** every task shows the failing test FIRST.
- Run offline tests: `pytest tests/test_codex_mcp_v2.py -m "not slow" -q` from `plugins/bulldozer/`.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `mcp/codex_server.py` | the bridge | all runtime edits |
| `tests/test_codex_mcp_v2.py` | tests | all new tests |
| `tests/fixtures/codex-protocol-fingerprint.json` | curated protocol facts | NEW |
| `tests/fixtures/fake_appserver.py` | fake app-server (subprocess; Reactor/dispatcher tests only) | NO change — the v2 drift tests use the in-process `ExtendedFakeChild` (in the test file), not this subprocess fake |
| `CLAUDE.md` | plugin docs | codex-MCP section update (final task) — path is repo-root-relative; the bulldozer worktree root IS `plugins/bulldozer` |

Task order: A1 (primitive) → A2/A3 (breadcrumbs) → A4 (version) → A5 (acc wiring) → A6 (pump) → A7 (CI) → B1 → B2 → B3 → D1. A5 consumes A1–A4; do A1 first.

> **Numbering note (intentional, not a typo):** the `(A0)`–`(A5)` code in each task's commit message traces that task back to its design-spec **section** (spec §A0–§A5); the plan's `Task A1`–`A7` **headers** are sequential. The two numberings differ on purpose because spec §A1 (observability) splits into Tasks A2 + A3, so every header is offset +1 from its spec-section code: A1→§A0, A2→§A1, A3→§A1, A4→§A2, A5→§A3, A6→§A4, A7→§A5. Copy each commit message verbatim — the code is correct as written.

---

## Task A1: `_drift_warn` + `_stamp_drift` + `_now_iso` (logging primitive)

**Files:**
- Modify: `mcp/codex_server.py` (add helpers near `log()`)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `_drift_warn(acc: list | None, code: str, detail: str) -> None`; `_stamp_drift(result: dict, acc: list) -> dict`; `_now_iso() -> str`.

- [ ] **Step 1: Write the failing test**

```python
def test_drift_warn_appends_to_acc_and_logs(tmp_path, monkeypatch):
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    acc = []
    cs._drift_warn(acc, "UNKNOWN_SERVER_METHOD", "foo/bar")
    assert acc == [{"code": "UNKNOWN_SERVER_METHOD", "detail": "foo/bar"}]
    assert "UNKNOWN_SERVER_METHOD" in logf.read_text()

def test_drift_warn_acc_none_is_log_only(tmp_path, monkeypatch):
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    cs._drift_warn(None, "VERSION_MISMATCH", "x")   # must not raise
    assert "VERSION_MISMATCH" in logf.read_text()

def test_drift_warn_never_raises_on_unwritable(monkeypatch):
    import codex_server as cs
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", "/nonexistent-root/x/y.log")
    cs._drift_warn([], "UNKNOWN_NOTIFICATION", "z")   # swallowed

def test_stamp_drift_attaches_only_when_nonempty():
    import codex_server as cs
    assert cs._stamp_drift({"ok": 1}, []) == {"ok": 1}
    assert cs._stamp_drift({"ok": 1}, [{"code": "X", "detail": "y"}])["_drift"] == [{"code": "X", "detail": "y"}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k drift_warn -q`
Expected: FAIL (`AttributeError: module 'codex_server' has no attribute '_drift_warn'`).

- [ ] **Step 3: Implement (add after `log()` in `mcp/codex_server.py`)**

```python
import datetime

def _now_iso() -> str:
    try:
        return datetime.datetime.now().isoformat(timespec="seconds")
    except Exception:
        return "?"

def _drift_warn(acc, code: str, detail: str) -> None:
    """Record an upstream-drift breadcrumb. NEVER raises.

    acc: per-call list (appended for user-facing _drift) or None (log-only,
    e.g. VERSION_MISMATCH). Always best-effort writes one line to the stable log.
    """
    if acc is not None:
        acc.append({"code": code, "detail": detail})
    try:
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer-codex.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{_now_iso()} | {code} | {detail}\n")
    except Exception:
        pass

def _stamp_drift(result: dict, acc) -> dict:
    """Attach result['_drift'] = acc iff acc is non-empty; return result."""
    if acc:
        result["_drift"] = acc
    return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "drift_warn or stamp_drift" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): _drift_warn/_stamp_drift logging primitive (A0)"
```

---

## Task A2: `handle_server_request` unknown-method breadcrumb

**Files:**
- Modify: `mcp/codex_server.py` (`handle_server_request` signature + unknown-method branch)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_drift_warn` (A1).
- Produces: `handle_server_request(msg, cc_write_fn=None, cc_read_fn=None, timeout=300.0, acc=None)` — new trailing `acc` param.

- [ ] **Step 1: Write the failing test**

```python
def test_unknown_server_method_records_breadcrumb_and_returns_32601():
    import codex_server as cs
    acc = []
    msg = {"id": 7, "method": "item/somethingNew/requestApproval", "params": {}}
    out = cs.handle_server_request(msg, lambda f: None, lambda timeout=10: None, acc=acc)
    assert out["error"]["code"] == -32601
    assert acc and acc[0]["code"] == "UNKNOWN_SERVER_METHOD"
    assert acc[0]["detail"] == "item/somethingNew/requestApproval"

def test_unsupported_method_unchanged_no_breadcrumb():
    import codex_server as cs
    acc = []
    msg = {"id": 1, "method": "item/tool/call", "params": {}}
    out = cs.handle_server_request(msg, lambda f: None, lambda timeout=10: None, acc=acc)
    assert out["error"]["code"] == -32601
    assert acc == []   # _UNSUPPORTED_METHODS is a known, correct -32601 (no drift)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "unknown_server_method or unsupported_method_unchanged" -q`
Expected: FAIL (`handle_server_request() got an unexpected keyword argument 'acc'`).

- [ ] **Step 3: Implement**

In `handle_server_request` change the signature to add `acc=None`, and in the unknown-method branch add the breadcrumb (the `_UNSUPPORTED_METHODS` branch stays untouched):

```python
def handle_server_request(msg: dict, cc_write_fn=None, cc_read_fn=None,
                          timeout: float = 300.0, acc=None) -> dict:
    ...
    if method in _UNSUPPORTED_METHODS:
        return _jsonrpc_lite_error(mid, -32601, f"{method} not supported by this client")

    if method not in _BRIDGED_METHODS:
        _drift_warn(acc, "UNKNOWN_SERVER_METHOD", method)
        return _jsonrpc_lite_error(mid, -32601, f"method not found: {method}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "unknown_server_method or unsupported_method_unchanged" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): UNKNOWN_SERVER_METHOD breadcrumb + acc param (A1)"
```

---

## Task A3: approval-label breadcrumbs + empty-dict guard

**Files:**
- Modify: `mcp/codex_server.py` (`build_command_approval_labels`, `bridge_approval`)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_drift_warn` (A1).
- Produces: `build_command_approval_labels(params: dict, acc=None) -> list`; `bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout=300.0, acc=None)`.

- [ ] **Step 1: Write the failing test**

```python
def test_unknown_decision_kind_breadcrumb_and_verbatim_preserved():
    import codex_server as cs
    acc = []
    future = {"acceptWithSomethingNew": {"x": 1}}
    pairs = cs.build_command_approval_labels({"availableDecisions": ["accept", future]}, acc=acc)
    lm = dict(pairs)
    lbl = [l for l, _ in pairs if l.endswith(":1")][0]
    assert lm[lbl] == future                      # verbatim preserved (#18268)
    assert any(r["code"] == "UNKNOWN_DECISION_VARIANT" for r in acc)

def test_empty_dict_availabledecision_entry_no_stopiteration():
    import codex_server as cs
    acc = []
    # {} passes isinstance(dict) but next(iter({})) would raise StopIteration
    pairs = cs.build_command_approval_labels({"availableDecisions": ["accept", {}]}, acc=acc)
    labels = [l for l, _ in pairs]
    assert "Allow once" in labels                 # the {} entry is skipped, not fatal
    assert any(r["code"] == "UNKNOWN_DECISION_VARIANT" for r in acc)

def test_out_of_enum_label_breadcrumb_via_handle_server_request():
    # R1-F4: exercises the acc-threading chain handle_server_request(acc=) -> bridge_approval(acc=).
    # CC answers with a label NOT in the map -> OUT_OF_ENUM_LABEL breadcrumb + safe "accept" default.
    import codex_server as cs
    acc = []
    cc = FakeCC()
    cc.set_answer("accept", {"label": "TOTALLY-BOGUS-LABEL"})
    msg = {"id": "req-x", "method": "item/commandExecution/requestApproval",
           "params": {"availableDecisions": ["accept", "decline"]}}
    resp = cs.handle_server_request(msg, cc.write, cc.read, acc=acc)
    assert resp["result"]["decision"] == "accept"               # #18268 safe default preserved
    assert any(r["code"] == "OUT_OF_ENUM_LABEL" for r in acc)   # breadcrumb reached the accumulator
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "unknown_decision_kind or empty_dict_availabledecision or out_of_enum_label" -q`
Expected: FAIL (`got an unexpected keyword argument 'acc'`, and `StopIteration` for the empty-dict case).

- [ ] **Step 3: Implement**

`build_command_approval_labels(params, acc=None)`. In the dict branch, guard the empty dict and emit breadcrumbs:

```python
            elif isinstance(entry, dict):
                if not entry:                        # {} → malformed, would StopIteration
                    _drift_warn(acc, "UNKNOWN_DECISION_VARIANT", "empty-dict entry")
                    continue
                kind = next(iter(entry))
                if kind == "acceptWithExecpolicyAmendment":
                    result.append((LBL_EXECPOLICY, entry))
                elif kind == "applyNetworkPolicyAmendment":
                    apnpa = entry.get("applyNetworkPolicyAmendment")
                    apnpa = apnpa if isinstance(apnpa, dict) else {}
                    amend = apnpa.get("network_policy_amendment")
                    amend = amend if isinstance(amend, dict) else {}
                    result.append((_network_label(amend.get("host", ""), amend.get("action", ""), i), entry))
                else:
                    _drift_warn(acc, "UNKNOWN_DECISION_VARIANT", kind)
                    result.append((f"{kind}:{i}", entry))
```

In `bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout=300.0, acc=None)`: pass `acc` to `build_command_approval_labels(params, acc=acc)`, and in the command/file/legacy accept-branches emit `OUT_OF_ENUM_LABEL` when the chosen label is not in the map BEFORE the `.get(...)` default, e.g.:

```python
            chosen = content.get("label", LBL_ALLOW_ONCE)
            if chosen not in label_map:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
            return label_map.get(chosen, "accept")
```

**Also thread `acc` from `handle_server_request` INTO `bridge_approval`** (without this, breadcrumbs fall to `bridge_approval`'s default `acc=None` = log-only and never reach the result `_drift`). `handle_server_request` calls `bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout)` at all **6** bridged-method sites — add `acc=acc` to each. Apply the same `chosen not in <map>` → `OUT_OF_ENUM_LABEL` guard before every `*_map.get(chosen, <default>)` that maps a CC-chosen label (the command/file accept-branch is the one the Step 1 test covers; the legacy/permissions/grant branches map labels too — add the guard there for parity).

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "unknown_decision_kind or empty_dict_availabledecision or out_of_enum_label" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): approval breadcrumbs + empty-dict StopIteration guard (A1/R1-F1)"
```

---

## Task A4: passive version capture (log-only)

**Files:**
- Modify: `mcp/codex_server.py` (`LAST_VERIFIED_CODEX_VERSION`, `_parse_codex_version`, `_do_initialize`, `AppServerManager.__init__`)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_drift_warn` (A1).
- Produces: `LAST_VERIFIED_CODEX_VERSION = "0.141"`; `_parse_codex_version(user_agent: str) -> str | None` (returns `"MAJOR.MINOR"` or None); `AppServerManager._codex_version` attribute.

- [ ] **Step 1: Write the failing test**

```python
import re
def test_parse_codex_version_anchored():
    import codex_server as cs
    assert cs._parse_codex_version("codex/fake-0.141.0") == "0.141"
    assert cs._parse_codex_version("Agent/1.0 Codex/0.141") == "0.141"  # not 1.0
    assert cs._parse_codex_version("no version here") is None

def test_version_mismatch_is_log_only(tmp_path, monkeypatch):
    import codex_server as cs
    logf = tmp_path / "d.log"; monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    # parsed != LAST_VERIFIED → a log line, NOT an exception, NOT a user-facing record
    acc = []
    cs._drift_warn(None if cs._parse_codex_version("codex/0.999.0") != cs.LAST_VERIFIED_CODEX_VERSION else acc,
                   "VERSION_MISMATCH", "live 0.999")
    assert acc == []                       # never user-facing
    assert "VERSION_MISMATCH" in logf.read_text()

def test_do_initialize_captures_codex_version(fake_child):
    # R2-F3: _do_initialize must parse the initialize userAgent into manager._codex_version.
    import codex_server as cs
    m = cs.AppServerManager(bin=fake_child)
    m.ensure()                             # FakeChild initialize emits userAgent "codex/fake-0.141.0"
    assert m._codex_version == "0.141"     # == LAST_VERIFIED_CODEX_VERSION → no VERSION_MISMATCH
```

(`fake_child` is the existing fixture; its `_dispatch` already answers `initialize` with `userAgent: "codex/fake-0.141.0"`.)

(The `_parse_codex_version` regex: `m = re.search(r"[Cc]odex/[^0-9]*(\d+)\.(\d+)", user_agent)` → anchor to a `codex/` token so an unrelated leading number can't win.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "parse_codex_version or version_mismatch_is_log_only or do_initialize_captures" -q`
Expected: FAIL (`no attribute '_parse_codex_version'` / `LAST_VERIFIED_CODEX_VERSION`).

- [ ] **Step 3: Implement**

```python
import re
LAST_VERIFIED_CODEX_VERSION = "0.141"   # last codex app-server version this bridge was verified against

def _parse_codex_version(user_agent: str):
    """Return 'MAJOR.MINOR' parsed from a codex userAgent, or None. Never raises."""
    try:
        m = re.search(r"[Cc]odex/[^0-9]*(\d+)\.(\d+)", user_agent or "")
        return f"{m.group(1)}.{m.group(2)}" if m else None
    except Exception:
        return None
```

In `AppServerManager.__init__`: add `self._codex_version = None`. In `_do_initialize`, after the `resp is None` guard, replace the discard with capture (log-only mismatch):

```python
        if resp is None:
            raise RuntimeError("AppServerManager: initialize response timed out")
        ua = (resp.get("result") or {}).get("userAgent", "") if isinstance(resp, dict) else ""
        self._codex_version = _parse_codex_version(ua)
        if self._codex_version != LAST_VERIFIED_CODEX_VERSION:
            _drift_warn(None, "VERSION_MISMATCH",
                        f"last-verified {LAST_VERIFIED_CODEX_VERSION}, live {ua!r}")
        self._write({"method": "initialized"})
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "parse_codex_version or version_mismatch_is_log_only or do_initialize_captures" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): passive codex version capture, log-only (A2)"
```

---

## Task A5: per-call accumulator wiring in `codex_run_v2`

**Files:**
- Modify: `mcp/codex_server.py` (`codex_run_v2`: create `acc`, thread to `handle_server_request`, `_stamp_drift` ALL returns)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_drift_warn`, `_stamp_drift` (A1), `handle_server_request(acc=)` (A2).
- Produces: every `codex_run_v2` return dict carries `_drift` iff behavioral drift was recorded.

- [ ] **Step 1: Write the failing test.** Use the EXISTING in-process harness already in `tests/test_codex_mcp_v2.py` — `ExtendedFakeChild` + the `ext_child` fixture + the `call_codex_run(fake_child_inst, prompt, ...)` driver (lines ~1121–1252). Do NOT invent a `make_v2_env` helper — it does not exist; `call_codex_run` IS its real equivalent. The in-process v2 tests do NOT use the subprocess `fake_appserver.py`.

```python
def test_codex_run_v2_no_drift_on_happy_path(ext_child):
    res = call_codex_run(ext_child, prompt="hi")     # basic turn: delta + turn/completed
    assert "_drift" not in res                        # happy path = byte-identical, no _drift key

def test_codex_run_v2_stamps_drift_from_unknown_server_request(ext_child):
    # ext_child emits a mid-turn server->client REQUEST with an UNBRIDGED method
    # (fire-and-forget: handle_server_request returns -32601 and records the breadcrumb,
    # which A2's acc threading surfaces and A5 stamps onto the result).
    res = call_codex_run(ext_child, prompt="hi", turn_variant="unknown_server_request")
    assert any(r["code"] == "UNKNOWN_SERVER_METHOD" for r in res.get("_drift", []))
```

The positive test needs a new fake turn-variant + a `turn_variant` param on `call_codex_run` — both added in Step 3 (item 4 below).

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "stamps_drift_from_unknown or no_drift_on_happy" -q`
Expected: FAIL (no `_drift` key on the unknown-request path).

- [ ] **Step 3: Implement** — in `codex_run_v2`:
  1. As the **FIRST statement of the function body** — BEFORE the graceful no-codex check (the current first `return`) — insert `acc = []`. (R1-F5: this MUST precede the no-codex AND prompt-required returns, both of which run before `mode = ...`; placing `acc` after `mode` would make `_stamp_drift(..., acc)` on those two early returns a `NameError`. An empty `acc` stamps to no `_drift` key → happy path stays byte-identical.)
  2. The server-request bridge call becomes: `manager._write(handle_server_request(frame, cc_write_fn, cc_read_fn, acc=acc))`.
  3. Wrap EVERY `return {...}` / `return _shape_result(...)` in `_stamp_drift(<dict>, acc)`. For `_shape_result`, stamp the returned dict **externally**: `return _stamp_drift(_shape_result(mode, thread_id, "".join(final_message_parts)), acc)` — this external stamp is intentionally **equivalent** to the spec's "thread drift through `_shape_result`" (it attaches the SAME `_drift` key to the SAME returned dict), chosen so `_shape_result`'s internals stay untouched. Apply to **ALL** returns **uniformly** — busy, no-codex, resume-fail, unknown-thread, thread/start-fail, turn/start-error, timeout, eof, exception, AND `busy_error()`/`eof_error()`. Because `acc=[]` is the FIRST statement of the function body (item 1), it always exists at every return — there is **no exemption** (e.g. `return _stamp_drift(state_machine.busy_error(), acc)`). An empty `acc` stamps to no `_drift` key → byte-identical.
  4. **Add the `unknown_server_request` fake turn-variant** (test-infra; the Step 1 positive test needs it). Extend the EXISTING in-process fake, NOT `fake_appserver.py`:
     - In `ExtendedFakeChild` (`tests/test_codex_mcp_v2.py`), add `script_turn_variant(name)` (mirrors `script_final_message`) storing `self._turn_variant = name` (default `None`). In `_dispatch`'s `turn/start` branch, when `self._turn_variant == "unknown_server_request"`, after emitting the TurnStartResponse, emit a server→client REQUEST with an UNBRIDGED method, then `turn/completed` (status `completed`) — fire-and-forget, do NOT wait for the bridge's reply:
       ```python
       self._write_msg({"id": "SRVREQ-1", "method": "item/somethingNew/requestApproval",
                        "params": {"threadId": thread_id, "turnId": turn_id}})
       # ...then the usual turn/completed (status="completed") so the pump returns.
       ```
       The pump (A5 item 2) bridges the request via `handle_server_request(..., acc=acc)` → unbridged method → `-32601` + `UNKNOWN_SERVER_METHOD` breadcrumb in `acc`; the fake ignores the `-32601` reply (id+error, no method → parent `_dispatch` no-op).
     - In `call_codex_run`, add a `turn_variant=None` param; when set, call `fake_child_inst.script_turn_variant(turn_variant)` before invoking `codex_run_v2`.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -q` (full offline suite — confirm no regression in existing v2 tests)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): per-call drift accumulator + _stamp_drift all returns (A3)"
```

---

## Task A6: `_KNOWN_NOTIFICATIONS` allowlist + terminal-failure detection

**Files:**
- Modify: `mcp/codex_server.py` (`_KNOWN_NOTIFICATIONS`, `codex_run_v2` Phase 2)
- Modify: `tests/test_codex_mcp_v2.py` (`ExtendedFakeChild` turn-variant branches `failed` + `unknown_notification`; the new tests). NOT `fake_appserver.py` — the subprocess fake is only used by the Reactor/dispatcher tests, never by `call_codex_run`.

**Interfaces:**
- Consumes: `_drift_warn` (A1), per-call `acc` (A5).
- Produces: `_KNOWN_NOTIFICATIONS = frozenset({...})` module constant.

- [ ] **Step 1: Write the failing test**

Same real harness as A5 (`call_codex_run` + `ext_child` + `turn_variant`) — NOT `make_v2_env`:

```python
def test_item_completed_not_flagged_as_drift(ext_child):
    res = call_codex_run(ext_child, prompt="hi")     # basic turn emits item/completed
    assert "_drift" not in res                        # item/completed is KNOWN, not UNKNOWN_NOTIFICATION

def test_unknown_notification_flagged(ext_child):
    res = call_codex_run(ext_child, prompt="hi", turn_variant="unknown_notification")  # fake emits item/bogusEvent
    assert any(r["code"] == "UNKNOWN_NOTIFICATION" for r in res.get("_drift", []))

def test_failed_turn_returns_clean_error(ext_child):
    res = call_codex_run(ext_child, prompt="hi", turn_variant="failed")   # turn/completed status="failed"
    assert "error" in res and "turn failed" in res["error"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "item_completed_not_flagged or unknown_notification_flagged or failed_turn_returns" -q`
Expected: FAIL (item/completed currently silently ignored → no drift either way; failed-turn currently returns a normal shape).

- [ ] **Step 3: Implement**

Add the allowlist constant near `_BRIDGED_METHODS`:

```python
_KNOWN_NOTIFICATIONS = frozenset({
    "item/agentMessage/delta", "item/completed", "turn/completed",
    "turn/started", "item/started",   # benign lifecycle events
})
```

In `codex_run_v2` Phase 2, after the `kind == "request"` block, extend the notification handling:

```python
                if kind == "notification":
                    if method == "item/agentMessage/delta":
                        final_message_parts.append(frame.get("params", {}).get("delta", ""))
                    elif method == "turn/completed":
                        t = frame.get("params", {}).get("turn", {}) or {}
                        if t.get("status") not in ("completed", "success") or t.get("error"):
                            state_machine.turn_completed()
                            return _stamp_drift({
                                "error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                                "thread_id": thread_id}, acc)
                        state_machine.turn_completed()
                        return _stamp_drift(_shape_result(mode, thread_id, "".join(final_message_parts)), acc)
                    elif method not in _KNOWN_NOTIFICATIONS:
                        _drift_warn(acc, "UNKNOWN_NOTIFICATION", method)
                    # known-but-ignored (item/completed, turn/started, ...) → no-op
                    continue
```

Add two more `turn_variant` branches to `ExtendedFakeChild._dispatch`'s `turn/start` handler (same `script_turn_variant` mechanism added in A5 item 4) — NOT to `fake_appserver.py`:
- `"failed"` → emit the TurnStartResponse then `turn/completed` with `turn.status="failed"` (no delta).
- `"unknown_notification"` → emit TurnStartResponse + `item/agentMessage/delta` + a bogus notification `self._write_msg({"method": "item/bogusEvent", "params": {}})` + `turn/completed` status `completed`.

**Also (R2-F1) add an `item/completed` notification to `ExtendedFakeChild`'s BASE happy turn** (between the existing `item/agentMessage/delta` and `turn/completed`), mirroring real codex + the subprocess `_handle_turn_start_basic`. Without it `test_item_completed_not_flagged_as_drift` is VACUOUS — the in-process fake otherwise never emits `item/completed`, so the test "proves" nothing. It's allowlisted (`_KNOWN_NOTIFICATIONS`) → the pump no-ops it → all existing `call_codex_run` tests are unaffected:
```python
self._write_msg({"method": "item/completed", "params": {
    "item": {"id": item_id, "type": "message", "role": "assistant", "content": []},
    "threadId": thread_id, "turnId": turn_id}})
```
(Adding emission + allowlist in the SAME task keeps it consistent: `item/completed` is never seen by the pump before A6 allowlists it.)

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -q`
Expected: PASS (incl. existing tests — happy path still returns normal shape, no `_drift`).

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): _KNOWN_NOTIFICATIONS allowlist + terminal-failure detection (A4/R1-F1)"
```

---

## Task A7: CI fingerprint (coherence tripwire + slow-e2e version)

**Files:**
- Create: `tests/fixtures/codex-protocol-fingerprint.json`
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_BRIDGED_METHODS`, `_UNSUPPORTED_METHODS`, `_is_valid_command_decision`, `LAST_VERIFIED_CODEX_VERSION`.

- [ ] **Step 1: Create the fixture**

```json
{
  "_comment": "Coherence snapshot of the bridge's protocol surface. NOT codex-drift detection — it only fails when code constants are edited without updating this file. Real codex-drift = the slow-e2e live-version assert (test_live_codex_version_matches_pin). Intentionally a SUBSET of the spec's listed fixture keys: only facts with a code constant to compare (bridged/unsupported methods, decision variants, version). turn_start_params is omitted by design — it has no code constant to diff against, so committing it would be dead snapshot data, not a coherence tripwire.",
  "bridged_methods": ["item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval", "item/tool/requestUserInput", "mcpServer/elicitation/request", "execCommandApproval", "applyPatchApproval"],
  "unsupported_methods": ["item/tool/call", "account/chatgptAuthTokens/refresh", "attestation/generate"],
  "command_decision_variants": ["accept", "acceptForSession", "decline", "cancel", {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow foo"]}}, {"applyNetworkPolicyAmendment": {"network_policy_amendment": {"host": "x"}}}],
  "last_verified_codex_version": "0.141"
}
```

- [ ] **Step 2: Write the failing test**

```python
def test_fingerprint_matches_code_constants():
    """Coherence tripwire (NOT drift detection): committed fingerprint == code constants."""
    import json, os, codex_server as cs
    fp = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "codex-protocol-fingerprint.json")))
    assert set(fp["bridged_methods"]) == set(cs._BRIDGED_METHODS)
    assert set(fp["unsupported_methods"]) == set(cs._UNSUPPORTED_METHODS)
    assert fp["last_verified_codex_version"] == cs.LAST_VERIFIED_CODEX_VERSION
    for d in fp["command_decision_variants"]:
        assert cs._is_valid_command_decision(d)   # strings AND dict-shaped amendment variants both valid as-is
        # NOTE (R1-F1): amendment variants are stored as truthy dicts in the fixture, NOT bare
        # strings — `_is_valid_command_decision` accepts acceptWithExecpolicyAmendment /
        # applyNetworkPolicyAmendment ONLY as truthy dict payloads, never as plain strings.

@skip_if_no_codex          # existing module-level marker (line ~37); self-skips without codex
@pytest.mark.slow
def test_live_codex_version_matches_pin():
    """Maintainer ritual: live codex version == pin (else re-verify the bridge and bump
    LAST_VERIFIED_CODEX_VERSION). Reuses A4's passive capture + the existing real-codex
    singleton pattern (mirrors test_e2e_review_real_appserver) — NO new helper/fixture.
    `cft_or_codex` and `_live_codex_user_agent` do NOT exist; do not reference them."""
    import codex_server as cs
    cs.codex_run_v2({"prompt": "ping", "mode": "review"})   # drives ensure()+initialize on the singleton
    assert cs._get_manager()._codex_version == cs.LAST_VERIFIED_CODEX_VERSION
```

- [ ] **Step 3: Run to verify** offline test passes immediately (it's a coherence check, the fixture matches the code):

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k fingerprint_matches -q`
Expected: PASS. (If FAIL → fixture and code diverged; fix the fixture.)

- [ ] **Step 4: Confirm the slow test self-skips without codex**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -q` → the slow test does NOT run.
Expected: PASS, slow test deselected.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/codex-protocol-fingerprint.json tests/test_codex_mcp_v2.py
git commit -m "test(codex-mcp-v2): protocol fingerprint coherence tripwire + slow version e2e (A5)"
```

---

## Task B1: expose `base_instructions` + `developer_instructions`

**Files:**
- Modify: `mcp/codex_server.py` (`TOOLS[0].inputSchema`, `start_thread`, `codex_run_v2` new-thread branch)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `start_thread(..., base_instructions=None, developer_instructions=None, ...)` — None-sentinel for base_instructions; `developer_instructions` maps to wire key `developerInstructions`.

- [ ] **Step 1: Write the failing test**

Do NOT invent a `v2_manager_capture` helper. `FakeChild.received()` returns the FIRST matching
message, so reusing one manager across multiple `start_thread` calls would always read the first
call's params. Use a fresh manager+FakeChild per call (mirrors the existing
`test_start_thread_is_nonephemeral_and_isolated`):

```python
# Helper (define once in this task; Task B2 reuses it). FakeChild/FakeCC are module-level.
def _started_params(**kwargs):
    """Fresh manager+FakeChild, ONE start_thread, return its thread/start params dict."""
    from codex_server import AppServerManager
    fc = FakeChild()
    try:
        m = AppServerManager(bin=fc)
        m.ensure()
        m.start_thread(**kwargs)
        return fc.received("thread/start")["params"]
    finally:
        fc.kill()

def test_start_thread_base_instructions_sentinel():
    import codex_server as cs
    assert _started_params(base_instructions=None)["baseInstructions"] == cs.STERILE_INSTRUCTIONS
    assert _started_params(base_instructions="")["baseInstructions"] == ""   # "" is a valid caller value, not "omitted"

def test_start_thread_developer_instructions_wire_key():
    assert _started_params(developer_instructions="be terse")["developerInstructions"] == "be terse"
    assert "developerInstructions" not in _started_params()                  # omitted → key absent
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "base_instructions_sentinel or developer_instructions_wire_key" -q`
Expected: FAIL (`start_thread() got an unexpected keyword argument 'developer_instructions'`).

- [ ] **Step 3: Implement**

`start_thread` signature: add `developer_instructions: str | None = None`. Change the base_instructions default handling to a None-sentinel (currently `base_instructions: str = STERILE_INSTRUCTIONS` — keep the default but make the caller able to pass `""`; the None-sentinel logic lives in `codex_run_v2` which passes `base_instructions=args.get("base_instructions")` → if None, omit so `start_thread`'s default applies; if a string incl. "", forward it). Concretely set in `start_thread`:

```python
    def start_thread(self, sandbox="read-only", approval_policy="on-request",
                     base_instructions=None, developer_instructions=None,
                     config=None, cwd=None, one_shot=False):
        bi = STERILE_INSTRUCTIONS if base_instructions is None else base_instructions
        if config is None:
            config = ISOLATION_CONFIG
        ...
        params = {"sandbox": sandbox, "approvalPolicy": approval_policy,
                  "baseInstructions": bi, "config": config}
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        ...
```

In `codex_run_v2` new-thread branch, forward the args:

```python
            thread_id = manager.start_thread(
                sandbox=sandbox_for_start, approval_policy=approval_policy_for_start,
                base_instructions=args.get("base_instructions"),
                developer_instructions=args.get("developer_instructions"),
                config=ISOLATION_CONFIG, cwd=cwd_for_start)
```

Add to `TOOLS[0]["inputSchema"]["properties"]`: `"base_instructions": {"type": "string"}`, `"developer_instructions": {"type": "string"}`.

**Test-infra:** extend the existing `call_codex_run(...)` driver with `base_instructions=None, developer_instructions=None, config=None` params (None = omit), forwarding each into `args` exactly like the existing `sandbox`/`effort`/`cwd` params — the B2 forwarding test (`test_codex_run_v2_forwards_parity_args_to_thread_start`) relies on this.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "base_instructions_sentinel or developer_instructions_wire_key" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): expose base_instructions + developer_instructions (B1)"
```

---

## Task B2: `config` passthrough with isolation scrub

**Files:**
- Modify: `mcp/codex_server.py` (`start_thread` config merge, `codex_run_v2` forward `config`, `TOOLS[0]` schema)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `ISOLATION_CONFIG`.
- Produces: a module-level `_CONFIG_DENY` frozenset; `start_thread(config=...)` deny-scrub merge.

- [ ] **Step 1: Write the failing test**

```python
def test_config_merge_scrubs_isolation_keys():
    # _started_params defined in Task B1 Step 1.
    sent = _started_params(config={"mcp_servers": {"evil": 1}, "mcpServers": {"evil": 1},
                                   "baseInstructions": "x", "developerInstructions": "y",
                                   "model_reasoning_effort": "high"})["config"]
    assert sent["mcp_servers"] == {}                  # ISOLATION wins
    assert "mcpServers" not in sent                    # alias scrubbed
    assert "baseInstructions" not in sent and "developerInstructions" not in sent
    assert sent["model_reasoning_effort"] == "high"    # benign key passes

def test_codex_run_v2_forwards_parity_args_to_thread_start(ext_child):
    # R1-F3/R2-F2: prove ALL parity args reach thread/start THROUGH codex_run_v2 (not just start_thread).
    res = call_codex_run(ext_child, prompt="hi",
                         base_instructions="custom-base",
                         developer_instructions="be terse",
                         config={"mcpServers": {"evil": 1}, "model_reasoning_effort": "high"})
    assert "error" not in res
    p = ext_child.received("thread/start")["params"]   # codex_run_v2 calls start_thread once (new thread)
    assert p["baseInstructions"] == "custom-base"      # forwarded — overrides the STERILE default
    assert p["developerInstructions"] == "be terse"    # forwarded by codex_run_v2
    assert p["config"]["mcp_servers"] == {}            # isolation wins after forward + merge
    assert "mcpServers" not in p["config"]
    assert p["config"]["model_reasoning_effort"] == "high"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "config_merge_scrubs or forwards_parity_args" -q`
Expected: FAIL (caller config is not merged/scrubbed yet — `start_thread` ignores a non-None config except as a whole-replace).

- [ ] **Step 3: Implement**

```python
_CONFIG_DENY = frozenset({
    "mcp_servers", "mcpServers",
    "baseInstructions", "base_instructions",
    "developerInstructions", "developer_instructions",
})
```

In `start_thread`, replace the `if config is None: config = ISOLATION_CONFIG` block with a scrub-merge:

```python
        caller_cfg = config if isinstance(config, dict) else {}
        merged = {k: v for k, v in caller_cfg.items() if k not in _CONFIG_DENY}
        merged.update(ISOLATION_CONFIG)     # our keys always win
        config = merged
```

In `codex_run_v2` new-thread branch, pass `config=args.get("config")` instead of the hard-coded `ISOLATION_CONFIG` (start_thread now does the merge). Add `"config": {"type": "object"}` to `TOOLS[0]` schema.

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k "config_merge_scrubs or forwards_parity_args" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp-v2): config passthrough with isolation-key scrub (B2)"
```

---

## Task B3: tool-schema regression test + `_drift` description

**Files:**
- Modify: `mcp/codex_server.py` (`TOOLS[0]["description"]` — mention `_drift`)
- Test: `tests/test_codex_mcp_v2.py`

- [ ] **Step 1: Write the failing test**

```python
def test_tools_list_exposes_parity_fields_and_drift():
    import codex_server as cs
    props = cs.TOOLS[0]["inputSchema"]["properties"]
    for f in ("base_instructions", "developer_instructions", "config"):
        assert f in props
    assert "_drift" in cs.TOOLS[0]["description"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -k tools_list_exposes_parity -q`
Expected: FAIL (`_drift` not yet in the description).

- [ ] **Step 3: Implement** — append to the `TOOLS[0]["description"]` string: `" Returns a _drift array if upstream codex protocol drift is detected."`

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -q` (full offline suite)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "test(codex-mcp-v2): tools/list schema + _drift description regression (R1-F4)"
```

---

## Task D1: docs

**Files:**
- Modify: `CLAUDE.md` (codex MCP v2 section) — worktree-root-relative; `git rev-parse --show-toplevel` == the bulldozer dir
- Modify: `docs/superpowers/specs/2026-06-18-codex-mcp-v2-app-server-bridge.md` (note the drift-resilience + param-parity additions)

- [ ] **Step 1:** Update the CLAUDE.md "Architecture: codex MCP server" section to document: the `_drift` tool-result field (behavioral codes only); the log-only version capture (`LAST_VERIFIED_CODEX_VERSION`, `~/.claude/hooks/bulldozer-codex.log`); the `_KNOWN_NOTIFICATIONS` allowlist + terminal-failure detection; the new `base_instructions`/`developer_instructions`/`config` params with the isolation scrub; the CI fingerprint coherence tripwire. Keep it concise — point to the spec for detail.

- [ ] **Step 2:** Run the full offline suite once more:

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -q`
Expected: PASS (all tasks green).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-06-18-codex-mcp-v2-app-server-bridge.md
git commit -m "docs(codex-mcp-v2): document drift-resilience + param-parity"
```

---

## Final verification (after all tasks)

- [ ] `pytest tests/test_codex_mcp_v2.py -m "not slow" -q` → all pass.
- [ ] `python3 -m py_compile mcp/codex_server.py` → clean.
- [ ] Manual smoke (happy path unchanged): a `mcp__plugin_bulldozer_codex__codex_run` review call returns the same shape, NO `_drift` key.
- [ ] Live drift smoke (optional, requires restart so the edited stdio server is live): confirm an unknown-notification turn surfaces `_drift` in the tool result.
- [ ] PR into `bulldozer/main` (auto-calver will bump `plugin.json` on merge — do NOT bump manually).
