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
echo "--- Architecture: skills-only (no commands/) ---"
check_no_file "commands/ directory removed" "$PLUGIN_ROOT/commands"
check "skills/check/SKILL.md" "$PLUGIN_ROOT/skills/check/SKILL.md"
check "skills/look/SKILL.md" "$PLUGIN_ROOT/skills/look/SKILL.md"

echo
echo "--- Check skill: frontmatter ---"
check_content "check SKILL.md has allowed-tools" "$PLUGIN_ROOT/skills/check/SKILL.md" "allowed-tools"
check_content "check SKILL.md has argument-hint" "$PLUGIN_ROOT/skills/check/SKILL.md" "argument-hint"

echo
echo "--- Look skill: frontmatter ---"
check_content "look SKILL.md has allowed-tools" "$PLUGIN_ROOT/skills/look/SKILL.md" "allowed-tools"
check_content "look SKILL.md has argument-hint" "$PLUGIN_ROOT/skills/look/SKILL.md" "argument-hint"

echo
echo "--- Check skill: model selection (merged from command) ---"
check_content "check has model selection step" "$PLUGIN_ROOT/skills/check/SKILL.md" "codex debug models"
check_content "check has AskUserQuestion" "$PLUGIN_ROOT/skills/check/SKILL.md" "AskUserQuestion"
check_content "check has config.md preference" "$PLUGIN_ROOT/skills/check/SKILL.md" "config.md"

echo
echo "--- Check skill: no stale cross-references ---"
if grep -q 'commands/check\.md' "$PLUGIN_ROOT/skills/check/SKILL.md"; then
  echo "  FAIL: check SKILL.md still references commands/check.md (dead file)"
  FAIL=$((FAIL + 1))
else
  echo "  PASS: check SKILL.md has no references to commands/check.md"
  PASS=$((PASS + 1))
fi

echo
echo "--- Look skill: quick invocation flow (merged from command) ---"
check_content "look has launch.sh reference" "$PLUGIN_ROOT/skills/look/SKILL.md" "launch.sh"
check_content "look has screenshot step" "$PLUGIN_ROOT/skills/look/SKILL.md" "screenshot"
check_content "look has OFFLINE branch" "$PLUGIN_ROOT/skills/look/SKILL.md" "OFFLINE"
check_content "look has navigate-when-online step" "$PLUGIN_ROOT/skills/look/SKILL.md" "navigate.*ARGUMENTS\|already ONLINE\|already running"
check_content "look has report-to-user step" "$PLUGIN_ROOT/skills/look/SKILL.md" "Report.*user\|report.*user"

echo
echo "--- Check skill: Russian depth explanations (merged from command) ---"
check_content "check has Уровни глубины" "$PLUGIN_ROOT/skills/check/SKILL.md" "Уровни глубины"

echo
echo "--- Check skill: description quality ---"
if head -5 "$PLUGIN_ROOT/skills/check/SKILL.md" | grep -q 'Triggers on\|triggers on\|Trigger'; then
  echo "  PASS: check description has trigger phrases"
  PASS=$((PASS + 1))
else
  echo "  FAIL: check description must contain trigger phrases (passive 'Use when' alone has low activation)"
  FAIL=$((FAIL + 1))
fi

echo
echo "--- Feedback accessibility ---"
check_content "check SKILL.md has Feedback section" "$PLUGIN_ROOT/skills/check/SKILL.md" "## Feedback"
check_content "check SKILL.md has gh issue create" "$PLUGIN_ROOT/skills/check/SKILL.md" "gh issue create"
check_content "look SKILL.md has Feedback section" "$PLUGIN_ROOT/skills/look/SKILL.md" "## Feedback"
check_content "look SKILL.md has gh issue create" "$PLUGIN_ROOT/skills/look/SKILL.md" "gh issue create"

echo
echo "--- Check skill scripts ---"
check "skills/check/scripts/log-round.sh" "$PLUGIN_ROOT/skills/check/scripts/log-round.sh"
check "skills/check/scripts/update-state.py" "$PLUGIN_ROOT/skills/check/scripts/update-state.py"
check "skills/check/scripts/test-log-round.sh" "$PLUGIN_ROOT/skills/check/scripts/test-log-round.sh"

echo
echo "--- Look skill scripts ---"
check "skills/look/scripts/cdp.py" "$PLUGIN_ROOT/skills/look/scripts/cdp.py"
check "skills/look/scripts/launch.sh" "$PLUGIN_ROOT/skills/look/scripts/launch.sh"

echo
echo "--- Hooks completeness ---"
check_content "hooks.json has check matcher" "$PLUGIN_ROOT/hooks/hooks.json" "bulldozer:check"
check_content "hooks.json has look matcher" "$PLUGIN_ROOT/hooks/hooks.json" "bulldozer:look"

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
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]]
