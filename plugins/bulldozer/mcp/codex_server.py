#!/usr/bin/env python3
"""bulldozer codex MCP server — v2 app-server bridge.

Shipped as a bulldozer plugin MCP server (`.mcp.json` → tool
`mcp__plugin_bulldozer_codex__codex_run`). Zero dependencies (Python stdlib).

V2 ARCHITECTURE (replaces v1 "wrap codex exec"):
  - Fronts `codex app-server` (bidirectional JSON-RPC 2.0 over stdio), the same
    engine the Codex web app drives.
  - CORRECT approvals: mid-turn command/file-change/permissions approvals are
    forwarded to Claude Code via MCP elicitation (fixes codex#18268 where the
    stock MCP accept was mis-parsed as Denied).
  - RESUME: `thread_id` resumes an existing app-server thread (cross-session
    rollout via non-ephemeral threads).
  - STRUCTURED output: mode=review constrains the turn via `outputSchema` →
    guaranteed {verdict,findings} JSON.
  - ISOLATION: per-thread `baseInstructions` + config override (`mcp_servers={}`)
    keeps each thread sterile of user's codex-plugin skills/config.
  - GRACEFUL no-codex: if codex is not installed, tools/call returns a clean
    error result — the server never crashes.

Wire protocol (CC-facing): line-delimited JSON-RPC 2.0 over stdio. stdout =
JSON-RPC 2.0 frames ONLY; all logging to stderr. During a tools/call, the
dispatcher holds stdin exclusively for cc_read_fn; #264: every CC read MUST route
through the shared CCStream (os.read + JsonRpcStream), NEVER sys.stdin.readline()
on the CC fd. main(), cc_read_fn, and Reactor.pump(watch_cc) all drain one module
singleton (_cc_stream) so a burst of frames in one CC write can't strand a 2nd
frame in the TextIOWrapper buffer, and os.read/readline never mix on the same fd.

See docs/superpowers/specs/2026-06-18-codex-mcp-v2-app-server-bridge.md.
"""
import atexit
import collections
import datetime
import functools
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


def _python_version_error(version_info=None):
    """Return a clear error string if the interpreter is too old for tomllib (3.11+), else None.

    `.mcp.json` launches a bare `python3`; on py<3.11 the unconditional `import tomllib` below
    would otherwise die with a cryptic `ModuleNotFoundError: tomllib` at import (#256).
    """
    vi = sys.version_info if version_info is None else version_info
    if tuple(vi[:2]) < (3, 11):
        return (f"bulldozer-codex MCP server requires Python 3.11+ (uses tomllib); "
                f"got {vi[0]}.{vi[1]}. Relaunch with a 3.11+ interpreter (e.g. via uv).")
    return None


_pyver_err = _python_version_error()
if _pyver_err:
    sys.stderr.write(_pyver_err + "\n")
    raise SystemExit(1)

import tomllib
import urllib.request

# #334: every audit line routes through the shared canonical writer (same
# sys.path pattern as codex_facade.py). Guarded: a missing/broken helper
# disables the audit channel (warn once at write time, drop lines) — NEVER a
# raw legacy fallback (a second writer is exactly the drift #334 closed).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
try:
    from bulldozer_log import (append_line as _bl_append,
                               redact_urls_in_text as _bl_redact)
except Exception:
    _bl_append = None
    _bl_redact = None

_CODEX_FALLBACK = "/opt/homebrew/bin/codex"


def _resolve_codex_bin() -> str:
    """Resolve the codex binary from the CURRENT environment (re-read per call, NOT frozen
    at import). Order: JAINE_CODEX_BIN override → bare-name PATH search → absolute fallback.
    Re-resolving per call picks up a mid-session codex install/upgrade/relocation: a binary
    newly added to PATH is found (bare 'codex' searches PATH) where a frozen absolute
    fallback would miss it (#227 item 1)."""
    return os.environ.get("JAINE_CODEX_BIN") or shutil.which("codex") or _CODEX_FALLBACK


def _codex_bin_available(bin_path: str | None = None) -> bool:
    """True iff the codex binary exists/is on PATH. Resolves from the current env when
    bin_path is None. A bare name is searched on PATH; an absolute/relative path is checked
    directly (shutil.which as a PATH fallback)."""
    b = bin_path if bin_path is not None else _resolve_codex_bin()
    return bool(os.path.isfile(b) or shutil.which(b))


# Import-time snapshot — kept for error-message display, the AppServerManager string default,
# and the canary tests (bin=cs.CODEX). RUNTIME binary checks call _resolve_codex_bin() so a
# mid-session codex install/upgrade is picked up (#227).
CODEX = _resolve_codex_bin()
PROTO = "2025-06-18"
LAST_VERIFIED_CODEX_VERSION = "0.144"   # last codex app-server version this bridge was verified against

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["GO", "NO-GO", "MINOR-FIXES"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "summary"],
                "properties": {
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}

# Reasoning-effort surface — single source for BOTH tool schemas (codex_run /
# codex_review) so they can never diverge; pinned by the protocol fingerprint
# ("effort_enum"). low..xhigh are supported by every model in the catalog; the
# GATED tiers exist only on newer families (2026-07: max = gpt-5.6 sol/terra/luna,
# ultra = sol/terra only), so a gated request is pre-validated against the LIVE
# model/list catalog (_validate_effort_support) instead of a hardcoded model→efforts
# table that would drift with every catalog change.
SUPPORTED_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
GATED_EFFORTS = frozenset({"max", "ultra"})

_EFFORT_DESCRIPTION = (
    "Reasoning effort. low/medium/high/xhigh: every model. max/ultra are PER-MODEL "
    "— the live catalog (codex_info query='models') is authoritative; currently "
    "(codex 0.144) max = GPT-5.6 family (sol/terra/luna), ultra = gpt-5.6-sol/terra "
    "only (adds automatic task delegation), older models (gpt-5.5/5.4/spark) cap at "
    "xhigh. With max/ultra pass `model` explicitly — the server pre-validates the "
    "pair against the live catalog and rejects unsupported combos BEFORE the "
    "expensive thread start; with `model` omitted the pair passes through as-is."
)

TOOLS = [
    {
        "name": "codex_run",
        "description": (
            "Run OpenAI Codex via the app-server bridge, returning structured JSON. "
            "mode=review -> {thread_id,verdict,findings,schema_ok} (outputSchema-enforced); "
            "mode=implement -> {thread_id,result} (free text). "
            "Pass thread_id to resume an existing thread (cross-session). "
            "Interactive approvals (command/file-change/permissions) are forwarded to "
            "Claude Code via MCP elicitation — approval_policy=on-request enables them. "
            "If UNATTENDED approval is armed, instead of a human dialog this returns "
            "{status:'awaiting_approval', park_token, approval:{kind, decisions:[{id,label}], "
            "evidence}} — YOU decide from the evidence and call codex_approve(park_token, "
            "decision_id) to resume (decision_id = an approval.decisions[].id, or 'decline'). "
            "REQUIRED mcp arg selects which MCP servers codex sees: 'isolated'/'all'/"
            "'list'/[subset] (call mcp='list' first to discover servers). "
            "Returns a _drift array if upstream codex protocol drift is detected."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["prompt", "mcp"],
            "properties": {
                "prompt": {"type": "string"},
                "mcp": {
                    "description": (
                        "REQUIRED. Which MCP servers codex sees this call: 'isolated' "
                        "(disable all user servers + apps; clean review/implement), 'all' "
                        "(full normal toolset), a list like ['dash','deepwiki','apps'] (keep "
                        "only those), or 'list' to discover the available servers without "
                        "running codex. computer-use (bundled) always remains."
                    ),
                    "oneOf": [
                        {"type": "string", "enum": ["isolated", "all", "list"]},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "mode": {"type": "string", "enum": ["review", "implement"], "default": "review"},
                "thread_id": {
                    "type": "string",
                    "description": "Resume an existing thread (cross-session). Omit to start a new thread.",
                },
                "approval_policy": {
                    "type": "string",
                    "enum": ["never", "on-request", "on-failure", "untrusted"],
                    "description": "on-request (default) triggers CC elicitation dialogs for approvals.",
                },
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write", "danger-full-access"],
                    "default": "read-only",
                },
                "effort": {"type": "string", "enum": list(SUPPORTED_EFFORTS),
                           "default": "medium", "description": _EFFORT_DESCRIPTION},
                "model": {"type": "string"},
                "cwd": {"type": "string", "description": "Working dir. Omit for an isolated tmpdir."},
                "base_instructions": {"type": "string"},
                "developer_instructions": {"type": "string"},
                "config": {
                    "type": "object",
                    "description": (
                        "Benign codex Config passthrough (keys NOT in _CONFIG_DENY pass "
                        "through to thread/start verbatim). Reachable knobs (codex 0.141): "
                        "model_verbosity (low/medium/high), web_search (bool), review_model, "
                        "model_reasoning_summary (auto/concise/detailed/none), personality, "
                        "model_context_window, model_auto_compact_token_limit, compact_prompt. "
                        "mcp_servers / baseInstructions / developerInstructions are scrubbed."
                    ),
                },
                "approvals_reviewer": {
                    "type": "string",
                    "enum": ["user", "auto_review", "guardian_subagent"],
                    "description": "Who approves codex actions: user (CC elicitation), "
                                   "auto_review / guardian_subagent (risk-based auto-approve). "
                                   "NOTE: auto_review is a serde ALIAS of guardian_subagent — "
                                   "codex accepts both as input but ECHOES back guardian_subagent "
                                   "(so r['codex']['approvals_reviewer'] reads guardian_subagent "
                                   "even when you sent auto_review).",
                },
                "service_tier": {"type": "string", "description": "codex speed/cost tier."},
                "timeout": {
                    "type": "number",
                    "description": "Optional work-duration cap in seconds. Omit for no cap "
                                   "(matches stock codex; the engine's stream-idle timeout "
                                   "still catches a hung model).",
                },
            },
        },
    },
    {
        "name": "codex_info",
        "description": (
            "Read codex app-server state (connection-level — no cold-start, fast). "
            "query: models (model catalog) | auth (login status) | config (current codex "
            "config) | limits (rate limits) | usage (token usage) | servers (MCP servers "
            "codex sees) | features (experimental flags) | profiles (permission profiles) | "
            "approval (the #277 unattended/park knobs + their source — purely local, no codex). "
            "Returns {query, result}."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["models", "auth", "config", "limits", "usage",
                             "servers", "features", "profiles", "approval"],
                    "description": "Which read to perform.",
                },
            },
        },
    },
    {
        "name": "codex_review",
        "description": (
            "Native git-aware codex code review (review/start). Reviews a git diff and "
            "returns prioritized findings as free text. target: 'uncommitted' (default) | "
            "'branch:<name>' | 'commit:<sha>' | 'custom:<instructions>'. Pass cwd = the git "
            "repo to review. Read-only. Returns {thread_id, review, ...}."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["mcp"],
            "properties": {
                "target": {
                    "type": "string",
                    "description": "uncommitted (default) | branch:<name> | commit:<sha> "
                                   "| custom:<instructions>",
                },
                "cwd": {"type": "string", "description": "Path to the git repo to review."},
                "mcp": {
                    "description": "REQUIRED. Same as codex_run: 'isolated' / 'all' / "
                                   "['dash', …] / 'list'.",
                    "oneOf": [
                        {"type": "string", "enum": ["isolated", "all", "list"]},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "model": {"type": "string"},
                "effort": {"type": "string", "enum": list(SUPPORTED_EFFORTS),
                           "default": "medium", "description": _EFFORT_DESCRIPTION},
                "timeout": {"type": "number",
                            "description": "Optional work-duration cap in seconds."},
            },
        },
    },
    {
        "name": "codex_approve",
        "description": (
            "Resume a codex turn that PARKED at an approval (unattended model-in-the-loop, #277). "
            "When codex_run returns {status:'awaiting_approval', park_token, approval:{decisions:[…]}}, "
            "the orchestrating model decides and calls codex_approve to resume the parked turn. "
            "Single-use park_token; decision_id is one of the approval's decisions[].id (or 'decline'). "
            "NOT a codex_run overload — no prompt/mcp. Returns the turn's final result, the next "
            "awaiting_approval payload (multi-approval turn), or {error:'parked turn expired'}."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["park_token", "decision_id"],
            "properties": {
                "park_token": {"type": "string",
                               "description": "The park_token from the awaiting_approval payload."},
                "decision_id": {"type": "string",
                                "description": "Chosen decisions[].id (or 'decline')."},
            },
        },
    },
]


def log(*a):
    print("[bulldozer-codex]", *a, file=sys.stderr, flush=True)


_HELPER_WARNED = False  # once-per-process "audit disabled" warning (#334)


def _drift_warn(acc, code: str, detail=None, **fields) -> None:
    """Record an upstream-drift breadcrumb / audit line. NEVER raises.

    acc: per-call list (appended for user-facing _drift) or None (log-only,
    e.g. VERSION_MISMATCH). acc keeps the ORIGINAL detail — only the durable
    copy is URL-redacted.

    #334: the durable line routes through lib/bulldozer_log.append_line —
    canonical grammar ({ts+offset} | event=CODE | session=S | k=v…), sanitized
    values, 5MB locked rotation. Generic codes pass detail= ; named writers
    pass structured **fields. Redaction is UNCONDITIONAL per value (R7-F1:
    the redactor str()-coerces internally, so a wire-derived dict/list cannot
    bypass it — append_line would stringify AFTER the redaction decision
    otherwise; output-equivalent for clean values). Helper unavailable →
    warn once, drop the line (no legacy fallback — a second writer is the
    drift #334 closed).
    """
    global _HELPER_WARNED
    if acc is not None:
        acc.append({"code": code, "detail": detail})
    try:
        if _bl_append is None:
            if not _HELPER_WARNED:
                _HELPER_WARNED = True
                log("audit disabled: bulldozer_log helper unavailable")
            return
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer-codex.log")
        kv = {}
        if detail is not None:
            kv["detail"] = _bl_redact(detail)
        for k, v in fields.items():
            kv[k] = _bl_redact(v)
        # #344 facade: the ONE engine touch — a facade worker sets
        # BULLDOZER_WORKER=N, stamping every audit line (TURN_OK/TURN_ERROR/
        # APPROVAL/… all route through here) with its worker id; stays LAST.
        worker = os.environ.get("BULLDOZER_WORKER")
        if worker:
            kv["worker"] = worker
        _bl_append(path, code, **kv)
    except Exception:
        pass


def _turn_error_log(ts: dict, emsg) -> None:
    """#320: best-effort TURN_ERROR audit line for a TERMINAL turn failure.

    Terminal errors are returned to the calling session and previously left NO
    durable trace (e4328466: 4x "model is at capacity", zero log entries) — this
    line makes error rates minable per model/effort. Transient willRetry errors
    are deliberately NOT logged (noise); their count arrives here as retries=N.
    Never raises; never touches the caller-facing result.
    """
    try:
        # Attribute to the EFFECTIVE model/effort: top-level args win, else the
        # thread/start|resume echo in _last_thread_meta (config-routed / resumed
        # calls) — the same fallback _build_result_meta uses (#321 review P2).
        tm = getattr(ts.get("manager"), "_last_thread_meta", {}) or {}
        # rerouted_model (model/rerouted, e.g. capacity fallback) outranks the REQUESTED
        # model — a failure after a reroute belongs to the model that actually ran (#321 r2).
        # TRUNCATION ORDER (#334 R3-F1): values pass UNtruncated — the helper's
        # _value cap is the ONE truncation point, applied AFTER redaction.
        _drift_warn(None, "TURN_ERROR",
                    model=ts.get('rerouted_model') or ts.get('model_val') or tm.get('model') or 'default',
                    effort=ts.get('effort_val') or tm.get('effort') or 'default',
                    mcp=ts.get('mcp_mode') or '?',
                    retries=ts.get('retries') or 0,
                    msg=str(emsg or 'unknown error'))
    except Exception:
        pass


def _turn_ok_log(ts: dict, meta: dict) -> None:
    """#322 PR2: best-effort TURN_OK audit line for a COMPLETED turn — the
    success-side counterpart of TURN_ERROR (#320). Without it error RATES are
    not computable (no denominator) and per-model/effort latency is invisible.
    Never raises; never touches the caller-facing result."""
    try:
        tm = getattr(ts.get("manager"), "_last_thread_meta", {}) or {}
        timing = (meta or {}).get("timing") or {}
        tokens = ((meta or {}).get("usage") or {}).get("total_tokens")
        # tokens is wire-derived (tokenUsage notification) — _drift_warn redacts
        # every value type-unconditionally (#334 R7-F1), the helper sanitizes.
        kw = dict(
            model=ts.get('rerouted_model') or ts.get('model_val') or tm.get('model') or 'default',
            effort=ts.get('effort_val') or tm.get('effort') or 'default',
            mcp=ts.get('mcp_mode') or '?',
            retries=ts.get('retries') or 0,
            duration_ms=timing.get('duration_ms'),
            tokens=tokens if tokens is not None else 'NA',
        )
        if ts.get("setup_ms") is not None:
            kw["setup_ms"] = ts["setup_ms"]
            kw["cold_spawn"] = 'true' if ts.get('cold_spawn') else 'false'
        _drift_warn(None, "TURN_OK", **kw)
    except Exception:
        pass


def _interrupt_log(ts: dict, interrupted_by: str, thread_warm: bool) -> None:
    """#322 PR2 (A5/#218): best-effort INTERRUPT audit line. An interrupt is a
    graceful partial, not a failure — it gets its own kind, never TURN_ERROR."""
    try:
        tm = getattr(ts.get("manager"), "_last_thread_meta", {}) or {}
        _drift_warn(None, "INTERRUPT",
                    interrupted_by=interrupted_by,
                    thread_warm='true' if thread_warm else 'false',
                    model=ts.get('rerouted_model') or ts.get('model_val') or tm.get('model') or 'default',
                    mcp=ts.get('mcp_mode') or '?')
    except Exception:
        pass


def _fail_meta(ts: dict) -> dict:
    """Result meta for a terminal failure OUTSIDE the frame-handler paths (start-ACK
    rejection, pre-ACK terminal error, child EOF, ACK/turn timeouts, the exception
    catch-all) — so the result schema does not depend on WHERE the turn failed
    (#325 r2). Best-effort: an inconsistent ts yields {} rather than masking the error."""
    try:
        return _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "failed", ts=ts)
    except Exception:
        return {}


def _info_error_log(query, msg) -> None:
    """#322 PR2 (F3): best-effort INFO_ERROR audit line — a codex_info outage
    (spawn/read failure) was previously visible only to the caller."""
    try:
        _drift_warn(None, "INFO_ERROR", query=query,
                    msg=str(msg or 'unknown error'))
    except Exception:
        pass


def _warning_log(params: dict) -> None:
    """#320: best-effort WARNING audit line for the codex `warning` notification.

    Previously flagged UNKNOWN_NOTIFICATION with the payload DROPPED (a lost
    capacity-storm precursor on 2026-07-11). It is an explicit codex signal, not
    protocol drift — log the message, keep it out of the _drift channel."""
    try:
        p = params or {}
        msg = p.get("message")
        if not isinstance(msg, str) or not msg:
            w = p.get("warning")
            msg = w.get("message") if isinstance(w, dict) else None
        if not isinstance(msg, str) or not msg:
            msg = json.dumps(p)  # unknown shape — keep SOMETHING greppable
        _drift_warn(None, "WARNING", msg=str(msg))
    except Exception:
        pass


def _parse_codex_version(user_agent: str):
    """Return 'MAJOR.MINOR' of the codex app-server version, parsed from the
    initialize userAgent ('<clientName>/<codexVersion> (os) <terminal> ...'),
    or None. Never raises.

    Extracts the version from the FIRST whitespace-delimited token
    ('<clientName>/<codexVersion>') so later tokens like 'iTerm.app/3.7...'
    cannot win.  Empty or malformed input returns None.
    """
    try:
        first = (user_agent or "").split(None, 1)[0]   # "<clientName>/<codexVersion>"
        m = re.search(r"/(\d+)\.(\d+)", first)
        return f"{m.group(1)}.{m.group(2)}" if m else None
    except Exception:
        return None


def _stamp_drift(result: dict, acc) -> dict:
    """Attach result['_drift'] = acc iff acc is non-empty; return result."""
    if acc:
        result["_drift"] = acc
    return result


_CC_EOF = object()   # #218: cc_read_fn EOF sentinel (distinct from None = transient/timeout)


def reply(mid, result=None, error=None):
    """Write a standard JSON-RPC 2.0 frame to CC (stdout)."""
    msg = {"jsonrpc": "2.0", "id": mid}
    msg["error" if error else "result"] = error if error else result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


SERVER_INSTRUCTIONS = (
    "External codex agent (separate from the check/consult skills, which shell out to "
    "`codex exec`). Routing:\n"
    "- Adversarial review of a git diff → `codex_review` (target `uncommitted` | `branch:<name>` "
    "| `commit:<sha>` | `custom:<instructions>`).\n"
    "- Autonomous coding/research task in isolation → `codex_run` (mode `review` | `implement`).\n"
    "- Check codex availability / models / config / auth / usage → `codex_info` (no cold start).\n"
    "- If unattended approval is armed, `codex_run` may return `{status:awaiting_approval, "
    "park_token, approval:{kind, decisions:[{id,label}], …evidence…}}` INSTEAD of prompting a human "
    "— YOU decide from the evidence and call `codex_approve(park_token, decision_id)` to resume the "
    "parked turn (decision_id = one of approval.decisions[].id, or 'decline').\n"
    "Pass `mcp:'isolated'` unless cross-server tools are needed."
)


def _lane_instructions() -> str:
    """The instructions manifest, lane-aware. When this process is a FAN-OUT LANE (a named
    `claude mcp add codex-laneN --env BULLDOZER_LANE=N …` registration of this same server),
    a concrete routing preamble LEADS the manifest — otherwise a fresh session sees several
    identical codex servers and has no way to know the lanes exist FOR parallel subagent
    dispatch (lane-pool, 2026-07-13: N lane registrations = N processes = true parallelism;
    one subagent per lane, since subagents SHARE each registration's single connection).
    No env → byte-identical to SERVER_INSTRUCTIONS (plugin server, consumer configs)."""
    lane = (os.environ.get("BULLDOZER_LANE") or "").strip()
    # Simple-integer validation (Copilot, PR #342): a non-numeric value ("lane1", "1-2", a
    # newline) would advertise a tool name that cannot exist (mcp__codex-lanelane1__…) and
    # could mangle the manifest — misconfiguration degrades to the plain instructions.
    if not lane.isdigit():
        return SERVER_INSTRUCTIONS
    return (
        f"FAN-OUT LANE {lane} (lane pool): use this server to run codex jobs IN PARALLEL from "
        "subagents — assign ONE subagent per lane; a second concurrent call into the SAME lane "
        "is rejected (`codex turn already in flight`). Parked approvals are LANE-LOCAL: if a "
        f"codex_run here returns awaiting_approval, resume it with THIS lane's codex_approve "
        f"(mcp__codex-lane{lane}__codex_approve) — any other server answers `parked turn "
        "expired`. Route single interactive codex work through the primary bulldozer codex "
        "server (or any idle lane).\n\n"
        + SERVER_INSTRUCTIONS
    )


def _initialize_result(params: dict) -> dict:
    """Build the MCP initialize reply. Carries an `instructions` routing manifest — CC injects
    InitializeResult.instructions into the model's context on connect, giving it a server-level
    map to discover/choose codex_review / codex_run / codex_info (#256); lane registrations
    self-describe as fan-out lanes (BULLDOZER_LANE, above)."""
    return {
        "protocolVersion": params.get("protocolVersion", PROTO),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "bulldozer-codex", "version": _plugin_version()},
        "instructions": _lane_instructions(),
    }


def main():
    """V2 MCP dispatcher: every CC read routes through the shared CCStream (#264) — os.read +
    JsonRpcStream, NEVER sys.stdin.readline() — so a burst of frames in one CC write can't
    strand a 2nd frame in the TextIOWrapper buffer, and main()/cc_read_fn/pump never mix
    os.read with readline on the same fd. main() is the first and only stdin reader."""
    _reset_cc_stream()   # fresh CC buffer; nothing has read stdin before this

    def cc_write_fn(frame: dict):
        """Forward a CC-facing JSON-RPC 2.0 frame to stdout (elicitation/create etc.)."""
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    def cc_read_fn(timeout: float = 10.0):
        """Read the next CC elicitation response via the shared CCStream (#264).

        Returns the parsed dict, None on timeout / blank / bad JSON, or _CC_EOF on EOF.
        bridge_approval expects: {"jsonrpc":"2.0","id":N,"result":{"action":...,"content":...}}
        """
        kind, frame = _cc_stream.next_frame(timeout)
        if kind == "eof":
            return _CC_EOF       # #218: EOF is distinct from None (transient) so the approval wait tears down
        if kind == "frame":
            return frame
        return None              # 'none' — timeout / blank / bad JSON (JsonRpcStream dropped it)

    while True:
        if _v2_state_machine.is_parked():
            # #277 §7: a turn is parked — DON'T block forever (the model may never resume). Run a
            # finite cap-bounded wait that also watches the child (CCStream is blind to it); on
            # cap/EOF/child-death it tears the park down. A CC frame is handed back to dispatch below.
            kind, req = _parked_wait(_get_manager(), _v2_state_machine, cc_write_fn)
        else:
            kind, req = _cc_stream.next_frame(None)   # block between turns
        if kind == "eof":
            break  # CC stdin closed
        if req is None:
            continue  # 'none' — partial frame buffered; keep waiting (bad-JSON/blank dropped by CCStream)
        method, mid, params = req.get("method"), req.get("id"), req.get("params", {}) or {}

        if method == "initialize":
            reply(mid, _initialize_result(params))
        elif method == "notifications/initialized":
            pass  # notification — no reply
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                # Keep ALL params access inside the try: `req.get("params",{}) or {}` only
                # coerces FALSY non-dicts, so a truthy non-object params (list/str) reaches
                # here as-is and .get() would raise — one malformed frame must NOT crash the
                # long-lived dispatcher (it returns the guarded error reply below).
                args = params.get("arguments") or {}  # `arguments: null` → {} (not None)
                args["_cc_id"] = mid  # inject for busy/eof framing
                tool_name = params.get("name")
                if _parked_busy_block(tool_name, _v2_state_machine):
                    # #277 C12: a turn is parked — every non-approve tool busy-blocks, park PRESERVED.
                    res = _v2_state_machine.busy_error()
                elif tool_name == "codex_info":
                    res = codex_info_v2(args)
                elif tool_name == "codex_review":
                    res = codex_review_v2(args, cc_write_fn=cc_write_fn, cc_read_fn=cc_read_fn)
                elif tool_name == "codex_approve":
                    # #277: resume a parked turn — routed BY NAME before the codex_run fallback and
                    # before any validation/ensure() (codex_approve needs no prompt/mcp/codex binary).
                    res = codex_approve_v2(args, cc_write_fn=cc_write_fn, cc_read_fn=cc_read_fn)
                else:
                    res = codex_run_v2(args, cc_write_fn=cc_write_fn, cc_read_fn=cc_read_fn)
                tool_result: dict = {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}
                if "error" in res:
                    tool_result["isError"] = True
                reply(mid, tool_result)
            except Exception as e:
                log("tool error:", repr(e))
                reply(mid, {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                            "isError": True})
        elif method is not None and mid is not None:
            reply(mid, error={"code": -32601, "message": f"method not found: {method}"})
        # else: a response-shaped frame (id, no method) — e.g. a late elicitation
        # reply that arrived in the main loop. Ignore it; never reply method-not-found
        # to CC's own response frames (that would be a JSON-RPC protocol violation).


# ── v2 app-server bridge ──────────────────────────────────────────────────
# Task 2: framed non-blocking JSON-RPC I/O + select-based two-fd Reactor.
# Tasks 3-6 (manager, approval bridge, tool) build on these.
# ─────────────────────────────────────────────────────────────────────────


class JsonRpcStream:
    """Per-fd newline-delimited JSON-RPC byte buffer.

    `feed(chunk: bytes) -> list[dict]` yields complete frames accumulated
    since the last call.  Partial frames are buffered; blank lines ignored;
    invalid JSON silently dropped (caller logs).
    """

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes) -> list:
        self._buf += chunk
        out = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # logged by caller
        return out


class CCStream:
    """Single owner of CC stdin (fd 0) (#264). All CC-side reads route here so a burst of
    frames in one OS write is fully drained (os.read) and parsed (JsonRpcStream) into a queue
    — no TextIOWrapper-buffer stranding (where select can't see a 2nd buffered line). os.read
    and TextIOWrapper.readline must NEVER both read this fd: every CC read goes through CCStream,
    none through readline (the no-mixing invariant). main(), cc_read_fn, and Reactor.pump
    (watch_cc=True) all drain the SAME module singleton (_cc_stream)."""

    def __init__(self):
        self._stream = JsonRpcStream()      # reuse the proven child-side byte buffer
        self._queue = collections.deque()   # complete parsed frames awaiting delivery
        self._eof = False

    def _fd(self):
        # Resolve LAZILY each call so a test that monkeypatches sys.stdin (os.fdopen(pipe))
        # is honored. Production fd is 0; never cached.
        return sys.stdin.fileno()

    def has_queued(self) -> bool:
        """True iff a parsed frame is already buffered (delivery needs no select). Lets pump
        force a 0 child-select timeout so a queued CC frame is delivered promptly — without
        leaking the deque."""
        return bool(self._queue)

    def _drain_ready(self, timeout):
        """select once; if the fd is ready, os.read the whole chunk and feed it to the
        JsonRpcStream, extending the queue. A 0-byte read => EOF (sticky)."""
        if self._eof:
            return
        ready, _, _ = select.select([self._fd()], [], [], timeout)
        if not ready:
            return
        try:
            chunk = os.read(self._fd(), 65536)
        except OSError:
            chunk = b""
        if not chunk:
            self._eof = True
            return
        self._queue.extend(self._stream.feed(chunk))

    def next_frame(self, timeout):
        """Return (kind, value):
          ('frame', dict) — a parsed frame. Queue checked FIRST: a prior burst's 2nd frame is
                            delivered WITHOUT re-selecting (the F4 fix — readline stranded it).
          ('none',  None) — timeout / only a partial frame this call (retry within deadline).
          ('eof',   None) — fd closed AND queue fully drained (real bytes always precede the
                            0-byte read on a pipe, so queued frames surface before EOF).
        """
        if self._queue:
            return ("frame", self._queue.popleft())
        self._drain_ready(timeout)
        if self._queue:
            return ("frame", self._queue.popleft())
        if self._eof:
            return ("eof", None)
        return ("none", None)


_cc_stream = CCStream()   # module singleton — like _v2_manager / _v2_state_machine


def _reset_cc_stream():
    """Install a fresh CCStream (clears queue / _buf / _eof). Called at main() startup
    (harmless in prod) and by an autouse test fixture so same-process tests never inherit
    queued frames, a partial buffer, or a sticky EOF from a prior test."""
    global _cc_stream
    _cc_stream = CCStream()


def classify(msg: dict) -> str:
    """Shape-first JSON-RPC message classifier.

    response     = has id + (result | error) and NO method
    request      = has method + id
    notification = has method, no id
    invalid      = everything else

    CRITICAL: id alone must NEVER decide — a server→client request and a
    response can share the same id.  Shape (presence of result/error/method)
    is the authoritative discriminator.
    """
    has_method = isinstance(msg, dict) and "method" in msg
    has_id = isinstance(msg, dict) and "id" in msg
    has_result = isinstance(msg, dict) and ("result" in msg or "error" in msg)
    if has_method and has_id:
        return "request"
    if has_method and not has_id:
        return "notification"
    if has_id and has_result and not has_method:
        return "response"
    return "invalid"


class Reactor:
    """select-based two-fd I/O multiplexer for the app-server bridge.

    Reads from `child_out_fd` (app-server → us) and writes are handled by
    the caller on `child_in_fd` (us → app-server).  Child stderr is drained
    to a temp file in a background thread so a stderr flood can't deadlock
    the select loop.

    `pump(timeout=0.1) -> list[dict]` returns all complete frames received
    from the child in one call; never blocks longer than `timeout` seconds.
    """

    def __init__(self, child_out_fd: int, child_in_fd: int):
        self._child_out_fd = child_out_fd
        self._stream = JsonRpcStream()
        self._child_in_fd = child_in_fd  # kept for caller convenience
        self.stderr_file = None          # set after _start_stderr_drain

    def _start_stderr_drain(self, stderr_fd: int):
        """Drain `stderr_fd` into a temp file on a background thread.

        Must be called before the first pump() when the child's stderr fd is
        known.  Prevents select-loop deadlock if the child floods stderr.

        Reads from the fd directly (os.read) rather than wrapping with
        os.fdopen so the caller retains ownership of the file object.
        """
        self.stderr_file = tempfile.TemporaryFile(prefix="reactor-stderr-")

        def _drain():
            try:
                while True:
                    chunk = os.read(stderr_fd, 4096)
                    if not chunk:
                        break
                    self.stderr_file.write(chunk)
            except (OSError, ValueError):
                # OSError: fd closed/child gone. ValueError: a teardown closed stderr_file
                # (_close_reactor_stderr) while this daemon was mid-write (#227b F3) — swallow
                # so the drain thread exits quietly instead of an unhandled-exception traceback.
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()

    def pump(self, timeout: float = 0.1, watch_cc: bool = False) -> list:
        """Return complete JSON-RPC frames received from the child.

        watch_cc=False (default): child-only select — byte-identical to the
        original; never blocks longer than `timeout` even on a partial frame.
        watch_cc=True: ALSO drain the shared CCStream (#264). A CC frame read this
        call is appended tagged {"__cc__": <parsed>} ({"__cc__": {"__eof__": True}}
        on EOF) so the caller can route it (#218). Child frames are never tagged; at
        most one CC frame per call. The child select uses a 0 timeout when CCStream
        already has a queued frame (deliver it now) — else it also watches the CC fd
        so a fresh CC frame wakes the select.

        #264: the CC side reads via CCStream (os.read + JsonRpcStream), NOT
        sys.stdin.readline(), so a burst of frames in one CC write is fully drained
        into the queue and a 2nd frame (e.g. a cancel after a ping) is delivered to
        the NEXT pump from the queue instead of stranding in the TextIOWrapper
        buffer (the F4 hole select couldn't see). A blank/bad-JSON CC line is dropped
        by JsonRpcStream (no {"__cc__": None} tag — was a no-op route anyway)."""
        cc_queued = watch_cc and _cc_stream.has_queued()
        watch = [self._child_out_fd]
        if watch_cc and not cc_queued:
            watch.append(_cc_stream._fd())
        ready, _, _ = select.select(watch, [], [], 0 if cc_queued else timeout)
        out = []
        if self._child_out_fd in ready:
            try:
                chunk = os.read(self._child_out_fd, 65536)
            except OSError:
                chunk = b""
            if chunk:
                out.extend(self._stream.feed(chunk))
        if watch_cc:
            kind, frame = _cc_stream.next_frame(0)
            if kind == "eof":                         # CC stdin closed (e.g. CC tool-call timeout)
                out.append({"__cc__": {"__eof__": True}})
            elif kind == "frame":
                out.append({"__cc__": frame})
            # 'none' → no CC frame this call
        return out


# ── v2 app-server manager ─────────────────────────────────────────────────
# Task 3: AppServerManager — lazy spawn, initialize/initialized handshake,
# start_thread (non-ephemeral isolation), resume_thread, crash-respawn.
# ─────────────────────────────────────────────────────────────────────────

def _plugin_version() -> str:
    """Read version from .claude-plugin/plugin.json (never drifts)."""
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_json = os.path.join(here, "..", ".claude-plugin", "plugin.json")
    try:
        with open(plugin_json) as f:
            return json.load(f).get("version", "0.0.0")
    except (OSError, json.JSONDecodeError):
        return "0.0.0"


STERILE_INSTRUCTIONS = (
    "You are a focused code reviewer and implementer invoked through an isolated tool. "
    "Act only on the instructions in the current turn's prompt. Do not load, infer, or apply "
    "any external project conventions, AGENTS/CLAUDE files, user-configured skills, or plugin "
    "instructions. Be precise, honest, and concise."
)
# Isolation lives at SPAWN now (#204): per-server `-c mcp_servers.<n>.enabled=false` (BARE
# keys — quoting CRASHES app-server, verified) + `--disable apps`, built by
# _build_isolation_argv from the caller's `mcp` knob.
# We do NOT relocate CODEX_HOME (auth/sessions stay intact) and we do NOT clear the
# whole mcp_servers table via `-c mcp_servers={}` — an empty-table deep-merge is a
# verified NO-OP (codex 0.141). computer-use is a bundled plugin with no ephemeral
# disable, so it remains even in "isolated" (documented limitation).

# Keys a caller MUST NOT inject into the per-thread config — they would
# compromise the isolation invariant or replicate thread-level params that
# are already handled by start_thread's explicit kwargs.
_CONFIG_DENY = frozenset({
    "mcp_servers", "mcpServers",
    "baseInstructions", "base_instructions",
    "developerInstructions", "developer_instructions",
})


def _is_child_alive(child) -> bool:
    """Return True if child is alive (poll() returns None)."""
    return child is not None and child.poll() is None


class _ChildHandle:
    """Thin wrapper around a subprocess.Popen child for I/O consistency.

    Provides the same interface as FakeChild:
    - .stdin  (writable binary file-like)
    - .stdout (readable binary file-like)
    - .stderr (readable binary file-like)
    - .poll() -> None (alive) or int (dead)
    - .kill()
    - .returncode
    """

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self.stdin = proc.stdin
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    def poll(self):
        return self._proc.poll()

    def kill(self):
        self._proc.kill()

    @property
    def returncode(self):
        return self._proc.returncode


VALID_MCP_MODES = frozenset({"isolated", "all", "list"})


def _codex_home() -> str:
    """The effective CODEX_HOME (env override or ~/.codex)."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _enumerate_config_mcp_servers(codex_home: str | None = None) -> list[str]:
    """Return the sorted names of [mcp_servers.*] tables in $CODEX_HOME/config.toml.

    Best-effort: a missing/unreadable/malformed config.toml yields []. Never raises.
    These are the user's configured MCP servers — the set we may disable at spawn.
    """
    home = codex_home or _codex_home()
    path = os.path.join(home, "config.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        servers = data.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            return []
        return sorted(servers.keys())
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return []


def _is_targetable_server_name(name: str) -> bool:
    """True if `name` can be targeted by `-c mcp_servers.<name>.enabled=false`.

    EMPIRICAL (codex 0.141, probe-verified): codex's CLI override parser splits the dotted
    key path on '.' NAIVELY and does NOT parse TOML quoted keys. A BARE identifier works
    (`-c mcp_servers.dash.enabled=false` → dash tools:4→0). A QUOTED key
    (`-c mcp_servers."dash".enabled=false`) does NOT merely mis-target — it CRASHES
    app-server startup: `Error: error loading default config after config error: invalid
    transport in mcp_servers."dash"` (rc=1). So we NEVER quote; a name containing '.'
    (path-separator ambiguity), '"' (crashes), or '=' (codex's override parser does
    `splitn(2, '=')`, so `mcp_servers.foo=bar.enabled=false` mis-targets the key
    `mcp_servers.foo` and silently fails to disable `foo=bar`) is unreachable via -c → skip it.
    """
    return "." not in name and '"' not in name and "=" not in name


def _build_isolation_argv(mcp, config_servers: list[str]) -> list[str]:
    """Build the `codex app-server` argv tokens (after 'app-server') for an mcp selection.

    'all'      → [] (nothing disabled; codex's full normal setup).
    'isolated' → disable every config server + `--disable apps` (computer-use remains).
    list[str]  → keep only the named servers/builtins; disable the rest; `--disable apps`
                 unless 'apps' is in the list.

    Server names are used BARE (unquoted) — quoting CRASHES app-server (see
    _is_targetable_server_name). A name with '.'/'"' is unreachable via -c and is SKIPPED
    with a stderr warning (left enabled), never silently mis-targeted or crashed. Raises
    ValueError for any other `mcp` value (callers validate first; defense-in-depth).
    """
    def _disable(name):
        if not _is_targetable_server_name(name):
            print(f"[bulldozer-codex] WARNING: cannot disable MCP server {name!r} via -c "
                  "(name contains '.', '\"', or '=', unreachable by codex's key-path/value "
                  "parser); leaving it enabled", file=sys.stderr)
            return []
        return ["-c", f"mcp_servers.{name}.enabled=false"]

    if mcp == "all":
        return []
    if mcp == "isolated":
        argv: list[str] = []
        for name in config_servers:
            argv += _disable(name)
        argv += ["--disable", "apps"]
        return argv
    if isinstance(mcp, list):
        keep = set(mcp)
        argv = []
        for name in config_servers:
            if name not in keep:
                argv += _disable(name)
        if "apps" not in keep:
            argv += ["--disable", "apps"]
        return argv
    raise ValueError(f"invalid mcp value: {mcp!r}")


# Fail-closed child-env allowlist (#204 1c). Only what codex itself needs passes;
# every unrelated CC secret (FORGEJO/ANTHROPIC/custom *_TOKEN/*_KEY) is dropped.
# codex's OWN credentials (OPENAI_*) are allowlisted — codex reading its own key is
# not the leak we guard against (unrelated CC secrets are).
_CHILD_ENV_ALLOW_EXACT = frozenset({
    "HOME", "PATH", "TMPDIR", "TMP", "TEMP", "TERM", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "CODEX_HOME",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "CODEX_CA_CERTIFICATE",   # codex-client custom CA — takes PRECEDENCE over SSL_CERT_FILE
                              # (codex-rs/codex-client custom_ca). Omitting it breaks TLS for
                              # corp users on a custom CA. (OPENAI_API_KEY is the only auth env
                              # codex reads; OPENAI_BASE_URL is config-only — kept as harmless.)
    # #357 R11-F1: the stable-log override knobs are forwarded WHEN PRESENT —
    # configuration paths, not secrets, so the fail-closed property is
    # unchanged. Without this a codex descendant is env-scrubbed past the test
    # suite's redirect and a stable-log producer it runs falls back to the real
    # $HOME. In production the knobs are unset → no-op. (codex SHELL children
    # still get shell_environment_policy inherit=core — the residual there is
    # covered by codex's own sandbox: no test runs danger-full-access.)
    "BULLDOZER_LOG", "BULLDOZER_CODEX_LOG", "BULLDOZER_LOOK_LOG",
    "BULLDOZER_CONSULT_LOG", "BULLDOZER_DRIVE_LOG", "WORKFLOW_HOOK_LOG",
    "BULLDOZER_INVOKE_LOG_DIR",
})
_CHILD_ENV_ALLOW_PREFIX = ("LC_",)


def _build_child_env(parent_env: dict) -> dict:
    """Return the fail-closed allowlisted env for the spawned codex child.

    Only known-needed vars pass; everything else (CC secrets) is dropped.
    """
    out = {}
    for k, v in parent_env.items():
        if k in _CHILD_ENV_ALLOW_EXACT or k.startswith(_CHILD_ENV_ALLOW_PREFIX):
            out[k] = v
    return out


# Layer 2 (spec 1c, defense-in-depth, F1): constrain what codex's SHELL SUBPROCESSES
# inherit, independent of the process env. Layer 1 (above) already drops CC secrets from
# the child env; layer 2 additionally keeps even allowlisted codex creds (OPENAI_*) out of
# arbitrary shell commands codex runs. `inherit=core` = codex's minimal-safe set (PATH/HOME/
# locale), which keeps implement-mode shells functional. Applied at spawn for ALL mcp modes.
_SHELL_ENV_POLICY_ARGV = ["-c", "shell_environment_policy.inherit=core"]


def _spawn_appserver(codex_bin: str, isolation_argv: list | None = None) -> _ChildHandle:
    """Spawn `codex app-server` with the resolved isolation argv and a scrubbed env.

    isolation_argv (from _build_isolation_argv) disables the user's MCP servers /
    `apps` at the PROCESS level — the only mechanism that actually works on codex
    0.141 (per-thread `config.mcp_servers` overrides are no-ops). The child env is
    fail-closed allowlisted (_build_child_env, layer 1) and `_SHELL_ENV_POLICY_ARGV`
    (layer 2, F1) constrains what codex's shell subprocesses inherit — both spec 1c
    layers, applied for ALL mcp modes.
    """
    argv = [codex_bin, "app-server", *_SHELL_ENV_POLICY_ARGV, *(isolation_argv or [])]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_build_child_env(os.environ),
    )
    return _ChildHandle(proc)


class AppServerManager:
    """Manages the lifecycle of a persistent `codex app-server` child.

    Responsibilities:
    - Lazy spawn: `ensure()` spawns the child on first call and returns it.
    - Initialize handshake: sends `initialize` (with clientInfo + capabilities)
      and waits for the response, then sends the `initialized` notification.
    - Crash detection: `ensure()` detects a dead child (poll() not None) and
      respawns + re-initializes on the next call.
    - `start_thread(...)`: sends `thread/start` with isolation params and
      returns the thread_id from the response.
    - `resume_thread(thread_id)`: sends `thread/resume {threadId}` (by-id,
      the verified stable method per Task 1 probe) and returns the result.

    `bin` accepts:
    - None (default): resolve the codex binary lazily from the env at EACH spawn
      (_resolve_codex_bin) — picks up a mid-session codex install/upgrade (#227 item 1c).
    - A string path (or CODEX constant): spawned via `codex app-server` (fixed; canary tests).
    - A pre-built child object (FakeChild in tests) with .stdin/.stdout/.stderr
      and .poll()/.kill()/.returncode.  Used directly on the first ensure();
      respawned via `bin.__class__()` on subsequent ensure() after a crash.
    """

    def __init__(self, bin=None):
        self._bin = bin   # None → resolve codex lazily at spawn; str → fixed; object → FakeChild
        self._child = None
        self._reactor: Reactor | None = None
        self._next_id_val = 1
        self._next_id_lock = threading.Lock()
        self._codex_version = None
        self._isolation_sig = None   # tuple(isolation_argv); None forces first spawn
        self._last_thread_meta = {}
        # #277 return-and-resume: the parked-turn record while a turn is suspended at a
        # park_for_model approval — {park_token, thread_id, inner_gen, isolation_sig, started_at,
        # request_frame, decision_ids}. None when no turn is parked.
        self._parked = None

    def _next_id(self) -> int:
        with self._next_id_lock:
            mid = self._next_id_val
            self._next_id_val += 1
        return mid

    def _write(self, msg: dict, child=None):
        """Write a single JSON-RPC frame to a child's stdin. Default: the live self._child;
        an explicit `child` lets the transactional respawn write to a temp child (#227b)."""
        c = child if child is not None else self._child
        data = (json.dumps(msg) + "\n").encode()
        c.stdin.write(data)
        c.stdin.flush()

    def _pump_until(self, predicate, timeout: float = 5.0, reactor=None) -> dict | None:
        """Pump a reactor until predicate(frame) is True or timeout expires. Default: the live
        self._reactor; an explicit `reactor` lets the transactional respawn pump a temp (#227b)."""
        r = reactor if reactor is not None else self._reactor
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            frames = r.pump(timeout=min(remaining, 0.2))
            for frame in frames:
                if predicate(frame):
                    return frame
        return None

    def _do_initialize(self, child=None, reactor=None):
        """Run the initialize → initialized handshake. Default: the live self._child/_reactor;
        explicit (child, reactor) let the transactional respawn initialize a temp pair BEFORE
        committing it onto self (#227b)."""
        mid = self._next_id()
        version = _plugin_version()
        self._write({
            "id": mid,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "bulldozer-codex-mcp",
                    "title": None,
                    "version": version,
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        }, child=child)
        # Wait for the initialize response.
        # _pump_until is safe here: any frames in the same chunk after the initialize
        # response are post-handshake notifications — dropping them is acceptable
        # because nothing post-response is consumed at this stage.
        resp = self._pump_until(
            lambda f: classify(f) == "response" and f.get("id") == mid,
            timeout=10.0,
            reactor=reactor,
        )
        if resp is None:
            raise RuntimeError("AppServerManager: initialize response timed out")
        ua = (resp.get("result") or {}).get("userAgent", "") if isinstance(resp, dict) else ""
        self._codex_version = _parse_codex_version(ua)
        if self._codex_version != LAST_VERIFIED_CODEX_VERSION:
            _drift_warn(None, "VERSION_MISMATCH",
                        f"last-verified {LAST_VERIFIED_CODEX_VERSION}, live {ua!r}")
        # Send the initialized notification
        self._write({"method": "initialized"}, child=child)

    def _make_reactor(self, child) -> "Reactor":
        """Build a Reactor for `child` and start its stderr drain. PURE: does NOT mutate self
        or close any existing reactor — the transactional respawn builds a temp reactor and
        commits it onto self only after initialize succeeds (#227b)."""
        r = Reactor(
            child_out_fd=child.stdout.fileno(),
            child_in_fd=child.stdin.fileno(),
        )
        r._start_stderr_drain(child.stderr.fileno())
        return r

    @staticmethod
    def _close_reactor_stderr(reactor) -> None:
        """Best-effort close of a reactor's stderr-drain TemporaryFile (+ its drain-thread ref)
        so a discarded reactor — the temp on failure, or the old one on commit — does not leak."""
        if reactor is not None and getattr(reactor, "stderr_file", None) is not None:
            try:
                reactor.stderr_file.close()
            except Exception:
                pass

    def ensure(self, isolation_argv: list | None = None) -> object:
        """Return the live child, spawning (and initializing) if necessary.

        Warm reuse iff the child is alive AND its isolation signature matches the request.
        A changed signature (different `mcp` selection) or a crashed child triggers a
        TRANSACTIONAL respawn: a fresh (child, reactor) is spawned AND initialized FIRST, and
        only on success is the old child killed and the new pair committed onto self. Any
        spawn/init failure tears down only the temp and leaves the existing warm child +
        signature intact (#227b) — the old kill-then-spawn destroyed a still-usable child
        whenever the new spawn or its initialize failed. Idempotent for the same signature.
        """
        sig = tuple(isolation_argv or [])
        if _is_child_alive(self._child) and self._isolation_sig == sig:
            self.last_ensure_spawned = False  # warm reuse (#322 PR2 B7)
            return self._child

        # Spawn a new child. _bin is None (lazy: resolve the codex binary from the CURRENT
        # env per spawn — #227 item 1c), a fixed path string, or a pre-built child object.
        codex_bin = _resolve_codex_bin() if self._bin is None else self._bin
        if isinstance(codex_bin, str):
            new_child = _spawn_appserver(codex_bin, list(isolation_argv or []))
        elif self._child is None:
            new_child = self._bin              # first call: provided object (FakeChild in tests)
        else:
            new_child = self._bin.__class__()  # respawn: fresh instance via the same class

        # Build + initialize the TEMP pair before touching self. On any failure — including a
        # _make_reactor that raises AFTER the child is spawned (#227b F1) — tear down only the
        # temp (kill the spawned child + close its reactor) so the warm child + _isolation_sig
        # stay intact (transactional respawn).
        new_reactor = None
        try:
            new_reactor = self._make_reactor(new_child)
            self._do_initialize(child=new_child, reactor=new_reactor)
        except Exception:
            try:
                new_child.kill()
            except Exception:
                pass
            self._close_reactor_stderr(new_reactor)   # None-safe
            raise

        # Commit: initialize succeeded → swap onto self, then retire the old child + reactor.
        old_child, old_reactor = self._child, self._reactor
        self._child = new_child
        self._reactor = new_reactor
        self._isolation_sig = sig
        self.last_ensure_spawned = True  # cold spawn (#322 PR2 B7)
        if old_child is not None and old_child is not new_child:
            try:
                old_child.kill()
            except Exception:
                pass
        if old_reactor is not None and old_reactor is not new_reactor:
            self._close_reactor_stderr(old_reactor)
        return self._child

    def start_thread(
        self,
        sandbox: str = "read-only",
        approval_policy: str = "on-request",
        base_instructions=None,
        developer_instructions=None,
        config: dict | None = None,
        cwd: str | None = None,
        one_shot: bool = False,
        approvals_reviewer: str | None = None,
        service_tier: str | None = None,
    ) -> str:
        """Send thread/start and return the thread_id.

        NON-ephemeral by default (ephemeral=True only when one_shot=True,
        as ephemeral threads have no on-disk rollout → no cross-session resume).

        base_instructions: None → STERILE_INSTRUCTIONS (sentinel); "" is a valid caller value.
        developer_instructions: None → omitted from params; any string → wire key developerInstructions.
        """
        bi = STERILE_INSTRUCTIONS if base_instructions is None else base_instructions
        caller_cfg = config if isinstance(config, dict) else {}
        # Scrub deny-keys (mcp_servers/instructions); pass benign keys through.
        # NOTE (#204): we do NOT inject mcp_servers={} — that per-thread override is a
        # no-op on codex 0.141. MCP isolation is enforced at SPAWN (_build_isolation_argv).
        config = {k: v for k, v in caller_cfg.items() if k not in _CONFIG_DENY}
        mid = self._next_id()
        params: dict = {
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
            "baseInstructions": bi,
            "config": config,
        }
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        if cwd is not None:
            params["cwd"] = cwd
        if approvals_reviewer is not None:
            params["approvalsReviewer"] = approvals_reviewer
        if service_tier is not None:
            params["serviceTier"] = service_tier
        if one_shot:
            params["ephemeral"] = True
        self._write({
            "id": mid,
            "method": "thread/start",
            "params": params,
        })
        resp = self._pump_until(
            lambda f: classify(f) == "response" and f.get("id") == mid,
            # The FIRST thread/start on a fresh app-server process pays codex's
            # one-time cold start; empirically highly variable (~28-80s+ observed)
            # depending on API/model-load conditions. Paid once per session — the
            # persistent process makes every later thread/start warm (~1-2s) — so a
            # generous ceiling here costs nothing on warm calls (returns on response).
            timeout=180.0,
        )
        if resp is None:
            raise RuntimeError("AppServerManager: thread/start response timed out")
        if "error" in resp:
            raise RuntimeError(f"AppServerManager: thread/start error: {resp['error']}")
        result = resp.get("result", {})
        self._last_thread_meta = {
            "model": result.get("model"),
            "service_tier": result.get("serviceTier"),
            "effort": result.get("reasoningEffort"),
            "approvals_reviewer": result.get("approvalsReviewer"),
        }
        return result["thread"]["id"]

    def resume_thread(self, thread_id: str, timeout: float = 180.0) -> dict:
        """Send thread/resume {threadId} (by-id, verified stable per Task 1 probe).

        Returns the response result dict. Default timeout is generous: a
        cross-session resume is the FIRST op on a freshly-spawned app-server
        process, so it pays the same one-time cold start as a cold thread/start
        (~28-80s+ variable). Warm resume returns in ~1s regardless.
        """
        mid = self._next_id()
        self._write({
            "id": mid,
            "method": "thread/resume",
            "params": {"threadId": thread_id},
        })
        resp = self._pump_until(
            lambda f: classify(f) == "response" and f.get("id") == mid,
            timeout=timeout,
        )
        if resp is None:
            raise RuntimeError(f"AppServerManager: thread/resume timed out for thread {thread_id!r}")
        if "error" in resp:
            raise RuntimeError(
                f"AppServerManager: thread/resume error for {thread_id!r}: {resp['error']}"
            )
        _r = resp.get("result", {}) or {}
        self._last_thread_meta = {
            "model": _r.get("model"),
            "service_tier": _r.get("serviceTier"),
            "effort": _r.get("reasoningEffort"),
            "approvals_reviewer": _r.get("approvalsReviewer"),
        }
        return _r

    def connection_request(self, method: str, params=None, timeout: float = 30.0) -> dict:
        """Send a connection-level request (no thread/start, no cold-start) and
        return its `result`. Used by codex_info for read methods that answer at
        connection level (model/list, config/read, account/*, …). Raises on
        timeout or an error reply."""
        mid = self._next_id()
        frame: dict = {"id": mid, "method": method}
        if params is not None:
            frame["params"] = params
        self._write(frame)
        resp = self._pump_until(
            lambda f: classify(f) == "response" and f.get("id") == mid,
            timeout=timeout,
        )
        if resp is None:
            raise RuntimeError(f"{method} response timed out")
        if "error" in resp:
            raise RuntimeError(f"{method} error: {resp['error']}")
        return resp.get("result", {}) or {}


# ── v2 approval bridge + ServerRequest router ─────────────────────────────
# Task 4: handle_server_request routes all 10 ServerRequest methods;
# bridge_approval issues a CC elicitation/create and maps the answer to the
# exact codex decision; TurnStateMachine tracks the in-flight turn.
# ─────────────────────────────────────────────────────────────────────────

_bridge_id_counter = 0
_bridge_id_lock = threading.Lock()


def _next_bridge_id() -> int:
    global _bridge_id_counter
    with _bridge_id_lock:
        _bridge_id_counter += 1
        return _bridge_id_counter


# ── Human-readable approval labels ────────────────────────────────────────
# CC renders the elicitation requestedSchema enum as a dropdown. These are the
# DISPLAY strings the human sees; the codex DECISION value (string or dict) is
# preserved EXACTLY so the #18268 label→decision mapping stays intact. A label
# also doubles as the reverse-map key, so labels must be UNIQUE within a prompt
# (enforced by _dedupe_labels for the variable amendment sets).
LBL_ALLOW_ONCE = "Allow once"
LBL_ALLOW_SESSION = "Allow for the rest of this session"
LBL_DONT_ALLOW = "Don't allow"
LBL_CANCEL = "Cancel the turn"
LBL_EXECPOLICY = "Allow & always permit this command"
LBL_GRANT_TURN = "Grant for this turn"
LBL_GRANT_SESSION = "Grant for this session"
LBL_DONT_GRANT = "Don't grant"

# commandExecution string decisions → display label
_CMD_STRING_DISPLAY = {
    "accept": LBL_ALLOW_ONCE,
    "acceptForSession": LBL_ALLOW_SESSION,
    "decline": LBL_DONT_ALLOW,
    "cancel": LBL_CANCEL,
}

# Minimal safe default for a permissions prompt (decline / timeout / non-accept).
PERM_DECLINE = {"permissions": {}, "scope": "turn"}


def _network_label(host: str, action: str, index: int) -> str:
    """Human label for an applyNetworkPolicyAmendment, embedding host (+action).

    host+action keeps two amendments for the same host distinct; the index is a
    last-resort uniqueness fallback when host is unknown.
    """
    if host:
        if action:
            return f"Allow & always permit network access to {host} ({action})"
        return f"Allow & always permit network access to {host}"
    return f"Allow & always permit network rule #{index + 1}"


def _dedupe_labels(pairs: list) -> list:
    """Guarantee display labels are unique (they double as reverse-map keys).

    A collision gets a numeric suffix; decision values are never touched. The
    suffix is checked against the labels already EMITTED (not merely the inputs
    seen), so a generated ``"foo (2)"`` cannot collide with a natural input
    ``"foo (2)"`` or a prior suffix — otherwise ``dict(pairs)`` would silently
    drop a decision and misroute the human's choice (the #18268 invariant).
    """
    emitted: set = set()
    out = []
    for label, decision in pairs:
        candidate = label
        n = 1
        while candidate in emitted:
            n += 1
            candidate = f"{label} ({n})"
        emitted.add(candidate)
        out.append((candidate, decision))
    return out


def build_command_approval_labels(params: dict, acc=None) -> list:
    """Build [(display_label, decision), ...] pairs for a commandExecution prompt.

    Display labels are human-readable (LBL_* / _network_label); the codex DECISION
    value is preserved verbatim. Labels are de-duplicated so each is a valid
    reverse-map key.

    PRIMARY: when params["availableDecisions"] is a non-empty list, build one pair
    per entry, in codex's order. String entry → mapped via _CMD_STRING_DISPLAY.
    Dict entry → human label by kind (execpolicy / network), unknown kinds fall
    back to ``"<first-key>:<array-index>"``.

    FALLBACK (availableDecisions absent/null/empty): derive from request fields.
    """
    avail = params.get("availableDecisions")
    if avail:
        result = []
        for i, entry in enumerate(avail):
            if isinstance(entry, str):
                result.append((_CMD_STRING_DISPLAY.get(entry, entry), entry))
            elif isinstance(entry, dict):
                if not entry:                        # {} → malformed, would StopIteration
                    _drift_warn(acc, "UNKNOWN_DECISION_VARIANT", "empty-dict entry")
                    continue
                kind = next(iter(entry))
                if kind == "acceptWithExecpolicyAmendment":
                    result.append((LBL_EXECPOLICY, entry))
                elif kind == "applyNetworkPolicyAmendment":
                    # isinstance discipline (mirrors the FALLBACK path): a truthy
                    # non-dict at either level must not reach .get() (AttributeError
                    # would escape the dispatcher and hang the turn).
                    apnpa = entry.get("applyNetworkPolicyAmendment")
                    apnpa = apnpa if isinstance(apnpa, dict) else {}
                    amend = apnpa.get("network_policy_amendment")
                    amend = amend if isinstance(amend, dict) else {}
                    result.append((
                        _network_label(amend.get("host", ""), amend.get("action", ""), i),
                        entry,
                    ))
                else:
                    _drift_warn(acc, "UNKNOWN_DECISION_VARIANT", kind)
                    result.append((f"{kind}:{i}", entry))  # unknown future variant — verbatim preserved
            # else: skip malformed entries
        if result:
            return _dedupe_labels(result)

    # FALLBACK: derive from request fields
    pairs: list = [
        (LBL_ALLOW_ONCE, "accept"),
        (LBL_ALLOW_SESSION, "acceptForSession"),
        (LBL_DONT_ALLOW, "decline"),
        (LBL_CANCEL, "cancel"),
    ]
    proposed_exec = params.get("proposedExecpolicyAmendment")
    if proposed_exec is not None:
        pairs.append((
            LBL_EXECPOLICY,
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": proposed_exec}},
        ))
    for i, amendment in enumerate(params.get("proposedNetworkPolicyAmendments") or []):
        host = amendment.get("host", "") if isinstance(amendment, dict) else ""
        action = amendment.get("action", "") if isinstance(amendment, dict) else ""
        pairs.append((
            _network_label(host, action, i),
            {"applyNetworkPolicyAmendment": {"network_policy_amendment": amendment}},
        ))
    return _dedupe_labels(pairs)


def _truncate_for_display(text, max_lines: int = 12, max_chars: int = 800,
                          tail_lines: int = 0) -> str:
    """Cap a command / narrative preview for an approval dialog (#239).

    Returns text unchanged when it fits (≤ max_lines AND ≤ max_chars). Otherwise
    keep a bounded preview and append a '… (+N more lines, M more chars)' marker.

    tail_lines > 0 ALWAYS keeps a bounded HEAD and a bounded TAIL (marker in the
    middle) — by lines when there are middle lines to omit, otherwise by CHARS
    (reserving ~1/3 of max_chars for the tail). So a dangerous op appended AFTER a
    long benign heredoc — OR after one huge generated line — stays visible at
    approval time. This is the command path (codex_review #239 security finding,
    rounds 1+2: the real 04ad23aa incident hid `uv run pytest` after a ~230-line
    `cat <<PY` heredoc; a char-only cap would still hide a trailing `&& rm`).
    tail_lines == 0 → head-only (narrative / single-value context fields).

    The approval DECISION still permits the FULL command — only the displayed
    preview is capped. max_chars bounds the BODY (the marker is appended after).
    Codepoint-safe (str slicing never splits a multi-byte char); not grapheme /
    display-width aware (sufficient for a preview).
    """
    if not text:
        return ""
    lines = text.split("\n")
    if len(lines) <= max_lines and len(text) <= max_chars:
        return text
    if tail_lines:
        # bounded head + bounded tail. Reserve part of the char budget for the tail so
        # an appended executable op stays visible even when truncation is char-driven.
        tail_budget = max(1, max_chars // 3)
        head_budget = max_chars - tail_budget
        if len(lines) > max_lines:
            head_text = "\n".join(lines[:max_lines - tail_lines])
            tail_text = "\n".join(lines[len(lines) - tail_lines:])
        else:
            head_text, tail_text = text, ""   # too long by CHARS only → char split below
        # Enforce char budgets: head from the START, tail from the END (where the
        # appended op lives). Fall to a pure char split when the line tail is empty
        # (few lines) or the line head+tail still blows the budget (a huge head line).
        if not tail_text or len(head_text) + len(tail_text) > max_chars:
            head_text = text[:head_budget]
            tail_text = text[-tail_budget:]
    else:
        # head-only (narrative / single-value context fields)
        head_text = "\n".join(lines[:max_lines])
        if len(head_text) > max_chars:
            head_text = head_text[:max_chars]
        tail_text = ""
    shown_lines = (head_text.count("\n") + 1) + (tail_text.count("\n") + 1 if tail_text else 0)
    dropped_lines = max(0, len(lines) - shown_lines)
    dropped_chars = len(text) - len(head_text) - len(tail_text)
    parts = []
    if dropped_lines > 0:
        parts.append(f"{dropped_lines} more line{'s' if dropped_lines != 1 else ''}")
    if dropped_chars > 0:
        parts.append(f"{dropped_chars} more char{'s' if dropped_chars != 1 else ''}")
    marker = f"… (+{', '.join(parts)})"
    return f"{head_text}\n{marker}\n{tail_text}" if tail_text else f"{head_text}\n{marker}"


def _summarize_command_actions(actions) -> str:
    """One compact friendly line summarizing best-effort parsed CommandActions (#224).

    codex's own TUI does not render these; they are a friendly supplement, never
    authoritative (the raw command stays the source of truth). Defensive against
    shape drift — non-dict items and 'unknown' actions (which add nothing over the
    raw command) are skipped. Shapes (codex 0.141): read{name,path}, listFiles{path?},
    search{query?,path?}, unknown{}.
    """
    if not isinstance(actions, list):
        return ""
    out = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        t = a.get("type")
        if t == "read":
            out.append(f"read {a.get('name') or a.get('path') or '?'}")
        elif t == "listFiles":
            out.append(f"list {a.get('path') or '.'}")
        elif t == "search":
            q = a.get("query")
            out.append(f"search {q!r}" if q else f"search {a.get('path') or '?'}")
        elif t and t != "unknown":
            out.append(str(t))  # future variant — surface the kind verbatim
        # 'unknown' / missing type → skip (raw command already shown)
    return ", ".join(out)


# ── #247: opt-in approval-dialog localization (translate codex's reason+narrative) ──
# codex itself stays English (likely better at code/command tasks); only the dialog's
# DYNAMIC content (reason + narrative) is translated, via the LiteLLM gateway. Static
# labels come from _dialog_labels. Opt-in (BULLDOZER_APPROVAL_LANG, default off),
# fail-open to English on ANY error; key/model/endpoint read FRESH per call (lazy).

_DIALOG_LABELS_EN = {
    "approval_request": "Codex approval request",
    "command": "Command", "cwd": "CWD", "reason": "Reason", "actions": "Actions",
    "network": "Network: codex requests {proto} access to {host}",
    "explained": "Codex explained",
    "filechange": "Codex file change approval",
    "permissions": "Codex permissions approval",
}
_DIALOG_LABELS = {
    "ru": {
        "approval_request": "Запрос codex на одобрение",
        "command": "Команда", "cwd": "CWD", "reason": "Причина", "actions": "Действия",
        "network": "Сеть: codex запрашивает {proto}-доступ к {host}",
        "explained": "Codex пояснил",
        "filechange": "Codex: одобрение изменения файла",
        "permissions": "Codex: одобрение прав",
    },
}


def _dialog_labels(lang):
    """Static dialog chrome for `lang` (English fallback for unset/unknown langs).

    Normalizes to the primary subtag ('ru-RU'/'ru_RU'/'RU' → 'ru') so a region-tagged
    language doesn't translate content yet show English chrome (mixed dialog)."""
    code = (lang or "").strip().lower().replace("_", "-").split("-")[0]
    return _DIALOG_LABELS.get(code, _DIALOG_LABELS_EN)


def _approval_lang():
    return (os.environ.get("BULLDOZER_APPROVAL_LANG") or "").strip()


def _translate_key():
    # read FRESH each call (lazy): the LiteLLM key isn't in the bridge env at import and
    # may rotate; never snapshot. BULLDOZER_TRANSLATE_API_KEY wins, else LITELLM_MASTER_KEY.
    return os.environ.get("BULLDOZER_TRANSLATE_API_KEY") or os.environ.get("LITELLM_MASTER_KEY") or ""


def _translate_endpoint():
    return os.environ.get("BULLDOZER_TRANSLATE_ENDPOINT") or "http://localhost:4000/v1/chat/completions"


def _translate_model():
    return os.environ.get("BULLDOZER_TRANSLATE_MODEL") or "grok-4.20"


def _translate_timeout():
    try:
        v = float(os.environ.get("BULLDOZER_TRANSLATE_TIMEOUT") or "2.5")
    except ValueError:
        return 2.5
    return max(0.5, min(v, 10.0))   # clamp: never block the dispatcher arbitrarily long


def _translate_http(endpoint, model, key, prompt, timeout):
    """Raw POST to an OpenAI-compatible chat endpoint (LiteLLM). Returns the content
    string; raises on any error. Isolated so tests can monkeypatch the transport."""
    payload = json.dumps({
        "model": model, "temperature": 0,
        "messages": [
            {"role": "system", "content": "You are a precise translation engine. "
                                          "Output only what is asked, no preamble."},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    req = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    return body["choices"][0]["message"]["content"]


def _extract_json_array(content):
    """Best-effort: pull a JSON string-array out of an LLM response (tolerates ``` fences)."""
    if not content:
        return None
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[A-Za-z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    a, b = s.find("["), s.rfind("]")
    if a == -1 or b == -1 or b <= a:
        return None
    try:
        return json.loads(s[a:b + 1])
    except Exception:
        return None


class _TranslateError(Exception):
    """Internal: a translation attempt failed. RAISED (not returned) so functools.lru_cache
    does NOT memoize the failure — a transient gateway/key error must retry, not poison the
    string for the process lifetime (codex_review #247 P3)."""


@functools.lru_cache(maxsize=256)
def _translate_cached(lang, endpoint, model, texts_tuple):
    """Cached batched translation → tuple of translations. RAISES on ANY failure (lru_cache
    does not cache exceptions, so failures retry; only successes are memoized). The key AND
    timeout are read FRESH here, so a cache MISS is always a fresh-key real call; hits skip the
    call entirely. The result depends only on (lang, endpoint, model, texts) — the key and the
    timeout are transport params, not translation determinants, so both are correctly absent
    from the cache key (key-independent result; timeout change must not force a re-call)."""
    key = _translate_key()
    if not key:
        raise _TranslateError("no key")
    prompt = ("Translate each string in this JSON array to " + lang + ". "
              "Keep code identifiers, file paths, shell commands, URLs and flags EXACTLY "
              "as-is (do not translate or alter them). Return ONLY a JSON array of strings, "
              "same length and order, nothing else.\n\n"
              + json.dumps(list(texts_tuple), ensure_ascii=False))
    content = _translate_http(endpoint, model, key, prompt, _translate_timeout())  # may raise → not cached
    arr = _extract_json_array(content)
    if (not isinstance(arr, list) or len(arr) != len(texts_tuple)
            or not all(isinstance(x, str) and x.strip() for x in arr)):
        raise _TranslateError("malformed / count-mismatched translation output")
    return tuple(arr)


# ── literal masking — protect paths/commands/identifiers from MT mangling ──
# Empirically a translator MANGLES file paths and shell commands ("check"->"keck",
# "git apply patch.diff"->"git applect.diff"). Swap each literal for a [N] placeholder
# (proven to survive OPUS-MT 3/3 and Google), translate only the prose, restore the
# literals verbatim. Side benefits: redacts the literals from any REMOTE egress (the
# translator sees only the prose skeleton), and raises the cache hit-rate (unique
# paths/UUIDs collapse to the same [N], so two narratives differing only in a path
# share a cache entry yet each restores its own path).
_MASK_SPAN_RE = re.compile(
    r"(`[^`]+`"                # backtick code span
    r"|[\w.+-]+/[\w./+-]+"     # RELATIVE path incl. leading segment (mcp/codex_server.py, src/a.py)
    r"|~?/[^\s\"'`),;]+"       # absolute / home path (/tmp/x, ~/foo)
    r"|\"[^\"]+\""             # double-quoted literal
    r"|(?<![A-Za-z])'[^']+'"   # single-quoted literal; lookbehind skips apostrophes (don't, user's)
    r"|\b\w+(?:[._]\w+)+\b)"   # dotted/snake identifier (test_parse_ledger, foo.bar)
)


def _mask_literals(text):
    """Return (masked_text, pairs): each protected literal replaced by a [N] placeholder.

    pairs is a list of (placeholder_token, original) — pass both to _unmask_literals.
    Placeholder numbering starts ABOVE any [d] already present in `text` so a generated
    placeholder can never collide with the input's own bracketed tokens (list indices,
    citations, foo[1].txt) — without that, _unmask's replace would corrupt both (#review-P2)."""
    existing = [int(m) for m in re.findall(r"\[(\d+)\]", text)]
    base = (max(existing) + 1) if existing else 0
    pairs = []

    def _repl(m):
        tok = f"[{base + len(pairs)}]"
        pairs.append((tok, m.group(0)))
        return tok

    return _MASK_SPAN_RE.sub(_repl, text), pairs


def _unmask_literals(text, pairs):
    """Restore generated [N] placeholders with their original literals (verbatim). Only the
    tokens THIS masking generated are replaced — the input's own [d] tokens are untouched."""
    for tok, original in pairs:
        text = text.replace(tok, original)
    return text


def _placeholders_intact(translated, pairs):
    """True iff `translated` is a string in which every generated placeholder token appears
    EXACTLY once. A translator that DROPPED a [N] would silently lose the literal (an approval
    dialog missing its path/command), one that DUPLICATED it would repeat the literal, and a
    NON-STRING element (buggy provider) must not crash the validation — all are rejected so the
    dispatcher falls through to the next provider / English."""
    if not isinstance(translated, str):
        return False
    return all(translated.count(tok) == 1 for tok, _ in pairs)


# ── google provider — keyless unofficial client=gtx endpoint ──────────────────
# No API key, zero new deps (stdlib urllib). Per-string, 1 retry on a transient
# failure, hard per-call timeout (the dialog is synchronous — a human waits). Best
# prose quality of the lightweight options; the masking layer above keeps paths/
# commands LOCAL so only the prose skeleton egresses. UNOFFICIAL: violates Google's
# robots.txt/ToS and can be rate-limited (429) on bursts — fine for the tiny opt-in
# volume here; failure falls through to the next provider, ultimately English.
_GOOGLE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
_GOOGLE_TIMEOUT = 1.5     # hard per-call ceiling (s)
_GOOGLE_BACKOFF = 0.2     # short pause before the single retry (s)


def _google_http(text, lang, timeout):
    """Translate ONE string via the keyless Google client=gtx endpoint. Raises on error.

    Isolated so tests can monkeypatch the transport."""
    q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": lang, "dt": "t", "q": text})
    req = urllib.request.Request(_GOOGLE_ENDPOINT + "?" + q,
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    # data[0] is a list of [translated_segment, original_segment, ...] chunks
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


def _translate_google(texts, lang):
    """Translate each string via the keyless Google endpoint (per-string, 1 retry).

    Returns a list (same length/order) on success, or None on persistent failure so
    the dispatcher falls through to the next provider. No API key needed."""
    out = []
    for t in texts:
        got = None
        for attempt in range(2):  # initial try + one retry
            try:
                got = _google_http(t, lang, _GOOGLE_TIMEOUT)
                break
            except Exception:
                if attempt == 0:
                    time.sleep(_GOOGLE_BACKOFF)
                else:
                    return None
        if not got or not got.strip():
            return None
        out.append(got)
    return out


# ── opus provider — offline OPUS-MT via CTranslate2 (lazy, optional) ───────────
# Privacy-first ON-DEVICE fallback: ZERO egress, ~70-80MB int8 model, runtime needs
# only `ctranslate2` + `sentencepiece` (NO torch/transformers). OPT-IN: the plugin
# does NOT install these deps or ship the model — set BULLDOZER_OPUS_MODEL_DIR to a
# CTranslate2 OPUS-MT model dir (e.g. a pre-converted ordois/opus-mt-en-ru-ctranslate2-int8)
# containing model.bin + source.spm (+ target.spm). Unavailable -> returns None and the
# dispatcher falls through. Prose is rougher than Google/LLM, but the masking layer keeps
# every path/command verbatim — which is exactly what an approval dialog requires.
_OPUS_ENGINE = None  # cached (Translator, sp_source, sp_target) once loaded


def _opus_model_dir():
    return (os.environ.get("BULLDOZER_OPUS_MODEL_DIR") or "").strip()


def _opus_available():
    """True iff the model dir holds a loadable CT2 model (model.bin) AND ctranslate2 +
    sentencepiece import. The model.bin check rejects a raw HF snapshot that still holds
    only a .zip — so 'available' means the engine can actually load, not just that a dir
    and the deps exist (a missing model.bin would otherwise fail-open silently)."""
    d = _opus_model_dir()
    if not d or not os.path.isfile(os.path.join(d, "model.bin")):
        return False
    import importlib.util
    return all(importlib.util.find_spec(m) for m in ("ctranslate2", "sentencepiece"))


def _opus_load(d):
    """Load (Translator, sp_source, sp_target) from CT2 model dir `d`. Raises on failure.
    Isolated so tests can monkeypatch the heavy ctranslate2/sentencepiece load."""
    import ctranslate2
    import sentencepiece as spm
    tr = ctranslate2.Translator(d, device="cpu")
    src = spm.SentencePieceProcessor(model_file=os.path.join(d, "source.spm"))
    tgt_path = os.path.join(d, "target.spm")
    tgt = (spm.SentencePieceProcessor(model_file=tgt_path)
           if os.path.isfile(tgt_path) else src)
    return (tr, src, tgt)


def _opus_engine():
    """Lazily load + cache (Translator, sp_source, sp_target), KEYED on the model dir so a
    mid-process BULLDOZER_OPUS_MODEL_DIR change reloads instead of returning a stale engine
    (module-singleton + context-param = stale-data trap). Raises on failure."""
    global _OPUS_ENGINE
    d = _opus_model_dir()
    if _OPUS_ENGINE is None or _OPUS_ENGINE[0] != d:
        _OPUS_ENGINE = (d,) + tuple(_opus_load(d))
    return _OPUS_ENGINE[1], _OPUS_ENGINE[2], _OPUS_ENGINE[3]


def _opus_translate_one(text, lang):
    """Translate ONE string offline via OPUS-MT/CTranslate2. Raises on error.

    `lang` is accepted for signature parity; the model dir IS the target language
    (OPUS-MT models are language-pair-specific)."""
    tr, src, tgt = _opus_engine()
    # Marian/OPUS REQUIRES the </s> end token on the source — without it the decoder
    # never terminates cleanly and degenerates into a repetition loop that also mangles
    # the [N] placeholders (verified empirically against ordois/opus-mt-en-ru CT2).
    tokens = src.encode(text, out_type=str) + ["</s>"]
    res = tr.translate_batch([tokens])
    return tgt.decode(res[0].hypotheses[0])


def _translate_opus(texts, lang):
    """Offline OPUS-MT translation. Returns a list (same length/order), or None if
    unavailable or on any failure (so the dispatcher falls through). Lazy/optional."""
    if not _opus_available():
        return None
    out = []
    for t in texts:
        try:
            got = _opus_translate_one(t, lang)
        except Exception:
            return None
        if not got or not got.strip():
            return None
        out.append(got)
    return out


def _translate_openai(texts, lang):
    """LLM / OpenAI-compatible provider (the original #247 path; also drives a local
    Ollama endpoint). Returns a list (same length/order), or None on no-key / failure
    so the dispatcher falls through. Batched JSON + lru-cached (successes only)."""
    if not _translate_key():
        return None
    try:
        out = _translate_cached(lang, _translate_endpoint(), _translate_model(), tuple(texts))
    except Exception as e:
        _drift_warn(None, "TRANSLATE_FAILED", f"openai: {type(e).__name__}: {e}")
        return None
    return list(out) if out and len(out) == len(texts) else None


# Provider registry — name -> function attribute name (resolved via globals() at call
# time, NOT captured function objects, so monkeypatch/hot-swap of a provider takes effect).
# The dispatcher tries the configured chain in order; each provider returns a list
# (success) or None (unavailable/failed -> try the next). Failures are NEVER cached —
# each provider's own cache memoizes successes only.
_PROVIDERS = {
    "openai": "_translate_openai",
    "google": "_translate_google",
    "opus": "_translate_opus",
}


def _translate_providers():
    """Parse BULLDOZER_TRANSLATE_PROVIDER into an ordered chain. Unset -> ['openai']
    (#247 back-compat); 'off' -> [] (disabled); else a comma-list, e.g. 'google,opus'
    (Google primary, OPUS-MT on-device fallback)."""
    raw = (os.environ.get("BULLDOZER_TRANSLATE_PROVIDER") or "").strip().lower()
    if not raw:
        return ["openai"]
    if raw == "off":
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _translate_texts(texts, lang):
    """Translate each string to `lang` (opt-in, fail-open).

    Pipeline: MASK literals (paths/commands/identifiers -> [N] placeholders) -> try the
    configured provider chain in order -> RESTORE the literals verbatim. lang-off,
    provider 'off', or ALL providers failing -> originals (English). None/blank entries
    pass through untouched (kept in position). Masking keeps every literal byte-exact AND
    out of any remote provider's egress, and raises the cache hit-rate (paths -> [N])."""
    texts = list(texts)
    if not lang:
        return texts
    providers = _translate_providers()
    if not providers:
        return texts
    idxs = [i for i, t in enumerate(texts) if t and str(t).strip()]
    if not idxs:
        return texts
    masked, spans = [], []
    for i in idxs:
        m, sp = _mask_literals(str(texts[i])[:2000])  # bound input size
        masked.append(m)
        spans.append(sp)
    out = None
    for name in providers:
        fn = globals().get(_PROVIDERS.get(name, ""))  # resolve fresh (monkeypatch-friendly)
        if fn is None:
            continue
        try:
            res = fn(masked, lang)
        except Exception as e:
            _drift_warn(None, "TRANSLATE_FAILED", f"{name}: {type(e).__name__}: {e}")
            res = None
        if (isinstance(res, list) and len(res) == len(masked)
                and all(isinstance(x, str) for x in res)
                and all(_placeholders_intact(res[k], spans[k]) for k in range(len(masked)))):
            out = res
            break
        # any malformed shape (non-list, wrong length, non-string element) or a dropped/
        # duplicated placeholder is rejected → try the next provider, ultimately English
        # (never accept a corrupted literal, never crash the dialog on a buggy provider).
    if out is None:
        return texts
    for j, i in enumerate(idxs):
        texts[i] = _unmask_literals(out[j], spans[j])
    return texts


def _approval_narrative_max() -> int:
    """Max chars for codex's narrative in an approval dialog / park payload (#224). codex
    routinely writes pre-tool-call explanations longer than the old hardcoded 500, so the
    default is raised to 2000 and made tunable via BULLDOZER_APPROVAL_NARRATIVE_MAX. Clamped
    [200, 8000]; the park's _bound_evidence total-budget guard remains the hard backstop."""
    try:
        v = int(os.environ.get("BULLDOZER_APPROVAL_NARRATIVE_MAX") or "2000")
    except (TypeError, ValueError):
        return 2000
    return max(200, min(v, 8000))


def _narrative_line(narrative, label="Codex explained") -> str:
    """Format codex's pre-approval agentMessage narrative for the dialog (#224), or ''.

    This is the 'codex explains what it's about to do' content — streamed as
    agentMessage deltas just before the approval, NOT part of the approval request
    protocol-wise. Context only, never authoritative for the action. `narrative` is
    already translated (if a language is configured) by the caller; `label` is localized.
    Cap is _approval_narrative_max() chars / 12 lines (raised from 6/500 — codex narratives
    are often larger; tune via BULLDOZER_APPROVAL_NARRATIVE_MAX)."""
    if not narrative:
        return ""
    text = _truncate_for_display(str(narrative).strip(), max_lines=12, max_chars=_approval_narrative_max())
    return f"{label}: {text}" if text else ""


def _build_command_approval_message(params: dict, narrative=None) -> str:
    """Compose the commandExecution approval dialog (#239 truncation + #224 context).

    Order: codex narrative (intent) LEADS — read what codex means to do before the raw,
    often-truncated command — then authoritative command + cwd, then optional
    context (reason / friendly actions / network host). EVERY
    variable-length field is individually bounded so a huge reason / actions / cwd
    can't reintroduce the #239 wall in spite of command truncation. When a language is
    configured (#247) the DYNAMIC content (reason + narrative) is translated and the
    labels localized — the command/cwd/actions/host are NEVER translated (authoritative).
    """
    lang = _approval_lang()
    L = _dialog_labels(lang)
    reason_t, narr_t = params.get("reason"), narrative
    if lang:
        reason_t, narr_t = _translate_texts([params.get("reason"), narrative], lang)
    lines = [L["approval_request"]]
    nar = _narrative_line(narr_t, L["explained"])
    if nar:                          # codex's intent LEADS — before the raw, often-truncated command
        lines.append(nar)
    lines += [
        # head+tail: keep the executable head AND tail of a long command visible.
        f"{L['command']}: {_truncate_for_display(params.get('command') or '(none)', tail_lines=4)}",
        f"{L['cwd']}: {_truncate_for_display(str(params.get('cwd') or '(unknown)'), max_lines=1, max_chars=200)}",
    ]
    if reason_t:
        lines.append(f"{L['reason']}: {_truncate_for_display(str(reason_t), max_lines=3, max_chars=300)}")
    actions = _summarize_command_actions(params.get("commandActions"))
    if actions:
        lines.append(f"{L['actions']}: {_truncate_for_display(actions, max_lines=2, max_chars=300)}")
    nac = params.get("networkApprovalContext")
    if isinstance(nac, dict) and nac.get("host"):
        proto = nac.get("protocol") or "network"
        host = _truncate_for_display(str(nac["host"]), max_lines=1, max_chars=100)
        lines.append(L["network"].format(proto=proto, host=host))
    return "\n".join(lines)


def _summarize_permissions(perms) -> str:
    """Bounded, human-readable summary of a RequestPermissionProfile
    ({fileSystem?, network?}) for the approval dialog, so the user SEES what an accept
    will grant (#4 / codex_review P1 — pre-#4 an accept granted {}, so the payload was
    never shown). Authoritative (real paths/hosts) → never translated. Returns '' for an
    empty/non-dict profile (no detail line). Defensive against malformed shapes (the
    bridge is fail-open everywhere on the approval path)."""
    if not isinstance(perms, dict) or not perms:
        return ""
    fs_part = ""
    fs = perms.get("fileSystem")
    if isinstance(fs, dict):
        items = []
        entries = fs.get("entries")
        if isinstance(entries, list):
            for e in entries:
                if not isinstance(e, dict):
                    continue
                access = e.get("access", "?")
                path = e.get("path")
                if isinstance(path, dict):
                    val = path.get("value")
                    p = (path.get("path") or path.get("pattern")
                         or (val.get("kind") if isinstance(val, dict) else None) or "?")
                else:
                    p = str(path) if path is not None else "?"
                items.append("{}:{}".format(access, p))
        for k in ("read", "write"):  # legacy path-list form
            v = fs.get(k)
            if isinstance(v, list):
                items += ["{}:{}".format(k, x) for x in v]
        if len(items) > _PAYLOAD_MAX_ITEMS:           # R1-F6: cap the joined list (a 1000-entry profile
            extra = len(items) - _PAYLOAD_MAX_ITEMS   # otherwise produced a ~15KB summary string)
            items = items[:_PAYLOAD_MAX_ITEMS] + ["(+{} more)".format(extra)]
        if items:
            fs_part = "fileSystem " + ", ".join(items)
    net_part = ""
    net = perms.get("network")
    if isinstance(net, dict) and net:
        if net.get("enabled"):
            net_part = "network: enabled"
        else:
            parts = []
            for k, v in net.items():                  # R1-F6: bound list values (e.g. a 2000-host list)
                if isinstance(v, list):
                    shown = [str(x) for x in v[:_PAYLOAD_MAX_ITEMS]]
                    if len(v) > _PAYLOAD_MAX_ITEMS:
                        shown.append("+{} more".format(len(v) - _PAYLOAD_MAX_ITEMS))
                    vs = "[" + ", ".join(shown) + "]"
                else:
                    vs = str(v)
                parts.append("{}={}".format(k, vs))
            net_part = "network: " + ", ".join(parts)
    # network FIRST, one part per line (review B): the security-sensitive egress grant stays
    # visible even when a large fileSystem list is truncated head-only — and a newline-joined
    # summary lets the caller's head+tail char cap keep BOTH ends instead of dropping the tail.
    return "\n".join(p for p in (net_part, fs_part) if p)


def _build_simple_approval_message(kind: str, reason, narrative=None, details=None) -> str:
    """fileChange / permissions dialog: localized header + bounded reason + optional
    narrative (#224). `kind` is a label key ('filechange'/'permissions'). When a language
    is configured (#247), reason + narrative are translated and the header localized.

    `details` (optional) is an authoritative payload summary rendered right after the
    header and NEVER translated — used by the permissions path to surface the requested
    fileSystem/network profile the user is about to grant (#4 / codex_review P1). When
    None (fileChange) the message shape is byte-identical to before.

    Explicit append (not filter(None)) so a falsy line never silently changes the
    message structure; reason is bounded like the command's context fields.
    """
    lang = _approval_lang()
    L = _dialog_labels(lang)
    reason_t, narr_t = reason, narrative
    if lang:
        reason_t, narr_t = _translate_texts([reason, narrative], lang)
    # fail-safe header lookup: an unexpected `kind` must degrade, never KeyError into the
    # approval path (every other path here is fail-open).
    header = L.get(kind) or _DIALOG_LABELS_EN.get(kind) or kind
    lines = [header]
    nar = _narrative_line(narr_t, L["explained"])
    if nar:                          # narrative LEADS — after the header, before details/reason
        lines.append(nar)
    if details:  # authoritative (paths/hosts) — bounded, never translated; tail_lines keeps
        # BOTH ends so the security-sensitive network grant (rendered first) AND the fileSystem
        # tail survive truncation of a large profile (review B).
        lines.append(_truncate_for_display(str(details), max_lines=6, max_chars=400, tail_lines=2))
    lines.append(
        f"{L['reason']}: {_truncate_for_display(str(reason_t) if reason_t else '(none)', max_lines=3, max_chars=300)}",
    )
    return "\n".join(lines)


def _approval_decision_label(decision) -> str:
    """Coarse, greppable label for a bridge_approval decision (str or dict)."""
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict):
        # command amendment-accepts (build_command_approval_labels dict variants) ARE accepts —
        # log the kind, not the generic 'other' (#251 mining needs it; codex_review P2).
        if "acceptWithExecpolicyAmendment" in decision:
            return "accept:execpolicy"
        if "applyNetworkPolicyAmendment" in decision:
            return "accept:network"
        for k in ("decision", "action"):  # legacy ReviewDecision / elicitation passthrough
            if k in decision:
                return str(decision[k])
        if "answers" in decision:         # tool input
            return "input"
        if "scope" in decision:           # permissions grant / PERM_DECLINE (value-identical)
            return "perm:" + str(decision.get("scope"))
    return "other"


def _log_approval_event(method, decision, wait_ms, timed_out, unattended=False, rule=None,
                        ui=None) -> None:
    """Best-effort one-line record of a completed approval (#251 step-0).

    Reuses the stable codex log channel (BULLDOZER_CODEX_LOG / bulldozer-codex.log).
    NEVER raises — logging must never break an approval. When the unattended judge (#251)
    decided in-process, appends `| unattended=true | rule=<verdict>` so the operator can
    review what was auto-decided while away (attended lines keep the original format).
    #340: when the native dialog answered, appends `| ui=dialog` (additive; cc-mode lines
    stay byte-compatible with the pre-#340 format).
    """
    try:
        # single shared writer (#322 PR2 → #334 canonical): sanitation lives in
        # the helper, redaction in _drift_warn; conditional fields keep the
        # pre-#334 semantics (absent unless unattended / non-cc UI).
        kw = dict(method=method,
                  decision=_approval_decision_label(decision),
                  wait_ms=wait_ms,
                  timed_out='true' if timed_out else 'false')
        if unattended:
            kw["unattended"] = 'true'
            kw["rule"] = rule
        if ui and ui != "cc":
            kw["ui"] = ui
        _drift_warn(None, "APPROVAL", **kw)
    except Exception:
        pass


def _log_unattended_decision(method, decision, rule) -> None:
    """#280 C: best-effort APPROVAL audit line for an AUTOMATED (non-attended) decision — inline
    fast_accept / fail_closed_decline, the model-resumed parked decision, and _teardown_park's
    auto-decline. Before #280 only attended bridge_approval logged, so unattended runs left NO audit
    trail for exactly the decisions an operator most wants to review. wait_ms=0 (no human wait);
    unattended=True + the rule that fired. _log_approval_event is itself never-raising."""
    _log_approval_event(method, decision, 0, False, unattended=True, rule=rule)


# ── #251: unattended approval judge ──────────────────────────────────────────
# When the operator ARMS unattended mode (env or sentinel file), a deterministic
# capability-judge answers approvals in-process — accepting routine in-sandbox work,
# gating escalations (permissions / network / catastrophic-destructive). Default OFF →
# today's human dialog. Empirical basis + posture rationale:
#   docs/superpowers/specs/2026-06-24-codex-unattended-approval-policy-design.md
# The safety FLOOR is codex's own sandbox + this escalation gate, NOT the string denylist
# (which is target-anchored defense-in-depth). 27,873 real codex commands studied: ~90%+
# reads, 0 real destructive escalations (every loose-regex "destructive" hit was an FP —
# grep-for-pattern, trap-cleanup-of-own-mktemp, in-project __pycache__). The classifier is
# tuned to reproduce that: those three shapes ACCEPT; real catastrophes DECLINE.

_UNATTENDED_SENTINEL_DEFAULT = "~/.claude/bulldozer-unattended"
_TRUTHY = {"1", "true", "yes", "on"}


def _unattended_active() -> bool:
    """True iff unattended approval mode is armed — env truthy OR sentinel file present.
    Resolved FRESH per approval so arming/disarming mid-run takes effect immediately."""
    if os.environ.get("BULLDOZER_APPROVAL_UNATTENDED", "").strip().lower() in _TRUTHY:
        return True
    sentinel = os.environ.get("BULLDOZER_APPROVAL_UNATTENDED_FILE") or os.path.expanduser(
        _UNATTENDED_SENTINEL_DEFAULT)
    try:
        return bool(sentinel) and os.path.exists(sentinel)
    except Exception:
        return False


# ── #340: approval UI — CC TUI elicitation (default) vs native macOS dialog ────────────
# Engine borrowed from the proven guards confirm-dialog (plugins/guards/hooks/
# guard-confirm-dialog.sh): text is never spliced into the script (no AppleScript injection) —
# the BODY/TITLE travel in the child's ENV (read via `system attribute`), since argv would
# publish a credential-bearing approval in the process table (round-6 P2); argv carries only
# non-secret chrome (give-up integer, button names, display labels). Allow = default
# button (Enter-activated — the guards #311 tradeoff: a stray Enter allows, silence still
# fail-safe declines), timeout → decline, Basso beeper until answered. macOS caps a dialog
# at 3 buttons, so the FULL label set (amendments / "Cancel the turn") lives behind
# "Опции…" (choose from list). Applies ONLY to the label-enum arms of
# _bridge_approval_dispatch; requestUserInput / mcpServer elicitation stay on the CC path
# (arbitrary schemas can't be rendered as buttons).

_APPROVAL_DIALOG_SENTINEL_DEFAULT = "~/.claude/bulldozer-approval-dialog"
# Machine-global sentinel (Chris, 2026-07-13): vault-level, NOT $HOME-bound — one touch flips
# dialog mode for EVERY user/config/session on the box (the user-level sentinel above only
# covers its own $HOME). On machines without the /0 vault the path simply never exists —
# bulldozer's home turf has it. Test override: BULLDOZER_APPROVAL_DIALOG_MACHINE_FILE.
_APPROVAL_DIALOG_MACHINE_SENTINEL_DEFAULT = "/0/.jaine/bulldozer-approval-dialog"
_DIALOG_TITLE = "🤖 Codex approval"
_BTN_OPTIONS = "Опции…"
_DIALOG_UNAVAILABLE = object()   # sentinel: dialog could not be SHOWN → fall back to CC

# Buttons + the default button are passed IN (argv), because "Allow" must NOT be offered when
# codex's availableDecisions omit plain `accept` (amendment-only shape): a bare Allow there would
# synthesize an UNOFFERED decision (codex round-2 P1). `cancel button "Deny"` makes Esc dismiss
# the dialog at all: AppleScript fires Escape ONLY when some button is designated the cancel
# button (verified live 2026-07-13 — without it Esc was inert and the user had to click Deny).
# Esc AND clicking Deny both raise -128 → _osascript_stage returns 'esc' → decline.
_ENV_BODY = "BULLDOZER_DIALOG_BODY"      # the approval text — may carry a literal credential
_ENV_TITLE = "BULLDOZER_DIALOG_TITLE"

# The BODY travels in the child's ENVIRONMENT, never in argv: an approval message can quote a
# codex command that contains a token (`curl -H "Authorization: Bearer …"`), and argv is world-
# readable in the process table for the dialog's whole lifetime, while macOS does not expose one
# uid's environment to another (codex round-6 P2). argv keeps only non-secret chrome: the give-up
# integer, the button names, and (stage 2) the display labels — LBL_* constants, or a host/kind
# from codex's own amendment offer; none is a credential.
_DIALOG_STAGE1 = '''on run argv
    set bodyText to system attribute "BULLDOZER_DIALOG_BODY"
    set titleText to system attribute "BULLDOZER_DIALOG_TITLE"
    set giveUp to (item 1 of argv) as integer
    set defBtn to item 2 of argv
    set btns to items 3 thru -1 of argv
    set r to display dialog bodyText with title titleText buttons btns default button defBtn cancel button "Deny" with icon caution giving up after giveUp
    if gave up of r then
        return "GAVEUP"
    end if
    return button returned of r
end run'''

_DIALOG_STAGE2 = '''on run argv
    set bodyText to system attribute "BULLDOZER_DIALOG_BODY"
    set titleText to system attribute "BULLDOZER_DIALOG_TITLE"
    set opts to items 1 thru -1 of argv
    set pick to choose from list opts with title titleText with prompt bodyText default items {item 1 of opts}
    if pick is false then
        return "CANCELLED"
    end if
    return item 1 of pick
end run'''


def _approval_ui() -> str:
    """'dialog' | 'cc' — which UI answers ATTENDED approvals (#340). env BULLDOZER_APPROVAL_UI
    wins both ways ('dialog' arms it, explicit 'cc'/'tui' disarms — the test-suite hermeticity
    hook); otherwise EITHER sentinel file toggles dialog mode LIVE (touch/rm — the server env
    is fixed at spawn, the files are not; mirrors the #277 unattended sentinel): the user-level
    one (BULLDOZER_APPROVAL_DIALOG_FILE, default ~/.claude/bulldozer-approval-dialog) or the
    machine-global vault one (BULLDOZER_APPROVAL_DIALOG_MACHINE_FILE, default
    /0/.jaine/bulldozer-approval-dialog — one touch covers every user/config/session on the
    box). Resolved FRESH per approval. Default 'cc' → the pre-#340 path, byte-identical."""
    env = (os.environ.get("BULLDOZER_APPROVAL_UI") or "").strip().lower()
    if env == "dialog":
        return "dialog"
    if env in ("cc", "tui"):
        return "cc"
    sentinel = os.environ.get("BULLDOZER_APPROVAL_DIALOG_FILE") or os.path.expanduser(
        _APPROVAL_DIALOG_SENTINEL_DEFAULT)
    machine = (os.environ.get("BULLDOZER_APPROVAL_DIALOG_MACHINE_FILE")
               or _APPROVAL_DIALOG_MACHINE_SENTINEL_DEFAULT)
    try:
        if (sentinel and os.path.exists(sentinel)) or (machine and os.path.exists(machine)):
            return "dialog"
    except Exception:
        pass
    return "cc"


def _warn_stderr(msg: str) -> None:
    """Best-effort stderr diagnostic. A write failure must never break the caller — stderr can
    be closed/broken (line-buffered → the newline flushes → EPIPE), and the #340 no-GUI fallback
    warns BEFORE sending the CC elicitation, so a raising warn would leave codex's approval
    unanswered (codex P2, reproduced). Swap in devnull after a failure: a CAUGHT EPIPE leaves the
    buffer dirty and the interpreter's SHUTDOWN flush re-raises it (the PR #339 lesson)."""
    try:
        print(msg, file=sys.stderr)
        sys.stderr.flush()
    except (OSError, ValueError):
        try:
            sys.stderr = open(os.devnull, "w")
        except OSError:
            pass


# The beeper is a DETACHED shell: if the server is SIGKILLed while a dialog is up,
# `_dialog_label_elicit`'s finally never runs (codex P2, reproduced — the loop beeped on after
# the parent died). So the loop is self-limiting on BOTH axes: it exits when its parent is gone
# (`kill -0 $PPID` each cycle — PPID is captured before the parent can die) and it is hard-bounded
# by an iteration cap. `start_new_session` puts it in its OWN process group so _stop_beeper can
# kill the shell AND its in-flight afplay child together (terminate() alone would leave afplay).
_BEEP_MAX_CYCLES = 1200                     # ~20 min at ~1s/cycle — a backstop, not the mechanism

_BEEPER_SH = f"""
parent=$PPID
i=0
while [ "$i" -lt {_BEEP_MAX_CYCLES} ]; do
    afplay /System/Library/Sounds/Basso.aiff 2>/dev/null
    sleep 0.5
    kill -0 "$parent" 2>/dev/null || exit 0
    i=$((i+1))
done
"""


def _start_beeper():
    """Guards-style attention beeper (Basso loop) while the dialog is up. Best-effort: a
    missing afplay / spawn failure must never block an approval."""
    try:
        return subprocess.Popen(
            ["/bin/sh", "-c", _BEEPER_SH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception:
        return None


def _stop_beeper(proc):
    """Kill the beeper's whole process GROUP (the shell + any in-flight afplay)."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def _osascript_stage(script, argv, cap_s, pump_fn=None, wait_state=None, env_payload=None):
    """Run ONE osascript dialog stage NON-blockingly: Popen + poll loop that keeps the
    approval-wait bookkeeping alive via pump_fn (#252 child drain, #269 CC-frame answering,
    EOF/our-cancel/terminal detection — the pump sets the ts flags itself). Returns
    (status, stdout_line):
      'ok'          — the script returned a line (button name / list pick);
      'timeout'     — cap expired or the dialog gave up (wait_state['timed_out'] set);
      'esc'         — the user dismissed the dialog (AppleScript -128) — an ANSWER;
      'aborted'     — the pump saw eof|cancel|terminal (dialog torn down);
      'unavailable' — no osascript / no GUI: the dialog could not be shown at all.
    Text reaches AppleScript via argv / env_payload, never spliced into the script (guards engine
    rule — $(…)/backticks/quotes inside a codex command cannot execute or break the script).
    env_payload (the approval BODY) is merged into the child's environment instead of argv, which
    would publish it in the process table (codex round-6 P2)."""
    child_env = None
    if env_payload:
        child_env = dict(os.environ)
        child_env.update({k: str(v) for k, v in env_payload.items()})
    try:
        proc = subprocess.Popen(["osascript", "-", *[str(a) for a in argv]],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=child_env)
        proc.stdin.write(script)
        proc.stdin.close()
    except (OSError, ValueError):
        return ("unavailable", "")
    try:
        deadline = time.time() + cap_s
        while proc.poll() is None:
            if pump_fn is not None:
                if pump_fn() in ("eof", "cancel", "terminal"):
                    proc.terminate()
                    return ("aborted", "")
            else:
                time.sleep(0.05)          # no pump (no drain ctx) — just pace the poll
            if time.time() >= deadline:   # hard backstop: kills the dialog too
                proc.terminate()
                if wait_state is not None:
                    wait_state["timed_out"] = True
                return ("timeout", "")
        out = (proc.stdout.read() or "").strip()
        err = proc.stderr.read() or ""
        # The loop pumps only WHILE the dialog is alive, so a CC EOF / our-turn cancel / terminal
        # child frame landing in the instant the user clicks would otherwise go unseen and the
        # decision would still be written to a dead bridge — the EOF-priority invariant (#218)
        # must win over a dialog answer. One final non-blocking check (codex round-5 P2).
        if pump_fn is not None and pump_fn() in ("eof", "cancel", "terminal"):
            return ("aborted", "")
        # …and an answer landing PAST the deadline is late, not valid: the loop exits on poll()
        # without ever running its timeout branch when the process ends just after the last check
        # (and stage 2 has no native `giving up after` at all). Fail-safe (codex round-7 P2).
        if time.time() >= deadline:
            if wait_state is not None:
                wait_state["timed_out"] = True
            return ("timeout", "")
    finally:
        try:
            proc.kill()                   # no-op if already exited
        except Exception:
            pass
    if proc.returncode != 0:
        # -128 = "User canceled" (Esc/Cancel) — an answer, not an outage.
        return ("esc", "") if "-128" in err else ("unavailable", "")
    if out == "GAVEUP":
        if wait_state is not None:
            wait_state["timed_out"] = True
        return ("timeout", "")
    return ("ok", out)


def _dialog_label_elicit(message, labels, deadline, allow_ok, pump_fn=None, wait_state=None):
    """#340: native macOS approval dialog. Stage 1: Deny / Опции… [/ Allow] (Allow default,
    Enter-activated). `allow_ok` — decided by the ARM from its label→decision MAP — says whether a
    bare Allow really resolves to plain accept; when it does not (an amendment-only
    `availableDecisions`, or a drifted decision string that collides with a display label and makes
    dedupe rename the real accept — round-2 P1 / round-4 P3) the Allow button is DROPPED and Опции…
    becomes the default, because synthesizing a decision codex never offered would be an invalid
    protocol reply. Stage 2
    ("Опции…"): choose-from-list with the FULL label set, so amendments and "Cancel the turn"
    stay reachable.

    `deadline` is the approval's ABSOLUTE deadline, owned by the caller and SHARED with the
    CC-fallback wait — so the whole approval (both stages + any fallback) is bounded by ONE budget
    (a stage that re-received the full timeout could double the wall-clock and blow the
    bridge/client deadline — codex round-2 P2 / round-3 P2). A stage is never STARTED with an
    exhausted budget (it could otherwise accept AFTER the deadline — round-3 P2): that declines,
    audited as timed_out. Esc/Deny = decline (an answer); a stage-2 pick outside the label set =
    decline (fail-safe, mirrors the perm arm's fail-closed posture). Returns a synthetic
    elicitation-response frame ({'result': {action, content}}), None (decisive no-answer → the
    arm's safe decline), or _DIALOG_UNAVAILABLE from EITHER stage (no dialog shown / GUI vanished
    mid-flow → caller falls back to CC elicitation rather than rejecting the approval)."""
    def _remaining():
        return deadline - time.time()

    def _out_of_time():
        if wait_state is not None:
            wait_state["timed_out"] = True
        return None                        # → the arm's safe decline

    buttons = ["Deny", _BTN_OPTIONS] + (["Allow"] if allow_ok else [])
    default_btn = "Allow" if allow_ok else _BTN_OPTIONS

    rem = _remaining()
    if rem <= 0:
        return _out_of_time()
    # `giving up after` self-dismisses the dialog AT the deadline; the poll-loop cap is the
    # backstop for a dialog that ignores it. AppleScript needs a >= 1 s integer.
    give_up = max(1, min(int(rem), 3600))

    beeper = _start_beeper()
    try:
        env_payload = {_ENV_BODY: message, _ENV_TITLE: _DIALOG_TITLE}
        status, out = _osascript_stage(
            _DIALOG_STAGE1, [give_up, default_btn, *buttons],
            cap_s=rem, pump_fn=pump_fn, wait_state=wait_state, env_payload=env_payload)
        if status == "unavailable":
            return _DIALOG_UNAVAILABLE
        if status in ("timeout", "aborted"):
            return None
        if status == "esc" or out == "Deny":
            return {"result": {"action": "decline", "content": None}}
        if out == "Allow" and allow_ok:
            return {"result": {"action": "accept", "content": {}}}
        if out != _BTN_OPTIONS:
            return {"result": {"action": "decline", "content": None}}   # unexpected → fail-safe
        rem2 = _remaining()
        if rem2 <= 0:                      # stage 1 ate the whole budget → no stage 2 (r3 P2)
            return _out_of_time()
        status2, out2 = _osascript_stage(
            _DIALOG_STAGE2, [*labels],
            cap_s=rem2, pump_fn=pump_fn, wait_state=wait_state, env_payload=env_payload)
        if status2 == "unavailable":       # GUI vanished between the stages → CC, not a decline
            return _DIALOG_UNAVAILABLE
        if status2 in ("timeout", "aborted"):
            return None
        if status2 == "ok" and out2 in labels:
            return {"result": {"action": "accept", "content": {"label": out2}}}
        return {"result": {"action": "decline", "content": None}}   # CANCELLED / esc / junk
    finally:
        _stop_beeper(beeper)


# read/search tools that, as the LEADING verb, make destructive/network-looking ARGS harmless
# (`rg "rm -rf /"` / `grep "curl …"` are SEARCHING for the text, not running it — the study's
# dominant FP). Checked BEFORE the network/destructive scans by deliberate design.
# ── #277 ALLOW-LIST posture: DECLINE-by-default; ACCEPT only forms provably in these sets. ──
# Network / arbitrary-execution / destructive verbs need NO denylist — absent from the allow-list,
# they DECLINE by default (this is what converges; the denylist treadmill is gone).

# absolute-path roots that are catastrophic when not under the project
_SYS_ROOTS = ("/etc", "/usr", "/bin", "/sbin", "/var", "/System", "/Library",
              "/Users", "/opt", "/private", "/dev", "/boot", "/root", "/Applications")


def _safe_tokens(s: str) -> list:
    try:
        import shlex
        return shlex.split(s, posix=True)
    except Exception:
        return s.split()


def _is_catastrophic_target(s: str, project_root) -> bool:
    """A destructive verb's argument is catastrophic if it targets ~ / $HOME / a system root /
    bare / — or an absolute path OUTSIDE the project. Relative paths, $vars (mktemp temps), and
    in-project absolute paths are NOT catastrophic (dodges the study's FPs)."""
    pr = (project_root or "").rstrip("/")
    for t in _safe_tokens(s):
        if t in ("~", "/") or t.startswith("~/") or t.startswith("$HOME") or t.startswith("${HOME}"):
            return True
        if re.search(r'(^|/)\.\.(/|$)', t):    # `..` path traversal — may escape the project
            return True
        if t.startswith("/"):
            if pr and (t == pr or t.startswith(pr + "/")):
                continue                       # in-project absolute → safe
            root = "/" + t.lstrip("/").split("/", 1)[0]
            if t == "/" or root in _SYS_ROOTS:
                return True
            return True                        # any absolute path outside the project → gate
    return False


def _split_segments(cmd: str) -> list:
    """Split a command line on shell separators (`&&` `||` `;` `|` newline) that are NOT inside
    quotes. A naive re.split breaks on separators inside quoted args — `rg "a|b"`, `sh -c 'x && y'`
    — fragmenting the command into bogus 'verbs' that an allow-list then wrongly DECLINES."""
    segs, buf, i, n, q = [], [], 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if q:                                    # inside a quote — copy verbatim until it closes
            buf.append(c)
            if c == q:
                q = None
            i += 1
            continue
        if c in ("'", '"'):
            q = c; buf.append(c); i += 1; continue
        if c in (";", "\n"):
            segs.append("".join(buf)); buf = []; i += 1; continue
        if c == "&" and i + 1 < n and cmd[i + 1] == "&":
            segs.append("".join(buf)); buf = []; i += 2; continue
        if c == "|" and i + 1 < n and cmd[i + 1] == "|":
            segs.append("".join(buf)); buf = []; i += 2; continue
        if c == "|":
            segs.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    segs.append("".join(buf))
    return segs


def _all_read_actions(actions) -> bool:
    if not isinstance(actions, list) or not actions:
        return False
    return all(isinstance(a, dict) and a.get("type") in ("read", "listFiles", "search")
               for a in actions)


def _has_escalation_amendment(params: dict) -> bool:
    """codex structured escalation signals → gate (network host, proposed network/exec amendments)."""
    nac = params.get("networkApprovalContext")
    if isinstance(nac, dict) and nac.get("host"):
        return True
    if params.get("proposedNetworkPolicyAmendments"):
        return True
    if params.get("proposedExecpolicyAmendment"):
        return True
    for d in (params.get("availableDecisions") or []):
        # A dict decision is an amendment-bearing escalation OFFER (acceptWithExecpolicyAmendment,
        # applyNetworkPolicyAmendment, …) — codex only offers it when the command needs to escalate.
        if isinstance(d, dict) and any("Amendment" in k or k.startswith("acceptWith") for k in d):
            return True
    return False


# ── #277 model-in-the-loop routing: the 3-way fast-path that REPLACES the classify_approval verdict ──
# route_approval / _is_trivially_safe / _fast_path_scope are the SURVIVING approval logic; the
# classify_approval verdict + the whack-a-mole gates below it are deleted at switchover (Task 7). The
# fast-path is deliberately NARROW: anything not provably trivial → "park_for_model" (the model decides).
# It NEVER parses git subcommands / wrappers / rm-targets to safely-accept (that treadmill is the
# non-convergent trap the design rejects — parking is always safe).
_TRIVIAL_READS = frozenset({
    "cat", "ls", "pwd", "grep", "egrep", "fgrep", "rg", "nl", "head", "tail", "wc", "cut",
    "sort", "uniq", "tr", "column", "diff", "comm", "stat", "file", "basename", "dirname",
    "realpath", "which", "type", "echo", "printf", "true", "date", "tree", "ps", "df", "du",
    "uname", "whoami", "hostname",
})
# Trivial in-project writes: purely ADDITIVE only. rm/cp/mv/ln/tee PARK (distinguishing a safe
# `rm <file>` from `rm -rf .` without _rm_target_too_broad — deleted in Task 7 — is the whack-a-mole
# the design rejects; parking them is the convergent, always-safe choice).
_TRIVIAL_WRITES = frozenset({"mkdir", "touch"})
# Opt-in "local-work" scope: a few PLAIN bare build/test verbs (npm/cargo gated to safe subcommands).
_LOCALWORK_BARE = frozenset({"pytest", "make"})
# Any shell-complexity marker disqualifies the fast-path: ANY `$` (cmd-subst $(…), $VAR/${VAR}/$1,
# AND special params $@/$*/$?/$$/$!/$# — R1-F1: the old `\$[\w({]` missed $@/$* which a read verb
# would then fast-accept), `…` cmd-subst, process-subst <(…)/>(…), or a leading-position ~ expansion.
# (write-redirects + root-glob are checked per-segment below.)
_TRIVIAL_COMPLEXITY = re.compile(r'\$|`|<\(|>\(|(?:^|\s)~')
# A trivial segment may contain ONLY these chars (positive allow-list — ends the operator whack-a-mole:
# any shell metachar OUTSIDE this set → park). Word chars, whitespace, common path/flag punctuation, globs
# (*?[]), and quotes are allowed; EXCLUDED (→ park): redirects < >, background/subshell/group & ( ) { },
# pipes/seps | ; (also split earlier), expansions $ ` ~, escape \, history !, comment #. (R6-F1/R7-F1.)
_TRIVIAL_SEG_RE = re.compile(r"^[\w\s./\-_=,:@%+*?\[\]'\"]*$")


def _fast_path_scope() -> str:
    """'reads' (default) | 'local-work'. Env BULLDOZER_FAST_PATH_SCOPE, read FRESH per approval.
    Any unrecognized value behaves as 'reads' (the local-work branch simply never fires)."""
    return os.environ.get("BULLDOZER_FAST_PATH_SCOPE") or "reads"


# #280 A: per-VERB flags/operands that turn a _TRIVIAL_READS verb into EXEC, arbitrary-WRITE, or state
# mutation — the CVE-2025-66032 (`sort -o`) bypass class. PER-VERB because the same flag differs by verb
# (`ls -o` = long listing [safe] vs `sort -o` = write [dangerous]), so a global flag denylist would
# wrongly park `ls -o`. Bounded by these verbs' stable documented flag sets — an explicit small map, NOT
# the open-ended shell-operator space _TRIVIAL_SEG_RE guards. (Broad-READ flags `grep -R` /
# `wc --files0-from` are finding-D — handled by read-path-bounding, not here.)
_DANGEROUS_READ_VERB_FLAGS = {
    "sort": frozenset({"-o", "--output", "--compress-program"}),   # -o/--output WRITE; --compress-program EXECs
    "rg": frozenset({"--pre", "--pre-glob", "--hostname-bin"}),     # --pre[-glob]/--hostname-bin run a command
    "tree": frozenset({"-o"}),                                      # -o writes the listing to a file
    "date": frozenset({"-s", "--set"}),                            # sets the system clock (mutation)
    "hostname": frozenset({"-F", "--file"}),                       # sets hostname from a file (mutation)
}


def _read_verb_subverts_to_exec_or_write(verb: str, toks: list) -> bool:
    """#280 A: True iff a _TRIVIAL_READS verb carries a flag/operand that turns it into EXEC, an
    arbitrary file WRITE, or a state mutation — so it must PARK, not fast-accept. Covers the flag forms
    (incl. `--flag=value` and glued GNU short `-ofile`) AND the positional-output writers
    (`uniq INPUT OUTPUT` writes the 2nd operand; a bare operand to `hostname` sets the name)."""
    bad = _DANGEROUS_READ_VERB_FLAGS.get(verb)
    if bad:
        for t in toks[1:]:
            base = t.split("=", 1)[0]                       # --output=/p → --output
            if base in bad:
                return True
            for b in bad:                                   # glued GNU short flag: sort -ofile, tree -o<x>
                if len(b) == 2 and b[0] == "-" and b[1] != "-" \
                        and not base.startswith("--") and base.startswith(b) and base != b:
                    return True
    operands = [t for t in toks[1:] if not t.startswith("-")]
    if verb == "uniq" and len(operands) >= 2:               # `uniq INPUT OUTPUT` → 2nd operand WRITES
        return True
    if verb == "hostname" and operands:                     # `hostname NEWNAME` → sets the hostname
        return True
    return False


def _is_trivially_safe(command, project_root=None) -> bool:
    """True iff EVERY segment of `command` is a plain read (or trivial in-project write) with NO
    shell-complexity marker — the narrow fast-path predicate. What it cannot PROVE trivial → False
    (→ park_for_model; the model decides with context). Honors _fast_path_scope(): 'local-work' also
    accepts a few PLAIN bare build/test forms. Reuses the simple tokenizers only — never the deleted
    parse-to-accept gates."""
    if not isinstance(command, str) or not command.strip():
        return False
    if _TRIVIAL_COMPLEXITY.search(command):          # any $ / `…` / <(…) / >(…) / ~ → not trivial
        return False
    scope = _fast_path_scope()
    for seg in _split_segments(command):
        if not seg.strip():
            continue                                 # blank piece (e.g. a trailing pipe) — skip
        # ANY shell metachar OUTSIDE quotes → park (redirects < >, background/subshell/group & ( ) { },
        # expansions $ ` ~, escape \, history ! — R1-F1/R6-F1/R7-F1: a positive allow-list, not a one-off
        # blocklist, so a novel operator form can never silently fast-accept). Quote-aware (R8-F2): inert
        # regex punctuation INSIDE quotes (rg "a|b") is fine — strip quoted spans before the check.
        bare = re.sub(r'"[^"]*"|\'[^\']*\'', "", seg)
        if not _TRIVIAL_SEG_RE.match(bare):
            return False
        toks = _safe_tokens(seg)                      # NO prefix-strip: env/time/FOO=bar stay the verb → park (R1-F1)
        if not toks:
            return False
        if any(t.startswith("/") and any(c in t for c in "*?[") for t in toks):
            return False                             # root-level glob → not trivial
        if "/" in toks[0]:                            # R8-F1: a path-qualified executable (./cat, /tmp/cat,
            return False                             # scripts/rg) is NOT a trusted bare verb → park
        verb = toks[0]
        if verb in _TRIVIAL_READS:
            if _read_verb_subverts_to_exec_or_write(verb, toks):
                return False                          # #280 A: read verb w/ exec/write/mutation flag → park
            if _is_catastrophic_target(seg, project_root):
                return False                          # #280 D: path-bound reads to the project (an
            continue                                  # absolute/~/.. target outside → park, model decides)
        if verb in _TRIVIAL_WRITES:
            if _is_catastrophic_target(seg, project_root):
                return False
            continue
        if scope == "local-work":
            # PLAIN forms only — reject options (-x) and variable assignments (FOO=bar): `pytest -p
            # evilplugin` loads a plugin, `make CC=/bad` overrides the compiler → both PARK (R1-F1).
            if not any(t.startswith("-") or "=" in t for t in toks[1:]):
                if verb in _LOCALWORK_BARE:
                    continue
                if verb == "npm" and len(toks) >= 2 and toks[1] == "test":
                    continue
                if verb == "cargo" and len(toks) >= 2 and toks[1] in ("build", "test"):
                    continue
        return False                                 # unknown / non-trivial verb → park
    return True


_WRAP_SHELLS = frozenset({"sh", "bash", "zsh"})
# Absolute shell paths are unwrapped ONLY from these known-good system locations (codex_review P2): an
# arbitrary absolute path like /tmp/sh is an untrusted executable — unwrapping it would let its trivial
# inner fast-accept while the real binary run is /tmp/sh. Bare basenames (PATH-resolved) stay allowed.
_TRUSTED_SHELL_DIRS = frozenset({"/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"})


def _unwrap_shell_wrapper(command):
    """#281: the codex app-server runs every command as `<shell> -lc '<script>'` (or `-c`). route_approval
    must evaluate the INNER script — else _is_trivially_safe parks on the path-qualified shell verb
    (`/bin/zsh`) at the R8-F1 check before any per-verb gate runs, so the fast-path (and the #280 A/D
    gates) is DEAD live. Return the inner script for the EXACT app-server shape; else `command` unchanged
    (fail-closed → the caller's predicate then parks the wrapper). Exact shape ONLY: basename ∈
    {sh,bash,zsh} (bare or absolute-path), exactly ONE option `-c`/`-lc`, exactly ONE script token, NO
    positional $0/args after it. `env zsh -lc` / `time sh -c` / multiple `-c` / `-s` / `--` / interactive
    `-ic` / relative-path shell / UNTRUSTED absolute shell path (/tmp/sh) / nested wrappers → NOT unwrapped.
    Absolute shells unwrap ONLY from _TRUSTED_SHELL_DIRS; bare basenames are PATH-resolved and allowed. The
    inner then runs the SAME predicate as a bare command (codex itself unwraps `bash -lc` — codex/rules)."""
    if not isinstance(command, str):
        return command
    toks = _safe_tokens(command)
    if len(toks) != 3:                       # EXACTLY shell, -c/-lc, script — any arg after script → park
        return command
    shell, opt, script = toks
    if shell.rsplit("/", 1)[-1] not in _WRAP_SHELLS:
        return command                       # basename not a known shell (incl. python3 -c) → no unwrap
    if "/" in shell:                         # has a path component (not a bare PATH-resolved basename)
        if not shell.startswith("/"):
            return command                   # relative-path shell (./zsh, scripts/sh) → no unwrap
        if shell.rsplit("/", 1)[0] not in _TRUSTED_SHELL_DIRS:
            return command                   # untrusted absolute shell (/tmp/sh) → don't trust inner (P2)
    if opt not in ("-c", "-lc"):             # exactly one -c/-lc; -ic/-s/--/combined → no unwrap
        return command
    if not isinstance(script, str) or not script.strip():
        return command
    return script


def route_approval(method, params, project_root=None):
    """3-way routing for a codex approval request (#277): "fast_accept" (answer inline, no model
    round-trip) | "park_for_model" (return to the orchestrating session model) | "fail_closed_decline"
    (only a malformed/unrepresentable request). Consulted ONLY for the five approval methods;
    requestUserInput / mcpServer-elicitation are NEVER passed here (they keep their human path)."""
    params = params if isinstance(params, dict) else {}   # handle_server_request keeps truthy non-dict;
    if _has_escalation_amendment(params):                 # normalize BEFORE any .get()/helper call (F5)
        return "park_for_model"                           # structured escalation → the model decides
    if method == "item/permissions/requestApproval":
        return "park_for_model"
    if method in ("item/fileChange/requestApproval", "applyPatchApproval"):
        return "park_for_model"
    cmd = params.get("command")
    if method == "execCommandApproval":          # R9-F1: legacy → ALWAYS park (spec §4); never fast-accept
        return "park_for_model"
    inner = _unwrap_shell_wrapper(cmd)           # #281: evaluate the INNER script, not the /bin/zsh wrapper
    if method == "item/commandExecution/requestApproval" and isinstance(cmd, str) and cmd.strip():
        return "fast_accept" if _is_trivially_safe(inner, project_root) else "park_for_model"
    if _all_read_actions(params.get("commandActions")) and isinstance(cmd, str) and cmd.strip():
        return "fast_accept" if _is_trivially_safe(inner, project_root) else "park_for_model"
    return "fail_closed_decline"


# ── #277 park_for_model: the awaiting-payload (model-facing) + decision-response (codex-facing) builders ──
# Both call _approval_decision_table on the SAME params → identical option order → identical opaque ids
# (d0,d1,…), so the model's chosen id re-maps deterministically WITHOUT shared state. The per-method
# result payloads MIRROR _bridge_approval_dispatch exactly (verified vs codex 0.142 schema), so a parked
# resume produces the byte-identical reply a human dialog would.
_APPROVAL_KIND = {
    "item/commandExecution/requestApproval": "commandExecution",
    "execCommandApproval": "commandExecution",
    "item/fileChange/requestApproval": "fileChange",
    "applyPatchApproval": "applyPatch",
    "item/permissions/requestApproval": "permissions",
}


def _approval_decline_payload(method: str) -> dict:
    """The method's safe-decline `result` payload (the literal 'decline' id + the fail-closed default)."""
    if method == "item/permissions/requestApproval":
        return PERM_DECLINE
    if method in ("execCommandApproval", "applyPatchApproval"):
        return {"decision": "denied"}
    return {"decision": "decline"}          # command + fileChange


def _approval_accept_payload(method: str) -> dict:
    """The method's PLAIN-accept `result` payload for the `fast_accept` loop-body fast-path (#277). Only
    the command methods ever fast-accept (route_approval parks permissions/fileChange), but the legacy
    execCommandApproval uses the review-decision shape — so map it correctly."""
    if method == "execCommandApproval":
        return {"decision": "approved"}
    return {"decision": "accept"}           # item/commandExecution/requestApproval (+ any other → bare accept)


def _approval_decision_table(method: str, params: dict, acc=None) -> list:
    """[(opaque_id, display_label, result_payload), …] — the bounded options the model picks from, each
    mapped to the EXACT `result` payload codex expects. Deterministic order ⇒ stable d-ids across both
    builders. Mirrors _bridge_approval_dispatch per method."""
    params = params if isinstance(params, dict) else {}
    if method == "item/commandExecution/requestApproval":
        pairs = [(lbl, {"decision": dec}) for lbl, dec in build_command_approval_labels(params, acc=acc)]
    elif method == "item/fileChange/requestApproval":
        pairs = [(LBL_ALLOW_ONCE, {"decision": "accept"}),
                 (LBL_ALLOW_SESSION, {"decision": "acceptForSession"}),
                 (LBL_DONT_ALLOW, {"decision": "decline"}),
                 (LBL_CANCEL, {"decision": "cancel"})]
    elif method == "item/permissions/requestApproval":
        requested = params.get("permissions")
        requested = requested if isinstance(requested, dict) else {}
        pairs = [(LBL_GRANT_TURN, {"permissions": requested, "scope": "turn"}),
                 (LBL_GRANT_SESSION, {"permissions": requested, "scope": "session"}),
                 (LBL_DONT_GRANT, PERM_DECLINE)]
    elif method in ("execCommandApproval", "applyPatchApproval"):
        pairs = [(LBL_ALLOW_ONCE, {"decision": "approved"}),
                 (LBL_ALLOW_SESSION, {"decision": "approved_for_session"}),
                 (LBL_DONT_ALLOW, {"decision": "denied"})]
    else:
        pairs = []
    return [(f"d{i}", lbl, payload) for i, (lbl, payload) in enumerate(pairs)]


_PAYLOAD_MAX_ITEMS = 40          # R1-F6: default max list entries / dict keys in a payload
_PAYLOAD_MAX_STR = 4000          # R1-F6: default max chars for any single string field in a payload
_PAYLOAD_MAX_TOTAL = 16000       # R1-F6: HARD byte budget for the whole model-facing awaiting payload


def _bound_evidence(obj, _depth=0, max_str=_PAYLOAD_MAX_STR, max_items=_PAYLOAD_MAX_ITEMS):
    """Recursively bound model-facing evidence (R1-F6): cap string length, list length (+N-more marker),
    dict KEY count, and nesting depth. Parametrized so build_awaiting_payload can tighten the caps until
    the WHOLE payload fits the total budget — no single field (network host list, unknown key, long
    path/diff/scalar) can blow the MCP token limit. The UNBOUNDED original is what build_decision_response
    echoes to codex on accept — only the evidence shown to the model is bounded."""
    if _depth > 6:
        return "… [nested too deep]"
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + "… [+{} chars]".format(len(obj) - max_str)
    if isinstance(obj, list):
        out = [_bound_evidence(x, _depth + 1, max_str, max_items) for x in obj[:max_items]]
        if len(obj) > max_items:
            out.append("… [+{} more]".format(len(obj) - max_items))
        return out
    if isinstance(obj, dict):
        items = list(obj.items())
        capped = {}
        for k, v in items[:max_items]:
            key = k if isinstance(k, str) else str(k)
            if len(key) > max_str:                            # cap pathological dict KEYS too (R1-F6)
                key = key[:max_str] + "…[+{} chars]".format(len(key) - max_str)
            while key in capped:                              # deterministic collision resolution
                key += "_"
            capped[key] = _bound_evidence(v, _depth + 1, max_str, max_items)
        if len(items) > max_items:
            capped["_omitted_keys"] = len(items) - max_items
        return capped
    return obj


def _bound_permissions(perms):
    """Reorder a permission profile network-FIRST (egress is the security-load-bearing grant — must
    survive truncation) for the model-facing payload. BOUNDING itself is applied by the final
    _bound_evidence pass in build_awaiting_payload (which preserves this key order). The UNBOUNDED
    original is what build_decision_response echoes to codex on accept."""
    if not isinstance(perms, dict) or "network" not in perms:
        return perms
    return {"network": perms["network"], **{k: v for k, v in perms.items() if k != "network"}}


def build_awaiting_payload(method: str, params, ts, narrative, park_token):
    """Build the {status:'awaiting_approval', …} payload returned to the orchestrating session model
    when a park_for_model approval suspends the turn (#277). Returns (payload, decision_ids). All
    variable-size evidence (permission profiles, file-change diffs) is BOUNDED (R1-F6) so a huge
    request can't blow the MCP token limit; build_decision_response still echoes the ORIGINAL to codex."""
    params = params if isinstance(params, dict) else {}
    ts = ts or {}
    table = _approval_decision_table(method, params, acc=ts.get("acc"))
    decisions = [{"id": d_id, "label": label} for d_id, label, _ in table]
    decision_ids = {d_id for d_id, _, _ in table} | {"decline"}
    kind = _APPROVAL_KIND.get(method, "unknown")
    # #322 PR2 (F4): a park that never resumes (cap teardown, session death) previously
    # left no "parked at t=X" line to correlate the resolution against. Token logged as
    # an 8-char prefix only — never verbatim (it is the resume capability).
    try:
        # token8 = the LAST 8 chars: production tokens are 'park-<hex>', so a prefix
        # slice would keep the constant 'park-' + 3 hex digits (4096 ids, ~50%
        # collision at ~75 parks) — the suffix carries full entropy (#325 r3).
        _drift_warn(None, "PARK", kind=kind, method=method,
                    token8=str(park_token)[-8:])
    except Exception:
        pass
    approval = {"kind": kind, "decisions": decisions}
    if narrative:
        approval["narrative"] = _truncate_for_display(
            narrative, max_lines=12, max_chars=_approval_narrative_max())
    if params.get("reason"):
        approval["reason"] = params.get("reason")
    if kind == "commandExecution":
        cmd = params.get("command")
        if isinstance(cmd, str):
            approval["command"] = _truncate_for_display(cmd, tail_lines=6)
        elif isinstance(cmd, list):                  # R10-F1: legacy argv-list shape (["ls","-la"]) → render it
            approval["command"] = _truncate_for_display(" ".join(str(x) for x in cmd), tail_lines=6)
        nac = params.get("networkApprovalContext")   # #280 B: surface the egress destination the human
        if isinstance(nac, dict) and nac.get("host"):  # dialog shows — added BEFORE cwd/actions so the
            approval["network"] = {                   # security-critical destination survives bounding
                "host": _truncate_for_display(str(nac["host"]), max_lines=1, max_chars=100),
                "protocol": nac.get("protocol") or "network"}
        approval["cwd"] = params.get("cwd")
        ca = _summarize_command_actions(params.get("commandActions"))
        if ca:
            approval["command_actions"] = ca
    elif kind == "permissions":
        requested = params.get("permissions")
        requested = requested if isinstance(requested, dict) else {}
        approval["permissions"] = _bound_permissions(requested)   # network-FIRST; bounding below
        approval["summary"] = _summarize_permissions(requested)
        approval["cwd"] = params.get("cwd")
        if params.get("environmentId") is not None:
            approval["environmentId"] = params.get("environmentId")
    elif kind == "applyPatch":                  # legacy patch — the diff is ON the request
        approval["file_changes"] = params.get("fileChanges")
    elif kind == "fileChange":                  # modern — the diff comes from the Task-4 patch buffer
        item_id = params.get("itemId")
        fc = ts.get("file_changes", {}).get(item_id) if item_id else None
        if fc and fc.get("changes"):
            approval["changes"] = fc["changes"]          # [{diff, kind, path}] (path+kind even for add/delete)
        else:
            # No captured patch yet (rare — patchUpdated not seen before the approval). Surface the
            # VALIDATED request fields (itemId/reason/grantRoot) instead of blind-declining (R2-F4) —
            # the model still decides (and can decline if the evidence is too thin).
            approval["item_id"] = item_id
            if params.get("grantRoot") is not None:
                approval["grantRoot"] = params.get("grantRoot")
            approval["note"] = "no diff captured yet — decide from reason, or decline if unsure"
    # R1-F6: ONE bounded pass over the ENTIRE approval evidence (every field — summary, raw scalar
    # reason/cwd/environmentId, unknown keys, long fs paths/diffs), then TIGHTEN the caps until the whole
    # payload fits the HARD total byte budget (per-field caps alone can't guarantee a total — 40 items ×
    # 4000 chars = 160KB). The UNBOUNDED original is still what build_decision_response echoes to codex.
    tid = ts.get("thread_id")
    for max_str, max_items in ((_PAYLOAD_MAX_STR, _PAYLOAD_MAX_ITEMS), (800, 20), (200, 10), (60, 5)):
        payload = {"status": "awaiting_approval", "park_token": park_token,
                   "approval": _bound_evidence(approval, max_str=max_str, max_items=max_items),
                   "thread_id": _bound_evidence(tid, max_str=max_str, max_items=max_items)}  # R1-F6: thread_id too
        if len(json.dumps(payload, default=str)) <= _PAYLOAD_MAX_TOTAL:
            break
    else:
        # HARD fallback (R1-F6): even the tightest caps still exceeded the budget → a minimal payload that
        # ALWAYS fits (kind + decisions + a decline-if-unsure note + bounded thread_id). The model can
        # still decide or decline.
        payload = {"status": "awaiting_approval", "park_token": park_token,
                   "approval": _bound_evidence(
                       {"kind": kind, "decisions": decisions,
                        "note": "evidence too large to display safely — decide conservatively or decline"},
                       max_str=200, max_items=10),
                   "thread_id": _bound_evidence(tid, max_str=200, max_items=10)}
    return payload, decision_ids


def build_decision_response(parked_request_frame, decision_id):
    """Build the EXACT jsonrpc-lite reply ({id, result}) for the model's chosen decision_id, reusing the
    parked request's ORIGINAL id (#277). Receives an ALREADY-VALID id (codex_approve_v2 pre-validates
    against the stored decision_ids BEFORE gen.send — F3). 'decline' → the method's safe decline. The
    unknown-id arm is a DEFENSIVE backstop: it returns an {"error":…} (NOT a written decline frame), so
    a bad id can never be written to the child as a permanent decline."""
    frame = parked_request_frame or {}
    mid = frame.get("id")
    method = frame.get("method", "")
    params = frame.get("params") or {}
    if decision_id == "decline":
        return {"id": mid, "result": _approval_decline_payload(method)}
    if decision_id == "accept":   # #277 fast_accept literal (loop-body fast-path; not a model choice)
        return {"id": mid, "result": _approval_accept_payload(method)}
    payload_map = {d_id: payload for d_id, _, payload in _approval_decision_table(method, params)}
    if decision_id in payload_map:
        return {"id": mid, "result": payload_map[decision_id]}
    return {"error": f"unknown decision_id: {decision_id!r}"}   # defensive-unreachable (pre-validated, F3)


# The five codex approval methods route_approval / the park-route gate apply to (#277). A
# non-approval server request (requestUserInput / mcpServer-elicitation) is NEVER routed/parked.
_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval", "execCommandApproval",
    "item/fileChange/requestApproval", "applyPatchApproval",
    "item/permissions/requestApproval",
})


def _park_token() -> str:
    """A single-use opaque park token (#277). `uuid` is not imported; os.urandom is unique enough to
    reject a stale/double-resume token (the park is also cleared after use, the real guard)."""
    return "park-" + os.urandom(8).hex()


def bridge_approval(method: str, params: dict, cc_write_fn, cc_read_fn,
                    timeout: float = 300.0, acc=None, narrative=None, drain_ctx=None):
    """Issue a CC elicitation/create, wait for the answer, return the codex decision.

    Thin wrapper over _bridge_approval_dispatch: records one best-effort approval-event
    log line (method / decision / wait_ms / timed_out, #251 step-0) at every exit, then
    returns the dispatch decision unchanged. drain_ctx (#252) is threaded through to the wait.

    ATTENDED-ONLY (#277): the unattended in-process judge is GONE — when unattended mode is armed,
    the turn-pump loop body routes approvals via route_approval (fast_accept / park_for_model /
    fail_closed_decline) BEFORE this function is ever reached, so bridge_approval now only computes
    the human elicitation (unarmed approvals + non-approval requests). Default OFF → unchanged.
    """
    t0 = time.time()
    wait_state = {"timed_out": False}
    decision = _bridge_approval_dispatch(
        method, params, cc_write_fn, cc_read_fn, timeout, acc, narrative, wait_state,
        drain_ctx=drain_ctx)
    _log_approval_event(method, decision, int((time.time() - t0) * 1000),
                        wait_state["timed_out"], ui=wait_state.get("ui"))
    return decision


def _bridge_approval_dispatch(method: str, params: dict, cc_write_fn, cc_read_fn,
                              timeout: float = 300.0, acc=None, narrative=None,
                              _wait_state=None, drain_ctx=None):
    """Issue a CC elicitation/create, wait for the answer, return the codex decision.

    CC-facing elicitation request: standard JSON-RPC 2.0 (has "jsonrpc":"2.0").
    Return value: the codex decision payload (string or dict) — NOT a full frame.
    The caller wraps it in the appropriate {id, result: {...}} envelope.

    On CC decline / cancel / timeout → safe default ("decline" for approvals).
    """
    eid = _next_bridge_id()

    # Shared approval-wait bookkeeping (#252/#269) — unpacked ONCE, used by both the
    # CC-elicitation wait (read_correlated) and the #340 native-dialog pump.
    _reactor = drain_ctx.get("reactor") if drain_ctx else None
    _ts = drain_ctx.get("ts") if drain_ctx else None
    _cc_id = drain_ctx.get("cc_id") if drain_ctx else None
    drain_active = _reactor is not None and _ts is not None

    def _approval_reply(mid, result=None, error=None):
        # #269: answer an id-bearing CC request via the approval path's writer (cc_write_fn),
        # same JSON-RPC 2.0 envelope as the module `reply` / turn-pump path.
        cc_write_fn({"jsonrpc": "2.0", "id": mid,
                     ("error" if error else "result"): (error if error else result)})

    def _wait_step(resolve_eid=None, read_timeout=0.05):
        """ONE iteration of the approval wait: drain the child (#252), read+route one CC frame
        (#269), detect stdin EOF / our-turn cancel / terminal child. Returns (kind, payload):
        'resolved' (payload = the elicitation response frame; only when resolve_eid is given),
        'eof' | 'terminal' | 'cancel' (the ts flags are set exactly as before), or (None, None)
        for a transient/handled frame. Extracted VERBATIM from the old read_correlated loop
        body so the CC path and the #340 dialog pump share one bookkeeping implementation."""
        pending_terminal = None
        if drain_active:
            # #252: drain child stdout (child-only, non-blocking). NOTIFICATIONS accumulate via
            # the shared handler. A NON-notification (turn/start ACK, another server request) is
            # BUFFERED for the turn loop to re-process — dropping it would falsely time out a
            # pre-ACK approval (codex P1). A terminal frame is HELD so a same-iteration EOF can
            # win (codex P2) before we surface it. Non-dict frames are skipped (reviewer F3).
            for cf in _reactor.pump(timeout=0.0):
                if not isinstance(cf, dict) or "__cc__" in cf:
                    continue
                if classify(cf) != "notification":
                    _ts.setdefault("drained_frames", []).append(cf)
                    continue
                _res = _handle_child_frame(cf, _ts)
                if _res is not None:
                    pending_terminal = _res
        frame = cc_read_fn(timeout=read_timeout)
        if frame is _CC_EOF:                 # CC stdin closed (#218) → EOF wins (even over a held terminal)
            if drain_active:
                _ts["eof_during_approval"] = True
            return ("eof", None)
        if pending_terminal is not None:    # terminal child this iteration = turn over (no EOF) → surface it
            _ts["terminal_during_approval"] = pending_terminal
            return ("terminal", None)
        if frame is not None:
            # Shape-first: ONLY a RESPONSE whose id matches resolves the elicitation.
            if (resolve_eid is not None and frame.get("id") == resolve_eid
                    and classify(frame) == "response"):
                return ("resolved", frame)
            # A mid-approval cancel for our turn (interrupts enabled, cc_id known) → flag + decline.
            if (drain_active and _cc_id is not None
                    and frame.get("method") == "notifications/cancelled"
                    and (frame.get("params") or {}).get("requestId") == _cc_id
                    and _interrupts_enabled()):
                _ts["cancel_during_approval"] = True
                return ("cancel", None)
            # #269: otherwise an id-bearing CC request (ping/tools/list/tools/call) MUST be
            # answered or CC blocks on it (the turn-pump path enforces the same contract via
            # _route_cc_frame). It answers requests and no-ops notifications / responses /
            # foreign-or-disabled cancels; its interrupt/teardown return is irrelevant here
            # (our-turn cancel + EOF are handled above).
            _route_cc_frame(frame, cc_id=_cc_id, reply_fn=_approval_reply)
        # transient (None) / skipped frame → caller retries
        return (None, None)

    def read_correlated(eid: int, timeout: float):
        """Wait for the CC elicitation reply (id==eid response) — the pre-#340 loop, now a thin
        driver over _wait_step. With drain_ctx active (#252) it ALSO drains the codex child each
        iteration and detects a mid-approval cancel / stdin EOF / terminal-child frame — each
        sets a `ts` flag and ends the wait via the per-method `None` decline below, which the
        turn loop acts on after writing that decline. drain_ctx=None → byte-identical behavior."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            kind, payload = _wait_step(
                resolve_eid=eid,
                read_timeout=min(remaining, 0.05) if drain_active else remaining)
            if kind == "resolved":
                return payload
            if kind in ("eof", "terminal", "cancel"):
                return None
        if _wait_state is not None:
            _wait_state["timed_out"] = True
        return None  # deadline expired without a matching reply

    def _pump_event():
        """#340: the dialog-wait pump — one _wait_step with no elicitation to resolve. Returns
        'eof' | 'terminal' | 'cancel' | None; ~50ms pacing comes from the CC read timeout."""
        return _wait_step()[0]

    def _elicit(message: str, labels: list, allow_ok: bool):
        """#340: route one label-enum approval through the selected UI. Dialog mode returns a
        SYNTHETIC response frame (same {'result': {action, content}} shape CC produces), so the
        arms' decision mapping (label tables / fail-closed defaults / drift warnings) is
        UI-agnostic. `allow_ok` (from the arm's label→decision MAP) says whether a BARE accept
        really resolves to plain accept — the dialog offers its Allow button only then. Dialog
        unavailable (no GUI / no osascript) → stderr warn + CC-elicitation fallback: an approval
        must not die headless.

        ONE deadline governs the WHOLE approval — the dialog stages AND a late CC fallback. A
        fallback that started a fresh full-length wait after a long dialog could block for ~2× the
        configured timeout and blow the bridge/client deadline (codex round-3 P2); with the budget
        already spent, no elicitation is opened at all — the approval times out (safe decline). In
        CC mode the deadline is set here and consumed immediately, so the wait is the full timeout
        exactly as before. None-sentinel discipline: an EXPLICIT timeout=0 means "already expired"
        (decline at once, ask nobody) — `timeout or 300` silently turned it into a 5-minute wait
        (round-4 P2, reproduced: the zero-timeout tests took the full 300 s)."""
        deadline = time.time() + (300 if timeout is None else timeout)
        if _approval_ui() == "dialog":
            if _wait_state is not None:
                _wait_state["ui"] = "dialog"
            resp = _dialog_label_elicit(message, labels, deadline, allow_ok,
                                        pump_fn=_pump_event, wait_state=_wait_state)
            if resp is not _DIALOG_UNAVAILABLE:
                return resp
            if _wait_state is not None:
                _wait_state["ui"] = "cc"      # fell back — audit the UI that actually answered
            # best-effort: a broken stderr must NOT abort the fallback (codex P2, reproduced —
            # BrokenPipeError before the elicitation was sent → codex's approval hung forever)
            _warn_stderr("bulldozer-codex: approval dialog unavailable (no GUI?) — "
                         "falling back to CC elicitation")
        remaining = deadline - time.time()
        if remaining <= 0:                   # budget spent in the dialog → don't ask CC (r3 P2)
            if _wait_state is not None:
                _wait_state["timed_out"] = True
            return None                      # → the arm's safe decline
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": message,
                "requestedSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}},
                },
            },
        })
        return read_correlated(eid, remaining)

    if method == "item/commandExecution/requestApproval":
        label_pairs = build_command_approval_labels(params, acc=acc)
        labels = [lbl for lbl, _ in label_pairs]
        label_map = dict(label_pairs)
        # a bare accept is only meaningful if the default label really maps to plain `accept`
        # (dedupe can rename the real accept when a decision string collides with a display label)
        allow_ok = label_map.get(LBL_ALLOW_ONCE) == "accept"
        resp = _elicit(_build_command_approval_message(params, narrative), labels, allow_ok)
        if resp is None:
            return "decline"
        result = resp.get("result", {})
        action = result.get("action")
        if action == "accept":
            content = result.get("content") or {}
            # Clicking CC's Accept WITHOUT picking a dropdown label = plain accept
            # (the dropdown is optional, for advanced amendment choices).
            if "label" in content:
                chosen = content["label"]
                if chosen not in label_map:
                    # An unrecognized label is ambiguous → fail CLOSED (the posture the permissions
                    # arm already takes, #272). Returning "accept" here sent codex a decision it
                    # never OFFERED (invalid reply / bypassed the amendment). Round-2 P1.
                    _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
                    return "decline"
                return label_map[chosen]
            # BARE accept (CC's plain Accept, no dropdown pick) — valid only if the default label
            # really means plain `accept` in THIS offer (round-2 P1 / round-4 P3).
            if not allow_ok:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", "bare-accept, plain accept not offered")
                return "decline"
            return "accept"
        if action == "cancel":
            return "cancel"  # abort the turn — distinct from "decline" (skip this command)
        return "decline"

    if method == "item/fileChange/requestApproval":
        fc_pairs = [
            (LBL_ALLOW_ONCE, "accept"),
            (LBL_ALLOW_SESSION, "acceptForSession"),
            (LBL_DONT_ALLOW, "decline"),
            (LBL_CANCEL, "cancel"),
        ]
        labels = [lbl for lbl, _ in fc_pairs]
        fc_map = dict(fc_pairs)
        resp = _elicit(_build_simple_approval_message(
            "filechange", params.get("reason"), narrative), labels,
            fc_map.get(LBL_ALLOW_ONCE) == "accept")
        if resp is None:
            return "decline"
        result = resp.get("result", {})
        action = result.get("action")
        if action == "accept":
            content = result.get("content") or {}
            # Accept w/o dropdown = plain accept (dropdown is optional)
            chosen = content.get("label", LBL_ALLOW_ONCE)
            if chosen not in fc_map:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
            return fc_map.get(chosen, "accept")
        if action == "cancel":
            return "cancel"  # abort — distinct from "decline" (FileChangeApprovalDecision)
        return "decline"

    if method == "item/permissions/requestApproval":
        # #4: grant exactly what codex asked for (echo the requested RequestPermissionProfile).
        # An accept that returned {} granted NOTHING (silent no-op). Request/response profiles
        # share the {fileSystem?,network?} shape (codex 0.141 schema), so the echo is valid.
        # A malformed truthy non-dict (review D) is NOT echoed verbatim — fail open to {}.
        requested = params.get("permissions")
        if not isinstance(requested, dict):
            requested = {}
        perm_pairs = [
            (LBL_GRANT_TURN, {"permissions": requested, "scope": "turn"}),
            (LBL_GRANT_SESSION, {"permissions": requested, "scope": "session"}),
            (LBL_DONT_GRANT, PERM_DECLINE),
        ]
        labels = [lbl for lbl, _ in perm_pairs]
        perm_map = dict(perm_pairs)
        resp = _elicit(_build_simple_approval_message(
            "permissions", params.get("reason"), narrative,
            details=_summarize_permissions(requested)), labels,
            LBL_GRANT_TURN in perm_map)
        if resp is None:
            return PERM_DECLINE
        result = resp.get("result", {})
        if result.get("action") == "accept":
            content = result.get("content") or {}
            # Bare accept (no dropdown label) = grant for this turn — CC's plain Accept, the
            # legitimate common case. But a PRESENT-but-UNRECOGNIZED label is ambiguous on a
            # now-load-bearing grant path (#4 made the grant real), so it fails CLOSED to decline
            # (review C) — NOT a silent full grant. Pre-#4 this fallback granted {} (a safe no-op);
            # this preserves that safety without reintroducing the no-op for the normal case.
            chosen = content.get("label", LBL_GRANT_TURN)
            if chosen not in perm_map:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
            return perm_map.get(chosen, PERM_DECLINE)
        return PERM_DECLINE

    if method == "item/tool/requestUserInput":
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": "Codex tool input request (automated: answering empty)",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": ["ok"]}},
                },
            },
        })
        read_correlated(eid, timeout)  # consume; result ignored
        return {"answers": {}}

    if method == "mcpServer/elicitation/request":
        elicit_params: dict = {
            "message": params.get("message", ""),
        }
        schema = params.get("requestedSchema")
        if schema:
            elicit_params["requestedSchema"] = schema
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": elicit_params,
        })
        resp = read_correlated(eid, timeout)
        if resp is None:
            return {"action": "cancel", "content": None, "_meta": None}
        result = resp.get("result", {})
        return {
            "action": result.get("action", "cancel"),
            "content": result.get("content"),
            "_meta": None,
        }

    # Legacy methods: ReviewDecision
    if method in ("execCommandApproval", "applyPatchApproval"):
        legacy_pairs = [
            (LBL_ALLOW_ONCE, "approved"),
            (LBL_ALLOW_SESSION, "approved_for_session"),
            (LBL_DONT_ALLOW, "denied"),
        ]
        labels = [lbl for lbl, _ in legacy_pairs]
        label_to_review = dict(legacy_pairs)
        resp = _elicit(f"Codex {method}\nCWD: {params.get('cwd') or '(unknown)'}", labels,
                       label_to_review.get(LBL_ALLOW_ONCE) == "approved")
        if resp is None:
            return {"decision": "denied"}
        result = resp.get("result", {})
        if result.get("action") == "accept":
            content = result.get("content") or {}
            chosen = content.get("label", LBL_ALLOW_ONCE)  # Accept w/o dropdown = approve
            if chosen not in label_to_review:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
            return {"decision": label_to_review.get(chosen, "approved")}
        return {"decision": "denied"}

    # Unreachable via public router but guard anyway
    return {"decision": "denied"}


def _jsonrpc_lite_error(mid, code: int, message: str) -> dict:
    """Return a jsonrpc_lite error frame (NO 'jsonrpc' key — app-server wire)."""
    return {"id": mid, "error": {"code": code, "message": message}}


_UNSUPPORTED_METHODS = frozenset({
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "attestation/generate",
})

_BRIDGED_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "execCommandApproval",
    "applyPatchApproval",
})

# Subset of approvals whose dialog surfaces the pre-approval agentMessage narrative
# (#224). The pump advances the narrative offset ONLY for these — a non-narrative
# bridged request (tool input, elicitation, legacy review) must not consume narrative
# that a following commandExecution approval should display (panel finding Grok#2).
_NARRATIVE_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
})

# Benign lifecycle notifications a healthy turn may emit. Sourced from the protocol's
# ServerNotification union (generated by mcp/gen_notifications.py → the checked-in
# fixture), so it stays complete across codex versions. "error"/"warning" are
# deliberately EXCLUDED (they signal problems — surfaced as before, #204). A stdlib
# fallback keeps the server functional if the fixture is missing.
_NOTIFICATION_FALLBACK = frozenset({
    "item/agentMessage/delta", "item/completed", "turn/completed",
    "turn/started", "item/started",
    "thread/settings/updated", "thread/status/changed", "thread/tokenUsage/updated",
    "mcpServer/startupStatus/updated", "hook/started", "hook/completed",
    "account/rateLimits/updated", "skills/changed", "remoteControl/status/changed",
})


def _load_known_notifications(path: str | None = None) -> frozenset:
    # F7: fixture is a SIBLING of this file (mcp/codex-notifications.json) so it ships
    # in the plugin cache. Loading from tests/fixtures/ risked a silent fallback to the
    # 14-name set in production (cache may prune tests/) → spurious _drift (defeats #8).
    # `path` override is for tests.
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "codex-notifications.json")
    # F7: validate SHAPE, not just open/decode — a valid-JSON-but-wrong-shape fixture
    # (a list, or {"server_notifications": "abc"}) must warn+fallback, never crash at import
    # (`data.get` on a list → AttributeError) or load a bogus char-set allowlist (`set("abc")`).
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            reason = f"top-level JSON is {type(data).__name__}, expected object"
        else:
            raw = data.get("server_notifications")
            if not isinstance(raw, list) or not raw:
                reason = "server_notifications missing/empty/not-a-list"
            elif not all(isinstance(x, str) for x in raw):
                reason = "server_notifications has non-string entries"
            else:
                return frozenset(set(raw) - {"error", "warning"})
    except (OSError, json.JSONDecodeError) as e:
        reason = f"{type(e).__name__}: {e}"
    # A missing/malformed fixture in PRODUCTION (cache miss) silently regressing to the
    # 14-name set reintroduces spurious _drift (defeats #8). Make the fallback LOUD in the
    # stable log so a packaging miss is diagnosable, then degrade gracefully.
    _drift_warn(None, "NOTIFICATION_FIXTURE_MISSING", f"{path} ({reason}) — using fallback set")
    return _NOTIFICATION_FALLBACK


_KNOWN_NOTIFICATIONS = _load_known_notifications()


def handle_server_request(msg: dict, cc_write_fn=None, cc_read_fn=None,
                          timeout: float = 300.0, acc=None, narrative=None,
                          drain_ctx=None) -> dict:
    """Route a server→client ServerRequest to the correct handler.

    Returns a jsonrpc_lite frame ({id, result} or {id, error}) — NO 'jsonrpc' key.
    Never returns None (spec invariant: never drop a ServerRequest).
    acc: optional list — if provided, drift breadcrumbs are appended via _drift_warn.
    narrative: optional str — codex's pre-approval agentMessage narrative (#224),
        surfaced in the approval dialog as context. None on the no-narrative path.
    """
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method in _UNSUPPORTED_METHODS:
        return _jsonrpc_lite_error(mid, -32601, f"{method} not supported by this client")

    if method not in _BRIDGED_METHODS:
        _drift_warn(acc, "UNKNOWN_SERVER_METHOD", method)
        return _jsonrpc_lite_error(mid, -32601, f"method not found: {method}")

    # Bridged methods — need cc_write_fn / cc_read_fn
    if cc_write_fn is None or cc_read_fn is None:
        return _jsonrpc_lite_error(mid, -32603, "bridge not wired (no CC channel)")

    if method == "item/commandExecution/requestApproval":
        decision = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc, narrative=narrative, drain_ctx=drain_ctx)
        return {"id": mid, "result": {"decision": decision}}

    if method == "item/fileChange/requestApproval":
        decision = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc, narrative=narrative, drain_ctx=drain_ctx)
        return {"id": mid, "result": {"decision": decision}}

    if method == "item/permissions/requestApproval":
        grant = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc, narrative=narrative, drain_ctx=drain_ctx)
        return {"id": mid, "result": grant}

    if method == "item/tool/requestUserInput":
        answers = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc, narrative=narrative, drain_ctx=drain_ctx)
        return {"id": mid, "result": answers}

    if method == "mcpServer/elicitation/request":
        elicit_result = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc, narrative=narrative, drain_ctx=drain_ctx)
        return {"id": mid, "result": elicit_result}

    if method in ("execCommandApproval", "applyPatchApproval"):
        review = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc, narrative=narrative, drain_ctx=drain_ctx)
        return {"id": mid, "result": review}

    # Should never reach here (covered above), but be safe
    return _jsonrpc_lite_error(mid, -32601, f"unhandled method: {method}")


def _is_valid_command_decision(d) -> bool:
    """True if d is a valid CommandExecutionApprovalDecision (string or known dict)."""
    if isinstance(d, str):
        return d in ("accept", "acceptForSession", "decline", "cancel")
    if isinstance(d, dict):
        return bool(d.get("acceptWithExecpolicyAmendment") or d.get("applyNetworkPolicyAmendment"))
    return False


def _is_valid_review_decision(d) -> bool:
    """True if d is a valid ReviewDecision."""
    if isinstance(d, str):
        return d in ("approved", "approved_for_session", "denied", "timed_out", "abort")
    if isinstance(d, dict):
        return bool(d.get("approved_execpolicy_amendment") or d.get("network_policy_amendment"))
    return False


SERVER_REQUEST_RESPONSE_SHAPE: dict = {
    "item/commandExecution/requestApproval": lambda r: (
        "result" in r and "decision" in r["result"]
        and _is_valid_command_decision(r["result"]["decision"])
    ),
    "item/fileChange/requestApproval": lambda r: (
        "result" in r and r["result"].get("decision") in
        ("accept", "acceptForSession", "decline", "cancel")
    ),
    "item/tool/requestUserInput": lambda r: (
        "result" in r and "answers" in r["result"]
    ),
    "mcpServer/elicitation/request": lambda r: (
        "result" in r and "action" in r["result"]
    ),
    "item/permissions/requestApproval": lambda r: (
        "result" in r
        and "permissions" in r["result"]
        and "scope" in r["result"]
    ),
    "execCommandApproval": lambda r: (
        "result" in r and "decision" in r["result"]
        and _is_valid_review_decision(r["result"]["decision"])
    ),
    "applyPatchApproval": lambda r: (
        "result" in r and "decision" in r["result"]
        and _is_valid_review_decision(r["result"]["decision"])
    ),
    "item/tool/call": lambda r: "error" in r,
    "account/chatgptAuthTokens/refresh": lambda r: "error" in r,
    "attestation/generate": lambda r: "error" in r,
}


class TurnStateMachine:
    """Tracks the in-flight turn state for the MCP bridge.

    A turn is in-flight from the moment a CC tools/call starts being served
    (turn_started) until turn/completed is received from the app-server
    (turn_completed).  A second tools/call arriving while in-flight gets a
    busy error.  On app-server EOF mid-turn, eof_error() clears the state
    and returns a terminal CC-facing error frame.
    """

    def __init__(self):
        self._in_flight = False
        self._pending_cc_id = None
        # #277 parked state: a turn suspended at a park_for_model approval, awaiting a codex_approve
        # resume. Holds the park token only (the live generator + frame live in manager._parked).
        # None when not parked. is_busy() stays True while parked.
        self._parked = None

    def turn_started(self, cc_id):
        self._in_flight = True
        self._pending_cc_id = cc_id

    def turn_completed(self):
        self._in_flight = False
        self._pending_cc_id = None
        self._parked = None                          # final completion clears any park (#277)

    def park(self, park_token, thread_id=None):
        """Mark the in-flight turn PARKED (suspended at a park_for_model approval, #277)."""
        self._parked = {"token": park_token, "thread_id": thread_id}

    def is_parked(self) -> bool:
        return self._parked is not None

    def parked_token(self):
        return self._parked["token"] if self._parked else None

    def unpark(self):
        """Clear ONLY the parked marker (the turn may resume in-flight or complete).
        NOTE: this does NOT clear _in_flight — final completion must call turn_completed()."""
        self._parked = None

    def is_busy(self) -> bool:
        return self._in_flight or self._parked is not None

    def busy_error(self) -> dict:
        """Return an error dict for a second concurrent tools/call.

        Returns {"error": str} — the same shape as every other error path in
        codex_run_v2, so the dispatcher's content-wrapping delivers it as a
        normal MCP tool-error result (consistent with the 11 other {"error":...}
        returns).  The serial dispatcher loop is the primary concurrency guard;
        this method is defense-in-depth and is rarely/never hit in production.
        A PARKED turn returns a distinct, actionable message (#277).
        """
        if self._parked is not None:
            return {"error": "codex turn parked — resume with codex_approve or wait"}
        return {"error": "codex turn already in flight"}

    def eof_error(self) -> dict:
        """App-server died mid-turn: clear state, return error dict for the pending CC call.

        Returns {"error": str} — same shape as every other error path in
        codex_run_v2.  State is cleared so the child can respawn on next call.
        """
        self._in_flight = False
        self._pending_cc_id = None
        self._parked = None                          # child died → no park survives (#277)
        return {"error": "codex app-server exited mid-turn (child will respawn on next call)"}


# ── v2 codex_run tool ─────────────────────────────────────────────────────
# Task 5: codex_run_v2 — the integration capstone wiring manager + approval
# bridge + reactor + state machine into the MCP tool.
# ─────────────────────────────────────────────────────────────────────────

def _sandbox_policy(sandbox: str, cwd: str) -> dict:
    """Convert a SandboxMode string to a SandboxPolicy object (turn/start wire format).

    NEVER pass the bare sandbox string as sandboxPolicy — the app-server expects
    the object union (verified vs codex 0.141 v2/SandboxPolicy.ts).
    """
    if sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if sandbox == "workspace-write":
        return {
            "type": "workspaceWrite",
            "writableRoots": [cwd],
            "networkAccess": False,
            "excludeTmpdirEnvVar": False,
            "excludeSlashTmp": False,
        }
    if sandbox == "danger-full-access":
        return {"type": "dangerFullAccess"}
    # Unknown value: fall back to read-only (safe default)
    return {"type": "readOnly", "networkAccess": False}


def _turn_input(prompt: str) -> list:
    """Build the turn/start `input` Array<UserInput> for a text prompt.

    A text UserInput requires `text_elements` (snake_case) per v2/UserInput.ts.
    """
    return [{"type": "text", "text": prompt, "text_elements": []}]


# Module-level singleton manager and state machine (shared across MCP tool calls)
_v2_manager: "AppServerManager | None" = None
_v2_state_machine = TurnStateMachine()
_ACK_TIMEOUT = 10.0   # turn/start ACK setup timeout (injectable for tests; F6)

# Track isolated tmpdirs created by this process for atexit cleanup (best-effort).
_isolated_tmpdirs: list[str] = []


def _cleanup_isolated_tmpdirs():
    for d in _isolated_tmpdirs:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


atexit.register(_cleanup_isolated_tmpdirs)


def _get_manager() -> "AppServerManager":
    global _v2_manager
    if _v2_manager is None:
        _v2_manager = AppServerManager()   # bin=None → resolve codex lazily at spawn (#227 item 1c)
    return _v2_manager


# codex_info query → (app-server method, params). These answer at CONNECTION level
# (no thread/start, no cold-start). account/* take NO params (undefined on the wire);
# the rest take {} (verified live, codex 0.141).
_INFO_QUERY_MAP = {
    "models":   ("model/list", {}),
    "auth":     ("getAuthStatus", {}),
    "config":   ("config/read", {}),
    "limits":   ("account/rateLimits/read", None),
    "usage":    ("account/usage/read", None),
    "servers":  ("mcpServerStatus/list", {}),
    "features": ("experimentalFeature/list", {}),
    "profiles": ("permissionProfile/list", {}),
}

# Operational config knobs surfaced by codex_info query="config" (whitelist). A discovery
# tool returns a STABLE SMALL projection — NOT the full ~/.codex config, whose raw
# `config/read` (origins map + projects/tui/hooks/marketplaces) blows the MCP token limit
# (~71K). New/unknown keys appear in `omitted` so they are never silently hidden
# (consult MINOR-FIXES 2026-06-21). Pinned by test_codex_info_config_is_compact_projection.
_CONFIG_OPERATIONAL_KEYS = (
    "model", "review_model", "web_search", "model_provider", "approvals_reviewer",
    "model_context_window", "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "sandbox_mode", "approval_policy", "model_auto_compact_token_limit",
)


def _project_config(raw: dict) -> dict:
    """Compact projection of `config/read` for the discovery tool: keep the whitelisted
    operational knobs, list the omitted top-level config keys (visibility, not silence),
    drop the huge `origins` map. NOT the full config."""
    full = raw.get("config") if isinstance(raw, dict) else None
    if not isinstance(full, dict):
        # Fail-CLOSED on unexpected shape: do NOT `return raw` — config/read carries the
        # ~71K origins map, so passing it through would re-introduce the token blowout (#6).
        return {"config": {}, "omitted": [], "note": "unexpected config/read shape; raw suppressed"}
    kept = {k: full.get(k) for k in _CONFIG_OPERATIONAL_KEYS if k in full}
    omitted = sorted(k for k in full if k not in _CONFIG_OPERATIONAL_KEYS)
    return {"config": kept, "omitted": omitted}


def codex_info_v2(args: dict, manager: "AppServerManager | None" = None) -> dict:
    """Connection-level read methods (discovery/introspection). No thread/start,
    no cold-start. REUSES the live app-server child if one is alive and does NOT
    change its isolation signature — so it never disrupts a warm codex_run session.
    When NO child is alive it spawns a default (all-servers) connection and sets that
    signature; a subsequent codex_run with a different mcp mode will then respawn
    (#3 — unavoidable: codex_info needs SOME live connection, reads are isolation-
    independent). Returns {query, result}."""
    acc: list = []
    query = args.get("query")
    if query == "approval":   # #277 §8: purely-local knob read-out — no app-server, no cold-start, no spawn
        return {"query": "approval", "result": _approval_knobs()}
    mapping = _INFO_QUERY_MAP.get(query) if isinstance(query, str) else None
    if mapping is None:
        return {"error": f"unknown query {query!r}; expected one of {sorted(_INFO_QUERY_MAP)}"}
    spawned_default = manager is None
    if manager is None:
        manager = _get_manager()   # construction does not spawn — needs no codex binary
    method, params = mapping

    def _respawn_allowed() -> bool:
        # A respawn needs the codex binary — but the check applies ONLY on the singleton
        # (manager-was-None) path, whose `_bin` IS the global codex. An explicit manager
        # owns its own `_bin` (ensure() spawns from that), so global codex must not block it
        # (#226 D). Resolve the binary lazily so a mid-session install/upgrade is seen (#227).
        return not (spawned_default and not _codex_bin_available())

    # Initial spawn (only when no child is alive). Kept OUT of the read's retry scope so an
    # initial-spawn failure surfaces ONCE — the #227 item-2 retry is for a WARM child dying
    # mid-read, NOT for a spawn that never produced a usable child (panel finding B).
    if not _is_child_alive(manager._child):
        # #225 P3: the codex binary is only needed to SPAWN a fresh connection. A live child
        # (e.g. left warm by a prior codex_run) is reused as-is — even if the codex symlink was
        # removed mid-session (an upgrade) — so gate the binary check on the spawn path only.
        if not _respawn_allowed():
            _info_error_log(query, "codex binary not found")
            return {"error": f"codex binary not found at '{_resolve_codex_bin()}'. Install codex or set JAINE_CODEX_BIN."}
        try:
            manager.ensure([])   # default connection (no isolation) only if none alive
        except Exception as e:
            _info_error_log(query, e)
            return _stamp_drift({"error": f"{method} failed: {e}"}, acc)

    try:
        result = manager.connection_request(method, params)
    except Exception as e:
        # #227 item 2: a warm child can die DURING connection_request (alive at the check,
        # dead by the read). Self-heal with ONE respawn+retry — but ONLY when the child is
        # now actually dead (a live-child error is a real protocol/timeout error: surface it,
        # don't mask) and a respawn is permitted (binary present on the singleton path).
        if _is_child_alive(manager._child) or not _respawn_allowed():
            _info_error_log(query, e)
            return _stamp_drift({"error": f"{method} failed: {e}"}, acc)
        try:
            manager.ensure([])
            result = manager.connection_request(method, params)
        except Exception as e2:
            _info_error_log(query, f"after respawn-retry: {e2}")
            return _stamp_drift({"error": f"{method} failed after respawn-retry: {e2}"}, acc)
    if query == "config":
        result = _project_config(result)
    return _stamp_drift({"query": query, "result": result}, acc)


def _parse_review_target(target):
    """Parse a codex_review `target` string into a ReviewTarget union (codex 0.141):
    'uncommitted' → uncommittedChanges; 'branch:<name>' → baseBranch;
    'commit:<sha>' → commit; 'custom:<instructions>' → custom. None if invalid."""
    if not isinstance(target, str):
        return None
    t = target.strip()
    if t in ("", "uncommitted", "uncommittedChanges"):
        return {"type": "uncommittedChanges"}
    if t.startswith("branch:") and t[len("branch:"):].strip():
        return {"type": "baseBranch", "branch": t[len("branch:"):].strip()}
    if t.startswith("commit:") and t[len("commit:"):].strip():
        return {"type": "commit", "sha": t[len("commit:"):].strip(), "title": None}
    if t.startswith("custom:") and t[len("custom:"):].strip():
        return {"type": "custom", "instructions": t[len("custom:"):].strip()}
    return None


def codex_review_v2(args: dict, manager: "AppServerManager | None" = None,
                    cc_write_fn=None, cc_read_fn=None,
                    state_machine: "TurnStateMachine | None" = None) -> dict:
    """Native git-aware codex review (review/start). Thin wrapper over codex_run_v2:
    parse `target`, run a read-only review turn, return {thread_id, review, ...}
    (free-text findings). cwd should point at the git repo to review."""
    target = _parse_review_target(args.get("target", "uncommitted"))
    if target is None:
        return {"error": (
            f"invalid review target {args.get('target')!r}; use 'uncommitted' | "
            "'branch:<name>' | 'commit:<sha>' | 'custom:<instructions>'"
        )}
    run_args = dict(args)
    run_args["_review_target"] = target
    run_args["mode"] = "implement"   # free-text output (no outputSchema)
    run_args["sandbox"] = args.get("sandbox", "read-only")
    # #12: review/start (ReviewStartParams) carries NO effort/model, and start_thread
    # sends neither — and the review path sends review/start INSTEAD of turn/start, so
    # turn-level effort/model never reach the wire. Route them through the thread `config`
    # (the one channel start_thread DOES send): model_reasoning_effort / model are real
    # codex config keys (not in _CONFIG_DENY). Without this, effort/model are silently
    # ignored and the review always runs at the codex config default.
    cfg = dict(args.get("config") or {})
    if args.get("effort"):
        cfg.setdefault("model_reasoning_effort", args["effort"])
    # #225 P2 / #226 A: native review (review/start) prefers the review-specific `review_model`
    # over the thread `model`, so config.model alone is silently ignored whenever a
    # `review_model` is configured. Make the public `model` arg authoritative: set BOTH
    # config.model and config.review_model to the effective model (arg first, else the
    # caller's config.model), overriding any caller config so the two never diverge.
    eff_model = args.get("model") or cfg.get("model")
    if eff_model:
        cfg["model"] = eff_model
        cfg["review_model"] = eff_model
    if cfg:
        run_args["config"] = cfg
    # prompt is unused for review (review/start carries its own), but codex_run_v2
    # requires a non-empty prompt — supply a descriptive placeholder.
    run_args.setdefault("prompt", f"native review: {args.get('target', 'uncommitted')}")
    r = codex_run_v2(run_args, manager=manager, cc_write_fn=cc_write_fn,
                     cc_read_fn=cc_read_fn, state_machine=state_machine)
    # implement-shape carries free text in `result`; rename to `review` for clarity.
    if isinstance(r, dict) and "error" not in r and "result" in r:
        r["review"] = r.pop("result")
    return r


def _foreign_thread(ts: dict, params: dict) -> bool:
    """True when a frame belongs to a thread OUTSIDE this turn's accepted set (#349 r1:
    ultra auto-delegation streams CHILD threads' items on the same connection — their
    text must not enter the root assembly, and their turn/completed must not end OUR
    pump). Accepted set = {root thread} ∪ {reviewThreadId} (captured at the start ACK).
    Missing frame threadId or an unknowable set → NOT foreign (defensive accept)."""
    fr = params.get("threadId")
    if not fr:
        return False
    allowed = ts.get("accepted_thread_ids")
    if not allowed:
        tid = ts.get("thread_id")
        if not tid:
            return False
        allowed = {tid}
    return fr not in allowed


def _assembled_message(ts: dict) -> str:
    """Assemble the turn's message text per agentMessage ITEM (#349). effort=ultra
    auto-delegation streams several items CONCURRENTLY — a flat join of deltas
    interleaves them delta-by-delta into unreadable mush, so deltas are keyed by
    itemId and joined per item (first-seen order, blank line between items). A
    completed item's full .text is authoritative over that item's deltas. Falls back
    to the legacy flat join when no per-item data was recorded (hand-built ts)."""
    order = ts.get("msg_item_order") or []
    if not order:
        return "".join(ts["final_message_parts"])
    blocks = []
    for iid in order:
        final = ts.get("msg_item_final", {}).get(iid)
        text = final if final is not None else "".join(ts.get("msg_item_parts", {}).get(iid, []))
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def _build_interrupted_result(ts: dict, interrupted_by: str, thread_warm: bool = True) -> dict:
    """Graceful, resumable result for an interrupted turn (#218 F7). Mode-shaped (so a
    review/implement/codex_review caller still gets its keys) with interrupt metadata and NO
    'error' key — the dispatcher marks isError iff 'error' in res, so an interrupt stays a
    graceful partial, not a failure."""
    partial = _assembled_message(ts)             # per-item assembly (#349)
    meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                              ts["mcp_mode"], ts["mcp_servers_enabled"],
                              ts["effort_val"], ts["model_val"], "interrupted", ts=ts)
    _interrupt_log(ts, interrupted_by, thread_warm)
    res = _shape_result(ts["mode"], ts["thread_id"], partial, meta)
    res["status"] = "interrupted"          # top-level status (overrides meta's status key too)
    res["interrupted_by"] = interrupted_by
    res["partial_text"] = partial
    res["thread_warm"] = thread_warm
    return res


_INTERRUPT_COMPLETE_TIMEOUT = 10.0  # bounded wait for turn/completed after turn/interrupt (#218 F6)


def _run_interrupt(manager, ts: dict, turn_id, interrupted_by: str) -> dict:
    """Stop the in-flight turn cleanly and return a graceful, resumable result (#218).

    Teardown invariant (R3-F1): the no-turnId and no-completion branches kill the child
    (→ respawn next call) and return thread_warm=False. The caller clears the TurnStateMachine."""
    ts["interrupting"] = True
    ts["interrupted_by"] = interrupted_by

    def _teardown():
        try:
            if manager._child is not None:
                manager._child.kill()
        except Exception:
            pass
        manager._child = None
        return _build_interrupted_result(ts, interrupted_by, thread_warm=False)

    if not turn_id:
        return _teardown()
    # turn/interrupt is a REQUEST (app-server replies an empty {} response), NOT a notification —
    # it MUST carry an id (R2-F1; verified vs the app-server schema and turn_interrupt_probe.py).
    iid = manager._next_id()
    manager._write({"id": iid, "method": "turn/interrupt",
                    "params": {"threadId": ts["thread_id"], "turnId": turn_id}})
    deadline = time.time() + _INTERRUPT_COMPLETE_TIMEOUT
    while time.time() < deadline:
        for frame in manager._reactor.pump(timeout=0.2):
            if "__cc__" in frame:          # ignore CC frames during the interrupt drain
                continue
            if frame.get("id") == iid:     # the empty {} response to turn/interrupt — consume, don't handle
                continue
            res = _handle_child_frame(frame, ts)
            if res is not None:
                return res                 # the interrupting branch → graceful result
    return _teardown()                     # no turn/completed within the bound


def _handle_child_frame(frame: dict, ts: dict):
    """Process one app-server (child) frame against turn-state `ts`.

    Returns None to continue the turn, or a result dict to terminate it. Shared by the turn
    pump and the #252 approval-wait drain so a frame is handled identically wherever it is read
    (extracted verbatim from the codex_run_v2 Phase-2 body; the only addition is the #218
    `interrupting` routing). Only NOTIFICATION frames are acted on — a response/request frame
    returns None (so it is safe to call on any drained child frame)."""
    if classify(frame) != "notification":
        return None
    method = frame.get("method", "")
    if method == "item/agentMessage/delta":
        p = frame.get("params", {})
        if _foreign_thread(ts, p):               # a delegated child thread's stream (#349 r1)
            return None
        # `or ""`: a present-but-null delta returns None from .get(k, "") (#18)
        d = p.get("delta") or ""
        ts["final_message_parts"].append(d)      # raw stream order — the #224 narrative cursor
        iid = p.get("itemId") or ""              # required per schema; "" = defensive bucket (#349)
        parts = ts.setdefault("msg_item_parts", {})
        if iid not in parts:
            parts[iid] = []
            ts.setdefault("msg_item_order", []).append(iid)
        parts[iid].append(d)
        return None
    if method == "item/completed":
        # A completed agentMessage carries the item's FULL text — authoritative over that
        # item's deltas for ALL turns (#349; native review delivers ONLY this, never deltas).
        if _foreign_thread(ts, frame.get("params", {})):   # child thread's item (#349 r1)
            return None
        _it = frame.get("params", {}).get("item", {}) or {}
        if _it.get("type") == "agentMessage":
            iid = _it.get("id") or ""
            if iid not in ts.setdefault("msg_item_parts", {}):
                ts["msg_item_parts"][iid] = []
                ts.setdefault("msg_item_order", []).append(iid)
            ts.setdefault("msg_item_final", {})[iid] = _it.get("text") or ""   # null-safe (#18)
        return None
    if method == "thread/tokenUsage/updated":
        tu = frame.get("params", {}).get("tokenUsage")
        if isinstance(tu, dict):
            ts["usage_snapshot"] = tu
        return None
    if method == "turn/completed":
        if _foreign_thread(ts, frame.get("params", {})):   # a child turn ending must not end OURS (#349 r1)
            return None
        t = frame.get("params", {}).get("turn", {}) or {}
        # An interrupted turn (no error) is GRACEFUL, not a failure — route it to the graceful
        # result, bypassing the generic terminal-failure arm below. Two sources, both deliberate:
        #   - #218: an interrupt WE initiated (Esc / opt-in timeout) — ts["interrupting"] set;
        #   - #287: the user picked "Cancel the turn" in an approval → codex cancels →
        #     status="interrupted" with ts["interrupting"] still False.
        # ("decline" CONTINUES the turn, so interrupted-without-error always means a deliberate stop.)
        if t.get("status") == "interrupted" and not t.get("error"):
            return _build_interrupted_result(ts, interrupted_by=ts.get("interrupted_by", "cancel"))
        if t.get("status") != "completed" or t.get("error"):  # TurnStatus has no "success" (codex 0.141: completed/interrupted/failed/inProgress)
            meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                      ts["mcp_mode"], ts["mcp_servers_enabled"],
                                      ts["effort_val"], ts["model_val"], "failed", ts=ts)
            _turn_error_log(ts, f"turn failed: status={t.get('status')!r} error={t.get('error')!r}")
            return {"error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                    "thread_id": ts["thread_id"], **meta}
        meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "completed", ts=ts)
        if ts["retries"]:
            meta["retries"] = ts["retries"]
        _turn_ok_log(ts, meta)
        return _shape_result(ts["mode"], ts["thread_id"], _assembled_message(ts), meta)   # #349
    if method == "error":
        # willRetry:true = transient stream reconnect (codex retries) → NOT terminal, NOT drift.
        is_terminal, emsg = _classify_error_notification(frame.get("params", {}) or {})
        if not is_terminal:
            ts["retries"] += 1
            return None
        meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "failed", ts=ts)
        _turn_error_log(ts, emsg)
        return {"error": f"codex error: {emsg or 'unknown error'}",
                "thread_id": ts["thread_id"], **meta}
    if method == "item/started":
        # #279: a fileChange item's diff arrives HERE — item={type:'fileChange', id, changes:[{path,
        # kind, diff}], status} — BEFORE the item/fileChange/requestApproval, and codex 0.142 does NOT
        # emit item/fileChange/patchUpdated before the park (empirically proven, bd279_probe). Capture it
        # keyed by item.id (== the parked approval's itemId) into the SAME store build_awaiting_payload
        # reads, so a parked fileChange shows the diff instead of the 'no diff captured' note. Other item
        # types (userMessage/reasoning/command) → no-op. A later patchUpdated (if any) OVERWRITES (full set).
        it = (frame.get("params") or {}).get("item")
        if isinstance(it, dict) and it.get("type") == "fileChange" and it.get("id"):
            ts.setdefault("file_changes", {})[it["id"]] = {
                "changes": it.get("changes") or [], "turn_id": (frame.get("params") or {}).get("turnId")}
        return None
    if method == "item/fileChange/patchUpdated":
        # #277 (C16): capture the file-change diff so a PARKED item/fileChange approval can show it
        # to the model. The REQUEST (FileChangeRequestApprovalParams, codex 0.142) carries NO patch —
        # only itemId/reason/grantRoot/threadId/turnId — so the diff MUST come from this notification.
        # Verified shape (R2-F4, generate-json-schema 0.142): params = {changes:[{diff, kind:{type:
        # add|delete|update}, path}], itemId, threadId, turnId}. patchUpdated carries the FULL current
        # change set → OVERWRITE (not append). A create/delete carries path+kind+diff too, so there is
        # no separate "no-patch" gap. (item/fileChange/outputDelta is DEPRECATED — codex 0.142 "no
        # longer emits" it — so it is NOT accumulated; it falls through to the known-ignored no-op.)
        p = frame.get("params", {}) or {}
        item_id = p.get("itemId")
        if item_id:
            ts.setdefault("file_changes", {})[item_id] = {
                "changes": p.get("changes") or [], "turn_id": p.get("turnId")}
        return None
    if method == "warning":
        # #320: an explicit codex signal (e.g. capacity degradation) — log the payload,
        # keep it out of the _drift channel (it is not protocol drift). Non-terminal.
        _warning_log(frame.get("params") or {})
        return None
    if method == "model/rerouted":
        # #321 r2: capture the EFFECTIVE model so a later TURN_ERROR is attributed to the
        # model that actually ran, not the requested one. Informational — the turn continues.
        to = (frame.get("params") or {}).get("toModel")
        if isinstance(to, str) and to:
            ts["rerouted_model"] = to
        return None
    if method not in _KNOWN_NOTIFICATIONS:
        _drift_warn(ts.get("acc"), "UNKNOWN_NOTIFICATION", method)
    # known-but-ignored (turn/started, item/completed non-review, ...) → no-op
    return None


def _route_cc_frame(frame, cc_id, reply_fn) -> str:
    """Route a mid-turn CC frame (#218). Returns 'interrupt' (our cancel — CC alive),
    'teardown' (stdin EOF — CC gone), or 'continue'. Only REQUEST-shaped id-bearing frames
    are answered (R1-F3); a response-shaped frame mid-turn is not ours to answer → ignored.
    Id-bearing requests get the SAME CC-facing envelopes main() writes (R3-F2)."""
    if not isinstance(frame, dict):
        return "continue"                       # unparseable CC line
    if frame.get("__eof__"):                     # CC stdin closed (e.g. CC tool-call timeout)
        return "teardown"                        # R1-F1: CC gone → teardown (cold); don't wait for ACK
    method = frame.get("method", "")
    if method == "notifications/cancelled":
        if (frame.get("params") or {}).get("requestId") == cc_id:
            return "interrupt"
        return "continue"
    if classify(frame) != "request":            # R1-F3: response/notification → not ours to answer
        return "continue"
    mid = frame.get("id")
    # id-bearing REQUEST → MUST answer (CC would block otherwise)
    if method == "ping":
        reply_fn(mid, result={})
    elif method == "tools/list":
        reply_fn(mid, result={"tools": TOOLS})
    elif method == "tools/call":
        busy = _v2_state_machine.busy_error()   # {"error": "codex turn already in flight"}
        reply_fn(mid, result={"content": [{"type": "text", "text": json.dumps(busy)}],
                              "isError": True})
    else:
        reply_fn(mid, error={"code": -32601,
                             "message": f"server busy; method {method!r} not serviced mid-turn"})
    return "continue"


def _interrupts_enabled() -> bool:
    """#218: interrupts (Esc-cancel + opt-in-timeout-graceful) are default-ON; the
    BULLDOZER_CODEX_NO_INTERRUPT kill-switch disables them. The #252 approval-wait child
    drain is independent and stays ON regardless (see the approval bridge)."""
    return not os.environ.get("BULLDOZER_CODEX_NO_INTERRUPT")


def _finish_interrupt(manager, ts: dict, turn_id, interrupted_by: str, state_machine) -> dict:
    """Run the interrupt routine, then clear the TurnStateMachine (centralized state-clear so
    every interrupt branch leaves the bridge ready for the next call)."""
    res = _run_interrupt(manager, ts, turn_id, interrupted_by)
    state_machine.turn_completed()
    return res


def _log_kill_switch_once() -> None:
    """Best-effort: log ONCE (per process) when the #218 interrupt kill-switch is set, to the
    stable codex log. No-op when interrupts are enabled. Never raises (F8)."""
    if getattr(_log_kill_switch_once, "_done", False) or _interrupts_enabled():
        return
    _log_kill_switch_once._done = True
    # single shared writer (#322 PR2): same line format, one open/append path
    _drift_warn(None, "INTERRUPT_DISABLED", "BULLDOZER_CODEX_NO_INTERRUPT set")


def codex_approve_v2(args: dict, manager: "AppServerManager | None" = None,
                     cc_write_fn=None, cc_read_fn=None,
                     state_machine: "TurnStateMachine | None" = None) -> dict:
    """Resume a turn parked at a park_for_model approval (#277). THIN (spec §5.2):
      1. validate park_token == manager._parked["park_token"] (else 'parked turn expired', park UNCHANGED);
      2. validate decision_id ∈ the parked payload's decisions[].id (or 'decline') BEFORE gen.send — an
         unknown id → a RETRYABLE error, park UNCHANGED, NO gen.send/child write (F3);
      3. gen.send(decision_id); generator yields again → RE-PARK; StopIteration → turn_completed()
         (clears _in_flight AND _parked, NOT unpark() alone — F1) + manager._parked=None + final dict.
    All the heavy resume work (drain / credit / build / write) lives INSIDE the generator (Task 6)."""
    if manager is None:
        manager = _get_manager()
    if state_machine is None:
        state_machine = _v2_state_machine
    parked = manager._parked
    token = args.get("park_token")
    if not isinstance(parked, dict) or token != parked.get("park_token"):
        return {"error": "parked turn expired"}        # stale/absent/double-resume → park UNCHANGED
    decision_id = args.get("decision_id")
    valid = set(parked.get("decision_ids") or ()) | {"decline"}
    if decision_id not in valid:                        # hallucinated id → retryable, park UNCHANGED (F3)
        return {"error": f"unknown decision_id: {decision_id!r}"}
    gen = parked["inner_gen"]
    ctx = parked.get("ctx") or {}
    acc = ctx.get("acc")
    # Rebind the active _cc_id to THIS resume call so an Esc during the resume leg is matched (C6).
    if isinstance(ctx.get("args"), dict):
        ctx["args"]["_cc_id"] = args.get("_cc_id")
    try:
        payload = gen.send(decision_id)
    except StopIteration as e:
        state_machine.turn_completed()                 # clears _in_flight AND _parked (F1)
        manager._parked = None
        return e.value                                 # already _stamp_drift'd inside the generator
    except Exception as e:                             # defensive — a resume crash must not strand the park
        _turn_error_log(ctx.get("ts") or {}, f"resume error: {e}")
        state_machine.turn_completed()
        manager._parked = None
        return _stamp_drift({"error": f"resume error: {e}"}, acc)
    # generator yielded AGAIN (multi-approval turn) → RE-PARK (new request_frame/decision_ids/park_token
    # were set into ctx by the generator's second yield).
    manager._parked = {
        "park_token": ctx.get("park_token"), "thread_id": parked.get("thread_id"), "inner_gen": gen,
        "isolation_sig": manager._isolation_sig, "started_at": time.monotonic(),
        "request_frame": ctx.get("request_frame"), "decision_ids": ctx.get("decision_ids"),
        "ctx": ctx,
    }
    state_machine.park(ctx.get("park_token"), parked.get("thread_id"))
    return _stamp_drift(payload, acc)


def _parked_busy_block(tool_name, state_machine) -> bool:
    """#277 C12 — the global parked guard. While a turn is PARKED, every tool EXCEPT codex_approve
    busy-blocks (park PRESERVED): a fresh codex_run / codex_info / codex_review would otherwise touch
    the parked child and steal the frames the suspended generator needs. codex_approve is routed by
    NAME to codex_approve_v2, which validates the token (a stale token → 'parked turn expired', not
    the busy path), so it is the ONLY tool allowed through while parked."""
    return state_machine.is_parked() and tool_name != "codex_approve"


_PARK_CAP_S_DEFAULT = 1800.0   # #277 §8: wall-clock cap on a parked turn (30 min)


def _park_cap_s() -> float:
    """The parked-turn wall-clock cap in seconds (#277 §7). Env BULLDOZER_PARK_CAP_S, read fresh per
    park; malformed → default; clamped to a sane range."""
    try:
        v = float(os.environ.get("BULLDOZER_PARK_CAP_S") or _PARK_CAP_S_DEFAULT)
    except (TypeError, ValueError):
        v = _PARK_CAP_S_DEFAULT
    return max(1.0, min(v, 86400.0))


def _approval_knobs() -> dict:
    """The effective #277 unattended/approval knobs + their source — computed by CALLING the live
    accessors (still fresh-per-call), for codex_info(query='approval') discoverability (§8). Knobs:
    BULLDOZER_APPROVAL_UNATTENDED (truthy) / *_FILE sentinel arm the model-in-the-loop;
    BULLDOZER_PARK_CAP_S (default 1800) is the parked wall-clock cap; BULLDOZER_FAST_PATH_SCOPE
    ('reads' | 'local-work') is the trivial fast-accept breadth. All resolved FRESH per approval."""
    env = os.environ.get("BULLDOZER_APPROVAL_UNATTENDED")
    sentinel = os.environ.get("BULLDOZER_APPROVAL_UNATTENDED_FILE") or os.path.expanduser(
        _UNATTENDED_SENTINEL_DEFAULT)
    active = _unattended_active()
    if env is not None and env.strip().lower() in _TRUTHY:
        source = "env"
    elif active:
        source = "sentinel-file"
    else:
        source = "off"
    # R1-F8 / R2-F2: each knob reports its EFFECTIVE (normalized) value + whether a VALID env value drove
    # it. An INVALID env (FAST_PATH_SCOPE=garbage / PARK_CAP_S=notanumber) falls back → source 'default',
    # and the reported value is the effective one (not the raw invalid string).
    raw_scope = os.environ.get("BULLDOZER_FAST_PATH_SCOPE")
    effective_scope = "local-work" if _fast_path_scope() == "local-work" else "reads"
    fast_path_scope_source = "env" if raw_scope in ("reads", "local-work") else "default"
    try:
        float(os.environ.get("BULLDOZER_PARK_CAP_S"))
        park_cap_source = "env"
    except (TypeError, ValueError):
        park_cap_source = "default"                  # absent / malformed → _park_cap_s fell back
    try:
        int(os.environ.get("BULLDOZER_APPROVAL_NARRATIVE_MAX"))
        narrative_max_source = "env"                 # a clamped-but-valid env still counts as env-driven
    except (TypeError, ValueError):
        narrative_max_source = "default"             # absent / malformed → _approval_narrative_max fell back
    # #340: approval UI (cc | dialog) + how it was selected.
    ui_env = (os.environ.get("BULLDOZER_APPROVAL_UI") or "").strip().lower()
    dialog_sentinel = os.environ.get("BULLDOZER_APPROVAL_DIALOG_FILE") or os.path.expanduser(
        _APPROVAL_DIALOG_SENTINEL_DEFAULT)
    machine_sentinel = (os.environ.get("BULLDOZER_APPROVAL_DIALOG_MACHINE_FILE")
                        or _APPROVAL_DIALOG_MACHINE_SENTINEL_DEFAULT)
    approval_ui = _approval_ui()
    if ui_env in ("dialog", "cc", "tui"):
        approval_ui_source = "env"
    elif approval_ui == "dialog":
        try:
            user_armed = os.path.exists(dialog_sentinel)
        except OSError:
            user_armed = False
        approval_ui_source = "sentinel-file" if user_armed else "machine-sentinel"
    else:
        approval_ui_source = "default"
    return {
        "unattended": active,
        "unattended_source": source,
        "approval_ui": approval_ui,
        "approval_ui_source": approval_ui_source,
        "approval_ui_sentinel_path": dialog_sentinel,
        "approval_ui_machine_sentinel_path": machine_sentinel,
        "park_cap_s": _park_cap_s(),
        "park_cap_source": park_cap_source,
        "fast_path_scope": effective_scope,
        "fast_path_scope_source": fast_path_scope_source,
        "narrative_max_chars": _approval_narrative_max(),
        "narrative_max_source": narrative_max_source,
        "sentinel_path": sentinel,
        # dialog localization (#247 + provider selector): language + the provider chain.
        "translate_lang": _approval_lang(),
        "translate_provider": _translate_providers(),
    }


def _teardown_park(manager, state_machine, reason: str) -> None:
    """Tear down a parked turn (#277 C7/C8). The parked child is BLOCKED awaiting the approval reply,
    so turn/interrupt alone is useless — AUTO-DECLINE the pending approval FIRST (unblocks the child),
    THEN kill the child (respawn next call), THEN clear state via turn_completed() (NOT unpark() alone —
    else _in_flight stays True forever and deadlocks the next tool). Best-effort: NEVER raises (main()'s
    parked wait has no tool-call try/except; a dead-child decline write is BrokenPipe-guarded, C8b)."""
    parked = manager._parked if isinstance(getattr(manager, "_parked", None), dict) else None
    if parked is not None and reason != "child-terminal":
        # #321 r3: a parked turn dying (cap / cc-eof / child-death) is a terminal failure —
        # audit it. child-terminal is EXCLUDED: an error frame was already audited by
        # _handle_child_frame, and a completed-during-park turn is a delivery loss, not an error.
        _ctx = parked.get("ctx")
        _pts = (_ctx or {}).get("ts") if isinstance(_ctx, dict) else None
        _turn_error_log(_pts if isinstance(_pts, dict) else {"manager": manager},
                        f"parked turn torn down: {reason}")
    if parked is not None:
        req = parked.get("request_frame")
        if isinstance(req, dict):
            try:
                manager._write(build_decision_response(req, "decline"))   # unblock the child first
            except Exception:
                pass                                                       # dead child / broken pipe → ignore
            _log_unattended_decision(req.get("method"), "decline", "teardown:" + reason)   # #280 C: audit
    try:
        if manager._child is not None:
            manager._child.kill()
    except Exception:
        pass
    manager._child = None
    state_machine.turn_completed()      # clears _in_flight AND _parked on the state machine
    manager._parked = None              # clear the manager record too


def _parked_wait(manager, state_machine, cc_write_fn=None):
    """main()'s between-calls read WHILE a turn is parked (#277 §7). CCStream.next_frame selects ONLY on
    sys.stdin — it is BLIND to the child — so each iteration ALSO pumps the child + polls it. CC-EOF /
    child-death / a terminal child frame WIN over the cap (don't mislabel a death as a timeout). On
    cap/EOF/death/terminal with no resume → _teardown_park. Returns (kind, req) like next_frame: a CC
    frame ('frame', req) is handed back to main() with the park PRESERVED (the codex_approve resume is
    incoming — or a wrong-token approve / other tool, handled by codex_approve_v2 / the parked guard);
    a teardown returns ('eof', None) on EOF, else ('none', None) so main()'s loop simply continues."""
    # R1-F2: the cap is anchored to the park's STORED started_at (recomputed each iteration from
    # monotonic) so an unrelated frame returning to main() + re-entering _parked_wait can NOT reset it.
    parked = manager._parked if isinstance(getattr(manager, "_parked", None), dict) else None
    ctx = (parked or {}).get("ctx") or {}
    ts = ctx.get("ts")                                        # the parked turn's turn-state (for terminal detection)
    owner_cc_id = (ctx.get("args") or {}).get("_cc_id")       # the codex_run tools/call id that owns this park
    started = (parked or {}).get("started_at")
    if not isinstance(started, (int, float)):
        started = time.monotonic()                            # defensive fallback (record missing started_at)
    cap = _park_cap_s()
    while True:
        reactor = getattr(manager, "_reactor", None)          # re-read LIVE (a respawn may swap it)
        # child-death / terminal child frame WIN over the cap (R2-F1) — pump the child non-blocking first.
        # R1-F4: route EVERY child notification through _handle_child_frame so a terminal `error` (not just
        # turn/completed) tears down; non-terminal frames (deltas) buffer into ts harmlessly.
        if reactor is not None:
            try:
                for cf in reactor.pump(timeout=0.0):
                    if not isinstance(cf, dict) or "__cc__" in cf:
                        continue
                    if classify(cf) != "notification":
                        # #278: a non-notification child frame (the turn/start ACK, another server
                        # request) that lands while parked must be BUFFERED for the resumed turn loop —
                        # dropping it would falsely time out a pre-ACK approval (parity with the attended
                        # drain at handle_server_request). The resume block re-feeds these survivors.
                        if ts is not None:
                            ts.setdefault("drained_frames", []).append(cf)
                        continue
                    terminal = (_handle_child_frame(cf, ts) if ts is not None
                                else ({} if cf.get("method") == "turn/completed" else None))
                    if terminal is not None:
                        _teardown_park(manager, state_machine, "child-terminal")
                        return ("none", None)
            except Exception:
                pass
        if manager._child is not None and manager._child.poll() is not None:
            _teardown_park(manager, state_machine, "child-death")
            return ("none", None)
        remaining = (started + cap) - time.monotonic()
        if remaining <= 0:
            _teardown_park(manager, state_machine, "cap")     # cap with no resume → graceful decline+kill
            return ("none", None)
        kind, req = _cc_stream.next_frame(min(remaining, 0.2))
        if kind == "eof":
            _teardown_park(manager, state_machine, "eof")     # CC channel closed → tear down
            return ("eof", None)
        if kind == "frame":
            # R1-F3: a parked cancel is a NOTIFICATION main() would drop — handle it HERE. An our-turn
            # cancel (requestId == the parked turn's cc_id) tears down; an UNRELATED cancel is preserved
            # (loop — never returned to main(), never resets the cap).
            if isinstance(req, dict) and req.get("method") == "notifications/cancelled":
                if (req.get("params") or {}).get("requestId") == owner_cc_id:
                    _teardown_park(manager, state_machine, "cancel")
                    return ("none", None)
                continue
            return (kind, req)                                # codex_approve / other tool → main() dispatches it
        # 'none' (timeout slice / partial frame) → loop until cap


def _drive_turn(ctx):
    """#277: the turn-pump loop as an INNER generator (spec §5.1). `codex_run_v2` builds `ctx` and drives
    this with next()/send(). At a park_for_model approval the loop body YIELDS build_awaiting_payload(...)
    and resumes when codex_approve_v2 sends the chosen decision_id back (return-and-resume); on completion
    it RETURNS the final dict (→ StopIteration.value). The review path never reaches a park (read-only).

    Control locals (deadline/ack_deadline/turn_acked/turn_id/cancel_pending/narrative_shown) are
    GENERATOR-locals — a generator frame persists them across the yield natively, so there is NO
    UnboundLocalError (this is a full-block move; the spec's ctx-attr note guards a PARTIAL move where
    setup stays in codex_run_v2 — we don't do that, and a near-verbatim move is the lowest-risk way to
    preserve the intricate #218/#252 logic). `ctx` carries the loop inputs + the cross-boundary park
    outputs (request_frame / decision_ids / park_token) codex_run_v2 reads after a yield. Deadlines use
    time.monotonic so a wall-clock shift can't trip them (spec §5.2). Every #218/#252 branch is verbatim."""
    manager = ctx["manager"]
    ts = ctx["ts"]
    args = ctx["args"]
    acc = ctx["acc"]
    cc_write_fn = ctx["cc_write_fn"]
    cc_read_fn = ctx["cc_read_fn"]
    state_machine = ctx["state_machine"]
    thread_id = ctx["thread_id"]
    review_target = ctx["review_target"]
    turn_params = ctx["turn_params"]
    reactor = manager._reactor
    narrative_shown = 0   # #224: char offset into the joined narrative already shown in a prior approval

    try:
        # Send turn/start; then run a unified pump loop (Phase 1 = ACK wait, Phase 2 = event stream).
        # CRITICAL: do NOT use _pump_until for turn/start — it discards same-chunk frames after the
        # matching response (the fake/real codex can flush ACK + delta + turn/completed together).
        mid = manager._next_id()
        start_method = "review/start" if review_target is not None else "turn/start"
        if review_target is not None:
            manager._write({"id": mid, "method": "review/start", "params": {
                "threadId": thread_id, "target": review_target, "delivery": "inline"}})
        else:
            manager._write({"id": mid, "method": "turn/start", "params": turn_params})

        # No work-duration cap by default (match stock; opt-in `timeout` re-imposes one). The ACK
        # timeout is a SETUP check (the engine must answer turn/start), distinct from limiting WORK.
        turn_acked = False
        turn_timeout = args.get("timeout")
        deadline = (time.monotonic() + turn_timeout) if turn_timeout else None
        ack_deadline = time.monotonic() + _ACK_TIMEOUT
        _log_kill_switch_once()                  # F8: note once if the kill-switch disabled interrupts
        watch = _interrupts_enabled() and args.get("_cc_id") is not None
        turn_id = None
        cancel_pending = False

        while deadline is None or time.monotonic() < deadline:
            frames = reactor.pump(timeout=0.2, watch_cc=watch)
            if ts.get("drained_frames"):
                frames = ts.pop("drained_frames") + frames
            if any(isinstance(f, dict) and isinstance(f.get("__cc__"), dict) and f["__cc__"].get("__eof__")
                   for f in frames):
                return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
            for frame in frames:
                if not isinstance(frame, dict):
                    continue                                   # bare non-dict JSON line from the child (F3)
                if "__cc__" in frame:                          # CC-side frame (cancel/other; EOF handled above)
                    if _route_cc_frame(frame["__cc__"], cc_id=args.get("_cc_id"), reply_fn=reply) == "interrupt":
                        cancel_pending = True                  # defer to END-OF-BATCH (capture same-batch work)
                    continue
                kind = classify(frame)
                method = frame.get("method", "")

                if kind == "request":
                    # #277: route the FIVE approval methods when armed (or test-forced). The decision is
                    # taken HERE in the loop body — the only place `yield` is legal and the sole
                    # handle_server_request call site. park_for_model BYPASSES handle_server_request (no
                    # child reply; yields); fast_accept / fail_closed_decline answer inline via
                    # build_decision_response (NOT bridge_approval — R5-F1); an unarmed approval or a
                    # non-approval request falls through to the UNCHANGED attended path below.
                    _route = None
                    if method in _APPROVAL_METHODS and (ctx.get("_force_park_route") or _unattended_active()):
                        if ctx.get("_force_park_route"):
                            _route = "park_for_model"          # Task 6 test override — force a park
                        else:
                            _rp = frame.get("params")
                            _route = route_approval(
                                method, _rp, (_rp or {}).get("cwd") if isinstance(_rp, dict) else None)
                    if _route == "fast_accept":                # trivial command → inline accept, no model
                        manager._write(build_decision_response(frame, "accept"))
                        _log_unattended_decision(method, "accept", "fast_path")    # #280 C: audit
                        continue
                    if _route == "fail_closed_decline":        # malformed/unrepresentable → inline decline
                        manager._write(build_decision_response(frame, "decline"))
                        _log_unattended_decision(method, "decline", "fail_closed")  # #280 C: audit
                        continue
                    if _route == "park_for_model":
                        _pt0 = time.monotonic()
                        _pnarr = None
                        if method in _NARRATIVE_APPROVAL_METHODS:
                            _pfull = "".join(ts["final_message_parts"])
                            _pnarr = _pfull[narrative_shown:]
                            narrative_shown = len(_pfull)
                        _payload, _dids = build_awaiting_payload(
                            method, frame.get("params"), ts, _pnarr, _park_token())
                        ctx["request_frame"] = frame
                        ctx["decision_ids"] = _dids
                        ctx["park_token"] = _payload["park_token"]
                        decision_id = yield _payload
                        # ── RESUME (codex_approve_v2 sent decision_id) — generator owns it (spec §5.2) ──
                        reactor = manager._reactor             # re-read LIVE (child may have respawned)
                        _drained = list(ts.pop("drained_frames", [])) + reactor.pump(timeout=0.0)
                        _term = None
                        _survivors = []                        # #278: non-notification frames (turn/start
                        for _cf in _drained:                   # ACK, server request) buffered during the
                            if not (isinstance(_cf, dict) and "__cc__" not in _cf):           # park — must be
                                continue                                                      # re-fed, not
                            if classify(_cf) == "notification":                               # dropped, or a
                                _r = _handle_child_frame(_cf, ts)        # surface a terminal/EOF that landed
                                if _r is not None:                       # during the park BEFORE writing
                                    _term = _r                           # (lost-terminal guard)
                            else:
                                _survivors.append(_cf)         # pre-ACK ACK / server request → re-feed in order
                        if _term is not None:
                            state_machine.turn_completed()
                            return _stamp_drift(_term, acc)
                        if _survivors:                         # #278: preserve for the loop-top re-prepend so
                            ts.setdefault("drained_frames", []).extend(_survivors)   # turn_acked gets set
                        _pelapsed = time.monotonic() - _pt0    # credit the park duration to the deadlines
                        if deadline is not None:
                            deadline += _pelapsed
                        ack_deadline += _pelapsed
                        _resp = build_decision_response(ctx.get("request_frame"), decision_id)
                        try:
                            if "error" not in _resp:           # pre-validated id (F3); error = defensive skip
                                manager._write(_resp)
                        except BrokenPipeError:                # child died mid-park (C8b) → graceful, no crash
                            _turn_error_log(ts, "codex child exited during park (decision undeliverable)")
                            state_machine.turn_completed()
                            return _stamp_drift(
                                {"error": "codex child exited during park (decision undeliverable)",
                                 **_fail_meta(ts)}, acc)
                        # #280 C audit (codex_review P3): log the RESOLVED grant from the built response
                        # (accept / acceptForSession / perm:* — what was GRANTED), NOT the opaque d-id the
                        # model picked. Mirrors the attended path, which passes the decision payload too.
                        _logged = _resp.get("result") if "error" not in _resp else decision_id
                        _log_unattended_decision(method, _logged, "model_resume")
                        continue
                    # ── attended / non-approval request → today's synchronous path (UNCHANGED) ──
                    _t0 = time.monotonic()
                    _new_narr = None
                    if frame.get("method") in _NARRATIVE_APPROVAL_METHODS:
                        _full_narr = "".join(ts["final_message_parts"])
                        _new_narr = _full_narr[narrative_shown:]
                        narrative_shown = len(_full_narr)
                    manager._write(handle_server_request(
                        frame, cc_write_fn, cc_read_fn, acc=acc, narrative=_new_narr,
                        drain_ctx={"reactor": reactor, "ts": ts, "cc_id": args.get("_cc_id")}))
                    if ts.pop("eof_during_approval", False):
                        return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                    if ts.get("terminal_during_approval") is not None:
                        state_machine.turn_completed()
                        return _stamp_drift(ts.pop("terminal_during_approval"), acc)
                    if ts.pop("cancel_during_approval", False):
                        if turn_id:
                            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "cancel", state_machine), acc)
                        elif turn_acked:
                            return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                        else:
                            cancel_pending = True   # pre-ACK approval cancel → defer to the ACK branch
                    _elapsed = time.monotonic() - _t0
                    if deadline is not None:
                        deadline += _elapsed
                    ack_deadline += _elapsed   # F6: pre-ACK approval is human time, not a setup stall
                    continue

                if not turn_acked:
                    # Phase 1: looking for the start ACK (response to our review/turn-start id)
                    if kind == "response" and frame.get("id") == mid:
                        if "error" in frame:
                            _turn_error_log(ts, f"{start_method} error: {frame['error']}")
                            state_machine.turn_completed()
                            return _stamp_drift({"error": f"{start_method} error: {frame['error']}",
                                                 **_fail_meta(ts)}, acc)
                        turn_acked = True
                        _ack_res = frame.get("result") or {}
                        turn_id = (_ack_res.get("turn") or {}).get("id")
                        # #349 r1: native review streams its items on a SEPARATE review
                        # thread — admit it (and only it) alongside the root thread.
                        ts["accepted_thread_ids"] = {thread_id}
                        _rtid = _ack_res.get("reviewThreadId")
                        if _rtid:
                            ts["accepted_thread_ids"].add(_rtid)
                    elif method == "error":
                        is_terminal, emsg = _classify_error_notification(frame.get("params", {}) or {})
                        if is_terminal:
                            _turn_error_log(ts, emsg)
                            state_machine.turn_completed()
                            return _stamp_drift({"error": f"codex error: {emsg or 'unknown error'}",
                                                 **_fail_meta(ts)}, acc)
                        ts["retries"] += 1   # pre-ACK transient counts too (#321 review P2)
                    elif method in ("warning", "model/rerouted"):
                        # #321 r2/r3: pre-ACK warning/reroute must not be dropped — delegate to the
                        # shared handler (WARNING audit line / rerouted_model capture, both non-terminal).
                        _handle_child_frame(frame, ts)
                    continue

                # Phase 2: event stream — delegate to the shared child-frame handler.
                if kind == "notification":
                    _res = _handle_child_frame(frame, ts)
                    if _res is not None:
                        state_machine.turn_completed()
                        return _stamp_drift(_res, acc)
                    continue

            # EOF check AFTER draining the batch: a child that wrote turn/completed then exited had its
            # completion consumed above. Only a child that died WITHOUT completing reaches here.
            if manager._child is not None and manager._child.poll() is not None:
                if cancel_pending:                   # R1-F2: cancel pending + child died → graceful COLD
                    return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                eof_err = state_machine.eof_error()
                _turn_error_log(ts, eof_err.get("error"))
                manager._child = None
                return _stamp_drift({**eof_err, **_fail_meta(ts)}, acc)

            if cancel_pending and turn_acked:        # AFTER the batch → same-batch ACK + deltas captured
                return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "cancel", state_machine), acc)

            if not turn_acked and not ts.get("drained_frames") and time.monotonic() > ack_deadline:
                # #278: do NOT declare a setup timeout while buffered frames are pending replay — a pre-ACK
                # turn/start ACK re-buffered by the parked-resume drain is consumed at the next loop-top and
                # may carry the ACK. The park path's `continue` stays inside the for-loop, so this end-of-batch
                # check would otherwise fire before the next iteration replays drained_frames.
                if cancel_pending:                   # R1-F2: cancel pending, ACK never arrived → graceful COLD
                    return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                _turn_error_log(ts, f"{start_method} response timed out")
                state_machine.turn_completed()
                return _stamp_drift({"error": f"{start_method} response timed out",
                                     **_fail_meta(ts)}, acc)

        # Opt-in work-duration deadline exceeded (only reachable when timeout was set).
        if _interrupts_enabled():                    # #218: graceful interrupt + resumable partial
            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "timeout", state_machine), acc)
        _turn_error_log(ts, f"turn timed out after {turn_timeout} s")
        state_machine.turn_completed()               # kill-switch: legacy bare error
        return _stamp_drift({"error": f"turn timed out after {turn_timeout} s",
                             **_fail_meta(ts)}, acc)

    except Exception as e:
        _turn_error_log(ts, f"turn execution error: {e}")
        state_machine.turn_completed()
        return _stamp_drift({"error": f"turn execution error: {e}", **_fail_meta(ts)}, acc)


def _validate_effort_support(manager, model: str, efforts) -> "str | None":
    """Pre-flight for the GATED reasoning efforts (max/ultra — not supported by every
    model): check the requested efforts against the LIVE model/list catalog entry for
    `model`; return an error string on a provable mismatch, else None.

    Deliberately DYNAMIC — the catalog comes from codex itself; a hardcoded
    model→efforts table is the #251 allowlist treadmill this server already deleted
    once. Fail-OPEN on every uncertainty (manager without connection_request, fetch
    error/timeout, paginated-away or unknown model id, malformed/empty efforts list):
    codex stays the authority and a legit call is never bricked. The payoff of the
    check is an actionable error BEFORE thread/start — i.e. before the 28-80s
    first-thread cold start is wasted on an opaque API rejection.
    """
    try:
        req = getattr(manager, "connection_request", None)
        if req is None:
            return None
        catalog = req("model/list", {}, timeout=15.0)
        entries = catalog.get("data") if isinstance(catalog, dict) else None
        dict_entries = [e for e in entries or [] if isinstance(e, dict)]
        # Exact `id` match takes precedence over the secondary `model` alias field —
        # an alias entry listed earlier must not shadow the canonical entry (PR #315
        # review P2). Only the FIRST page is examined: pagination-away = unknown =
        # fail-open by design (catalog is 7 entries live; cursor-following is
        # speculative complexity for a best-effort preflight).
        entry = next((e for e in dict_entries if e.get("id") == model), None) \
            or next((e for e in dict_entries if e.get("model") == model), None)
        if entry is None:
            return None  # unknown/paginated-away model ≠ invalid — let codex decide
        raw = entry.get("supportedReasoningEfforts")
        if not isinstance(raw, list) or not raw:
            return None
        supported = []
        for s in raw:
            eff = s.get("reasoningEffort") if isinstance(s, dict) else None
            if not isinstance(eff, str):
                # ANY malformed element = uncertainty about the FULL list (protocol
                # drift?) — a surviving subset must not drive a rejection (P1).
                return None
            supported.append(eff)
        bad = [e for e in efforts if e not in supported]
        if bad:
            return (f"effort {bad[0]!r} is not supported by model {model!r}; "
                    f"supported: {', '.join(supported)} (live catalog — see "
                    "codex_info query='models')")
    except Exception:
        return None  # catalog unavailable → fail-open, codex validates downstream
    return None


def codex_run_v2(
    args: dict,
    manager: "AppServerManager | None" = None,
    cc_write_fn=None,
    cc_read_fn=None,
    state_machine: "TurnStateMachine | None" = None,
) -> dict:
    """Run codex via the app-server bridge with full turn lifecycle management.

    This is the v2 implementation of the `codex_run` MCP tool. It wires the
    AppServerManager + approval bridge + Reactor + TurnStateMachine into a
    single call that returns mode-shaped structured output.

    Parameters
    ----------
    args : dict
        MCP tool arguments: {prompt (required), mode=review|implement,
        sandbox=read-only, approval_policy=on-request, effort=medium,
        model?, cwd?, thread_id?}.
    manager : AppServerManager | None
        Explicit manager (for testing). If None, uses the module singleton.
    cc_write_fn : callable | None
        Write frame to CC (for elicitation forwarding).
    cc_read_fn : callable | None
        Read elicitation response from CC.
    state_machine : TurnStateMachine | None
        Explicit state machine (for testing). If None, uses the module singleton.

    Returns
    -------
    dict
        review mode: {thread_id, verdict, findings, schema_ok}
        implement mode: {thread_id, result}
        error: {"error": str}
    """
    # A5: per-call drift accumulator — MUST be first statement (before no-codex
    # and prompt-required returns, both of which precede `mode = ...`).
    acc = []

    # ── Graceful no-codex + peek-warm (#227 part-2) ────────────────────────
    # The codex binary is needed only to SPAWN. A child left warm by a prior codex_run can
    # serve a SAME-isolation-signature call without it — mirrors codex_info #225 P3. Admitting
    # the call here can never destroy the warm child: a mismatched-signature respawn fails
    # safely either way — _spawn_appserver runs BEFORE any self mutation (a missing-binary
    # FileNotFoundError can't touch the warm child), and design C's (#227b) transactional
    # commit only retires the old child AFTER a fresh init succeeds (so a post-spawn init
    # failure can't either). The signature is not known until _build_isolation_argv (below),
    # so here we can only tell "is ANY child alive": if none is, a spawn is unavoidable →
    # require the binary NOW. This keeps the no-codex error ahead of mcp validation
    # (test_codex_run_no_codex_returns_error). A live-but-mismatched child falls through to
    # ensure(), which respawns (needs the binary) and fails safely if it is gone.
    if manager is None:
        manager = _get_manager()         # construction does not spawn → needs no codex binary
        if not _is_child_alive(manager._child) and not _codex_bin_available():
            return _stamp_drift({
                "error": (
                    f"codex binary not found at '{_resolve_codex_bin()}'. "
                    "Install codex or set JAINE_CODEX_BIN."
                )
            }, acc)

    if state_machine is None:
        state_machine = _v2_state_machine

    prompt = args.get("prompt")
    if not prompt:
        return _stamp_drift({"error": "prompt is required"}, acc)

    mode = args.get("mode", "review")
    # Internal: codex_review_v2 sets _review_target → start the turn via review/start
    # (native git-aware review) instead of turn/start, and collect findings from the
    # item/completed agentMessage item (review output is NOT streamed as deltas).
    review_target = args.get("_review_target")

    # ── mcp knob (REQUIRED, no default) ──────────────────────────────────────
    mcp = args.get("mcp")
    if mcp is None:
        return _stamp_drift({"error": (
            "mcp is required (no default). Pass mcp='isolated' (review/implement clean), "
            "mcp='all' (full toolset), mcp=['dash', ...] (subset), or mcp='list' to "
            "discover the available servers."
        )}, acc)
    # Type-check BEFORE membership (F2): `["x"] in frozenset` raises TypeError (lists are
    # unhashable), so a list/dict mcp must NOT reach the `in` test.
    if isinstance(mcp, str):
        if mcp not in VALID_MCP_MODES:
            return _stamp_drift({"error": f"invalid mcp value {mcp!r}; use 'isolated'/'all'/'list'/[list]"}, acc)
    elif isinstance(mcp, list):
        if not all(isinstance(s, str) for s in mcp):
            return _stamp_drift({"error": "mcp list items must be strings"}, acc)
    else:
        return _stamp_drift({"error": f"invalid mcp value {mcp!r}; use 'isolated'/'all'/'list'/[list]"}, acc)

    config_servers = _enumerate_config_mcp_servers()

    # Reject unknown subset names BEFORE spawn (F2): a typo must fail loud, not silently
    # disable the server the caller meant to keep.
    if isinstance(mcp, list):
        valid_targets = set(config_servers) | {"apps", "computer-use"}
        unknown = [s for s in mcp if s not in valid_targets]
        if unknown:
            return _stamp_drift({"error": (
                f"unknown mcp server(s) {unknown}; valid: {sorted(valid_targets)} "
                "(call mcp='list' to discover the available servers)")}, acc)

    if mcp == "list":
        return _stamp_drift({
            "available_mcp_servers": config_servers,
            "builtins": ["apps", "computer-use"],
            "computer_use_note": ("computer-use is a bundled codex plugin and cannot be "
                                  "disabled — it remains even in 'isolated'."),
            "thread_id": None,
        }, acc)

    try:
        isolation_argv = _build_isolation_argv(mcp, config_servers)
    except ValueError as e:
        return _stamp_drift({"error": str(e)}, acc)

    # Observability for the result metadata (Task 6). computer-use is a bundled plugin
    # that CANNOT be disabled (decision #6), so it is ALWAYS enabled — reflect that (F2b).
    # (Unknown subset names were already rejected pre-spawn above, so the subset here is
    # all valid.)
    if mcp == "isolated":
        mcp_servers_enabled = ["computer-use"]
    elif mcp == "all":
        mcp_servers_enabled = config_servers + ["apps", "computer-use"]
    else:  # subset list (names validated above)
        mcp_servers_enabled = [s for s in config_servers if s in set(mcp)]
        if "apps" in set(mcp):
            mcp_servers_enabled.append("apps")
        mcp_servers_enabled.append("computer-use")
    mcp_mode = mcp if isinstance(mcp, str) else "subset"

    # F4: thread-level control knobs apply only to a NEW thread (set at thread/start).
    # On resume there is no thread/start → they'd silently no-op. Fail loud.
    if args.get("thread_id") is not None and any(
        args.get(k) is not None for k in ("approvals_reviewer", "service_tier")):
        return _stamp_drift({"error": (
            "approvals_reviewer/service_tier apply only to a NEW thread; "
            "omit them when resuming a thread_id.")}, acc)

    # Explicit caller values (None = omitted = "keep thread's current posture")
    sandbox_val = args.get("sandbox")
    approval_policy_val = args.get("approval_policy")
    effort_val = args.get("effort")
    cwd_val = args.get("cwd")
    model_val = args.get("model")
    thread_id = args.get("thread_id")

    # ── Busy guard ─────────────────────────────────────────────────────────
    # The MCP call id is not available here (v2 tool is called from the main
    # dispatcher which has the id). We use None as a sentinel for tests; the
    # real dispatcher can pass the cc_id separately if needed.
    cc_id = args.get("_cc_id")
    if state_machine.is_busy():
        return _stamp_drift(state_machine.busy_error(), acc)

    # ── Ensure child is alive ───────────────────────────────────────────────
    setup_t0 = time.time()  # #322 PR2 (B7): ensure + thread setup = the cold-start window
    try:
        manager.ensure(isolation_argv)
    except Exception as e:
        return _stamp_drift({"error": f"app-server ensure failed: {e}"}, acc)

    # ── Gated-effort preflight (max/ultra are per-model; low..xhigh universal) ──
    # Validate BOTH channels codex will see: the turn-level `effort` arg (codex_run;
    # wins at turn/start) and a config-routed model_reasoning_effort (codex_review
    # routing + raw config passthrough; wins at thread/start). Runs only when a GATED
    # effort is requested AND the effective model is known from the call — with
    # `model` omitted the effective model is the user's config.toml default, not
    # resolvable without a ~71K config/read per call → documented fail-open.
    cfg_args = args.get("config")
    if not isinstance(cfg_args, dict):
        cfg_args = {}
    # isinstance(str) filters BEFORE the set: raw config can carry garbage (a JSON
    # array under model_reasoning_effort would make the set literal raise TypeError
    # outside the helper's fail-open boundary — PR #315 review P2); garbage keeps
    # passing through to codex verbatim, exactly as pre-preflight.
    gated_efforts = sorted(
        {v for v in (effort_val, cfg_args.get("model_reasoning_effort"))
         if isinstance(v, str) and v in GATED_EFFORTS})
    model_eff = model_val or cfg_args.get("model")
    if gated_efforts and isinstance(model_eff, str):
        preflight_err = _validate_effort_support(manager, model_eff, gated_efforts)
        if preflight_err:
            return _stamp_drift({"error": preflight_err}, acc)

    # ── Thread setup ────────────────────────────────────────────────────────
    if thread_id is not None:
        # RESUME path: send thread/resume; fail-loud on unknown thread
        try:
            result = manager.resume_thread(thread_id)
        except Exception as e:
            return _stamp_drift({"error": f"thread/resume failed for {thread_id!r}: {e}"}, acc)
        # Check for error in result (unknown thread from fake or real server)
        if isinstance(result, dict) and "error" in result:
            return _stamp_drift({"error": f"unknown thread_id {thread_id!r}: {result['error']}"}, acc)
    else:
        # NEW thread: apply defaults for omitted posture params
        sandbox_for_start = sandbox_val or "read-only"
        approval_policy_for_start = approval_policy_val or "on-request"
        # cwd: omitted → isolated tmpdir; NEVER the caller's cwd
        if cwd_val:
            cwd_for_start = cwd_val
        else:
            cwd_for_start = tempfile.mkdtemp(prefix="bulldozer-codex-")
            _isolated_tmpdirs.append(cwd_for_start)
        try:
            thread_id = manager.start_thread(
                sandbox=sandbox_for_start,
                approval_policy=approval_policy_for_start,
                base_instructions=args.get("base_instructions"),
                developer_instructions=args.get("developer_instructions"),
                config=args.get("config"),
                cwd=cwd_for_start,
                approvals_reviewer=args.get("approvals_reviewer"),
                service_tier=args.get("service_tier"),
            )
        except Exception as e:
            return _stamp_drift({"error": f"thread/start failed: {e}"}, acc)

    # ── Build turn/start params ─────────────────────────────────────────────
    turn_params: dict = {
        "threadId": thread_id,
        "input": _turn_input(prompt),
    }

    # POSTURE PRECEDENCE: on a new thread, the defaults were set at thread/start;
    # any explicit per-call param overrides via turn/start.
    # On RESUME: ONLY send what the caller EXPLICITLY set — omitted = don't send
    # = keep the thread's current posture (including cwd). Never silently reset
    # a resumed thread to read-only / tmpdir.
    is_resume = args.get("thread_id") is not None
    if is_resume:
        # Per-call posture overrides for resume — only what was explicitly set
        effective_cwd = cwd_val  # None if omitted (don't send)
        if sandbox_val == "workspace-write" and effective_cwd is None:
            # Pre-turn early return: turn_started() has not run yet, so do NOT call
            # turn_completed() here (it would be an unmatched completion).
            return _stamp_drift({"error": "workspace-write sandbox requires an explicit cwd on resume"}, acc)
        if sandbox_val is not None:
            turn_params["sandboxPolicy"] = _sandbox_policy(
                sandbox_val, effective_cwd or ""
            )
        if approval_policy_val is not None:
            turn_params["approvalPolicy"] = approval_policy_val
        if effort_val is not None:
            turn_params["effort"] = effort_val
        if cwd_val is not None:
            turn_params["cwd"] = cwd_val
        if model_val is not None:
            turn_params["model"] = model_val
    else:
        # New thread: posture is established at thread/start; only pass per-turn
        # overrides that differ from the defaults (effort and model are turn-level only)
        if effort_val is not None:
            turn_params["effort"] = effort_val
        if model_val is not None:
            turn_params["model"] = model_val

    # Review mode: constrain output via outputSchema
    if mode == "review":
        turn_params["outputSchema"] = REVIEW_SCHEMA

    # ── Turn execution loop ─────────────────────────────────────────────────
    turn_start_t = time.time()
    state_machine.turn_started(cc_id)
    # Shared turn-state: _handle_child_frame (+ the #218 interrupt routine / #252 approval drain)
    # read/mutate these so a child frame is handled identically wherever it is read.
    ts = {
        "final_message_parts": [], "usage_snapshot": {}, "retries": 0,
        # #349: per-item assembly (ultra delegation streams concurrent agentMessage items)
        "msg_item_parts": {}, "msg_item_order": [], "msg_item_final": {},
        "interrupting": False, "interrupted_by": "cancel", "acc": acc,
        "manager": manager, "turn_start_t": turn_start_t, "mcp_mode": mcp_mode,
        "mcp_servers_enabled": mcp_servers_enabled, "effort_val": effort_val,
        "model_val": model_val, "mode": mode, "thread_id": thread_id,
        "review_target": review_target,
        "file_changes": {},   # #277 (C16): itemId → {changes:[{diff,kind,path}], turn_id} from patchUpdated
        # #322 PR2 (B7): ensure + thread setup wall-clock; cold_spawn = ensure() spawned
        "setup_ms": int((turn_start_t - setup_t0) * 1000),
        "cold_spawn": bool(getattr(manager, "last_ensure_spawned", False)),
    }

    # #277: the turn-pump loop is an INNER generator (_drive_turn). codex_run_v2 stays dict-returning
    # (codex_review_v2 json.dumps-es its result). Build ctx, drive the generator: a yielded payload =
    # the turn PARKED at a park_for_model approval → store the park record + RETURN the payload; a
    # StopIteration = the turn completed → RETURN its final dict (already _stamp_drift'd inside).
    ctx = {
        "manager": manager, "ts": ts, "args": args, "acc": acc,
        "cc_write_fn": cc_write_fn, "cc_read_fn": cc_read_fn,
        "state_machine": state_machine, "thread_id": thread_id,
        "review_target": review_target, "turn_params": turn_params, "mode": mode,
        # R2-F1: _force_park_route is NOT copied from args — it is a TEST-ONLY ctx seam (a direct
        # _drive_turn caller sets it), never reachable from public MCP args; armed routing is the only
        # public park trigger (the gate also consults _unattended_active()).
        # cross-boundary park outputs (the generator sets these at a yield):
        "request_frame": None, "decision_ids": None, "park_token": None,
    }
    gen = _drive_turn(ctx)
    try:
        payload = next(gen)
    except StopIteration as e:
        return e.value                       # turn completed (or errored) without parking — the common case
    # The generator YIELDED → the turn is PARKED at a park_for_model approval (#277). Store the record so
    # codex_approve_v2 can resume it, mark the state machine parked, and return the awaiting payload.
    manager._parked = {
        "park_token": ctx["park_token"], "thread_id": thread_id, "inner_gen": gen,
        "isolation_sig": manager._isolation_sig, "started_at": time.monotonic(),
        "request_frame": ctx["request_frame"], "decision_ids": ctx["decision_ids"],
        "ctx": ctx,   # codex_approve_v2 reads ctx after gen.send to RE-PARK a multi-approval turn (§5.2)
    }
    state_machine.park(ctx["park_token"], thread_id)
    return _stamp_drift(payload, acc)


def _classify_error_notification(params: dict) -> tuple:
    """Classify a codex `error` notification (shared by codex_run / codex_review).

    `willRetry: true` → transient stream reconnect (e.g. "Reconnecting N/5"); codex
    retries on its own, so it is NOT terminal. Returns (is_terminal, message)."""
    err = params.get("error")
    msg = err.get("message") if isinstance(err, dict) else err
    return (not params.get("willRetry"), msg)


def _build_result_meta(manager, usage_snapshot: dict, turn_start_t: float,
                       mcp_mode: str, mcp_servers_enabled: list, effort_val,
                       model_val, status: str, ts: dict | None = None) -> dict:
    """Build the ADDITIVE result metadata (usage/codex/timing/status).

    `status` is "completed" on success or "failed" on a terminal turn failure (F11) —
    a failed turn still consumed tokens, so usage/timing observability matters there too.
    `ts` (kw-only-ish, optional) contributes setup timing (#322 PR2 B7): setup_ms =
    ensure+thread-setup wall-clock, cold_spawn = whether ensure() spawned a child —
    the 28-80s cold-start vs ~1s warm split was previously measured nowhere.
    """
    tm = getattr(manager, "_last_thread_meta", {}) or {}
    # Wire: usage_snapshot is params.tokenUsage = {last, total}, each a TokenUsageBreakdown
    # with camelCase keys. Map the cumulative `total` into our snake_case result API (spec 2a).
    breakdown = (usage_snapshot or {}).get("total") or (usage_snapshot or {}).get("last") or {}
    return {
        "usage": {
            "input_tokens": breakdown.get("inputTokens"),
            "cached_input_tokens": breakdown.get("cachedInputTokens"),
            "output_tokens": breakdown.get("outputTokens"),
            "reasoning_output_tokens": breakdown.get("reasoningOutputTokens"),
            "total_tokens": breakdown.get("totalTokens"),
        },
        "codex": {
            "model": model_val or tm.get("model"),
            "service_tier": tm.get("service_tier"),
            "effort": effort_val or tm.get("effort"),
            "approvals_reviewer": tm.get("approvals_reviewer"),
            "mcp_mode": mcp_mode,
            "mcp_servers_enabled": mcp_servers_enabled,
        },
        "timing": dict(
            {"duration_ms": int((time.time() - turn_start_t) * 1000)},
            **({"setup_ms": ts["setup_ms"], "cold_spawn": bool(ts.get("cold_spawn"))}
               if ts and ts.get("setup_ms") is not None else {}),
        ),
        "status": status,
    }


def _shape_result(mode: str, thread_id: str, final_text: str, meta: dict | None = None) -> dict:
    """Return the mode-shaped result dict, merged with additive meta (usage/codex/timing/status).

    review mode: {thread_id, verdict, findings, schema_ok, **meta}
    implement mode: {thread_id, result, **meta}
    """
    if mode == "review":
        try:
            parsed = json.loads(final_text)
            base = {"thread_id": thread_id, "schema_ok": True,
                    "verdict": parsed.get("verdict", "UNKNOWN"),
                    "findings": parsed.get("findings", [])}
        except (json.JSONDecodeError, AttributeError):
            base = {"thread_id": thread_id, "schema_ok": False,
                    "verdict": "UNKNOWN", "findings": [], "raw": final_text}
    else:
        base = {"thread_id": thread_id, "result": final_text}
    if meta:
        base.update(meta)
    return base


# Entrypoint LAST: main() is the v2 dispatcher, which references codex_run_v2 and
# the rest of the v2 section above — so the guard must come after they are defined.
if __name__ == "__main__":
    main()
