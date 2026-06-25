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
LAST_VERIFIED_CODEX_VERSION = "0.141"   # last codex app-server version this bridge was verified against

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
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"], "default": "medium"},
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
            "codex sees) | features (experimental flags) | profiles (permission profiles). "
            "Returns {query, result}."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "enum": ["models", "auth", "config", "limits", "usage",
                             "servers", "features", "profiles"],
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
                "effort": {"type": "string",
                           "enum": ["low", "medium", "high", "xhigh"], "default": "medium"},
                "timeout": {"type": "number",
                            "description": "Optional work-duration cap in seconds."},
            },
        },
    },
]


def log(*a):
    print("[bulldozer-codex]", *a, file=sys.stderr, flush=True)


def _now_iso() -> str:
    try:
        return datetime.datetime.now().isoformat(timespec="seconds")
    except Exception:
        return "?"


def _drift_warn(acc, code: str, detail: str) -> None:
    """Record an upstream-drift breadcrumb. NEVER raises.

    acc: per-call list (appended for user-facing _drift) or None (log-only,
    e.g. VERSION_MISMATCH). Always best-effort writes one line to the stable log.
    """
    if acc is not None:
        acc.append({"code": code, "detail": detail})
    try:
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer-codex.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{_now_iso()} | {code} | {detail}\n")
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
    "Pass `mcp:'isolated'` unless cross-server tools are needed."
)


def _initialize_result(params: dict) -> dict:
    """Build the MCP initialize reply. Carries an `instructions` routing manifest — CC injects
    InitializeResult.instructions into the model's context on connect, giving it a server-level
    map to discover/choose codex_review / codex_run / codex_info (#256)."""
    return {
        "protocolVersion": params.get("protocolVersion", PROTO),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "bulldozer-codex", "version": _plugin_version()},
        "instructions": SERVER_INSTRUCTIONS,
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
                if tool_name == "codex_info":
                    res = codex_info_v2(args)
                elif tool_name == "codex_review":
                    res = codex_review_v2(args, cc_write_fn=cc_write_fn, cc_read_fn=cc_read_fn)
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


def _translate_texts(texts, lang):
    """Translate each string to `lang` via LiteLLM (opt-in, fail-open, batched).

    Returns a list (same length/order). lang-off, no key, or ANY failure → originals.
    None / blank entries pass through untouched (kept in position).
    """
    texts = list(texts)
    if not lang or not _translate_key():
        return texts
    idxs = [i for i, t in enumerate(texts) if t and str(t).strip()]
    if not idxs:
        return texts
    payload = tuple(str(texts[i])[:2000] for i in idxs)  # bound input size
    try:
        out = _translate_cached(lang, _translate_endpoint(), _translate_model(), payload)
    except Exception as e:
        # fail-open on ANY failure (TranslateError, HTTP/timeout, weird return); log best-effort
        # so a silent English fallback is diagnosable (why localization isn't working).
        _drift_warn(None, "TRANSLATE_FAILED", f"{type(e).__name__}: {str(e)[:120]}")
        return texts
    if not out or len(out) != len(idxs):
        return texts
    for j, i in enumerate(idxs):
        texts[i] = out[j]
    return texts


def _narrative_line(narrative, label="Codex explained") -> str:
    """Format codex's pre-approval agentMessage narrative for the dialog (#224), or ''.

    This is the 'codex explains what it's about to do' content — streamed as
    agentMessage deltas just before the approval, NOT part of the approval request
    protocol-wise. Context only, never authoritative for the action. `narrative` is
    already translated (if a language is configured) by the caller; `label` is localized.
    """
    if not narrative:
        return ""
    text = _truncate_for_display(str(narrative).strip(), max_lines=6, max_chars=500)
    return f"{label}: {text}" if text else ""


def _build_command_approval_message(params: dict, narrative=None) -> str:
    """Compose the commandExecution approval dialog (#239 truncation + #224 context).

    Order: authoritative command (head+tail truncated) + cwd first, then optional
    context (reason / friendly actions / network host / codex narrative). EVERY
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
    lines = [
        L["approval_request"],
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
    nar = _narrative_line(narr_t, L["explained"])
    if nar:
        lines.append(nar)
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
        if items:
            fs_part = "fileSystem " + ", ".join(items)
    net_part = ""
    net = perms.get("network")
    if isinstance(net, dict) and net:
        if net.get("enabled"):
            net_part = "network: enabled"
        else:
            net_part = "network: " + ", ".join("{}={}".format(k, v) for k, v in net.items())
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
    if details:  # authoritative (paths/hosts) — bounded, never translated; tail_lines keeps
        # BOTH ends so the security-sensitive network grant (rendered first) AND the fileSystem
        # tail survive truncation of a large profile (review B).
        lines.append(_truncate_for_display(str(details), max_lines=6, max_chars=400, tail_lines=2))
    lines.append(
        f"{L['reason']}: {_truncate_for_display(str(reason_t) if reason_t else '(none)', max_lines=3, max_chars=300)}",
    )
    nar = _narrative_line(narr_t, L["explained"])
    if nar:
        lines.append(nar)
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


def _log_approval_event(method, decision, wait_ms, timed_out) -> None:
    """Best-effort one-line record of a completed approval (#251 step-0).

    Reuses the stable codex log channel (BULLDOZER_CODEX_LOG / bulldozer-codex.log).
    NEVER raises — logging must never break an approval.
    """
    def _san(v):
        # Keep the line greppable for the #251 miner: a CC-controlled value (e.g. the
        # mcpServer/elicitation 'action' passthrough) must not inject a newline (spurious
        # line) or the ' | ' field delimiter (split corruption).
        return str(v).replace("\n", " ").replace("\r", " ").replace("|", "/")
    try:
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer-codex.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(
                f"{_now_iso()} | APPROVAL | method={_san(method)} | "
                f"decision={_san(_approval_decision_label(decision))} | "
                f"wait_ms={wait_ms} | timed_out={'true' if timed_out else 'false'}\n")
    except Exception:
        pass


def bridge_approval(method: str, params: dict, cc_write_fn, cc_read_fn,
                    timeout: float = 300.0, acc=None, narrative=None, drain_ctx=None):
    """Issue a CC elicitation/create, wait for the answer, return the codex decision.

    Thin wrapper over _bridge_approval_dispatch: records one best-effort approval-event
    log line (method / decision / wait_ms / timed_out, #251 step-0) at every exit, then
    returns the dispatch decision unchanged. drain_ctx (#252) is threaded through to the wait.
    """
    t0 = time.time()
    wait_state = {"timed_out": False}
    decision = _bridge_approval_dispatch(
        method, params, cc_write_fn, cc_read_fn, timeout, acc, narrative, wait_state,
        drain_ctx=drain_ctx)
    _log_approval_event(method, decision, int((time.time() - t0) * 1000),
                        wait_state["timed_out"])
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

    def read_correlated(eid: int, timeout: float):
        """Wait for the CC elicitation reply (id==eid response). With drain_ctx active (#252),
        ALSO drain the codex child each iteration so a flooding child can't deadlock, and detect
        a mid-approval cancel / stdin EOF / terminal-child frame — each sets a `ts` flag and ends
        the wait via the per-method `None` decline below, which the turn loop acts on after writing
        that decline. drain_ctx=None → behavior is byte-identical to before."""
        _reactor = drain_ctx.get("reactor") if drain_ctx else None
        _ts = drain_ctx.get("ts") if drain_ctx else None
        _cc_id = drain_ctx.get("cc_id") if drain_ctx else None
        drain_active = _reactor is not None and _ts is not None

        def _approval_reply(mid, result=None, error=None):
            # #269: answer an id-bearing CC request via the approval path's writer (cc_write_fn),
            # same JSON-RPC 2.0 envelope as the module `reply` / turn-pump path.
            cc_write_fn({"jsonrpc": "2.0", "id": mid,
                         ("error" if error else "result"): (error if error else result)})

        deadline = time.time() + timeout
        while time.time() < deadline:
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
            remaining = max(0.0, deadline - time.time())
            frame = cc_read_fn(timeout=min(remaining, 0.05) if drain_active else remaining)
            if frame is _CC_EOF:                 # CC stdin closed (#218) → EOF wins (even over a held terminal)
                if drain_active:
                    _ts["eof_during_approval"] = True
                return None
            if pending_terminal is not None:    # terminal child this iteration = turn over (no EOF) → surface it
                _ts["terminal_during_approval"] = pending_terminal
                return None
            if frame is not None:
                # Shape-first: ONLY a RESPONSE whose id matches resolves the elicitation.
                if frame.get("id") == eid and classify(frame) == "response":
                    return frame
                # A mid-approval cancel for our turn (interrupts enabled, cc_id known) → flag + decline.
                if (drain_active and _cc_id is not None
                        and frame.get("method") == "notifications/cancelled"
                        and (frame.get("params") or {}).get("requestId") == _cc_id
                        and _interrupts_enabled()):
                    _ts["cancel_during_approval"] = True
                    return None
                # #269: otherwise an id-bearing CC request (ping/tools/list/tools/call) MUST be
                # answered or CC blocks on it (the turn-pump path enforces the same contract via
                # _route_cc_frame). It answers requests and no-ops notifications / responses /
                # foreign-or-disabled cancels; its interrupt/teardown return is irrelevant here
                # (our-turn cancel + EOF are handled above).
                _route_cc_frame(frame, cc_id=_cc_id, reply_fn=_approval_reply)
            # transient (None) / skipped frame → retry within the deadline
        if _wait_state is not None:
            _wait_state["timed_out"] = True
        return None  # deadline expired without a matching reply

    if method == "item/commandExecution/requestApproval":
        label_pairs = build_command_approval_labels(params, acc=acc)
        labels = [lbl for lbl, _ in label_pairs]
        label_map = dict(label_pairs)
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": _build_command_approval_message(params, narrative),
                "requestedSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}},
                },
            },
        })
        resp = read_correlated(eid, timeout)
        if resp is None:
            return "decline"
        result = resp.get("result", {})
        action = result.get("action")
        if action == "accept":
            content = result.get("content") or {}
            # Clicking CC's Accept WITHOUT picking a dropdown label = plain accept
            # (the dropdown is optional, for advanced amendment choices).
            chosen = content.get("label", LBL_ALLOW_ONCE)
            if chosen not in label_map:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
            return label_map.get(chosen, "accept")
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
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": _build_simple_approval_message(
                    "filechange", params.get("reason"), narrative),
                "requestedSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}},
                },
            },
        })
        resp = read_correlated(eid, timeout)
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
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": _build_simple_approval_message(
                    "permissions", params.get("reason"), narrative,
                    details=_summarize_permissions(requested)),
                "requestedSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}},
                },
            },
        })
        resp = read_correlated(eid, timeout)
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
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": f"Codex {method}\nCWD: {params.get('cwd') or '(unknown)'}",
                "requestedSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "enum": labels}},
                },
            },
        })
        resp = read_correlated(eid, timeout)
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

    def turn_started(self, cc_id):
        self._in_flight = True
        self._pending_cc_id = cc_id

    def turn_completed(self):
        self._in_flight = False
        self._pending_cc_id = None

    def is_busy(self) -> bool:
        return self._in_flight

    def busy_error(self) -> dict:
        """Return an error dict for a second concurrent tools/call.

        Returns {"error": str} — the same shape as every other error path in
        codex_run_v2, so the dispatcher's content-wrapping delivers it as a
        normal MCP tool-error result (consistent with the 11 other {"error":...}
        returns).  The serial dispatcher loop is the primary concurrency guard;
        this method is defense-in-depth and is rarely/never hit in production.
        """
        return {"error": "codex turn already in flight"}

    def eof_error(self) -> dict:
        """App-server died mid-turn: clear state, return error dict for the pending CC call.

        Returns {"error": str} — same shape as every other error path in
        codex_run_v2.  State is cleared so the child can respawn on next call.
        """
        self._in_flight = False
        self._pending_cc_id = None
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
            return {"error": f"codex binary not found at '{_resolve_codex_bin()}'. Install codex or set JAINE_CODEX_BIN."}
        try:
            manager.ensure([])   # default connection (no isolation) only if none alive
        except Exception as e:
            return _stamp_drift({"error": f"{method} failed: {e}"}, acc)

    try:
        result = manager.connection_request(method, params)
    except Exception as e:
        # #227 item 2: a warm child can die DURING connection_request (alive at the check,
        # dead by the read). Self-heal with ONE respawn+retry — but ONLY when the child is
        # now actually dead (a live-child error is a real protocol/timeout error: surface it,
        # don't mask) and a respawn is permitted (binary present on the singleton path).
        if _is_child_alive(manager._child) or not _respawn_allowed():
            return _stamp_drift({"error": f"{method} failed: {e}"}, acc)
        try:
            manager.ensure([])
            result = manager.connection_request(method, params)
        except Exception as e2:
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


def _build_interrupted_result(ts: dict, interrupted_by: str, thread_warm: bool = True) -> dict:
    """Graceful, resumable result for an interrupted turn (#218 F7). Mode-shaped (so a
    review/implement/codex_review caller still gets its keys) with interrupt metadata and NO
    'error' key — the dispatcher marks isError iff 'error' in res, so an interrupt stays a
    graceful partial, not a failure."""
    partial = "".join(ts["final_message_parts"])
    meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                              ts["mcp_mode"], ts["mcp_servers_enabled"],
                              ts["effort_val"], ts["model_val"], "interrupted")
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
        # `or ""`: a present-but-null delta returns None from .get(k, "") (#18)
        ts["final_message_parts"].append(frame.get("params", {}).get("delta") or "")
        return None
    if method == "item/completed" and ts["review_target"] is not None:
        # Native review output is delivered as a COMPLETED agentMessage item (.text), not deltas.
        _it = frame.get("params", {}).get("item", {}) or {}
        if _it.get("type") == "agentMessage":
            ts["final_message_parts"].append(_it.get("text") or "")   # null-safe (#18)
        return None
    if method == "thread/tokenUsage/updated":
        tu = frame.get("params", {}).get("tokenUsage")
        if isinstance(tu, dict):
            ts["usage_snapshot"] = tu
        return None
    if method == "turn/completed":
        t = frame.get("params", {}).get("turn", {}) or {}
        # #218: an interrupt WE initiated terminates as status="interrupted" — route it to the
        # graceful result, bypassing the generic terminal-failure arm below.
        if ts.get("interrupting") and t.get("status") == "interrupted" and not t.get("error"):
            return _build_interrupted_result(ts, interrupted_by=ts.get("interrupted_by", "cancel"))
        if t.get("status") != "completed" or t.get("error"):  # TurnStatus has no "success" (codex 0.141: completed/interrupted/failed/inProgress)
            meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                      ts["mcp_mode"], ts["mcp_servers_enabled"],
                                      ts["effort_val"], ts["model_val"], "failed")
            return {"error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                    "thread_id": ts["thread_id"], **meta}
        meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "completed")
        if ts["retries"]:
            meta["retries"] = ts["retries"]
        return _shape_result(ts["mode"], ts["thread_id"], "".join(ts["final_message_parts"]), meta)
    if method == "error":
        # willRetry:true = transient stream reconnect (codex retries) → NOT terminal, NOT drift.
        is_terminal, emsg = _classify_error_notification(frame.get("params", {}) or {})
        if not is_terminal:
            ts["retries"] += 1
            return None
        meta = _build_result_meta(ts["manager"], ts["usage_snapshot"], ts["turn_start_t"],
                                  ts["mcp_mode"], ts["mcp_servers_enabled"],
                                  ts["effort_val"], ts["model_val"], "failed")
        return {"error": f"codex error: {emsg or 'unknown error'}",
                "thread_id": ts["thread_id"], **meta}
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
    try:
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer-codex.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(f"{_now_iso()} | INTERRUPT_DISABLED | BULLDOZER_CODEX_NO_INTERRUPT set\n")
    except Exception:
        pass


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
    try:
        manager.ensure(isolation_argv)
    except Exception as e:
        return _stamp_drift({"error": f"app-server ensure failed: {e}"}, acc)

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
    reactor = manager._reactor
    narrative_shown = 0   # #224: char offset into the joined narrative already shown in a prior approval
    # Shared turn-state: _handle_child_frame (+ the #218 interrupt routine / #252 approval drain)
    # read/mutate these so a child frame is handled identically wherever it is read.
    ts = {
        "final_message_parts": [], "usage_snapshot": {}, "retries": 0,
        "interrupting": False, "interrupted_by": "cancel", "acc": acc,
        "manager": manager, "turn_start_t": turn_start_t, "mcp_mode": mcp_mode,
        "mcp_servers_enabled": mcp_servers_enabled, "effort_val": effort_val,
        "model_val": model_val, "mode": mode, "thread_id": thread_id,
        "review_target": review_target,
    }

    try:
        # Send turn/start; then run a unified pump loop that:
        #   Phase 1 — awaits the TurnStartResponse (response to our turn/start id)
        #   Phase 2 — processes events (delta, turn/completed) and server→client
        #             requests (approvals) until turn/completed arrives.
        #
        # CRITICAL: do NOT use _pump_until for turn/start because it discards all
        # frames in the same chunk that arrive after the matching response.  The
        # fake (and real codex) can send TurnStartResponse + delta + turn/completed
        # in a single flush → _pump_until would drop the notifications.
        mid = manager._next_id()
        start_method = "review/start" if review_target is not None else "turn/start"
        if review_target is not None:
            # Native review: review/start kicks off a turn on this thread. The
            # ReviewStartResponse carries our id (Phase 1 ACK works unchanged).
            manager._write({"id": mid, "method": "review/start", "params": {
                "threadId": thread_id, "target": review_target, "delivery": "inline"}})
        else:
            manager._write({"id": mid, "method": "turn/start", "params": turn_params})

        # Unified pump: Phase 1 = waiting for turn/start ACK; Phase 2 = event stream.
        # No work-duration cap by default (match stock — stock has none; codex's own
        # stream_idle_timeout_ms handles a genuinely hung model). Opt-in `timeout`
        # re-imposes a work cap. The turn/start ACK timeout is a SETUP check (the
        # engine must answer turn/start), distinct from limiting WORK duration.
        turn_acked = False
        turn_timeout = args.get("timeout")
        deadline = (time.time() + turn_timeout) if turn_timeout else None
        ack_deadline = time.time() + _ACK_TIMEOUT   # SETUP check (engine must answer turn/start)
        # #218: watch CC stdin for a mid-turn cancel only when interrupts are enabled AND we have
        # a cc_id to correlate it against (R5-F1: a direct unit test without _cc_id must not read
        # global sys.stdin). turn_id is captured at the ACK; cancel_pending bridges a pre-ACK cancel.
        _log_kill_switch_once()                  # F8: note once if the kill-switch disabled interrupts
        watch = _interrupts_enabled() and args.get("_cc_id") is not None
        turn_id = None
        cancel_pending = False

        while deadline is None or time.time() < deadline:
            frames = reactor.pump(timeout=0.2, watch_cc=watch)
            # codex P1: re-process any non-notification child frames the approval drain buffered
            # (e.g. a turn/start ACK that arrived while an approval was pending) — prepend so they
            # are handled before this batch (otherwise a pre-ACK approval would falsely time out).
            if ts.get("drained_frames"):
                frames = ts.pop("drained_frames") + frames
            # CC stdin EOF has BATCH PRIORITY (R1-F1): a closed CC channel can't receive ANY
            # result, so a same-batch child completion is undeliverable → force cold teardown.
            # (isinstance guard: a child may emit a bare non-dict JSON line — reviewer F3.)
            if any(isinstance(f, dict) and isinstance(f.get("__cc__"), dict) and f["__cc__"].get("__eof__")
                   for f in frames):
                return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
            for frame in frames:
                if not isinstance(frame, dict):
                    continue                                   # bare non-dict JSON line from the child — ignore (reviewer F3)
                if "__cc__" in frame:                          # CC-side frame (cancel/other; EOF handled by the scan)
                    if _route_cc_frame(frame["__cc__"], cc_id=args.get("_cc_id"), reply_fn=reply) == "interrupt":
                        # Defer the interrupt to END-OF-BATCH so a same-batch ACK + deltas are
                        # captured first → a real cancel returns the partial work produced so far.
                        cancel_pending = True
                    continue
                kind = classify(frame)
                method = frame.get("method", "")

                # Server→client requests (approvals) are bridged in EITHER phase: an
                # approval can in principle arrive before the TurnStartResponse, and
                # dropping it would hang the app-server until the outer deadline.
                if kind == "request":
                    # An approval blocks on a HUMAN reply (up to the elicitation
                    # timeout). That wall-clock is the user's thinking time, not codex
                    # working — credit it back so a slow human approval doesn't trip the
                    # opt-in turn timeout (if set) nor the turn/start ACK setup window.
                    _t0 = time.time()
                    # #224: thread the narrative codex streamed SINCE the last approval into
                    # the dialog. final_message_parts already holds the deltas flushed before
                    # this request (same-batch frames are processed in wire order). Advance the
                    # offset ONLY for narrative-bearing approvals (Grok#2) — a non-narrative
                    # request must not consume narrative a following command approval should show.
                    _new_narr = None
                    if frame.get("method") in _NARRATIVE_APPROVAL_METHODS:
                        _full_narr = "".join(ts["final_message_parts"])
                        _new_narr = _full_narr[narrative_shown:]
                        narrative_shown = len(_full_narr)
                    manager._write(handle_server_request(
                        frame, cc_write_fn, cc_read_fn, acc=acc, narrative=_new_narr,
                        drain_ctx={"reactor": reactor, "ts": ts, "cc_id": args.get("_cc_id")}))
                    # #218/#252 post-approval-write checks, in priority order. EOF first: a closed
                    # CC channel makes any result undeliverable → cold teardown (R6-F1). Then a
                    # terminal turn that ended during the approval. Then a cancel — phase-aware,
                    # mirroring the turn pump (warm / cold / pre-ACK defer).
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
                    _elapsed = time.time() - _t0
                    if deadline is not None:
                        deadline += _elapsed
                    ack_deadline += _elapsed   # F6: pre-ACK approval is human time, not a setup stall
                    continue

                if not turn_acked:
                    # Phase 1: looking for the start ACK (response to our review/turn-start id)
                    if kind == "response" and frame.get("id") == mid:
                        if "error" in frame:
                            state_machine.turn_completed()
                            return _stamp_drift({"error": f"{start_method} error: {frame['error']}"}, acc)
                        turn_acked = True
                        # turnId from the start ACK (TurnStartResponse.turn.id); may be None for a
                        # review/start ACK without a turn id → a later interrupt routes to teardown.
                        # A pending cancel is acted on at end-of-batch (after same-batch deltas).
                        turn_id = ((frame.get("result") or {}).get("turn") or {}).get("id")
                    elif method == "error":
                        # A TERMINAL error can arrive BEFORE the ACK (e.g. during the
                        # cold-start setup window). Surface it instead of dropping it →
                        # which would mask it as a generic ACK timeout (#4). Transient
                        # willRetry errors are ignored here — keep waiting for the ACK.
                        is_terminal, emsg = _classify_error_notification(frame.get("params", {}) or {})
                        if is_terminal:
                            state_machine.turn_completed()
                            return _stamp_drift({"error": f"codex error: {emsg or 'unknown error'}"}, acc)
                    # Any other pre-ack frame (stray notification) is ignored.
                    continue

                # Phase 2: event stream — delegate to the shared child-frame handler
                # (so a frame is processed identically here and in the #252 approval drain).
                if kind == "notification":
                    _res = _handle_child_frame(frame, ts)
                    if _res is not None:
                        state_machine.turn_completed()
                        return _stamp_drift(_res, acc)
                    continue

            # EOF check AFTER draining this batch: a child that wrote turn/completed
            # then exited has its completion consumed by the pump above (→ returns
            # success). Only a child that died WITHOUT completing reaches here.
            if manager._child is not None and manager._child.poll() is not None:
                if cancel_pending:                   # R1-F2: cancel pending + child died → graceful COLD
                    return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                eof_err = state_machine.eof_error()
                manager._child = None
                return _stamp_drift(eof_err, acc)

            # Pending cancel + turn ACKed + child alive → interrupt now (warm if turn_id known,
            # else cold for a review/start ACK without a turn id). Runs AFTER the batch so a
            # same-batch ACK + deltas were captured → the partial work is returned (R1-F2).
            if cancel_pending and turn_acked:
                return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "cancel", state_machine), acc)

            # Phase 1 timeout check (after each pump batch)
            if not turn_acked and time.time() > ack_deadline:
                if cancel_pending:                   # R1-F2: cancel pending, ACK never arrived → graceful COLD
                    return _stamp_drift(_finish_interrupt(manager, ts, None, "cancel", state_machine), acc)
                state_machine.turn_completed()
                return _stamp_drift({"error": f"{start_method} response timed out"}, acc)

        # Opt-in work-duration deadline exceeded (only reachable when timeout was set).
        if _interrupts_enabled():                    # #218: graceful interrupt + resumable partial
            return _stamp_drift(_finish_interrupt(manager, ts, turn_id, "timeout", state_machine), acc)
        state_machine.turn_completed()               # kill-switch: legacy bare error
        return _stamp_drift({"error": f"turn timed out after {turn_timeout} s"}, acc)

    except Exception as e:
        state_machine.turn_completed()
        return _stamp_drift({"error": f"turn execution error: {e}"}, acc)


def _classify_error_notification(params: dict) -> tuple:
    """Classify a codex `error` notification (shared by codex_run / codex_review).

    `willRetry: true` → transient stream reconnect (e.g. "Reconnecting N/5"); codex
    retries on its own, so it is NOT terminal. Returns (is_terminal, message)."""
    err = params.get("error")
    msg = err.get("message") if isinstance(err, dict) else err
    return (not params.get("willRetry"), msg)


def _build_result_meta(manager, usage_snapshot: dict, turn_start_t: float,
                       mcp_mode: str, mcp_servers_enabled: list, effort_val,
                       model_val, status: str) -> dict:
    """Build the ADDITIVE result metadata (usage/codex/timing/status).

    `status` is "completed" on success or "failed" on a terminal turn failure (F11) —
    a failed turn still consumed tokens, so usage/timing observability matters there too.
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
        "timing": {"duration_ms": int((time.time() - turn_start_t) * 1000)},
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
