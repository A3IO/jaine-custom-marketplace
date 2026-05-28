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

**17 CDP commands (zero dependencies — websocket-client bundled):**

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

Multi-channel: CDP WebSocket (primary) → AppleScript + DOM injection (fallback) → macOS screencapture (screenshot fallback). 13/17 commands work without websocket.

**Log:** `~/.claude/hooks/bulldozer-look.log`

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

```
2026-05-09T10:30:00+03:00 | session=bf5a38d6 | round=1 | artifact=spec.md | verdict=NO-GO | findings=8 | fixed=7 | fp=1 | reviewer=codex/gpt-5.5 | project=/path
```

View: `column -t -s'|' ~/.claude/hooks/bulldozer.log`

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
