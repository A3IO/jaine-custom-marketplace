# require-workflow-skill — keep Workflow swarms cheap + survivable (bulldozer plugin hook)

**Event:** `PreToolUse` (matcher `Workflow`)
**Files:** `hooks/require-workflow-skill.py` (all logic) + `hooks/hooks.json` (registers it as `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/require-workflow-skill.py` — marketplace-portable, no hardcoded paths, no `.sh` wrapper).
**Type:** **ADVISORY by default** — injects the routing/throttle doctrine on every Workflow call. The **DENY is OPT-IN**: it fires only when `BULLDOZER_ENFORCE_WORKFLOW_ROUTING` is truthy (`1`/`true`/`yes`/`on`). So installing bulldozer never surprise-blocks a consumer's workflows — you enable enforcement deliberately (panel verdict 2026-06-14: an opinionated heuristic guardrail must not be imposed by default). When enforced, deny = PreToolUse JSON `permissionDecision: "deny"` (+ reason) fed to Claude, which re-authors with routing+throttle (no user prompt).
**Log:** `~/.claude/hooks/require-workflow-skill.log` (one pipe-KV line per decision via `lib/bulldozer_log.py` — `{ts} | event=decision | session=… | decision=… | signals… | project=…`, incl. `enforce=0/1`; override via `WORKFLOW_HOOK_LOG`). Records before 2026-07-12 are the old `---`-separated YAML blocks (#322 C5 migration) — mining the historical epoch needs the dual parser.
**Escape:** a real `// workflow-routing-ok` **comment** (not the token inside a string), or a **cheap** `CLAUDE_CODE_SUBAGENT_MODEL` pin (haiku/sonnet — NOT opus/fable, which pin everything expensive = the very burst we deny), bypasses the deny.

---

## Why this exists

The Workflow tool spawns `agent()`/`parallel()`/`pipeline()` subagents. By default each subagent **inherits the session model** — Opus, or **Fable (2× Opus price)** in a Mythos session. On 2026-06-14 a `/code-review` workflow burst ~32 subagents that all inherited Opus, hit a server-side rate-limit ("Server is temporarily limiting requests · Rate limited"), and — because the script did `parallel(...).filter(Boolean)` — **silently dropped every rate-limited null and returned 0 findings**. ~2.9M tokens, nothing to show.

This hook is the backstop for that class of mistake. The primary fix is behavioral: **invoke `skill:workflow-swarms` when authoring a workflow** (its description already says ALWAYS). The hook enforces the one part that is *always wrong to ship*.

**Empirical basis (238 persisted workflow runs on disk, 2026-06-14):**

| signature | OK | rate-limited | rate |
|---|---|---|---|
| no routing (`model:`=0) + fan-out | 39 | 7 | **15%** |
| routed (`model:`≥1) | 178 | 8 | **4.3%** |
| of the 15 rate-limited runs | — | **14/15 had NO throttle** | — |
| no routing **and** no throttle (the denied combo) | — | **7/15** of all failures | — |

Routing cuts rate-limits ~3.5×; throttling (`mapThrottled`, which retries nulls instead of dropping them) is what turns an unavoidable ~6% server-side rate-limit into "degraded but recovered" instead of "0 findings". The deny targets the combo where **both** are missing — the incident's exact shape.

## What it does

1. Reads the `PreToolUse` stdin JSON; silent `exit 0` unless `tool_name == "Workflow"`.
2. Resolves the script body: inline `script`, else reads `scriptPath`, else empty (a `name`d or resumed workflow is trusted).
3. **Strips `//` + `/* */` comments and string literals**, then computes signals on the remaining *code*: `fanout` (`parallel(`/`pipeline(` with optional space, or `Promise.all`), `model_count` (`model:` keys in code), `throttle` (`mapThrottled`/`chunk(`/`CLAUDE_FALLBACK_MODEL`). `escape` is matched **only in comment text**. Stripping is what makes `// model: haiku` or `"see workflow-routing-ok"` no longer flip a signal.
4. **DENY** iff `fanout && model_count==0 && !throttle && !escape && no cheap CLAUDE_CODE_SUBAGENT_MODEL pin` — reason tells Claude to route per role **and** wrap the fan-out in `mapThrottled`; it does **not** embed the literal escape token (so re-authoring can't copy-paste the bypass).
5. Otherwise **ALLOW** + a `systemMessage` carrying the doctrine summary (points at `skill:workflow-swarms`).
6. Logs every decision with its signals, in the SAME process that emits it (so the log can never show `DENY` while `ALLOW` was emitted). Decisions: `DENY` / `ADVISORY` (the would-deny case when enforcement is OFF — same emit as ALLOW, logged distinctly) / `ALLOW` / `ALLOW_ENV` / `ALLOW_ESCAPE` / `ALLOW_NAMED` / `ALLOW_UNREADABLE` / `ALLOW_UNPARSED` / `ALLOW_PARSE_ERROR` (unparseable stdin) / `SKIP_NOT_WORKFLOW` (dead under the `Workflow` matcher — a line here means a misrouted registration).

**Fail-open:** a JSON parse error, a non-Workflow tool, an unreadable `scriptPath`, or a malformed `tool_input` → ALLOW. A hook bug must never block a workflow — but each fail-open path logs a **distinct** decision so the log never shows a false `safe` (#322 D4 closed the last two unlogged paths: parse-error and non-Workflow).

## Scope — what is and isn't denied

| Script shape | Decision |
|---|---|
| fan-out, no `model:`, no `mapThrottled` | **DENY** (the incident combo) |
| fan-out with per-agent `model:` routing | allow (routed — 3.5× safer) |
| fan-out wrapped in `mapThrottled`/`chunk()` | allow (resilient — survives a rate-limit) |
| single `agent()`, no fan-out | allow (one inherited-model agent is fine) |
| any of the above + `// workflow-routing-ok` | allow (deliberate escape) |
| any of the above + `CLAUDE_CODE_SUBAGENT_MODEL` set | allow (global pin handles routing) |
| `name`d / resumed / `scriptPath` unreadable | allow (trusted / can't inspect) |

## Limitations

These are the **genuine static-analysis limits** — found by a find-holes experiment (swarm + opus baseline, 2026-06-14) and confirmed unfixable without executing the script. Comment/string false-matches, `parallel (` spacing, `Promise.all`, escape-in-string, and garbage-env were the *fixable* findings and **are fixed** (see above); these remain:

- **Aliased / computed fan-out evades detection.** `const p = parallel; p(...)`, dynamic dispatch `ops['parallel'](...)`, or a custom wrapper has no literal `parallel(`/`pipeline(`/`Promise.all` → `fanout=0` → not denied. Detecting this needs execution, not regex. (Mitigated by the advisory + skill discipline.)
- **Can't measure runtime fan-out size.** `parallel(items.map(...))` over a computed array is N agents invisibly; the hook keys on *presence* of fan-out, not count. It targets "no routing + no resilience", not "too many agents". (Data says size×model-weight is the real risk; opus-at-scale collapses 58-93%, haiku is ~immune — but model weight is also a runtime fact.)
- **Partial routing passes.** `model_count` is a global count, not per-agent: a fan-out with 1 routed agent and N inheriting scores `model_count≥1` → ALLOW. Per-agent enforcement is impossible statically.
- **`model:` is not anchored to an `agent()` options object.** A `model:` token in a ternary (`flag ? model : x`), a JS label, a destructure (`{model: m}`), or an unrelated object satisfies the routing check → false-ALLOW. Distinguishing "routes an agent" from "any `model:`" needs JS parsing. (Found by the v2 find-holes run; fail-open direction.)
- **`strip()` handles `//`, `/* */`, and `"'\`` strings — but NOT JS regex literals or template-`${}` interpolation.** A token inside `/model:/` or `` `${parallel(...)}` `` is not stripped/parsed, so it can flip a signal. JS `/` is context-ambiguous (regex vs divide), so robust handling needs a JS parser; an *unterminated* string/`/*` runs to EOF and swallows later tokens. In practice these only arise from nonsensical or syntactically-invalid scripts (which fail at workflow runtime anyway). CR/CRLF line endings ARE normalized.
- **Rare residual false-matches.** JSON-quoted `{"model": ...}` (string-stripped → `model_count=0` → false-DENY; uncommon since agent opts use JS object literals); an *unrelated* `chunk(` (e.g. lodash) still reads as throttle. Both are rare and erring toward the safe-ish direction.
- **scriptPath TOCTOU / decoy.** If both inline `script` and `scriptPath` are present the inline wins; an unreadable path fails open (logged `ALLOW_UNREADABLE`). The hook inspects what it can read, which may differ from what runs.
- **Backstop, not the primary fix.** The real fix is invoking `skill:workflow-swarms` while authoring. The hook catches one submit-time combo.

## Tests

```bash
bash "$CLAUDE_PLUGIN_ROOT/tests/test_require_workflow_skill.sh"
```

Covers: the deny combo (inline + `scriptPath`), allow when routed / throttled / escaped / no-fan-out / env-pinned, non-Workflow tool, malformed stdin (fail-open), `name`d workflow, **and the experiment's bypasses** — `model:`/throttle token in a comment, escape in a string, `parallel (` spacing, `Promise.all`, garbage env value, and "deny message must not embed the escape token". The test points `WORKFLOW_HOOK_LOG` at a temp file so runs never append to the production log.

## Enabling enforcement (opt-in)

Off by default — advisory only. To turn the DENY on, set the env var before launching Claude Code (e.g. in your shell profile / `conf.d`):
```bash
export BULLDOZER_ENFORCE_WORKFLOW_ROUTING=1
```
Unset → advisory-only (the doctrine is still injected, nothing is blocked).

## Registration

Automatic when the bulldozer plugin is enabled — `hooks/hooks.json` registers it (marketplace-portable, no hardcoded paths, no `~/.claude/settings.json` edit):
```json
{ "hooks": { "PreToolUse": [
  { "matcher": "Workflow", "hooks": [ { "type": "command",
    "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/hooks/require-workflow-skill.py\"",
    "timeout": 10 } ] }
] } }
```

## Disable

Disable the bulldozer plugin, or remove the `PreToolUse`/`Workflow` block from `hooks/hooks.json`. Even while enabled it does nothing beyond advise unless `BULLDOZER_ENFORCE_WORKFLOW_ROUTING` is set.

## See also

- `bulldozer:workflow-swarms` — the full routing/throttle + recall-preserving swarm→rank→verify-all doctrine this hook guards (same plugin).
