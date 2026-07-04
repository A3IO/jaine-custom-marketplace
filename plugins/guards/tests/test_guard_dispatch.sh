#!/bin/bash
# Test the generic guard-dispatch.sh end-to-end (dispatch -> detector -> engine), driving
# all three detectors, with osascript/afplay stubbed so no real dialog pops. Confirms: a
# detector HIT reaches the dialog (and the chosen button maps to the right exit code), a
# MISS allows silently, and malformed/non-Bash input fails OPEN.
#
# Run: ./test_guard_dispatch.sh   (exit 0 = all pass)
set -u
HOOKS="$(cd "$(dirname "$0")/../hooks" && pwd)"
DISPATCH="$HOOKS/guard-dispatch.sh"
pass=0
fail=0

STUB=$(mktemp -d)
printf '#!/bin/bash\nexit 0\n' > "$STUB/afplay"; chmod +x "$STUB/afplay"
set_button() {  # stub osascript to "click" $1
    printf '#!/bin/bash\nprintf "%%s" %q\nexit 0\n' "$1" > "$STUB/osascript"; chmod +x "$STUB/osascript"
}

run() {  # $1=desc $2=expected_exit $3=detector $4=json(stdin)
    local desc="$1" exp="$2" det="$3" json="$4"
    local got
    got=$(printf '%s' "$json" | PATH="$STUB:$PATH" "$DISPATCH" "$det" "🛡 Test" >/dev/null 2>&1; echo $?)
    if [ "$got" -eq "$exp" ]; then echo "PASS: $desc"; pass=$((pass + 1));
    else echo "FAIL: $desc -> exit $got, expected $exp"; fail=$((fail + 1)); fi
}

KILLDET="guard-process-kill-detect.py"
GITDET="guard-git-destructive-detect.py"
BRANCHDET="guard-git-branch-delete-detect.py"

# kill detector through the dispatcher
set_button "Deny"
run "kill 1234 + Deny -> 2"          2 "$KILLDET" '{"tool_input":{"command":"kill 1234"}}'
set_button "Allow"
run "kill 1234 + Allow -> 0"         0 "$KILLDET" '{"tool_input":{"command":"kill 1234"}}'
run "safe cmd (ls) -> 0, no dialog"  0 "$KILLDET" '{"tool_input":{"command":"ls -la"}}'
run "kill \$! (own) -> 0"            0 "$KILLDET" '{"tool_input":{"command":"kill $!"}}'

# git detector through the SAME dispatcher (proves it is generic)
set_button "Deny"
run "git reset --hard + Deny -> 2"   2 "$GITDET" '{"tool_input":{"command":"git reset --hard"}}'
run "git status -> 0, no dialog"     0 "$GITDET" '{"tool_input":{"command":"git status"}}'

# branch-delete detector through the SAME dispatcher. Use the $VAR fail-closed path — it
# reaches exit 2 with NO git subprocess, so the HIT is deterministic regardless of repo state.
set_button "Deny"
run "branch -D \$VAR (fail-closed) + Deny -> 2" 2 "$BRANCHDET" '{"tool_input":{"command":"git branch -D $X"}}'
run "branch-delete: git status -> 0, no dialog"  0 "$BRANCHDET" '{"tool_input":{"command":"git status"}}'

# fail-open paths
run "empty stdin -> 0"               0 "$KILLDET" ''
run "malformed JSON -> 0"            0 "$KILLDET" 'not json{{'
run "no command key -> 0"            0 "$KILLDET" '{"tool_input":{}}'
# MISSING detector must fail OPEN — python3 on a nonexistent file exits 2 (ENOENT), which must
# NOT be mistaken for the detector's "dangerous" exit 2 (that would dialog on every command).
run "missing detector -> 0 (fail-open, not dialog)" 0 "nonexistent-detector.py" '{"tool_input":{"command":"kill 1234"}}'

# WHY-stripping: the reason must not trip detection (a # WHY: mentioning a PID)
set_button "Allow"
run "WHY reason mentions a PID -> still allow (ls)" 0 "$KILLDET" '{"tool_input":{"command":"ls # WHY: not killing 1234"}}'

rm -rf "$STUB"
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
