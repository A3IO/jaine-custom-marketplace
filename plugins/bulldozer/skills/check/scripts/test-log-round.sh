#!/bin/bash
# Tests for log-round.sh (Bug 2 fix: auto-calls update-state.py)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0
ERRORS=""

assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        ((PASS++))
    else
        ((FAIL++))
        ERRORS+="  FAIL: ${desc}\n    expected: '${expected}'\n    actual:   '${actual}'\n"
    fi
}

setup() {
    FIXTURE_DIR=$(mktemp -d)
    cd "$FIXTURE_DIR"
    git init --quiet -b main && git config user.email "t@t" && git config user.name "t"
    echo "test" > README.md && git add -A && git commit -m "init" --quiet
    LOG_FILE="$FIXTURE_DIR/test.log"
}

teardown() {
    cd /
    rm -rf "$FIXTURE_DIR"
}

# Test 1: log-round.sh creates state.json automatically
test_creates_state_json() {
    setup
    BULLDOZER_LOG="$LOG_FILE" \
        bash "$SCRIPT_DIR/log-round.sh" 1 "spec.md" "NO-GO" 7 3 1 "codex/gpt-5.5"
    [[ -f ".bulldozer/state.json" ]]
    assert_eq "state.json created" "0" "$?"
    teardown
}

# Test 2: state.json has correct round data after round 1
test_state_has_round_1_data() {
    setup
    BULLDOZER_LOG="$LOG_FILE" \
        bash "$SCRIPT_DIR/log-round.sh" 1 "spec.md" "NO-GO" 7 3 1 "codex/gpt-5.5"
    local round
    round=$(python3 -c "import json; print(json.load(open('.bulldozer/state.json'))['round'])")
    assert_eq "round=1" "1" "$round"
    local findings
    findings=$(python3 -c "import json; print(json.load(open('.bulldozer/state.json'))['findings_total'])")
    assert_eq "findings_total=7" "7" "$findings"
    teardown
}

# Test 3: state.json accumulates across rounds (Bug 2 regression test)
test_state_accumulates_rounds() {
    setup
    BULLDOZER_LOG="$LOG_FILE" \
        bash "$SCRIPT_DIR/log-round.sh" 1 "spec.md" "NO-GO" 7 3 1 "codex/gpt-5.5"
    BULLDOZER_LOG="$LOG_FILE" \
        bash "$SCRIPT_DIR/log-round.sh" 2 "spec.md" "NO-GO" 3 2 0 "codex/gpt-5.5"
    BULLDOZER_LOG="$LOG_FILE" \
        bash "$SCRIPT_DIR/log-round.sh" 3 "spec.md" "GO" 1 1 0 "codex/gpt-5.5"
    local history_len
    history_len=$(python3 -c "import json; print(len(json.load(open('.bulldozer/state.json'))['history']))")
    assert_eq "history has 3 rounds" "3" "$history_len"
    local total_findings
    total_findings=$(python3 -c "import json; print(json.load(open('.bulldozer/state.json'))['findings_total'])")
    assert_eq "findings_total=11 (7+3+1)" "11" "$total_findings"
    teardown
}

# Test 4: log file gets entries too
test_log_file_written() {
    setup
    BULLDOZER_LOG="$LOG_FILE" \
        bash "$SCRIPT_DIR/log-round.sh" 1 "spec.md" "NO-GO" 5 2 0 "codex/gpt-5.5"
    local lines
    lines=$(wc -l < "$LOG_FILE" | tr -d ' ')
    assert_eq "log has 1 line" "1" "$lines"
    teardown
}

echo "=== log-round.sh tests ==="
echo ""

test_creates_state_json
test_state_has_round_1_data
test_state_accumulates_rounds
test_log_file_written

echo ""
if [[ $FAIL -gt 0 ]]; then
    echo -e "$ERRORS"
fi
echo "Results: $PASS passed, $FAIL failed ($(( PASS + FAIL )) total)"
exit $FAIL
