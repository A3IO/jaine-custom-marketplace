# Codex MCP v2 Bridge Hardening + Surface — Design (#204)

**Goal:** Make the codex MCP v2 bridge (`mcp/codex_server.py`) a flexible, well-isolated tool: let the caller choose per-call which MCP servers/tools codex sees (a REQUIRED explicit per-call choice — no silent default), stop leaking the user's environment, expose the control + observability the caller needs, and close the class of fake-grounded divergences from real `codex app-server` — all verified against live codex 0.141.

**Architecture:** The bridge fronts a persistent `codex app-server` child and exposes one `codex_run` tool. Isolation is achieved with **per-server `-c` flags at spawn (no `CODEX_HOME` relocation)** so the user's real auth/sessions are untouched. This design changes the spawn argv builder (`_spawn_appserver`), the env passed to the child, the **manager lifecycle** (`AppServerManager.ensure`), the per-thread params (`start_thread`), the turn loop (`codex_run_v2`), the notification-allowlist source, and the test fake — without breaking the shipped invariants (#18268 approvals, happy-path-no-`_drift`, backward-compatible result shape).

**Singleton / respawn (load-bearing):** the `mcp` selection is per-call, but the `codex app-server` child is a SESSION SINGLETON and isolation argv is a PROCESS-level property. `AppServerManager` therefore tracks an *isolation signature* (`tuple(isolation_argv)`) and reuses the live child only when the signature matches; a different `mcp` selection kills and respawns the child, re-paying codex's one-time cold start (~28–80s) — same-selection calls stay warm. Resuming a `thread_id` under a different `mcp` than it was created with is legitimate: the thread rollout/history is preserved while the available tool set for that turn reflects the new selection.

**Tech Stack:** Python stdio MCP server; `codex app-server` v2 JSON-RPC; pytest (offline + `@pytest.mark.slow` live-codex e2e).

## Global Constraints

- **Empirical basis is authoritative.** Every mechanism below was verified against live codex 0.141 (probes + a primary-source research pass on `codex-rs`, recorded in #204). Do NOT regress to fake-only verification — the `@pytest.mark.slow` live-codex gate is mandatory after any `mcp/codex_server.py` change (it caught the userAgent, allowlist, timeout, and the `.enabled` metric bugs).
- **Verify isolation by TOOLS-COUNT, not name-absence.** A disabled server STILL appears in `mcpServerStatus/list` by name but with `tools: 0`. The empirical metric for "disabled" is `tools == 0` (and no helper process spawned), NOT absence from the list. (This burned the first probe.)
- **Preserve invariants:** (a) #18268 approval bridge keeps working (`{decision:...}` to app-server, dynamic `availableDecisions` labels incl. the `acceptWithExecpolicyAmendment` dict + `cancel`); (b) happy path returns NO `_drift`; (c) result-shape changes are ADDITIVE only (existing `{thread_id,verdict,findings,schema_ok}` / `{thread_id,result}` keys stay); (d) MCP/tool isolation is a **REQUIRED explicit per-call choice — no silent default** (see 1b).
- **Do NOT mutate the user's `~/.codex`** — no relocation, no `codex plugin remove`, no writing the user's `config.toml`. All isolation is via ephemeral `-c`/`--disable` argv on our spawn.
- **No manual `plugin.json` bump** (auto-calver on merge to `bulldozer/main`).
- Findings detail lives in #204 comments — this spec is the consolidated design, not a re-statement of evidence.

---

## Decision Rationale — do NOT naively revert (read before changing anything)

These are hard-won, empirically-verified choices (codex 0.141). Each = the decision, WHY, and the alternative we REJECTED and why — so a reviewer doesn't "simplify" back into a known trap.

1. **No `CODEX_HOME` relocation; disable via `-c mcp_servers.<n>.enabled=false`.** Keeps `~/.codex` auth/sessions untouched. REJECTED relocation + auth-sharing: OAuth refresh token is single-use/rotating — the backend rejects reuse with `refresh_token_reused` (→ forced re-login) and issues a NEW token on each refresh that overwrites the old one on disk, so two homes sharing one `auth.json` copy collide (source chain: `codex-rs/login/src/auth/manager.rs` — `REFRESH_TOKEN_REUSED_MESSAGE`, `classify_refresh_token_failure`, `RefreshResponse.refresh_token`, `persist_tokens`). SYMLINK races (non-atomic write, no cross-process lock); no `CODEX_AUTH_HOME`/`--auth-from` override exists in 0.141 source (grep-confirmed); shared-auth is #15410 "not planned" (issue# unverified offline, but the mechanism's ABSENCE is source-confirmed). Do NOT switch to relocation to "also strip skills/config".
2. **Disable key is `mcp_servers.<n>.enabled=false` (targeted deep-merge).** `McpServerConfig` has an `enabled: bool` field (source: `codex-rs/config/src/mcp_types.rs`), so a targeted `-c` override lands. REJECTED: `mcp_servers={}` (empty-table deep-merge = no-op — `merge_toml_values` iterates the overlay table's keys; an empty table has zero keys → base untouched, `codex-rs/config/src/merge.rs`); `.enabled=` empty (the CLI parser fails to parse `""` as TOML, falls back to a literal string `""`, which can't deserialize into `Option<bool>` → serde error blocks app-server startup); `.command="/bin/false"` (for url-type servers a hard config error `"url is not supported for stdio"`; for stdio servers it spawns+exits → `Failed` status with 0 tools, NOT cleanly `enabled:false` — still listed, may emit error events). Do NOT use a whole-table clear.
3. **`mcp` is REQUIRED, no default; `mcp="list"` discovery.** Explicit per-call choice + visibility (caller sees the available servers before choosing). Do NOT add a silent default — it would hide whether codex saw the user's tools.
4. **Verify isolation by `tools == 0`, NOT name-absence.** A disabled server STAYS in `mcpServerStatus/list` by name with `tools:0` (re-verified twice: enabled→tools:4, disabled→tools:0, name present both times). A test asserting the name is GONE gives a FALSE PASS. Do NOT assert list-membership.
5. **env ALLOWLIST (fail-closed), not denylist.** Secrets are unbounded (`*_TOKEN`/`*_KEY`/custom). Do NOT "simplify" to a denylist scrub — it leaks.
6. **computer-use REMAINS in `isolated` (documented limit).** Bundled plugin (`computer-use@openai-bundled`): `codex plugin` has subcommands `add`/`list`/`marketplace`/`remove` — **no `enable`/`disable`** (only `remove`, which MUTATES the user's `~/.codex` → forbidden). `-c plugins."computer-use@openai-bundled".enabled=false` does NOT crash and does NOT work — it's a **silent no-op** (the `.`-split key path stores the `"computer-use@openai-bundled"` segment with literal quote chars → wrong HashMap key → never matched; verified `exit 0`, plugin stays enabled). `--disable computer_use` is also a no-op: `Feature::ComputerUse` is a "requirements-only gate" that no non-test code path consults to gate plugin loading. Do NOT add `codex plugin remove` or any `~/.codex` mutation to disable it. Full-zero = the rejected relocation path or a future codex plugin-disable.
7. **No own turn-duration cap (match stock).** Stock has none (ran 136s); our 120s was a regression and wrongly counted cold-start. Opt-in `timeout` param, default none. Do NOT reinstate a hard default cap.
8. **`_KNOWN_NOTIFICATIONS` GENERATED from the protocol schema.** Hand-curation is structurally incomplete (14 vs **66** in codex 0.141; count is version-dependent → the fixture is schema-generated so the number is not load-bearing) → recurring spurious `_drift`. Do NOT just hand-add the missing names — generate from the `ServerNotification` union.
9. **Result-shape changes are ADDITIVE only.** check/consult parse the existing keys; do NOT rename/remove `thread_id`/`verdict`/`findings`/`schema_ok`/`result`.
10. **Never mutate the user's `~/.codex`** (no relocation, no `plugin remove`, no config writes). All isolation is ephemeral spawn argv.

---

## Group 1 — Isolation, made flexible (the original #204 primary)

**Why no `CODEX_HOME` relocation** (the path we deliberately reject): relocating `CODEX_HOME` would force us to handle auth in the new home, and that is the genuinely hacky part — copying `auth.json` is unsafe (the OAuth refresh token is **single-use/rotating**; a refresh by either home invalidates the other → forced re-login; confirmed in `codex-rs` source), symlinking it races (non-atomic in-place write, no cross-process lock), and OpenAI declined first-class shared-auth (#15410 "not planned"; no `CODEX_AUTH_HOME`/`--auth-from`). Per-server `-c` flags keep `CODEX_HOME=~/.codex`, so **auth, sessions, and cross-session resume are untouched** and the whole auth problem disappears.

### 1a. Disable mechanism (per-server `-c`, verified on 0.141)

- **User `config.toml` MCP servers** (e.g. dash, deepwiki): `-c mcp_servers.<name>.enabled=false` per server. Verified: the override lands in the effective config (`enabled: false`) and the server reports `tools: 0` (not initialized). NB: `-c mcp_servers={}` (clearing the whole table) is a no-op — a deep-merge of an empty table touches zero keys; only **targeted** keys merge in.
- **`apps` built-in** (the `codex_apps` server / documents·spreadsheets·presentations): `--disable apps` (= `-c features.apps=false`) — removes it.
- **`computer-use`** is a **bundled codex PLUGIN** (`computer-use@openai-bundled`; the `.mcp.json` command is the inner binary `…/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient`; ~9 tools per a live `mcpServerStatus/list` probe — re-verify after a codex upgrade), NOT a feature flag — so `--disable computer_use` does NOT disable it (`Feature::ComputerUse` is a "requirements-only gate" that no non-test code path consults to gate plugin loading). And there is **no clean ephemeral disable**: `codex plugin` has subcommands `add`/`list`/`marketplace`/`remove` — no `enable`/`disable` (only `remove` mutates `~/.codex` — forbidden); and `-c plugins."computer-use@openai-bundled".enabled=false` is a **silent no-op, NOT a crash** (the `.`-split key path stores the `"computer-use@openai-bundled"` segment WITH literal quote chars → wrong key, never matched; `exit 0`, plugin stays enabled). **Consequence:** in `isolated` mode computer-use REMAINS (documented limitation, low-risk: GUI-automation tools codex won't call on code tasks under read-only sandbox). Full-zero (incl. computer-use) is only reachable via the rejected relocation path or a future codex plugin-disable; tracked in Out-of-scope.

Enumerate the servers to disable by parsing `$CODEX_HOME/config.toml`'s `[mcp_servers.*]` keys at spawn (tomllib) — handles arbitrary user servers, not just dash/deepwiki.

**Migration (shipped → target):** the bridge currently injects a per-thread `config: {mcp_servers: {}}` (the shipped `ISOLATION_CONFIG`). Since that override is a verified no-op (decision #2), REMOVE it from `start_thread` — but KEEP the `_CONFIG_DENY` scrub that strips a caller-supplied `mcp_servers`/`mcpServers`/`baseInstructions`/`developerInstructions` from per-thread `config` (benign keys still pass through). Real MCP isolation moves entirely to the SPAWN argv (`_build_isolation_argv`); per-thread `config` is no longer an isolation surface, only a benign-passthrough surface.

### 1b. The flexible `mcp` knob — REQUIRED, with discovery

**`mcp` is a REQUIRED `codex_run` param — no default.** The caller makes an explicit choice every call, and can discover the available servers first:

| `mcp` value | Behavior | Spawn argv |
|---|---|---|
| `"list"` | **discovery — does NOT run codex**; returns `{available: [user config servers + builtins]}` so the caller re-calls with a choice | (no spawn) |
| `"isolated"` | disable all user `config.toml` MCP servers + `apps` (computer-use remains, see 1a) | `-c mcp_servers.<each>.enabled=false` + `--disable apps` |
| `"all"` | nothing disabled — codex's full normal setup (dash/deepwiki/apps/computer-use) | (no disable flags) |
| `["dash", …]` (list) | keep only the named servers; disable the rest | `-c mcp_servers.<name>.enabled=false` for every config server NOT listed; `--disable apps` unless `"apps"` is listed |

The caller (Claude) MUST pass `mcp` every call (no implicit isolate-or-not). To pick a subset it first calls `mcp="list"` (returns the user's configured servers + builtins, no codex run), then re-calls with `"isolated"` / `"all"` / a subset. `isolated` = clean review/implement; a subset = "review this code AND let codex use deepwiki for library docs"; `"all"` = autonomous implement with the full toolset. This supersedes the old "opt-in via seeded config.toml" idea — no relocation, no seeding; selection is pure spawn-argv. The tool description documents `mcp` as required + the `list` discovery mode.

### 1c. Environment isolation (secret leak) — independent of the above

**Problem:** `_spawn_appserver` inherits the parent's full env; codex shell commands read it (a canary set only in the parent surfaced via `printenv`). This is independent of `CODEX_HOME` (the app-server process inherits CC's env regardless).

**Design (two layers):**
1. **Spawn-level allowlist (primary):** pass `codex app-server` an explicit `env=` containing only what codex needs — `PATH`, `HOME`, `CODEX_HOME`, `TMPDIR`/`TEMP`/`TMP`, `USER`, `LOGNAME`, `SHELL`, `TERM`, locale (`LANG`/`LANGUAGE`/`LC_*`), **codex's own auth (`OPENAI_API_KEY`, `OPENAI_BASE_URL`)** (required when the user auths via API key rather than keychain OAuth — codex reads `OPENAI_API_KEY` via `OPENAI_API_KEY_ENV_VAR` in `login/src/auth/manager.rs`), proxy vars (`HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`/`ALL_PROXY` + lowercase), and TLS/CA (`SSL_CERT_FILE`/`SSL_CERT_DIR`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`, plus **`CODEX_CA_CERTIFICATE`** — codex's own custom-CA var that takes PRECEDENCE over `SSL_CERT_FILE`; omitting it breaks TLS for corp users on a custom CA). This inline list is **illustrative, not exhaustive** — the canonical set is `_CHILD_ENV_ALLOW_EXACT` + `_CHILD_ENV_ALLOW_PREFIX` in code (Task 2). Denylist rejected (secrets are unbounded → fail-closed: a missing codex-needed var fails functionally, never leaks).
2. **`shell_environment_policy` (defense-in-depth):** set via `-c shell_environment_policy.inherit=core` (ephemeral override, no seeded config needed) to constrain what codex's shell subprocesses inherit. `inherit=core` is codex's own minimal-safe set — on UNIX `PATH`, `SHELL`, `TMPDIR`, `TEMP`, `TMP`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `LOGNAME`, `USER` (source: `UNIX_CORE_ENV_VARS`, `codex-rs/protocol/src/shell_environment.rs`; the `Core` enum variant serializes kebab-case → `"core"`). Using codex's own Core definition (not a hand-picked list) means future codex changes are inherited automatically; it keeps implement-mode shells functional while excluding `OPENAI_*`/proxy/any unrelated CC credential from arbitrary shell commands codex runs.

Implement mode stays functional (PATH preserved). The drift log already records only `{code, detail}` (no content) — no change.

---

## Group 2 — Caller surface

### 2a. tokenUsage + per-call metadata in the result

**Problem:** `thread/tokenUsage/updated` arrives every turn but the result drops it; no model/effort/timing metadata.

**Design:** capture the final `thread/tokenUsage/updated` snapshot during the turn loop and add ADDITIVE fields to the result. **Wire reality (codex 0.141, source-confirmed `app-server-protocol/src/protocol/v2/thread.rs`):** the notification's `params` carries **`tokenUsage`** (NOT `usage`) — an object with `last` (per-turn delta) and `total` (cumulative), each a `TokenUsageBreakdown` whose keys are **camelCase**: `inputTokens`, `cachedInputTokens`, `outputTokens`, `reasoningOutputTokens`, `totalTokens`. The bridge reads `params.tokenUsage.total` and maps it into OUR result `usage` (snake_case = our additive API):
```
usage:  {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}  ← mapped from params.tokenUsage.total.<camelCase>
codex:  {model, service_tier, effort, approvals_reviewer, mcp_mode, mcp_servers_enabled}            ← from ThreadStartResponse (camelCase echoes) + resolved mcp knob
timing: {duration_ms}
status: "completed" | "failed" | ...
```
`mcp_mode`/`mcp_servers_enabled` reflect the resolved `mcp` knob (observability into what codex actually had). Existing keys (`thread_id`, `verdict`, `findings`, `schema_ok` / `result`) unchanged → check/consult parsers keep working. **Anti-divergence (3c class):** the offline fake MUST emit the real `params.tokenUsage.{last,total}` camelCase shape, and a slow test MUST assert `usage.total_tokens` is non-null against real codex — otherwise a fake-grounded reader (e.g. `params.usage` / snake_case) ships all-null usage silently. Optional `usage_events` (full progression) only behind a debug flag.

### 2b. Control knobs — first-class

**Design:** promote to first-class `codex_run` params (wired to `thread/start`): `approvalsReviewer` (`user`/`auto_review`/`guardian_subagent` — high-leverage: autonomous risk-based approval) and `serviceTier` (speed/cost). `serviceTier` is a plain nullable string on the wire (ThreadStartParams: `{"type":["string","null"]}`, no enum); the Rust config type `ServiceTier` defines `fast`/`flex` (lowercase) — the empirically safe values; the slow echo test passes `"flex"` and asserts it round-trips. **`verbosity` was DEMOTED (R1-F5 review finding) — NOT first-class:** its wire key `model_verbosity` is a Config-object property, not a ThreadStartParams field, so it cannot be a thread/start param; it has the lowest operational value of the three candidates. Callers set it via the generic `config` passthrough: `config={"model_verbosity": "low"}` (`low`/`medium`/`high`; benign, not in `_CONFIG_DENY`). Leave `summary` (reasoning summary) and `personality` also in `config` passthrough (lower operational value, style-churn risk). Document the passthrough-reachable knobs (`web_search`, `review_model`, `model_context_window`, etc. — all confirmed Config keys in 0.141) in the tool description — tool-level flexibility complements the `mcp` knob.

---

## Group 3 — Robustness fixes (from the systematic hunt)

### 3a. Turn-timeout → match stock (remove our self-imposed cap)

**Problem:** the hard `deadline = time.time() + 120.0` turn cap kills long ACTIVE turns; stock `codex mcp-server` has NO such server-side cap (empirically ran a 136s turn to completion); and cold-start (~28-80s) was charged against the same budget. A timeout is a safety net (a hung codex must not hang Claude forever) — but ours fired on legitimate work.

**Design (decision: match stock — no own turn cap):**
- **Remove the hard turn-duration cap.** The turn loop runs until `turn/completed` (or child EOF/crash), like stock — run to completion.
- **Opt-in `timeout` param (default = none/unbounded):** a caller may impose a cap if it wants one; off by default. The safety valve, without biting legit work.
- **Keep the `thread/start` setup timeout** (generous) — it detects "the engine never came up" (a SETUP failure), distinct from limiting WORK duration; without it the very first call could hang forever.
- **Residual risk is small (accepted).** Confirmed stock has NO turn-duration cap: `codex-rs/mcp-server/src/` has zero timeout/deadline refs, codex itself confirms it, and a 136s turn ran to completion. Crucially, codex's OWN `stream_idle_timeout_ms` catches a stalled model/network stream (no streaming bytes → connection treated as lost → the turn errors), so a genuinely HUNG model is NOT infinite. Only a pathological always-actively-streaming turn would run unbounded — covered by the opt-in `timeout` param (and/or any CC MCP-client timeout). This matches stock; do not re-add a hard work-duration cap to "fix" a hang that codex's stream-idle already handles.

### 3b. Schema-generated notification allowlist

**Problem:** `_KNOWN_NOTIFICATIONS` is hand-curated (~14) vs the protocol's `ServerNotification` union (**66** in codex 0.141; exact count is version-dependent — the fixture is schema-generated so the number is not load-bearing); any command/plan turn emits spurious `_drift` (confirmed: `turn/plan/updated`, `item/commandExecution/outputDelta`).

**Design:** derive the known-notification set from the authoritative protocol schema. Generate a checked-in constant from `codex app-server generate-json-schema` (the `ServerNotification` method list) via a small generator script + a checked-in `codex-notifications.json` fixture (regenerated alongside the version pin); the runtime loads it. Only methods absent from the protocol entirely emit `UNKNOWN_NOTIFICATION`. `error`/`warning` stay OUT of the benign set (they signal problems — surfaced as before). A CI test asserts the constant matches the fixture (same pattern as the fingerprint tripwire).

### 3c. Fake fidelity (meta root-cause)

**Problem:** `tests/fixtures/fake_appserver.py` undermodels real app-server — approval `availableDecisions: ["accept","decline"]` (real: `['accept', {acceptWithExecpolicyAmendment:{…}}, 'cancel']`), missing `commandActions`/`proposedExecpolicyAmendment`, phantom `approvalId`; emits only a happy-path notification subset. This is why fake-grounded divergences ship.

**Design:** align the fake's APPROVAL shape to the real schema — real `availableDecisions` shape (string + `acceptWithExecpolicyAmendment` dict + `cancel`), the real approval param keys (`commandActions`/`proposedExecpolicyAmendment`/`proposedNetworkPolicyAmendments`, no phantom `approvalId`) — and the real `thread/tokenUsage/updated` shape (2a). Add an offline test exercising the dict-variant + `cancel` approval reverse-mapping. **Scope note (spec↔plan reconciled):** the broader notification-SET (`turn/plan/updated`, `item/commandExecution/outputDelta`, `thread/started`, …) is handled at the RUNTIME layer by the schema-generated allowlist (3b) — those names are NOT additionally emitted from the fake's turn stream (out of scope this pass; the bridge has no per-notification branch beyond the allowlist membership test, so emitting them from the fake adds no coverage). Where practical, derive the fake's shapes from the same schema dump as 3b.

**Wire-fact (LOCKED — read before any recon; verified against codex 0.141 source 2026-06-20):**
`availableDecisions` is a REAL field but marked `#[experimental("item/commandExecution/requestApproval.availableDecisions")]` in codex source (`codex-rs/app-server-protocol/src/protocol/v2/item.rs`: `available_decisions: Option<Vec<CommandExecutionApprovalDecision>>`, `#[serde(default, skip_serializing_if = "Option::is_none")]`). **Consequence: `codex app-server generate-json-schema` does NOT emit it** (the schema generator excludes experimental fields) — its absence from the schema dump is NOT evidence it's unreal. codex serializes it on the wire only when the experiment populates it (→ bridge PRIMARY path `build_command_approval_labels`); when `None` it is omitted (→ bridge FALLBACK derives decisions from `command`/`commandActions`/`proposedExecpolicyAmendment`/`proposedNetworkPolicyAmendments`). The bridge MUST keep handling both — do NOT "simplify" by dropping either path because the schema dump lacks the field. Verify this field via the source `item.rs` (or a live approval probe), NEVER via `generate-json-schema`. The decision union — SIX variants: `accept` / `acceptForSession` / `{acceptWithExecpolicyAmendment}` / `{applyNetworkPolicyAmendment}` / `decline` / `cancel` — lives in `CommandExecutionRequestApprovalResponse → CommandExecutionApprovalDecision`. Note `decline` (deny the command, CONTINUE the turn) is distinct from `cancel` (interrupt the turn); the bridge reverse-map must handle both. `approvalId` IS present in the stable schema as an OPTIONAL (non-`required`) param key; codex omits it in practice, so the fake omits it too.

---

## Invariants to verify (post-implementation)

- Happy path (review + implement, with and without command execution) returns NO `_drift`.
- #18268: an approval turn through the bridge with the real `availableDecisions` (string + amendment dict + cancel) maps to correct labels and honors `{decision}` (file created on accept).
- Result backward-compat: check/consult still parse `verdict`/`findings`/`result`.
- **Isolation (by tools-count):** `mcp="isolated"` → every user `config.toml` server reports `tools: 0` and `codex_apps` is gone (computer-use remains by documented limitation); `mcp="all"` → servers load normally; `mcp=["X"]` → only X has tools. A secret in CC's env does NOT surface via `printenv` in a turn.
- **Auth untouched:** no writes to `~/.codex/auth.json` or `config.toml`; `getAuthStatus` works; the user's interactive codex is unaffected.
- Resume: a codex_run thread resumes (same `~/.codex/sessions`, unchanged).

## Testing strategy

- Offline (fast): updated fake (3c) drives the reactor; new tests for the `mcp` knob → correct argv for `isolated`/`all`/list, config.toml `[mcp_servers.*]` enumeration, env-allowlist construction, allowlist-from-schema loading, tokenUsage/metadata shaping, new param wiring, timeout-excludes-cold-start logic.
- `@pytest.mark.slow` (live codex, self-skip): `mcp="isolated"` → user servers tools:0; `mcp="all"` → servers load; `mcp=[X]` → only X; env-no-leak; tokenUsage present; approval dict-variant; a >120s turn runs to completion (no self-imposed cap; `timeout` param off by default); the opt-in `timeout` fires when set; auth-untouched (no file writes); version pin.
- Reuse the #204 probe harness patterns for the slow tests. **Assert on tools-count, not list membership.**

## Out of scope / YAGNI

- **Full-zero isolation including computer-use** — needs a codex plugin-disable mechanism (doesn't exist ephemerally) or the rejected `CODEX_HOME`-relocation path; revisit if computer-use's cold-start cost or presence proves a real problem.
- `CODEX_HOME` relocation + separate-login (the "option B" full-isolation path) — rejected for the auth complexity; documented here as the fallback if full-zero becomes required.
- `summary`/`personality` as first-class params (passthrough only).
- Surfacing `error`/`warning` as a structured terminal signal — #204 parking lot.
