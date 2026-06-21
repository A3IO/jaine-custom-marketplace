# codex MCP bridge — surface additions (codex_info + codex_review + error handling)

**Date:** 2026-06-21
**Branch:** `bulldozer/feat/codex-mcp-info-review`
**Builds on:** #204 / PR #215 (v2 app-server bridge), spec `2026-06-20-codex-mcp-bridge-hardening-design.md`
**Codex:** 0.141 (all wire-facts below verified live this session via raw `codex app-server` JSON-RPC probes)

## Goal

Expose more of the `codex app-server` surface through the bridge, cheaply and verified:
1. **`codex_info`** — connection-level read methods (discovery/introspection), one tool.
2. **`codex_review`** — codex's NATIVE git-aware review (`review/start`), the one capability neither stock `codex mcp-server` nor our `codex_run` has.
3. **`error` notification handling** — distinguish transient retries from terminal failures (discovered-necessary while probing review; also fixes spurious `_drift` in `codex_run` and closes the #204 parking-lot item).
4. **Doc fix (Q1)** — document the `config` passthrough keys in the tool schema (spec'd in #204 §95, never shipped).

Non-goal: `turn/interrupt`/`turn/steer` (→ #218, dispatcher rework), `model/list` write-side, plan/memory modes.

## Verified wire-facts (codex 0.141, LOCKED — probed live this session)

**F1 — connection-level reads answer WITHOUT `thread/start` (no cold-start).** All eight return clean structured JSON in <1s (except `mcpServerStatus/list` ~5.8s — it pings servers):

| method | params | result top-keys (real) |
|---|---|---|
| `model/list` | `{}` | `{data:[{id,model,displayName,…}], nextCursor}` |
| `getAuthStatus` | `{}` | `{authMethod:"chatgpt", authToken, requiresOpenaiAuth}` |
| `config/read` | `{}` | `{config:{model, review_model, web_search, model_provider, approvals_reviewer, model_context_window, …}, origins}` |
| `account/rateLimits/read` | none | `{rateLimits:{primary:{usedPercent,resetsAt}, secondary, credits, planType}, …}` |
| `account/usage/read` | none | `{summary:{lifetimeTokens,…}, dailyUsageBuckets}` |
| `mcpServerStatus/list` | `{}` | `{data:[{name, serverInfo, tools, …}], nextCursor}` |
| `experimentalFeature/list` | `{}` | `{data:[{name, stage, enabled, …}], nextCursor}` |
| `permissionProfile/list` | `{}` | `{data:[{id:":read-only"/":workspace"/":danger-full-access"}], nextCursor}` |

**F2 — `thread/start` result shape is `result.thread.id`** (NOT top-level `threadId`). The existing `start_thread` already extracts via the `.thread.id` path.

**F3 — `review/start` is a TURN-starter.** `ReviewStartParams = {threadId, target, delivery?}`:
- `target` (ReviewTarget): `{type:"uncommittedChanges"}` | `{type:"baseBranch", branch}` | `{type:"commit", sha, title?}` | `{type:"custom", instructions}`.
- `delivery`: `"inline"` (default, runs on the given thread) | `"detached"` (new thread; id in `reviewThreadId`).
- Response `ReviewStartResponse = {turn, reviewThreadId}` — then the review runs as a normal turn that streams events.

**F4 — review OUTPUT is in `item/completed`, NOT `item/agentMessage/delta`.** The review turn emits `item/completed` items of type `enteredReviewMode` (`{review}`), `userMessage` (the auto-prompt "Review the current code changes … provide prioritized findings"), `reasoning`, and finally **`{type:"agentMessage", id, text, …}`** — the findings live in that item's `.text` (`ThreadItem` union, schema-confirmed). A deltas-only pump (what `codex_run` uses) returns EMPTY for a review.

**F5 — transient stream errors arrive as `method=="error"` with `willRetry:true`.** Real payload: `{error:{message:"Reconnecting... 2/5", codexErrorInfo:{responseStreamDisconnected:{…}}, additionalDetails:"request timed out"}, willRetry:true, threadId, turnId}`. Codex retries on its own; the turn is NOT dead. Currently `error` is excluded from `_KNOWN_NOTIFICATIONS` (#204) → the pump routes it to `UNKNOWN_NOTIFICATION` `_drift` → **every transient reconnect spams `_drift`** (FP). Review is stream-heavy → hits this constantly.

## Design

### Item 1 — `config` passthrough documentation (Q1)
The `config` param is `{"type":"object"}` with no description; the tool description never lists the passthrough keys, despite #204 §95 requiring it. Add a `description` to the `config` param enumerating the confirmed-0.141 keys (`model_verbosity`, `web_search`, `review_model`, `model_context_window`, `model_auto_compact_token_limit`, `compact_prompt`, `model_reasoning_summary`, `personality`) and noting they are benign passthrough (not in `_CONFIG_DENY`).
**Test:** offline — `TOOLS` schema's `config.description` mentions the key set.

### Item 2 — `codex_info` tool
New tool `codex_info`, single required param `query` (enum):
`models | auth | config | limits | usage | servers | features | profiles` → mapped to the F1 methods.
- Implementation: `AppServerManager.connection_request(method, params)` — ensure a live child, send the request, `_pump_until` the matching-id response, return `result` (drift-stamped via the existing accumulator). These are connection-level → independent of isolation; **reuse the live child if any (do NOT change the isolation signature** — avoids disrupting a warm `codex_run` session); spawn a default only if none alive.
- `servers` reflects the current connection's isolation (documented).
- Result: `{query, result, _drift?}` (additive; `result` = the raw method result).
- **`config` projection (consult MINOR-FIXES, 2026-06-21):** raw `config/read` is ~71K (the `origins` map is 37K + `projects`/`tui`/`hooks`/`marketplaces` sections) → blows the MCP token limit, unreadable inline. So `query="config"` returns a **whitelist projection** of operational knobs (`_CONFIG_OPERATIONAL_KEYS`: model/review_model/web_search/model_provider/approvals_reviewer/model_context_window/model_reasoning_effort/model_reasoning_summary/model_verbosity/sandbox_mode/approval_policy/model_auto_compact_token_limit) PLUS `omitted` = the other top-level config key names (so new codex keys are **visible, not silently hidden** — the whitelist's hole, closed per consult), and DROPS `origins` entirely. Not "full config". Pinned by `test_codex_info_config_is_compact_projection`. (Consult also suggested a `config_full` escape hatch — declined as YAGNI; it would reintroduce the 71K blowout. A targeted per-section query is future work if needed.)
**Tests:** offline (fake replies per query → correct mapping/shape; config projection keeps whitelist + lists omitted + drops origins); **slow** (live: each query returns the F1 top-keys; config result is compact, no `origins`, has `omitted`).

### Item 3 — `codex_review` tool
New tool `codex_review`. Params: `target` (string `"uncommitted"` default | `"branch:<name>"` | `"commit:<sha>"` | `"custom:<instructions>"` — parsed to the ReviewTarget union), `cwd` (the repo), `model?`, `effort?`, `mcp` (REQUIRED, same as codex_run), `sandbox` (default `read-only`). `delivery` fixed `"inline"`.
- Flow: `thread/start {cwd, sandbox, ephemeral}` → `review/start {threadId, target, delivery:"inline"}` → pump the turn collecting `item/completed` where `item.type=="agentMessage"` → join `.text`. Terminal on `turn/completed` (status) or terminal error (Item 4).
- Result: `{thread_id, review, status, usage?, codex?, timing?, _drift?}` (free-text `review`, mirrors implement-mode shaping).
**Tests:** offline (fake review-flow emitting enteredReviewMode + agentMessage item/completed → `review` populated); **slow** (live review of a tmp git repo with a planted bug → `review` non-empty, mentions the bug).

### Item 4 — `error` notification handling (shared pump)
In the turn-pump, handle `method=="error"` explicitly BEFORE the `UNKNOWN_NOTIFICATION` fallback:
- `params.willRetry == true` → transient: do NOT `_drift`; increment a local `retries` counter (surface count in `timing`/meta, optional). Continue.
- else (terminal / `willRetry==false`) → capture `params.error` and treat as a turn failure: return a clean `{error: <message>, …meta}` (drift-stamped), NOT `UNKNOWN_NOTIFICATION`.
This fixes the FP `_drift` for `codex_run` too and is the #204 parking-lot "structured error signal".
**Tests:** offline — willRetry:true error → result has NO `UNKNOWN_NOTIFICATION` drift; terminal error → result `{error}` carries the codex message.

## Testing strategy
- Mandatory `pytest -m slow` (live codex, self-skip) after every `mcp/codex_server.py` change.
- New offline tests in `tests/test_codex_mcp_v2.py`; new slow e2e for codex_info (each query) + codex_review (tmp-repo planted bug) + error handling (fake).
- Fingerprint coherence: if new bridged methods are added to code constants, update `tests/fixtures/codex-protocol-fingerprint.json`.

## Out of scope
- `turn/interrupt` / `turn/steer` → #218 (dispatcher rework).
- plan mode (`CollaborationMode`), `ThreadMemoryMode`, `modelProvider` → deferred (Lower; document in #204-era out-of-scope).
- `detached` review delivery (YAGNI for now; inline covers the use case).

## Post-review hardening (`/code-review` xhigh, 2026-06-21)

A recall-mode `/code-review` (10 finder angles → verify → sweep) ran on the diff; 19 raw findings deduped to these REAL fixes (5 were false positives — `prompt`-required is the documented placeholder; `result→review` rename is correct on the error path; `effort`-default is subsumed by #12; `approvals_reviewer` IS forwarded via `dict(args)`; `_review_target` injection has no real boundary, review is read-only).

- **#12 (HIGH) — `codex_review` silently ignored `effort` AND `model`.** `ReviewStartParams = {threadId, target, delivery}` carries neither, `start_thread` sends neither, and the review path sends `review/start` INSTEAD of `turn/start` (so turn-level `effort`/`model` never reach the wire). **Fix:** `codex_review_v2` routes `effort`→`config.model_reasoning_effort` and `model`→`config.model` (the thread `config` start_thread DOES send; both are real codex config keys, not in `_CONFIG_DENY`). **Honesty note:** the prior verification session's "effort reaches the wire (low 29.9s vs xhigh 41.8s)" A/B was INVALID — both ran at the codex config default (`xhigh`); the duration delta was noise. Offline test proves the keys are now SENT; codex honoring thread `config` is its documented contract (no claim of observed reasoning-depth change).
- **#4 (MED) — terminal `error` arriving pre-ACK (Phase 1) was dropped** → masked as a generic "response timed out". Fix: Phase 1 now surfaces a terminal `error` (transient `willRetry` still ignored).
- **#18 — null-safe text:** a present-but-null `delta`/`text` returned `None` from `.get(k, "")` → `TypeError` on `"".join`. Fix: `or ""`.
- **#1/#5/#13 — "codex error: None"** when an `error` notification lacks the `error` key → `emsg or "unknown error"`.
- **#2/#9 — misleading "turn/start …" message** for `review/start` ACK-timeout / error → `start_method` variable.
- **#6 — `_project_config` fail-OPEN `return raw`** re-introduced the ~71K blowout on an unexpected shape → fail-CLOSED to a small marker.
- **#16 — `codex_info` no-codex guard `if not CODEX`** never fired (`CODEX` is a truthy path string) → filesystem check, matching `codex_run_v2`.
- **#3 (doc) / #15 (timeout 20→30s for slow `servers` read).**
