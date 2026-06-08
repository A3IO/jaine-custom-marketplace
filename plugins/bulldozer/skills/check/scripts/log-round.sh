#!/usr/bin/env bash
set -euo pipefail

ROUND="${1:?usage: log-round.sh ROUND ARTIFACT VERDICT FINDINGS FIXED FP REVIEWER [PROJECT] [MANUAL_EXTRACTION_PENDING]}"
ARTIFACT="${2:?}"
VERDICT="${3:?}"
FINDINGS="${4:?}"
FIXED="${5:?}"
FP="${6:?}"
REVIEWER="${7:-codex/unknown}"
PROJECT="${8:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
MANUAL_EXTRACTION_PENDING="${9:-}"
# Manual-extraction (wrapper exit-11) rounds call this with VERDICT=UNKNOWN +
# FINDINGS=0 (R4-F1 dogfood). UNKNOWN is the unambiguous audit marker that the
# reviewer produced prose but skipped LEDGER_PATCH and prose extraction was
# still owed at log time — no other code path emits verdict=UNKNOWN. The
# bulldozer.log line below stays FROZEN at findings=0 even after
# `update-state.py --mode=replace-extraction` reconciles the real count: the log
# is an append-only audit trail (roadmap-spec decision), so the reconciled count
# + final GO/NO-GO verdict live in state.json's history entry, not here. A bare
# `verdict=UNKNOWN | findings=0` line therefore means "manual extraction
# pending/performed", NOT "reviewer found zero issues".
SESSION="${CLAUDE_CODE_SESSION_ID:-unknown}"
SESSION="${SESSION:0:8}"
LOG_FILE="${BULLDOZER_LOG:-$HOME/.claude/hooks/bulldozer.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPTH="${BULLDOZER_DEPTH:-standard}"

update_args=("$ROUND" "$VERDICT" "$FINDINGS" "$FIXED" "$FP" "$ARTIFACT" "$DEPTH" "$REVIEWER")
update_flags=()
if [[ "$MANUAL_EXTRACTION_PENDING" == "true" ]]; then
    update_flags+=("--manual-extraction-pending")
fi

BULLDOZER_REVIEW_DIR="${BULLDOZER_REVIEW_DIR:-.bulldozer}" \
python3 "$SCRIPT_DIR/update-state.py" ${update_flags[@]+"${update_flags[@]}"} "${update_args[@]}" \
    > /dev/null

mkdir -p "$(dirname "$LOG_FILE")"
if ! echo "$(date -Iseconds) | session=${SESSION} | round=${ROUND} | artifact=${ARTIFACT} | verdict=${VERDICT} | findings=${FINDINGS} | fixed=${FIXED} | fp=${FP} | reviewer=${REVIEWER} | project=${PROJECT}" >> "$LOG_FILE"; then
    echo "warning: state.json updated but log append to $LOG_FILE failed — audit trail incomplete" >&2
fi
