#!/usr/bin/env bash
set -euo pipefail

ROUND="${1:?usage: log-round.sh ROUND ARTIFACT VERDICT FINDINGS FIXED FP REVIEWER}"
ARTIFACT="${2:?}"
VERDICT="${3:?}"
FINDINGS="${4:?}"
FIXED="${5:?}"
FP="${6:?}"
REVIEWER="${7:-codex/unknown}"
PROJECT="${8:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SESSION="${CLAUDE_CODE_SESSION_ID:0:8}"
LOG_FILE="${ADVERSARIAL_REVIEW_LOG:-$HOME/.claude/hooks/adversarial-review.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPTH="${ADVERSARIAL_REVIEW_DEPTH:-standard}"

echo "$(date -Iseconds) | session=${SESSION:-unknown} | round=${ROUND} | artifact=${ARTIFACT} | verdict=${VERDICT} | findings=${FINDINGS} | fixed=${FIXED} | fp=${FP} | reviewer=${REVIEWER} | project=${PROJECT}" >> "$LOG_FILE"

ADVERSARIAL_REVIEW_DIR="${ADVERSARIAL_REVIEW_DIR:-.adversarial-review}" \
python3 "$SCRIPT_DIR/update-state.py" \
    "$ROUND" "$VERDICT" "$FINDINGS" "$FIXED" "$FP" "$ARTIFACT" "$DEPTH" "$REVIEWER" \
    > /dev/null
