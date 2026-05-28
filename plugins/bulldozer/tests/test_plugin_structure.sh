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
check "skills/consult/SKILL.md" "$PLUGIN_ROOT/skills/consult/SKILL.md"

echo
echo "--- Check skill: frontmatter ---"
check_content "check SKILL.md has allowed-tools" "$PLUGIN_ROOT/skills/check/SKILL.md" "allowed-tools"
check_content "check SKILL.md has argument-hint" "$PLUGIN_ROOT/skills/check/SKILL.md" "argument-hint"

echo
echo "--- Look skill: frontmatter ---"
check_content "look SKILL.md has allowed-tools" "$PLUGIN_ROOT/skills/look/SKILL.md" "allowed-tools"
check_content "look SKILL.md has argument-hint" "$PLUGIN_ROOT/skills/look/SKILL.md" "argument-hint"

echo
echo "--- Consult skill: frontmatter (issue #96) ---"
check_content "consult SKILL.md has allowed-tools" "$PLUGIN_ROOT/skills/consult/SKILL.md" "allowed-tools"
check_content "consult SKILL.md has argument-hint" "$PLUGIN_ROOT/skills/consult/SKILL.md" "argument-hint"
check_content "consult description has Triggers" "$PLUGIN_ROOT/skills/consult/SKILL.md" "Triggers on\|triggers on"
check_content "consult description has anti-trigger for check" "$PLUGIN_ROOT/skills/consult/SKILL.md" "Do NOT use.*file\|Do NOT use.*artifact\|bulldozer:check instead"

echo
echo "--- Consult skill: v3 isolation flags (locked design) ---"
check_content "consult uses --skip-git-repo-check" "$PLUGIN_ROOT/skills/consult/SKILL.md" "skip-git-repo-check"
check_content "consult uses --ignore-user-config" "$PLUGIN_ROOT/skills/consult/SKILL.md" "ignore-user-config"
check_content "consult uses --ignore-rules" "$PLUGIN_ROOT/skills/consult/SKILL.md" "ignore-rules"
check_content "consult uses --ephemeral" "$PLUGIN_ROOT/skills/consult/SKILL.md" "ephemeral"
check_content "consult uses read-only sandbox" "$PLUGIN_ROOT/skills/consult/SKILL.md" "read-only"
check_content "consult runs from empty tmpdir" "$PLUGIN_ROOT/skills/consult/SKILL.md" "tmpdir\|/tmp/bulldozer-consult"
check_content "consult uses timeout wrapper" "$PLUGIN_ROOT/skills/consult/SKILL.md" "timeout"
check_content "consult has pre-flight artifact detection" "$PLUGIN_ROOT/skills/consult/SKILL.md" "pre-flight\|Pre-flight\|artifact reference\|artifact detect"
check_content "consult has fail-closed verdict parsing" "$PLUGIN_ROOT/skills/consult/SKILL.md" "fail.closed\|fail-closed"
check_content "consult has escalation rule" "$PLUGIN_ROOT/skills/consult/SKILL.md" "escalat"

echo
echo "--- Consult: NOT supported (architectural guarantees from issue #96 review) ---"
# v3 design REMOVED persistent mode — must not creep back in.
# Detect ACTUAL usage in command lines (not anti-feature documentation in tables/prose).
# Real usage looks like a shell command line: `codex exec resume "$SESSION"` or `--persistent` as a CLI flag.
if grep -nE '^\s*(timeout [0-9]+s )?codex exec resume\b' "$PLUGIN_ROOT/skills/consult/SKILL.md" 2>/dev/null; then
  echo "  FAIL: consult must NOT invoke 'codex exec resume' (REMOVE_PERSISTENT decision, dogfood-2/A2/A3)"
  FAIL=$((FAIL + 1))
elif grep -nE '^\s*[^|]*--persistent\b' "$PLUGIN_ROOT/skills/consult/SKILL.md" 2>/dev/null | grep -v '^\s*|' >/dev/null; then
  echo "  FAIL: consult must NOT accept '--persistent' flag (REMOVE_PERSISTENT decision)"
  FAIL=$((FAIL + 1))
else
  echo "  PASS: consult is stateless-only (no persistent mode usage)"
  PASS=$((PASS + 1))
fi
# Positive evidence: every codex exec invocation in SKILL.md must use --ephemeral
EPHEMERAL_USES=$(grep -cE '^\s*(timeout [0-9]+s )?codex exec\b.*\\$' "$PLUGIN_ROOT/skills/consult/SKILL.md" 2>/dev/null || echo 0)
if [[ "$EPHEMERAL_USES" -gt 0 ]]; then
  # for each multiline codex exec block, ensure --ephemeral is on a continuation line within ~15 lines
  if grep -A15 -E '^\s*(timeout [0-9]+s )?codex exec\b' "$PLUGIN_ROOT/skills/consult/SKILL.md" | grep -q -- '--ephemeral'; then
    echo "  PASS: codex exec invocations include --ephemeral"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: codex exec invocation missing --ephemeral flag"
    FAIL=$((FAIL + 1))
  fi
else
  echo "  FAIL: no codex exec invocation block found in consult SKILL.md (positive evidence missing)"
  FAIL=$((FAIL + 1))
fi
# No session storage with prompt content (dogfood-1 finding #6)
check_no_file "no session log with raw prompts" "$PLUGIN_ROOT/.bulldozer/consult-sessions.log"

echo
echo "--- Consult: Feedback section (parity with check/look) ---"
check_content "consult SKILL.md has Feedback section" "$PLUGIN_ROOT/skills/consult/SKILL.md" "## Feedback"
check_content "consult SKILL.md has gh issue create" "$PLUGIN_ROOT/skills/consult/SKILL.md" "gh issue create"

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
check_content "hooks.json has consult matcher" "$PLUGIN_ROOT/hooks/hooks.json" "bulldozer:consult"

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
check_content "README mentions /bulldozer:consult" "$PLUGIN_ROOT/README.md" "bulldozer:consult"

echo
echo "--- launch.sh hardening (review findings) ---"
check_content "launch.sh uses env bash shebang" "$PLUGIN_ROOT/skills/look/scripts/launch.sh" "#!/usr/bin/env bash"
check_content "launch.sh logs Chrome output" "$PLUGIN_ROOT/skills/look/scripts/launch.sh" "\.log\|chrome\.log"

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]]
