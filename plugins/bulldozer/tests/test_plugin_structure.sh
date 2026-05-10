#!/usr/bin/env bash
# Bulldozer plugin structure verification test
# Run: bash tests/test_plugin_structure.sh
# Exit 0 = all checks pass, non-zero = failures found

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0
PASS=0

check() {
  local desc="$1" path="$2"
  if [[ -e "$path" ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc — missing: $path"
    FAIL=$((FAIL + 1))
  fi
}

check_content() {
  local desc="$1" file="$2" pattern="$3"
  if [[ -f "$file" ]] && grep -q "$pattern" "$file"; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc — pattern '$pattern' not found in $file"
    FAIL=$((FAIL + 1))
  fi
}

check_no_file() {
  local desc="$1" path="$2"
  if [[ ! -e "$path" ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc — should not exist: $path"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Bulldozer Plugin Structure Test ==="
echo "Plugin root: $PLUGIN_ROOT"
echo

echo "--- Core files ---"
check "plugin.json exists" "$PLUGIN_ROOT/.claude-plugin/plugin.json"
check "README.md exists" "$PLUGIN_ROOT/README.md"
check "hooks.json exists" "$PLUGIN_ROOT/hooks/hooks.json"

echo
echo "--- Check skill (adversarial review) ---"
check "skills/check/SKILL.md" "$PLUGIN_ROOT/skills/check/SKILL.md"
check "skills/check/scripts/log-round.sh" "$PLUGIN_ROOT/skills/check/scripts/log-round.sh"
check "skills/check/scripts/update-state.py" "$PLUGIN_ROOT/skills/check/scripts/update-state.py"
check "skills/check/scripts/test-log-round.sh" "$PLUGIN_ROOT/skills/check/scripts/test-log-round.sh"
check "commands/check.md" "$PLUGIN_ROOT/commands/check.md"

echo
echo "--- Look skill (browser verification) ---"
check "skills/look/SKILL.md" "$PLUGIN_ROOT/skills/look/SKILL.md"
check "skills/look/scripts/cdp.py" "$PLUGIN_ROOT/skills/look/scripts/cdp.py"
check "skills/look/scripts/launch.sh" "$PLUGIN_ROOT/skills/look/scripts/launch.sh"
check "commands/look.md" "$PLUGIN_ROOT/commands/look.md"

echo
echo "--- Hooks completeness ---"
check_content "hooks.json has check matcher" "$PLUGIN_ROOT/hooks/hooks.json" "bulldozer:check"
check_content "hooks.json has look matcher" "$PLUGIN_ROOT/hooks/hooks.json" "bulldozer:look"

echo
echo "--- Command frontmatter ---"
check_content "check.md has argument-hint" "$PLUGIN_ROOT/commands/check.md" "argument-hint"
check_content "look.md has argument-hint" "$PLUGIN_ROOT/commands/look.md" "argument-hint"
check_content "look.md has description" "$PLUGIN_ROOT/commands/look.md" "^description:"

echo
echo "--- Plugin description ---"
check_content "plugin.json mentions look/browser" "$PLUGIN_ROOT/.claude-plugin/plugin.json" "browser\|visual\|CDP"

echo
echo "--- Stale files ---"
check_no_file "BUGS.md removed (all bugs fixed)" "$PLUGIN_ROOT/BUGS.md"

echo
echo "--- README completeness ---"
check_content "README mentions /bulldozer:look" "$PLUGIN_ROOT/README.md" "bulldozer:look"
check_content "README mentions CDP or browser" "$PLUGIN_ROOT/README.md" "CDP\|browser\|screenshot"
check_content "README lists status command" "$PLUGIN_ROOT/README.md" "status"
check_content "README lists tabs command" "$PLUGIN_ROOT/README.md" "tabs"

echo
echo "--- launch.sh hardening (review findings) ---"
check_content "launch.sh uses env bash shebang" "$PLUGIN_ROOT/skills/look/scripts/launch.sh" "#!/usr/bin/env bash"
check_content "launch.sh logs Chrome output" "$PLUGIN_ROOT/skills/look/scripts/launch.sh" "\.log\|chrome\.log"

echo
echo "--- Command consistency ---"
check_content "look.md has allowed-tools" "$PLUGIN_ROOT/commands/look.md" "allowed-tools"

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]]
