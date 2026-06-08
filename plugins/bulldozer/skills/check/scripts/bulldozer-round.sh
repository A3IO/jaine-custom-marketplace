#!/usr/bin/env bash
# Composer wrapper for /bulldozer:check rounds.
# See SKILL.md Step 2 and Issue #102 for design rationale.
#
# Exit code contract (partitioned by origin; see SKILL.md Step 3 table):
#   0       success (round logged)
#   2-5     parser outcomes (malformed / schema / PyYAML / IO)
#   10      pivot signal (max rounds reached without GO)
#   11      manual-extraction required (PR-1, issue #110 B5): reviewer
#           produced prose but no LEDGER_PATCH block. Wrapper has ALREADY
#           logged the round to state.json with verdict=UNKNOWN +
#           manual_extraction_pending=true; caller must read verdict file,
#           extract findings, and call update-state.py with
#           --mode=replace-extraction to overwrite the placeholder entry.
#   64      wrapper preflight / usage error (EX_USAGE) — bad CLI args or env
#   70      wrapper-internal failure (EX_SOFTWARE) — script path missing,
#           log-round failed, downstream helper crashed
#   71      codex exec crashed — original code preserved in stderr diagnostic
#
# Reserved codes do NOT overlap origins. Parser exit 1 (no LEDGER_PATCH)
# is mapped to wrapper exit 11 post-state-write, NOT raw 1. If you see
# 64, wrapper rejected the call; if 71, codex itself crashed.
set -euo pipefail

# Hoisted to top so the R1-F1 pre-round guard (below) can reference
# update-state.py in its recovery diagnostic before the parser block
# resolves PARSER/LOG_ROUND from CLAUDE_PLUGIN_ROOT (with this dir as
# fallback). Single definition; the parser block reuses this value.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# D1 (#110): pre-write probe helper. Runs the exact
# open(O_WRONLY|O_CREAT|O_TRUNC) kernel path (`: > PATH`) a later redirect will
# use; on failure emits STOP 70 with REASON + the path + caller DETAILs and
# exits. Two file-probe sites share this (FULL_LOG, PARSED_FILE). The mkdir
# REVIEW_DIR probe is a directory-create (different op) and stays inline.
_probe_writable() {
    local path="$1"; shift
    local reason="$1"; shift
    if ! : > "$path" 2>/dev/null; then
        _emit_stop 70 "$reason" "Path: ${path}" "$@"
    fi
}

# D2 (#110): single home for the sibling-script path-resolution pattern. NAME is
# a script under skills/check/scripts/. Prefers CLAUDE_PLUGIN_ROOT (set by
# Claude Code when invoking the skill); falls back to SCRIPT_DIR (which IS
# skills/check/scripts/) so direct/test invocation works. Output is byte-for-byte
# the two-line `X=...; X=${X:-...}` form it replaces at 5 sites (PARSER,
# LOG_ROUND, RENDER_TRAJECTORY, EMIT_PIVOT, READ_DEPTH_CONFIG). DEPTH_CONFIG
# resolves a data/ asset (different subdir + `../` fallback) and stays inline.
_resolve_script() {
    local name="$1"
    local p="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/${name}}"
    printf '%s' "${p:-${SCRIPT_DIR}/${name}}"
}

# B2 (#110): single home for parser-exit diagnostics. Each documented parser
# exit code (2-5, plus the catch-all for anything outside 0-5) maps to a wrapper
# _emit_stop call here, so adding/altering a parser exit code touches ONE place
# instead of a sprawling case body. exit 0 (success) and exit 1 (manual
# extraction, PR-1 B5) are NOT handled here — they have control-flow side
# effects (continue / log-round + exit 11), not just a diagnostic. Reads
# VERDICT_FILE / FULL_LOG from outer scope (both set before the parser case).
_emit_parser_exit_diagnostic() {
    local code="$1"
    case "$code" in
        2)
            # Malformed YAML inside LEDGER_PATCH. Parser saved the raw block as
            # a .malformed.yml sibling + printed YAML details on its own stderr;
            # add round/artifact context so the caller can route without
            # re-parsing parser output.
            _emit_stop 2 "malformed LEDGER_PATCH YAML." \
                "Inspect ${VERDICT_FILE%.txt}.malformed.yml, then either fix the reviewer template or re-run the round."
            ;;
        3)
            # Schema violation — YAML parsed fine but structurally wrong (e.g.
            # missing 'findings', per-finding without 'id'). Patch MUST NOT be
            # applied to the ledger; the caller must prompt the user.
            _emit_stop 3 "schema violation in LEDGER_PATCH." \
                "Do NOT apply this patch. Inspect ${VERDICT_FILE} for the offending block, then re-run after fixing the reviewer template."
            ;;
        4)
            # PyYAML missing — environment problem, not a reviewer problem.
            # Wrapper packages the remediation prominently so the operator
            # doesn't have to scan parser stderr.
            _emit_stop 4 "PyYAML is not installed (parser dependency)." \
                "Run: python3 -m pip install pyyaml   (or: uv pip install pyyaml; then retry the round)"
            ;;
        5)
            # File / stdin IO failure — verdict missing (codex exited 0 but
            # didn't write -o; rare bug), pipe truncated, fd exhausted. Distinct
            # from 2/3 (reviewer-side): almost always transient. Suggest retry.
            _emit_stop 5 "verdict file missing or unreadable (parser IO failure)." \
                "Expected ${VERDICT_FILE}. Check codex output in ${FULL_LOG}, then retry the round (often transient)."
            ;;
        *)
            # R2-F2: unexpected parser exit codes (outside 0-5) must NOT pass
            # through raw — that would leak into wrapper-reserved ranges if the
            # parser ever returns 64/70/71/10/etc. Map to 70 and preserve the
            # original code in the diagnostic so the operator can debug.
            _emit_stop 70 "parse-ledger-patch.py exited ${code} (outside documented 0-5 range)." \
                "Verdict file: ${VERDICT_FILE}" \
                "Check parser version / behavior; original exit code preserved above."
            ;;
    esac
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

# A3 (#110): --round must be a positive integer (1-based, per the usage
# block). Without this, a non-numeric value flowed to update-state.py →
# ValueError → wrapper exit 70 with a misleading "log-round.sh failed"
# diagnostic; "0" breaks the verdict-r0 filenames and the
# (( ROUND >= max_rounds )) pivot guard. Reject at preflight (EX_USAGE).
if [[ ! "$ROUND" =~ ^[1-9][0-9]*$ ]]; then
    echo "error: --round must be a positive integer (1-based), got: $ROUND" >&2
    exit 64
fi

# Reviewer ID format is "provider/model" (e.g. codex/gpt-5.1) so the ledger
# records a consistent label and the wrapper can extract the model name for
# codex's -m flag without a separate --model argument.
# A2 (#110) + R1-F3 dogfood: require a non-empty provider and a non-empty
# model, allowing the model to contain slashes (routed slugs like
# codex/openai/gpt-4o → MODEL=openai/gpt-4o). The old `#*/` +
# (MODEL==REVIEWER || -z MODEL) test admitted leading-slash (/gpt → MODEL=gpt,
# empty provider). ^[^/]+/.+$ rejects the genuinely-broken shapes — no-slash
# (codex), leading-slash (/gpt), trailing-slash (codex/, empty model) — while
# permitting multi-segment models. MODEL = everything after the first slash.
if [[ ! "$REVIEWER" =~ ^[^/]+/.+$ ]]; then
    echo "error: --reviewer must be in form 'provider/model' (got: $REVIEWER)" >&2
    exit 64
fi
MODEL="${REVIEWER#*/}"

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

# B1 (#110): depth parameters (max_rounds, codex reasoning effort, --ephemeral
# toggle, quick-depth prompt prefix) come from data/depth-config.json — the
# single source of truth shared with the SKILL.md "Depth Levels" table
# (TestDepthConfigContract guards drift). Resolve the config + reader script
# with the same CLAUDE_PLUGIN_ROOT-with-SCRIPT_DIR fallback as PARSER/LOG_ROUND.
# Reading at preflight keeps the early-rejection property: an unknown depth
# exits 64 BEFORE codex is invoked, so no doomed round burns a stub.
DEPTH_CONFIG="${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/skills/check/data/depth-config.json}"
DEPTH_CONFIG="${DEPTH_CONFIG:-${SCRIPT_DIR}/../data/depth-config.json}"
if [[ ! -f "$DEPTH_CONFIG" ]]; then
    _emit_stop 70 "depth-config.json not found at expected path." \
        "Expected at: ${DEPTH_CONFIG}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi
READ_DEPTH_CONFIG="$(_resolve_script read-depth-config.py)"
if [[ ! -f "$READ_DEPTH_CONFIG" ]]; then
    _emit_stop 70 "read-depth-config.py not found at expected path." \
        "Expected at: ${READ_DEPTH_CONFIG}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi

# read-depth-config.py exits 2 for an unknown depth (→ usage error 64) and 3
# for a missing/corrupt config (→ wrapper-internal 70). Output is TAB-delimited
# (prompt_prefix LAST so its significant trailing space survives the read —
# only TAB is an IFS delimiter here, never the embedded space).
depth_cfg_exit=0
depth_cfg_out=$(python3 "$READ_DEPTH_CONFIG" "$DEPTH_CONFIG" "$DEPTH") || depth_cfg_exit=$?
case "$depth_cfg_exit" in
    0) : ;;
    2)
        echo "error: --depth must be one of the keys in depth-config.json (got: $DEPTH)" >&2
        exit 64
        ;;
    *)
        _emit_stop 70 "depth-config.json unreadable or corrupt (read-depth-config.py exit ${depth_cfg_exit})." \
            "Config: ${DEPTH_CONFIG}"
        ;;
esac
IFS=$'\t' read -r max_rounds reasoning ephemeral prompt_prefix <<< "$depth_cfg_out"

# R3-F2: wrap mkdir so failure (parent is a non-dir, EACCES, fs full)
# maps to wrapper exit 70 instead of leaking raw 1 under set -e.
if ! mkdir -p "$REVIEW_DIR" 2>/dev/null; then
    _emit_stop 70 "cannot create review directory." \
        "Path: ${REVIEW_DIR}" \
        "Causes: parent is not a directory, EACCES, fs full. Fix --review-dir and retry."
fi

# Canonicalize REVIEW_DIR to absolute path AFTER mkdir succeeds (R3-F1 dogfood):
# downstream diagnostics (MANUAL_EXTRACTION_REQUIRED stderr, recovery command
# Claude copy-pastes for update-state.py --mode=replace-extraction) MUST print
# an absolute path. Otherwise Claude's later Bash tool invocations may run from
# a different cwd and the relative path resolves to the wrong .bulldozer/...
# directory. update-state.py also calls .resolve() defensively, but the
# user-visible recovery command in stderr is the load-bearing display surface.
REVIEW_DIR="$(cd "$REVIEW_DIR" && pwd)"

# Shell-escaped form of REVIEW_DIR for the copy-paste recovery commands the
# wrapper prints on stderr (pre-round guard exit 64, exit-11 manual-extraction).
# `printf %q` escapes spaces AND metacharacters ($, backticks, quotes) so the
# operator can paste the recovery command verbatim — bare double-quotes (R5-F1)
# protected spaces but a path like `.../rev $(cmd) dir` (REVIEW_DIR derives from
# artifact basenames per SKILL.md Step 1) would still command-substitute. One
# shared value used at BOTH emit sites so they cannot drift (R6-F2 dogfood; the
# drift between the two sites is exactly what produced R5-F1/R6-F1).
REVIEW_DIR_Q="$(printf '%q' "$REVIEW_DIR")"

# Shell-escaped full path to update-state.py for the recovery commands (R8-F1
# dogfood). SCRIPT_DIR resolves to the plugin's scripts dir, which on macOS can
# contain spaces (e.g. ~/.claude/plugins/cache/.../bulldozer/...). REVIEW_DIR_Q
# escaped the --review-dir value but `python3 ${SCRIPT_DIR}/update-state.py` left
# the script path bare — a spaces/metachar plugin path would still split or
# command-substitute on copy-paste. Computed once, used at both recovery sites.
UPDATE_STATE_Q="$(printf '%q' "${SCRIPT_DIR}/update-state.py")"

# PR-1 manual-extraction discipline guard (R1-F1):
# If a prior round left a manual_extraction_pending=true entry in state.json,
# the caller (Claude) MUST resolve it via update-state.py --mode=replace-extraction
# before starting a new round. Otherwise stale UNKNOWN/findings=0 placeholders
# pollute trajectory/pivot decisions, and the discipline invariant is lost.
# See SKILL.md Step 7 "Wrapper exited 11" branch for the recovery protocol.
STATE_FILE="${REVIEW_DIR}/state.json"
if [[ -f "$STATE_FILE" ]]; then
    # Bug #1: prior version printed "?" when entry was missing the round
    # key, then bash `$(( pending_round + 1 ))` syntax-errored INSIDE the
    # _emit_stop argument list — under set -e this error is silently
    # swallowed (argument-expansion errors don't trigger ERR), so the
    # guard never fired and the next round overwrote the corrupt entry.
    # Fix: print a non-numeric sentinel ("CORRUPT_NO_ROUND_KEY") rather
    # than "?" so the bash regex check below catches it, AND drop the
    # `$(( pending_round + 1 ))` arithmetic in the diagnostic so no
    # arithmetic class exists on this path at all.
    pending_round=$(python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fp:
        state = json.load(fp)
except (OSError, json.JSONDecodeError):
    sys.exit(0)  # let downstream cat/update-state surface the corruption
for entry in state.get("history", []):
    pending_value = entry.get("manual_extraction_pending")
    if pending_value is True:
        # Canonical pending — emit round number for routing.
        round_val = entry.get("round")
        if isinstance(round_val, int) and not isinstance(round_val, bool):
            print(round_val)
        else:
            print("CORRUPT_NO_ROUND_KEY")
        sys.exit(0)
    elif pending_value is not False and pending_value is not None:
        # R1-F3 (R2 dogfood): non-canonical truthy/falsy value present
        # (e.g. string "true" from hand-edit, int 1, list). Strict `is True`
        # would skip it → guard never fires → next round overwrites
        # unresolved corrupt state. Emit non-numeric sentinel to route into
        # the existing _emit_stop 70 corrupt-pending diagnostic path.
        print("CORRUPT_NON_BOOL_FLAG")
        sys.exit(0)
' "$STATE_FILE" 2>/dev/null) || pending_round=""
    if [[ -n "$pending_round" ]]; then
        if [[ ! "$pending_round" =~ ^[0-9]+$ ]]; then
            _emit_stop 70 "pending entry in ${STATE_FILE} is corrupt (missing or non-integer 'round' key)." \
                "Cannot determine which round to resolve. Inspect state.json manually:" \
                "  jq '.history' ${REVIEW_DIR_Q}/state.json" \
                "Then either fix the entry or delete state.json to start fresh."
        fi
        _emit_stop 64 "round ${pending_round} has unresolved manual_extraction_pending=true in ${STATE_FILE}." \
            "Resolve before starting a new round:" \
            "  1. Read ${REVIEW_DIR}/verdict-r${pending_round}.txt" \
            "  2. Extract findings from prose (count K, determine VERDICT)" \
            "  3. Run: python3 ${UPDATE_STATE_Q} --review-dir ${REVIEW_DIR_Q} \\" \
            "         --mode=replace-extraction ${pending_round} <K> <GO|NO-GO>" \
            "Then re-invoke this wrapper for the next round (current pending: ${pending_round})."
    fi
fi

VERDICT_FILE="${REVIEW_DIR}/verdict-r${ROUND}.txt"
FULL_LOG="${REVIEW_DIR}/full-r${ROUND}.txt"

# R6-F1: pre-write probe for FULL_LOG (symmetric with the PARSED_FILE
# probe further down). codex stdout redirects to FULL_LOG; if the path
# is unwritable (pre-existing chmod-000 file, parent EACCES, fs full),
# bash redirection fails BEFORE codex runs → codex_exit=1 → diagnostic
# `tail FULL_LOG` ALSO fails under pipefail → wrapper exits raw 1.
# Catching the write-failure here exits 70 cleanly with no codex spend.
_probe_writable "$FULL_LOG" "cannot write full log." \
    "Causes: target exists unwritable, parent dir EACCES, fs full."

# Depth-specific codex configuration. Reasoning effort + the --ephemeral toggle
# come from depth-config.json (read at preflight into $reasoning / $ephemeral);
# $prompt_prefix is likewise already set there (B1, #110).
codex_args=(exec -s read-only -m "$MODEL" -o "$VERDICT_FILE" -C "$PROJECT_ROOT")
codex_args+=(-c "model_reasoning_effort=${reasoning}")
if [[ "$ephemeral" == "true" ]]; then
    codex_args+=(--ephemeral)
fi

# A4 (#110): feed the prompt to codex via stdin (codex 0.134.0: "if `-` is
# used, instructions are read from stdin") instead of a positional arg. A
# positional risked E2BIG/ARG_MAX (Linux 128KB) on large round-N prompts
# (ledger + full previous verdict appended), and $(<file) stripped trailing
# newlines. prompt_prefix (quick-depth "SKIP SKILLS. ") is prepended.
#
# R1-F1 (dogfood): assemble prefix + prompt-file into a regular file and feed
# it via a stdin REDIRECT, NOT a pipe. Under `set -o pipefail` a pipe
# conflates failures: if codex closes stdin early on a large prompt, `cat`
# takes SIGPIPE and a successful review is mislabeled exit 71; a `cat` TOCTOU
# failure is likewise reported as a codex crash. A file redirect removes the
# pipe (no writer to signal), isolates assembly errors from codex's exit, and
# leaves the exact bytes sent as a debugging artifact. stdin EOF after the
# prompt == the old < /dev/null (codex never blocks on an auth prompt).
codex_stdin="${REVIEW_DIR}/prompt-r${ROUND}.codex-stdin.txt"
if ! { printf '%s' "$prompt_prefix"; cat "$PROMPT_FILE"; } > "$codex_stdin" 2>/dev/null; then
    _emit_stop 70 "failed to assemble codex prompt (prefix + --prompt-file)." \
        "Prompt file: ${PROMPT_FILE}" \
        "Assembled stdin path: ${codex_stdin}"
fi

# FOREGROUND ONLY (NEVER run_in_background) — -o is written LAST by codex,
# polling is unreliable. stderr merged into FULL_LOG for crash diagnostics.
codex_exit=0
codex "${codex_args[@]}" - < "$codex_stdin" > "$FULL_LOG" 2>&1 || codex_exit=$?

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
# directly (e.g. from tests via PLUGIN_ROOT). SCRIPT_DIR was hoisted to the
# top of the file (after set -euo pipefail) so the R1-F1 pre-round guard
# could reference update-state.py in its diagnostic — same value here.
# ---------------------------------------------------------------------------
PARSER="$(_resolve_script parse-ledger-patch.py)"
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
_probe_writable "$PARSED_FILE" "cannot write parsed file." \
    "Causes: target is a directory, target unwritable (chmod), parent dir EACCES, fs full."

# Hoisted from post-parser block: parser exit 1 (manual-extraction branch,
# PR-1 issue #110 B5) calls log-round.sh too, so FIXED/FP/LOG_ROUND must
# be resolved BEFORE the parser case statement, not after. Definitions
# unchanged from the post-parser site (now removed below) — same validation,
# same env-var fallback, same path-existence check.
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

LOG_ROUND="$(_resolve_script log-round.sh)"

# R1-F3c (companion to parser path check): pre-validate log-round.sh
# path so missing-script doesn't leak as bash "command not found" (exit
# 127) or an opaque set -e bail.
if [[ ! -f "$LOG_ROUND" ]]; then
    _emit_stop 70 "log-round.sh not found at expected path." \
        "Expected log-round.sh at: ${LOG_ROUND}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi

# B3 (#110): trajectory render + pivot emit are extracted to standalone
# scripts (render-trajectory.py, emit-pivot.py) so they are unit-testable.
# Same CLAUDE_PLUGIN_ROOT-with-SCRIPT_DIR-fallback resolution as PARSER /
# LOG_ROUND, with the same R1-F3c pre-validation (missing script → 70, not
# an opaque set -e bail or a "command not found" 127).
RENDER_TRAJECTORY="$(_resolve_script render-trajectory.py)"
if [[ ! -f "$RENDER_TRAJECTORY" ]]; then
    _emit_stop 70 "render-trajectory.py not found at expected path." \
        "Expected render-trajectory.py at: ${RENDER_TRAJECTORY}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi

EMIT_PIVOT="$(_resolve_script emit-pivot.py)"
if [[ ! -f "$EMIT_PIVOT" ]]; then
    _emit_stop 70 "emit-pivot.py not found at expected path." \
        "Expected emit-pivot.py at: ${EMIT_PIVOT}" \
        "Check CLAUDE_PLUGIN_ROOT (currently: '${CLAUDE_PLUGIN_ROOT:-<unset>}'). Stale cache after plugin update?"
fi

parser_exit=0
python3 "$PARSER" --file "$VERDICT_FILE" > "$PARSED_FILE" || parser_exit=$?

case "$parser_exit" in
    0)
        : # success path — wrapper continues (log-round composition in next commit)
        ;;
    1)
        # PR-1 manual-fallback discipline (issue #110 B5):
        # Reviewer narrated the verdict but skipped LEDGER_PATCH. Instead of
        # raw exit 1 (which re-creates #98/#102 discipline gap by handing
        # control to Claude with no state recorded), log the round to
        # state.json with verdict=UNKNOWN + manual_extraction_pending=true,
        # append to bulldozer.log, then exit 11 so caller knows to:
        #   1. Read $VERDICT_FILE
        #   2. Extract findings from prose (count K, determine VERDICT)
        #   3. Call update-state.py --review-dir $REVIEW_DIR \
        #          --mode=replace-extraction $ROUND $K $VERDICT
        # See SKILL.md Step 7 "manual-extraction branch" for the protocol.
        manual_log_exit=0
        BULLDOZER_REVIEW_DIR="$REVIEW_DIR" BULLDOZER_DEPTH="$DEPTH" \
            bash "$LOG_ROUND" "$ROUND" "$ARTIFACT" "UNKNOWN" \
                "0" "$FIXED" "$FP" "$REVIEWER" "$PROJECT_ROOT" "true" \
                > /dev/null || manual_log_exit=$?
        if (( manual_log_exit != 0 )); then
            _emit_stop 70 "log-round.sh failed during manual-extraction logging (exit ${manual_log_exit})." \
                "Helper script: ${LOG_ROUND}" \
                "Cannot proceed with exit 11 because state.json was not written."
        fi
        {
            echo "MANUAL_EXTRACTION_REQUIRED: round=${ROUND} artifact=${ARTIFACT}"
            echo "      Verdict file: ${VERDICT_FILE}"
            echo "      Extract findings from prose, then call:"
            echo "      python3 ${UPDATE_STATE_Q} --review-dir ${REVIEW_DIR_Q} \\"
            echo "          --mode=replace-extraction ${ROUND} <K> <GO|NO-GO>"
        } >&2
        exit 11
        ;;
    *)
        # B2 (#110): all diagnostic exit codes (2/3/4/5 + anything unexpected)
        # route through one helper so the code→message mapping has a single
        # home. exit 0 (success) and exit 1 (manual extraction) stay above
        # because they drive control flow, not just a diagnostic.
        _emit_parser_exit_diagnostic "$parser_exit"
        ;;
esac

# ---------------------------------------------------------------------------
# Step 5-7: derive findings count + verdict, call log-round.sh, emit state.
# Forgetting any of these is the discipline failure #102 exists to eliminate.
# ---------------------------------------------------------------------------
# Read findings count AND verdict in one python3 call. Since B8 (#110) the
# parser emits a canonical TOP-LEVEL `verdict` ("go"/"no_go") on every exit-0
# parse, so the wrapper reads it directly — single source of truth (the
# GO/NO-GO derivation lives in the parser now, not duplicated here). The
# canonical field is read at top level, NOT from meta: meta preserves the
# reviewer's raw verdict token (Issue #100 case #7), which may be uppercase
# or a YAML bool-ish token ("GO", "no") and must not be the decision input.
# A missing verdict key maps to NO-GO (fail-safe; never a false GO),
# preserving the BUG-2 invariant that empty findings never flip an
# explicit NO-GO to GO.
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
verdict = "GO" if data.get("verdict") == "go" else "NO-GO"
print(f"{len(findings)}|{verdict}")
' "$PARSED_FILE") || parser_out_exit=$?

if (( parser_out_exit != 0 )); then
    _emit_stop 70 "failed to read parsed findings/verdict (python3 exit ${parser_out_exit})." \
        "Parsed file: ${PARSED_FILE}" \
        "Likely cause: corrupted JSON output from parser. Inspect the file and the parser stderr."
fi

# A1 (#110): defensive guard. The inline python above always prints
# "COUNT|VERDICT" (any read error exits non-zero → the branch above), so
# this is unreachable via normal PARSED_FILE content. But if a future edit
# makes that print conditional, an empty parser_out would split into empty
# findings_count/VERDICT and reach log-round with a misleading
# "log-round.sh failed". Convert to a clear EX_SOFTWARE failure instead.
if [[ -z "$parser_out" ]]; then
    _emit_stop 70 "parser produced empty output (expected 'COUNT|VERDICT')." \
        "Parsed file: ${PARSED_FILE}"
fi

findings_count="${parser_out%|*}"
VERDICT="${parser_out#*|}"

# FIXED, FP, LOG_ROUND already validated/resolved BEFORE parser invocation
# (hoisted so the parser-exit-1 manual-extraction branch could use them).
# Do NOT re-define here.

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

# max_rounds was read from depth-config.json at preflight (B1, #110); the
# AskUser-pivot guard below uses it (previously a duplicated depth case here).

# Step 8 (U7): trajectory display — only round >= 2, since round 1 has
# nothing to plot. Goes to stderr (informational) so stdout stays JSON.
#
# R2-F3: capture failure so set -e doesn't bubble python3 exit raw —
# corrupted state.json post-log-round (race delete, fs corruption,
# update-state.py bug) would otherwise look like parser-no-LEDGER
# (exit 1) to the caller.
if (( ROUND >= 2 )); then
    trajectory_exit=0
    python3 "$RENDER_TRAJECTORY" "$ROUND" "$max_rounds" "${REVIEW_DIR}/state.json" >&2 || trajectory_exit=$?
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

# Step 9 (U5a): AskUser pivot signal. Two triggers:
#  - flat: ROUND >= max_rounds without GO (the depth's hard cap).
#  - calibrated early-pivot (B6, #128): exhaustive only, ROUND >= 5, not GO, and
#    the mean of the last 3 rounds' findings >= 3.0 (trajectory not converging).
#    Surfaces the dialog earlier than round 10 to save doomed rounds. Scoped to
#    exhaustive because widening it false-pivoted converging standard reviews on
#    the session corpus (docs/superpowers/analysis/2026-06-01-b6-pivot-calibration.md
#    — 0 FP on exhaustive, FP=4 any-depth). The pivot is an AskUser dialog, not an
#    abort; the flat trigger stays the round-10 backstop, so this only moves the
#    dialog earlier on a clearly-doomed exhaustive run.
# Caller (Claude) reads exit 10 + the pivot file and wraps in AskUserQuestion
# (continue / restructure / accept-with-TODO).
CALIBRATED_PIVOT_MIN_ROUND=5
CALIBRATED_PIVOT_AVG_THRESHOLD=3.0
pivot_trigger=""
if [[ -n "${max_rounds:-}" ]] && (( ROUND >= max_rounds )) && [[ "$VERDICT" != "GO" ]]; then
    pivot_trigger="max_rounds_reached"
elif [[ "$DEPTH" == "exhaustive" ]] && (( ROUND >= CALIBRATED_PIVOT_MIN_ROUND )) && [[ "$VERDICT" != "GO" ]]; then
    # Reuse the trajectory metric (mean of last 3 findings) the render step
    # computes — render-trajectory.py --avg-meets is now the single source
    # (#133 F1), so the displayed avg and this gate can never diverge. It
    # decides in python (bash never float-compares) and prints "0" on an
    # unreadable state.json (exit 0); 2>/dev/null + || avg_meets=0 keep any
    # unexpected failure non-fatal (no early pivot, flat round-10 backstop stays).
    avg_meets=$(python3 "$RENDER_TRAJECTORY" --avg-meets \
        "${REVIEW_DIR}/state.json" "$CALIBRATED_PIVOT_AVG_THRESHOLD" 2>/dev/null) || avg_meets=0
    if (( avg_meets == 1 )); then
        pivot_trigger="calibrated_nonconvergence"
    fi
fi

if [[ -n "$pivot_trigger" ]]; then
    PIVOT_FILE="${REVIEW_DIR}/pivot-r${ROUND}.json"
    # R2-F3: pivot write may fail (EACCES on review dir, fs full, python3
    # crash). Capture so set -e doesn't bubble python3 exit raw — exit 10
    # is meaningful ONLY when the pivot file actually exists for the
    # caller to read.
    pivot_exit=0
    python3 "$EMIT_PIVOT" "$ROUND" "$max_rounds" "$findings_count" "$DEPTH" "$ARTIFACT" "$PIVOT_FILE" "$pivot_trigger" || pivot_exit=$?
    if (( pivot_exit != 0 )) || [[ ! -f "$PIVOT_FILE" ]]; then
        _emit_stop 70 "pivot file write failed (python3 exit ${pivot_exit})." \
            "Expected pivot file: ${PIVOT_FILE}" \
            "Exit 10 suppressed because caller cannot read a missing pivot file."
    fi
    if [[ "$pivot_trigger" == "calibrated_nonconvergence" ]]; then
        echo "PIVOT: exhaustive review not converging by round ${ROUND} (avg last 3 findings >= ${CALIBRATED_PIVOT_AVG_THRESHOLD}). See ${PIVOT_FILE} for AskUserQuestion options." >&2
    else
        echo "PIVOT: max rounds reached without GO. See ${PIVOT_FILE} for AskUserQuestion options." >&2
    fi
    exit 10
fi
