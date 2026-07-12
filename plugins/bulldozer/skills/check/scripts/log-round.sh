#!/usr/bin/env bash
set -euo pipefail

ROUND="${1:?usage: log-round.sh ROUND ARTIFACT VERDICT FINDINGS FIXED FP REVIEWER [PROJECT] [MANUAL_EXTRACTION_PENDING] [DURATION_S]}"
ARTIFACT="${2:?}"
VERDICT="${3:?}"
FINDINGS="${4:?}"
FIXED="${5:?}"
FP="${6:?}"
REVIEWER="${7:-codex/unknown}"
PROJECT="${8:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
MANUAL_EXTRACTION_PENDING="${9:-}"
DURATION_S="${10:-}"   # optional: codex exec wall-clock, threaded from the wrapper (#322 B4)
# reviewer format canon: codex/<model> — a bare model id gets the codex/ prefix
# (the live log carried codex/gpt-5.5 AND gpt-5.5 for the same concept, #322 B3)
[[ "$REVIEWER" != */* ]] && REVIEWER="codex/${REVIEWER}"
# Manual-extraction (wrapper exit-11) rounds call this with VERDICT=UNKNOWN +
# FINDINGS=0 (R4-F1 dogfood). UNKNOWN is the unambiguous audit marker that the
# reviewer produced prose but skipped LEDGER_PATCH and prose extraction was
# still owed at log time — no other code path emits verdict=UNKNOWN. The
# bulldozer.log line below stays FROZEN at findings=0 even after
# `update-state.py --mode=replace-extraction` reconciles the real count: the log
# is an append-only audit trail (roadmap-spec decision) — but replace-extraction
# now appends an `event=reconciled` CORRECTION line (#322 D6), so a naive miner
# can detect and supersede the frozen UNKNOWN line instead of trusting it.
# token-normalize BEFORE slicing — an adversarial env value must not split the
# pipe grammar (canonical grammar rule, #322 PR1 spec)
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

# event=round discriminator (#322 C2); depth= (B3); duration_s= only when measured (B4).
# Written via the canonical helper (lib/bulldozer_log.py — sanitization, 5MB rotation,
# session= from env, one stderr warning; Copilot #327): the shim always exits 0.
LOG_HELPER="$SCRIPT_DIR/../../../lib/bulldozer_log.py"
dur_kv=()
[[ -n "$DURATION_S" ]] && dur_kv=("duration_s=${DURATION_S}")
python3 "$LOG_HELPER" "$LOG_FILE" round \
    "round=${ROUND}" "artifact=${ARTIFACT}" "verdict=${VERDICT}" \
    "findings=${FINDINGS}" "fixed=${FIXED}" "fp=${FP}" "reviewer=${REVIEWER}" \
    "depth=${DEPTH}" ${dur_kv[@]+"${dur_kv[@]}"} "project=${PROJECT}" \
    || echo "warning: state.json updated but log append to $LOG_FILE failed — audit trail incomplete" >&2
