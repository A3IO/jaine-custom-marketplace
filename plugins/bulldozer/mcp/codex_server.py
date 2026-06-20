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
dispatcher holds stdin exclusively for cc_read_fn; MUST use sys.stdin.readline()
consistently in the loop (NOT `for line in sys.stdin`) to allow cc_read_fn to
read mid-turn elicitation responses without buffering corruption.

See docs/superpowers/specs/2026-06-18-codex-mcp-v2-app-server-bridge.md.
"""
import atexit
import datetime
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

CODEX = os.environ.get("JAINE_CODEX_BIN") or shutil.which("codex") or "/opt/homebrew/bin/codex"
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
            "Isolated per-thread from ~/.codex config and codex-plugin skills by default. "
            "Returns a _drift array if upstream codex protocol drift is detected."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["prompt"],
            "properties": {
                "prompt": {"type": "string"},
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
                "config": {"type": "object"},
            },
        },
    }
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


def reply(mid, result=None, error=None):
    """Write a standard JSON-RPC 2.0 frame to CC (stdout)."""
    msg = {"jsonrpc": "2.0", "id": mid}
    msg["error" if error else "result"] = error if error else result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    """V2 MCP dispatcher: reads CC requests via sys.stdin.readline() (NOT the
    iterator) so cc_read_fn can safely call readline() mid-turn without
    buffering corruption."""

    def cc_write_fn(frame: dict):
        """Forward a CC-facing JSON-RPC 2.0 frame to stdout (elicitation/create etc.)."""
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    def cc_read_fn(timeout: float = 10.0):
        """Read the next CC elicitation response from stdin.

        Returns the parsed dict or None on timeout / EOF.
        bridge_approval expects: {"jsonrpc":"2.0","id":N,"result":{"action":...,"content":...}}
        """
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    while True:
        line = sys.stdin.readline()
        if not line:
            break  # EOF
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid, params = req.get("method"), req.get("id"), req.get("params", {}) or {}

        if method == "initialize":
            reply(mid, {
                "protocolVersion": params.get("protocolVersion", PROTO),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "bulldozer-codex", "version": _plugin_version()},
            })
        elif method == "notifications/initialized":
            pass  # notification — no reply
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                args = params.get("arguments") or {}  # `arguments: null` → {} (not None)
                args["_cc_id"] = mid  # inject for busy/eof framing
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
            except OSError:
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()

    def pump(self, timeout: float = 0.1) -> list:
        """Return complete JSON-RPC frames received from the child.

        Uses select() so it never blocks longer than `timeout` seconds even
        if the child sends a partial frame.
        """
        ready, _, _ = select.select([self._child_out_fd], [], [], timeout)
        if not ready:
            return []
        try:
            chunk = os.read(self._child_out_fd, 65536)
        except OSError:
            return []
        if not chunk:
            return []
        return self._stream.feed(chunk)


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
# NOTE (2026-06-20, A3IO/jaine-plugins#204): this mcp_servers={} override is a NO-OP against
# codex 0.141 — the isolated thread STILL loads ALL user + built-in MCP servers (the -c TOML
# override does not clear the [mcp_servers.*] table; empirically verified, identical to no flag).
# app-server has NO --ignore-user-config flag. REAL isolation (verified, not yet wired here):
# point CODEX_HOME at a dir seeded with only auth.json + `--disable apps --disable computer_use`
# -> 0 servers, auth intact. CODEX_HOME *can* be relocated (the earlier "cannot" was wrong); the
# only cost is that rollouts / cross-session resume move with CODEX_HOME. Until #204 ships, this
# map only documents intent; the _CONFIG_DENY scrub below still meaningfully blocks caller keys.
ISOLATION_CONFIG = {"mcp_servers": {}}

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


def _spawn_appserver(codex_bin: str) -> _ChildHandle:
    """Spawn `codex app-server` and return a _ChildHandle.

    `-c mcp_servers={}` disables the user's MCP servers at the PROCESS level
    (stronger than the per-thread `config` override, which only fires after the
    process is already alive).  Empirically: cold thread/start dropped from
    51.6 s → 34.7 s (codex was loading MCP servers before/independent of the
    per-thread override).  The per-thread ISOLATION_CONFIG is kept as a
    belt-and-suspenders guard for any config the process-level flag doesn't cover.
    """
    proc = subprocess.Popen(
        [codex_bin, "app-server", "-c", "mcp_servers={}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    - A string path (or CODEX constant): spawned via `codex app-server`.
    - A pre-built child object (FakeChild in tests) with .stdin/.stdout/.stderr
      and .poll()/.kill()/.returncode.  Used directly on the first ensure();
      respawned via `bin.__class__()` on subsequent ensure() after a crash.
    """

    def __init__(self, bin=None):
        self._bin = bin or CODEX
        self._child = None
        self._reactor: Reactor | None = None
        self._next_id_val = 1
        self._next_id_lock = threading.Lock()
        self._codex_version = None

    def _next_id(self) -> int:
        with self._next_id_lock:
            mid = self._next_id_val
            self._next_id_val += 1
        return mid

    def _write(self, msg: dict):
        """Write a single JSON-RPC frame to the child's stdin."""
        data = (json.dumps(msg) + "\n").encode()
        self._child.stdin.write(data)
        self._child.stdin.flush()

    def _pump_until(self, predicate, timeout: float = 5.0) -> dict | None:
        """Pump the reactor until predicate(frame) is True or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            frames = self._reactor.pump(timeout=min(remaining, 0.2))
            for frame in frames:
                if predicate(frame):
                    return frame
        return None

    def _do_initialize(self):
        """Run the initialize → initialized handshake with the child."""
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
        })
        # Wait for the initialize response.
        # _pump_until is safe here: any frames in the same chunk after the initialize
        # response are post-handshake notifications — dropping them is acceptable
        # because nothing post-response is consumed at this stage.
        resp = self._pump_until(
            lambda f: classify(f) == "response" and f.get("id") == mid,
            timeout=10.0,
        )
        if resp is None:
            raise RuntimeError("AppServerManager: initialize response timed out")
        ua = (resp.get("result") or {}).get("userAgent", "") if isinstance(resp, dict) else ""
        self._codex_version = _parse_codex_version(ua)
        if self._codex_version != LAST_VERIFIED_CODEX_VERSION:
            _drift_warn(None, "VERSION_MISMATCH",
                        f"last-verified {LAST_VERIFIED_CODEX_VERSION}, live {ua!r}")
        # Send the initialized notification
        self._write({"method": "initialized"})

    def _adopt(self, child) -> object:
        """Wire up a new child: create Reactor, start stderr drain."""
        # On respawn, close the previous reactor's stderr drain file so we don't leak
        # a TemporaryFile (+ its drain-thread reference) across crash-respawns.
        old = getattr(self, "_reactor", None)
        if old is not None and getattr(old, "stderr_file", None) is not None:
            try:
                old.stderr_file.close()
            except Exception:
                pass
        self._child = child
        self._reactor = Reactor(
            child_out_fd=child.stdout.fileno(),
            child_in_fd=child.stdin.fileno(),
        )
        self._reactor._start_stderr_drain(child.stderr.fileno())
        return child

    def ensure(self) -> object:
        """Return the live child, spawning (and initializing) if necessary.

        Detects a dead child (poll() is not None) and respawns on the next call.
        Idempotent when the child is alive.
        """
        if _is_child_alive(self._child):
            return self._child

        # Spawn a new child
        if isinstance(self._bin, str):
            child = _spawn_appserver(self._bin)
        elif self._child is None:
            # First call: use the provided bin object directly (FakeChild in tests)
            child = self._bin
        else:
            # Respawn after crash: create a fresh instance via the same class
            child = self._bin.__class__()

        self._adopt(child)
        self._do_initialize()
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
    ) -> str:
        """Send thread/start and return the thread_id.

        NON-ephemeral by default (ephemeral=True only when one_shot=True,
        as ephemeral threads have no on-disk rollout → no cross-session resume).

        base_instructions: None → STERILE_INSTRUCTIONS (sentinel); "" is a valid caller value.
        developer_instructions: None → omitted from params; any string → wire key developerInstructions.
        """
        bi = STERILE_INSTRUCTIONS if base_instructions is None else base_instructions
        caller_cfg = config if isinstance(config, dict) else {}
        merged = {k: v for k, v in caller_cfg.items() if k not in _CONFIG_DENY}
        merged.update(ISOLATION_CONFIG)     # our keys always win
        config = merged
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
        return resp.get("result", {})


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


def bridge_approval(method: str, params: dict, cc_write_fn, cc_read_fn,
                    timeout: float = 300.0, acc=None):
    """Issue a CC elicitation/create, wait for the answer, return the codex decision.

    CC-facing elicitation request: standard JSON-RPC 2.0 (has "jsonrpc":"2.0").
    Return value: the codex decision payload (string or dict) — NOT a full frame.
    The caller wraps it in the appropriate {id, result: {...}} envelope.

    On CC decline / cancel / timeout → safe default ("decline" for approvals).
    """
    eid = _next_bridge_id()

    def read_correlated(eid: int, timeout: float):
        """Read from CC, skipping frames whose id != eid (e.g. ping/notifications)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            frame = cc_read_fn(timeout=remaining)
            if frame is None:
                continue  # transient (blank line, JSON decode error, select timeout) — retry within the deadline, do NOT prematurely decline
            # Shape-first: ONLY a RESPONSE whose id matches resolves the elicitation.
            # A REQUEST whose id numerically collides with eid must NOT be mistaken
            # for the reply (it falls through to be skipped / handled by its method).
            if frame.get("id") == eid and classify(frame) == "response":
                return frame
            # Skip unrelated frame (ping, notification, id-colliding request); keep reading.
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
                "message": (
                    f"Codex approval request\n"
                    f"Command: {params.get('command') or '(none)'}\n"
                    f"CWD: {params.get('cwd') or '(unknown)'}"
                ),
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
                "message": (
                    f"Codex file change approval\n"
                    f"Reason: {params.get('reason') or '(none)'}"
                ),
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
        perm_pairs = [
            (LBL_GRANT_TURN, {"permissions": {}, "scope": "turn"}),
            (LBL_GRANT_SESSION, {"permissions": {}, "scope": "session"}),
            (LBL_DONT_GRANT, PERM_DECLINE),
        ]
        labels = [lbl for lbl, _ in perm_pairs]
        perm_map = dict(perm_pairs)
        cc_write_fn({
            "jsonrpc": "2.0",
            "id": eid,
            "method": "elicitation/create",
            "params": {
                "message": (
                    f"Codex permissions approval\n"
                    f"Reason: {params.get('reason') or '(none)'}"
                ),
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
            # Accept w/o dropdown = grant for this turn (the minimal grant)
            chosen = content.get("label", LBL_GRANT_TURN)
            if chosen not in perm_map:
                _drift_warn(acc, "OUT_OF_ENUM_LABEL", str(chosen))
            return perm_map.get(chosen, perm_map[LBL_GRANT_TURN])
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

_KNOWN_NOTIFICATIONS = frozenset({
    "item/agentMessage/delta", "item/completed", "turn/completed",
    "turn/started", "item/started",   # benign lifecycle events
    # Real codex app-server benign lifecycle/status notifications (observed live via the
    # MCP tool's _drift, 2026-06-20). Allowlisted so a healthy turn returns no _drift.
    # NOTE: "error"/"warning" are deliberately NOT here — they signal problems (tracked
    # in issue #204), not benign lifecycle.
    "thread/settings/updated", "thread/status/changed", "thread/tokenUsage/updated",
    "mcpServer/startupStatus/updated", "hook/started", "hook/completed",
    "account/rateLimits/updated", "skills/changed", "remoteControl/status/changed",
})


def handle_server_request(msg: dict, cc_write_fn=None, cc_read_fn=None,
                          timeout: float = 300.0, acc=None) -> dict:
    """Route a server→client ServerRequest to the correct handler.

    Returns a jsonrpc_lite frame ({id, result} or {id, error}) — NO 'jsonrpc' key.
    Never returns None (spec invariant: never drop a ServerRequest).
    acc: optional list — if provided, drift breadcrumbs are appended via _drift_warn.
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
        decision = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc)
        return {"id": mid, "result": {"decision": decision}}

    if method == "item/fileChange/requestApproval":
        decision = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc)
        return {"id": mid, "result": {"decision": decision}}

    if method == "item/permissions/requestApproval":
        grant = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc)
        return {"id": mid, "result": grant}

    if method == "item/tool/requestUserInput":
        answers = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc)
        return {"id": mid, "result": answers}

    if method == "mcpServer/elicitation/request":
        elicit_result = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc)
        return {"id": mid, "result": elicit_result}

    if method in ("execCommandApproval", "applyPatchApproval"):
        review = bridge_approval(method, params, cc_write_fn, cc_read_fn, timeout, acc=acc)
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
        _v2_manager = AppServerManager(bin=CODEX)
    return _v2_manager


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

    # ── Graceful no-codex (carry v1 behaviour) ─────────────────────────────
    if manager is None:
        if not (os.path.isfile(CODEX) or shutil.which(CODEX)):
            return _stamp_drift({
                "error": (
                    f"codex binary not found at '{CODEX}'. "
                    "Install codex or set JAINE_CODEX_BIN."
                )
            }, acc)
        manager = _get_manager()

    if state_machine is None:
        state_machine = _v2_state_machine

    prompt = args.get("prompt")
    if not prompt:
        return _stamp_drift({"error": "prompt is required"}, acc)

    mode = args.get("mode", "review")

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
        manager.ensure()
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
    state_machine.turn_started(cc_id)
    reactor = manager._reactor
    final_message_parts: list[str] = []

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
        manager._write({"id": mid, "method": "turn/start", "params": turn_params})

        # Unified pump: Phase 1 = waiting for turn/start ACK; Phase 2 = event stream
        turn_acked = False
        deadline = time.time() + 120.0  # generous timeout for real codex
        ack_deadline = time.time() + 10.0

        while time.time() < deadline:
            frames = reactor.pump(timeout=0.2)
            for frame in frames:
                kind = classify(frame)
                method = frame.get("method", "")

                # Server→client requests (approvals) are bridged in EITHER phase: an
                # approval can in principle arrive before the TurnStartResponse, and
                # dropping it would hang the app-server until the outer deadline.
                if kind == "request":
                    # An approval blocks on a HUMAN reply (up to the elicitation
                    # timeout). That wall-clock is the user's thinking time, not codex
                    # working — credit it back to the turn deadline, else a slow human
                    # approval would trip the 120s turn timeout right after they answered.
                    _t0 = time.time()
                    manager._write(handle_server_request(frame, cc_write_fn, cc_read_fn, acc=acc))
                    deadline += time.time() - _t0
                    continue

                if not turn_acked:
                    # Phase 1: looking for the TurnStartResponse (our turn/start id)
                    if kind == "response" and frame.get("id") == mid:
                        if "error" in frame:
                            state_machine.turn_completed()
                            return _stamp_drift({"error": f"turn/start error: {frame['error']}"}, acc)
                        turn_acked = True
                    # Any other pre-ack frame (stray notification) is not expected per
                    # spec; ignore it.
                    continue

                # Phase 2: event stream
                if kind == "notification":
                    if method == "item/agentMessage/delta":
                        final_message_parts.append(frame.get("params", {}).get("delta", ""))
                    elif method == "turn/completed":
                        t = frame.get("params", {}).get("turn", {}) or {}
                        if t.get("status") not in ("completed", "success") or t.get("error"):
                            state_machine.turn_completed()
                            return _stamp_drift({
                                "error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                                "thread_id": thread_id}, acc)
                        state_machine.turn_completed()
                        return _stamp_drift(_shape_result(mode, thread_id, "".join(final_message_parts)), acc)
                    elif method not in _KNOWN_NOTIFICATIONS:
                        _drift_warn(acc, "UNKNOWN_NOTIFICATION", method)
                    # known-but-ignored (item/completed, turn/started, ...) → no-op
                    continue

            # EOF check AFTER draining this batch: a child that wrote turn/completed
            # then exited has its completion consumed by the pump above (→ returns
            # success). Only a child that died WITHOUT completing reaches here.
            if manager._child is not None and manager._child.poll() is not None:
                eof_err = state_machine.eof_error()
                manager._child = None
                return _stamp_drift(eof_err, acc)

            # Phase 1 timeout check (after each pump batch)
            if not turn_acked and time.time() > ack_deadline:
                state_machine.turn_completed()
                return _stamp_drift({"error": "turn/start response timed out"}, acc)

        # Deadline exceeded
        state_machine.turn_completed()
        return _stamp_drift({"error": "turn timed out after 120 s"}, acc)

    except Exception as e:
        state_machine.turn_completed()
        return _stamp_drift({"error": f"turn execution error: {e}"}, acc)


def _shape_result(mode: str, thread_id: str, final_text: str) -> dict:
    """Return the mode-shaped result dict.

    review mode: {thread_id, verdict, findings, schema_ok}
    implement mode: {thread_id, result}
    """
    if mode == "review":
        try:
            parsed = json.loads(final_text)
            return {
                "thread_id": thread_id,
                "schema_ok": True,
                "verdict": parsed.get("verdict", "UNKNOWN"),
                "findings": parsed.get("findings", []),
            }
        except (json.JSONDecodeError, AttributeError):
            return {
                "thread_id": thread_id,
                "schema_ok": False,
                "verdict": "UNKNOWN",
                "findings": [],
                "raw": final_text,
            }
    return {"thread_id": thread_id, "result": final_text}


# Entrypoint LAST: main() is the v2 dispatcher, which references codex_run_v2 and
# the rest of the v2 section above — so the guard must come after they are defined.
if __name__ == "__main__":
    main()
