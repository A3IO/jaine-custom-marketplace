# codex MCP v2 — Drift-Resilience + Param-Parity — Design

**Date:** 2026-06-19
**Status:** Approved design (brainstorming → 4× consult-panel validated → MINOR-FIXES folded in)
**Target:** `mcp/codex_server.py` (v2 app-server bridge) + `tests/test_codex_mcp_v2.py`

**Goal:** Make the codex MCP facade resilient to codex upstream protocol drift — detect it, degrade gracefully instead of hanging, and surface an early-warning signal — AND reach feature-parity with the stock `codex mcp-server` by exposing the launch knobs it has and we don't.

**Architecture:** Two independent parts in one spec, sharing one file. Surgical edits at the choke points a prior empirical verification + three informed consult-panels (codex+grok+agy reading the real code) identified. One new internal helper (`_drift_warn` + a per-call accumulator); one new CI fixture. No new runtime modules. No behavior change on the happy path.

**Validation trail (durable):**
- Verification pass (this session): read the whole file, traced every cited finding, probed the `label_map.get` default. Classified real vs by-design vs FP.
- consult-panel #1 (drift mechanisms): Approach 1 (runtime-defensive + lightweight CI) chosen over runtime-only / full-schema-diff.
- consult-panel #2 (approach validation): confirmed Approach 1; corrected safe-default, fingerprint-is-hybrid, notifications-unreliable.
- consult-panel #3 (this design): **MINOR-FIXES ×3** (codex+grok+agy). All 8 fixes below are folded in and grounded against `tests/fixtures/fake_appserver.py`.
- consult-panel #4 (version-pin decision): unanimous **Option B** — the version check is log-only forensic + a maintainer CI re-verify ritual, NOT a user-facing drift WARN (version number ≠ protocol drift). Also surfaced the empty-dict `StopIteration` latent bug (PR #201 class). Folded into A1/A2/A3/A5.

---

## Global Constraints

- **#18268 invariant (unchanged):** the chosen elicitation LABEL must reverse-map to the EXACT codex decision (string or dict); never use the elicitation `action`; never downgrade an amendment dict to a plain string. Nothing in this spec touches the reverse-map.
- **Isolation invariant:** the isolated thread must never load the user's MCP servers. Primary guarantee = the launch-level `-c mcp_servers={}` in `_spawn_appserver` (process-wide, not per-thread-bypassable). The per-thread `ISOLATION_CONFIG` is belt-and-suspenders.
- **`_drift_warn` is best-effort:** it must NEVER raise. Log mkdir/append failures are swallowed (`try/except`, stderr fallback). Logging never blocks or breaks the bridge.
- **Stable log path:** drift log → `~/.claude/hooks/bulldozer-codex.log` (per plugin-hook-logging doctrine; the plugin cache is wiped on update). Env override `BULLDOZER_CODEX_LOG` for test isolation.
- **No happy-path change:** when codex behaves exactly as 0.141, every observable output (review/implement result shape, latency, approval flow) is byte-identical to today. Drift machinery only activates on the off-nominal.
- **Python stdlib only** (zero deps, like the rest of the file).
- **TDD:** every change lands RED → GREEN with a visible failing test first.

---

## PART A — Drift-Resilience

### A0. `_drift_warn` + per-call accumulator (shared primitive)

**What:** a module-level function `_drift_warn(acc, code, detail)` that (1) appends a record to `acc` (a per-call list) and (2) best-effort writes one structured line to the stable log.

**Why a per-call accumulator, not a module global:** a module-global list would leak warnings across calls (and across a future concurrent dispatcher). The accumulator is created fresh per `codex_run_v2` invocation and threaded explicitly.

**Interface:**
```python
def _drift_warn(acc: list | None, code: str, detail: str) -> None:
    rec = {"code": code, "detail": detail}
    if acc is not None:
        acc.append(rec)
    try:
        line = f"{_now_iso()} | {code} | {detail}\n"   # _now_iso: best-effort, see note
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser("~/.claude/hooks/bulldozer-codex.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(line)
    except Exception:
        pass  # logging never breaks the bridge
```
- `acc is None` is allowed (callers without a turn context, e.g. a direct `handle_server_request` unit test) — log only.
- Codes: `UNKNOWN_SERVER_METHOD`, `UNKNOWN_NOTIFICATION`, `OUT_OF_ENUM_LABEL`, `UNKNOWN_DECISION_VARIANT` (user-facing, behavioral) + `VERSION_MISMATCH` (**log-only** — passed with `acc=None`, never enters the user-facing `_drift`; see A2/A3). (`EMPTY_OUTPUT` was considered and **cut** — see A4.)
- Timestamp: the dispatcher process is long-lived; `time.time()`/`datetime.now()` are available here (this is NOT a Workflow script). Use a small `_now_iso()` wrapper, best-effort.

**`_stamp_drift(result, acc)` helper:** attaches `result["_drift"] = acc` when `acc` is non-empty; returns `result` unchanged otherwise. Used at EVERY return path of `codex_run_v2`.

### A1. Observability for unknown server→client methods (`handle_server_request`)

**Decision (revised by panel):** do NOT synthesize a reply for a truly unknown method. A fabricated `{"decision":"decline"}` is unsafe — a future `*/requestApproval` of the `permissions` family expects `{permissions,scope}`, not `{decision}`; a wrong envelope can crash or hang codex worse than an honest error. The `endswith("/requestApproval")` heuristic is dropped.

**What:**
- `handle_server_request(msg, cc_write_fn, cc_read_fn, timeout, acc=None)` gains an `acc` param. **(R1-F2) The accumulator threads all the way down — `handle_server_request(acc) → bridge_approval(..., acc) → build_command_approval_labels(params, acc)` — each gains an `acc` param (NO module globals); each breadcrumb is emitted at the deepest level where the variant/label is actually seen.** `acc=None` (e.g. a direct unit-test call) → log-only, no append.
- Unknown method (not in `_BRIDGED_METHODS`, not in `_UNSUPPORTED_METHODS`): `_drift_warn(acc, "UNKNOWN_SERVER_METHOD", method)` then return `-32601` as today.
- `_UNSUPPORTED_METHODS` path unchanged (known non-approval → `-32601`, correct).
- In `build_command_approval_labels`, the unknown-kind branch (`f"{kind}:{i}"`) and the `label_map.get(chosen, "accept")` out-of-enum default each emit a drift breadcrumb (`UNKNOWN_DECISION_VARIANT` / `OUT_OF_ENUM_LABEL`) — decision values still preserved verbatim (no #18268 change).
- **Latent-bug fix (found by panel #4, same class as PR #201):** the dict-entry branch does `kind = next(iter(entry))`, which raises `StopIteration` on an EMPTY dict `{}` (an empty dict passes `isinstance(entry, dict)` but has no first key). Guard it: treat an empty dict as a malformed entry → skip (matches the existing "skip malformed entries" comment) and emit `UNKNOWN_DECISION_VARIANT`. We are already editing this function for the breadcrumbs, so the guard rides along.

**Value:** the hang risk for a genuinely-new method type becomes *observable* (WARN + `_drift`), which is the deliverable. The honest `-32601` + the 120 s turn backstop bound the failure; the breadcrumb tells the maintainer to add a real handler.

### A2. Passive version-capture (`_do_initialize` + `AppServerManager`)

**Decision (revised by panel #4): version is a MAINTAINER/forensic signal, NOT a user-facing drift WARN.** A codex version number changing ≠ protocol drift; minor bumps are usually wire-compatible, so surfacing a version mismatch per-call to the end user is pure FP-fatigue. Real runtime drift is caught by the version-INDEPENDENT behavioral signals (A1/A4). Version capture earns its place only as (1) forensic log context and (2) the CI re-verify ritual anchor (A5).

**What:**
- `_do_initialize` currently checks the initialize response for non-None then discards it. Instead, defensively read the version from the initialize response's `userAgent` via a `codex/`-ANCHORED parse (verified: `fake_appserver`'s initialize response emits `userAgent: "codex/fake-0.141.0"` and **NO `cliVersion`** — `cliVersion` lives in the later `thread/start` thread object, NOT the initialize handshake; capture happens at initialize, so `userAgent` is the only source there). Never a bare `(\d+)\.(\d+)` (would match an unrelated number in a longer userAgent, e.g. `Agent/1.0 Codex/0.141`). Never raises on shape change. (If a real codex initialize ever omits `userAgent`, version stays unknown → logged, never fatal.)
- Compare the parsed major.minor against module constant `LAST_VERIFIED_CODEX_VERSION = "0.141"` (renamed from `EXPECTED_*` — it is "last verified against" metadata, not a runtime compatibility gate; patch ignored). On mismatch (or unparseable), `_drift_warn(None, "VERSION_MISMATCH", f"last-verified {LAST_VERIFIED_CODEX_VERSION}, live {raw!r}")` — **`acc=None` → log-only, never user-facing**. Also store the raw live version on the manager (`self._codex_version`) for forensic context. NEVER raise, NEVER fail startup.
- `codex_run_v2` does NOT stamp version mismatch into the call's user-facing `_drift`. (Earlier draft did; removed.)

**`LAST_VERIFIED_CODEX_VERSION`** is the single source of truth; the A5 slow-e2e asserts the live version against it (the maintainer-facing ritual). Future-better-anchor (out of scope, needs verification): if the codex app-server initialize handshake exposes a stable `protocolVersion` (changes only on protocol breaks, not every release), that would be a lower-churn anchor than the CLI version — note for a follow-up once confirmed it exists.

### A3. Drift surfacing via tool-result field

**Decision (verified):** Claude Code silently drops MCP `notifications/message` (GitHub #3174, closed "Not planned"; locally: 0 occurrences across all MCP logs; our server declares only `{"tools":{}}`). The tool's return value is the only reliable client-visible channel.

**What:**
- Accumulate drift into the per-call `acc`.
- Every `codex_run_v2` return path passes through `_stamp_drift(result, acc)` → adds `"_drift": [...]` when non-empty, absent otherwise.
- This includes ALL early returns: busy-guard, no-codex, `thread/resume` fail, unknown-thread, `thread/start` fail, `turn/start` error, turn timeout, eof. (So a BEHAVIORAL breadcrumb recorded mid-turn — e.g. an `UNKNOWN_SERVER_METHOD` during an approval — isn't lost when the turn then errors out. Version mismatch is NOT among these — it is log-only per A2.)
- The user-facing `_drift` carries ONLY behavioral codes (`UNKNOWN_SERVER_METHOD`, `UNKNOWN_NOTIFICATION`, `UNKNOWN_DECISION_VARIANT`, `OUT_OF_ENUM_LABEL`); `VERSION_MISMATCH` never appears here (log-only).
- Tool `description` updated: "Returns a `_drift` array if upstream codex protocol drift is detected."

**Channel note (panel-confirmed):** the dispatcher returns the tool result as `{"content":[{"type":"text","text":json.dumps(res)}]}` — `_drift` rides inside that JSON text exactly like `verdict`/`findings`/`result` already do. Claude parses the JSON and sees `_drift`; it is as visible as every other result field (same channel, not "buried"). No structured-field plumbing change needed.

**Schema safety (confirmed by panel):** `REVIEW_SCHEMA` constrains only codex's emitted text (parsed in `_shape_result`); `_drift` is added by the facade to the OUTER result dict after `_shape_result`. No interaction. `_shape_result` is extended to thread an optional drift list through both `schema_ok=True` and `schema_ok=False` review returns and the implement return. (Caveat documented: an external consumer that hard-validates the review payload as exact-schema would need to tolerate the extra key — acceptable; the payload was never a published exact-schema.)

### A4. Pump-loop terminal-failure detection (`codex_run_v2` Phase 2)

**Grounded shape** (`tests/fixtures/fake_appserver.py`): failure arrives as `turn/completed` with `params.turn.status == "failed"` (success = `"completed"`). There is NO separate `turn/failed` notification. So detection lives in the existing `turn/completed` arm — no guessing.

**What:**
- In the `turn/completed` arm, inspect `t = frame.get("params", {}).get("turn", {})`. If `t.get("status")` not in `{"completed", "success"}` OR `t.get("error")` is truthy → `state_machine.turn_completed()`, `_drift_warn(acc, ...)` is NOT used here (a failed turn is not protocol *drift*) — return a clean `_stamp_drift({"error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}", "thread_id": thread_id}, acc)` instead of `_shape_result`. `turn/completed` is ALWAYS terminal.
- Maintain a `_KNOWN_NOTIFICATIONS` allowlist of benign events the bridge deliberately ignores (at minimum `item/agentMessage/delta`, `item/completed`, `turn/completed`; add `turn/started` / `item/started` and any others observed against `fake_appserver` + a live e2e capture before shipping). A notification whose method is NOT in this set → `_drift_warn(acc, "UNKNOWN_NOTIFICATION", method)` and continue (NOT terminal — avoids truncating a healthy turn on a new non-terminal event). **CRITICAL (R1-F1):** `item/completed` MUST be in the allowlist — the happy-path fake (and real codex) emits it between the delta and `turn/completed`, so a naive "anything but delta/turn-completed is unknown" rule would stamp a spurious `_drift` on EVERY healthy turn. The allowlist (not a 2-item exclusion) is the fix.
- Keep the 120 s deadline as the ultimate backstop.

**`EMPTY_OUTPUT` cut:** considered, removed. It is FP-prone (codex may legitimately emit zero text, e.g. silent tool execution) and redundant in review mode (empty/unparseable already yields `schema_ok=False`). The delta→`item/completed` contract shift it was meant to catch is monitored behaviorally by the A5 slow-e2e instead.

### A5. CI: hybrid fingerprint + behavioral e2e

Three layers, with honest framing about what each catches:

1. **Committed fixture** `tests/fixtures/codex-protocol-fingerprint.json` — curated static facts: `bridged_methods`, `unsupported_methods`, `command_decision_variants`, `turn_start_params`, `last_verified_codex_version`.
2. **Offline coherence tripwire** — asserts the fixture matches the code constants (`_BRIDGED_METHODS`, `_UNSUPPORTED_METHODS`, the decision variants `_is_valid_command_decision` accepts, `LAST_VERIFIED_CODEX_VERSION`). **Framed explicitly as a "code↔fingerprint coherence" guard, NOT codex-drift detection** — it only fails when someone edits the code's method sets without updating the fixture (a one-sided-edit tripwire). It detects ZERO real upstream drift. (Runs in the default offline suite — it never touches codex, so it is not a PR nuisance.)
3. **Slow-e2e (`@pytest.mark.slow`)** — spawns real codex, captures the live version, asserts the `LAST_VERIFIED_CODEX_VERSION` prefix. **This is the maintainer-facing codex-drift signal / re-verify ritual.** Failure mode (panel #4): it is `@pytest.mark.slow` and self-skips when codex is absent, so it is **already excluded from the default PR run** — it does NOT red-CI ordinary feature PRs. When run against live codex (a maintainer run / scheduled / pre-release job) a version mismatch HARD-FAILS = the deliberate "codex bumped → re-verify behavioral e2e → bump `LAST_VERIFIED_CODEX_VERSION` + fixture" prompt. (A soft warn-only here would defeat the ritual; the point is a conscious re-verification, not a blocker on unrelated PRs.) Plus the EXISTING review/implement slow-e2e exercise the live protocol — they catch **intra-method FIELD drift** (a new required field inside an already-bridged approval's params) behaviorally (empty output / error → test fails), which the name-fingerprint cannot.

**Documented limitation:** intra-method field drift is caught ONLY by behavioral slow-e2e, never by the fixture. The fixture catches method-NAME / variant-NAME / version drift coherence only.

---

## PART B — Param-Parity

Reach parity with the stock `codex mcp-server`'s launch knobs we don't expose. (`compact-prompt` is deliberately omitted — it is not an app-server `thread/start` param; confirmed against the 0.141 schema snapshot.)

### B1. Expose `base_instructions` + `developer_instructions`

**What:**
- Add `base_instructions` (string) and `developer_instructions` (string) to the `codex_run` `inputSchema`.
- Thread to `start_thread` → `thread/start` params. **None-sentinel discipline** (because `""` is a valid, if degenerate, instruction): `base_instructions is None` → use `STERILE_INSTRUCTIONS` default; any string (incl. `""`) → use caller's verbatim.
- `developer_instructions is not None` → set `thread/start` wire key `developerInstructions` (camelCase, matching the existing `baseInstructions`).
- **NEW threads only.** On resume, posture is the thread's existing one; instructions are a thread-start concept and are not re-sent on `turn/start`.
- Hard isolation (`mcp_servers={}`) is unaffected — it lives in config, independent of instructions.

### B2. Expose `config` with isolation-preserving merge

**What:**
- Add `config` (object) to the `inputSchema`.
- Merge in `start_thread`: start from caller config, **scrub the isolation-sensitive key family**, then apply `ISOLATION_CONFIG` last:
  ```python
  DENY = {"mcp_servers", "mcpServers",
          "baseInstructions", "base_instructions",
          "developerInstructions", "developer_instructions"}  # R1-F3: dev-instr smuggling vector too
  merged = {k: v for k, v in (caller_config or {}).items() if k not in DENY}
  merged.update(ISOLATION_CONFIG)   # our keys win
  ```
  - Scrubs `mcp_servers` AND the camelCase alias `mcpServers` (a caller can't smuggle MCP servers back in).
  - Scrubs `baseInstructions`/`base_instructions` AND `developerInstructions`/`developer_instructions` from config (instruction-smuggling: a caller could otherwise nest an instruction key in config and bypass the dedicated B1 handling / sterile default if app-server prioritizes config over the top-level `thread/start` params).
- **Primary guarantee remains the launch-level `-c mcp_servers={}`** (process-wide); the scrub+merge is the per-thread belt-and-suspenders.
- **NEW threads only** (resume does not take a full config).
- Invariant stated narrowly: "caller config cannot override `mcp_servers`/`mcpServers` or smuggle base/developer instructions via this merge."

---

## Error Handling

- `_drift_warn` / log I/O: best-effort, never raises (try/except + stderr fallback).
- Version-capture: defensive nav, never raises, never fails startup.
- Terminal-failure (A4): returns a clean `{"error": ...}` (drift-stamped) — distinguishable from a timeout (different message).
- Safe-default (A1): no synthesis; honest `-32601` + observability + 120 s backstop.
- config-merge (B2): scrub guarantees isolation regardless of caller input shape (known key family); unknown alternate vectors are bounded by the launch-level flag.

## Testing (TDD, all RED→GREEN)

Offline unit/structural (default run):
- `_drift_warn`: appends to acc; writes to `BULLDOZER_CODEX_LOG`; never raises on unwritable path.
- `_stamp_drift`: attaches `_drift` iff non-empty.
- A1: unknown method → `UNKNOWN_SERVER_METHOD` recorded + `-32601` returned; `_UNSUPPORTED_METHODS` unchanged; out-of-enum label → `OUT_OF_ENUM_LABEL` + verbatim decision preserved; unknown decision kind → `UNKNOWN_DECISION_VARIANT` + verbatim preserved; **empty-dict `availableDecisions` entry `{}` → skipped, no `StopIteration`** (regression test for the panel-found latent bug).
- A2: version parse is `codex/`-anchored on the initialize `userAgent` (no `cliVersion` in the initialize handshake); a longer userAgent with an unrelated number (`Agent/1.0 Codex/0.141`) parses to `0.141` not `1.0`; mismatch → log-only line, **NOT** in the returned `_drift`; unparseable → log-only, no raise; matching version → no drift, no log.
- A3: every early-return path stamps `_drift` when acc has BEHAVIORAL codes; review (schema_ok True/False) + implement carry `_drift`; absent when no drift; `VERSION_MISMATCH` never appears in `_drift`; REVIEW_SCHEMA path still returns verdict/findings.
- A4: `turn/completed status=failed` → clean error (not `_shape_result`); `status=completed` → normal result; unknown notification → `UNKNOWN_NOTIFICATION` + continue (no truncation).
- A5: offline fixture↔constants coherence test (named/documented as coherence, not drift).
- B1: `base_instructions=None` → STERILE; `""` → empty caller; string → caller; `developer_instructions` → `developerInstructions` wire key; resume does not re-send.
- B2: caller `mcp_servers`/`mcpServers`/`baseInstructions`/`base_instructions`/`developerInstructions`/`developer_instructions` all scrubbed from config; ISOLATION_CONFIG wins; other caller keys pass through.
- **Tool schema (R1-F4, B1/B2/A3): `tools/list` exposes `base_instructions`, `developer_instructions`, `config` in `TOOLS[0].inputSchema`, and the tool `description` mentions `_drift`** — a regression test against `TOOLS[0]` so a future edit can't silently drop a parity field or the drift mention.

Slow-e2e (`@pytest.mark.slow`, self-skip without codex):
- A2/A5: live codex version matches `LAST_VERIFIED_CODEX_VERSION` prefix (maintainer ritual; slow-only, self-skips without codex).
- Existing review/implement e2e remain the behavioral intra-method-field-drift catch.

## Out of Scope (documented, not bugs)

- **Active runtime schema negotiation** — rejected (FP-fatigue; app-server exposes no stable schema surface).
- **`compact-prompt`** — not an app-server param.
- **`_pump_until` same-batch frame drop** — by-design, documented; harmless today.
- **`read_correlated` cancellation handling** — enhancement; bounded by the 300 s elicitation timeout.
- **Synchronous-approval stdout-flood deadlock** — theoretical (requires codex to flood >64 KB stdout while an approval is outstanding, which it doesn't); documented known-edge.
- **Caller cannot disable isolation** — invariant, not a limitation.
- **Param-parity on resume** — instructions/config apply to NEW threads only.

## File-Level Plan (for writing-plans)

All edits in `mcp/codex_server.py` unless noted:
- Add `_drift_warn`, `_stamp_drift`, `_now_iso`, `LAST_VERIFIED_CODEX_VERSION`, `_KNOWN_NOTIFICATIONS`.
- `_do_initialize` + `AppServerManager.__init__`: capture live codex version (`codex/`-anchored parse of the initialize `userAgent`) → `self._codex_version`; version mismatch → `_drift_warn(None, "VERSION_MISMATCH", …)` (log-only).
- `handle_server_request`: `acc` param + `UNKNOWN_SERVER_METHOD` breadcrumb.
- `build_command_approval_labels` / `bridge_approval`: gain `acc` param (threaded from `handle_server_request`); `UNKNOWN_DECISION_VARIANT` / `OUT_OF_ENUM_LABEL` breadcrumbs (no decision change) + empty-dict-entry `StopIteration` guard.
- `start_thread`: `base_instructions` None-sentinel, `developer_instructions`, scrub+merge `config`.
- `codex_run_v2`: per-call `acc`; `_stamp_drift` (behavioral codes only) on all returns; A4 terminal-status inspection + `_KNOWN_NOTIFICATIONS` allowlist → `UNKNOWN_NOTIFICATION`; new tool inputSchema fields wired (new-thread only). (Version mismatch is captured log-only in `_do_initialize` — NEVER stamped into the call's `_drift`.)
- `_shape_result`: thread optional drift through all shapes.
- `TOOLS[0]` inputSchema + description: add `base_instructions`, `developer_instructions`, `config`; mention `_drift`.
- New: `tests/fixtures/codex-protocol-fingerprint.json` + tests in `tests/test_codex_mcp_v2.py`.
- Docs: update `plugins/bulldozer/CLAUDE.md` codex-MCP section + the v2 app-server spec.
