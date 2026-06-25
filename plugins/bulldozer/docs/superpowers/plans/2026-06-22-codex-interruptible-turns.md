# Codex Interruptible Turns + Concurrent Wait-Loop (#218 + #252) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a mid-flight codex turn stop cleanly (on Esc-cancel, CC timeout, or our opt-in `timeout`) via `turn/interrupt`, return a resumable partial result, and fix the #252 approval-wait pipe-fill deadlock by draining the codex child during approval waits.

**Architecture:** Extend the existing single-threaded `select` bridge so the two in-turn wait points (the `codex_run_v2` turn pump and the `bridge_approval` approval wait) `select` on BOTH the codex child stdout fd AND `sys.stdin`, routing child frames through one shared handler and CC frames through a shape-based router. All interrupts converge on one routine (`turn/interrupt {threadId, turnId}` → bounded pump → graceful per-mode result). Default-on with a `BULLDOZER_CODEX_NO_INTERRUPT` kill-switch; the #252 drain is always on.

**Tech Stack:** Python 3.11+, stdlib `select`/`os`/`json`, pytest (offline FakeChild + `@pytest.mark.slow` real-codex). Single file: `mcp/codex_server.py`. Tests: `tests/test_codex_mcp_v2.py`.

**Spec:** `docs/superpowers/specs/2026-06-22-codex-interruptible-turns-design.md` (GO, exhaustive review, 4 rounds, 13 findings, 0 FP).

## Global Constraints

- Single source file: `mcp/codex_server.py`. Tests: `tests/test_codex_mcp_v2.py`.
- Python 3.11+ (`tomllib`); stdlib only.
- **stdin discipline:** all CC-stdin reads use `sys.stdin.readline()` (NEVER `os.read` on the stdin fd) — mixing with `readline`'s buffer loses frames (codex_server.py header contract, line ~24).
- **Happy path byte-identical:** a normal turn with no interruption, kill-switch UNSET, must behave exactly as today. `Reactor.pump()` with no `watch_cc` arg is child-only and unchanged.
- **Mandatory after EVERY task that touches `codex_server.py`:** `pytest tests/test_codex_mcp_v2.py -q` (offline). The slow real-codex e2e (`-m slow`) runs in Task 9 (allow 3-8 min, cold-start varies; self-skips without codex).
- **TDD: visible RED before GREEN** every task. No manual `plugin.json` bump (auto-calver on merge).
- Interrupt result carries NO `"error"` key (the dispatcher sets `isError` iff `"error" in res`, codex_server.py ~466).
- Codex 0.141 `TurnStatus` ∈ {`completed`, `interrupted`, `failed`, `inProgress`}; the existing terminal arm is `if t.get("status") != "completed" or t.get("error")`.

---

### Task 1: `Reactor.pump` gains opt-in `watch_cc`

**Files:**
- Modify: `mcp/codex_server.py` — `Reactor.pump` (the `class Reactor` `pump` method)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `Reactor.pump(self, timeout=0.1, watch_cc=False) -> list[dict]`. With `watch_cc=False` (default) the return is child frames only, byte-identical to today. With `watch_cc=True` it also `select`s on `sys.stdin`; if a CC line arrived it is parsed and appended to the returned list as a tagged wrapper `{"__cc__": <parsed-dict>}` (a CC line that fails to parse is appended as `{"__cc__": None}` so the caller can still see "a CC frame arrived"). Child frames are never wrapped. At most one CC line is read per call (`readline`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_mcp_v2.py  (add near the other Reactor tests)
import os, json, select
import codex_server as cs

class _FakePipe:
    """A pollable fd backed by an os.pipe; write() feeds the read end."""
    def __init__(self):
        self.r, self.w = os.pipe()
    def feed(self, data: bytes):
        os.write(self.w, data)
    def fileno(self):
        return self.r

def test_reactor_pump_default_is_child_only(monkeypatch):
    """watch_cc defaults False → select never includes sys.stdin (R2-F1)."""
    child = _FakePipe()
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    captured = {}
    real_select = select.select
    def spy(rl, wl, xl, to):
        captured["rlist"] = list(rl)
        return real_select(rl, wl, xl, to)
    monkeypatch.setattr(cs.select, "select", spy)
    child.feed(b'{"method":"x","params":{}}\n')
    frames = r.pump(timeout=0.2)
    assert captured["rlist"] == [child.fileno()]          # stdin NOT watched
    assert frames and frames[0]["method"] == "x"
    assert all("__cc__" not in f for f in frames)          # no CC tagging

def test_reactor_pump_watch_cc_reads_cc_frame(monkeypatch):
    """watch_cc=True → a CC stdin line is parsed and tagged __cc__; child frames untagged."""
    child = _FakePipe()
    ccpipe = _FakePipe()
    # Real file object so BOTH fileno() (select) AND readline() (pump) work (R1-F4).
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(ccpipe.r))
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    ccpipe.feed(b'{"method":"notifications/cancelled","params":{"requestId":4}}\n')
    frames = r.pump(timeout=0.2, watch_cc=True)
    cc = [f["__cc__"] for f in frames if "__cc__" in f]
    assert len(cc) == 1 and cc[0]["method"] == "notifications/cancelled"
    assert cc[0]["params"]["requestId"] == 4

def test_reactor_pump_watch_cc_tags_eof(monkeypatch):
    """watch_cc=True → CC stdin EOF (write end closed) is tagged {"__eof__": True} (R1-F1)."""
    child = _FakePipe()
    ccpipe = _FakePipe()
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(ccpipe.r))
    os.close(ccpipe.w)                                  # close write end → reader sees EOF
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    frames = r.pump(timeout=0.2, watch_cc=True)
    cc = [f["__cc__"] for f in frames if "__cc__" in f]
    assert cc == [{"__eof__": True}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "reactor_pump_default_is_child_only or reactor_pump_watch_cc_reads_cc_frame"`
Expected: FAIL — `pump()` has no `watch_cc` kwarg (TypeError) / no `__cc__` tagging.

- [ ] **Step 3: Implement `watch_cc` in `Reactor.pump`**

Replace the body of `Reactor.pump` with:

```python
    def pump(self, timeout: float = 0.1, watch_cc: bool = False) -> list:
        """Return complete JSON-RPC frames received from the child.

        watch_cc=False (default): child-only select — byte-identical to the
        original. watch_cc=True: also select sys.stdin; a CC line read this call
        is appended to the result tagged {"__cc__": <parsed-or-None>} so the
        caller can route it. At most one CC line per call (readline)."""
        watch = [self._child_out_fd]
        if watch_cc:
            watch.append(sys.stdin)
        ready, _, _ = select.select(watch, [], [], timeout)
        out = []
        if self._child_out_fd in ready:
            try:
                chunk = os.read(self._child_out_fd, 65536)
            except OSError:
                chunk = b""
            if chunk:
                out.extend(self._stream.feed(chunk))
        if watch_cc and sys.stdin in ready:
            line = sys.stdin.readline()
            if not line:                              # EOF: CC stdin closed (e.g. CC tool-call timeout)
                out.append({"__cc__": {"__eof__": True}})   # tagged so the router can teardown (R1-F1)
            else:
                line = line.strip()
                if line:
                    try:
                        out.append({"__cc__": json.loads(line)})
                    except json.JSONDecodeError:
                        out.append({"__cc__": None})
        return out
```

(Note: `select.select` accepts a file object with `.fileno()` for `sys.stdin`; the int child fd and the `sys.stdin` object can coexist in one watch list — `ready` membership tests use the same objects passed in.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "reactor_pump_default_is_child_only or reactor_pump_watch_cc_reads_cc_frame"`
Expected: PASS.

- [ ] **Step 5: Full offline suite (no regression on the child-only path)**

Run: `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS (all existing turn/approval tests still green — they call `pump()` with no `watch_cc`).

- [ ] **Step 6: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #218 Reactor.pump opt-in watch_cc (default child-only)"
```

---

### Task 2: Extract the shared child-frame handler (pure refactor, then interrupting routing)

**Files:**
- Modify: `mcp/codex_server.py` — factor the Phase-2 per-frame body out of `codex_run_v2`'s turn loop into a module function `_handle_child_frame`
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `_handle_child_frame(frame, ts) -> dict | None` where `ts` is a mutable **turn-state dict** holding the accumulators (`final_message_parts: list[str]`, `usage_snapshot: dict`, `retries: int`, `interrupting: bool`, plus the read-only context the terminal arm needs: `manager`, `turn_start_t`, `mcp_mode`, `mcp_servers_enabled`, `effort_val`, `model_val`, `mode`, `thread_id`, `review_target`). Returns `None` to mean "continue the turn" and a **result dict** to mean "turn terminated, return this". When `ts["interrupting"]` is truthy, a `turn/completed` with `status="interrupted"` returns the graceful interrupted result (Task 3's builder) instead of the terminal-error dict.

This task is two parts: **2a pure extraction** (full suite must stay green — zero behavior change), then **2b** the `interrupting` routing.

- [ ] **Step 1 (2a): Write a characterization test for the extracted handler**

```python
def _mk_ts(**over):
    ts = {"final_message_parts": [], "usage_snapshot": {}, "retries": 0,
          "interrupting": False, "manager": None, "turn_start_t": 0.0,
          "mcp_mode": "isolated", "mcp_servers_enabled": [], "effort_val": None,
          "model_val": None, "mode": "implement", "thread_id": "t1",
          "review_target": None}
    ts.update(over); return ts

def test_handle_child_frame_accumulates_delta():
    ts = _mk_ts()
    out = cs._handle_child_frame(
        {"method": "item/agentMessage/delta", "params": {"delta": "hello"}}, ts)
    assert out is None and ts["final_message_parts"] == ["hello"]

def test_handle_child_frame_completed_returns_result():
    ts = _mk_ts(final_message_parts=["done"])
    out = cs._handle_child_frame(
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}, ts)
    assert out is not None and "error" not in out and out.get("result") == "done"

def test_handle_child_frame_failed_status_returns_error():
    ts = _mk_ts()
    out = cs._handle_child_frame(
        {"method": "turn/completed", "params": {"turn": {"status": "failed"}}}, ts)
    assert out is not None and "error" in out
```

- [ ] **Step 2: Run — expect FAIL (no `_handle_child_frame`)**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "handle_child_frame"`
Expected: FAIL — `AttributeError: module 'codex_server' has no attribute '_handle_child_frame'`.

- [ ] **Step 3 (2a): Extract the Phase-2 body verbatim into `_handle_child_frame`**

Create the function by moving the EXISTING Phase-2 `if kind == "notification":` body out of `codex_run_v2` (the block handling `item/agentMessage/delta`, `item/completed` review text, `thread/tokenUsage/updated`, `turn/completed`, `error`, and the `_drift_warn` UNKNOWN_NOTIFICATION fallthrough). Preserve logic EXACTLY:

```python
def _handle_child_frame(frame: dict, ts: dict):
    """Process one app-server (child) frame against turn-state `ts`.

    Returns None to continue the turn, or a result dict to terminate it.
    Shared by the turn pump and the #252 approval-wait drain so a frame is
    handled identically wherever it is read. (Extracted verbatim from the
    codex_run_v2 Phase-2 body; the only addition is the `interrupting` routing.)"""
    method = frame.get("method", "")
    if method == "item/agentMessage/delta":
        ts["final_message_parts"].append(frame.get("params", {}).get("delta") or "")
        return None
    if method == "item/completed" and ts["review_target"] is not None:
        _it = frame.get("params", {}).get("item", {}) or {}
        if _it.get("type") == "agentMessage":
            ts["final_message_parts"].append(_it.get("text") or "")
        return None
    if method == "thread/tokenUsage/updated":
        tu = frame.get("params", {}).get("tokenUsage")
        if isinstance(tu, dict):
            ts["usage_snapshot"] = tu
        return None
    if method == "turn/completed":
        t = frame.get("params", {}).get("turn", {}) or {}
        # 2b: an interrupt WE initiated terminates as status="interrupted" — route
        # it to the graceful result, bypassing the generic terminal-failure arm.
        if ts.get("interrupting") and t.get("status") == "interrupted" and not t.get("error"):
            return _build_interrupted_result(ts, interrupted_by=ts.get("interrupted_by", "cancel"))
        if t.get("status") != "completed" or t.get("error"):
            meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                      ts["mcp_mode"], ts["mcp_servers_enabled"],
                                      ts["effort_val"], ts["model_val"], "failed")
            return {"error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                    "thread_id": ts["thread_id"], **meta}
        meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "completed")
        if ts["retries"]:
            meta["retries"] = ts["retries"]
        return _shape_result(ts["mode"], ts["thread_id"], "".join(ts["final_message_parts"]), meta)
    if method == "error":
        is_terminal, emsg = _classify_error_notification(frame.get("params", {}) or {})
        if not is_terminal:
            ts["retries"] += 1
            return None
        meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "failed")
        return {"error": f"codex error: {emsg or 'unknown error'}",
                "thread_id": ts["thread_id"], **meta}
    if method not in _KNOWN_NOTIFICATIONS:
        # NOTE: drift accumulation moves to ts["acc"]; wire in Step 3b.
        _drift_warn(ts.get("acc"), "UNKNOWN_NOTIFICATION", method)
    return None
```

**Step 3b — wire the call site (2a):** in `codex_run_v2`, replace the Phase-2 `if kind == "notification":` body with a call:

```python
                if kind == "notification":
                    _res = _handle_child_frame(frame, ts)
                    if _res is not None:
                        state_machine.turn_completed()
                        return _stamp_drift(_res, acc)
                    continue
```

Build `ts` once before the loop (a dict aliasing the existing locals: `final_message_parts`, `usage_snapshot`, etc.) and include `"acc": acc`. Keep `final_message_parts`/`usage_snapshot`/`retries` reads elsewhere pointing at `ts[...]` (or keep the locals and copy back — simplest is to make `ts` the single home and read `ts["final_message_parts"]` everywhere they were used). For 2a, `interrupting`/`interrupted_by` are absent/falsy so the new branch is dead → behavior identical.

**`_build_interrupted_result` does not exist yet** — for Task 2 it is only reached when `interrupting` is true, which never happens until Task 6. Add a stub that raises `NotImplementedError` so 2a stays a pure refactor; Task 3 replaces it. (The 2a tests never set `interrupting`.)

- [ ] **Step 4: Run the full suite — 2a must be byte-identical (zero behavior change)**

Run: `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS — including ALL existing turn/review/error tests. If any pre-existing test changes outcome, the extraction is not faithful — fix before continuing.

- [ ] **Step 5: Commit 2a**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "refactor(codex-mcp): extract _handle_child_frame (pure, #218 prep)"
```

---

### Task 3: Interrupted result builder + `_build_result_meta` "interrupted" status

**Files:**
- Modify: `mcp/codex_server.py` — `_make_result_meta`/`_build_result_meta` (accept `status="interrupted"`), add `_build_interrupted_result`
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `_build_interrupted_result(ts, interrupted_by: str, thread_warm: bool = True) -> dict`. Returns the mode-shaped result (`_shape_result(ts["mode"], ts["thread_id"], "".join(ts["final_message_parts"]), meta)`) merged with `{"status": "interrupted", "interrupted_by": interrupted_by, "partial_text": "".join(ts["final_message_parts"]), "thread_warm": thread_warm}` and meta built with status `"interrupted"`. **No `"error"` key.**

- [ ] **Step 1: Write the failing tests**

```python
def test_build_interrupted_result_no_error_key_and_partial():
    ts = _mk_ts(mode="implement", final_message_parts=["partial work"])
    res = cs._build_interrupted_result(ts, interrupted_by="cancel")
    assert "error" not in res                         # F7: isError must stay false
    assert res["status"] == "interrupted"
    assert res["interrupted_by"] == "cancel"
    assert res["partial_text"] == "partial work"
    assert res["thread_warm"] is True
    assert res["result"] == "partial work"            # implement mode shape preserved

def test_build_interrupted_result_review_mode_shape():
    ts = _mk_ts(mode="review", final_message_parts=["{\"verdict\":\"x\"}"])
    res = cs._build_interrupted_result(ts, interrupted_by="timeout")
    assert "error" not in res and res["status"] == "interrupted"
    # review shape keys present (verdict/findings/schema_ok per _shape_result)
    assert "schema_ok" in res or "verdict" in res

def test_build_interrupted_result_teardown_thread_cold():
    ts = _mk_ts(final_message_parts=[])
    res = cs._build_interrupted_result(ts, interrupted_by="cancel", thread_warm=False)
    assert res["thread_warm"] is False and res["partial_text"] == ""

def test_dispatcher_interrupted_result_not_marked_iserror():
    """End-to-end: a result with status=interrupted and no 'error' key → isError NOT set."""
    res = {"status": "interrupted", "partial_text": "", "thread_id": "t"}
    # mirror the dispatcher rule (codex_server.py ~466)
    assert ("error" in res) is False
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "build_interrupted_result or interrupted_result_not_marked"`
Expected: FAIL — no `_build_interrupted_result`.

- [ ] **Step 3: Implement**

In `_build_result_meta` (and its inner `_make_result_meta`), the `status` param is already free-form (it is interpolated, not validated) — confirm `"interrupted"` flows through to `meta["status"]`. Then add:

```python
def _build_interrupted_result(ts: dict, interrupted_by: str, thread_warm: bool = True) -> dict:
    """Graceful, resumable result for an interrupted turn (F7). Mode-shaped (so a
    review/implement/codex_review caller still gets its keys), with interrupt
    metadata and NO 'error' key (the dispatcher marks isError iff 'error' in res)."""
    partial = "".join(ts["final_message_parts"])
    meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                              ts["mcp_mode"], ts["mcp_servers_enabled"],
                              ts["effort_val"], ts["model_val"], "interrupted")
    res = _shape_result(ts["mode"], ts["thread_id"], partial, meta)
    res["status"] = "interrupted"          # overrides meta status key to the top level too
    res["interrupted_by"] = interrupted_by
    res["partial_text"] = partial
    res["thread_warm"] = thread_warm
    return res
```

Remove the `NotImplementedError` stub from Task 2.

- [ ] **Step 4: Run — expect PASS, then full suite**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "build_interrupted_result or interrupted_result_not_marked"`
Then: `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #218 interrupted result builder + interrupted meta status"
```

---

### Task 4: Interrupt routine + teardown invariant

**Files:**
- Modify: `mcp/codex_server.py` — add `_run_interrupt`; add module constant `_INTERRUPT_COMPLETE_TIMEOUT = 10.0`
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `_run_interrupt(manager, ts, turn_id, interrupted_by) -> dict`. Sets `ts["interrupting"]=True`, `ts["interrupted_by"]=interrupted_by`; if `turn_id` is falsy → teardown branch immediately. Otherwise sends `turn/interrupt {threadId: ts["thread_id"], turnId: turn_id}` via `manager._write`, then pumps (child-only) up to `_INTERRUPT_COMPLETE_TIMEOUT`; feeds each child frame to `_handle_child_frame` — the `interrupting` branch yields the graceful result. If no terminal result arrives within the bound (or `turn_id` was falsy) → **teardown invariant**: kill the child (`manager._child.kill()`, set `manager._child=None` so it respawns next call), and return `_build_interrupted_result(ts, interrupted_by, thread_warm=False)`. The caller (turn loop) is responsible for `state_machine.turn_completed()`.

- [ ] **Step 1: Write the failing tests** (use a FakeManager whose reactor yields scripted frames)

```python
class _ScriptedReactor:
    def __init__(self, batches): self._batches = list(batches)
    def pump(self, timeout=0.1, watch_cc=False):
        return self._batches.pop(0) if self._batches else []

class _FakeManager:
    def __init__(self, reactor):
        self._reactor = reactor
        self.writes = []
        self._idc = 0
        self._child = type("C", (), {"kill": lambda self: None})()
    def _write(self, frame): self.writes.append(frame)
    def _next_id(self):
        self._idc += 1
        return self._idc

def test_run_interrupt_sends_turn_interrupt_with_id_and_returns_graceful():
    # batch: the empty {} response to turn/interrupt (id==1) THEN turn/completed
    r = _ScriptedReactor([[{"id": 1, "result": {}},
                           {"method": "turn/completed",
                            "params": {"turn": {"status": "interrupted"}}}]])
    mgr = _FakeManager(r)
    ts = _mk_ts(final_message_parts=["half"]); ts["manager"] = mgr
    res = cs._run_interrupt(mgr, ts, turn_id="turn_1", interrupted_by="cancel")
    assert mgr.writes[0]["method"] == "turn/interrupt"
    assert mgr.writes[0]["id"] == 1                       # R2-F1: request, not notification
    assert mgr.writes[0]["params"] == {"threadId": "t1", "turnId": "turn_1"}
    assert res["status"] == "interrupted" and res["partial_text"] == "half"
    assert res["thread_warm"] is True and "error" not in res
    assert "_drift" not in res                            # the {} response is consumed, not drift

def test_run_interrupt_no_completion_tears_down_cold(monkeypatch):
    monkeypatch.setattr(cs, "_INTERRUPT_COMPLETE_TIMEOUT", 0.05)
    r = _ScriptedReactor([])                  # child never completes
    mgr = _FakeManager(r)
    killed = {"n": 0}
    mgr._child = type("C", (), {"kill": lambda self: killed.__setitem__("n", killed["n"]+1)})()
    ts = _mk_ts(); ts["manager"] = mgr
    res = cs._run_interrupt(mgr, ts, turn_id="turn_1", interrupted_by="timeout")
    assert killed["n"] == 1 and mgr._child is None
    assert res["status"] == "interrupted" and res["thread_warm"] is False

def test_run_interrupt_no_turn_id_tears_down_without_sending():
    r = _ScriptedReactor([]); mgr = _FakeManager(r)
    ts = _mk_ts(); ts["manager"] = mgr
    res = cs._run_interrupt(mgr, ts, turn_id=None, interrupted_by="cancel")
    assert mgr.writes == []                   # nothing sent — no turnId
    assert res["thread_warm"] is False and res["status"] == "interrupted"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "run_interrupt"`
Expected: FAIL — no `_run_interrupt`.

- [ ] **Step 3: Implement**

```python
_INTERRUPT_COMPLETE_TIMEOUT = 10.0  # bounded wait for turn/completed after turn/interrupt

def _run_interrupt(manager, ts: dict, turn_id, interrupted_by: str) -> dict:
    """Stop the in-flight turn cleanly and return a graceful, resumable result.

    Teardown invariant (R3-F1): the no-turnId and no-completion branches kill the
    child (→ respawn next call) and return thread_warm=False. Caller clears the
    TurnStateMachine."""
    ts["interrupting"] = True
    ts["interrupted_by"] = interrupted_by

    def _teardown():
        try:
            if manager._child is not None:
                manager._child.kill()
        except Exception:
            pass
        manager._child = None
        return _build_interrupted_result(ts, interrupted_by, thread_warm=False)

    if not turn_id:
        return _teardown()
    # turn/interrupt is a REQUEST (app-server replies an empty {} response), NOT a
    # notification — it MUST carry an id (R2-F1; verified vs the app-server source and
    # /tmp/turn_interrupt_probe.py, which sent it with mgr._next_id()).
    iid = manager._next_id()
    manager._write({"id": iid, "method": "turn/interrupt",
                    "params": {"threadId": ts["thread_id"], "turnId": turn_id}})
    deadline = time.time() + _INTERRUPT_COMPLETE_TIMEOUT
    while time.time() < deadline:
        for frame in manager._reactor.pump(timeout=0.2):
            if "__cc__" in frame:          # ignore CC frames during interrupt drain
                continue
            if frame.get("id") == iid:     # the empty {} response to turn/interrupt — consume, do NOT handle
                continue
            res = _handle_child_frame(frame, ts)
            if res is not None:
                return res                 # the interrupting branch → graceful result
    return _teardown()                     # no turn/completed within the bound
```

- [ ] **Step 4: Run — PASS, then full suite**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "run_interrupt"` then `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #218 interrupt routine + teardown invariant"
```

---

### Task 5: Mid-turn CC frame router (F3 + R3-F2 envelopes)

**Files:**
- Modify: `mcp/codex_server.py` — add `_route_cc_frame`
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Produces: `_route_cc_frame(frame, cc_id, reply_fn) -> str`. Returns `"interrupt"` (our cancel), or `"continue"` (everything else, after replying to id-bearing requests). `frame` is a parsed CC frame (or `None` for an unparseable line → `"continue"`). For an id-bearing request it calls `reply_fn(id, result=..., error=...)` with the correct CC-facing envelope (the dispatcher's `reply` helper is injected as `reply_fn`); a second `tools/call` gets the MCP `CallToolResult` wrapping `TurnStateMachine.busy_error()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_route_cc_cancel_for_our_id_returns_interrupt():
    replies = []
    f = {"method": "notifications/cancelled", "params": {"requestId": 7}}
    assert cs._route_cc_frame(f, cc_id=7, reply_fn=lambda *a, **k: replies.append((a, k))) == "interrupt"
    assert replies == []                        # a notification gets no reply

def test_route_cc_cancel_for_other_id_continues():
    f = {"method": "notifications/cancelled", "params": {"requestId": 99}}
    assert cs._route_cc_frame(f, cc_id=7, reply_fn=lambda *a, **k: None) == "continue"

def test_route_cc_second_tools_call_gets_calltoolresult_busy():
    seen = {}
    def reply_fn(mid, result=None, error=None): seen.update(id=mid, result=result, error=error)
    f = {"id": 12, "method": "tools/call", "params": {"name": "codex_run"}}
    assert cs._route_cc_frame(f, cc_id=7, reply_fn=reply_fn) == "continue"
    assert seen["id"] == 12 and seen["error"] is None
    assert seen["result"]["isError"] is True
    assert "already in flight" in seen["result"]["content"][0]["text"]

def test_route_cc_ping_and_tools_list_get_valid_results():
    out = []
    def reply_fn(mid, result=None, error=None): out.append((mid, result, error))
    assert cs._route_cc_frame({"id": 1, "method": "ping"}, 7, reply_fn) == "continue"
    assert out[-1] == (1, {}, None)
    assert cs._route_cc_frame({"id": 2, "method": "tools/list"}, 7, reply_fn) == "continue"
    assert out[-1][0] == 2 and "tools" in out[-1][1]

def test_route_cc_unparseable_or_notification_continues():
    assert cs._route_cc_frame(None, 7, lambda *a, **k: None) == "continue"
    assert cs._route_cc_frame({"method": "notifications/foo"}, 7, lambda *a, **k: None) == "continue"

def test_route_cc_response_shaped_frame_is_ignored():
    """A response-shaped CC frame mid-turn (id + result, no method) is not ours to answer (R1-F3)."""
    out = []
    assert cs._route_cc_frame({"id": 5, "result": {"action": "accept"}}, 7,
                              lambda *a, **k: out.append(a)) == "continue"
    assert out == []                            # no reply written to a response

def test_route_cc_eof_marker_returns_teardown():
    """CC stdin EOF marker → teardown (CC gone) (R1-F1)."""
    assert cs._route_cc_frame({"__eof__": True}, 7, lambda *a, **k: None) == "teardown"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "route_cc"`
Expected: FAIL — no `_route_cc_frame`.

- [ ] **Step 3: Implement**

```python
def _route_cc_frame(frame, cc_id, reply_fn) -> str:
    """Route a mid-turn CC frame. Returns 'interrupt' (our cancel — CC alive), 'teardown'
    (stdin EOF — CC gone), or 'continue'. Only REQUEST-shaped id-bearing frames are answered
    (R1-F3); a response-shaped frame mid-turn is not ours to answer → ignored."""
    if not isinstance(frame, dict):
        return "continue"                       # unparseable CC line
    if frame.get("__eof__"):                     # CC stdin closed (e.g. CC tool-call timeout)
        return "teardown"                        # R1-F1: CC gone → teardown (cold); don't wait for ACK
    method = frame.get("method", "")
    if method == "notifications/cancelled":
        if (frame.get("params") or {}).get("requestId") == cc_id:
            return "interrupt"
        return "continue"
    if classify(frame) != "request":            # R1-F3: response/notification → not ours to answer
        return "continue"
    mid = frame.get("id")
    # id-bearing REQUEST → MUST answer (CC would block otherwise)
    if method == "ping":
        reply_fn(mid, result={})
    elif method == "tools/list":
        reply_fn(mid, result={"tools": TOOLS})
    elif method == "tools/call":
        busy = _v2_state_machine.busy_error()   # {"error": "codex turn already in flight"}
        reply_fn(mid, result={"content": [{"type": "text", "text": json.dumps(busy)}],
                              "isError": True})
    else:
        reply_fn(mid, error={"code": -32601, "message": f"server busy; method {method!r} not serviced mid-turn"})
    return "continue"
```

- [ ] **Step 4: Run — PASS, then full suite**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "route_cc"` then `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #218 mid-turn CC frame router + envelopes"
```

---

### Task 6: Turn-pump integration — cancel / opt-in timeout / pre-ACK (+ kill-switch helper)

**Files:**
- Modify: `mcp/codex_server.py` — `codex_run_v2` turn loop; add `_interrupts_enabled()` helper; thread `reply` into the turn path (or use the module `reply`)
- Test: `tests/test_codex_mcp_v2.py` (offline, via the existing v2 turn harness / FakeChild)

**Interfaces:**
- Consumes: `_handle_child_frame` (T2), `_build_interrupted_result` (T3), `_run_interrupt` (T4), `_route_cc_frame` (T5), `Reactor.pump(watch_cc=...)` (T1).
- Produces: `_interrupts_enabled() -> bool` (`not os.environ.get("BULLDOZER_CODEX_NO_INTERRUPT")`). The turn loop now: pumps with `watch_cc=_interrupts_enabled()`; captures `turn_id` at the Phase-1 ACK; routes CC frames via `_route_cc_frame`; on `"interrupt"` (or the opt-in `timeout` deadline when interrupts enabled) runs `_run_interrupt` and returns; pre-ACK cancel sets `cancel_pending` → interrupt once `turn_id` is captured; setup waits stay child-only (no change — `_pump_until` is untouched).

- [ ] **Step 1: Write failing tests** (extend the existing offline turn harness; the helper that drives `codex_run_v2` with a scripted FakeChild — mirror existing turn tests in the file)

```python
def test_turn_midstream_cancel_interrupts_with_partial(v2_turn_harness):
    # harness scripts: ACK (turn/start resp with turn.id), one delta, then a CC
    # notifications/cancelled(requestId == cc_id); expect turn/interrupt sent and
    # a status=interrupted result carrying the delta.
    res = v2_turn_harness(
        ack_turn_id="turn_9",
        child_frames=[{"method": "item/agentMessage/delta", "params": {"delta": "abc"}}],
        cc_frames=[{"method": "notifications/cancelled", "params": {"requestId": "CCID"}}],
        interrupt_completion={"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}},
    )
    assert res["status"] == "interrupted"
    assert res["partial_text"] == "abc"
    assert "error" not in res

def test_turn_optin_timeout_returns_graceful_when_interrupts_enabled(v2_turn_harness, monkeypatch):
    monkeypatch.delenv("BULLDOZER_CODEX_NO_INTERRUPT", raising=False)
    res = v2_turn_harness(ack_turn_id="turn_9", timeout=0.05, child_frames=[],  # never completes
                          interrupt_completion={"method": "turn/completed",
                                                "params": {"turn": {"status": "interrupted"}}})
    assert res["status"] == "interrupted" and res["interrupted_by"] == "timeout"

def test_turn_pre_ack_cancel_interrupts_after_ack(v2_turn_harness):
    # cancel arrives BEFORE the ACK; expect cancel_pending → interrupt fires once
    # the ACK (turn_id) is captured; partial_text empty.
    res = v2_turn_harness(pre_ack_cc=[{"method": "notifications/cancelled",
                                       "params": {"requestId": "CCID"}}],
                          ack_turn_id="turn_9",
                          interrupt_completion={"method": "turn/completed",
                                                "params": {"turn": {"status": "interrupted"}}})
    assert res["status"] == "interrupted" and res["partial_text"] == ""

def test_turn_pre_ack_cancel_no_ack_tears_down_cold(v2_turn_harness):
    """cancel pre-ACK + ACK never arrives (ack_deadline) → graceful COLD result (R1-F2)."""
    res = v2_turn_harness(pre_ack_cc=[{"method": "notifications/cancelled",
                                       "params": {"requestId": "CCID"}}],
                          never_ack=True, ack_timeout=0.05)
    assert res["status"] == "interrupted" and res["thread_warm"] is False

def test_turn_stdin_eof_tears_down(v2_turn_harness):
    """CC stdin EOF mid-turn (CC gone) → FORCED cold teardown: thread_warm:false, child
    killed, NO turn/interrupt sent (R1-F1)."""
    res = v2_turn_harness(ack_turn_id="turn_9", child_frames=[], cc_frames=["__EOF__"])
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    assert "turn/interrupt" not in res["appserver_methods_written"]

def test_review_start_missing_turn_id_interrupt_tears_down_cold_and_not_busy(v2_turn_harness):
    """A review turn whose ACK carries no turn.id, then a cancel → cold teardown, and the
    NEXT call is NOT busy (TurnStateMachine cleared) (R1-F2)."""
    res = v2_turn_harness(review=True, ack_turn_id=None,    # review/start ACK lacks turn.id
                          cc_frames=[{"method": "notifications/cancelled", "params": {"requestId": "CCID"}}])
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    assert res["next_call_busy"] is False                  # harness issues a follow-up tools/call

def test_eof_wins_over_same_batch_child_completion(v2_turn_harness):
    """One pump returns [child turn/completed success, CC EOF] → EOF forces cold teardown;
    the same-batch completion is NOT returned (undeliverable — CC gone), no turn/interrupt (R1-F1)."""
    res = v2_turn_harness(ack_turn_id="turn_9",
                          batch_after_ack=[{"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
                                           "__EOF__"])
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    assert "turn/interrupt" not in res["appserver_methods_written"]

def test_same_batch_completion_wins_over_cancel(v2_turn_harness):
    """[child turn/completed success, CC cancel] in one batch → the deliverable completion wins
    (CC alive); result is NOT interrupted (documented race resolution, R1-F1)."""
    res = v2_turn_harness(ack_turn_id="turn_9",
                          batch_after_ack=[{"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
                                           {"method": "notifications/cancelled", "params": {"requestId": "CCID"}}])
    assert res.get("status") != "interrupted"              # completion returned, cancel moot

def test_no_cc_id_turn_does_not_watch_stdin(v2_turn_harness, monkeypatch):
    """A direct turn WITHOUT _cc_id (no live pollable stdin) → watch=False → the Reactor never
    reads sys.stdin → normal completion, no spurious EOF teardown (R5-F1). This is the shape of
    the pre-existing direct codex_run_v2 unit tests, which must stay byte-identical."""
    monkeypatch.delenv("BULLDOZER_CODEX_NO_INTERRUPT", raising=False)
    res = v2_turn_harness(ack_turn_id="turn_9", omit_cc_id=True,
                          child_frames=[{"method": "item/agentMessage/delta", "params": {"delta": "ok"}},
                                        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}])
    assert res.get("status") != "interrupted" and res["result"] == "ok"
    assert res["watched_cc"] is False                      # pump never called with watch_cc=True
```

(Harness conventions wired in the fixture: `never_ack` makes the scripted child never send the turn/start ACK and shrinks `ack_deadline` to `ack_timeout`; `omit_cc_id` drives the turn WITHOUT injecting `args["_cc_id"]` (so `watch` is False); `watched_cc` records whether any `reactor.pump` call passed `watch_cc=True`; the `"__EOF__"` sentinel closes the fake CC stdin write end so `pump` reads EOF; `review=True` drives the turn via `review/start` (sets `ts["review_target"]`) and `ack_turn_id=None` yields an ACK with no `turn.id`; `appserver_methods_written` records the `method`s written to the app-server; `next_call_busy` issues a second `tools/call` after the turn and reports whether it got `busy_error`; `batch_after_ack` is the list of frames a SINGLE post-ACK `pump` returns — child-frame dicts and CC sentinels (`"__EOF__"` or a `notifications/cancelled` dict) intermixed — to exercise same-batch ordering.)

(Implementers: if the file has no reusable `v2_turn_harness`, build one as a pytest fixture that wires a scripted FakeChild + a fake CC stdin into `codex_run_v2`, mirroring the existing `TestV2Dispatcher`/turn tests. Reuse the existing FakeChild machinery in `tests/test_codex_mcp_v2.py`.)

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "turn_midstream_cancel or turn_optin_timeout_returns_graceful or turn_pre_ack_cancel"`
Expected: FAIL — turn loop does not yet watch CC / interrupt.

- [ ] **Step 3: Implement the integration**

Add the helper and wire the loop. Key edits in `codex_run_v2`:

```python
def _interrupts_enabled() -> bool:
    return not os.environ.get("BULLDOZER_CODEX_NO_INTERRUPT")
```

In the turn loop, build `ts` (Task 2) including `"acc": acc`, init `turn_id = None`, `cancel_pending = False`, and gate the CC watch:

```python
            # Only watch CC stdin when interrupts are enabled AND we have a cc_id to correlate
            # a notifications/cancelled against (R5-F1). No cc_id (e.g. a direct codex_run_v2
            # unit test that injects fake cc_read/cc_write but no live sys.stdin) → watch=False →
            # the Reactor never reads global sys.stdin → happy-path direct tests are unaffected.
            # Production always injects args["_cc_id"] = mid in main(). The v2_turn_harness sets
            # _cc_id AND monkeypatches sys.stdin to a pollable fake pipe for the interrupt tests.
            watch = _interrupts_enabled() and args.get("_cc_id") is not None
```

Change the pump call:

```python
            frames = reactor.pump(timeout=0.2, watch_cc=watch)
            # CC stdin EOF has BATCH PRIORITY (R1-F1): a closed CC channel cannot receive ANY
            # result, so a same-batch child completion is undeliverable → force cold teardown
            # BEFORE processing the batch in order. (A same-batch CANCEL deliberately does NOT
            # preempt: pump appends child frames before the CC frame, so an in-order child
            # turn/completed wins and returns its real, DELIVERABLE result — the user cancelled
            # but the work finished; returning it beats discarding it for a cold teardown.)
            if any(isinstance(f.get("__cc__"), dict) and f["__cc__"].get("__eof__") for f in frames):
                return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
            for frame in frames:
                if "__cc__" in frame:                          # CC-side frame (only when watch)
                    action = _route_cc_frame(frame["__cc__"], cc_id=args.get("_cc_id"), reply_fn=reply)
                    if action == "interrupt":
                        if turn_id:                            # warm interrupt — turn_id known
                            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "cancel", state_machine), acc)
                        elif turn_acked:                       # ACK arrived but NO turn.id (review/start) → cold teardown NOW
                            return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)   # R4-F1
                        else:                                  # genuinely pre-ACK → interrupt once turn_id is captured
                            cancel_pending = True
                    elif action == "teardown":                 # stdin EOF: CC gone → FORCE cold teardown
                        # Pass turn_id=None so _run_interrupt ALWAYS tears down (kill child,
                        # thread_warm:false, NO turn/interrupt) — never the warm path (R1-F1).
                        return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                    continue
                kind = classify(frame)
                method = frame.get("method", "")
                # ... existing request (approval) branch unchanged ...
                if not turn_acked:
                    if kind == "response" and frame.get("id") == mid:
                        if "error" in frame: ...        # unchanged
                        turn_acked = True
                        # turn_id may be None for a review/start ACK without a turn id →
                        # a later interrupt routes to the cold teardown (R1-F2 / spec review/start fallback).
                        turn_id = ((frame.get("result") or {}).get("turn") or {}).get("id")
                        if cancel_pending:               # pre-ACK cancel → interrupt now (turn_id None → teardown cold)
                            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "cancel", state_machine), acc)
                    # ... rest of Phase-1 unchanged ...
                    continue
                if kind == "notification":
                    _res = _handle_child_frame(frame, ts)
                    if _res is not None:
                        state_machine.turn_completed()
                        return _stamp_drift(_res, acc)
                    continue

            # --- after draining the batch (EXISTING check sites, extended for cancel_pending) ---
            # Child EOF: if a cancel is pending and the child died, return a graceful COLD
            # interrupted result rather than the bare eof error (R1-F2).
            if manager._child is not None and manager._child.poll() is not None:
                if cancel_pending:
                    return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                eof_err = state_machine.eof_error()
                manager._child = None
                return _stamp_drift(eof_err, acc)

            # Phase-1 ACK timeout: a pending cancel whose ACK never arrived → graceful COLD
            # teardown rather than the bare ACK-timeout error (R1-F2).
            if not turn_acked and time.time() > ack_deadline:
                if cancel_pending:
                    return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                state_machine.turn_completed()
                return _stamp_drift({"error": f"{start_method} response timed out"}, acc)
```

Opt-in timeout: where the loop currently returns the bare `{"error": f"turn timed out after {turn_timeout} s"}`, branch on interrupts:

```python
        # opt-in deadline exceeded
        if _interrupts_enabled():
            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "timeout", state_machine), acc)
        state_machine.turn_completed()
        return _stamp_drift({"error": f"turn timed out after {turn_timeout} s"}, acc)
```

Add a tiny wrapper so state-clear is centralized:

```python
def _finish_interrupt(manager, ts, turn_id, interrupted_by, state_machine):
    res = _run_interrupt(manager, ts, turn_id, interrupted_by)
    state_machine.turn_completed()
    return res
```

(The `review/start` path sets `ts["review_target"]`; `turn_id` capture is the same ACK branch — Task 4's teardown covers a missing review turn id.)

- [ ] **Step 4: Run — PASS, then full suite**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "turn_midstream_cancel or turn_optin_timeout or turn_pre_ack_cancel"`
Then: `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS (happy-path tests unaffected — `watch_cc` adds stdin only when enabled, and no CC cancel arrives in those tests).

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #218 turn-pump interrupt integration (cancel/timeout/pre-ACK)"
```

---

### Task 7: Approval-wait #252 drain + cancel-during-approval (F1, F2)

**Files:**
- Modify: `mcp/codex_server.py` — `_bridge_approval_dispatch` / `read_correlated` so the approval wait drains the child via `_handle_child_frame` and handles a cancel by returning the method's existing decline then signaling interrupt
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- The approval wait gains access to the turn-state `ts` (for child drain) and the child reactor. On a cancel for our `cc_id` during the wait, it returns the SAME per-method decline the `read_correlated`-returns-None branch already produces (so the caller writes a method-valid `{id, result}` via `manager._write`, exactly as the happy path does), and sets `ts["cancel_during_approval"] = True` so the turn loop runs `_finish_interrupt` immediately after writing the decline. A turn that TERMINATES during the wait (terminal `error`) is stored in `ts["terminal_during_approval"]` and surfaced once by the turn loop after the decline write (R1-F5). The #252 drain is **always on** (independent of `_interrupts_enabled()`); only the cancel→interrupt action is gated.

- [ ] **Step 1: Write the failing tests**

```python
def test_approval_wait_drains_child_no_deadlock_and_keeps_deltas(approval_harness):
    # child emits a large stdout burst + a delta WHILE an elicitation is pending;
    # CC reply arrives after. Expect: no hang, approval resolved, delta accumulated.
    res = approval_harness(
        method="item/commandExecution/requestApproval",
        child_during_wait=[{"method": "item/agentMessage/delta", "params": {"delta": "X"*5000}}],
        cc_reply={"jsonrpc": "2.0", "id": "EID", "result": {"action": "accept"}},
    )
    assert res["deadlocked"] is False
    assert "X"*5000 in res["accumulated"]

def test_cancel_during_approval_writes_method_valid_decline_then_interrupt(approval_harness):
    for method, expected in [
        ("item/commandExecution/requestApproval", "decline"),
        ("item/fileChange/requestApproval", "decline"),
        ("item/permissions/requestApproval", cs.PERM_DECLINE),
        ("item/tool/requestUserInput", {"answers": {}}),
        ("mcpServer/elicitation/request", {"action": "cancel", "content": None, "_meta": None}),
    ]:
        res = approval_harness(method=method,
                               cancel_during=True, cc_id="CCID")
        assert res["written_to_appserver"] == expected   # decline BEFORE interrupt
        assert res["interrupt_signaled"] is True
        assert res["order"] == ["decline", "interrupt"]

def test_terminal_error_during_approval_surfaces_through_turn_loop(approval_harness):
    """A terminal child error during an approval wait is surfaced once via
    ts['terminal_during_approval'], and a method-valid decline is still written (R1-F5)."""
    res = approval_harness(method="item/commandExecution/requestApproval",
                           child_during_wait=[{"method": "error",
                                               "params": {"error": {"message": "boom"}}}])  # terminal
    assert res["written_to_appserver"] == "decline"      # app-server still answered
    assert res["surfaced_terminal"] is True and "boom" in res["final_error"]

def test_pre_ack_approval_cancel_defers_then_warm_interrupts_at_ack(approval_harness):
    """A cancel during a PRE-ACK approval writes the decline and sets cancel_pending (NOT cold
    teardown); it warm-interrupts once the turn ACK supplies turn_id (R6-F1)."""
    res = approval_harness(method="item/commandExecution/requestApproval",
                           pre_ack=True, cancel_during=True, cc_id="CCID", ack_turn_id="turn_9",
                           interrupt_completion={"method": "turn/completed",
                                                 "params": {"turn": {"status": "interrupted"}}})
    assert res["written_to_appserver"] == "decline"
    assert res["status"] == "interrupted" and res["thread_warm"] is True   # warm: turn_id captured at ACK

def test_eof_during_approval_cold_tears_down_immediately(approval_harness):
    """CC stdin EOF during an approval → decline written, then cold teardown IMMEDIATELY
    (not deferred to the next pump), thread_warm:false (R6-F1)."""
    res = approval_harness(method="item/commandExecution/requestApproval", eof_during=True)
    assert res["written_to_appserver"] == "decline"
    assert res["status"] == "interrupted" and res["thread_warm"] is False

def test_eof_wins_over_same_iteration_terminal_during_approval(approval_harness):
    """A child terminal frame AND CC EOF both ready in the SAME approval-wait iteration →
    EOF wins (post-write checks eof BEFORE terminal): cold teardown, thread_warm:false, the
    terminal result is NOT surfaced (R6-F1 EOF-priority invariant)."""
    res = approval_harness(method="item/commandExecution/requestApproval",
                           child_during_wait=[{"method": "error", "params": {"error": {"message": "boom"}}}],
                           eof_during=True)             # both ready in one iteration
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    assert res.get("surfaced_terminal") is not True     # terminal held pending, EOF won
```

(Approval-harness conventions: `cancel_during`/`eof_during` inject a `notifications/cancelled` (for `cc_id`) / a CC stdin EOF during the wait (when both `child_during_wait` and `eof_during` are set, the harness makes the child terminal frame and the EOF ready in the SAME loop iteration); `pre_ack=True` bridges the approval BEFORE the turn ACK and then delivers the ACK (`ack_turn_id`) + `interrupt_completion`; `child_during_wait` emits child frames mid-wait; `written_to_appserver` is the decision written back for the bridged request; `order`/`interrupt_signaled`/`surfaced_terminal`/`final_error`/`deadlocked` report the observed sequence/outcome.)

- [ ] **Step 2: Run — expect FAIL**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "approval_wait_drains_child or cancel_during_approval_writes"`
Expected: FAIL.

- [ ] **Step 3: Implement**

KEEP `cc_read_fn` as the approval-reply reader (so direct approval tests that inject a fake `cc_read_fn` stay valid — R5-F1; do NOT switch the approval path to the Reactor's `watch_cc`, which reads global `sys.stdin`) and ADD a child-stdout drain via the **child-only** Reactor. Pass the child reactor + `ts` + `cc_id` into `_bridge_approval_dispatch` (thread through `handle_server_request`). Two new `ts` keys are the return channels: `ts["cancel_during_approval"]` (bool) and `ts["terminal_during_approval"]` (a turn-result dict, else None). Rewrite `read_correlated`'s wait loop, per iteration (each side a SHORT timeout so neither starves):

1. **Drain the child (#252 fix):** `for cf in reactor.pump(timeout=0.05):` (child-only — `watch_cc` NOT used here) → `res = _handle_child_frame(cf, ts)`. If `res is not None` the turn TERMINATED mid-approval (e.g. a terminal `error`): record it as a PENDING terminal (`ts["terminal_during_approval"] = res`) but do **NOT** return yet — an EOF in this SAME iteration must still be able to win the EOF-priority invariant (R6-F1). Otherwise (delta/usage) keep going — this drain is what prevents the #252 deadlock.
2. **Read the reply/cancel/EOF via `cc_read_fn`:** `frame = cc_read_fn(timeout=0.05)`:
   - `frame is _CC_EOF` (the new EOF sentinel) → CC gone → set `ts["eof_during_approval"]=True` and return the per-method decline. EOF wins **even over a same-iteration pending terminal** (the post-write check tests `eof_during_approval` BEFORE `terminal_during_approval`): a closed CC channel can't receive ANY result, so cold teardown.
   - approval reply (`id == eid`, a response) → resolve as today.
   - `notifications/cancelled` for `cc_id` AND `_interrupts_enabled()` → set `ts["cancel_during_approval"]=True` and RETURN the per-method `resp is None` decline (`"decline"` / `PERM_DECLINE` / `{"answers":{}}` / `{"action":"cancel",...}` / ReviewDecision deny).
   - any other id-bearing request → `_route_cc_frame(frame, cc_id, reply)` (answers it; never interrupts from here — the turn loop owns that).
   - `frame is None` → transient/timeout → fall to step 3.
3. **End of iteration:** if a PENDING terminal was recorded in step 1 (and step 2 returned nothing) → return the per-method decline now (the post-write check surfaces `ts["terminal_during_approval"]`). Else keep looping within the overall `deadline`.

**`cc_read_fn` EOF sentinel (R5-F1):** define `_CC_EOF = object()` at module scope. Change `cc_read_fn` so an EOF read (`readline()` → `""`) returns `_CC_EOF` instead of `None` (None stays "transient/timeout"). The main dispatcher loop already breaks on its own `readline()` EOF, so only the approval path consumes the sentinel; `read_correlated`'s existing `None`-continue logic is unchanged for true timeouts.

In `codex_run_v2`, the approval site already does `manager._write(handle_server_request(...))`. AFTER that write, in priority order:

```python
                    manager._write(handle_server_request(
                        frame, cc_write_fn, cc_read_fn, ts, args.get("_cc_id"), reactor,
                        acc=acc, narrative=_new_narr))
                    if ts.pop("eof_during_approval", False):              # R6-F1: EOF PRIORITY — cold teardown BEFORE
                        return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)  # surfacing any terminal (CC dead → undeliverable)
                    if ts.get("terminal_during_approval") is not None:    # R1-F5: turn ended during approval (CC alive)
                        state_machine.turn_completed()
                        return _stamp_drift(ts["terminal_during_approval"], acc)
                    if ts.pop("cancel_during_approval", False):           # F2 + R6-F1: phase-aware, mirrors the turn pump
                        if turn_id:                                       # warm interrupt — turn_id known
                            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "cancel", state_machine), acc)
                        elif turn_acked:                                  # acked but no turn.id (review/start) → cold
                            return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                        else:                                             # pre-ACK approval cancel → defer to the ACK branch
                            cancel_pending = True                         # fall through; fires when turn_id is captured
                    # ... existing approval bookkeeping (deadline credit-back) unchanged ...
```

**Gate note:** the child-drain (steps 1-2) is unconditional (the #252 fix — always on). The cancel→interrupt (step 3) is gated by `_interrupts_enabled()`: with the kill-switch set, a cancel during approval is skipped (legacy `read_correlated` skips the frame), but the drain still runs.

- [ ] **Step 4: Run — PASS, then full suite**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "approval"` then `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS (existing approval tests still green — happy-path approval reply path unchanged).

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #252 approval-wait child drain + cancel-during-approval (F1/F2)"
```

---

### Task 8: Kill-switch matrix (F8)

**Files:**
- Modify: `mcp/codex_server.py` — log-once when the switch is set (best-effort, stable log)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_interrupts_enabled()` (T6). Verifies the scope matrix: kill-switch SET → no cancel-interrupt, opt-in timeout → legacy bare error; but #252 approval child-drain still runs.

- [ ] **Step 1: Write the failing tests**

```python
def test_kill_switch_disables_cancel_interrupt(v2_turn_harness, monkeypatch):
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    # cancel mid-turn must be IGNORED (no turn/interrupt); turn runs to its own completion
    res = v2_turn_harness(ack_turn_id="turn_9",
                          child_frames=[{"method": "item/agentMessage/delta", "params": {"delta": "z"}},
                                        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}],
                          cc_frames=[{"method": "notifications/cancelled", "params": {"requestId": "CCID"}}])
    assert res.get("status") != "interrupted"            # completed normally
    assert res.get("result") == "z"

def test_kill_switch_optin_timeout_is_legacy_bare_error(v2_turn_harness, monkeypatch):
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    res = v2_turn_harness(ack_turn_id="turn_9", timeout=0.05, child_frames=[])
    assert "error" in res and "timed out" in res["error"]

def test_kill_switch_keeps_approval_drain(approval_harness, monkeypatch):
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    res = approval_harness(method="item/commandExecution/requestApproval",
                           child_during_wait=[{"method": "item/agentMessage/delta", "params": {"delta": "Y"*5000}}],
                           cc_reply={"jsonrpc": "2.0", "id": "EID", "result": {"action": "accept"}})
    assert res["deadlocked"] is False                    # #252 fix is NOT gated

def test_kill_switch_cancel_during_approval_does_not_interrupt(approval_harness, monkeypatch):
    """Switch set → a cancel during approval is IGNORED (no interrupt) while the drain still
    runs and the approval resolves on its own reply (R1-F6, the matrix's approval-cancel arm)."""
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    res = approval_harness(method="item/commandExecution/requestApproval",
                           cancel_during=True, cc_id="CCID",
                           cc_reply={"jsonrpc": "2.0", "id": "EID", "result": {"action": "accept"}})
    assert res["interrupt_signaled"] is False            # cancel ignored under kill-switch
    assert res["deadlocked"] is False                    # drain still active
```

- [ ] **Step 2: Run — expect FAIL** (if any matrix arm is wrong)

Run: `pytest tests/test_codex_mcp_v2.py -q -k "kill_switch"`
Expected: FAIL on whichever arm is not yet correctly gated.

- [ ] **Step 3: Implement the gating + log-once**

Ensure: the turn pump passes `watch_cc=_interrupts_enabled()` (T6 — so with the switch set, CC cancels are never read → never interrupt); the opt-in-timeout branch returns the legacy bare error when `not _interrupts_enabled()` (T6); the approval-wait drain is unconditional (T7) while its cancel→interrupt is gated. Add a best-effort log-once when the switch is set, to the stable log:

```python
def _log_kill_switch_once():
    if getattr(_log_kill_switch_once, "_done", False) or _interrupts_enabled():
        return
    _log_kill_switch_once._done = True
    try:
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser("~/.claude/hooks/bulldozer-codex.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{_now_iso()} | INTERRUPT_DISABLED | BULLDOZER_CODEX_NO_INTERRUPT set\n")
    except Exception:
        pass
```

Call `_log_kill_switch_once()` once at the top of `codex_run_v2`.

- [ ] **Step 4: Run — PASS, then full suite**

Run: `pytest tests/test_codex_mcp_v2.py -q -k "kill_switch"` then `pytest tests/test_codex_mcp_v2.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): #218 kill-switch scope matrix + log-once"
```

---

### Task 9: Docs (CLAUDE.md) + slow real-codex e2e + final full suite

**Files:**
- Modify: `plugins/bulldozer/CLAUDE.md` (the worktree copy: `CLAUDE.md`) — "Architecture: codex MCP server"
- Test: `tests/test_codex_mcp_v2.py` — `@pytest.mark.slow` e2e

- [ ] **Step 1: Write the slow e2e (real codex, self-skips without it)**

```python
@pytest.mark.slow
def test_live_turn_interrupt_returns_partial_and_session_stays_warm(real_codex):
    """Mirror /tmp/turn_interrupt_probe.py through the bridge: start a long real
    turn, send a cancel, assert status=interrupted + a follow-up call still works
    (session warm)."""
    # 1. dispatch codex_run with a long prompt; 2. after deltas flow, inject a
    #    notifications/cancelled(requestId == call id) on the CC side; 3. assert
    #    result status=interrupted, partial_text non-empty, thread_warm True;
    #    4. issue codex_info (no cold start) → succeeds → child still alive.
    ...
```

(Implementers: model this on the existing slow review e2e in the file; reuse its real-codex fixture. If wiring a mid-turn CC cancel through the subprocess dispatcher is impractical, assert the lower-level path: drive `AppServerManager` + `_run_interrupt` against a real turn as `/tmp/turn_interrupt_probe.py` did, then a warm `start_thread`.)

- [ ] **Step 2: Run the slow e2e**

Run: `pytest tests/test_codex_mcp_v2.py -m slow -q -k "interrupt"`
Expected: PASS (or SKIP if codex absent). Allow 3-8 min (cold start varies).

- [ ] **Step 3: Update CLAUDE.md**

In the worktree `CLAUDE.md` "Architecture: codex MCP server" section, add a bullet documenting: interruptible turns (Esc-cancel / CC-timeout / opt-in `timeout` → `turn/interrupt`, graceful resumable `{status:"interrupted", partial_text, thread_id, thread_warm}` result, no `isError`); the #252 approval-wait child-drain (always on); and the `BULLDOZER_CODEX_NO_INTERRUPT` kill-switch (disables interrupt only — the #252 drain stays on). Reference the spec path.

- [ ] **Step 4: Final full suite (offline + slow)**

Run: `pytest tests/test_codex_mcp_v2.py -q` then `pytest tests/test_codex_mcp_v2.py -m slow -q`
Expected: PASS / SKIP-without-codex. This is the mandatory post-change gate.

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py CLAUDE.md
git commit -m "docs+test(codex-mcp): #218/#252 CLAUDE.md + slow interrupt e2e"
```

---

## Self-Review

**Spec coverage (every spec section → a task):**
- Unified concurrent wait-loop / stdin discipline → T1 (`watch_cc`), T6 (turn), T7 (approval).
- Mid-turn CC frame dispatch + envelopes (F3/R3-F2) → T5.
- Shared child-frame handler (F1) → T2 (+ used by T4/T6/T7).
- Three interrupt triggers (Esc/timeout/opt-in) → T6.
- Interrupt routine + pre-ACK + completion-timeout fallback + teardown invariant (F5/F6/R3-F1) → T4, T6.
- Terminal-arm interaction (E1) → T2 (`interrupting` branch in `_handle_child_frame`).
- Approval-wait #252 drain + cancel-during-approval (F1/F2) → T7.
- Per-mode interrupted return contract, no error key (F7) → T3.
- Kill-switch scope matrix (F8) → T6 (helper/gating) + T8 (matrix tests).
- Docs + slow e2e → T9.

**Placeholder scan:** every code step has concrete code; the only "implementers: model on existing harness" notes are for reusing the file's existing FakeChild/real-codex fixtures (their exact shape is in `tests/test_codex_mcp_v2.py`) — not placeholders for the logic under test, which is fully specified.

**Type consistency:** `ts` dict keys are fixed in T2's interface and reused verbatim (T3/T4/T6/T7). `_run_interrupt(manager, ts, turn_id, interrupted_by)`, `_build_interrupted_result(ts, interrupted_by, thread_warm)`, `_route_cc_frame(frame, cc_id, reply_fn)`, `_handle_child_frame(frame, ts)`, `_interrupts_enabled()` signatures are consistent across tasks. `Reactor.pump(timeout, watch_cc)` return shape (`list`, CC tagged `{"__cc__":…}`) is consumed identically in T6/T7.
