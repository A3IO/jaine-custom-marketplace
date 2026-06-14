#!/bin/bash
# Test the confirm-dialog ENGINE's non-interactive paths WITHOUT popping a real dialog:
# stub `osascript` (its button output + exit code) and `afplay` (silence) on PATH, then
# assert the engine's exit code for each branch. This is a net safety gain — the macOS gate,
# previously untested, is now covered (and so, by reuse, is every guard built on it).
#
# Run: ./test_guard_confirm_dialog.sh   (exit 0 = all pass)
set -u
ENGINE="$(cd "$(dirname "$0")/../hooks" && pwd)/guard-confirm-dialog.sh"
pass=0
fail=0

STUB=$(mktemp -d)
printf '#!/bin/bash\nexit 0\n' > "$STUB/afplay"   # silent afplay so tests never beep
chmod +x "$STUB/afplay"

run() {  # $1=desc  $2=expected_exit  $3=osascript_stdout  $4=osascript_exit  $5=expected_log_decision
    local desc="$1" exp="$2" out="$3" rc="$4" logdec="$5"
    printf '#!/bin/bash\nprintf "%%s" %q\nexit %s\n' "$out" "$rc" > "$STUB/osascript"
    chmod +x "$STUB/osascript"
    local logf="$STUB/guards-test.log"; : > "$logf"
    GUARDS_LOG="$logf" PATH="$STUB:$PATH" "$ENGINE" "🛡 Test" "some-command # WHY: because reasons" >/dev/null 2>&1
    local got=$?
    if [ "$got" -eq "$exp" ]; then
        echo "PASS: $desc"; pass=$((pass + 1))
    else
        echo "FAIL: $desc -> exit $got, expected $exp"; fail=$((fail + 1))
    fi
    # audit-log assertion: one line with the decision + the command (NOT the # WHY tail)
    if grep -q "decision=${logdec}" "$logf" && grep -q "command=some-command" "$logf"; then
        echo "PASS: $desc — logged decision=${logdec}"; pass=$((pass + 1))
    else
        echo "FAIL: $desc — log missing 'decision=${logdec}' (got: $(cat "$logf"))"; fail=$((fail + 1))
    fi
}

run "Allow -> 0 (run)"                 0 "Allow"   0 "ALLOW"
run "Deny -> 2 (block)"                2 "Deny"    0 "DENY"
run "Объясни -> 2 (block, ask WHY)"    2 "Объясни" 0 "EXPLAIN"
run "GAVEUP -> 2 (timeout fail-safe)"  2 "GAVEUP"  0 "BLOCKED-timeout"
run "osascript fail -> 2 (no GUI/Esc)" 2 ""        1 "BLOCKED-nogui"

# empty command -> allow (nothing to confirm), regardless of the dialog; must NOT log
printf '#!/bin/bash\nexit 1\n' > "$STUB/osascript"; chmod +x "$STUB/osascript"
EMPTYLOG="$STUB/empty.log"; : > "$EMPTYLOG"
GUARDS_LOG="$EMPTYLOG" PATH="$STUB:$PATH" "$ENGINE" "🛡 Test" "" >/dev/null 2>&1
ec=$?
if [ "$ec" -eq 0 ]; then echo "PASS: empty cmd -> 0"; pass=$((pass + 1)); else echo "FAIL: empty cmd -> non-0"; fail=$((fail + 1)); fi
if [ ! -s "$EMPTYLOG" ]; then echo "PASS: empty cmd -> no log line"; pass=$((pass + 1)); else echo "FAIL: empty cmd logged: $(cat "$EMPTYLOG")"; fail=$((fail + 1)); fi

# audit-log must not be poisoned by control chars: a command with an embedded CR must log as
# ONE clean line (no \r, no extra lines) so a terminal-reading auditor can't be deceived.
printf '#!/bin/bash\nprintf Allow\nexit 0\n' > "$STUB/osascript"; chmod +x "$STUB/osascript"
CRLOG="$STUB/cr.log"; : > "$CRLOG"
GUARDS_LOG="$CRLOG" PATH="$STUB:$PATH" "$ENGINE" "🛡 Test" "$(printf 'kill 1234\rFAKE | decision=ALLOW')" >/dev/null 2>&1
if [ "$(wc -l < "$CRLOG")" -eq 1 ] && ! LC_ALL=C grep -q $'\r' "$CRLOG"; then
    echo "PASS: CR in command -> single clean log line"; pass=$((pass + 1))
else
    echo "FAIL: CR poisoned log: $(LC_ALL=C cat -v "$CRLOG")"; fail=$((fail + 1))
fi

rm -rf "$STUB"
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
