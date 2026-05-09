# adversarial-review

Iterative adversarial review loop with external AI reviewer.

Send artifact to reviewer (Codex CLI) → parse findings → verify each empirically → fix confirmed → re-send → repeat until GO.

## Usage

```
/adversarial-review                                    # standard (3 rounds max), ask for artifact
/adversarial-review quick path/to/spec.md              # single round, specific file
/adversarial-review standard src/gateway/              # 3 rounds, review a directory
/adversarial-review exhaustive                         # until GO (max 10), ask for artifact
```

## Supported Artifact Types

| Type | Example | What codex reviews |
|------|---------|-------------------|
| File | `docs/specs/auth-design.md` | Read and review the file |
| Directory | `src/gateway/` | Review architecture of the module |
| Git diff | (auto-detected from branch) | Review current branch changes |

Free-form prompts without a concrete artifact are NOT supported — the fix→re-review loop requires something to iterate on.

## Requirements

- `codex` CLI installed and authenticated (`npm i -g @openai/codex`)
- Git repository (reviewer needs `git ls-tree`, `git show`)

## Files

Each review gets an isolated directory — no collisions between sessions or artifacts:

```
.adversarial-review/                        # gitignore this entire dir
  bf5a38d6-auth-design/                     # {session_id_prefix}-{artifact_basename}
    state.json                              # round state
    verdict-r1.txt                          # clean codex answer (via -o flag)
    verdict-r2.txt
    full-r1.txt                             # full codex output (debug only)
    full-r2.txt
  e34e20d7-preset-name-storage/             # different session/artifact
    ...
```

**Global audit log:** `~/.claude/hooks/adversarial-review.log` (alongside other CC hook logs)

## Log Format

```
2026-05-09T10:30:00+03:00 | session=bf5a38d6 | round=1 | artifact=spec.md | verdict=NO-GO | findings=8 | fixed=7 | fp=1 | reviewer=codex/gpt-5.5 | project=/path
```

View: `column -t -s'|' ~/.claude/hooks/adversarial-review.log`

## How It Works

1. **Send** artifact to codex via `codex exec -s read-only -o verdict-rN.txt`
2. **Read** clean verdict from `verdict-rN.txt` (no parsing of noisy full output)
3. **Verify** each finding empirically (grep/read/run — using `/receiving-code-review`)
4. **Fix** confirmed issues, commit
5. **Log** round via `log-round.sh` (writes log + updates state.json)
6. **Repeat** until GO or max rounds

## Origin

Developed from a real workflow that found 37 issues in 7 rounds (0 false positives) reviewing a REVERSING audit spec. The iterative loop with empirical verification caught issues that single-pass review missed.
