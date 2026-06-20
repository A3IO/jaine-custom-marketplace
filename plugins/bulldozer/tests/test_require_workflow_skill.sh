#!/bin/bash
# Tests for hooks/require-workflow-skill.py (bulldozer plugin PreToolUse(Workflow) hook).
# Run: bash plugins/bulldozer/tests/test_require_workflow_skill.sh
# Classifies hook stdout: DENY (permissionDecision deny) | ALLOW (systemMessage) | NONE (no output).
# DENY is OPT-IN (BULLDOZER_ENFORCE_WORKFLOW_ROUTING); this suite enables it so the deny paths run.

HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/require-workflow-skill.py"
pass=0
fail=0

# Isolate the decision log so test runs never pollute the production log.
export WORKFLOW_HOOK_LOG="$(mktemp -t workflow-hook-test.XXXXXX)"
export BULLDOZER_ENFORCE_WORKFLOW_ROUTING=1   # exercise the opt-in DENY paths (advisory-only by default)
trap 'rm -f "$WORKFLOW_HOOK_LOG" "$BADJS" "$GOODJS"' EXIT

mkinput() { python3 -c "import sys,json; print(json.dumps({'tool_name':sys.argv[1],'tool_input':json.loads(sys.argv[2])}))" "$1" "$2"; }
ti_script() { python3 -c "import sys,json; print(json.dumps({'script':sys.argv[1]}))" "$1"; }

classify() {
  if grep -q 'permissionDecision.*deny' <<<"$1"; then echo DENY
  elif grep -q 'systemMessage' <<<"$1"; then echo ALLOW
  else echo NONE; fi
}

# $1 desc  $2 stdin-json  $3 want(DENY|ALLOW|NONE)  $4 env-assignment(optional, e.g. CLAUDE_CODE_SUBAGENT_MODEL=haiku)
check() {
  local desc="$1" input="$2" want="$3" envset="$4" out got
  if [[ -n "$envset" ]]; then
    out=$(printf '%s' "$input" | env -u CLAUDE_CODE_SUBAGENT_MODEL "$envset" python3 "$HOOK" 2>/dev/null)
  else
    out=$(printf '%s' "$input" | env -u CLAUDE_CODE_SUBAGENT_MODEL python3 "$HOOK" 2>/dev/null)
  fi
  got=$(classify "$out")
  if [[ "$got" == "$want" ]]; then
    echo "PASS: $desc ($got)"; pass=$((pass + 1))
  else
    echo "FAIL: $desc — want $want, got $got"; echo "  out: ${out:0:120}"; fail=$((fail + 1))
  fi
}

# script fixtures
BAD='phase("Find"); const r = await parallel(ANGLES.map(a => () => agent(a.prompt, {schema: S}))); return r.filter(Boolean);'
ROUTED='await parallel(ANGLES.map(a => () => agent(a.prompt, {model: "sonnet", schema: S})));'
THROTTLED='await mapThrottled(items, x => agent("grep "+x, {schema: S}));'
ESCAPED="$BAD"$'\n// workflow-routing-ok: tiny deliberate run'
NOFANOUT='const out = await agent("single synthesis pass", {schema: S}); return out;'
# bypass fixtures the swarm/baseline experiment surfaced (2026-06-14) — must now be CAUGHT:
MODEL_IN_COMMENT='await parallel(items.map(i => () => agent(i, {schema: S}))); // later use model: haiku'
THROTTLE_IN_COMMENT='await parallel(items.map(i => () => agent(i, {schema: S}))); // TODO: add mapThrottled'
ESCAPE_IN_STRING='await parallel(items.map(i => () => agent("see workflow-routing-ok docs", {schema: S})));'
FANOUT_SPACE='await parallel (items.map(i => () => agent(i, {schema: S})));'
FANOUT_PROMISE='await Promise.all(items.map(i => agent(i, {schema: S})));'
# v2 experiment (verify-all) findings — must now be CAUGHT:
CR_SCRIPT='// note'$'\r''await parallel(items.map(i => () => agent(i, {schema: S})));'
# pipeline() is the OTHER fan-out primitive (regex: parallel|pipeline) — must trigger detection too:
PIPELINE_BAD='await pipeline(items, x => agent(x.prompt, {schema: S}), v => agent(v, {schema: S}));'
PIPELINE_ROUTED='await pipeline(items, x => agent(x.prompt, {model: "sonnet", schema: S}));'

echo "=== require-workflow-skill.sh tests ==="

# DENY: the only blocked combo — fan-out, no model:, no throttle
check "fan-out + no routing + no throttle → DENY" "$(mkinput Workflow "$(ti_script "$BAD")")" DENY

# ALLOW: routing present
check "fan-out + per-agent model: → ALLOW"        "$(mkinput Workflow "$(ti_script "$ROUTED")")" ALLOW
# ALLOW: throttle present (mapThrottled)
check "fan-out + mapThrottled → ALLOW"            "$(mkinput Workflow "$(ti_script "$THROTTLED")")" ALLOW
# pipeline() fan-out detected like parallel(): bad combo → DENY, routed → ALLOW
check "pipeline fan-out + no routing → DENY"      "$(mkinput Workflow "$(ti_script "$PIPELINE_BAD")")" DENY
check "pipeline fan-out + per-agent model: → ALLOW" "$(mkinput Workflow "$(ti_script "$PIPELINE_ROUTED")")" ALLOW
# ALLOW: escape comment overrides
check "bad combo + // workflow-routing-ok → ALLOW" "$(mkinput Workflow "$(ti_script "$ESCAPED")")" ALLOW
# ALLOW: no fan-out (single agent inheriting is fine)
check "no fan-out (single agent) → ALLOW"         "$(mkinput Workflow "$(ti_script "$NOFANOUT")")" ALLOW
# ALLOW: CLAUDE_CODE_SUBAGENT_MODEL pins everything globally
check "bad combo + env subagent model → ALLOW"    "$(mkinput Workflow "$(ti_script "$BAD")")" ALLOW "CLAUDE_CODE_SUBAGENT_MODEL=haiku"

# scriptPath variant — hook reads the file and denies the bad pattern
BADJS="$(mktemp -t wf-bad.XXXXXX).js"; printf '%s' "$BAD" > "$BADJS"
check "scriptPath to bad-combo file → DENY"       "$(mkinput Workflow "$(python3 -c "import json,sys;print(json.dumps({'scriptPath':sys.argv[1]}))" "$BADJS")")" DENY
GOODJS="$(mktemp -t wf-good.XXXXXX).js"; printf '%s' "$ROUTED" > "$GOODJS"
check "scriptPath to routed file → ALLOW"         "$(mkinput Workflow "$(python3 -c "import json,sys;print(json.dumps({'scriptPath':sys.argv[1]}))" "$GOODJS")")" ALLOW

# Non-Workflow tool / fail-open → no output (NONE)
check "non-Workflow tool (Bash) → NONE"           '{"tool_name":"Bash","tool_input":{"command":"ls"}}' NONE
check "malformed stdin → NONE (fail-open)"        'not json{{' NONE
check "named workflow (no script) → ALLOW"        "$(mkinput Workflow '{"name":"saved-flow"}')" ALLOW

# --- bypasses the find-holes experiment surfaced (must be CAUGHT, not ALLOW'd) ---
check "model: only in a // comment → DENY"        "$(mkinput Workflow "$(ti_script "$MODEL_IN_COMMENT")")" DENY
check "throttle token only in a // comment → DENY" "$(mkinput Workflow "$(ti_script "$THROTTLE_IN_COMMENT")")" DENY
check "escape token in a string (not comment) → DENY" "$(mkinput Workflow "$(ti_script "$ESCAPE_IN_STRING")")" DENY
check "parallel( with space before paren → DENY"  "$(mkinput Workflow "$(ti_script "$FANOUT_SPACE")")" DENY
check "Promise.all fan-out, no routing → DENY"     "$(mkinput Workflow "$(ti_script "$FANOUT_PROMISE")")" DENY
check "bad combo + GARBAGE env model → DENY"       "$(mkinput Workflow "$(ti_script "$BAD")")" DENY "CLAUDE_CODE_SUBAGENT_MODEL=not-a-real-model"
check "bad combo + UPPERCASE env CLAUDE-INVALID → DENY" "$(mkinput Workflow "$(ti_script "$BAD")")" DENY "CLAUDE_CODE_SUBAGENT_MODEL=CLAUDE-INVALID"
check "bad combo + degenerate env claude-. → DENY"  "$(mkinput Workflow "$(ti_script "$BAD")")" DENY "CLAUDE_CODE_SUBAGENT_MODEL=claude-."
# env bypass is only for CHEAP pins — opus/fable pin everything EXPENSIVE = the very burst we block (panel GPT#5)
check "bad combo + env=sonnet (cheap) → ALLOW"      "$(mkinput Workflow "$(ti_script "$BAD")")" ALLOW "CLAUDE_CODE_SUBAGENT_MODEL=sonnet"
check "bad combo + env=opus (expensive) → DENY"     "$(mkinput Workflow "$(ti_script "$BAD")")" DENY "CLAUDE_CODE_SUBAGENT_MODEL=opus"
check "bad combo + env=claude-opus-4-8 (expensive) → DENY" "$(mkinput Workflow "$(ti_script "$BAD")")" DENY "CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-4-8"
check "bad combo + env=claude-haiku-4-5 (cheap) → ALLOW" "$(mkinput Workflow "$(ti_script "$BAD")")" ALLOW "CLAUDE_CODE_SUBAGENT_MODEL=claude-haiku-4-5"
check "non-string script value → ALLOW (no crash)"  '{"tool_name":"Workflow","tool_input":{"script":{"a":1}}}' ALLOW
check "CR-only line ending before fan-out → DENY"   "$(mkinput Workflow "$(ti_script "$CR_SCRIPT")")" DENY

# deny message must NOT embed the literal escape token (else re-authoring copy-pastes the bypass)
denyout=$(printf '%s' "$(mkinput Workflow "$(ti_script "$BAD")")" | env -u CLAUDE_CODE_SUBAGENT_MODEL python3 "$HOOK" 2>/dev/null)
if [[ "$(classify "$denyout")" != "DENY" ]]; then
  echo "FAIL: deny-token precondition — expected a real DENY, got $(classify "$denyout")"; fail=$((fail + 1))
elif grep -q 'workflow-routing-ok' <<<"$denyout"; then
  echo "FAIL: deny message embeds the escape token (spoofable copy-paste)"; fail=$((fail + 1))
else
  echo "PASS: deny message is a real DENY and does not embed the escape token"; pass=$((pass + 1))
fi

# enforcement OFF (the DEFAULT for consumers): the bad combo ADVISES, never blocks
advout=$(printf '%s' "$(mkinput Workflow "$(ti_script "$BAD")")" | env -u CLAUDE_CODE_SUBAGENT_MODEL -u BULLDOZER_ENFORCE_WORKFLOW_ROUTING python3 "$HOOK" 2>/dev/null)
if [[ "$(classify "$advout")" == "ALLOW" ]]; then
  echo "PASS: enforcement OFF → bad combo advises (ALLOW), no block"; pass=$((pass + 1))
else
  echo "FAIL: enforcement OFF should ALLOW (advisory), got $(classify "$advout")"; fail=$((fail + 1))
fi

echo "=== $pass passed, $fail failed ==="
[[ $fail -eq 0 ]]
