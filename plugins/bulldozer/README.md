# bulldozer

Adversarial review loop with external AI reviewer + visual browser verification via JAINE Browser CDP.

## Commands

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
/bulldozer:look                                     # screenshot current tab
/bulldozer:look http://localhost:9401                # open URL, screenshot
```

**CDP commands:** status, tabs, screenshot, js, navigate, open, title, html, wait, reload.
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

1. **Send** artifact to codex via `codex exec -s read-only -o verdict-rN.txt`
2. **Read** clean verdict from `verdict-rN.txt` (no parsing of noisy full output)
3. **Verify** each finding empirically (grep/read/run — using `/receiving-code-review`)
4. **Fix** confirmed issues, commit
5. **Log** round via `log-round.sh` (writes log + updates state.json)
6. **Repeat** until GO or max rounds

## Origin

Developed from a real workflow that found 37 issues in 7 rounds (0 false positives) reviewing a REVERSING audit spec.
