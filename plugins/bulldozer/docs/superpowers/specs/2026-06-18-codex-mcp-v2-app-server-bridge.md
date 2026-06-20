# codex MCP v2 — app-server bridge (design)

**Date:** 2026-06-18 · **Status:** implemented (Tasks 1-6 merged, 2026-06-19; live e2e passes) · **Issue:** A3IO/jaine-plugins#198
**Builds on:** `2026-06-18-codex-mcp-server-design.md` (v1 wrap-exec, shipped)

## Goal

Upgrade the bulldozer codex MCP module from v1 (wrap `codex exec`, non-interactive) to
a **bidirectional bridge over `codex app-server`** that delivers, through the MCP tool,
the two interactive capabilities an MCP tool *can* carry:

1. **Correct interactive approvals** — codex requests escalation → user approves in Claude
   Code → it actually runs. (Fixes [openai/codex#18268](https://github.com/openai/codex/issues/18268)
   for the MCP-in-CC path: our server translates between MCP elicitation and app-server's
   approval, which the stock `codex mcp-server` gets wrong.)
2. **Resume** — multi-turn continuity. In-session resume is reliable WHILE the child lives
   (threads live in the persistent child). Cross-session resume (survive a CC restart) is
   **PROVEN** (Task 1 gating probe + `test_resume_by_thread_id_recalls_across_restart`):
   `thread/resume {threadId}` (by-id, the stable schema-exposed key) reloads the on-disk
   rollout and the model recalls context planted before the restart, verified against live
   codex 0.141 (by-path also recalls, but is UNSTABLE; by-history is cloud-only).

Streaming/steering are explicitly OUT — they can't cross the MCP request/response boundary;
full streaming would require a standalone app-server client (separate future project).

## Verified assumptions (all empirically confirmed 2026-06-18)

1. **app-server approval works** — direct-client probe: a write-outside-sandbox task that
   #18268 breaks succeeded through app-server after replying `{decision:"accept"}`.
2. **Thread persistence to disk works** — after killing app-server, the thread's rollout
   file (`~/.codex/sessions/.../rollout-*-<threadId>.jsonl`) exists and contains the planted
   codeword. `thread/resume {threadId}` is documented (0.140.0 schema) as "load the thread
   from disk by thread_id". (Fallbacks: resume by `path` / by `history`.)
3. **An MCP server can drive CC elicitation** — a minimal spike MCP server (`elicit-probe`)
   registered in CC, when its tool was called, issued `elicitation/create`, CC showed the
   dialog, the user clicked Accept, and `{action:"accept"}` returned to the server. This was
   the single biggest unknown; it is now de-risked.

## Architecture — bidirectional bridge

`Claude Code ↔ our MCP server ↔ codex app-server` (both legs stdio JSON-RPC).

Four components in `mcp/codex_server.py` (evolves the v1 server):

1. **MCP front (faces CC)** — handles `initialize`/`tools/list`/`tools/call` AND *initiates*
   `elicitation/create` requests to CC. New vs v1: the server sends requests to the client,
   not only responses.
2. **app-server manager (faces codex)** — lazily spawns and holds ONE persistent
   `codex app-server` child per CC session; reuses it; auto-restarts on crash;
   `thread/resume <id>` from disk for cross-session and post-crash recovery (proven — Task 1 gating probe).
3. **approval bridge** — app-server `item/commandExecution/requestApproval` /
   `item/fileChange/requestApproval` → MCP `elicitation/create` to CC → CC's
   `{action:"accept"|"decline"|"cancel"}` → translate to app-server `{decision:...}`:
   `accept`→`accept`, `decline`→`decline`, `cancel`→`cancel`. app-server ALSO accepts
   `acceptForSession` (don't-ask-again-this-session) and structured variants
   (`acceptWithExecpolicyAmendment`, `applyNetworkPolicyAmendment`) — surface
   `acceptForSession` as an elicitation option when useful and pass structured
   amendments through; never silently downgrade them to plain `accept`.
4. **event pump** — reads the app-server turn stream, accumulates the final agent message
   (+ structured output via the turn's output-schema param — see Tool schema Returns), returns it as the tool result.

## Approval-bridge message flow

```
1. CC → server:        tools/call codex_run (id=X)          [server does NOT reply yet]
2. server → app-server: thread/start | thread/resume, then turn/start
3. app-server → server: item/commandExecution/requestApproval (id=A)
4. server → CC:        elicitation/create (id=E, message=command summary)
5. CC dialog → user clicks → CC → server: {action:"accept", content:{}} (response to id=E)
6. server → app-server: response to id=A = {decision:"accept"}     ← the #18268 fix
7. app-server → server: ... turn/completed
8. server → CC:        response to id=X = {thread_id, verdict?|result, schema_ok?}
```

**Core technical requirement:** the event loop must **multiplex two input streams** via
`select`: **CC-stdin** (new tool calls AND responses to our elicitation requests) and
**app-server-stdout** (events + approval requests). v1 read one input; v2 is a small reactor
over two fds (pattern in `mcp/appserver_probe.py`, extended to two inputs). Hard requirements:
- **Framed, non-blocking reads** — never `readline()` on a raw blocking fd inside the select
  loop; keep a per-fd byte buffer, split on `\n`, parse only COMPLETE JSONL frames. A partial
  frame on one fd must not block the other.
- **Drain app-server stderr** — redirect the child's stderr to a file (or a third, drained fd).
  An undrained stderr PIPE can fill its OS buffer and deadlock the child mid-turn.

**In-flight JSON-RPC state machine (CC-stdin):** at most one turn in flight per thread.
- **Classify by SHAPE first, never by id** — bidirectional JSON-RPC keeps separate id spaces, so a
  client request and a server-initiated-request response can legally share an id value. A CC
  message is: a RESPONSE iff it has `id` + `result`/`error` and NO `method`; a REQUEST iff it has
  `method` + `id`; a NOTIFICATION iff it has `method` and no `id`. ONLY a RESPONSE whose id matches
  our pending `elicitation/create` resolves that elicitation; a REQUEST is handled by its `method`
  even if its id numerically collides with the pending elicitation id (never misread a `tools/call`
  as an approval reply).
- A new `tools/call` arriving mid-turn → reply immediately with JSON-RPC error (busy,
  `-32000`); do NOT queue (keeps the reactor simple, avoids unbounded backlog). `initialize` /
  `tools/list` are answered normally (read-only).
- Unknown/unexpected id or method → JSON-RPC error `-32601`; never silently dropped.

**Correctness / edges:**
- One `codex_run` may trigger multiple approvals → loop, match EACH by its request id.
- Decline/cancel → `{decision:"decline"|"cancel"}`; codex decides how to proceed.
- Elicitation timeout / no response from CC → safe default **decline**.
- app-server crash mid-turn → error result to CC; mark child dead; respawn next call.
- Serialize: one in-flight turn at a time per thread (concurrent `codex_run` → busy error above).

### Approval elicitation schema

app-server's `CommandExecutionApprovalDecision` is a UNION of strings AND objects:
`"accept" | "acceptForSession" | {"acceptWithExecpolicyAmendment":{…}} | {"applyNetworkPolicyAmendment":{…}} | "decline" | "cancel"`
— so a `type:string` enum CANNOT carry the object variants. The bridge maps UI **labels**, not raw
decision values:
- Build a per-request **label→decision map** from the decisions the request offers. Each offered
  decision gets a **human-readable** string LABEL: `Allow once`, `Allow for the rest of this session`,
  `Allow & always permit this command`, `Allow & always permit network access to <host> (<action>)`
  (the `(<action>)` suffix is dropped when action is empty; host-less falls back to
  `network rule #<n>`), `Don't allow`, `Cancel the turn` (permissions prompts use `Grant for this
  turn`/`Grant for this session`/`Don't grant`; legacy ReviewDecision prompts reuse
  `Allow once`/`…session`/`Don't allow`). host+action carries primary distinctness for network
  labels; `_dedupe_labels` is a last-resort numeric-suffix fallback so each label is a unique
  reverse-map key.
- `requestedSchema`: `{type:object, properties:{label:{type:string, enum:[<labels>]}}}` — the `label`
  field is **OPTIONAL** (NOT `required`; a required field made CC block its Accept button, #200);
  `message` = command/patch summary + cwd.
- **Translation:** `action:"decline"|"cancel"` (dialog dismissed) → `decline`/`cancel`.
  `action:"accept"` → look up `content.label` (a LABEL) in the map and send the EXACT
  app-server decision: a STRING for `accept`/`acceptForSession`, the full OBJECT for amendment
  variants. Empty/legacy `content` → default to plain `accept`. NEVER serialize an object decision
  as a string, NEVER downgrade an amendment to plain `accept`.
- File-change approvals (`FileChangeRequestApprovalParams`) carry `grantRoot` → surface it as a
  second schema field when present.

### Server-request coverage (no unanswered request-with-id)

app-server sends NINE server-initiated request types (`ServerRequest`); EVERY one carries an `id`
and MUST get a terminal response **in ITS OWN generated response schema** or the turn deadlocks.
The spec does NOT transcribe each shape — they live in the authoritative generated TS types
(`codex-rs/app-server-protocol/schema/typescript/`, e.g. `*RequestApprovalParams` /
`*Response`); duplicating them here would drift. Instead it fixes the handling CATEGORY per
method, and the implementation pins each exact request/response pair against those types (asserted
by per-method tests):
- **Bridge to an MCP dialog** — each maps to its OWN response type, NOT a shared `{decision}`:
  `item/commandExecution/requestApproval` (the string|object decision union above),
  `item/fileChange/requestApproval` (`{decision}` only — `grantRoot` is request/display CONTEXT,
  not a response field), `item/permissions/requestApproval` (response is a permission grant, not
  accept/decline), `item/tool/requestUserInput` (returns `ToolRequestUserInputResponse` — a
  structured answer set, NOT a `requestedSchema` passthrough), `mcpServer/elicitation/request`
  (MCP elicitation passthrough).
- **Legacy** `execCommandApproval` / `applyPatchApproval` → `ReviewDecision` (`approved`/`denied`),
  NOT v2 `accept`/`decline`.
- **Unsupported** `item/tool/call` (no dynamic tools) / `account/chatgptAuthTokens/refresh` → a
  SCHEMA-VALID failure payload or a JSON-RPC error (NEVER an empty `{}` — an invalid shape). Any
  future/unknown method → JSON-RPC error.
- INVARIANT: every request-with-`id` gets a response valid for THAT method's generated type; never
  dropped, never wrong-shaped (deadlock + protocol-error guard; mirrors Error handling).

## Lifecycle / resume / crash-recovery (Approach C)

- **Persistent child per CC session** — lazy spawn on first `codex_run`; ONE handshake:
  `initialize` with `capabilities.experimentalApi: true` (REQUIRED — experimental methods incl.
  `thread/resume` need it; omitted capabilities default to false) → `initialized` notification.
  Reused across calls. A `fake_appserver_initialize` test asserts the child receives this exact
  handshake.
- **In-session resume** — threads live in the child; `thread_id` returned to caller; passing
  it back continues that thread (fast, in-memory).
- **Cross-session resume (PROVEN)** — `thread_id` survives on disk; a fresh CC session spawns
  a fresh child and calls `thread/resume {threadId}`. The Task 1 gating probe confirmed the
  round-trip against live codex 0.141: a codeword planted before a process restart is recalled
  after it (by-id, the stable schema-exposed key; by-path also recalls but is UNSTABLE;
  by-history is cloud-only / DO-NOT-USE). No descope was needed.
- **Crash recovery** — child death detected via EOF/broken pipe; lazy respawn on next call +
  `thread/resume` of the active thread by id. Works because `thread/resume` is proven (same
  gating task); a crash mid-turn surfaces an honest terminal error to CC, then the next call
  respawns and can resume.
- **Isolation at `thread/start`** (exact `ThreadStartParams` keys): `baseInstructions: <minimal
  sterile instruction>` (REPLACES codex defaults → suppresses plugin/superpowers skills, the
  `--ignore-rules` equivalent) + `config: {…}` (config.toml-override MAP). ⚠️ **Do NOT set
  `ephemeral: true` for resumable threads** — per `Thread.ts`, ephemeral = "not materialized on
  disk", which DELETES the cross-session-resume rollout (conflict R4-F1). Resumable threads are
  NON-ephemeral (persisted) BECAUSE cross-session resume requires the on-disk rollout; persistence
  is intentional (pruning old `~/.codex/sessions` is a separate out-of-band concern). `ephemeral:
  true` is available ONLY for an explicit one-shot `codex_run` that opts OUT of resume. Isolation
  therefore comes from `baseInstructions`+`config`, NOT from ephemerality. ⚠️ app-server has NO
  direct `--ignore-user-config` flag — it loads `~/.codex/config.toml` by default; the impl plan
  decides whether to neutralize keys via `config` or accept user config, and
  `thread_start_isolation` pins the chosen set.
- **Posture precedence (in-session AND resume)** — initial posture at `thread/start`
  (`sandbox: SandboxMode`, `approvalPolicy`); per-turn overrides on `turn/start`
  (`sandboxPolicy: SandboxPolicy` — note the different field name + type — plus `approvalPolicy`,
  `cwd`, `effort`, `model`, each "for this turn and subsequent turns"); `thread/resume` takes its
  own `approvalPolicy`/`sandbox`. Precedence in all three: explicit per-call `codex_run` params
  WIN; if omitted, KEEP the thread's current posture (never silently reset to read-only).
  Unknown/duplicate `thread_id` → fail-loud error, never a silent new thread.
- **End of life** — child terminated on CC session end / server process exit.

## Tool schema (per-call posture)

`codex_run`:
- `prompt` (required)
- `mode` (default `review`) — `review` (structured `{verdict,findings}`) | `implement` (free-text)
- `sandbox` (default `read-only`) — `read-only` | `workspace-write` | `danger-full-access`
- `approval_policy` (default `on-request`) — `untrusted` | `on-failure` | `on-request` | `never`
- `effort` (default `medium`) — `low` | `medium` | `high` | `xhigh`
- `model` (optional)
- `cwd` (optional; omit → isolated tmpdir)
- `thread_id` (optional; present → resume that thread)

Returns by mode (carries over v1's structured-output contract): `review` →
`{thread_id, verdict, findings, schema_ok}` — schema enforced via `turn/start`'s `outputSchema`
field (JsonValue — confirmed in the 0.140.0 protocol schema; "Optional JSON Schema used to
constrain the final assistant message"), with a post-hoc parse of the final message only as a
fallback; `implement` → `{thread_id, result}` (free text).
No baked-in posture beyond a safe fallback (read-only) when unset — the caller (JAINE) sets
sandbox+policy+mode per call.

## Error handling

- codex binary absent → graceful error result (carried from v1).
- app-server spawn failure → error result, no crash.
- elicitation timeout / CC no-response → decline (safe).
- turn error / app-server protocol error → error result with diagnostics.
- **A REQUEST (has `id`) on EITHER leg ALWAYS gets a terminal response** — a result, or a
  JSON-RPC error / decline if unsupported (see Server-request coverage + the in-flight state
  machine). NEVER ignore a request-with-`id` — it deadlocks the waiting peer.
- Only NOTIFICATIONS (no `id`) and non-JSON garbage are logged to stderr and dropped.

## Testing strategy

- **Offline (default suite):** a FAKE app-server (scripted JSONL: emits an approval request
  then a turn/completed) drives our server; assert the server emits `elicitation/create`,
  translates the CC response to `{decision:...}`, and returns the final result. Plus handshake,
  tool-schema, multiplexing, crash-respawn (fake child exits), graceful-no-codex.
- **Live e2e (manual / dogfood):** the elicitation round-trip is already proven by the spike;
  a full live run (real app-server + real CC dialog) is validated in a CC session, not the
  default suite.
- Follows bulldozer's "every cmd ships with tests" doctrine.

## Open items / risks

- **`thread/resume` round-trip — GATING TASK — RESOLVED.** The earlier probe got no response,
  so this was the implement-first gate. Task 1 proved it: `mcp/appserver_resume_probe.py` +
  `test_resume_by_thread_id_recalls_across_restart` confirm by-id recall across a process
  restart against live codex 0.141 (by-path also works, UNSTABLE; by-history cloud-only). No
  descope needed — cross-session resume and crash recovery are delivered.
- **Version drift** — app-server is experimental, internal crates `0.0.0`, no BC guarantee;
  pin to a codex revision and regenerate the protocol schema (`codex app-server
  generate-json-schema --experimental`) per version.
- **Public dependency (carried from v1)** — bulldozer ships a codex-dependent MCP server that
  spawns per session for all users; fail-graceful mitigates; consider an env opt-in gate.
- **Two-leg reactor complexity** — the multiplexing loop is the riskiest code; keep it small,
  well-tested, and isolated from the tool logic.

## Addendum: Drift-Resilience + Param-Parity (2026-06-19)

Tasks A1–A7 (drift-resilience) and B1–B3 (param-parity) were designed and implemented in the same session. Full design: `2026-06-19-codex-drift-resilience-param-parity-design.md`.

**In brief:** the bridge now surfaces protocol drift via a `_drift` tool-result field (behavioral codes only; happy path byte-identical); captures the live codex version from the `initialize` `userAgent` and logs mismatches against `LAST_VERIFIED_CODEX_VERSION` to `~/.claude/hooks/bulldozer-codex.log` (never user-facing); detects `turn/completed` terminal-failure states cleanly (no hang); adds a `_KNOWN_NOTIFICATIONS` allowlist to avoid false drift signals on benign events; exposes `base_instructions` / `developer_instructions` / `config` params with an isolation-preserving scrub; and ships a `tests/fixtures/codex-protocol-fingerprint.json` coherence tripwire guarded by `test_fingerprint_matches_code_constants`.

## References

- v1 + findings: `2026-06-18-codex-mcp-server-design.md`
- drift-resilience + param-parity: `2026-06-19-codex-drift-resilience-param-parity-design.md`
- v1 server / tests: `mcp/codex_server.py`, `tests/test_codex_mcp.py`
- app-server PoC: `mcp/appserver_probe.py`
- elicitation spike: `/tmp/elicit-probe.py` (throwaway; proved assumption 3)
- protocol schema (0.140.0): `codex app-server generate-json-schema --experimental`
- memory: `reference_codex_mcp_vs_cli`
