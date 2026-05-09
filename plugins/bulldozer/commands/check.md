---
description: Start or continue an adversarial review loop with external AI reviewer
argument-hint: "[quick|standard|exhaustive] [file|dir|diff]"
allowed-tools: ["Bash", "Read", "Edit", "Write"]
---

# Bulldozer Check

Use the `check` skill to orchestrate this review.

## Input

$ARGUMENTS

## If no arguments provided

Explain to the user:

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

## If arguments provided

Parse depth and artifact from $ARGUMENTS:
- First word matching `quick|standard|exhaustive` → depth (default: `standard`)
- Remaining → artifact path (file or directory)
- If only depth given, ask for artifact
- If only path given, use `standard` depth

## Orchestration

1. Load the check skill if not already loaded
2. Create per-review directory: `.bulldozer/${CLAUDE_CODE_SESSION_ID:0:8}-${artifact_basename}/`
3. Use `codex exec -o verdict-rN.txt` to get clean reviewer output
4. Follow the skill's step-by-step process: send → read verdict → verify → fix → log → repeat
5. Log every round with `${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/log-round.sh`
