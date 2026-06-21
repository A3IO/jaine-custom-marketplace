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
import tomllib

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

    def ensure(self, isolation_argv: list | None = None) -> object:
        """Return the live child, spawning (and initializing) if necessary.

        Warm reuse iff the child is alive AND its isolation signature matches the
        request. A changed signature (different `mcp` selection) kills the old child
        and respawns with the new spawn argv — re-paying codex's one-time cold start.
        Crash (dead child) also respawns. Idempotent for the same signature.
        """
        sig = tuple(isolation_argv or [])
        alive = _is_child_alive(self._child)
        if alive and self._isolation_sig == sig:
            return self._child
        if alive:                          # signature changed → kill the old child
            try:
                self._child.kill()
            except Exception:
                pass

        # Spawn a new child. _bin is None (lazy: resolve the codex binary from the CURRENT
        # env per spawn — #227 item 1c), a fixed path string, or a pre-built child object.
        codex_bin = _resolve_codex_bin() if self._bin is None else self._bin
        if isinstance(codex_bin, str):
            child = _spawn_appserver(codex_bin, list(isolation_argv or []))
        elif self._child is None:
            # First call: use the provided bin object directly (FakeChild in tests)
            child = self._bin
        else:
            # Respawn (crash or signature change): fresh instance via the same class
            child = self._bin.__class__()

        self._isolation_sig = sig
        self._adopt(child)
        try:
            self._do_initialize()
        except Exception:
            # initialize failed (e.g. cold-start timeout) — tear down so the next
            # ensure() does NOT warm-reuse an alive-but-uninitialised child (which would
            # hang start_thread on a server with no session). Commit (sig, child) only
            # when BOTH spawn AND initialize succeed; else clear both.
            try:
                if self._child is not None:
                    self._child.kill()
            except Exception:
                pass
            self._child = None
            self._isolation_sig = None
            raise
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
        if not _codex_bin_available():   # lazy resolve (#227): mid-session install/upgrade is seen
            return _stamp_drift({
                "error": (
                    f"codex binary not found at '{_resolve_codex_bin()}'. "
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
    usage_snapshot: dict = {}
    turn_start_t = time.time()
    state_machine.turn_started(cc_id)
    reactor = manager._reactor
    final_message_parts: list[str] = []
    retries = 0   # transient stream reconnects (willRetry errors); surfaced if >0

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

        while deadline is None or time.time() < deadline:
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
                    # working — credit it back so a slow human approval doesn't trip the
                    # opt-in turn timeout (if set) nor the turn/start ACK setup window.
                    _t0 = time.time()
                    manager._write(handle_server_request(frame, cc_write_fn, cc_read_fn, acc=acc))
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

                # Phase 2: event stream
                if kind == "notification":
                    if method == "item/agentMessage/delta":
                        # `or ""`: a present-but-null delta returns None from .get(k, "") (#18)
                        final_message_parts.append(frame.get("params", {}).get("delta") or "")
                    elif method == "item/completed" and review_target is not None:
                        # Native review output is delivered as a COMPLETED agentMessage
                        # item (.text), NOT as deltas (verified live, codex 0.141).
                        _it = frame.get("params", {}).get("item", {}) or {}
                        if _it.get("type") == "agentMessage":
                            final_message_parts.append(_it.get("text") or "")   # null-safe (#18)
                    elif method == "thread/tokenUsage/updated":
                        # Wire: params.tokenUsage = {last, total} (NOT params.usage). See spec 2a.
                        tu = frame.get("params", {}).get("tokenUsage")
                        if isinstance(tu, dict):
                            usage_snapshot = tu
                    elif method == "turn/completed":
                        t = frame.get("params", {}).get("turn", {}) or {}
                        if t.get("status") != "completed" or t.get("error"):  # TurnStatus has no "success" (codex 0.141: completed/interrupted/failed/inProgress)
                            state_machine.turn_completed()
                            meta = _build_result_meta(manager, usage_snapshot, turn_start_t,
                                                      mcp_mode, mcp_servers_enabled,
                                                      effort_val, model_val, "failed")
                            return _stamp_drift({
                                "error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                                "thread_id": thread_id, **meta}, acc)
                        state_machine.turn_completed()
                        meta = _build_result_meta(manager, usage_snapshot, turn_start_t,
                                                  mcp_mode, mcp_servers_enabled,
                                                  effort_val, model_val, "completed")
                        if retries:
                            meta["retries"] = retries
                        return _stamp_drift(
                            _shape_result(mode, thread_id, "".join(final_message_parts), meta), acc)
                    elif method == "error":
                        # codex `error` notification. willRetry:true = transient stream
                        # reconnect (codex retries on its own) → NOT drift, NOT terminal.
                        # Otherwise → terminal failure surfaced as a structured signal
                        # (#204 parking-lot), NOT UNKNOWN_NOTIFICATION. See spec 2026-06-21 Item 4.
                        is_terminal, emsg = _classify_error_notification(frame.get("params", {}) or {})
                        if not is_terminal:
                            retries += 1
                        else:
                            state_machine.turn_completed()
                            meta = _build_result_meta(manager, usage_snapshot, turn_start_t,
                                                      mcp_mode, mcp_servers_enabled,
                                                      effort_val, model_val, "failed")
                            return _stamp_drift({
                                "error": f"codex error: {emsg or 'unknown error'}",
                                "thread_id": thread_id, **meta}, acc)
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
                return _stamp_drift({"error": f"{start_method} response timed out"}, acc)

        # Opt-in work-duration deadline exceeded (only reachable when timeout was set).
        state_machine.turn_completed()
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
