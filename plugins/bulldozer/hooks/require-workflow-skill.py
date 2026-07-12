#!/usr/bin/env python3
"""require-workflow-skill — PreToolUse(Workflow) guardrail for the bulldozer plugin.

Registered by hooks/hooks.json (`python3 ${CLAUDE_PLUGIN_ROOT}/hooks/require-workflow-skill.py`);
the tool JSON arrives on stdin. Single process: parse → resolve script → strip comments+strings →
compute signals → decide → log → emit.

ADVISORY by default — injects the routing/throttle doctrine on every Workflow call. The DENY is
OPT-IN: it fires ONLY when BULLDOZER_ENFORCE_WORKFLOW_ROUTING is truthy, so installing bulldozer
never surprise-blocks a consumer's workflows — you enable enforcement deliberately (panel verdict
2026-06-14: an opinionated heuristic guardrail must not be imposed by default). When enforced,
DENY iff a fan-out has NO per-agent model routing AND NO mapThrottled/chunk() retry, no escape
comment, and no CHEAP CLAUDE_CODE_SUBAGENT_MODEL pin. See the README for the empirical basis + limits.

Hardening from the 2026-06-14 find-holes experiment (swarm + opus baseline on this hook):
  - signals computed on code with COMMENTS and STRING LITERALS stripped → a token in a
    comment/string ('// model: haiku', "see workflow-routing-ok") no longer flips a signal
  - escape recognized ONLY inside a real comment (not any substring of the script)
  - fan-out tolerates whitespace (`parallel (`) and catches `Promise.all`
  - CLAUDE_CODE_SUBAGENT_MODEL bypass requires a CHEAP pin (haiku/sonnet); opus/fable pin everything expensive and do NOT bypass
  - the deny message no longer embeds the literal escape token (re-authoring can't copy it)
Known static limits (cannot fix without executing the script — see README): aliased/computed
fan-out (`const p=parallel; p(...)`), partial routing (1-of-N agents routed), JSON-quoted
`{"model":...}`, scriptPath TOCTOU/decoy. Fail-open everywhere; each fail-open path logs a
DISTINCT decision so the log never shows a false 'safe' (incl. unparseable stdin →
ALLOW_PARSE_ERROR and a misrouted non-Workflow tool → SKIP_NOT_WORKFLOW, #322 D4).

Log format (#322 C5/F6): one pipe-KV line per decision via lib/bulldozer_log.py —
`{ts} | event=decision | session=… | decision=… | signals… | project=…` — replacing
the pre-2026-07-12 multi-line YAML records (miners of the old epoch: records before
that date are `---`-separated YAML).
"""
import sys, json, os, re

LOG = os.environ.get("WORKFLOW_HOOK_LOG") or os.path.expanduser("~/.claude/hooks/require-workflow-skill.log")
# Canonical writer (#322 C5): sanitization, rotation, session= from env. Import
# fail-open — a guardrail hook must never block a Workflow over its own telemetry.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
try:
    from bulldozer_log import append_line as _append_line
except Exception:
    _append_line = None
_HELPER_WARNED = False
ESCAPE = "workflow-routing-ok"
# The env bypass premise is "everything pinned to a CHEAP model" → safe from the opus-burst.
# So bypass ONLY for a haiku/sonnet pin; opus/fable (or any value naming them) pins everything
# EXPENSIVE = the exact burst we deny, so it must NOT bypass (panel GPT#5, 2026-06-14).
CHEAP_ENV = re.compile(r"haiku|sonnet", re.IGNORECASE)
EXPENSIVE_ENV = re.compile(r"opus|fable", re.IGNORECASE)
# Opt-in enforcement: DENY fires only when truthy; otherwise advisory-only (never blocks).
# Keeps the distributed hook from imposing an opinionated deny on consumers (panel verdict 2026-06-14).
ENFORCE = os.environ.get("BULLDOZER_ENFORCE_WORKFLOW_ROUTING", "").strip().lower() in ("1", "true", "yes", "on")

DOCTRINE = (
    "WORKFLOW ROUTING DOCTRINE (auto-injected):\n\n"
    "1. MODEL ROUTING BY ROLE — do NOT default all agents to inherited Opus/Fable:\n"
    "   - grep/search/extraction/lookup/read-only → model: \"haiku\" (~10-15x cheaper, rate-limit-safe at scale)\n"
    "   - verify/judge/review/classify → model: \"sonnet\" (~3-5x cheaper, degrades gracefully)\n"
    "   - synthesis/planning/hard reasoning → model: \"opus\" EXPLICITLY (omit = inherit = fable in a Fable session)\n"
    "   Example: agent(prompt, { model: \"haiku\", schema: S })\n\n"
    "2. THROTTLE + RESILIENCE — do NOT burst dozens, and NEVER drop rate-limited nulls:\n"
    "   - wrap schema-agent fan-out in mapThrottled (retries nulls); never .filter(Boolean) the gap away\n"
    "   - opus collapses under rate-limit (58-93% of agents fail); haiku/sonnet survive — route the bulk cheap\n\n"
    "3. TOKEN BUDGET — guard loops on budget.total (null → remaining()=Infinity → unguarded → 1000-agent cap).\n\n"
    "4. pipeline() runs items through stages with NO barrier — it FANS OUT across items, not sequential.\n\n"
    "5. Use aliases haiku/sonnet/opus/fable, not pinned model IDs.\n\n"
    "6. find-holes/review at scale: recall-preserving swarm → rank-by-agreement → escalate → verify. "
    "Full doctrine + patterns: invoke skill:workflow-swarms."
)

DENY_REASON = (
    "Workflow blocked by require-workflow-skill: this fan-out sets NO per-agent model AND has no "
    "mapThrottled/chunk() retry. Every agent would inherit your session model (Opus/Fable — 2x cost, "
    "and ~3.5x more rate-limits in our 238-run history; opus also collapses 58-93% under rate-limit), "
    "and a rate-limited result would be silently dropped (the 0-findings incident). FIX BOTH: "
    "(1) route per role — agent(p, {model: 'haiku'}) for grep/extract, {model: 'sonnet'} for verify, "
    "{model: 'opus'} explicitly for synthesis; (2) wrap the fan-out in mapThrottled so rate-limited nulls "
    "are retried/flagged, never .filter(Boolean)'d away. Invoke skill:workflow-swarms for the patterns. "
    "A deliberate-override escape exists for genuinely-small or all-opus runs — see "
    "skill:bulldozer:workflow-swarms — but prefer fixing routing+throttle."
)


def strip(src):
    """Return (code, comments): code with // line, /* */ block comments and string literals
    removed (replaced by a space); comments = the concatenated comment text."""
    src = src.replace("\r\n", "\n").replace("\r", "\n")  # CR/CRLF → LF so `//` stops at any line end
    code, comments = [], []
    i, n = 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            comments.append(src[i:j]); i = j; continue
        if two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            comments.append(src[i:j]); i = j; continue
        ch = src[i]
        if ch in "\"'`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2; continue
                if src[j] == ch:
                    j += 1; break
                j += 1
            code.append(" "); i = j; continue
        code.append(ch); i += 1
    return "".join(code), "\n".join(comments)


def log(decision, project=None, **sig):
    global _HELPER_WARNED
    if _append_line is None:
        if not _HELPER_WARNED:
            print("warning: bulldozer_log helper unavailable — decision line dropped",
                  file=sys.stderr)
            _HELPER_WARNED = True
        return
    fields = {"decision": decision}
    fields.update(sig)
    if project:
        fields["project"] = project  # F6: join key to the per-skill invoke lines
    _append_line(LOG, "decision", **fields)


def resolve_project(data):
    """Project root for the F6 `project=` field: git toplevel of the hook's cwd
    (same normalization as log_skill_invoke.py, so lines join across logs),
    falling back to the raw cwd. None when the payload carries no usable cwd."""
    cwd = data.get("cwd") if isinstance(data, dict) else None
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        from log_skill_invoke import resolve_project as _rp  # sibling hook module
        return _rp(cwd)
    except Exception:
        return cwd


def emit_allow():
    print(json.dumps({"systemMessage": DOCTRINE}))


def emit_deny():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": DENY_REASON,
    }}))


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        # the matcher guarantees Workflow-tool JSON here — a parse failure is a
        # broken CC contract, not out-of-scope traffic (#322 D4). Still silent
        # on stdout (allow); the log is the only place the anomaly surfaces.
        log("ALLOW_PARSE_ERROR", note="stdin not JSON")
        return
    project = resolve_project(data)
    if not isinstance(data, dict) or data.get("tool_name") != "Workflow":
        # dead under the hooks.json matcher — a line here means the hook got
        # registered with a broader matcher somewhere (#322 D4)
        tool = data.get("tool_name") if isinstance(data, dict) else type(data).__name__
        log("SKIP_NOT_WORKFLOW", project=project, tool=str(tool))
        return  # not the Workflow tool → silent (exit 0, no output)

    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        log("ALLOW_UNPARSED", project=project, note="tool_input not an object")
        emit_allow(); return

    script = ti.get("script") or ""
    origin = "inline"
    if not script and ti.get("scriptPath"):
        try:
            script = open(os.path.expanduser(str(ti["scriptPath"]))).read()
            origin = "scriptPath"
        except Exception as e:
            log("ALLOW_UNREADABLE", project=project,
                scriptPath=str(ti.get("scriptPath")), error=type(e).__name__)
            emit_allow(); return
    if not script:
        log("ALLOW_NAMED", project=project, note="named/resume — no script body to inspect")
        emit_allow(); return
    if not isinstance(script, str):  # truthy non-string (dict/list/int) → strip() would crash
        log("ALLOW_UNPARSED", project=project, note=f"script not a string ({type(script).__name__})")
        emit_allow(); return

    code, comments = strip(script)
    fanout = bool(re.search(r"\b(?:parallel|pipeline)\s*\(", code) or re.search(r"\bPromise\.all\b", code))
    model_count = len(re.findall(r"\bmodel\s*:", code))
    throttle = bool(
        re.search(r"\bmapThrottled\b", code)
        or re.search(r"\bchunk\s*\(", code)
        or re.search(r"\bCLAUDE_FALLBACK_MODEL\b", code)
    )
    escape = ESCAPE in comments.lower()
    env = os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL", "").strip()
    env_cheap = bool(env) and bool(CHEAP_ENV.search(env)) and not EXPENSIVE_ENV.search(env)

    if env_cheap:
        decision = "ALLOW_ENV"
    elif escape:
        decision = "ALLOW_ESCAPE"
    elif fanout and model_count == 0 and not throttle:
        decision = "DENY" if ENFORCE else "ADVISORY"  # opt-in: advise (don't block) unless enforcing
    else:
        decision = "ALLOW"

    log(decision, project=project, fanout=int(fanout), model_count=model_count,
        throttle=int(throttle), escape=int(escape), env=(env or "unset"),
        env_cheap=int(env_cheap), enforce=int(ENFORCE), origin=origin)

    emit_deny() if decision == "DENY" else emit_allow()


if __name__ == "__main__":
    main()
