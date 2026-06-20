"""Offline + e2e suite for the codex MCP v2 app-server bridge.

Grows task-by-task (plan docs/superpowers/plans/2026-06-18-codex-mcp-v2.md):
  Task 1 — resume gating (real codex, self-skips)
  Task 2 — JsonRpcStream / classify / reactor (offline, fake_appserver)
  Task 3 — AppServerManager lifecycle (offline)
  Task 4 — approval bridge + ServerRequest coverage (offline)
  Task 5 — codex_run tool (offline)
  Task 6 — live e2e (real codex, self-skips)

Real-codex tests self-skip when `codex` is absent so the default suite stays
offline/fast.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

MCP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp")
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
sys.path.insert(0, MCP_DIR)


def _has_codex():
    return bool(
        os.environ.get("JAINE_CODEX_BIN")
        and os.path.isfile(os.environ["JAINE_CODEX_BIN"])
        or shutil.which("codex")
        or os.path.isfile("/opt/homebrew/bin/codex")
    )


skip_if_no_codex = pytest.mark.skipif(not _has_codex(), reason="codex CLI not installed")


# ---------------------------------------------------------------------------
# Task 2: JsonRpcStream, classify, Reactor
# ---------------------------------------------------------------------------

def test_jsonrpcstream_splits_complete_frames_only():
    from codex_server import JsonRpcStream
    s = JsonRpcStream()
    assert s.feed(b'{"id":1,') == []          # partial: nothing yet
    assert s.feed(b'"result":{}}\n{"method":"x"}\n') == [
        {"id": 1, "result": {}}, {"method": "x"}]   # two complete frames


def test_classify_is_shape_first_not_id():
    from codex_server import classify
    assert classify({"id": 7, "result": {}}) == "response"
    assert classify({"id": 7, "method": "tools/call"}) == "request"   # same id 7, but REQUEST
    assert classify({"method": "x"}) == "notification"
    assert classify({"id": 7, "error": {"code": -1}}) == "response"
    assert classify({"garbage": 1}) == "invalid"


def test_reactor_reads_frames_from_both_fds_no_deadlock():
    """Drive the Reactor against fake_appserver.py; verify it collects frames
    from the child (simulate a split partial frame), and drains stderr without
    deadlocking. Times out in 10 s if the reactor blocks."""
    from codex_server import JsonRpcStream, Reactor

    fake = os.path.join(FIXTURES_DIR, "fake_appserver.py")
    env = os.environ.copy()
    env["FAKE_SCRIPT"] = "basic"

    proc = subprocess.Popen(
        [sys.executable, fake],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        reactor = Reactor(proc.stdout.fileno(), proc.stdin.fileno())
        # Start stderr drain BEFORE pumping so stderr flood can't deadlock us
        reactor._start_stderr_drain(proc.stderr.fileno())

        # Send initialize request (split across two writes to test partial framing)
        import json
        init_req = json.dumps({"id": 1, "method": "initialize",
                               "params": {"clientInfo": {"name": "test"}}})
        half = len(init_req) // 2
        proc.stdin.write(init_req[:half].encode())
        proc.stdin.flush()
        time.sleep(0.05)
        proc.stdin.write((init_req[half:] + "\n").encode())
        proc.stdin.flush()

        # Pump until we get the initialize response
        deadline = time.time() + 10
        collected = []
        while time.time() < deadline:
            frames = reactor.pump(timeout=0.5)
            collected.extend(frames)
            # Look for the initialize response
            if any("result" in f and f.get("id") == 1 for f in collected):
                break
        else:
            pytest.fail(f"Reactor timed out waiting for initialize response. "
                        f"Collected frames: {collected}")

        assert any(f.get("id") == 1 and "result" in f for f in collected), \
            f"No initialize response in frames: {collected}"

        # Verify stderr was drained (fake floods stderr; no deadlock means it worked)
        # We already completed without hanging, which proves no deadlock.
        # Also check the stderr temp file is non-None (Reactor drains to temp file).
        assert reactor.stderr_file is not None, "Reactor must have a stderr drain file"

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        proc.wait()


def test_reactor_sees_server_request_frame():
    """Reactor must classify and surface server→client REQUEST frames (have both
    id and method). The fake emits one during the approval script."""
    from codex_server import JsonRpcStream, Reactor, classify

    fake = os.path.join(FIXTURES_DIR, "fake_appserver.py")
    env = os.environ.copy()
    env["FAKE_SCRIPT"] = "with_approval"

    proc = subprocess.Popen(
        [sys.executable, fake],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    try:
        import json
        reactor = Reactor(proc.stdout.fileno(), proc.stdin.fileno())
        reactor._start_stderr_drain(proc.stderr.fileno())  # symmetry: drain the fake's stderr flood

        # Initialize
        req = json.dumps({"id": 1, "method": "initialize",
                          "params": {"clientInfo": {"name": "test"}}}) + "\n"
        proc.stdin.write(req.encode())
        proc.stdin.flush()

        # thread/start
        ts = json.dumps({"id": 2, "method": "thread/start",
                         "params": {"cwd": "/tmp", "approvalPolicy": "auto",
                                    "sandbox": "read-only"}}) + "\n"
        proc.stdin.write(ts.encode())
        proc.stdin.flush()

        # turn/start
        turn = json.dumps({"id": 3, "method": "turn/start",
                           "params": {"threadId": "T1",
                                      "input": [{"type": "text", "text": "hi",
                                                 "text_elements": []}]}}) + "\n"
        proc.stdin.write(turn.encode())
        proc.stdin.flush()

        deadline = time.time() + 10
        collected = []
        found_server_req = False
        while time.time() < deadline:
            frames = reactor.pump(timeout=0.5)
            for f in frames:
                collected.append(f)
                if classify(f) == "request":  # server→client request
                    found_server_req = True
                    # Reply with decline so fake can continue
                    reply = json.dumps({"id": f["id"], "result": {"decision": "decline"}}) + "\n"
                    proc.stdin.write(reply.encode())
                    proc.stdin.flush()
            # Look for turn/completed to know we're done
            completed = next((f for f in frames if f.get("method") == "turn/completed"), None)
            if completed is not None:
                break
        else:
            pytest.fail(f"Reactor timed out. Frames seen: {len(collected)}")

        assert found_server_req, \
            f"Expected a server→client request frame (approval); got: {collected}"
        # The approval round-trip actually COMPLETED — not the timeout/deadlock path.
        # Regression: the fake used to t.join() the handler thread, blocking the main
        # loop from reading this reply, so the waiter timed out → status='failed' and
        # the offline approval round-trip was never truly exercised.
        status = (completed.get("params", {}).get("turn", {}) or {}).get("status")
        assert status == "completed", (
            f"Expected turn/completed status='completed' (approval round-trip succeeded), "
            f"got: {status!r}; frames: {collected}"
        )

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        proc.wait()


# ---------------------------------------------------------------------------
# Task 3: AppServerManager — lifecycle, initialize handshake, isolation, crash-respawn
# ---------------------------------------------------------------------------

import json as _json
import threading as _threading
import io as _io


class FakeChild:
    """In-process fake `codex app-server` child for AppServerManager tests.

    Simulates the app-server wire protocol (jsonrpc_lite, newline-delimited).
    The manager writes JSON-RPC frames to `.stdin`; the fake reads them,
    records them in `.received_msgs`, and sends back canned responses via
    in-memory pipes exposed as `.stdout` / `.stderr`.

    Lifecycle:
    - Created alive (`.poll()` returns None).
    - `.kill()` closes pipes → EOF on both sides → manager detects dead child.
    """

    def __init__(self):
        # Pipes: manager writes to _client_write_r/w, fake reads from _client_read_r/w
        # Manager side: writes to self.stdin (write end), reads from self.stdout (read end)
        # Fake server side: reads from _srv_stdin (read end), writes to _srv_stdout (write end)

        # Pipe pair: manager→fake  (manager writes, fake reads)
        mgr_to_fake_r, mgr_to_fake_w = os.pipe()
        # Pipe pair: fake→manager  (fake writes, manager reads)
        fake_to_mgr_r, fake_to_mgr_w = os.pipe()
        # Pipe pair: stderr (fake writes, manager can drain)
        stderr_r, stderr_w = os.pipe()

        # Expose as file-like objects the manager can use
        self.stdin = _io.open(mgr_to_fake_w, "wb", buffering=0)
        self.stdout = _io.open(fake_to_mgr_r, "rb", buffering=0)
        self.stderr = _io.open(stderr_r, "rb", buffering=0)

        self._srv_stdin = _io.open(mgr_to_fake_r, "rb", buffering=0)
        self._srv_stdout = _io.open(fake_to_mgr_w, "wb", buffering=0)
        self._srv_stderr = _io.open(stderr_w, "wb", buffering=0)

        # Message log: list of dicts sent TO us by the manager
        self.received_msgs: list = []
        self._lock = _threading.Lock()
        self._dead = False
        self.returncode = None

        # Start fake server loop in background
        t = _threading.Thread(target=self._serve, daemon=True)
        t.start()

    def _write_msg(self, msg: dict):
        data = (_json.dumps(msg) + "\n").encode()
        try:
            self._srv_stdout.write(data)
            self._srv_stdout.flush()
        except (OSError, ValueError):
            pass

    def _serve(self):
        """Read requests from manager, record them, send canned replies."""
        buf = b""
        while True:
            try:
                chunk = self._srv_stdin.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                with self._lock:
                    self.received_msgs.append(msg)
                self._dispatch(msg)

    def _dispatch(self, msg: dict):
        # jsonrpc_lite contract: the app-server wire protocol omits "jsonrpc".
        # Any frame from the manager that includes "jsonrpc" is a contract violation.
        assert "jsonrpc" not in msg, (
            f"FakeChild received a frame with 'jsonrpc' field — "
            f"app-server frames must be jsonrpc_lite (no 'jsonrpc' key). "
            f"Frame: {msg!r}"
        )
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            self._write_msg({"id": mid, "result": {
                "userAgent": "bulldozer-codex-mcp/0.141.0 (Mac OS 26.4.1; arm64) fake/1.0 (bulldozer-codex-mcp; 0.0.0)",
                "codexHome": "/tmp/fake-codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
            }})
        elif method == "initialized":
            pass  # notification — no reply
        elif method == "thread/start":
            self._write_msg({"id": mid, "result": {
                "thread": {"id": "T1", "sessionId": "S1", "status": "idle"},
                "model": "gpt-4o",
                "cwd": msg.get("params", {}).get("cwd", "/tmp"),
                "approvalPolicy": "on-request",
                "sandbox": "read-only",
            }})
        elif method == "thread/resume":
            self._write_msg({"id": mid, "result": {
                "thread": {"id": msg.get("params", {}).get("threadId", "T1"), "status": "idle"},
            }})

    def received(self, method: str, timeout: float = 1.0):
        """Return the first message with the given method, or None.

        Polls briefly (up to `timeout` seconds) to allow the background
        _serve thread to process notifications sent just before this call.
        """
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            with self._lock:
                for m in self.received_msgs:
                    if m.get("method") == method:
                        return m
            _time.sleep(0.01)
        return None

    def poll(self):
        """None = alive, 0 = dead (mirrors subprocess.Popen.poll)."""
        return None if not self._dead else 0

    def kill(self):
        """Simulate child death by closing all pipes (EOF)."""
        self._dead = True
        self.returncode = -9
        for f in (self.stdin, self.stdout, self.stderr,
                  self._srv_stdin, self._srv_stdout, self._srv_stderr):
            try:
                f.close()
            except (OSError, ValueError):
                pass


@pytest.fixture
def fake_child():
    fc = FakeChild()
    yield fc
    fc.kill()


def test_manager_initialize_sends_clientInfo_and_experimentalApi(fake_child):
    from codex_server import AppServerManager
    m = AppServerManager(bin=fake_child)
    m.ensure()
    init = fake_child.received("initialize")
    # InitializeParams REQUIRES clientInfo; ClientInfo = {name, title (REQUIRED, nullable), version}
    ci = init["params"]["clientInfo"]
    assert ci["name"] and ci["version"]
    assert "title" in ci          # required key, value may be null
    assert init["params"]["capabilities"]["experimentalApi"] is True
    assert fake_child.received("initialized") is not None  # notification


def test_start_thread_is_nonephemeral_and_isolated(tmp_path, fake_child):
    from codex_server import AppServerManager, STERILE_INSTRUCTIONS, ISOLATION_CONFIG
    m = AppServerManager(bin=fake_child)
    m.ensure()
    m.start_thread(sandbox="read-only", approval_policy="on-request",
                   base_instructions=STERILE_INSTRUCTIONS, config=ISOLATION_CONFIG,
                   cwd=str(tmp_path))
    p = fake_child.received("thread/start")["params"]
    assert p.get("ephemeral") in (None, False)   # NEVER True for resumable
    assert p["baseInstructions"] == STERILE_INSTRUCTIONS  # pinned sterile constant, not a placeholder
    assert p["config"] == ISOLATION_CONFIG               # pinned config-override policy
    assert p["sandbox"] == "read-only"
    assert p["cwd"] == str(tmp_path)   # cwd pinned at thread/start (app-server reads it at config-load)


# ── B1 helpers ─────────────────────────────────────────────────────────────
# Fresh manager+FakeChild per call so FakeChild.received() always sees the
# ONE thread/start produced by that call. Task B2 reuses this helper.

def _started_params(**kwargs):
    """Fresh manager+FakeChild, ONE start_thread, return its thread/start params dict."""
    from codex_server import AppServerManager
    fc = FakeChild()
    try:
        m = AppServerManager(bin=fc)
        m.ensure()
        m.start_thread(**kwargs)
        return fc.received("thread/start")["params"]
    finally:
        fc.kill()


def test_start_thread_base_instructions_sentinel():
    import codex_server as cs
    assert _started_params(base_instructions=None)["baseInstructions"] == cs.STERILE_INSTRUCTIONS
    assert _started_params(base_instructions="")["baseInstructions"] == ""   # "" is a valid caller value, not "omitted"


def test_start_thread_developer_instructions_wire_key():
    assert _started_params(developer_instructions="be terse")["developerInstructions"] == "be terse"
    assert "developerInstructions" not in _started_params()                  # omitted → key absent


def test_child_death_respawns_on_next_ensure(fake_child):
    from codex_server import AppServerManager
    m = AppServerManager(bin=fake_child)
    c1 = m.ensure()
    fake_child.kill()                 # simulate EOF
    c2 = m.ensure()
    assert c2 is not c1               # respawned


# ---------------------------------------------------------------------------
# Task 4: approval bridge + ServerRequest coverage + in-flight state machine
# ---------------------------------------------------------------------------

class FakeCC:
    """Simulates Claude Code's elicitation side for bridge tests.

    cc.write(msg) — called by bridge to send elicitation/create to CC.
    cc.read(timeout) — called by bridge to read CC's elicitation response.
    """

    def __init__(self):
        self._next_answer = ("accept", {"label": "accept"})  # default: accept
        self._requests: list = []
        self._pending: list = []

    def set_answer(self, action, content=None):
        self._next_answer = (action, content)

    def never_answer_elicitation(self):
        self._next_answer = None  # simulate timeout (read will sleep then return None)

    def write(self, msg: dict):
        # CC-facing frames MUST carry jsonrpc:2.0
        assert msg.get("jsonrpc") == "2.0", (
            f"FakeCC.write: expected jsonrpc:2.0 on CC-facing frame, got: {msg!r}"
        )
        self._requests.append(msg)
        if msg.get("method") == "elicitation/create":
            self._pending.append(msg["id"])

    def read(self, timeout=10.0):
        if self._next_answer is None:
            import time as _t
            _t.sleep(min(timeout, 0.05))  # short sleep, then None
            return None
        action, content = self._next_answer
        if not self._pending:
            return None
        eid = self._pending.pop(0)
        return {"jsonrpc": "2.0", "id": eid, "result": {"action": action, "content": content}}

    def pending_requests(self):
        return list(self._pending)


# ── Step 1 tests (build_command_approval_labels) ──────────────────────────

def test_command_approval_primary_path_uses_available_decisions_verbatim():
    """PRIMARY: availableDecisions present & non-empty → one human-labelled pair per
    entry, in codex's order, with the DECISION value preserved verbatim."""
    from codex_server import (
        build_command_approval_labels, LBL_ALLOW_ONCE, LBL_DONT_ALLOW, LBL_EXECPOLICY,
    )
    params = {
        "availableDecisions": [
            "accept",
            "decline",
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow foo"]}},
        ]
    }
    labels = build_command_approval_labels(params)
    assert len(labels) == 3
    assert labels[0] == (LBL_ALLOW_ONCE, "accept")
    assert labels[1] == (LBL_DONT_ALLOW, "decline")
    # Object at index 2: human label, decision is the verbatim dict
    label2, dec2 = labels[2]
    assert label2 == LBL_EXECPOLICY
    assert dec2 == {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow foo"]}}


def test_command_approval_fallback_derives_base_set():
    """FALLBACK: no availableDecisions → derive base set with human labels for
    {accept, acceptForSession, decline, cancel}."""
    from codex_server import (
        build_command_approval_labels,
        LBL_ALLOW_ONCE, LBL_ALLOW_SESSION, LBL_DONT_ALLOW, LBL_CANCEL,
    )
    labels = build_command_approval_labels({})
    d = dict(labels)
    assert d[LBL_ALLOW_ONCE] == "accept"
    assert d[LBL_ALLOW_SESSION] == "acceptForSession"
    assert d[LBL_DONT_ALLOW] == "decline"
    assert d[LBL_CANCEL] == "cancel"
    # No extras when no proposed amendments
    assert len(labels) == 4


def test_command_approval_fallback_derives_network_amendments_by_index():
    """FALLBACK: proposedNetworkPolicyAmendments → one human (host-bearing) label per
    amendment; decisions preserved verbatim, labels unique even for a shared host."""
    from codex_server import build_command_approval_labels
    params = {
        "proposedNetworkPolicyAmendments": [
            {"host": "x", "action": "allow"},
            {"host": "x", "action": "deny"},  # same host, different action
        ]
    }
    labels = build_command_approval_labels(params)
    net = [(lbl, dec) for lbl, dec in labels
           if isinstance(dec, dict) and "applyNetworkPolicyAmendment" in dec]
    assert len(net) == 2
    assert len({lbl for lbl, _ in net}) == 2, "network labels must be unique (reverse-map keys)"
    decs = [dec["applyNetworkPolicyAmendment"]["network_policy_amendment"] for _, dec in net]
    assert {"host": "x", "action": "allow"} in decs
    assert {"host": "x", "action": "deny"} in decs


def test_command_approval_fallback_derives_execpolicy_amendment():
    """FALLBACK: proposedExecpolicyAmendment present → one human-labelled execpolicy entry."""
    from codex_server import build_command_approval_labels, LBL_EXECPOLICY
    params = {"proposedExecpolicyAmendment": ["allow echo"]}
    labels = build_command_approval_labels(params)
    d = dict(labels)
    assert LBL_EXECPOLICY in d
    assert d[LBL_EXECPOLICY] == {
        "acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow echo"]}
    }


# ── Human-readable approval labels ────────────────────────────────────────

def test_approval_labels_are_human_readable():
    """Display labels are human-readable prose; the codex DECISION value is preserved
    exactly so the #18268 label→decision mapping stays intact. The human label doubles
    as the reverse-map key, so CC returning it must round-trip to the exact decision."""
    from codex_server import (
        build_command_approval_labels, handle_server_request,
        LBL_ALLOW_ONCE, LBL_ALLOW_SESSION, LBL_DONT_ALLOW, LBL_CANCEL,
    )
    # 1. Fallback base set: human labels → exact string decisions.
    base = dict(build_command_approval_labels({}))
    assert base[LBL_ALLOW_ONCE] == "accept"
    assert base[LBL_ALLOW_SESSION] == "acceptForSession"
    assert base[LBL_DONT_ALLOW] == "decline"
    assert base[LBL_CANCEL] == "cancel"
    # Labels are prose, not the raw machine tokens.
    assert "accept" not in base, "raw 'accept' token must not appear as a display label"

    # 2. Round-trip a human label through the bridge → exact decision.
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_ALLOW_ONCE})
    msg = {"id": "h1", "method": "item/commandExecution/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                      "startedAtMs": 1, "command": "echo", "cwd": "/tmp",
                      "availableDecisions": ["accept", "decline"]}}
    assert handle_server_request(msg, cc.write, cc.read)["result"]["decision"] == "accept"


def test_execpolicy_label_is_human_readable_and_round_trips():
    """An acceptWithExecpolicyAmendment dict decision gets a human label, and CC
    returning that label round-trips to the VERBATIM dict (not the fallback string)."""
    from codex_server import (
        build_command_approval_labels, handle_server_request, LBL_EXECPOLICY,
    )
    DEC = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow echo"]}}
    params = {"threadId": "T", "turnId": "U", "itemId": "I", "startedAtMs": 1,
              "command": "echo", "cwd": "/tmp", "availableDecisions": [DEC]}
    labels = dict(build_command_approval_labels(params))
    assert LBL_EXECPOLICY in labels
    assert labels[LBL_EXECPOLICY] == DEC
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_EXECPOLICY})
    msg = {"id": "h2", "method": "item/commandExecution/requestApproval", "params": params}
    assert handle_server_request(msg, cc.write, cc.read)["result"]["decision"] == DEC


def test_network_amendment_label_carries_host_and_is_unique():
    """Network amendment labels embed the host so the human can tell which host they're
    permitting, and stay UNIQUE even when two amendments share a host (reverse-map keys
    must not collide)."""
    from codex_server import build_command_approval_labels
    params = {"proposedNetworkPolicyAmendments": [
        {"host": "api.example.com", "action": "allow"},
        {"host": "api.example.com", "action": "deny"},  # same host, different action
    ]}
    labels = build_command_approval_labels(params)
    label_names = [l for l, _ in labels]
    net_labels = [n for n in label_names if "api.example.com" in n]
    assert len(net_labels) == 2, f"expected 2 host-bearing labels, got {label_names}"
    assert len(set(net_labels)) == 2, f"network labels must be unique: {net_labels}"
    d = dict(labels)
    decs = [d[n]["applyNetworkPolicyAmendment"]["network_policy_amendment"] for n in net_labels]
    assert {"host": "api.example.com", "action": "allow"} in decs
    assert {"host": "api.example.com", "action": "deny"} in decs


def test_permissions_and_legacy_human_labels_round_trip():
    """Permissions + legacy approval prompts also use human labels that round-trip to
    the exact codex decision (permissions: {permissions,scope}; legacy: ReviewDecision)."""
    from codex_server import handle_server_request, LBL_GRANT_SESSION, LBL_ALLOW_SESSION
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_GRANT_SESSION})
    msg = {"id": "p1", "method": "item/permissions/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I", "startedAtMs": 1,
                      "cwd": "/tmp", "reason": None, "permissions": {}}}
    r = handle_server_request(msg, cc.write, cc.read)["result"]
    assert r["scope"] == "session" and "permissions" in r
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_ALLOW_SESSION})
    msg = {"id": "l1", "method": "execCommandApproval",
           "params": {"conversationId": "T", "callId": "C", "command": ["ls"], "cwd": "/tmp"}}
    assert handle_server_request(msg, cc.write, cc.read)["result"]["decision"] == "approved_for_session"


def test_dedupe_labels_no_collision_with_preexisting_suffix():
    """_dedupe_labels must guarantee UNIQUE output labels even when a generated
    "(N)" suffix would collide with a natural input label or a prior suffix —
    otherwise dict(pairs) silently drops a decision, corrupting the reverse-map
    (the #18268 misroute). Regression: it tracked input-seen labels, not emitted
    ones, so [Foo, Foo, "Foo (2)"] produced two identical "Foo (2)" keys."""
    from codex_server import _dedupe_labels
    pairs = [("Foo", "A"), ("Foo", "B"), ("Foo (2)", "C")]
    out = _dedupe_labels(pairs)
    labels = [lbl for lbl, _ in out]
    assert len(labels) == len(set(labels)), f"labels must be unique, got {labels}"
    # No decision lost: the reverse-map keeps all three.
    assert len(dict(out)) == 3, f"dict(out) collapsed a decision: {out}"
    assert {dec for _, dec in out} == {"A", "B", "C"}


def test_build_labels_primary_network_nondict_no_crash():
    """PRIMARY path must not crash when an applyNetworkPolicyAmendment entry (or its
    inner network_policy_amendment) is a truthy non-dict — the `or {}` guard only
    handles falsy values, so a str/list used to raise AttributeError, which escaped
    the dispatcher and hung the turn until the 120s deadline. Mirror FALLBACK's
    isinstance discipline."""
    from codex_server import build_command_approval_labels
    # Outer value is a non-dict string.
    labels = build_command_approval_labels(
        {"availableDecisions": [{"applyNetworkPolicyAmendment": "str"}]})
    assert len(labels) == 1
    # Decision is preserved verbatim regardless of malformed shape.
    assert labels[0][1] == {"applyNetworkPolicyAmendment": "str"}
    # Inner network_policy_amendment is a non-dict.
    entry = {"applyNetworkPolicyAmendment": {"network_policy_amendment": "str"}}
    labels2 = build_command_approval_labels({"availableDecisions": [entry]})
    assert labels2[0][1] == entry


def test_build_labels_primary_network_dict_has_host():
    """PRIMARY path (availableDecisions) for a well-formed applyNetworkPolicyAmendment
    dict produces a host-bearing human label and preserves the verbatim decision —
    guards the lines-686-692 extraction against a wrong-sub-key regression."""
    from codex_server import build_command_approval_labels
    entry = {"applyNetworkPolicyAmendment": {"network_policy_amendment": {"host": "api.com", "action": "allow"}}}
    labels = build_command_approval_labels({"availableDecisions": [entry]})
    assert len(labels) == 1
    label, dec = labels[0]
    assert "api.com" in label, f"host must appear in the label, got {label!r}"
    assert dec == entry, "decision must round-trip verbatim"


# ── Step 2 tests (handle_server_request + SERVER_REQUEST_RESPONSE_SHAPE) ──

def test_permissions_request_gets_permissions_scope_not_decision():
    """item/permissions/requestApproval must return {permissions, scope}, not {decision}."""
    from codex_server import handle_server_request, LBL_GRANT_TURN
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_GRANT_TURN})
    msg = {
        "id": "req-perm",
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
            "environmentId": None, "startedAtMs": 1000, "cwd": "/tmp",
            "reason": None, "permissions": {},
        },
    }
    resp = handle_server_request(msg, cc.write, cc.read)
    result = resp.get("result", {})
    assert "permissions" in result, f"Missing 'permissions' in {result}"
    assert "scope" in result, f"Missing 'scope' in {result}"
    assert "decision" not in result, f"'decision' must not be in {result}"
    assert "jsonrpc" not in resp, "Response to app-server must be jsonrpc_lite (no 'jsonrpc' key)"


def test_every_server_request_gets_schema_valid_response():
    """INVARIANT: every method in SERVER_REQUEST_RESPONSE_SHAPE gets a non-None,
    schema-valid, jsonrpc_lite response — never dropped."""
    from codex_server import handle_server_request, SERVER_REQUEST_RESPONSE_SHAPE

    base = {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1", "startedAtMs": 1000}
    METHOD_PARAMS = {
        "item/commandExecution/requestApproval": {**base, "command": "echo hi", "cwd": "/tmp"},
        "item/fileChange/requestApproval": {**base},
        "item/tool/requestUserInput": {**base, "questions": [], "autoResolutionMs": None},
        "mcpServer/elicitation/request": {
            "threadId": "T1", "turnId": "TURN1", "serverName": "test",
            "mode": "form", "_meta": None, "message": "choose?",
            "requestedSchema": {"type": "object", "properties": {}},
        },
        "item/permissions/requestApproval": {
            **base, "environmentId": None, "cwd": "/tmp", "reason": None, "permissions": {},
        },
        "execCommandApproval": {
            "conversationId": "T1", "callId": "C1", "approvalId": None,
            "command": ["echo"], "cwd": "/tmp", "reason": None, "parsedCmd": [],
        },
        "applyPatchApproval": {
            "conversationId": "T1", "callId": "C1", "fileChanges": {},
            "reason": None, "grantRoot": None,
        },
        "item/tool/call": {**base},
        "account/chatgptAuthTokens/refresh": {},
        "attestation/generate": {},
    }

    for method, is_valid in SERVER_REQUEST_RESPONSE_SHAPE.items():
        cc = FakeCC()
        cc.set_answer("accept", {"label": "accept"})
        msg = {"id": f"req-{method}", "method": method,
               "params": METHOD_PARAMS.get(method, {})}
        resp = handle_server_request(msg, cc.write, cc.read)
        assert resp is not None, f"{method}: got None (must never drop)"
        assert "jsonrpc" not in resp, (
            f"{method}: response has 'jsonrpc' field — must be jsonrpc_lite"
        )
        assert is_valid(resp), f"{method}: invalid response shape: {resp}"


def test_unsupported_methods_return_jsonrpc_error():
    """item/tool/call, account/chatgptAuthTokens/refresh, attestation/generate → error."""
    from codex_server import handle_server_request
    for method in ("item/tool/call", "account/chatgptAuthTokens/refresh", "attestation/generate"):
        cc = FakeCC()
        resp = handle_server_request({"id": "x", "method": method, "params": {}},
                                      cc.write, cc.read)
        assert "error" in resp, f"{method}: expected error response, got {resp}"
        assert "result" not in resp


def test_unknown_method_returns_jsonrpc_error():
    """An unrecognised method must return a JSON-RPC error (never dropped)."""
    from codex_server import handle_server_request
    cc = FakeCC()
    resp = handle_server_request({"id": "x", "method": "totally/unknown", "params": {}},
                                  cc.write, cc.read)
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_command_approval_bridge_sends_correct_decision():
    """bridge_approval maps CC accept→human label→verbatim decision (accept string)."""
    from codex_server import handle_server_request, LBL_ALLOW_ONCE
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_ALLOW_ONCE})
    msg = {
        "id": "req-cmd",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
            "startedAtMs": 1000, "command": "echo hi", "cwd": "/tmp",
            "availableDecisions": ["accept", "decline"],
        },
    }
    resp = handle_server_request(msg, cc.write, cc.read)
    assert resp["result"]["decision"] == "accept"
    assert "jsonrpc" not in resp


def test_command_approval_bridge_dict_decision_round_trip():
    """bridge_approval sends the VERBATIM dict decision for an object availableDecisions entry.

    This exercises the #18268-critical path: an availableDecisions list that contains a
    dict entry (e.g. acceptWithExecpolicyAmendment), CC answering with the :-indexed label
    that build_command_approval_labels assigns to it, and the response carrying the exact
    verbatim dict — not the fallback string "decline".
    """
    from codex_server import build_command_approval_labels, handle_server_request, LBL_EXECPOLICY

    DICT_DECISION = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git", "status"]}}
    params = {
        "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
        "startedAtMs": 1000, "command": "git status", "cwd": "/tmp",
        "availableDecisions": [DICT_DECISION],
    }

    # Derive the exact label the code assigns to a dict execpolicy entry: the
    # human display string, mapped back to the verbatim decision.
    labels = build_command_approval_labels(params)
    assert len(labels) == 1
    expected_label, expected_decision = labels[0]
    assert expected_label == LBL_EXECPOLICY
    assert expected_decision == DICT_DECISION

    # CC answers accept with the exact :-indexed label; bridge must return the verbatim dict.
    cc = FakeCC()
    cc.set_answer("accept", {"label": expected_label})
    msg = {"id": "req-dict-decision", "method": "item/commandExecution/requestApproval", "params": params}
    resp = handle_server_request(msg, cc.write, cc.read)

    assert "jsonrpc" not in resp, "Response to app-server must be jsonrpc_lite (no 'jsonrpc' key)"
    assert resp["id"] == "req-dict-decision"
    decision = resp["result"]["decision"]
    assert isinstance(decision, dict), f"Expected dict decision, got {type(decision).__name__}: {decision!r}"
    assert decision == DICT_DECISION, f"Expected verbatim dict, got: {decision!r}"
    assert "acceptWithExecpolicyAmendment" in decision


def test_command_approval_decline_cc_action_returns_decline():
    """CC action=decline → bridge sends {decision:'decline'} to app-server."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("decline")
    msg = {
        "id": "req-dec",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
            "startedAtMs": 1000, "command": "rm -rf /", "cwd": "/tmp",
        },
    }
    resp = handle_server_request(msg, cc.write, cc.read)
    assert resp["result"]["decision"] == "decline"


def test_legacy_exec_approval_maps_to_review_decision():
    """execCommandApproval approve → ReviewDecision 'approved'."""
    from codex_server import handle_server_request, LBL_ALLOW_ONCE
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_ALLOW_ONCE})
    msg = {
        "id": "req-exec",
        "method": "execCommandApproval",
        "params": {
            "conversationId": "T1", "callId": "C1", "approvalId": None,
            "command": ["ls"], "cwd": "/tmp", "reason": None, "parsedCmd": [],
        },
    }
    resp = handle_server_request(msg, cc.write, cc.read)
    assert resp["result"]["decision"] == "approved"


# ── Step 3 tests (elicitation_timeout → decline) ─────────────────────────

def test_elicitation_timeout_defaults_to_decline():
    """If CC never answers, bridge sends {decision:'decline'} — no hang."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.never_answer_elicitation()
    msg = {
        "id": "req-timeout",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
            "startedAtMs": 1000, "command": "echo hi", "cwd": "/tmp",
        },
    }
    # Pass a tiny-timeout read so the test completes fast
    def fast_read(timeout=10.0):
        import time as _t
        _t.sleep(0.02)
        return None

    # Short timeout so the no-answer path completes fast (real default is 300s,
    # human-paced — see test_handle_server_request_threads_timeout).
    resp = handle_server_request(msg, cc.write, fast_read, timeout=0.1)
    assert resp["result"]["decision"] == "decline"


def _cmd_approval_msg():
    return {
        "id": "req-ux", "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                   "startedAtMs": 1000, "command": "echo hi", "cwd": "/tmp"},
    }


def test_command_approval_accept_without_label_defaults_to_accept():
    """Clicking CC's Accept WITHOUT selecting a dropdown label (action=accept,
    content empty) → 'accept', NOT 'decline'. Regression: the natural one-click
    Accept used to default to 'decline' (the live UI bug Chris hit)."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("accept", None)  # accept with NO content/label
    resp = handle_server_request(_cmd_approval_msg(), cc.write, cc.read)
    assert resp["result"]["decision"] == "accept"


def test_filechange_approval_accept_without_label_defaults_to_accept():
    """fileChange: bare Accept (no label) → 'accept', not 'decline'."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("accept", None)
    msg = {"id": "req-fc", "method": "item/fileChange/requestApproval",
           "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                      "startedAtMs": 1000, "reason": "edit"}}
    resp = handle_server_request(msg, cc.write, cc.read)
    assert resp["result"]["decision"] == "accept"


def test_command_approval_schema_label_is_optional():
    """The elicitation/create requestedSchema must NOT mark 'label' required —
    a required field makes CC block the Accept button ('This field is required'),
    which is exactly the UI friction Chris saw."""
    from codex_server import handle_server_request
    cc = FakeCC()
    resp = handle_server_request(_cmd_approval_msg(), cc.write, cc.read)  # noqa: F841
    elicit = next(r for r in cc._requests if r.get("method") == "elicitation/create")
    schema = elicit["params"]["requestedSchema"]
    assert "required" not in schema, f"label must be optional; schema={schema}"


def test_handle_server_request_threads_timeout():
    """handle_server_request passes its timeout through to bridge_approval, so the
    interactive-approval wait is human-paced (default 300s) and overridable."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.never_answer_elicitation()

    waited = []

    def timing_read(timeout=10.0):
        waited.append(timeout)
        import time as _t
        _t.sleep(min(timeout, 0.02))
        return None

    resp = handle_server_request(_cmd_approval_msg(), cc.write, timing_read, timeout=0.15)
    assert resp["result"]["decision"] == "decline"
    # the read got the threaded (short, test) timeout — not the 300s default
    assert waited and max(waited) < 1.0


def test_elicitation_reply_id_correlation_skips_unrelated_frames():
    """bridge_approval skips frames with id != eid and honors the matching reply.

    An unrelated frame (e.g. a ping or notification arriving before the real
    elicitation reply) must be skipped; the real reply is still honored.
    """
    from codex_server import handle_server_request

    cc_written: list = []

    def cc_write(msg: dict):
        assert msg.get("jsonrpc") == "2.0"
        cc_written.append(msg)

    # Capture eid from the elicitation/create frame, then deliver:
    #   1. an unrelated frame (id != eid, simulating a mid-turn ping)
    #   2. the real elicitation reply (id == eid, action=accept)
    call_count = [0]

    def cc_read(timeout=10.0):
        call_count[0] += 1
        # The first write is the elicitation/create; extract its eid
        eid = cc_written[-1]["id"] if cc_written else 999
        if call_count[0] == 1:
            # Unrelated frame — different id
            return {"jsonrpc": "2.0", "id": eid + 1000, "method": "ping", "result": {}}
        # Real reply matching eid
        return {"jsonrpc": "2.0", "id": eid, "result": {"action": "accept",
                                                          "content": {"label": "accept"}}}

    msg = {
        "id": "req-corr",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
            "startedAtMs": 1000, "command": "ls", "cwd": "/tmp",
        },
    }
    resp = handle_server_request(msg, cc_write, cc_read)
    # Unrelated frame skipped; real accept reply honored → decision is "accept" string
    assert resp["result"]["decision"] == "accept"
    assert call_count[0] == 2  # skipped 1 unrelated, consumed 1 real


def test_command_approval_cancel_cc_action_returns_cancel():
    """CC action=cancel → bridge sends {decision:'cancel'} (abort the turn), which is
    DISTINCT from 'decline' (skip this command). Regression: the else-branch used to
    map both decline AND cancel to 'decline'."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("cancel")
    msg = {
        "id": "req-cancel", "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                   "startedAtMs": 1000, "command": "rm -rf /", "cwd": "/tmp"},
    }
    resp = handle_server_request(msg, cc.write, cc.read)
    assert resp["result"]["decision"] == "cancel"


def test_filechange_approval_cancel_cc_action_returns_cancel():
    """fileChange CC action=cancel → {decision:'cancel'} (FileChangeApprovalDecision
    has a distinct 'cancel'); regression for the decline/cancel conflation."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("cancel")
    msg = {
        "id": "req-fc-cancel", "method": "item/fileChange/requestApproval",
        "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                   "startedAtMs": 1000, "reason": "edit file"},
    }
    resp = handle_server_request(msg, cc.write, cc.read)
    assert resp["result"]["decision"] == "cancel"


def test_read_correlated_retries_on_transient_none_frame():
    """A transient None from cc_read (blank line / JSON decode error / select timeout)
    BEFORE the real reply must NOT cause a premature decline — read_correlated retries
    within the deadline and honors the real accept. Regression: it used to `return None`
    on the first None, declining a command the user actually approved (#18268-class)."""
    from codex_server import handle_server_request
    written: list = []

    def cc_write(msg):
        assert msg.get("jsonrpc") == "2.0"
        written.append(msg)

    calls = [0]

    def cc_read(timeout=10.0):
        calls[0] += 1
        eid = written[-1]["id"] if written else 999
        if calls[0] == 1:
            return None  # transient (blank/malformed) — must be retried, not declined
        return {"jsonrpc": "2.0", "id": eid,
                "result": {"action": "accept", "content": {"label": "accept"}}}

    msg = {
        "id": "req-tn", "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                   "startedAtMs": 1000, "command": "ls", "cwd": "/tmp"},
    }
    resp = handle_server_request(msg, cc_write, cc_read)
    assert resp["result"]["decision"] == "accept"  # NOT a premature decline
    assert calls[0] == 2  # retried after the transient None


def test_read_correlated_skips_id_colliding_request_frame():
    """A REQUEST frame whose id numerically equals the pending elicitation id must NOT
    be mistaken for the reply (shape-first: only a RESPONSE resolves the elicitation).
    Regression: read_correlated matched on id alone, so a same-id tools/list would be
    returned and misread → action None → spurious decline."""
    from codex_server import handle_server_request
    written: list = []

    def cc_write(msg):
        assert msg.get("jsonrpc") == "2.0"
        written.append(msg)

    calls = [0]

    def cc_read(timeout=10.0):
        calls[0] += 1
        eid = written[-1]["id"] if written else 999
        if calls[0] == 1:
            # A REQUEST (has 'method') with the SAME id as our elicitation/create.
            return {"jsonrpc": "2.0", "id": eid, "method": "tools/list"}
        return {"jsonrpc": "2.0", "id": eid,
                "result": {"action": "accept", "content": {"label": "accept"}}}

    msg = {
        "id": "req-collide", "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                   "startedAtMs": 1000, "command": "ls", "cwd": "/tmp"},
    }
    resp = handle_server_request(msg, cc_write, cc_read)
    assert resp["result"]["decision"] == "accept"  # id-colliding request skipped
    assert calls[0] == 2


# ── Step 4 tests (TurnStateMachine) ──────────────────────────────────────

def test_turn_state_machine_lifecycle():
    """TurnStateMachine: not busy → started → busy → completed → not busy."""
    from codex_server import TurnStateMachine
    sm = TurnStateMachine()
    assert not sm.is_busy()
    sm.turn_started(cc_id=42)
    assert sm.is_busy()
    sm.turn_completed()
    assert not sm.is_busy()


def test_turn_state_machine_busy_error_returns_error_dict():
    """busy_error() returns {"error": str} — same shape as every other codex_run_v2 error path."""
    from codex_server import TurnStateMachine
    sm = TurnStateMachine()
    sm.turn_started(cc_id=1)
    err = sm.busy_error()
    assert "error" in err
    assert "jsonrpc" not in err  # NOT a JSON-RPC frame — plain error dict
    assert "in flight" in err["error"].lower() or "busy" in err["error"].lower()


def test_turn_state_machine_eof_clears_in_flight():
    """eof_error() clears in-flight state and returns {"error": str} — same shape
    as every other codex_run_v2 error path (not a JSON-RPC frame)."""
    from codex_server import TurnStateMachine
    sm = TurnStateMachine()
    sm.turn_started(cc_id=5)
    err = sm.eof_error()
    assert "error" in err
    assert "jsonrpc" not in err  # NOT a JSON-RPC frame — plain error dict
    assert not sm.is_busy()      # state cleared so child can respawn


def test_classify_shape_first_colliding_id():
    """classify() shape-first: {id, method} is a REQUEST even if id matches a pending elicitation."""
    from codex_server import classify
    # Both msg share id=99 — classify by shape, not by id
    assert classify({"id": 99, "method": "tools/list"}) == "request"
    assert classify({"id": 99, "result": {}}) == "response"


# ---------------------------------------------------------------------------
# Task 5: codex_run tool — modes, posture, resume, structured output
# ---------------------------------------------------------------------------
#
# Design: call_codex_run wires fake_child into a fresh AppServerManager and
# calls codex_run_v2(args, manager, cc_write_fn, cc_read_fn).
# FakeChild is extended (script_final_message, turn/start dispatch) here in
# the test module via a subclass so the core FakeChild stays minimal.


class ExtendedFakeChild(FakeChild):
    """FakeChild + turn/start handling + scripted final message + unknown-thread guard."""

    def __init__(self):
        super().__init__()
        self._final_message = "fake result"
        self._known_threads: set = set()
        # T1 is always "known" (returned by thread/start handler in parent)
        self._known_threads.add("T1")
        # For posture-omit test: track what turn/start params were received
        self.turn_start_params: dict | None = None
        self._turn_variant: str | None = None

    def script_final_message(self, text: str):
        """Configure the text that the fake emits via item/agentMessage/delta."""
        self._final_message = text

    def script_turn_variant(self, name: str):
        """Configure the turn variant (e.g. 'unknown_server_request')."""
        self._turn_variant = name

    def _dispatch(self, msg: dict):
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "thread/resume":
            tid = params.get("threadId")
            if tid not in self._known_threads:
                # Unknown thread → error (fail-loud)
                self._write_msg({"id": mid, "error": {
                    "code": -32001,
                    "message": f"thread not found: {tid!r}",
                }})
                return
            self._write_msg({"id": mid, "result": {
                "thread": {"id": tid, "status": "idle"},
            }})
            return

        if method == "thread/start":
            # Extract threadId from response (always T1 in parent)
            # Call parent's handler to record + respond
            super()._dispatch(msg)
            return

        if method == "turn/start":
            # Record params for assertion
            self.turn_start_params = params
            turn_id = "TURN1"
            item_id = "ITEM1"
            thread_id = params.get("threadId", "T1")
            # 1. TurnStartResponse
            self._write_msg({"id": mid, "result": {
                "turn": {
                    "id": turn_id,
                    "items": [],
                    "itemsView": "loaded",
                    "status": "running",
                    "error": None,
                    "startedAt": 0,
                    "completedAt": None,
                    "durationMs": None,
                }
            }})
            if self._turn_variant == "failed":
                # Variant: turn/completed with status="failed" (no delta)
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "items": [],
                        "itemsView": "loaded",
                        "status": "failed",
                        "error": None,
                        "startedAt": 0,
                        "completedAt": 0,
                        "durationMs": 10,
                    },
                }})
                return
            if self._turn_variant == "unknown_server_request":
                # Emit a server→client REQUEST with an UNBRIDGED method (fire-and-forget:
                # handle_server_request returns -32601 + UNKNOWN_SERVER_METHOD breadcrumb;
                # the fake ignores the error reply since it has no method field).
                self._write_msg({"id": "SRVREQ-1", "method": "item/somethingNew/requestApproval",
                                 "params": {"threadId": thread_id, "turnId": turn_id}})
            if self._turn_variant == "unknown_notification":
                # Variant: emit delta + bogus notification + turn/completed (status completed)
                self._write_msg({"method": "item/agentMessage/delta", "params": {
                    "delta": self._final_message,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": item_id,
                }})
                self._write_msg({"method": "item/bogusEvent", "params": {}})
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "items": [],
                        "itemsView": "loaded",
                        "status": "completed",
                        "error": None,
                        "startedAt": 0,
                        "completedAt": 0,
                        "durationMs": 10,
                    },
                }})
                return
            if self._turn_variant == "benign_lifecycle":
                # Variant: emit the REAL codex benign lifecycle notifications (observed
                # live via mcp__plugin_bulldozer_codex__codex_run _drift) around a normal
                # happy turn. After the allowlist fix these must NOT produce _drift.
                for _m in ("thread/settings/updated", "thread/status/changed",
                           "mcpServer/startupStatus/updated", "hook/started",
                           "hook/completed", "thread/tokenUsage/updated",
                           "account/rateLimits/updated", "skills/changed",
                           "remoteControl/status/changed"):
                    self._write_msg({"method": _m, "params": {}})
                self._write_msg({"method": "item/agentMessage/delta", "params": {
                    "delta": self._final_message, "threadId": thread_id,
                    "turnId": turn_id, "itemId": item_id,
                }})
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [], "itemsView": "loaded",
                             "status": "completed", "error": None,
                             "startedAt": 0, "completedAt": 0, "durationMs": 10},
                }})
                return
            # 2. Delta notification with scripted final text
            self._write_msg({"method": "item/agentMessage/delta", "params": {
                "delta": self._final_message,
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": item_id,
            }})
            # 2b. item/completed — mirrors real codex (emitted between delta and turn/completed)
            self._write_msg({"method": "item/completed", "params": {
                "item": {"id": item_id, "type": "message", "role": "assistant", "content": []},
                "threadId": thread_id,
                "turnId": turn_id,
            }})
            # 3. turn/completed (NO final text — only end signal)
            self._write_msg({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "items": [],
                    "itemsView": "loaded",
                    "status": "completed",
                    "error": None,
                    "startedAt": 0,
                    "completedAt": 0,
                    "durationMs": 10,
                },
            }})
            return

        # Delegate everything else to parent
        super()._dispatch(msg)


@pytest.fixture
def ext_child():
    """Extended fake child with turn/start support."""
    fc = ExtendedFakeChild()
    yield fc
    fc.kill()


def call_codex_run(fake_child_inst, prompt, mode="review", sandbox=None,
                   approval_policy=None, effort=None, cwd=None, model=None,
                   thread_id=None, _force_bad_final=None, turn_variant=None,
                   base_instructions=None, developer_instructions=None, config=None):
    """Call codex_run_v2 with a fake child wired as the manager's backend.

    Sentinel: None = omit (keep thread posture on resume); use the string
    value to set explicitly.
    """
    from codex_server import codex_run_v2, AppServerManager

    if _force_bad_final is not None:
        fake_child_inst.script_final_message(_force_bad_final)

    if turn_variant is not None:
        fake_child_inst.script_turn_variant(turn_variant)

    manager = AppServerManager(bin=fake_child_inst)

    # Fake CC write/read (no approvals in these tests)
    def cc_write(msg):
        pass

    def cc_read(timeout=10.0):
        return None

    args = {"prompt": prompt, "mode": mode}
    if sandbox is not None:
        args["sandbox"] = sandbox
    if approval_policy is not None:
        args["approval_policy"] = approval_policy
    if effort is not None:
        args["effort"] = effort
    if cwd is not None:
        args["cwd"] = cwd
    if model is not None:
        args["model"] = model
    if thread_id is not None:
        args["thread_id"] = thread_id
    if base_instructions is not None:
        args["base_instructions"] = base_instructions
    if developer_instructions is not None:
        args["developer_instructions"] = developer_instructions
    if config is not None:
        args["config"] = config

    return codex_run_v2(args, manager=manager, cc_write_fn=cc_write, cc_read_fn=cc_read)


# ── Step 1: review mode sets outputSchema ─────────────────────────────────

def test_review_mode_sets_output_schema(ext_child):
    call_codex_run(ext_child, prompt="review x", mode="review")
    p = ext_child.turn_start_params
    assert p is not None, "turn/start was not sent"
    # outputSchema must CONSTRAIN to the review contract
    schema = p.get("outputSchema")
    assert schema is not None, "outputSchema must be present for mode=review"
    assert set(schema.get("required", [])) >= {"verdict", "findings"}
    assert schema["properties"]["verdict"]["enum"] == ["GO", "NO-GO", "MINOR-FIXES"]
    assert "findings" in schema["properties"]
    # turn/start REQUIRES input: Array<UserInput>; a text UserInput needs text_elements
    assert p["input"] == [{"type": "text", "text": "review x", "text_elements": []}]


def test_review_invalid_final_json_sets_schema_ok_false(ext_child):
    r = call_codex_run(ext_child, prompt="review x", mode="review",
                       _force_bad_final="not json")
    assert r["schema_ok"] is False
    assert "verdict" in r   # degraded gracefully, carried from v1


# ── Step 2: thread_id resumes; posture precedence ─────────────────────────

def test_thread_id_resumes_and_explicit_posture_wins(ext_child):
    test_cwd = "/tmp/test-resume-cwd"
    call_codex_run(ext_child, prompt="...", thread_id="T1",
                   sandbox="workspace-write", cwd=test_cwd)
    resume = ext_child.received("thread/resume")
    assert resume is not None, "thread/resume was not sent"
    assert resume["params"]["threadId"] == "T1"
    # explicit per-call sandbox passed as turn/start override (sandboxPolicy)
    p = ext_child.turn_start_params
    assert p is not None
    assert "sandboxPolicy" in p, "explicit sandbox must produce sandboxPolicy override"
    assert p["sandboxPolicy"]["type"] == "workspaceWrite"
    # writableRoots must contain the explicit cwd — NOT [""] (empty string is invalid)
    assert p["sandboxPolicy"]["writableRoots"] == [test_cwd], (
        f"writableRoots must be [cwd], got {p['sandboxPolicy']['writableRoots']!r}"
    )


def test_resume_workspace_write_without_cwd_fails_loud(ext_child):
    """workspace-write + no cwd on RESUME must return error (not silently emit writableRoots: [''])."""
    r = call_codex_run(ext_child, prompt="...", thread_id="T1", sandbox="workspace-write")
    # cwd is omitted — must fail loud, never produce writableRoots: [""]
    assert "error" in r, (
        f"workspace-write without cwd on resume must return error, got: {r!r}"
    )
    assert "cwd" in r["error"].lower(), (
        f"error message should mention 'cwd', got: {r['error']!r}"
    )


def test_unknown_thread_id_fails_loud(ext_child):
    r = call_codex_run(ext_child, prompt="...", thread_id="NOPE")
    assert "error" in r, "unknown thread_id must return error, never a silent new thread"


def test_resume_without_posture_keeps_thread_posture(ext_child):
    # Caller omits sandbox/approval_policy/effort/cwd/model on RESUME
    # → NO turn/start posture override is sent
    call_codex_run(ext_child, prompt="...", thread_id="T1")   # no posture params
    p = ext_child.turn_start_params
    assert p is not None
    for k in ("sandboxPolicy", "approvalPolicy", "effort", "cwd", "model"):
        assert k not in p, f"omitted posture key {k!r} must NOT be sent on resume"


def test_sandbox_string_converts_to_policy_object():
    from codex_server import _sandbox_policy
    assert _sandbox_policy("read-only", cwd="/x") == {"type": "readOnly", "networkAccess": False}
    assert _sandbox_policy("danger-full-access", cwd="/x") == {"type": "dangerFullAccess"}
    p = _sandbox_policy("workspace-write", cwd="/x")
    assert p["type"] == "workspaceWrite" and p["writableRoots"] == ["/x"] \
        and p["networkAccess"] is False and p["excludeTmpdirEnvVar"] is False \
        and p["excludeSlashTmp"] is False


def test_cwd_omitted_uses_isolated_tmpdir_on_thread_start(ext_child):
    call_codex_run(ext_child, prompt="...")      # no cwd, NEW thread
    ts = ext_child.received("thread/start")
    assert ts is not None, "thread/start must be sent"
    p = ts["params"]
    # cwd isolation is pinned at THREAD/START
    assert p.get("cwd"), "cwd must be set on thread/start"
    assert p["cwd"] != os.getcwd(), "cwd must be an isolated tmpdir, never caller cwd"


def test_codex_run_returns_final_message_from_agent_events(ext_child):
    ext_child.script_final_message("DONE-42")
    r = call_codex_run(ext_child, prompt="...", mode="implement")
    assert r.get("result") == "DONE-42", (
        f"final text must be extracted from item/agentMessage/delta events, got: {r!r}")


# ── Step 4: implement mode + no-codex graceful ────────────────────────────

def test_implement_mode_returns_free_text(ext_child):
    ext_child.script_final_message("implemented!")
    r = call_codex_run(ext_child, prompt="do x", mode="implement")
    assert "result" in r
    assert "outputSchema" not in (ext_child.turn_start_params or {})


def test_codex_run_no_codex_returns_error(monkeypatch):
    """If codex binary is absent, codex_run_v2 returns a clean error result (no manager → binary check)."""
    import codex_server
    monkeypatch.setattr(codex_server, "CODEX", "/nonexistent/codex")
    # Call codex_run_v2 directly without an explicit manager so the binary check runs
    r = codex_server.codex_run_v2({"prompt": "test"})
    assert "error" in r


# ---------------------------------------------------------------------------
# Task 6 (Fix 2): V2 dispatcher integration test — subprocess-level contract.
#
# Drives the REAL main() dispatcher over stdio (Popen python3 mcp/codex_server.py)
# without needing a live codex binary.  Regression guard for:
#   - initialize: JSON-RPC 2.0 reply with protocolVersion + serverInfo.version
#   - tools/list: codex_run tool present; inputSchema has thread_id + approval_policy
#   - tools/call (no codex): graceful error result (not a crash, not a hang)
# ---------------------------------------------------------------------------

class TestV2Dispatcher:
    """Subprocess-level integration tests for the MCP dispatcher (main())."""

    CODEX_SERVER = os.path.join(MCP_DIR, "codex_server.py")
    READ_TIMEOUT = 5.0  # seconds — hang = test failure

    def _start_server(self, env_override=None):
        """Start codex_server.py as a subprocess, return Popen."""
        env = os.environ.copy()
        if env_override:
            env.update(env_override)
        return subprocess.Popen(
            [sys.executable, self.CODEX_SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def _send(self, proc, msg: dict):
        """Send one JSON-RPC frame to the server's stdin."""
        data = (_json.dumps(msg) + "\n").encode()
        proc.stdin.write(data)
        proc.stdin.flush()

    def _recv(self, proc, timeout=None) -> dict:
        """Read one JSON-RPC frame from the server's stdout.

        Raises TimeoutError if nothing arrives within `timeout` seconds.
        """
        timeout = timeout or self.READ_TIMEOUT
        import select as _sel
        ready, _, _ = _sel.select([proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"No response from dispatcher within {timeout}s")
        line = proc.stdout.readline()
        if not line:
            raise EOFError("Dispatcher closed stdout unexpectedly")
        return _json.loads(line.strip())

    def _shutdown(self, proc):
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    def test_initialize_returns_jsonrpc2_with_server_info(self):
        """initialize → well-formed JSON-RPC 2.0 reply (protocolVersion + serverInfo)."""
        proc = self._start_server()
        try:
            self._send(proc, {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            })
            resp = self._recv(proc)
            assert resp.get("jsonrpc") == "2.0", f"Missing jsonrpc:2.0 in {resp}"
            assert resp.get("id") == 1, f"Wrong id in {resp}"
            result = resp.get("result", {})
            assert "protocolVersion" in result, f"Missing protocolVersion in {result}"
            server_info = result.get("serverInfo", {})
            assert server_info.get("name") == "bulldozer-codex", (
                f"Expected serverInfo.name='bulldozer-codex', got: {server_info!r}"
            )
            # serverInfo.version tracks the live plugin CalVer (not a hardcoded
            # literal that drifts after every auto-bump) — same source as the
            # app-server clientInfo leg.
            from codex_server import _plugin_version
            assert server_info.get("version") == _plugin_version(), (
                f"Expected serverInfo.version={_plugin_version()!r}, got: {server_info.get('version')!r}"
            )
        finally:
            self._shutdown(proc)

    def test_tools_list_includes_v2_params(self):
        """tools/list → codex_run present; inputSchema includes thread_id and approval_policy."""
        proc = self._start_server()
        try:
            # Send initialize first (good MCP practice; dispatcher handles it fine either way)
            self._send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self._recv(proc)  # consume initialize response

            self._send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            resp = self._recv(proc)
            assert resp.get("jsonrpc") == "2.0"
            assert resp.get("id") == 2
            tools = resp.get("result", {}).get("tools", [])
            names = [t["name"] for t in tools]
            assert "codex_run" in names, f"codex_run not in tools: {names}"
            codex_tool = next(t for t in tools if t["name"] == "codex_run")
            schema_props = codex_tool.get("inputSchema", {}).get("properties", {})
            assert "thread_id" in schema_props, (
                f"inputSchema missing 'thread_id' — v2 regression: {list(schema_props)}"
            )
            assert "approval_policy" in schema_props, (
                f"inputSchema missing 'approval_policy' — v2 regression: {list(schema_props)}"
            )
        finally:
            self._shutdown(proc)

    def test_tools_call_no_codex_returns_graceful_error(self):
        """tools/call with JAINE_CODEX_BIN=/nonexistent → graceful error result, no crash."""
        # Point JAINE_CODEX_BIN at a non-existent path to trigger the no-codex path
        proc = self._start_server(env_override={"JAINE_CODEX_BIN": "/nonexistent/codex"})
        try:
            self._send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self._recv(proc)  # consume initialize

            self._send(proc, {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "codex_run",
                    "arguments": {"prompt": "hello", "mode": "review"},
                },
            })
            resp = self._recv(proc, timeout=self.READ_TIMEOUT)
            # Must be a proper JSON-RPC 2.0 response (not a crash, not a hang)
            assert resp.get("jsonrpc") == "2.0", f"Not a JSON-RPC 2.0 frame: {resp}"
            assert resp.get("id") == 3, f"Wrong id: {resp}"
            # Graceful no-codex: result with content (error text), NOT a JSON-RPC error
            result = resp.get("result")
            assert result is not None, (
                f"Expected 'result' (not 'error') for graceful no-codex path; got: {resp}"
            )
            content = result.get("content", [])
            assert content, f"Expected non-empty content list in result: {result}"
            # The error message is JSON-encoded in content[0]["text"]
            text = content[0].get("text", "")
            payload = _json.loads(text)
            assert "error" in payload, (
                f"Expected {{error: ...}} payload in content text, got: {payload!r}"
            )
            # Tighten (regression guard): it must be the SPECIFIC graceful no-codex
            # message, proving main() actually reached codex_run_v2 and ran its
            # binary check. A loose "error in payload" check previously PASSED while
            # the dispatcher raised NameError("codex_run_v2 is not defined") — that
            # happened when `if __name__ == "__main__": main()` sat ABOVE the v2
            # definitions, so main() ran before codex_run_v2 existed. Assert it's the
            # no-codex error, NOT a NameError.
            assert "codex binary not found" in payload["error"], (
                f"Expected the graceful no-codex message; got: {payload['error']!r}"
            )
            assert "not defined" not in payload["error"], (
                f"NameError leaked — entrypoint guard ran before v2 defs: {payload['error']!r}"
            )
        finally:
            self._shutdown(proc)


# ---------------------------------------------------------------------------
# Task 1 (GATING): thread/resume round-trips across a process restart.
# ---------------------------------------------------------------------------
@skip_if_no_codex
@pytest.mark.slow
def test_resume_by_thread_id_recalls_across_restart():
    """A thread started by one app-server child is resumed by a DIFFERENT child
    (simulating a new session) and the model recalls a codeword planted in the
    first turn. This is the gate: cross-session resume is buildable."""
    import appserver_resume_probe as probe

    tid, recalled = probe.round_trip(method="by-id")
    assert tid, "thread/start did not yield a thread_id"
    assert recalled, "by-id resume did NOT recall the planted codeword across restart"


# ---------------------------------------------------------------------------
# Task 6 (LIVE E2E): real app-server review turn against codex 0.141.
# ---------------------------------------------------------------------------

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_review_real_appserver():
    """End-to-end review turn against the REAL codex app-server singleton.

    Exercises the entire v2 stack:
      ensure() → initialize/initialized handshake → start_thread (isolated)
      → turn/start (outputSchema) → accumulate item/agentMessage/delta
      → turn/completed → _shape_result → {thread_id, verdict, findings, schema_ok}

    Allow 30-120 s — real codex reasoning takes time. The module singleton is
    used (no manager arg) to prove the production code path, not a test double.
    """
    from codex_server import codex_run_v2

    r = codex_run_v2({
        "prompt": "Review this Python function for bugs: def avg(n): return sum(n)/len(n)",
        "mode": "review",
    })
    assert "error" not in r, f"codex_run_v2 returned error: {r.get('error')}"
    assert r.get("schema_ok") is True, f"schema_ok must be True, got: {r}"
    assert r.get("verdict") in ("GO", "NO-GO", "MINOR-FIXES"), (
        f"verdict must be one of GO/NO-GO/MINOR-FIXES, got: {r.get('verdict')!r}"
    )
    assert r.get("thread_id"), f"thread_id must be non-empty, got: {r}"
    # findings is a list (may be empty for GO)
    assert isinstance(r.get("findings"), list), f"findings must be a list, got: {r}"


@skip_if_no_codex
@pytest.mark.slow
def test_e2e_implement_real_appserver():
    """End-to-end implement turn against the REAL codex app-server singleton.

    Verifies implement mode returns non-empty result text and a thread_id.
    Uses the module singleton (production code path).
    """
    from codex_server import codex_run_v2

    r = codex_run_v2({
        "prompt": "Write a one-liner Python function that returns the square of a number.",
        "mode": "implement",
    })
    assert "error" not in r, f"codex_run_v2 returned error: {r.get('error')}"
    assert r.get("thread_id"), f"thread_id must be non-empty, got: {r}"
    assert r.get("result"), f"result must be non-empty for implement mode, got: {r}"


# ---------------------------------------------------------------------------
# Task A1: _drift_warn / _stamp_drift / _now_iso (logging primitive)
# ---------------------------------------------------------------------------

def test_drift_warn_appends_to_acc_and_logs(tmp_path, monkeypatch):
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    acc = []
    cs._drift_warn(acc, "UNKNOWN_SERVER_METHOD", "foo/bar")
    assert acc == [{"code": "UNKNOWN_SERVER_METHOD", "detail": "foo/bar"}]
    assert "UNKNOWN_SERVER_METHOD" in logf.read_text()


def test_drift_warn_acc_none_is_log_only(tmp_path, monkeypatch):
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    cs._drift_warn(None, "VERSION_MISMATCH", "x")   # must not raise
    assert "VERSION_MISMATCH" in logf.read_text()


def test_drift_warn_never_raises_on_unwritable(monkeypatch):
    import codex_server as cs
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", "/nonexistent-root/x/y.log")
    cs._drift_warn([], "UNKNOWN_NOTIFICATION", "z")   # swallowed


def test_stamp_drift_attaches_only_when_nonempty():
    import codex_server as cs
    assert cs._stamp_drift({"ok": 1}, []) == {"ok": 1}
    assert cs._stamp_drift({"ok": 1}, [{"code": "X", "detail": "y"}])["_drift"] == [{"code": "X", "detail": "y"}]


# ---------------------------------------------------------------------------
# A2: handle_server_request unknown-method breadcrumb
# ---------------------------------------------------------------------------

def test_unknown_server_method_records_breadcrumb_and_returns_32601():
    import codex_server as cs
    acc = []
    msg = {"id": 7, "method": "item/somethingNew/requestApproval", "params": {}}
    out = cs.handle_server_request(msg, lambda f: None, lambda timeout=10: None, acc=acc)
    assert out["error"]["code"] == -32601
    assert acc and acc[0]["code"] == "UNKNOWN_SERVER_METHOD"
    assert acc[0]["detail"] == "item/somethingNew/requestApproval"


def test_unsupported_method_unchanged_no_breadcrumb():
    import codex_server as cs
    acc = []
    msg = {"id": 1, "method": "item/tool/call", "params": {}}
    out = cs.handle_server_request(msg, lambda f: None, lambda timeout=10: None, acc=acc)
    assert out["error"]["code"] == -32601
    assert acc == []   # _UNSUPPORTED_METHODS is a known, correct -32601 (no drift)


# ---------------------------------------------------------------------------
# A3: approval breadcrumbs + empty-dict guard + acc threading
# ---------------------------------------------------------------------------

def test_unknown_decision_kind_breadcrumb_and_verbatim_preserved():
    import codex_server as cs
    acc = []
    future = {"acceptWithSomethingNew": {"x": 1}}
    pairs = cs.build_command_approval_labels({"availableDecisions": ["accept", future]}, acc=acc)
    lm = dict(pairs)
    lbl = [l for l, _ in pairs if l.endswith(":1")][0]
    assert lm[lbl] == future                      # verbatim preserved (#18268)
    assert any(r["code"] == "UNKNOWN_DECISION_VARIANT" for r in acc)


def test_empty_dict_availabledecision_entry_no_stopiteration():
    import codex_server as cs
    acc = []
    # {} passes isinstance(dict) but next(iter({})) would raise StopIteration
    pairs = cs.build_command_approval_labels({"availableDecisions": ["accept", {}]}, acc=acc)
    labels = [l for l, _ in pairs]
    assert "Allow once" in labels                 # the {} entry is skipped, not fatal
    assert any(r["code"] == "UNKNOWN_DECISION_VARIANT" for r in acc)


def test_out_of_enum_label_breadcrumb_via_handle_server_request():
    # R1-F4: exercises the acc-threading chain handle_server_request(acc=) -> bridge_approval(acc=).
    # CC answers with a label NOT in the map -> OUT_OF_ENUM_LABEL breadcrumb + safe "accept" default.
    import codex_server as cs
    acc = []
    cc = FakeCC()
    cc.set_answer("accept", {"label": "TOTALLY-BOGUS-LABEL"})
    msg = {"id": "req-x", "method": "item/commandExecution/requestApproval",
           "params": {"availableDecisions": ["accept", "decline"]}}
    resp = cs.handle_server_request(msg, cc.write, cc.read, acc=acc)
    assert resp["result"]["decision"] == "accept"               # #18268 safe default preserved
    assert any(r["code"] == "OUT_OF_ENUM_LABEL" for r in acc)   # breadcrumb reached the accumulator


# ---------------------------------------------------------------------------
# Task A4: passive version capture (log-only)
# ---------------------------------------------------------------------------

def test_parse_codex_version_anchored():
    # A4: _parse_codex_version extracts version from FIRST token ("<clientName>/<codexVersion>")
    # so later tokens like "iTerm.app/3.7..." cannot win.
    import codex_server as cs
    # Real format: first token wins (0.141, not 3.7 from iTerm)
    assert cs._parse_codex_version(
        "bulldozer-codex-mcp/0.141.0 (Mac OS; arm64) iTerm.app/3.7.0beta2 (x; 0.0.0)"
    ) == "0.141"
    # Minimal real format
    assert cs._parse_codex_version("probe/0.141.0 (Mac OS; arm64)") == "0.141"
    # No slash-version in first token → None
    assert cs._parse_codex_version("no-slash-version-here") is None
    # Empty string → None
    assert cs._parse_codex_version("") is None


def test_version_mismatch_is_log_only(tmp_path, monkeypatch):
    # A4: VERSION_MISMATCH must be acc=None (log-only), NEVER user-facing.
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    # parsed != LAST_VERIFIED → a log line, NOT an exception, NOT a user-facing record
    acc = []
    cs._drift_warn(
        None if cs._parse_codex_version("codex/0.999.0") != cs.LAST_VERIFIED_CODEX_VERSION else acc,
        "VERSION_MISMATCH", "live 0.999",
    )
    assert acc == []                        # never user-facing
    assert "VERSION_MISMATCH" in logf.read_text()


def test_do_initialize_captures_codex_version(fake_child):
    # A4/R2-F3: _do_initialize must parse the initialize userAgent into manager._codex_version.
    import codex_server as cs
    m = cs.AppServerManager(bin=fake_child)
    m.ensure()                              # FakeChild initialize emits userAgent "codex/fake-0.141.0"
    assert m._codex_version == "0.141"     # == LAST_VERIFIED_CODEX_VERSION → no VERSION_MISMATCH


# ---------------------------------------------------------------------------
# Task A5: per-call drift accumulator wiring in codex_run_v2
# ---------------------------------------------------------------------------

def test_codex_run_v2_no_drift_on_happy_path(ext_child):
    res = call_codex_run(ext_child, prompt="hi")     # basic turn: delta + turn/completed
    assert "_drift" not in res                        # happy path = byte-identical, no _drift key


def test_codex_run_v2_stamps_drift_from_unknown_server_request(ext_child):
    # ext_child emits a mid-turn server->client REQUEST with an UNBRIDGED method
    # (fire-and-forget: handle_server_request returns -32601 and records the breadcrumb,
    # which A2's acc threading surfaces and A5 stamps onto the result).
    res = call_codex_run(ext_child, prompt="hi", turn_variant="unknown_server_request")
    assert any(r["code"] == "UNKNOWN_SERVER_METHOD" for r in res.get("_drift", []))


# ---------------------------------------------------------------------------
# Task A6: _KNOWN_NOTIFICATIONS allowlist + terminal-failure detection
# ---------------------------------------------------------------------------

def test_item_completed_not_flagged_as_drift(ext_child):
    res = call_codex_run(ext_child, prompt="hi")     # basic turn emits item/completed
    assert "_drift" not in res                        # item/completed is KNOWN, not UNKNOWN_NOTIFICATION


def test_unknown_notification_flagged(ext_child):
    res = call_codex_run(ext_child, prompt="hi", turn_variant="unknown_notification")  # fake emits item/bogusEvent
    assert any(r["code"] == "UNKNOWN_NOTIFICATION" for r in res.get("_drift", []))


def test_real_benign_lifecycle_notifications_not_flagged(ext_child):
    # The REAL codex app-server emits these benign lifecycle notifications every healthy
    # turn (observed live via the MCP tool's _drift). They MUST be in _KNOWN_NOTIFICATIONS
    # so a healthy turn returns NO _drift (happy-path-byte-identical invariant). Grounding
    # against real codex, not just the original fake.
    res = call_codex_run(ext_child, prompt="hi", turn_variant="benign_lifecycle")
    assert "_drift" not in res, f"benign lifecycle notifications wrongly flagged: {res.get('_drift')}"
    assert res.get("verdict") in ("GO", "NO-GO", "MINOR-FIXES", "UNKNOWN")  # turn still completed normally


def test_failed_turn_returns_clean_error(ext_child):
    res = call_codex_run(ext_child, prompt="hi", turn_variant="failed")   # turn/completed status="failed"
    assert "error" in res and "turn failed" in res["error"]


# ---------------------------------------------------------------------------
# B2: config passthrough with isolation scrub
# ---------------------------------------------------------------------------

def test_config_merge_scrubs_isolation_keys():
    """Deny-keys are scrubbed; benign keys pass; ISOLATION_CONFIG always wins."""
    sent = _started_params(config={"mcp_servers": {"evil": 1}, "mcpServers": {"evil": 1},
                                   "baseInstructions": "x", "developerInstructions": "y",
                                   "model_reasoning_effort": "high"})["config"]
    assert sent["mcp_servers"] == {}                  # ISOLATION wins
    assert "mcpServers" not in sent                    # alias scrubbed
    assert "baseInstructions" not in sent and "developerInstructions" not in sent
    assert sent["model_reasoning_effort"] == "high"    # benign key passes


def test_codex_run_v2_forwards_parity_args_to_thread_start(ext_child):
    """R1-F3/R2-F2: all parity args reach thread/start THROUGH codex_run_v2."""
    res = call_codex_run(ext_child, prompt="hi",
                         base_instructions="custom-base",
                         developer_instructions="be terse",
                         config={"mcpServers": {"evil": 1}, "model_reasoning_effort": "high"})
    assert "error" not in res
    p = ext_child.received("thread/start")["params"]   # codex_run_v2 calls start_thread once (new thread)
    assert p["baseInstructions"] == "custom-base"      # forwarded — overrides the STERILE default
    assert p["developerInstructions"] == "be terse"    # forwarded by codex_run_v2
    assert p["config"]["mcp_servers"] == {}            # isolation wins after forward + merge
    assert "mcpServers" not in p["config"]
    assert p["config"]["model_reasoning_effort"] == "high"


# ---------------------------------------------------------------------------
# A7: CI fingerprint — coherence tripwire + slow version e2e
# ---------------------------------------------------------------------------

def test_fingerprint_matches_code_constants():
    """Coherence tripwire (NOT drift detection): committed fingerprint == code constants."""
    import json
    import os
    import codex_server as cs
    fp = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "codex-protocol-fingerprint.json")))
    assert set(fp["bridged_methods"]) == set(cs._BRIDGED_METHODS)
    assert set(fp["unsupported_methods"]) == set(cs._UNSUPPORTED_METHODS)
    assert fp["last_verified_codex_version"] == cs.LAST_VERIFIED_CODEX_VERSION
    for d in fp["command_decision_variants"]:
        assert cs._is_valid_command_decision(d)   # strings AND dict-shaped amendment variants both valid as-is
        # NOTE (R1-F1): amendment variants are stored as truthy dicts in the fixture, NOT bare
        # strings — `_is_valid_command_decision` accepts acceptWithExecpolicyAmendment /
        # applyNetworkPolicyAmendment ONLY as truthy dict payloads, never as plain strings.


@skip_if_no_codex          # existing module-level marker (line ~37); self-skips without codex
@pytest.mark.slow
def test_live_codex_version_matches_pin():
    """Maintainer ritual: live codex version == pin (else re-verify the bridge and bump
    LAST_VERIFIED_CODEX_VERSION). Reuses A4's passive capture + the existing real-codex
    singleton pattern (mirrors test_e2e_review_real_appserver) — NO new helper/fixture.
    `cft_or_codex` and `_live_codex_user_agent` do NOT exist; do not reference them."""
    import codex_server as cs
    cs.codex_run_v2({"prompt": "ping", "mode": "review"})   # drives ensure()+initialize on the singleton
    assert cs._get_manager()._codex_version == cs.LAST_VERIFIED_CODEX_VERSION


def test_tools_list_exposes_parity_fields_and_drift():
    import codex_server as cs
    props = cs.TOOLS[0]["inputSchema"]["properties"]
    for f in ("base_instructions", "developer_instructions", "config"):
        assert f in props
    assert "_drift" in cs.TOOLS[0]["description"]
