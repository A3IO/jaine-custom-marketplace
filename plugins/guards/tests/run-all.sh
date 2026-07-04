#!/bin/bash
# Run every guards test (1 lexer unit test + 4 python detector tests + 2 bash engine/dispatch tests).
# Exit 0 iff all pass.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
fail=0
run() { echo "=== $* ==="; if "$@" >/dev/null 2>&1; then echo "✓ pass"; else echo "✗ FAIL ($*)"; fail=1; fi; }

run python3 "$DIR/test_git_lexer.py"
run python3 "$DIR/test_guard_git_destructive_detect.py"
run python3 "$DIR/test_guard_git_branch_delete_detect.py"
run python3 "$DIR/test_guard_process_kill_detect.py"
run python3 "$DIR/test_guard_dropped_toolcall_detect.py"
run bash "$DIR/test_guard_confirm_dialog.sh"
run bash "$DIR/test_guard_dispatch.sh"

echo ""
[ "$fail" -eq 0 ] && echo "ALL GREEN" || echo "SOME FAILED"
exit "$fail"
