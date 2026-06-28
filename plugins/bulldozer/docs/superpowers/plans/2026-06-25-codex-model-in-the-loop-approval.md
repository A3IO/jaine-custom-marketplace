# Codex Model-in-the-Loop Approval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`)
> syntax for tracking.

**Status:** CONSOLIDATED — all review corrections (panel P1-P9, the config + holistic passes) are folded
INTO the tasks below; there is no separate "corrections" appendix to reconcile. Read top-to-bottom.

**Goal:** When codex needs an approval and unattended mode is armed, the bridge routes trivially-safe
commands to an instant fast-path and everything else BACK to the orchestrating session model (which
decides accept/decline with context), then resumes the parked codex turn — replacing the brittle
regex allow-list.

**Architecture:** `codex_run_v2`'s turn-pump loop body becomes an **inner generator** `_drive_turn(ctx)`
that `yield`s an `{awaiting_approval}` payload at a non-trivial approval; `codex_run_v2` stays a
dict-returning function that drives it. A dedicated `codex_approve({park_token, decision_id})` MCP tool
resumes via `gen.send(...)`. The unattended judge becomes a 3-way ROUTING predicate (`fast_accept` /
`park_for_model` / `fail_closed_decline`); the whack-a-mole shell gates are deleted. All of #218/#252
(EOF-priority, interrupt, child-drain) is preserved inside the generator.

**Tech Stack:** Python 3.11+, the codex app-server JSON-RPC bridge (`mcp/codex_server.py`), pytest.

**Design spec (the CONTRACT — read it; this plan implements its C1-C17 + config):**
`docs/superpowers/specs/2026-06-25-codex-model-in-the-loop-approval-design.md`. Feasibility proof:
`docs/superpowers/probes/2026-06-24-return-and-resume-feasibility-probe.py`. (Code refs are by SYMBOL,
not line number — line numbers drift.)

## Global Constraints

- **Python 3.11+** (the server already guards this; do not regress).
- **codex `LAST_VERIFIED_CODEX_VERSION = "0.142"`** — bump + the fingerprint fixture together if it drifts.
- **Default OFF.** The whole feature is gated by `_unattended_active()` (env `BULLDOZER_APPROVAL_UNATTENDED`
  truthy OR the sentinel file, read FRESH per approval). Unarmed → the attended `elicitation/create` path
  is **byte-identical** to today.
- **LOCAL single-user threat model** (spec §2): prompt-injection of the judge is OUT OF SCOPE — do NOT
  add untrusted-input framing. Concerns are correctness + operational, not security.
- **Preserve #218/#252 invariants:** EOF has batch/priority precedence; our-turn `notifications/cancelled`
  interrupts; the child stdout is drained during any approval wait. Every task must keep these.
- **TDD: visible RED before GREEN.** Run each new test and SEE it fail before implementing.
- **After EVERY `mcp/codex_server.py` change:** run the FULL suite incl `-m slow` real-codex
  (`pytest tests/test_codex_mcp_v2.py -q` then `... -m slow -v`). The slow e2e is the only real proof.
- **No manual `plugin.json` bump** (auto-calver on merge).
- **Logging** → stable `~/.claude/hooks/bulldozer-codex.log` (env `BULLDOZER_CODEX_LOG`), best-effort.
- **Parked-turn state** lives in the singleton: `manager._parked = {park_token, thread_id, inner_gen,
  isolation_sig, started_at, request_frame, decision_ids}`; `None` when not parked. `decision_ids` =
  the set of valid `approval.decisions[].id` (+ `"decline"`) — the source `codex_approve_v2`
  pre-validates against BEFORE `gen.send` (F3). `ts`/deadlines live in the generator's suspended frame
  — NOT duplicated in `_parked`.

## File Structure

- **Modify `mcp/codex_server.py`** — the whole surgery (one file by design): `route_approval` +
  `_is_trivially_safe` + `_fast_path_scope`; `TurnStateMachine` parked state; `codex_approve` tool +
  `TOOLS` entry + `main()` route/guard/finite-wait; `build_awaiting_payload` + `build_decision_response`;
  fileChange capture in `_handle_child_frame`; the inner-generator refactor; `_park_cap_s`; `_teardown_park`;
  the `codex_info "approval"` query.
- **Modify `tests/test_codex_mcp_v2.py`** — new tests. **Tasks 6-9 need the SUBPROCESS harness**
  (`tests/fixtures/fake_appserver.py` with `FAKE_SCRIPT=with_approval`) — the in-process `call_codex_run`
  fake answers synchronously with ZERO server→client request and CANNOT model pending-approval→resume.
  Extend `with_approval` for the multi-approval/resume path.
- **Modify `tests/fixtures/codex-protocol-fingerprint.json`** — only if a code constant changes.
- **Modify `plugins/bulldozer/CLAUDE.md`** — replace the "Unattended approval judge" bullet (Task 12).

**Ordering note (was the #1 plan defect):** the unattended judge's runtime behavior does NOT flip until
**Task 7**. Task 1 only ADDS pure functions; the whack-a-mole gates + `classify_approval` are deleted in
Task 7, atomically with the `yield` (Task 6) and `codex_approve_v2`. Deleting/flipping earlier would
send a routing STRING to codex as a literal decision and break existing tests.

---

## Task 1: ADD the routing predicate (PURE, no runtime change yet)

**Files:** Modify `mcp/codex_server.py` (add functions only — do NOT touch `bridge_approval`/`classify_approval`
yet); Test `tests/test_codex_mcp_v2.py`.

**Interfaces:**
- Produces: `route_approval(method, params, project_root) -> "fast_accept" | "park_for_model" | "fail_closed_decline"`
  — consulted ONLY for the FIVE approval methods (`item/commandExecution|fileChange|permissions/requestApproval`,
  legacy `execCommandApproval`/`applyPatchApproval`); `requestUserInput`/`mcpServer/elicitation` are NEVER
  passed to it (they keep their human path — wired in Task 7).
- Produces: `_is_trivially_safe(command, project_root) -> bool`.
- Produces: `_fast_path_scope() -> str` — `os.environ.get("BULLDOZER_FAST_PATH_SCOPE") or "reads"`.

- [ ] **Step 1: Write the failing tests** (pure functions)
```python
def _route(method, params, root="/proj"):
    from codex_server import route_approval
    return route_approval(method, params, root)

def test_route_trivial_read_fast_accept():
    assert _route("item/commandExecution/requestApproval", {"command": "cat README.md"}) == "fast_accept"
    assert _route("item/commandExecution/requestApproval", {"command": "ls -la | grep x"}) == "fast_accept"

def test_route_complexity_and_vars_park():
    for cmd in ("echo $(curl x)", "cat <(curl x)", "python3 build.py", "rm -rf .",
                "rm -rf $FOO/", "cp x ~root/.profile"):
        assert _route("item/commandExecution/requestApproval", {"command": cmd}) == "park_for_model"

def test_route_permissions_and_filechange_park_not_decline():
    assert _route("item/permissions/requestApproval", {"permissions": {"network": {}}}) == "park_for_model"
    assert _route("item/fileChange/requestApproval", {}) == "park_for_model"
    assert _route("applyPatchApproval", {}) == "park_for_model"

def test_route_malformed_fail_closed():
    assert _route("item/commandExecution/requestApproval", {}) == "fail_closed_decline"

def test_route_non_dict_params_no_crash():            # F5 — truthy non-dict params must NOT raise
    for bad in (None, [], "x", 7):
        assert _route("item/commandExecution/requestApproval", bad) == "fail_closed_decline"
```

- [ ] **Step 2: Run → RED** `pytest tests/test_codex_mcp_v2.py -k route_ -v` → FAIL (`route_approval` undefined).

- [ ] **Step 3: Implement `route_approval` + `_is_trivially_safe` + `_fast_path_scope` (ADD only).**
  Reuse the existing `_safe_tokens`/`_strip_cmd_prefixes`/`_split_segments` tokenizers. `_is_trivially_safe`:
  every segment's de-prefixed verb ∈ a SMALL read set (`cat ls pwd grep rg nl head tail wc stat …`) or an
  in-project simple write (`mkdir touch`, `rm <in-project-subpath>`) **AND** the whole string has NO
  complexity marker: `$(` `` ` `` **`$VAR`/`${VAR}`** `<(` `>(` redirect-outside-cwd `~`anything root-glob.
  Honor `_fast_path_scope()`: `"reads"` = the small set above; `"local-work"` adds ONLY a few PLAIN
  bare-verb forms (`pytest`, `make`, `npm test`, `cargo build`) with NO options-bearing escape. **F2:
  `local-work` must NOT re-introduce the deleted git/`--work-tree`/`-C`/wrapper parsing** — any global
  git option, runner wrapper (`sh -c`/`env`/`xargs`/`timeout`), or §4 complexity marker still PARKS;
  git writes are NOT in `local-work` v1. (If a form can't be matched by a trivial bare-verb rule, it
  parks — never parse-to-accept.)
```python
def route_approval(method, params, project_root=None):
    params = params if isinstance(params, dict) else {}   # F5: handle_server_request keeps truthy
    if _has_escalation_amendment(params):                 # non-dict params; normalize BEFORE any .get()
        return "park_for_model"                           # structured escalation → model
    if method == "item/permissions/requestApproval":
        return "park_for_model"
    if method in ("item/fileChange/requestApproval", "applyPatchApproval"):
        return "park_for_model"
    cmd = (params or {}).get("command")
    if method in ("item/commandExecution/requestApproval", "execCommandApproval") and isinstance(cmd, str) and cmd.strip():
        return "fast_accept" if _is_trivially_safe(cmd, project_root) else "park_for_model"
    if _all_read_actions((params or {}).get("commandActions")) and isinstance(cmd, str) and cmd.strip():
        return "fast_accept" if _is_trivially_safe(cmd, project_root) else "park_for_model"
    return "fail_closed_decline"
```
  DO NOT delete the whack-a-mole gates or `classify_approval` here — that is Task 7 (ordering).

- [ ] **Step 4: GREEN** `pytest tests/test_codex_mcp_v2.py -k route_ -v` (existing unattended tests untouched).
- [ ] **Step 5: Commit** `feat(codex-mcp): add approval routing predicate (pure, not yet wired) (#277)`

---

## Task 2: `TurnStateMachine` parked state (C3)

**Files:** Modify `mcp/codex_server.py` (`class TurnStateMachine`); Test.

**Interfaces:** `tsm.park(park_token, thread_id)`, `tsm.is_parked() -> bool`, `tsm.parked_token() -> str|None`,
`tsm.unpark()`. `is_busy()` returns True while parked; `busy_error` distinguishes parked from in-flight.

- [ ] **Step 1: Failing test**
```python
def test_tsm_parked_state():
    from codex_server import TurnStateMachine
    t = TurnStateMachine(); t.turn_started(cc_id=1); t.park("tok-abc", "thr-1")
    assert t.is_parked() and t.is_busy() and t.parked_token() == "tok-abc"
    t.unpark(); assert not t.is_parked()
```
- [ ] **Step 2: RED.** - [ ] **Step 3:** add `self._parked = None`; `park`/`is_parked`/`parked_token`/`unpark`;
  `is_busy()` → `self._in_flight or self._parked is not None`; `turn_completed`/`eof_error` also clear `_parked`.
- [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit** `feat(codex-mcp): TurnStateMachine parked state (#277)`.

---

## Task 3: `codex_approve` MCP tool — `TOOLS` entry + `main()` early route (C9/C15)

**Files:** Modify `mcp/codex_server.py` (`TOOLS`, `main()`); Test (`TestV2Dispatcher`).

**Interfaces:** a `codex_approve` tool in `tools/list`, `inputSchema {park_token:str(req), decision_id:str(req)}`,
NO `prompt`/`mcp`. `main()` dispatches `codex_approve` BY NAME before the `codex_run` fallback and before
any validation/`ensure()`.

- [ ] **Step 1: Failing tests** (subprocess dispatcher):
```python
def test_codex_approve_advertised_in_tools_list(...):
    # initialize → tools/list → assert codex_approve present, schema requires park_token+decision_id, no prompt/mcp.
def test_codex_run_thread_only_still_requires_prompt(...):
    # codex_run({thread_id:"x"}) → "prompt is required" — proves the two surfaces are distinct
```
- [ ] **Step 2: RED.** - [ ] **Step 3:** add the `TOOLS` entry; in `main()` add
  `elif tool_name == "codex_approve": res = codex_approve_v2(args, cc_write_fn=cc_write_fn, cc_read_fn=cc_read_fn)`
  before `else: codex_run_v2(...)`. Stub `codex_approve_v2` → `{"error":"not parked"}` (filled Task 7).
- [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit** `feat(codex-mcp): codex_approve tool + dispatch (#277)`.

---

## Task 4: Capture file-change patches in `_handle_child_frame` (C16 source)

**Files:** Modify `mcp/codex_server.py` (`_handle_child_frame`, `ts` init); Test.

**Interfaces:** `ts["file_changes"][itemId] = {"patch": "<accumulated>", "turn_id": …}` from
`item/fileChange/patchUpdated` (full) + `item/fileChange/outputDelta` (incremental) — NOT `item/completed`
(which is captured only in the `review_target` branch). Both are already in `_KNOWN_NOTIFICATIONS`.

- [ ] **Step 0 (R2-F4): PROVE the real field names** — before coding, dump the actual
  `item/fileChange/patchUpdated`/`outputDelta`/`requestApproval` shapes via
  `codex app-server generate-json-schema` (or a live probe) and pin the real key names (`itemId` vs
  `item.id`, the patch/delta field, any path/op field on `requestApproval`). Build the test fixture from
  the REAL shape, not an assumed one — the no-patch fallback (Task 5) may rely only on CONFIRMED fields.
- [ ] **Step 1: Failing test** — feed a `item/fileChange/patchUpdated` (REAL shape from Step 0) to
  `_handle_child_frame` with a `ts`; assert `ts["file_changes"][itemId]["patch"]` holds the patch text.
- [ ] **Step 2: RED.** - [ ] **Step 3:** add the two non-terminal branches accumulating into
  `ts.setdefault("file_changes", {})` keyed by `params.item.id`/`itemId`; init `ts["file_changes"] = {}` in
  the `ts` build. - [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit** `feat(codex-mcp): capture item/fileChange patches into ts (#277)`.

---

## Task 5: payload + decision-response builders (C10/C13/C16, P4/P6, decision_id-retry, no-patch)

**Files:** Modify `mcp/codex_server.py` (two pure helpers); Test.

**Interfaces:**
- `build_awaiting_payload(method, params, ts, narrative, park_token) -> dict` →
  `{status:"awaiting_approval", park_token, approval:{kind, …evidence…, decisions:[{id,label}]}, thread_id}`.
  `decisions[]` = bounded opaque ids from `build_command_approval_labels`. Per-kind evidence:
  command → `command/cwd/reason`; permissions → bounded `params["permissions"]` +
  `_summarize_permissions(...)` + `cwd` + `environmentId` + scope decisions; legacy `applyPatchApproval` →
  `params["fileChanges"]`; modern `fileChange` → `ts["file_changes"][itemId]["patch"]`.
  Also return the `decision_ids` set (the `decisions[].id` + `"decline"`) so `codex_run_v2` can store it
  in `manager._parked` for pre-validation (F3). **No-patch case (R2-F4):** PROVE the real
  `item/fileChange/requestApproval` field names first (schema/live probe — the request models only
  `threadId`/`turnId`/`itemId`/`startedAtMs`; today's bridge uses only `reason`, so do NOT assume a
  path/op field). Evidence order: the `ts["file_changes"][itemId]` patch buffer → only probe-CONFIRMED
  fields → fail-closed decline ONLY if neither yields anything (never blind-decline a legit op).
- `build_decision_response(parked_request_frame, decision_id) -> dict` — **takes the FRAME** (needs the
  original request id `msg.get("id")` — P4). It receives an **ALREADY-VALID `decision_id`** (the
  unknown-id check is `codex_approve_v2` step 2, pre-`gen.send` — F3); no unknown-id branch except
  defensive-unreachable. Per method: command → `build_command_approval_labels` reverse-map; permissions
  accept → echo `params["permissions"]` + scope (#272 grant-echo); legacy → review-decision shape;
  `"decline"` → safe decline.

- [ ] **Step 1: Failing tests** — per-kind payload (permissions exposes profile+cwd+environmentId;
  fileChange attaches the patch; a create with no patch still yields request-level evidence, not a blind
  decline); response builder round-trips a chosen id (permissions accept echoes `params["permissions"]`,
  #272); an unknown `decision_id` → an error dict (retryable), not a decline frame.
- [ ] **Step 2: RED.** - [ ] **Step 3:** implement both, reusing `build_command_approval_labels`,
  `_summarize_permissions`, `LBL_*`/`PERM_*`, and the `_bridge_approval_dispatch` per-method shapes.
- [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit** `feat(codex-mcp): awaiting-payload + decision-response builders (#277)`.

---

## Task 6: inner generator `_drive_turn` — ctx-rewrite + park-yield + generator-owned resume (C1/C2/C5/C6/C8b, P3/P5/P6/P9)

**Files:** Modify `mcp/codex_server.py` (`codex_run_v2`); Test (subprocess `with_approval` harness — P7).

**This is the core surgery. It is an explicit ctx-attribute REWRITE, NOT a verbatim move (P5).** The loop
MUTATES locals (`deadline += _elapsed`, `cancel_pending = True`, `turn_id`, `turn_acked`, `narrative_shown`);
a move-only generator raises `UnboundLocalError`. Make a `ctx` carrying the FULL local set the loop reads
(`manager, reactor, ts, mid, start_method, turn_params, deadline, ack_deadline, watch, mode, review_target,
thread_id, meta, cc_write_fn, cc_read_fn, args/_cc_id, acc, state_machine, turn_timeout, narrative_shown,
turn_acked, turn_id, cancel_pending`); every mutated local becomes `ctx.<attr>`; re-read
`manager._child`/`manager._reactor` LIVE (never a stale local). Use `time.monotonic` for the deadlines.

**The park decision is made in the generator's turn-pump LOOP BODY, not inside a nested helper (R3-F1).**
The escape boundary from the synchronous write path is concrete: the existing `if kind == "request"`
block (today `manager._write(handle_server_request(frame, …))` — the ONLY place `yield` is syntactically
legal) consults `route_approval` FIRST for the FIVE approval methods under `_unattended_active()` and
BRANCHES ON THE WRITE: `park_for_model` → it does NOT call `handle_server_request` (NO child reply
written) and instead `decision_id = yield build_awaiting_payload(...)`; a `fast_accept` → also IN the loop
body, `manager._write(build_decision_response(frame, <the method's plain accept>))` WITHOUT entering
`bridge_approval` (its unattended branch is deleted, Task 7); only an attended (unarmed) approval /
non-approval request → `manager._write(handle_server_request(frame, …))` UNCHANGED. So
`handle_server_request`/`bridge_approval` are NEVER entered for the park OR the unattended-`fast_accept`
case — no half-written reply, and no reference to the deleted judge.

**The generator OWNS the resume logic (P3).** On resume the generator (its frame holds `ts`,
deadlines, the request frame): (a) DRAIN buffered child frames (`ts["drained_frames"]` + fresh
`reactor.pump`); (b) check a drained **terminal/EOF FIRST** and surface it BEFORE any write (lost-terminal
guard); (c) CREDIT the park duration to `ctx.deadline`/`ctx.ack_deadline`; (d) build the reply via
`build_decision_response(ctx.request_frame, decision_id)`; (e) WRITE it guarded for `BrokenPipeError` → a
graceful declined/teardown result (C8b); (f) continue the loop. A `fast_accept` route answers inline via
`manager._write(build_decision_response(frame, <plain accept>))` IN the loop body — NOT
`handle_server_request`/`bridge_approval` (R5-F1). The in-generator `eof_during_approval`/`cancel_during_approval`/
`terminal_during_approval` checks STAY (they cover the inline + resume-drain windows — P9; the between-calls
parked window is `main()`'s job, Task 8/9 — disjoint, keep both).

**`codex_run_v2` drives it** (still returns a DICT): `gen = _drive_turn(ctx)`; advance; on a yielded
payload → store `manager._parked = {park_token, thread_id, inner_gen: gen, isolation_sig, started_at,
request_frame, decision_ids}` (the `decision_ids` set comes from `build_awaiting_payload` — Global
Constraints / spec §5.1 / Task 5; it is the SOURCE `codex_approve_v2` pre-validates against BEFORE
`gen.send`, F3 — do NOT drop it from this literal), `tsm.park(...)`, RETURN the payload; on
`StopIteration` → `tsm.turn_completed()`, RETURN `e.value`.

> **No public behavior flip here (F2/P1).** Task 6 EXTRACTS the loop + ADDS the park-yield + the
> resume block, but the yield is DORMANT via the live path — `bridge_approval` still drives the OLD
> inline approval until Task 7 wires routing. So Task 6's test drives the generator DIRECTLY (internal),
> NOT the public `codex_run_v2 → awaiting_approval` path (that test is Task 7, where routing flips).

- [ ] **Step 1: Failing test (drive `_drive_turn` DIRECTLY — internal, no public flip)** — build a
  `ctx` over a fake child (subprocess `with_approval`) that emits a `commandExecution/requestApproval`
  for a NON-trivial command, with the park route FORCED (monkeypatch `route_approval` → `"park_for_model"`,
  or set the internal park flag). Drive the generator (`next`/`send`): assert it YIELDS
  `{"status":"awaiting_approval", "park_token":…, "approval":{"kind":"commandExecution",…}}`, then
  `.send(<a valid decision id>)` resumes it to `StopIteration` with the final dict. This proves the
  generator mechanism WITHOUT touching the live `bridge_approval` routing. (The public
  `codex_run_v2 → awaiting_approval` test lives in Task 7.)
- [ ] **Step 2: RED.** - [ ] **Step 3:** do the ctx-rewrite extraction (behavior-preserving via the
  live path); add the park-yield + the generator-owned resume block (DORMANT until Task 7); have
  `codex_run_v2` drive + park when the generator yields. Keep every #218/#252 branch.
- [ ] **Step 4: GREEN** + run the WHOLE offline suite (no regressions). - [ ] **Step 5: Commit**
  `refactor(codex-mcp): turn-pump → inner _drive_turn generator with park-yield + owned resume (#277)`.

---

## Task 7: THE SWITCHOVER — wire routing + thin `codex_approve_v2` + DELETE the old judge (P1)

**Files:** Modify `mcp/codex_server.py` (`bridge_approval`, `codex_approve_v2`; DELETE `classify_approval`/
`_unattended_decision` + whack-a-mole helpers); Test (subprocess `with_approval`).

This is where runtime behavior flips — atomically, now that the yield (Task 6) and the builders (Task 5)
exist.

**Interfaces:**
- **Routing + park live in the generator LOOP BODY, NOT inside `bridge_approval` (R3-F1)** — a nested
  helper cannot make the generator `yield`. The `if kind == "request"` block consults `route_approval`
  for the FIVE approval methods under `_unattended_active()` and branches on the write (Task 6):
  `park_for_model` → bypass `handle_server_request` (no child write) + `yield` the awaiting payload;
  `fast_accept` → `manager._write(build_decision_response(frame, <plain accept>))` IN the loop body (NOT
  `bridge_approval` — R5-F1); `fail_closed_decline` → `manager._write(build_decision_response(frame, "decline"))`.
  `requestUserInput`/`mcpServer/elicitation` and the unarmed path stay on the unchanged
  `manager._write(handle_server_request(...))` path (P2 — never routed). `bridge_approval` becomes
  ATTENDED-ONLY: its `_unattended_active()` inline-decision branch (the `classify_approval`+
  `_unattended_decision` call) is DELETED; it computes ONLY the attended elicitation (unarmed) + the
  non-approval requests; it does NOT route, does NOT park, and no longer produces the `fast_accept` reply
  (the loop body does, via `build_decision_response`).
- `codex_approve_v2(args, …)` THIN: (1) validate `args["park_token"] == manager._parked["park_token"]`
  (else `{"error":"parked turn expired"}`, park UNCHANGED — double-resume guard); (2) **validate
  `args["decision_id"]` against the parked payload's `decisions[].id` (or `"decline"`) BEFORE `gen.send`**
  — an unknown id → a RETRYABLE `{"error":"unknown decision_id …"}` (`isError`), park UNCHANGED, NO
  `gen.send`/child write, so a hallucinated id never consumes the park (F3); (3) `gen.send(args["decision_id"])`;
  if the generator yields AGAIN → **RE-PARK** (re-store `manager._parked` + `tsm.park`) and return the new
  payload; if `StopIteration` → **`tsm.turn_completed()`** (clears `_in_flight` AND `_parked` — NOT
  `unpark()` alone, which deadlocks the next tool; F1) + `manager._parked = None` + return `e.value`.
- DELETE `classify_approval`, `_unattended_decision`, **`bridge_approval`'s `_unattended_active()` branch
  that called them** (→ `bridge_approval` becomes attended-only; R5-F1), and the whack-a-mole helpers
  (`_command_substitutions`, `_substitution_is_dangerous`, `_SED_EXEC_WRITE`, `_rm_target_too_broad`,
  `_xargs_command_index`, `_git_subcommand`, `_drop_leading_words`, the per-verb gates) + their tests.
- **The `main()` parked guard ships HERE, not in Task 8 (R2-F3).** Because this task makes `codex_run`
  publicly park, the global guard MUST exist the same moment — else a `codex_info` between Task 7 and
  Task 8 touches the parked child (the frame-stealing hazard §6 prevents). So Task 7 adds: while
  `tsm.is_parked()`, `codex_approve` routes by NAME to `codex_approve_v2`; any OTHER tool → busy, park
  preserved, BEFORE its dispatch. (Task 8 then adds `_teardown_park` + cancel/EOF recognition on top.)

- [ ] **Step 1: Failing tests** — (a) armed + non-trivial command → `codex_run` returns `awaiting_approval`;
  (b) `codex_approve(accept-id)` → the fake child gets the exact decision frame (original request id), the
  turn completes, final dict returned; (c) WRONG token → expired error, park unchanged; (d) a multi-approval
  fake → second `codex_approve` resumes the re-park; (e) `requestUserInput` while armed still hits the human
  path (NOT routed); (f) **`codex_info` while parked → busy, park preserved (the guard ships here — R2-F3)**;
  (g) **armed + TRIVIAL command (e.g. `cat x`) → inline accept written to the child (the exact accept
  decision frame, original id), NO yield, NO `awaiting_approval`, NO human elicitation, `bridge_approval`
  NOT consulted (R5-F1)**.
- [ ] **Step 2: RED.** - [ ] **Step 3:** wire routing **in `_drive_turn`'s `if kind == "request"` loop
  body** (NOT in `bridge_approval` — a nested helper can't make the generator `yield`; R3-F1): for the
  five approval methods under `_unattended_active()`, consult `route_approval` BEFORE the write —
  `park_for_model` bypasses `handle_server_request`/`bridge_approval` and yields; `fast_accept` →
  `manager._write(build_decision_response(frame, <plain accept>))` (loop body, NOT `bridge_approval`);
  `fail_closed_decline` → `manager._write(build_decision_response(frame, "decline"))`. `bridge_approval`
  becomes attended-only (its `_unattended_active()` branch deleted — R5-F1; no `route_approval` call, no
  park). Then implement the thin `codex_approve_v2`; add the `main()` parked
  guard; delete the old judge + gates. - [ ] **Step 4: GREEN** + full offline suite.
- [ ] **Step 5: Commit** `feat(codex-mcp): wire routing + thin codex_approve resume; delete old judge (#277)`
- [ ] **Step 6: SLOW GATE** — `pytest tests/test_codex_mcp_v2.py -m slow -v` (first real park→resume e2e).

---

## Task 8: `_teardown_park` + cancel/EOF/respawn teardown (C7/C8/C14, P9)

**Files:** Modify `mcp/codex_server.py` (`_teardown_park`; cancel/EOF recognition; `ensure`/`connection_request` guard); Test.

**Interfaces:**
- The `main()` parked guard already SHIPPED in Task 7 (R2-F3) — `codex_approve` routes by NAME (token
  validated in `codex_approve_v2` → wrong/stale → `parked turn expired`, NOT busy; F4); any OTHER tool →
  `{"error":"codex turn parked — resume or wait"}` (park PRESERVED, C14) before dispatch, so
  `codex_info_v2`/`connection_request` never touch the parked child. THIS task adds the teardown paths.
- `_teardown_park(reason)`: **auto-decline the pending approval FIRST** (the child is blocked awaiting it —
  `turn/interrupt` alone is useless), THEN `turn/interrupt`/kill child (respawn next call), THEN
  `tsm.turn_completed()` (NOT just `unpark()` — else `_in_flight` stays True forever) + `manager._parked=None`.
  Fired on: our-turn `notifications/cancelled` (matched against the parked `_pending_cc_id` — an unrelated
  cancel must NOT tear down), EOF while parked, deliberate isolation-change respawn.

- [ ] **Step 1: Failing tests** — (a) `codex_info` while parked → busy error, `_parked` unchanged;
  (b) EOF while parked → teardown (park cleared, child killed, `_in_flight` False); (c) our-turn cancel →
  teardown; (d) an UNRELATED cancel (different requestId) → park PRESERVED; (e) **a WRONG-token
  `codex_approve` through `main()` → `parked turn expired` (routed to `codex_approve_v2` by name), NOT the
  busy guard, park PRESERVED (F4)**.
- [ ] **Step 2: RED.** - [ ] **Step 3:** add the guard + `_teardown_park` (auto-decline→interrupt→
  turn_completed); recognise cancel/EOF-while-parked in the dispatcher (Task 9's finite wait surfaces them).
- [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit** `feat(codex-mcp): parked guard + _teardown_park ordering (#277)`
- [ ] **Step 6: SLOW GATE.**

---

## Task 9: wall-clock cap — `_park_cap_s()` + parked-aware finite wait in `main()` (C17, P8)

**Files:** Modify `mcp/codex_server.py` (`_park_cap_s`, `main()` read loop); Test.

**Interfaces:**
- `_park_cap_s() -> float` → `float(os.environ.get("BULLDOZER_PARK_CAP_S") or _PARK_CAP_S_DEFAULT)` clamped
  (`_PARK_CAP_S_DEFAULT = 1800.0`).
- `main()`: while `tsm.is_parked()`, run a parked wait bounded by `cap_remaining` (recompute from
  `monotonic` each wake). **`CCStream.next_frame` selects ONLY on `sys.stdin` — it is BLIND to the
  child (R2-F1).** So each iteration must ALSO `reactor.pump(timeout=0.0)` (route a terminal child
  frame → `_teardown_park`) and check `manager._child.poll()`; iterate on a short slice (≤0.2 s) until
  `cap_remaining` exhausts. on cap with no resume → `_teardown_park("cap")`. **Priority each iteration:**
  CC EOF / child-death / a terminal child frame WIN over the cap (don't mislabel a death as a timeout).
  A `codex_approve` after the cap → `{"error":"parked turn expired"}`. The cap-teardown auto-decline write
  is `BrokenPipeError`-guarded (`main()`'s parked wait has no tool-call try/except).

- [ ] **Step 1: Failing test** — park, advance a fake/monotonic clock past `_park_cap_s()` with NO resume →
  teardown fires WITHOUT a fresh tools/call; later `codex_approve` → expired error; an EOF during the
  parked wait tears down first (EOF-priority); **a child that DIES / emits `turn/completed` while parked
  (no `codex_approve`) is observed via the iteration's `reactor.pump`/`_child.poll()` and torn down
  BEFORE cap, not mislabelled as a timeout (R2-F1)**; a dead-child cap write does not crash `main()`.
- [ ] **Step 2: RED.** - [ ] **Step 3:** implement the finite-timeout parked read + cap teardown +
  EOF-priority + BrokenPipeError guard. - [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit**
  `feat(codex-mcp): parked wall-clock cap + finite-wait firing (#277)` - [ ] **Step 6: SLOW GATE.**

---

## Task 10: discoverability — `codex_info "approval"` query + knob block comment (config §8)

**Files:** Modify `mcp/codex_server.py` (`_INFO_QUERY_MAP` / `codex_info_v2`, query enum); Test.

**Interfaces:** a `codex_info(query="approval")` that returns each knob's effective value + source —
`{unattended: bool+source, park_cap_s, fast_path_scope, sentinel_path}` — computed by CALLING the live
accessors (still fresh). Add a block comment near the unattended accessors listing env names + defaults +
"resolved fresh."

- [ ] **Step 1: Failing test** — `codex_info(query="approval")` returns the knob dict; monkeypatching
  `BULLDOZER_PARK_CAP_S`/`BULLDOZER_FAST_PATH_SCOPE` is reflected.
- [ ] **Step 2: RED.** - [ ] **Step 3:** add the query to the enum + `_INFO_QUERY_MAP`; implement the
  read-out. - [ ] **Step 4: GREEN.** - [ ] **Step 5: Commit** `feat(codex-mcp): codex_info approval-knobs query (#277)`.

---

## Task 11: full slow e2e + attended-unchanged guard

**Files:** Test (`@pytest.mark.slow`).

- [ ] **Step 1:** slow e2e — arm unattended; a real codex turn that triggers a NON-trivial approval →
  `codex_run` returns `awaiting_approval` → `codex_approve(accept)` → side effect happens → `turn/completed`;
  a second e2e: the `decline` path → graceful declined result. PLUS: unattended OFF → the unchanged
  `elicitation/create` path (a fake-CC driver answers → byte-identical to today).
- [ ] **Step 2:** `pytest tests/test_codex_mcp_v2.py -m slow -v` (3-5 min) → PASS.
- [ ] **Step 3:** full suite `pytest tests/test_codex_mcp_v2.py -q` → PASS.
- [ ] **Step 4: Commit** `test(codex-mcp): slow e2e for park→resume + attended-unchanged (#277)`.

---

## Task 12: docs + fingerprint coherence

**Files:** Modify `plugins/bulldozer/CLAUDE.md`; `tests/fixtures/codex-protocol-fingerprint.json` if a constant changed.

- [ ] **Step 1:** rewrite the CLAUDE.md "Unattended approval judge" bullet → routing fast-path + the
  model-in-the-loop (`codex_approve`, park/resume, the C14 single policy, the cap, the env knobs +
  `codex_info "approval"`). Add `codex_approve` to the tool inventory.
- [ ] **Step 2:** if `_KNOWN_DECISION_VARIANTS`/method lists or constants changed, update the fingerprint +
  run `test_fingerprint_matches_code_constants`. - [ ] **Step 3: Commit**
  `docs(codex-mcp): model-in-the-loop CLAUDE.md + fingerprint (#277)`.

---

## Self-Review — spec coverage

| Spec item | Task |
|---|---|
| C1 inner generator (not codex_run_v2) | 6 |
| C2 drain-on-resume (in generator) | 6 |
| C3 parked TurnStateMachine state | 2 |
| C4 routing only 5 methods; elicitation bypass | 1 (route surface) + 7 (wiring) |
| C5/C6 deadline credit + cc_id rebind | 6 |
| C7/C8/C14 parked teardown (auto-decline→interrupt→turn_completed) + single policy | 8 |
| C8b BrokenPipeError on resume write | 6; cap write 9 |
| C9 resume = codex_approve | 3 (tool) + 7 (thin impl) |
| C10 decision-response builder (frame, retryable id) | 5 |
| C11 3-way routing | 1 |
| C12 parked guard covers connection reads | 7 (guard ships with switchover, R2-F3) + 8 (teardown) |
| C13/C16 per-kind payload + fileChange source + no-patch | 4 + 5 |
| C15 codex_approve TOOLS entry | 3 |
| C17 cap (_park_cap_s, finite wait, monotonic, EOF-priority) | 9 |
| config: env accessors + codex_info "approval" | 1 (_fast_path_scope) + 9 (_park_cap_s) + 10 (query) |
| ordering (P1) | deletion/switchover in Task 7, not Task 1 |
| test harness (P7) | subprocess `with_approval` in Tasks 6-9 |

**Dependency order:** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12. Tasks 1-5 are pure additions
(no runtime change); **Task 7 is the atomic switchover**; 6-9 are the sequential core surgery. Run the
SLOW gate after Tasks 7, 8, 9, and 11 (every surgery task that changes the live turn path).

**Defaults chosen (flag at implementation):** `_fast_path_scope` = `"reads"`; amendments = the
`build_command_approval_labels` `decisions[]` ids (accept/decline minimal if confusing);
`_PARK_CAP_S_DEFAULT` = 1800s; tool name `codex_approve`.

**Review provenance:** hardened by `/consult --panel` on this plan (corrections P1-P9, now folded into the
tasks above) + the spec's own C1-C17 (panel ×1 + `/bulldozer:check` 4 rounds) + a config + holistic panel.
