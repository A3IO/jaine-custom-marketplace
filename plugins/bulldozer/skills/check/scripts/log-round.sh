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
SESSION="${CLAUDE_CODE_SESSION_ID:-unknown}"
SESSION="${SESSION:0:8}"
LOG_FILE="${BULLDOZER_LOG:-$HOME/.claude/hooks/bulldozer.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPTH="${BULLDOZER_DEPTH:-standard}"

BULLDOZER_REVIEW_DIR="${BULLDOZER_REVIEW_DIR:-.bulldozer}" \
python3 "$SCRIPT_DIR/update-state.py" \
    "$ROUND" "$VERDICT" "$FINDINGS" "$FIXED" "$FP" "$ARTIFACT" "$DEPTH" "$REVIEWER" \
    > /dev/null

mkdir -p "$(dirname "$LOG_FILE")"
if ! echo "$(date -Iseconds) | session=${SESSION} | round=${ROUND} | artifact=${ARTIFACT} | verdict=${VERDICT} | findings=${FINDINGS} | fixed=${FIXED} | fp=${FP} | reviewer=${REVIEWER} | project=${PROJECT}" >> "$LOG_FILE"; then
    echo "warning: state.json updated but log append to $LOG_FILE failed — audit trail incomplete" >&2
fi
