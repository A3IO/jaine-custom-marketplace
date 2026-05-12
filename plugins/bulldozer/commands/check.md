---
description: Start or continue an adversarial review loop with external AI reviewer
argument-hint: "[quick|standard|exhaustive] [file|dir|diff]"
allowed-tools: ["Bash", "Read", "Edit", "Write", "AskUserQuestion"]
---

# Bulldozer Check

Use the `check` skill to orchestrate this review.

## Input

$ARGUMENTS

## Step 1: Model Selection (EVERY launch)

Before any other action, discover available models and ask the user which one to use.

**1a. Get available models** — run:
```bash
if ! codex_output=$(codex debug models 2>&1); then
    echo "ERROR: 'codex debug models' failed. Check: codex installed? logged in? (codex login)" >&2
    exit 1
fi
printf '%s' "$codex_output" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    print('ERROR: codex returned non-JSON output', file=sys.stderr)
    sys.exit(1)
models = data.get('models', [])
if not models:
    print('ERROR: empty model catalog', file=sys.stderr)
    sys.exit(1)
listed = [m for m in models if m.get('visibility') == 'list']
if not listed:
    print(f'ERROR: no models with visibility=list ({len(models)} models found, schema may have changed)', file=sys.stderr)
    sys.exit(1)
for m in listed:
    slug = m.get('slug')
    if not slug: continue
    name = m.get('display_name', slug)
    print(f'{slug}|{name}|{m.get("priority", 999)}')
"
```

If the snippet exits non-zero, tell the user to check `codex --version` and `codex login`, then ask them to type a model name manually.

**1b. Read saved preference** — if `.bulldozer/config.md` exists, read `reviewer_model` from its YAML frontmatter. If the file is malformed or missing the key, warn the user ("`.bulldozer/config.md` unreadable — ignoring saved preference") and let them pick fresh. Mark the saved model as "(Recommended)" in options.

**1c. Ask user** — via AskUserQuestion, show 4 models. Selection rules (in order):
1. **ALWAYS** include current global model from `~/.codex/config.toml` (line 1: `model = "..."`)
2. **ALWAYS** include last used model from `.bulldozer/config.md` (if different from global)
3. Fill remaining slots from: gpt-5.5, gpt-5.3-codex-spark, gpt-5.4-mini (skip gpt-5.4 and gpt-5.3-codex — redundant)
4. If global = last used, you get 3 slots for the above

This guarantees the user's configured model is never hidden by priority sorting.

**1d. Save choice** — update ONLY `reviewer_model` in `.bulldozer/config.md`, preserving any other keys. Pass to codex exec via `-m <model>`.

## Step 2: Parse arguments

If no arguments provided, explain to the user:

> **Bulldozer** sends ваш артефакт (спеку, код, конфиг) на ревью внешнему AI-рецензенту (Codex CLI), затем каждый finding проверяется эмпирически в коде, фиксится, и отправляется на повторное ревью — и так до вердикта GO.
>
> **Использование:**
> ```
> /bulldozer:check path/to/spec.md              — standard (до 3 раундов)
> /bulldozer:check quick path/to/config.json     — один раунд, только блокеры
> /bulldozer:check exhaustive docs/design.md     — до полного GO (макс 10 раундов)
> /bulldozer:check standard src/gateway/         — ревью директории
> ```
>
> **Уровни глубины:**
> - `quick` — один раунд, только критичные проблемы
> - `standard` — до 3 раундов, баланс глубины и скорости (по умолчанию)
> - `exhaustive` — крутится пока рецензент не скажет GO (для спек, управляющих автоматизацией)

Then ask: what artifact to review, and which depth level.

If arguments provided, parse depth and artifact from $ARGUMENTS:
- First word matching `quick|standard|exhaustive` → depth (default: `standard`)
- Remaining → artifact path (file or directory)
- If only depth given, ask for artifact
- If only path given, use `standard` depth

## Step 3: Orchestration

1. Load the check skill if not already loaded
2. Resolve project root (see SKILL.md §1b):
   ```bash
   PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
       echo "ERROR: not in a git repository." >&2; exit 1
   }
   ```
3. Select codex reasoning based on depth:
   - `quick` → `-c model_reasoning_effort=medium --ephemeral` + prompt prefix `SKIP SKILLS.`
   - `standard` / `exhaustive` → `-c model_reasoning_effort=xhigh`
4. Create per-review directory: `.bulldozer/${CLAUDE_CODE_SESSION_ID:0:8}-${artifact_basename}/`
5. Run `codex exec` in **FOREGROUND** (NEVER `run_in_background`) with all required flags:
   ```bash
   codex exec -s read-only -c model_reasoning_effort=<EFFORT> -m <MODEL> \
     -o "${REVIEW_DIR}/verdict-r${ROUND}.txt" \
     -C "$PROJECT_ROOT" \
     "<PROMPT>" \
     < /dev/null > "${REVIEW_DIR}/full-r${ROUND}.txt" 2>&1
   ```
   Check exit code. If non-zero: read last 20 lines of `full-r{N}.txt`, mark round as `crash`, report to user.
6. Read ONLY `verdict-r{N}.txt` — NEVER parse `full-r{N}.txt`
7. Extract `LEDGER_PATCH` from verdict, apply to `review-ledger.yml` (see SKILL.md for error handling)
8. Follow the skill's step-by-step process: send → read verdict → verify → fix → log → repeat
9. Log every round with:
   ```bash
   BULLDOZER_REVIEW_DIR="$REVIEW_DIR" BULLDOZER_DEPTH="$DEPTH" \
   ${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/log-round.sh \
     "$ROUND" "$ARTIFACT" "$VERDICT" "$FINDINGS" "$FIXED" "$FP" "$REVIEWER"
   ```
