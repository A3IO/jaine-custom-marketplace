# bulldozer

Adversarial review loop with external AI reviewer + visual browser verification via JAINE Browser CDP + lightweight conversational design consultation.

## Commands

### /bulldozer:consult — Conversational Design Validation (issue #96)

Lightweight stateless codex consultation for abstract design questions — "should I X or Y?", architectural tradeoffs, sanity-check before any artifact exists.

```
/bulldozer:consult                                         # ask, then provide design question
/bulldozer:consult Help me choose between A and B for X    # inline question
```

Use for: design Q&A, architecture tradeoffs, pre-implementation sanity checks. Stateless by design — each invocation is independent. ~4-15s and ~5K tokens per round (vs ~30-80s and ~50K for check on small artifacts) because codex runs fully isolated (`--skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral -s read-only`) from an empty tmpdir.

**Do NOT use for:** anything referencing files/paths/diffs on disk → use `/bulldozer:check` instead. Pre-flight detection redirects you automatically.

**Log:** `~/.claude/hooks/bulldozer-consult.log` (metadata only — no prompts, no verdict bodies)

### /bulldozer:check — Adversarial Review

Send artifact to reviewer (Codex CLI) → parse findings → verify each empirically → fix confirmed → re-send → repeat until GO.

```
/bulldozer:check                                    # standard (3 rounds max), ask for artifact
/bulldozer:check quick path/to/spec.md              # single round, specific file
/bulldozer:check standard src/gateway/              # 3 rounds, review a directory
/bulldozer:check exhaustive docs/design.md          # until GO (max 10)
```

### /bulldozer:look — Visual Browser Verification

Open URL in JAINE Browser (CDP :9333), take screenshot, show it.

```
/bulldozer:look                                                     # screenshot current tab
/bulldozer:look http://localhost:9401                                # open URL, screenshot
/bulldozer:look file:///tmp/page.html — проверить рендеринг таблицы  # URL + task description (description is your own brief, not passed to scripts)
```

**CDP commands (zero dependencies — websocket-client bundled):**

| Category | Commands |
|----------|----------|
| Status | `status`, `tabs` |
| Navigation | `navigate`, `open`, `reload` |
| See | `screenshot [FILE] [--full-page] [--clip X Y W H] [--scale N]`, `title`, `html` |
| Execute | `js`, `wait`, `click`, `fill` |
| Debug | `console`, `network` |
| Generate | `pdf`, `viewport` |
| Window | `window [bounds\|upper\|lower\|activate]` |

Screenshot prints `PATH  W×H` to stdout. Default output is at native DPR (Retina ≈ 2×); `--clip X Y W H` captures a CSS-pixel region; `--scale 1` forces CSS-pixel output via `clip.scale = 1 / window.devicePixelRatio`.

Multi-channel: CDP WebSocket (primary) → AppleScript + DOM injection (fallback) → macOS screencapture (screenshot fallback). Most commands work without websocket; CDP-only commands are marked in Quick Reference.

**Log:** `~/.claude/hooks/bulldozer-look.log`

### /bulldozer:drive — Agent-Driven Product Testing

Product testing on isolated Chrome-for-Testing lanes (ports 9340-9349; your daily :9333
browser is structurally unreachable from a drive lane). A verify-core on top of the same
cdp.py engine: navigate-that-waits, console error gate, stability-window assertions,
trusted clicks, navigation-bound screenshots, opt-in cookie-seed auth. Autonomous
(headless, default) and co-pilot (headful, human checkpoints) modes; honest STOP after
3 fix-verify iterations.

```
/bulldozer:drive http://localhost:9401 прогнать сценарий логина
```

Requires the pinned Chrome for Testing (`skills/look/scripts/update-cft.sh` installs it).

## MCP server (codex bridge)

The plugin ships its own MCP server — four tools for driving OpenAI Codex from any
session, with interactive approvals, cross-session resume, structured review output, and
per-call MCP isolation. (Do NOT use the stock `codex mcp-server`: its Accept is
mis-parsed as Denied — openai/codex#18268; this bridge fronts `codex app-server`, where
approvals work.)

| Tool | Use for |
|------|---------|
| `codex_review` | Adversarial review of a git diff — `target`: `uncommitted` \| `branch:<name>` \| `commit:<sha>` \| `custom:<instructions>`; returns prioritized free-text findings |
| `codex_run` | Autonomous coding/research task in isolation — `mode: review` (schema-enforced `{verdict, findings}`) or `implement` (free text); resumable across sessions via `thread_id` |
| `codex_info` | Connection-level reads (`models` / `auth` / `config` / `limits` / `usage` / `approval` knobs) — fast, no cold start |
| `codex_approve` | Resume a turn parked at an unattended approval (`{park_token, decision_id}`) |

Pass `mcp:'isolated'` unless codex genuinely needs cross-server tools. The first thread
of a fresh server process pays codex's cold start (~28–80 s, variable); subsequent turns
are warm (~1–2 s).

### Parallelism (facade multiplexer)

`mcp/codex_facade.py` — **the default since 2026-07-18** (`.mcp.json` launches it) —
fronts a lazy pool of workers (each an unchanged single-turn bridge), so concurrent
turns genuinely overlap — wall-clock = max, not sum. **Which bridge a session is on:**
the server's injected MCP instructions carry a `FACADE:` line when the worker pool is
live; without it you are on the legacy serial bridge (an old plugin cache or the kill
switch — a second concurrent call is rejected with `codex turn already in flight`).

Fan-out recipe: `codex_review`/`codex_info` carry the MCP `readOnlyHint` annotation, and
a client honoring it (Claude Code ≥ 2.1.214 verified) dispatches several review calls
from ONE assistant message in parallel — review fan-out needs no subagents. For
`codex_run` (unhinted — it can mutate), same-message calls dispatch serially: fan out one
call per subagent (sonnet or stronger — weaker models fumble MCP tool calls) and pass
`approval_policy:'never'` with a read-only sandbox. Approval-capable turns and
overlapping writable roots serialize BY DESIGN (`codex_review` is always
parallel-class). A second, slower lane: clients that
auto-background long MCP calls (Claude Code ≥ 2.1.212 backgrounds a call still running
at ~120 s into a task) let the MAIN session stack long turns one per message — each
holds the session ~120 s before backgrounding; a call finishing under ~120 s blocks its
message; subagent calls are never auto-backgrounded. Kill switch: `BULLDOZER_FACADE_OFF=1`
reverts to the legacy single bridge.

### Approvals

- Default: interactive approval dialogs in the client (MCP elicitation).
- Native macOS dialog instead of the client TUI: `touch ~/.claude/bulldozer-approval-dialog`
  (per-user) or `/0/.jaine/bulldozer-approval-dialog` (machine-wide); env
  `BULLDOZER_APPROVAL_UI=cc` forces the TUI back. Live toggle — the sentinel is checked
  fresh per approval, no restart needed.
- Unattended (model-in-the-loop): arm `~/.claude/bulldozer-unattended` — instead of a
  human dialog, `codex_run` returns `{status:'awaiting_approval', park_token, approval}`;
  the orchestrating model decides from the evidence and resumes via `codex_approve`.
- Introspect all knobs: `codex_info(query="approval")`.

### Troubleshooting

- Tools absent in a session ⇒ the server did not connect: check `claude mcp list`,
  approve the server if pending. The server requires **Python 3.11+** (`tomllib`).
- Server CODE changes need a full client restart — stdio MCP servers are spawned at
  launch and do not hot-reload (`/reload-plugins` re-registers config only).
- Audit log: `~/.claude/hooks/bulldozer-codex.log` (env override `BULLDOZER_CODEX_LOG`) —
  `TURN_OK`/`TURN_ERROR` per turn, `FACADE_*` scheduler events, `APPROVAL`/`PARK`/
  `INTERRUPT` lines. See "Log Format" below for the shape caveats.

## Supported Artifact Types

| Type | Example | What codex reviews |
|------|---------|-------------------|
| File | `docs/specs/auth-design.md` | Read and review the file |
| Directory | `src/gateway/` | Review architecture of the module |
| Git diff | (auto-detected from branch) | Review current branch changes |

## Requirements

- `codex` CLI installed and authenticated (`npm i -g @openai/codex`)
- Git repository (reviewer needs `git ls-tree`, `git show`)

## Files

Each review gets an isolated directory — no collisions between sessions or artifacts:

```
.bulldozer/                                 # gitignore this entire dir
  bf5a38d6-auth-design/                     # {session_id_prefix}-{artifact_basename}
    review-ledger.yml                       # cumulative ledger (inter-round context)
    state.json                              # round state
    verdict-r1.txt                          # clean codex answer (via -o flag)
    verdict-r2.txt
    full-r1.txt                             # full codex output (debug only)
    full-r2.txt
```

**Global audit log:** `~/.claude/hooks/bulldozer.log`

## Log Format

The canonical grammar (issue #322), produced by the shared writer `lib/bulldozer_log.py`:

```
{ts} | event={event} | session={sid} | k1=v1 | k2=v2 …
```

`event=` and `session=` come first, in that order; the rest are event-specific `k=v`
pairs. Timestamps are ISO-8601 with a colon offset. Values are sanitized (no newline, no
` | `), so every line stays greppable and `cut`-able.

**Two producers are NOT on the shared writer yet** — a miner must handle their shapes or
skip them explicitly (do not assume `event=` is present on every line):

| Producer | Shape it emits | Difference |
|---|---|---|
| `mcp/codex_server.py` (`_drift_warn`) | `{ts} | TURN_OK | model=… | effort=…` | event is **positional**, no `event=`, no `session=`, timestamp has no offset |
| `consult_panel.py` (`_log_completion`) | `{ts} | session=… | round=1 | verdict=… | models=…` | no `event=` key (its `consult-invoke` lines DO use the helper) |
| `consult/SKILL.md` (inline single-codex flow) | `{ts} | session=… | round=1 | verdict=… | model=…` | a bare `echo >>` from the skill itself — no `event=`, and **`model=` singular** where the panel writes `models=` plural |

```
2026-07-12T17:25:03+07:00 | event=round | session=3d3b6182 | round=4 | artifact=specs/design.md | verdict=NO-GO | findings=1 | fixed=3 | fp=0 | reviewer=codex/gpt-5.6-sol | depth=standard | duration_s=364 | project=/path
2026-07-12T17:25:03+07:00 | event=pivot | session=3d3b6182 | round=4 | artifact=specs/design.md | depth=standard | trigger=max_rounds_reached | findings=1 | max_rounds=3 | project=/path
```

**Stable channels** — all under `~/.claude/hooks/` (never in the plugin cache, which is
wiped on every update):

| Log | Written by | Carries |
|---|---|---|
| `bulldozer.log` | /check wrappers | `event=round`, `pivot`, `reconciled`, `audit`, `wrapper-fail` |
| `bulldozer-codex.log` | the codex MCP bridge | `TURN_OK`, `TURN_ERROR`, `INTERRUPT`, `PARK`, `APPROVAL`, `INFO_ERROR`, `WARNING` |
| `bulldozer-consult.log` | consult + panel legs | per-leg outcomes, resolved model ids |
| `bulldozer-look.log` | `cdp.py` | per-command outcomes (`port=`, `target=`; URLs and JS are **redacted** — origin+path only, `expr_sha=` for JS) |
| `bulldozer-drive.log` | drive lanes | lane lifecycle, cookie-seed audit, tripped circuit-breakers |
| `require-workflow-skill.log` | the Workflow guardrail hook | routing decisions (`project=`, `session=`) |

**Redaction is scoped to the look channel**, where it is implemented (`cdp.py`: URLs are
reduced to origin+path with a `?<redacted>` marker; JS becomes `expr_len=`/`expr_sha=`).
The other producers only strip newlines and ` | ` — so a codex `TURN_ERROR` message or a
warning payload CAN still carry a full URL with its query string. Do not treat
"no secrets in the logs" as a plugin-wide guarantee.

Mining code should key off `event=` where it exists, never off line position.

View: `column -t -s'|' ~/.claude/hooks/bulldozer.log`
Full grammar + rationale: `docs/superpowers/specs/2026-07-11-bulldozer-log-grammar-design.md`.

> **Note (2026-07-12):** no log miner or HTML report exists in this repo yet. This section
> is the contract to build one against — not a description of an existing consumer. Lines
> written before 2026-07-11 predate the grammar (no `event=` key); `require-workflow-skill.log`
> additionally has a YAML-era prefix. A miner must tolerate both, or start at the cutover.

## How It Works

1. **Select model** — `codex debug models` → AskUserQuestion (every launch)
2. **Send** artifact to codex in FOREGROUND via `codex exec -s read-only -c model_reasoning_effort=... -o verdict-rN.txt`
3. **Read** clean verdict from `verdict-rN.txt` (no parsing of noisy full output)
4. **Extract** `LEDGER_PATCH` from verdict → apply to `review-ledger.yml` (cumulative inter-round context)
5. **Verify** each finding empirically (grep/read/run — using `/receiving-code-review`)
6. **Fix** confirmed issues, commit
7. **Log** round via `log-round.sh` (writes log + updates state.json)
8. **Repeat** with ledger + previous verdict as appendix, until GO or max rounds

## Origin

Developed from a real workflow that found 37 issues in 7 rounds (0 false positives) reviewing a REVERSING audit spec.
<!-- c9 multi-commit test doc -->
