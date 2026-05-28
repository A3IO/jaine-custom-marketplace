#!/usr/bin/env bash
# Composer wrapper for /bulldozer:check rounds.
# See SKILL.md Step 2 and Issue #102 for design rationale.
#
# Exit code contract (partitioned by origin; see SKILL.md Step 3 table):
#   0       success (round logged)
#   1-5     parser outcomes (no LEDGER_PATCH / malformed / schema / PyYAML / IO)
#   10      pivot signal (max rounds reached without GO)
#   64      wrapper preflight / usage error (EX_USAGE) — bad CLI args or env
#   70      wrapper-internal failure (EX_SOFTWARE) — script path missing,
#           log-round failed, downstream helper crashed
#   71      codex exec crashed — original code preserved in stderr diagnostic
#
# Reserved codes do NOT overlap origins. If you see 1, parser produced it;
# if 64, wrapper rejected the call; if 71, codex itself crashed.
set -euo pipefail

usage() {
    cat <<'EOF'
bulldozer-round.sh — compose one round of /bulldozer:check end-to-end.

Required flags:
  --round N            Round number (1-based).
  --review-dir PATH    Per-review sandbox (e.g. .bulldozer/SESSION-ARTIFACT/).
  --artifact NAME      Human-readable artifact label (used in log + state.json).
  --depth LEVEL        quick | standard | exhaustive.
  --reviewer ID        e.g. codex/gpt-5.1.
  --prompt-file PATH   File whose contents are fed to codex as the prompt.
  --project-root PATH  Repository root for codex -C and relative paths.

Optional:
  --help               Print this message and exit 0.
EOF
}

if [[ "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

ROUND=""
REVIEW_DIR=""
ARTIFACT=""
DEPTH=""
REVIEWER=""
PROMPT_FILE=""
PROJECT_ROOT=""

# R1-F3a: wrapper preflight failures exit 64 (sysexits.h EX_USAGE) so they
# do NOT collide with parser exit 2 (malformed LEDGER_PATCH YAML). Before
# this change, the caller searching for .malformed.yml on exit 2 found
# nothing when the failure was actually a usage error. See issue #110
# dogfood comment 4557164679 (R1-F3).
#
# R2-F1 (hotfix dogfood round 2): value-taking flags at end of argv used to
# bail with raw exit 1 because case bodies read `$2` directly under set -u.
# require_value aborts with 64 BEFORE the unbound-variable error fires.
require_value() {
    local flag="$1"; local remaining="$2"
    if (( remaining < 2 )); then
        echo "error: $flag requires a value" >&2
        exit 64
    fi
}

# Emit a structured STOP diagnostic and exit. After the 7-round dogfood
# loop on PR #111 surfaced 17 unique reserved-code-leak sites — each
# producing a 5-7 line `{ echo STOP; echo " <line>"; ... } >&2 + exit N`
# block — this helper enforces consistent formatting at every emission
# site and makes new failure-path additions a 1-line call. See #110
# "Code simplification" item promoted to High after PR #111.
#
# Usage: _emit_stop EXIT_CODE REASON [INDENTED_DETAIL_LINE...]
# Output goes to stderr; ROUND and ARTIFACT are read from outer scope
# (always set by preflight before any _emit_stop call would fire).
_emit_stop() {
    local code="$1"; shift
    local reason="$1"; shift
    {
        echo "STOP: round=${ROUND} artifact=${ARTIFACT} — ${reason}"
        local line
        for line in "$@"; do
            echo "      ${line}"
        done
    } >&2
    exit "$code"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --round)        require_value "$1" "$#"; ROUND="$2"; shift 2 ;;
        --review-dir)   require_value "$1" "$#"; REVIEW_DIR="$2"; shift 2 ;;
        --artifact)     require_value "$1" "$#"; ARTIFACT="$2"; shift 2 ;;
        --depth)        require_value "$1" "$#"; DEPTH="$2"; shift 2 ;;
        --reviewer)     require_value "$1" "$#"; REVIEWER="$2"; shift 2 ;;
        --prompt-file)  require_value "$1" "$#"; PROMPT_FILE="$2"; shift 2 ;;
        --project-root) require_value "$1" "$#"; PROJECT_ROOT="$2"; shift 2 ;;
        --help)         usage; exit 0 ;;
        *)
            echo "error: unknown flag: $1" >&2
            usage >&2
            exit 64
            ;;
    esac
done

missing=()
[[ -z "$ROUND"        ]] && missing+=(--round)
[[ -z "$REVIEW_DIR"   ]] && missing+=(--review-dir)
[[ -z "$ARTIFACT"     ]] && missing+=(--artifact)
[[ -z "$DEPTH"        ]] && missing+=(--depth)
[[ -z "$REVIEWER"     ]] && missing+=(--reviewer)
[[ -z "$PROMPT_FILE"  ]] && missing+=(--prompt-file)
[[ -z "$PROJECT_ROOT" ]] && missing+=(--project-root)

if (( ${#missing[@]} > 0 )); then
    echo "error: missing required flag(s): ${missing[*]}" >&2
    usage >&2
    exit 64
fi

# Reviewer ID format is "provider/model" (e.g. codex/gpt-5.1) so the ledger
# records a consistent label and the wrapper can extract the model name for
# codex's -m flag without a separate --model argument.
MODEL="${REVIEWER#*/}"
if [[ "$MODEL" == "$REVIEWER" || -z "$MODEL" ]]; then
    echo "error: --reviewer must be in form 'provider/model' (got: $REVIEWER)" >&2
    exit 64
fi

# R4-F3 + R5-F1: require BOTH regular file (-f) AND readable (-r).
# -r alone admits /dev/null and other char/block devices (readable but
# returning empty content via $(</dev/null) — wrapper would invoke codex
# with empty prompt and burn real $). -f alone admits chmod-000 files
# (preflight passes, command substitution then fails raw exit 1 — collides
# with parser-no-LEDGER). Combined check covers both failure shapes.
if [[ ! -f "$PROMPT_FILE" || ! -r "$PROMPT_FILE" ]]; then
    echo "error: --prompt-file must be a readable regular file: $PROMPT_FILE" >&2
    exit 64
fi

# R1-F3a: validate --depth at preflight so codex isn't invoked for a
# doomed depth. Previously caught only inside the case statement after
# codex_args was built (line ~91-103); the bad-depth path still ran
# `mkdir` and burned a stub round.
case "$DEPTH" in
    quick|standard|exhaustive) : ;;
    *)
        echo "error: --depth must be quick|standard|exhaustive (got: $DEPTH)" >&2
        exit 64
        ;;
esac

# R3-F2: wrap mkdir so failure (parent is a non-dir, EACCES, fs full)
# maps to wrapper exit 70 instead of leaking raw 1 under set -e.
if ! mkdir -p "$REVIEW_DIR" 2>/dev/null; then
    _emit_stop 70 "cannot create review directory." \
        "Path: ${REVIEW_DIR}" \
        "Causes: parent is not a directory, EACCES, fs full. Fix --review-dir and retry."
fi
VERDICT_FILE="${REVIEW_DIR}/verdict-r${ROUND}.txt"
FULL_LOG="${REVIEW_DIR}/full-r${ROUND}.txt"

# R6-F1: pre-write probe for FULL_LOG (symmetric with the PARSED_FILE
# probe further down). codex stdout redirects to FULL_LOG; if the path
# is unwritable (pre-existing chmod-000 file, parent EACCES, fs full),
# bash redirection fails BEFORE codex runs → codex_exit=1 → diagnostic
# `tail FULL_LOG` ALSO fails under pipefail → wrapper exits raw 1.
# Catching the write-failure here exits 70 cleanly with no codex spend.
if ! : > "$FULL_LOG" 2>/dev/null; then
    _emit_stop 70 "cannot write full log." \
        "Path: ${FULL_LOG}" \
        "Causes: target exists unwritable, parent dir EACCES, fs full."
fi

# Depth-specific codex configuration mirrors SKILL.md Step 2. The
# unknown-depth branch is unreachable here because preflight already
# rejected anything outside {quick, standard, exhaustive} with exit 64.
codex_args=(exec -s read-only -m "$MODEL" -o "$VERDICT_FILE" -C "$PROJECT_ROOT")
prompt_prefix=""
case "$DEPTH" in
    quick)
        codex_args+=(-c model_reasoning_effort=medium --ephemeral)
        prompt_prefix="SKIP SKILLS. "
        ;;
    standard|exhaustive)
        codex_args+=(-c model_reasoning_effort=xhigh)
        ;;
esac

prompt_body="$(<"$PROMPT_FILE")"

# FOREGROUND ONLY (NEVER run_in_background) — -o is written LAST by codex,
# polling is unreliable. stdin closed to prevent waiting on auth prompts;
# stderr merged into FULL_LOG for crash diagnostics.
codex_exit=0
codex "${codex_args[@]}" "${prompt_prefix}${prompt_body}" \
    < /dev/null > "$FULL_LOG" 2>&1 || codex_exit=$?

if (( codex_exit != 0 )); then
    # Map codex non-zero to wrapper exit 71 (sysexits.h EX_OSERR-style
    # "external command failure"). Raw passthrough would collide with
    # reserved codes: codex exit 1 looks like parser-no-LEDGER_PATCH,
    # exit 10 looks like the pivot signal (and caller searches for a
    # pivot-rN.json that was never written). Original codex code is
    # preserved in the diagnostic so the operator can debug.
    # See issue #110 comment 4557164679 (dogfood R1-F1).
    {
        echo "error: codex exec failed with exit code ${codex_exit}"
        echo "       round=${ROUND} reviewer=${REVIEWER} depth=${DEPTH}"
        echo "       last lines of ${FULL_LOG}:"
        # Defense-in-depth: -r covers race-between-write-and-readback
        # (something chmod'd FULL_LOG to 000 after codex wrote it).
        # `|| true` keeps pipefail from killing the diagnostic before
        # we can `exit 71`.
        if [[ -r "$FULL_LOG" ]]; then
            tail -n 20 "$FULL_LOG" 2>/dev/null | sed 's/^/         /' || true
        else
            echo "         (full log not readable: ${FULL_LOG})"
        fi
    } >&2
    exit 71
fi

# R1-F2 guard: codex can exit 0 but leave the -o file empty (or unwritten —
# rare bug under fs-full / EACCES). Without this guard, parser would see
# zero bytes, return 1 ("no LEDGER_PATCH"), wrapper would exit 1 → caller
# routes to manual prose extraction. SKILL.md error table documents this
# exact case as the rerun-same-round path, so wrap it as IO failure
# (exit 5) instead of leaking through the parser-fallback branch.
if [[ ! -s "$VERDICT_FILE" ]]; then
    _emit_stop 5 "codex exited 0 but verdict file is empty or missing." \
        "Expected non-empty ${VERDICT_FILE}. Check ${FULL_LOG} for codex behavior, then retry the round (often transient)."
fi

# ---------------------------------------------------------------------------
# Step 3-4: extract LEDGER_PATCH via parser, branch on exit code.
# CLAUDE_PLUGIN_ROOT is set by Claude Code when invoking the skill; fall back
# to the script's own directory so the wrapper also works when invoked
# directly (e.g. from tests via PLUGIN_ROOT).
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARSER="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/parse-ledger-patch.py}"
PARSER="${PARSER:-${SCRIPT_DIR}/parse-ledger-patch.py}"
PARSED_FILE="${REVIEW_DIR}/parsed-r${ROUND}.json"

# R1-F3c: validate parser path BEFORE python3 invocation. Stale
# CLAUDE_PLUGIN_ROOT (cache path no longer containing the script after
# `jaine-sync plugins update`) caused `python3 missing-parser.py` to
# exit 2 — wrapper then printed "STOP: malformed LEDGER_PATCH YAML"
# and the operator hunted for a non-existent .malformed.yml. Pre-
# validation maps this to exit 70 (EX_SOFTWARE — wrapper-internal)
# with a diagnostic naming the missing path and the env var to fix.
if [[ ! -f "$PARSER" ]]; then
    _emit_stop 70 "parser script not found at expected path." \
        "Expected parse-ledger-patch.py at: ${PARSER}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi

# R4-F2/R4-F4: bash evaluates `> "$PARSED_FILE"` BEFORE invoking python.
# If the redirection fails for ANY reason — target is a directory, target
# exists as read-only regular file, parent dir unwritable, fs full —
# python never runs, parser_exit=1 from bash's failure, wrapper case 1
# routes to manual fallback (misleading: parser didn't execute).
#
# Pre-write probe (`: > "$PARSED_FILE"`) exercises the exact same kernel
# open(O_WRONLY|O_CREAT|O_TRUNC) path that python's stdout redirect uses.
# If the probe fails, we know the parser invocation would fail too, and
# we map it to wrapper exit 70 with a clear diagnostic. Replaces the
# narrower `-d` check from R4-F2 (kept that test as the directory
# subcase; R4-F4 added a regular-unwritable-file test for general
# coverage). TOCTOU is acceptable here: REVIEW_DIR is wrapper-owned and
# the round is single-threaded.
if ! : > "$PARSED_FILE" 2>/dev/null; then
    _emit_stop 70 "cannot write parsed file." \
        "Path: ${PARSED_FILE}" \
        "Causes: target is a directory, target unwritable (chmod), parent dir EACCES, fs full."
fi

parser_exit=0
python3 "$PARSER" --file "$VERDICT_FILE" > "$PARSED_FILE" || parser_exit=$?

case "$parser_exit" in
    0)
        : # success path — wrapper continues (log-round composition in next commit)
        ;;
    1)
        # Reviewer narrated the verdict but skipped the LEDGER_PATCH block.
        # Caller (Claude) falls back to extracting findings from prose by
        # reading $VERDICT_FILE directly. Same exit code so the SKILL.md
        # branch can react without parsing our stderr.
        echo "warning: no LEDGER_PATCH block — caller must extract findings manually from ${VERDICT_FILE}" >&2
        exit 1
        ;;
    2)
        # Malformed YAML inside LEDGER_PATCH block. Parser already saved
        # the raw block as a .malformed.yml sibling and printed YAML error
        # details on its own stderr; the wrapper adds round/artifact context
        # so the caller can route the failure without re-parsing parser output.
        malformed_sibling="${VERDICT_FILE%.txt}.malformed.yml"
        _emit_stop 2 "malformed LEDGER_PATCH YAML." \
            "Inspect ${malformed_sibling}, then either fix the reviewer template or re-run the round."
        ;;
    3)
        # Schema violation — YAML parsed fine but structurally wrong (e.g.
        # missing 'findings' field, per-finding without 'id'). Patch MUST
        # NOT be applied to the ledger; the caller must prompt the user.
        _emit_stop 3 "schema violation in LEDGER_PATCH." \
            "Do NOT apply this patch. Inspect ${VERDICT_FILE} for the offending block, then re-run after fixing the reviewer template."
        ;;
    4)
        # PyYAML missing — environment problem, not a reviewer problem.
        # User-actionable: install pyyaml. Wrapper packages the remediation
        # prominently so the operator doesn't have to scan parser stderr.
        _emit_stop 4 "PyYAML is not installed (parser dependency)." \
            "Run: pip install pyyaml   (then retry the round)"
        ;;
    5)
        # File / stdin IO failure — verdict file missing (codex exited 0 but
        # didn't write -o; rare bug), pipe truncated, fd exhausted, etc.
        # Operationally distinct from exit 2/3 (reviewer-side bugs): this is
        # almost always transient. Suggest retrying before escalating.
        _emit_stop 5 "verdict file missing or unreadable (parser IO failure)." \
            "Expected ${VERDICT_FILE}. Check codex output in ${FULL_LOG}, then retry the round (often transient)."
        ;;
    *)
        # R2-F2: unexpected parser exit codes (anything outside 0-5) must
        # NOT pass through raw — that would leak into wrapper-reserved
        # ranges if the parser ever returns 64/70/71/10/etc. Map to 70
        # (wrapper-internal) and preserve the original code in the
        # diagnostic so the operator can debug the parser.
        _emit_stop 70 "parse-ledger-patch.py exited ${parser_exit} (outside documented 0-5 range)." \
            "Verdict file: ${VERDICT_FILE}" \
            "Check parser version / behavior; original exit code preserved above."
        ;;
esac

# ---------------------------------------------------------------------------
# Step 5-7: derive findings count + verdict, call log-round.sh, emit state.
# Forgetting any of these is the discipline failure #102 exists to eliminate.
# ---------------------------------------------------------------------------
# Read findings count AND verdict in one python3 call. Verdict prefers
# `meta.verdict` from the parser (preserves explicit `verdict: no_go +
# findings: []` — the legitimate "reviewer rejects but can't enumerate"
# signal). Falls back to structural len(findings) check only when meta
# omits a verdict. BUG-2 fix: pure structural inference was silently
# flipping explicit NO-GO to GO when findings happened to be empty.
#
# R2-F2: capture failure so set -e doesn't bubble python3 exit raw —
# corrupted parsed-rN.json (parser bug, disk corruption, race with
# concurrent writer) would otherwise look like parser-no-LEDGER (exit
# 1) to the caller.
parser_out_exit=0
parser_out=$(python3 -c '
import json, sys
with open(sys.argv[1]) as fp:
    data = json.load(fp)
findings = data.get("findings", [])
meta_verdict = (data.get("meta") or {}).get("verdict")
if meta_verdict is not None:
    verdict = "GO" if str(meta_verdict).strip().lower() == "go" else "NO-GO"
else:
    verdict = "GO" if not findings else "NO-GO"
print(f"{len(findings)}|{verdict}")
' "$PARSED_FILE") || parser_out_exit=$?

if (( parser_out_exit != 0 )); then
    _emit_stop 70 "failed to read parsed findings/verdict (python3 exit ${parser_out_exit})." \
        "Parsed file: ${PARSED_FILE}" \
        "Likely cause: corrupted JSON output from parser. Inspect the file and the parser stderr."
fi

findings_count="${parser_out%|*}"
VERDICT="${parser_out#*|}"

# Caller passes fix/false-positive accounting via env vars so the wrapper
# CLI stays narrow (no extra flags per round). Defaults to 0/0 for the
# initial post-review log; the caller can re-invoke or post-update later.
#
# R1-F3b: validate at wrapper boundary so non-numeric input maps to exit
# 64 (usage error), not exit 1 from update-state.py ValueError under
# set -e (which would falsely look like parser-no-LEDGER_PATCH).
FIXED="${BULLDOZER_FIXED:-0}"
FP="${BULLDOZER_FP:-0}"
if [[ ! "$FIXED" =~ ^[0-9]+$ ]]; then
    echo "error: BULLDOZER_FIXED must be a non-negative integer (got: '$FIXED')" >&2
    exit 64
fi
if [[ ! "$FP" =~ ^[0-9]+$ ]]; then
    echo "error: BULLDOZER_FP must be a non-negative integer (got: '$FP')" >&2
    exit 64
fi

LOG_ROUND="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/log-round.sh}"
LOG_ROUND="${LOG_ROUND:-${SCRIPT_DIR}/log-round.sh}"

# R1-F3c (companion to parser path check): pre-validate log-round.sh
# path so missing-script doesn't leak as bash "command not found" (exit
# 127) or an opaque set -e bail.
if [[ ! -f "$LOG_ROUND" ]]; then
    _emit_stop 70 "log-round.sh not found at expected path." \
        "Expected log-round.sh at: ${LOG_ROUND}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi

# Pin BULLDOZER_REVIEW_DIR to our --review-dir so log-round/update-state
# always write state.json into the per-review sandbox the caller asked for,
# regardless of what the surrounding env happens to have set.
#
# R1-F3d: capture log-round non-zero so set -e doesn't bubble it as the
# raw exit code. update-state.py's sys.exit(1) on json.JSONDecodeError
# / OSError would otherwise look like parser-no-LEDGER_PATCH (exit 1) to
# the caller. Map any downstream helper failure to wrapper exit 70.
log_round_exit=0
BULLDOZER_REVIEW_DIR="$REVIEW_DIR" BULLDOZER_DEPTH="$DEPTH" \
    bash "$LOG_ROUND" "$ROUND" "$ARTIFACT" "$VERDICT" \
        "$findings_count" "$FIXED" "$FP" "$REVIEWER" "$PROJECT_ROOT" \
        > /dev/null || log_round_exit=$?

if (( log_round_exit != 0 )); then
    _emit_stop 70 "log-round.sh failed with exit ${log_round_exit}." \
        "Helper script: ${LOG_ROUND}" \
        "Common causes: corrupted state.json in ${REVIEW_DIR}, EACCES on review dir, update-state.py environment issue."
fi

# Depth → max_rounds mapping. Computed unconditionally (not gated by trajectory
# display) so the AskUser-pivot guard below can fire on round 1 quick depth too.
case "$DEPTH" in
    quick)      max_rounds=1 ;;
    standard)   max_rounds=3 ;;
    exhaustive) max_rounds=10 ;;
    *)          max_rounds=0 ;;
esac

# Step 8 (U7): trajectory display — only round >= 2, since round 1 has
# nothing to plot. Goes to stderr (informational) so stdout stays JSON.
#
# R2-F3: capture failure so set -e doesn't bubble python3 exit raw —
# corrupted state.json post-log-round (race delete, fs corruption,
# update-state.py bug) would otherwise look like parser-no-LEDGER
# (exit 1) to the caller.
if (( ROUND >= 2 )); then
    trajectory_exit=0
    python3 - "$ROUND" "$max_rounds" "${REVIEW_DIR}/state.json" <<'PYEOF' >&2 || trajectory_exit=$?
import json
import sys

round_num = int(sys.argv[1])
max_rounds = int(sys.argv[2])
state_path = sys.argv[3]

with open(state_path) as fp:
    state = json.load(fp)

history = state.get("history", [])
trajectory = [h.get("findings", 0) for h in history]
last = history[-1] if history else {"verdict": "UNKNOWN", "findings": 0}
last_verdict = last.get("verdict", "UNKNOWN")
last_findings = last.get("findings", 0)

noun = "finding" if last_findings == 1 else "findings"
print(
    f"[bulldozer/check] Round {round_num}/{max_rounds} — "
    f"verdict: {last_verdict} — {last_findings} {noun} open"
)

traj_str = " → ".join(str(f) for f in trajectory)
window = trajectory[-3:]
avg = sum(window) / len(window) if window else 0
print(f"Trajectory: {traj_str}  (avg last 3: {avg:.1f})")
PYEOF
    if (( trajectory_exit != 0 )); then
        _emit_stop 70 "trajectory rendering failed (python3 exit ${trajectory_exit})." \
            "State file: ${REVIEW_DIR}/state.json" \
            "Likely cause: corrupted state.json post-log-round, or python3 environment issue."
    fi
fi

# Step 7: emit state.json contents on stdout so the caller can derive
# trajectory/AskUser-pivot decisions without re-reading the file.
#
# R2-F3: state.json may not exist if log-round helper was stubbed or
# failed silently. Pre-check rather than letting `cat` exit 1 leak.
# R4-F1: [[ -f ]] checks existence, NOT readability. An existing but
# unreadable file (EACCES, chmod 000) would slip past and crash cat
# with exit 1 — collides with parser-no-LEDGER. Wrap cat too.
if [[ ! -f "${REVIEW_DIR}/state.json" ]]; then
    _emit_stop 70 "state.json missing after log-round." \
        "Expected: ${REVIEW_DIR}/state.json" \
        "log-round.sh reported success but no state was written. Inspect helper."
fi
cat_exit=0
cat "${REVIEW_DIR}/state.json" || cat_exit=$?
if (( cat_exit != 0 )); then
    _emit_stop 70 "state.json exists but cannot be read (cat exit ${cat_exit})." \
        "File: ${REVIEW_DIR}/state.json" \
        "Likely cause: permission denied (EACCES). Fix permissions and retry."
fi

# Step 9 (U5a): AskUser pivot signal — fires when we've hit the depth's
# max rounds without a GO verdict. Caller (Claude) reads exit 10 + the
# pivot file and wraps in AskUserQuestion (continue / restructure /
# accept-with-TODO).
if [[ -n "${max_rounds:-}" ]] && (( ROUND >= max_rounds )) && [[ "$VERDICT" != "GO" ]]; then
    PIVOT_FILE="${REVIEW_DIR}/pivot-r${ROUND}.json"
    # R2-F3: pivot write may fail (EACCES on review dir, fs full, python3
    # crash). Capture so set -e doesn't bubble python3 exit raw — exit 10
    # is meaningful ONLY when the pivot file actually exists for the
    # caller to read.
    pivot_exit=0
    python3 - "$ROUND" "$max_rounds" "$findings_count" "$DEPTH" "$ARTIFACT" "$PIVOT_FILE" <<'PYEOF' || pivot_exit=$?
import json
import sys

round_num, max_rounds, open_findings, depth, artifact, pivot_path = (
    int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]),
    sys.argv[4], sys.argv[5], sys.argv[6],
)
pivot = {
    "trigger": "max_rounds_reached",
    "round": round_num,
    "max_rounds": max_rounds,
    "depth": depth,
    "artifact": artifact,
    "open_findings": open_findings,
    # AskUserQuestion-compatible fields below: caller can pass these
    # directly to the tool without renaming or synthesizing missing keys.
    "question": (
        f"Reached max rounds ({max_rounds}) without GO — "
        f"{open_findings} finding(s) open. How to proceed?"
    ),
    "header": "Pivot",  # chip label, ≤12 chars per AskUserQuestion schema
    "multiSelect": False,
    "options": [
        {
            "label": "continue",
            "description": "Run another round (exceeds max for this depth)",
        },
        {
            "label": "restructure",
            "description": "Pause review, restructure the artifact, re-launch /bulldozer:check",
        },
        {
            "label": "accept-with-TODO",
            "description": "Accept current state, log open findings as project TODOs",
        },
    ],
}
with open(pivot_path, "w") as fp:
    json.dump(pivot, fp, indent=2)
PYEOF
    if (( pivot_exit != 0 )) || [[ ! -f "$PIVOT_FILE" ]]; then
        _emit_stop 70 "pivot file write failed (python3 exit ${pivot_exit})." \
            "Expected pivot file: ${PIVOT_FILE}" \
            "Exit 10 suppressed because caller cannot read a missing pivot file."
    fi
    echo "PIVOT: max rounds reached without GO. See ${PIVOT_FILE} for AskUserQuestion options." >&2
    exit 10
fi
