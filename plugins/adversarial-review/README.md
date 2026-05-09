# adversarial-review

Iterative adversarial review loop with external AI reviewer.

Send artifact to reviewer (Codex CLI) → parse findings → verify each empirically → fix confirmed → re-send → repeat until GO.

## Usage

```
/adversarial-review                          # standard (3 rounds max)
/adversarial-review quick path/to/file.md    # single round
/adversarial-review exhaustive               # until GO (max 10)
```

## Requirements

- `codex` CLI installed and authenticated (`npm i -g @openai/codex`)
- Git repository (reviewer needs `git ls-tree`, `git show`)

## Files

| File | Purpose |
|------|---------|
| `.adversarial-review/state.json` | Round state (gitignore this) |
| `.adversarial-review/last-review.txt` | Last reviewer output |
| `~/.adversarial-review.log` | Global audit log (append-only) |
| `.adversarial-review/config.md` | Optional project config |

## Log Format

```
2026-05-09T10:30:00+03:00 | round=1 | artifact=spec.md | verdict=NO-GO | findings=8 | fixed=7 | fp=1 | reviewer=codex/gpt-5.5 | project=/path
```

View: `column -t -s'|' ~/.adversarial-review.log`

## Origin

Developed from a real workflow that found 37 issues in 7 rounds (0 false positives) reviewing a REVERSING audit spec. The iterative loop with empirical verification caught issues that single-pass review missed.
