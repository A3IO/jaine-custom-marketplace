---
description: Start or continue an adversarial review loop with external AI reviewer
argument-hint: "[quick|standard|exhaustive] [path/to/artifact]"
allowed-tools: ["Bash", "Read", "Edit", "Write"]
---

# Adversarial Review

Use the `adversarial-review` skill to orchestrate this review.

## Input

**Depth and artifact:** $ARGUMENTS (default: standard, artifact auto-detected)

## Instructions

1. Load the adversarial-review skill if not already loaded
2. Determine the artifact to review:
   - If artifact path provided in arguments, use it
   - Otherwise, check `.adversarial-review/state.json` for in-progress review
   - If neither, ask the user what to review
3. Check if this is a new review or continuation:
   - If `.adversarial-review/state.json` exists and matches the artifact, continue from last round
   - Otherwise, start fresh (Phase 0: setup + baseline capture)
4. Follow the skill's step-by-step process
5. Log every round using `${CLAUDE_PLUGIN_ROOT}/skills/adversarial-review/scripts/log-round.sh`
6. Update state using `python3 ${CLAUDE_PLUGIN_ROOT}/skills/adversarial-review/scripts/update-state.py`
