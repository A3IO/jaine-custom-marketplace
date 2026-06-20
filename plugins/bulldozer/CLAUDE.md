# Bulldozer Plugin

Adversarial review (`/bulldozer:check`) + visual browser verification (`/bulldozer:look`) + agent-driven product testing (`/bulldozer:drive`) + lightweight design consultation (`/bulldozer:consult`) + multi-agent Workflow orchestration doctrine (`workflow-swarms`).

## Skills

| Skill | Command | What it does |
|-------|---------|-------------|
| check | `/bulldozer:check` | Adversarial review loop with external AI reviewer (model selection → `-c` reasoning overrides → structured ledger). For artifacts on disk. |
| look | `/bulldozer:look [URL] [task description]` | Browser automation via CDP, AppleScript, macOS native |
| drive | `/bulldozer:drive [URL] [test task]` | Product testing on isolated Chrome-for-Testing lanes with a verify-core (navigate-wait, console-gate, stability-window assert, trusted clicks, bound screenshots); autonomous / co-pilot modes; opt-in cookie-seed auth. SP2 (#164 epic). |
| consult | `/bulldozer:consult [design question]` · opt-in `--panel [--repo PATH] [--verdict]` | Lightweight stateless codex consultation for abstract design Q&A (inline text, no artifacts), process-isolated from an empty tmpdir. Opt-in `--panel` runs codex+grok+agy in parallel for multi-model find-holes (`scripts/consult_panel.py`); `--repo` lets them read real code (informed review). |
| workflow-swarms | (doctrine skill — auto-triggers on "build a workflow" / "fan out agents" / ultracode) | Per-role model routing (haiku breadth / sonnet judge / explicit opus synth) + throttling doctrine for multi-agent Workflow-tool swarms, and the recall-preserving swarm→rank→verify-all find-holes pattern with a bounded stopping-rule. Ships WITH a matching **`require-workflow-skill`** PreToolUse(Workflow) guardrail hook (`hooks/require-workflow-skill.py` + `hooks/hooks.json`, `${CLAUDE_PLUGIN_ROOT}`-portable) — **ADVISORY by default** (injects the doctrine on every Workflow call); **DENY is OPT-IN** via the `BULLDOZER_ENFORCE_WORKFLOW_ROUTING` env var, so installing bulldozer never surprise-blocks a consumer's workflows (panel verdict 2026-06-14: an opinionated heuristic guardrail must not be imposed by default). |

## Architecture: /look

`cdp.py` — CDP commands across 3 communication channels:

| Channel | When | Commands |
|---------|------|----------|
| CDP WebSocket | websocket-client available (bundled) | All 18 |
| AppleScript + DOM injection | websocket missing | js, title, click, fill, wait, navigate, reload, viewport |
| macOS native | screenshot without websocket | screenshot only |

JAINE Browser = separate Chrome instance on CDP port 9333.

**Lanes (/look-v2, 2026-06):** `launch.sh` is parameterized via `CDP_PORT` / `LOOK_PROFILE_DIR` / `LOOK_HEADLESS` — the daily browser (port 9333, daily profile) and isolated test lanes (non-9333 port + own profile dir) coexist without fighting over state. Opt-in `--insecure` (or `LOOK_INSECURE=1`) adds `--disable-web-security` for trusted-LAN / `file://` testing, gated **fail-closed to isolated lanes only**: non-9333 port + explicit non-default `LOOK_PROFILE_DIR` (realpath-canonicalized, alias-proof). Full contract: `skills/look/SKILL.md` → "Web-security lane". **Automation lane (SP1, #164):** opt-in `--automation` / `LOOK_AUTOMATION=1` — pinned Chrome for Testing binary by default (`/0/.jaine/.browser/cft/current`, installed/pinned ONLY by `skills/look/scripts/update-cft.sh`), `--enable-automation` + `--use-mock-keychain`, temp per-port drive-profile (`$TMPDIR/jaine-drive-<port>`), gated **fail-closed**: non-9333 port + profile that does not resolve to the daily profile. `CHROME_APP_NAME` threads the AppleScript/Quartz app name into cdp.py (defaults: stock `Google Chrome`; automation `Google Chrome for Testing`) — every cdp.py call against a CfT lane carries BOTH `CDP_PORT` and `CHROME_APP_NAME`. Full contract: `skills/look/SKILL.md` → "Automation lane"; empirical basis: `docs/superpowers/analysis/2026-06-05-sp1-cft-empirical-findings.md`. **Cert-pin lane (dogfood #1, PR #169):** opt-in `--cert-spki=<PIN>` / `LOOK_CERT_SPKI` appends `--ignore-certificate-errors-spki-list=<PIN>` to Chrome argv — pin-only TLS bypass for self-signed HTTPS LAN targets (typical home-lab); same fail-closed gate as `--insecure` (non-9333 + provably-isolated profile); `--automation` temp profile satisfies it. Malformed pins rejected fail-loud. Full contract: `skills/look/SKILL.md` → "Cert-pin lane".

**Also in /look-v2:** sub-B — `window bounds` answers over CDP (`Browser.getWindowForTarget`, headless-capable, stdout contract `left,top,width,height`); sub-C — global `--target SEL` pins every command in a call to one tab (full id / 12-char prefix / url substring; ambiguous or unknown → fail-loud; websocket channel required). Command inventory lives in the `COMMANDS` dict in cdp.py; look-facing commands are listed in look SKILL.md Quick Reference.

**Design principle:** cdp.py is used by JAINE (Claude Code agent), not a human. Design accordingly:
- Explicit flags (`--js`, `--full-page`) over heuristic auto-detection
- Parseable, stable output format over pretty formatting
- No silent fallbacks — warn on stderr when requested behavior degrades
- SKILL.md = API docs for the agent; if a capability isn't described there, JAINE won't use it

## Architecture: /drive (SP2, #164 epic)

`skills/drive/SKILL.md` — agent-driven product testing on isolated CfT lanes (ports 9340-9349 interactive; the daily 9333 browser is structurally unreachable — launch.sh gate + script guards). The verify-core is **opt-in extensions of the shared cdp.py engine** (spec §4.1 — commands differ in SKILL.md defaults, not in the engine; /look's no-flag defaults byte-identical):

| Primitive | Surface | Contract |
|---|---|---|
| navigate-wait | `navigate URL --wait [load\|domcontentloaded\|networkidle] [--expect-url S] [--timeout S]` | blocks on `Page.lifecycleEvent` filtered by OUR loaderId (prior-page race closed); prints final URL + `loader=` token; `NAVIGATE_FAIL`/`NAVIGATE_URL_MISMATCH` + exit 1 |
| console-gate | `console --gate` | exit 1 on any error/exception; FAIL line carries the per-leg breakdown (`N (X exception(s), Y console, Z log)`). Three-channel contract: **exceptions retro** (replayed to late subscriber) + **console.* live 3s window** (call the gate right after the action; retro console.* replay is a fragile activation quirk — NOT promised) + **Log domain live** (CORS/CSP/net::ERR_* surface ONLY as `Log.entryAdded` — code-review pack A, empirically confirmed). Buffer clears per-navigation. Empirics: `docs/superpowers/analysis/2026-06-05-sp2-console-gate-verification.md` |
| assertion | `assert [--js] EXPR_OR_SEL [--visible\|--actionable] [--stable MS] [--timeout S]` | condition must hold CONTINUOUSLY for --stable ms; flap diagnostics distinguish flaky from absent; `--actionable` = visible+enabled+hit-test with cmd_click's scrollIntoView parity |
| trusted-click signal | `click SEL --require-trusted` | refuses the untrusted `el.click()` fallback (`CLICK_REQUIRE_TRUSTED_FAIL` + exit 1, NO click) — R2-U |
| screenshot-binding | `screenshot F --bind` | second stdout line `BIND url=… loader=… t=…`; compare `loader=` with navigate's token to detect stale captures |
| cookie-seed | `skills/drive/scripts/cookie_seed.py --domains … --to-port …` | two-port script (cdp.py stays single-port): Storage.getCookies(9333) → dot-anchored domain filter → setCookies(lane); never seeds INTO 9333; counts-only output |

Modes (§4.4): autonomous (headless, default) / co-pilot (headful, main-session-only — **subagents are always autonomous**, hard-coded in delegation prompts). Circuit-breaker: max 3 fix-verify iterations, then honest STOP. Engine boundary (SP0 bounded-both): Playwright NOT built — a demonstrated cdp.py wall files an issue.

**Ephemeral lanes (SP4):** subagent delegation uses `CDP_PORT=0` — the OS assigns the
port, launch.sh derives a unique mktemp profile (`jaine-drive-eph-XXXXXX`), and prints a
4-line contract (`CDP_PORT/LANE_PROFILE/LANE_KILL_MATCH/LANE_BROWSER_BIN`). The unique
profile IS the ownership token (holes R1-H/R2-R closed structurally — no allocator, no
locks); teardown by the launcher-escaped `LANE_KILL_MATCH` can only kill its own browser.
Gated to `--automation` + launcher-owned profile, fail-loud otherwise. Delegation
protocol: `skills/drive/SKILL.md` → "Subagent delegation". Calibration assets:
`skills/drive/data/calibration-manifests.json` (frozen oracles) +
`skills/drive/scripts/grade_run.py` (external grader) + `tests/fixtures/calibration/`.

## Testing

### Test suites

Four domains — `/look` (browser/CDP), `/drive` (verify-core + cookie-seed), `/check` (review composer wrapper + parser + state), and `/consult` (panel orchestrator + wrappers + parsers + classifier). Live inventory: `ls tests/`; live count: `pytest tests/ --co -q | tail -1`.

| Domain | Files | Type | External dep |
|--------|-------|------|--------------|
| `/look` | `test_cdp.py`, `test_launch.py`, `test_e2e.py`, `test_e2e_cft.py` | structural + behavioral | `test_e2e.py` → JAINE Browser; `test_e2e_cft.py` → pinned CfT (self-skips if not installed) |
| `/drive` | `test_e2e_drive.py`, `test_e2e_lanes.py`, `test_cookie_seed.py`, `test_drive_skill.py`, `test_grade_run.py` | behavioral (CfT lane) + offline unit/structural | `test_e2e_drive.py` → pinned CfT via `cft_browser` (self-skips); `test_e2e_lanes.py` → pinned CfT, self-launches ephemeral lanes (self-skips); cookie-seed e2e launches a second transient lane on 9362 |
| `/check` | `test_check_round_wrapper.py`, `test_parse_ledger_patch.py`, `test_skill_prompts.py`, `test_verify_audit_findings.py`, `test_update_state.py`, `test_check_pipeline_integration.py`, `test_log_round_bash32_compat.py`, `test_check_e2e.py` | structural + behavioral | `test_check_e2e.py` → codex |
| `/consult` | `test_consult_panel.py` | structural + behavioral (injected runner; one real-subprocess reaping test) | live `--panel` e2e is dogfood-only |

Most are offline structural tests (fast); `test_e2e.py` (browser) and `test_check_e2e.py` (codex) are the only ones needing an external dep — both excluded from the default run. `test_e2e_cft.py` (SP1) auto-launches its own pinned-CfT lane on `DRIVE_TEST_PORT` and self-skips when CfT is not installed (`update-cft.sh`), so it stays safe in any run.

### Running tests

```bash
# Structural tests (fast, offline)
pytest tests/test_cdp.py -v

# E2E tests (auto-launches JAINE Browser if not running)
pytest tests/test_e2e.py -v

# All tests
pytest tests/ -v
```

### Test pages

`tests/fixtures/test-page.html` — deterministic HTML with known selectors for each /look command. `tests/fixtures/drive-page.html` — dynamic page for the SP2 verify-core (async element, flapping element, delayed-enable, occluded + below-fold targets, mid-flow error buttons). Both served by `conftest.py` on a random port during e2e runs.

### Adding new commands — MANDATORY

Every new `cmd_*` function in `cdp.py` MUST have:

1. An entry in `COMMANDS` dict
2. A structural test in `test_cdp.py` (function exists + registered)
3. A behavioral e2e test in `test_e2e.py` (look commands) or `test_e2e_drive.py` (verify-core) — against a real browser
4. A test element in `test-page.html` / `drive-page.html` if the command interacts with DOM

No command ships without all 4.

## Architecture: /check

Each round runs through the composer wrapper `skills/check/scripts/bulldozer-round.sh` (PR #109 / #110): it invokes codex, parses the verdict via `parse-ledger-patch.py`, calls `log-round.sh` (writes `state.json` + `bulldozer.log`), renders the trajectory (`render-trajectory.py`), and emits pivot signals (`emit-pivot.py`) — behind a partitioned exit-code contract (`0` ok, `2-5` parser, `10` pivot, `11` manual-extraction, `64/70/71` wrapper). Depth params (max_rounds, reasoning, ephemeral, prompt prefix) come from `data/depth-config.json` — single source, mirrored by the SKILL.md "Depth Levels" table (`TestDepthConfigContract` guards drift).

Codex reasoning via `-c` overrides (codex 0.135.0; named profiles still unsupported):
- quick: `-c model_reasoning_effort=medium --ephemeral` + prompt `SKIP SKILLS.`
- standard/exhaustive: `-c model_reasoning_effort=xhigh`

Inter-round context: structured `review-ledger.yml` (cumulative, append-only) + full previous verdict as appendix. Codex outputs `LEDGER_PATCH` YAML block at the end of each verdict — Claude applies it to the ledger.

Pivot triggers (exit 10): flat `ROUND >= max_rounds && verdict != GO`, OR the B6 calibrated early-pivot (#128: `depth == exhaustive` + `round >= 5` + `verdict != GO` + mean-last-3 findings `>= 3.0`). The exit-11 manual-extraction caller protocol mirrors both (SKILL.md Step 7).

**CRITICAL:** Codex exec runs in FOREGROUND only. `-o` verdict file is written last — background + polling causes false truncation diagnosis.

E1 (#94): a per-round read-only `consistency-auditor` agent + `verify-audit-findings.py`
(quote-presence anti-hallucination) pre-cleans doc artifacts before each codex round;
soft-enforced (SKILL Step 1.7 + structural test, no `bulldozer-round.sh` change). See
`docs/superpowers/specs/2026-06-01-e1-pre-review-consistency-audit-design.md`.

## Feedback Protocol

Before working on skill improvements, check for feedback issues from other JAINE sessions:

```bash
gh issue list --repo A3IO/jaine-plugins --label feedback,bulldozer --state open
```

Feedback issues are created by JAINE-consumer sessions that encountered friction while using bulldozer skills. Each issue follows a structured template: what was attempted, what went wrong, workaround used, and plugin version.

When fixing a feedback issue:
1. Read the issue body for reproduction context
2. Fix the skill/code/documentation
3. Refresh consumer's plugin cache (`jaine-sync plugins update` or `rm -rf ~/.claude/plugins/cache/jaine-custom/bulldozer/`)
4. Close the issue with a reference to the fix commit

**CRITICAL:** Step 3 prevents stale cache — the root cause of false positives in issue #46 where 3/6 feedback items were invalid because JAINE-consumer was running an old plugin version.

## Versioning

`plugin.json` version auto-bumps on every merge to `bulldozer/main` via the `auto-calver` post-merge hook. Format: `YYYY.MM.DD` (first merge of the day) → `YYYY.MM.DD.N` (subsequent merges same day). **Do NOT bump manually** — it causes double-bump (your bump + auto-bump = `.1` suffix for a cosmetic-only PR).

## Architecture: skills-only

Plugin uses `skills/` directory exclusively. No `commands/` directory — per Claude Code docs, commands and skills are the same mechanism, and having both with the same name causes one to be silently dropped. Each `/bulldozer:*` invocation loads `skills/*/SKILL.md` directly.

## Architecture: consult vs check (issue #96)

`consult` and `check` solve overlapping problems with disjoint scopes:

| Aspect | `bulldozer:check` | `bulldozer:consult` |
|--------|-------------------|---------------------|
| Input | File/dir/diff on disk | Inline text in conversation |
| State | `.bulldozer/<session>-<artifact>/` (per-review dir, ledger, state) | None (each invocation independent) |
| Codex sandbox | Read-only at `-C $PROJECT_ROOT` | Read-only at empty `/tmp/bulldozer-consult-$$/` |
| Codex isolation flags | `--ephemeral` (quick only) | Always `--skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral` |
| Verdict format | LEDGER_PATCH YAML in clean `-o` file | Inline prose + GO/NO-GO/MINOR-FIXES + basis sentence, parsed fail-closed |
| Multi-round | Reviewer sees ledger + previous verdict as appendix | Each round independent; user re-prompts manually |
| Empirical verification | Required (`/receiving-code-review` discipline) | Not applicable — codex has no file access |
| Per-round cost | ~30-80s, ~50-100K tokens | ~4-15s, ~5-15K tokens |

**Routing rule (claude-side, enforced by pre-flight in consult Step 2):** if user's prompt mentions any artifact (file path, `.md`/`.py`/etc., "see specs/X", "attached"), redirect to `check`. Otherwise consult.

**Escalation rule** (consult Step 7): if `round_count >= 3` AND last 2 verdicts contain NO-GO, prompt the user to consider `/bulldozer:check`. User decides — we do NOT auto-invoke.

**Panel mode (`--panel`, opt-in, 2026-06-02):** `scripts/consult_panel.py` runs codex+grok+agy in parallel for multi-model find-holes (~50% of findings unique to one model; verdict diversity ≈ 0, so panel is for holes, not GO/NO-GO). (`agy` = Antigravity CLI / Gemini models; replaced the retired gemini CLI, #189.) `--repo <path>` = informed mode — the **deliberate exception to the routing rule above**: the three models read real code (split-test 2026-06-02: informed ≫ isolated for repo-specific questions, tie for abstract). grok hard-no-read proved unachievable on macOS (sandbox governs write/network, not read; `--disallowed-tools` is whack-a-mole) → all three are soft-no-read (empty cwd + prompt). **All three run on the REAL HOME now (#189):** codex isolates via flags (no HOME trick); **grok** via `--no-memory`/`--no-subagents` — a HOME-override sandbox broke grok's `--repo` tool-worker auth (`Auth(AuthorizationRequired)` → grok cancelled on *every* informed run) and was leaky anyway (grok wrote to the real `~/.grok` regardless); real HOME → grok survives 3/3, verified through the panel. **agy** is keychain+OAuth-bound (no copyable token to sandbox); non-interactive `-p` (stdin=DEVNULL → onboards an unknown repo without OAuth, starts no MCP). **Read-only via PreToolUse hook (#189):** `agy --print` AUTO-ACCEPTS every tool (verified no flag/config disables it — not dropping `--dangerously-skip-permissions`, not `--sandbox`, not `autoAccept:false`, not deny lists; print mode can't prompt). The deterministic gate is a **fail-closed** PreToolUse deny hook: the agy leg runs with cwd = a temp dir seeded with `.agents/hooks.json` whose hook ALLOWS only an EXACT set of known read tools (`view_file`/`list_dir`/`read_file`/`grep_search`/… — `view_file`+`list_dir` empirically confirmed) and DENIES everything else — any unlisted/mutating/command-exec tool AND any malformed input → `{"decision":"deny"}` (the earlier substring blocklist let `save_memory`/`shell`/`exec`/`save_file`/malformed-stdin through, #189 code-review); the repo is read via `--add-dir`, never as cwd, so a denied write can't touch it. Reads allowed → review still works. agy persists each call's prompt+response in a per-call `brain/<conversationId>/` session dir (plaintext transcripts — verified by marker probe) + a `conversations/<id>.db`. For statelessness the run injects a unique NONCE into agy's prompt; `_run_one` snapshots brain/ BEFORE the run and afterward deletes only NEW UUID-named dirs whose transcript carries the nonce + `conversations/<id>.db*` — a concurrent agy session (the user's visual Antigravity app) creates a new dir WITHOUT our nonce, so it is never swept (id-via-hook capture failed in the panel-default isolated mode — agy makes ZERO tool calls there, so the hook never fired and the transcript LEAKED until the nonce fix; id is UUID-validated, glob anchored to `<id>.db*` so a prefix-sibling is never swept, #189 code-review). The panel does NOT write `cache/projects.json` (it accumulates a harmless stale temp-cwd entry per run). Model overridable via `BULLDOZER_AGY_MODEL`. Design + empirical basis: `docs/superpowers/specs/2026-06-02-consult-panel-design.md` (§8 = implementation deltas + 2-round dogfood). Post-ship hardening (#142): per-model `_MODEL_SPECS` registry + unified `wrap()` collapse the per-model/per-mode duplication; summarizer delimiter carries a per-call nonce (no boundary spoofing); `run_model` reaps the whole process group on timeout (no orphaned helpers). agy prints plain text (parser = `parse_codex`). Informed find-holes uses BEHAVIORAL wording (NOT "holes/bugs/vulnerabilities") — trigger words trip Gemini's safety refusal on security-flavoured code (proven on an auth file: trigger framing → refused ×2; behavioral → full review); the no-`write_file` clause stays (agy can also save findings to a file and return empty). The `_parse_json_field` three-way "empty response" sentinel is now grok-only (the old gemini-CLI `write_file` empty-response bug it was built for is gone with that CLI — `docs/superpowers/specs/2026-06-03-consult-gemini-write-file-design.md`, historical).

**REMOVED features** (validated against shipping by 3 of 4 independent codex dogfood runs):
- Persistent mode (`codex exec resume`) — data retention risk, stale context contamination, 2× implementation surface. Users wanting continuity copy prior verdict text into a new prompt.
- Session log with prompt content — only metadata in `bulldozer-consult.log`. Raw prompts and verdict bodies are not persisted.

## Architecture: codex MCP server (mcp/codex_server.py)

The bulldozer plugin ships its OWN MCP server (`mcp/codex_server.py`, tool `mcp__plugin_bulldozer_codex__codex_run`) — v2 as of 2026-06-19. **Do NOT use the stock `codex mcp-server`** — see below.

**V2 architecture** (replaced "wrap codex exec" v1): fronts `codex app-server` (bidirectional JSON-RPC over stdio — the same engine the Codex web app drives) for full interactive approvals, cross-session resume, and structured output:

- **Interactive approvals**: mid-turn command/file-change/permissions requests are bridged to CC via MCP elicitation (`elicitation/create`). Fixes codex#18268 where the stock MCP's Accept was mis-parsed as Denied. The elicitation wait is **human-paced (300s default**, threaded through `handle_server_request(..., timeout=)`); the turn deadline is **credited back** the approval wall-clock so a slow human Accept doesn't trip the 120s turn timeout. The `requestedSchema.label` enum carries **human-readable display strings** ("Allow once" / "Allow & always permit this command" / "Allow & always permit network access to `<host>` (`<action>`)" / "Cancel the turn" / "Grant for this session" / …) rendered as CC's dropdown; the chosen label is reverse-mapped back to the EXACT codex decision (string or amendment dict) so the #18268 fix stays intact (`LBL_*` constants + `build_command_approval_labels`; host+action carries primary distinctness for network labels, with `_dedupe_labels` as a last-resort numeric-suffix fallback that guarantees unique reverse-map keys). The enum is **OPTIONAL** — clicking CC's bare Accept (no dropdown selection) defaults to plain `accept`; a required field made CC block Accept with "This field is required" (live-UI bug, fixed). Both surfaced only via a real CC dialog (a programmatic driver answers instantly and never hits the 10s window or the form).
- **Resume**: `thread_id` arg resumes an existing non-ephemeral thread across sessions (cross-session via `~/.codex/sessions/` rollout files).
- **Structured output**: mode=review constrains via `outputSchema` → `{verdict,findings}` JSON guaranteed.
- **Isolation — KNOWN-BROKEN, fix pending (A3IO/jaine-plugins#204):** `_spawn_appserver` passes `-c mcp_servers={}` and `start_thread` sets `config: {"mcp_servers": {}}` — but **both are no-ops** against codex 0.141: the isolated thread loads ALL user + built-in MCP servers (dash/deepwiki from `config.toml` + codex_apps/computer-use from feature flags), byte-identical to passing no flag (empirically verified — the `-c` TOML override does not clear the `[mcp_servers.*]` table). **Working disable (verified, not yet wired):** point `CODEX_HOME` at a dir seeded with only `auth.json` + `--disable apps --disable computer_use` → 0 servers, auth intact. (Related #204 findings: codex app-server inherits CC's full env — secrets reachable by codex shell commands; `thread/tokenUsage/updated` arrives but `codex_run`'s result drops it.)
- **Graceful no-codex**: binary check on first call; returns error result, never crashes.

**Latency profile (empirical, codex 0.141)**: the persistent `codex app-server` child is spawned once per session. The FIRST `thread/start` of a session pays a one-time cold-start cost that is **highly variable — ~28-80 s+ observed** (codex loads its setup even with `-c mcp_servers={}`; the spread depends on API/model-load conditions). Subsequent `thread/start` calls on the SAME alive process are warm (~1-2 s). `start_thread` (and `resume_thread`, since a cross-session resume is the first op on a fresh process) use a **180 s** `_pump_until` timeout to absorb the cold case — a 60 s ceiling intermittently timed out under real load (caught by a live MCP drive). The generous ceiling costs nothing on warm calls (it returns on the response).

**Known v1-of-v2 limitation**: the `item/permissions/requestApproval` bridge returns `{"permissions": {}, "scope": "turn"}` as a minimal safe default (`PERM_DECLINE`; the `perm_map` only maps permission labels to scope, not the actual permissions object — this is adequate for most cases but a future improvement would populate `permissions` from the request params).

**Dispatcher**: v2 `main()` uses `sys.stdin.readline()` (NOT `for line in sys.stdin`) so `cc_read_fn` can safely call `readline()` mid-turn for elicitation responses without iterator buffering conflicts. `cc_write_fn` writes CC-facing JSON-RPC 2.0 frames (with `"jsonrpc":"2.0"`) to stdout; the app-server gets jsonrpc_lite frames (no `"jsonrpc"` key) — asymmetric by design.

**Module singleton**: `_v2_manager` (AppServerManager) + `_v2_state_machine` (TurnStateMachine) are module-level singletons. The serial dispatcher loop is the primary concurrency protection (MCP calls arrive one at a time); the busy guard (`TurnStateMachine.is_busy()`) is defense-in-depth and is rarely/never hit in production. `.mcp.json` = `{"codex": {"command": "python3", "args": ["${CLAUDE_PLUGIN_ROOT}/mcp/codex_server.py"], "env": {}}}`.

**Tests**: `tests/test_codex_mcp_v2.py` — offline tests (including `TestV2Dispatcher` subprocess-level integration) + `@pytest.mark.slow` e2e tests against real codex (self-skip without codex). Run: `pytest tests/test_codex_mcp_v2.py -m slow -v` (allow 3-5 min). The review e2e exercises the full stack: ensure→initialize→thread/start→turn/start(outputSchema)→delta events→turn/completed→_shape_result.

**Drift-resilience + param-parity (2026-06-19):** see `docs/superpowers/specs/2026-06-19-codex-drift-resilience-param-parity-design.md` for the full design. What shipped:

- **`_drift` tool-result field** — behavioral drift only: `UNKNOWN_SERVER_METHOD`, `UNKNOWN_NOTIFICATION`, `OUT_OF_ENUM_LABEL`, `UNKNOWN_DECISION_VARIANT`. Absent on the happy path (byte-identical to pre-ship). `_stamp_drift` attaches it at every return path of `codex_run_v2` when the per-call accumulator is non-empty.
- **Log-only version capture** — `LAST_VERIFIED_CODEX_VERSION = "0.141"` (module constant). Version parsed from the `codex app-server` initialize `userAgent` as the `/MAJOR.MINOR` in the **first whitespace-delimited token** (`<clientName>/<codexVersion>`) — NOT a `codex/`-anchored match (the real userAgent starts with our client name, not `codex/`). Mismatch → `_drift_warn(None, "VERSION_MISMATCH", …)` → stable log `~/.claude/hooks/bulldozer-codex.log` (env override `BULLDOZER_CODEX_LOG`); **never in `_drift`** (version number ≠ protocol drift — FP-fatigue avoided).
- **`_KNOWN_NOTIFICATIONS` allowlist** (incl. `item/completed`) — notifications not in the set emit `UNKNOWN_NOTIFICATION` and continue (non-terminal; preserves healthy turns). `turn/completed` terminal-failure detection: `status != "completed"/"success"` OR `error` truthy → clean `{"error": …}` (drift-stamped), not `_shape_result`. No separate `turn/failed` event — the status arm is the only detection point.
- **New `thread/start` params**: `base_instructions` (`None` → `STERILE_INSTRUCTIONS` default; `""` is a valid caller value — None-sentinel discipline, not empty-string); `developer_instructions` (wires as `developerInstructions`, omitted when `None`). New threads only; resume does not re-send.
- **`config` passthrough with isolation scrub** — `_CONFIG_DENY` strips `mcp_servers`, `mcpServers`, `baseInstructions`, `base_instructions`, `developerInstructions`, `developer_instructions`; then `ISOLATION_CONFIG`'s `mcp_servers={}` overwrites (always wins as a config key — though that value is a **no-op** against codex 0.141; real isolation pending, see #204). Caller config passes through for non-deny keys.
- **CI fingerprint coherence tripwire** — `tests/fixtures/codex-protocol-fingerprint.json` (curated: bridged/unsupported methods, decision variants, `last_verified_codex_version`); `test_fingerprint_matches_code_constants` asserts code constants match the fixture (catches one-sided edits; does NOT detect upstream protocol drift). Slow-e2e `test_live_codex_version_matches_pin` spawns real codex, asserts live version against `LAST_VERIFIED_CODEX_VERSION` — the maintainer re-verify ritual.

## Known Issues

**Open issues live on GitHub — do NOT hardcode them here.** This section drifted badly precisely because it did: #94 sat under "In Progress" for weeks carrying calibration numbers that #128 later disproved. Source of truth:

```
gh issue list --repo A3IO/jaine-plugins --label bulldozer --state open
```

### Calibration history (durable fact — the trap that caused the drift)

The PR#95 calibrated pivot trigger (`exhaustive + round ≥ 5 + avg-last-3 ≥ 3.0`) once claimed **"0 FP / 60% TP across 26 sessions"**. **That claim does NOT hold.** B6 (#128) re-derived it on a 65-session corpus: the 0-FP claim did not reproduce any-depth (FP = 4 of 10 converged sessions reaching round 5) — it ships **exhaustive-only**. The trigger was removed in PR1b and re-added in #128. See `docs/superpowers/analysis/2026-06-01-b6-pivot-calibration.md`. Do NOT cite the old 26-session numbers. (#94 itself stays open for its matrix-pattern-recognition + multi-mode parts; only the pivot-decision slice shipped.)

### Fixed (historical — see git log + closed issues for detail)

- **2026-06-01:** #110 composer-wrapper epic — PRs #114/#121 (manual-fallback discipline), #122 (hardening A1-A4), #123/#124 (structural B1-B8), #125 (efficiency C3), #126 (simplification D1/D2/D4/D5), #129 (docs E1-E3); #127 (#130, parser fenced-LEDGER_PATCH bug); #128 (#131, B6 calibrated pivot); #132 (post-review doc-drift). Also: closed #100/#105 as already-resolved (verified by probe).

- **2026-05-16:** Three feedback issues from a JAINE-consumer session in `/0/SANDBOX/BRANCHLAB` (PR #57):
  - **#54** — `launch.sh` no longer passes `--force-dark-mode` or `--enable-features=WebContentsForceDark`. WebContentsForceDark was the sole cause of content recoloring; `--force-dark-mode` was verified inert on Dark-OS for CDP screenshots (chrome is never captured) and a latent risk on Light-OS — both dropped.
  - **#55** — `screenshot` gained `--clip X Y W H` (CSS-pixel region capture, mutex with `--full-page`) and `--scale N` (opt-in output resolution via `clip.scale = N / window.devicePixelRatio`). Every screenshot prints `PATH  W×H` on stdout. Empirical lesson encoded in cdp.py comments: `Emulation.setDeviceMetricsOverride{deviceScaleFactor:1}` does **not** change `Page.captureScreenshot` output size — only `clip.scale` does.
  - **#56** — SKILL.md Quick Invoke now instructs the agent to parse `$ARGUMENTS` into "URL token + task description". Previously the whole string was substituted into the URL slot, producing malformed navigation when a description was present.
  - **Post-review polish** (silent-failure-hunter + pr-test-analyzer + code-reviewer + comment-analyzer found 21 real findings, 1 FP): `_image_dimensions` rejects 0×0 from truncated headers (M2); WARN on stderr when `devicePixelRatio` read fails (SF1) or `_image_dimensions` returns None (SF3); `log("screenshot", …)` records `clip=`/`scale=` (M1); +14 structural tests covering arg-parse error paths, zero-DPR guard, native-path rejection of `--clip/--scale`; +5 unit tests for `_image_dimensions` (truncated JPEG/PNG, non-image, missing file, valid PNG); +1 e2e combo test (`--clip + --scale 1` → exact CSS-pixel output); tightened stdout format regex; `test_e2e.py` now imports `_image_dimensions` from `cdp.py` instead of duplicating the JPEG SOF parser; SKILL.md description/argument-hint updated per `/en/skills` docs (added triggers `capture region`, `check if X is aligned`, `захватить область`, `UI detail check`; argument-hint `[URL] [task description]`).

- **2026-05-14:** Commands/skills architecture — `commands/` directory removed, content merged into `skills/*/SKILL.md`. Root cause: `commands/check.md` and `skills/check/SKILL.md` both registered `/bulldozer:check`, only one loaded — consumer never saw Feedback section, Anti-patterns, Red Flags (336 lines of dead content). Same bug in look. (PR #51)
- **2026-05-11:** B1-B9 bugfixes, D1-D6 documentation fixes. See git log for details.

