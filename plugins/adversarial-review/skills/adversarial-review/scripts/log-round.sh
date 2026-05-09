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
LOG_FILE="${ADVERSARIAL_REVIEW_LOG:-$HOME/.adversarial-review.log}"

echo "$(date -Iseconds) | round=${ROUND} | artifact=${ARTIFACT} | verdict=${VERDICT} | findings=${FINDINGS} | fixed=${FIXED} | fp=${FP} | reviewer=${REVIEWER} | project=${PROJECT}" >> "$LOG_FILE"
