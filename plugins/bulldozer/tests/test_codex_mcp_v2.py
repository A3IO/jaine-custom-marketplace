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


@pytest.fixture(autouse=True)
def _isolate_codex_home(request, tmp_path_factory, monkeypatch):
    """Hermetic offline tests: CODEX_HOME → empty tmp dir. Slow (live-codex) tests
    are exempt — they need the real ~/.codex auth (F10)."""
    if request.node.get_closest_marker("slow"):
        return
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))


@pytest.fixture(autouse=True)
def _reset_cc_stream_fixture():
    """#264: install a fresh CCStream singleton before every test so same-process tests never
    inherit queued frames, a partial buffer, or a sticky EOF from a prior test (the module
    singleton _cc_stream is shared by main()/cc_read_fn/pump)."""
    import codex_server as cs
    cs._reset_cc_stream()
    yield


@pytest.fixture(autouse=True)
def _redirect_codex_log(tmp_path_factory, monkeypatch):
    """Hygiene: NO test (offline OR slow) writes drift/approval lines to the real
    ~/.claude/hooks/bulldozer-codex.log. Tests that assert log contents override this
    with their own monkeypatch.setenv in the body (runs after fixtures, so it wins).
    Independent of CODEX_HOME, so slow/live-codex tests are NOT exempt."""
    logdir = tmp_path_factory.mktemp("codexlog")
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logdir / "bulldozer-codex.log"))


def test_real_codex_log_is_never_touched_by_tests(tmp_path_factory):
    """Hygiene guard: the autouse redirect must point BULLDOZER_CODEX_LOG OFF the real
    monitoring log for EVERY test (offline + slow), so the suite never pollutes it
    (the cause of the uncalibratable #251 corpus)."""
    p = os.environ.get("BULLDOZER_CODEX_LOG")
    assert p, "BULLDOZER_CODEX_LOG must be set by the autouse redirect fixture"
    assert "/.claude/hooks/" not in p, f"test log must not be under the real hooks dir: {p}"
    # review E1: assert it lives under pytest's actual tmp base (robust to --basetemp), not a
    # fragile "/tmp"/"pytest" substring heuristic that false-fails on a custom basetemp.
    base = str(tmp_path_factory.getbasetemp())
    assert p.startswith(base), f"redirect must be under pytest tmp base {base}, got {p}"


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


class _FakePipe:
    """A pollable fd backed by an os.pipe; feed() writes the write end."""
    def __init__(self):
        self.r, self.w = os.pipe()
    def feed(self, data: bytes):
        os.write(self.w, data)
    def fileno(self):
        return self.r


def test_reactor_pump_default_is_child_only(monkeypatch):
    """watch_cc defaults False → select never includes sys.stdin (R2-F1)."""
    import json
    import codex_server as cs
    child = _FakePipe()
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    captured = {}
    real_select = cs.select.select
    def spy(rl, wl, xl, to):
        captured["rlist"] = list(rl)
        return real_select(rl, wl, xl, to)
    monkeypatch.setattr(cs.select, "select", spy)
    child.feed(b'{"method":"x","params":{}}\n')
    frames = r.pump(timeout=0.5)
    assert captured["rlist"] == [child.fileno()]          # stdin NOT watched
    assert frames and frames[0]["method"] == "x"
    assert all("__cc__" not in f for f in frames)          # no CC tagging


def test_reactor_pump_watch_cc_reads_cc_frame(monkeypatch):
    """watch_cc=True → a CC stdin line is parsed and tagged __cc__; child frames untagged."""
    import codex_server as cs
    child = _FakePipe()
    ccpipe = _FakePipe()
    # Real file object so BOTH fileno() (select) AND readline() (pump) work (R1-F4).
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(ccpipe.r))
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    ccpipe.feed(b'{"method":"notifications/cancelled","params":{"requestId":4}}\n')
    frames = r.pump(timeout=0.5, watch_cc=True)
    cc = [f["__cc__"] for f in frames if "__cc__" in f]
    assert len(cc) == 1 and cc[0]["method"] == "notifications/cancelled"
    assert cc[0]["params"]["requestId"] == 4


def test_reactor_pump_watch_cc_tags_eof(monkeypatch):
    """watch_cc=True → CC stdin EOF (write end closed) is tagged {"__eof__": True} (R1-F1)."""
    import codex_server as cs
    child = _FakePipe()
    ccpipe = _FakePipe()
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(ccpipe.r))
    os.close(ccpipe.w)                                  # close write end → reader sees EOF
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    frames = r.pump(timeout=0.5, watch_cc=True)
    cc = [f["__cc__"] for f in frames if "__cc__" in f]
    assert cc == [{"__eof__": True}]


# ---------------------------------------------------------------------------
# #264: CCStream — the single CC-stdin owner (shared os.read + JsonRpcStream queue)
# ---------------------------------------------------------------------------

def _cc_stdin(monkeypatch):
    """Point cs.sys.stdin at a fresh pipe; return the _FakePipe (write end via .feed)."""
    import codex_server as cs
    p = _FakePipe()
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(p.r))
    return p


def test_ccstream_single_frame(monkeypatch):
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    p.feed(b'{"method":"ping"}\n')
    assert s.next_frame(0.5) == ("frame", {"method": "ping"})


def test_ccstream_burst_two_frames_no_second_read(monkeypatch):
    """THE F4 FIX: two frames in ONE os.write — both delivered across two next_frame
    calls, the 2nd WITHOUT any new write (queue-first). Under readline the 2nd line
    would strand in the TextIOWrapper buffer (select reports fd-not-ready)."""
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    p.feed(b'{"method":"ping"}\n{"method":"notifications/cancelled"}\n')  # ONE write
    assert s.next_frame(0.5) == ("frame", {"method": "ping"})
    # No second feed: the 2nd frame must come from the queue.
    assert s.next_frame(0) == ("frame", {"method": "notifications/cancelled"})


def test_ccstream_partial_frame_across_writes(monkeypatch):
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    p.feed(b'{"id":1,')
    assert s.next_frame(0.2) == ("none", None)        # partial → nothing yet
    p.feed(b'"result":{}}\n')
    assert s.next_frame(0.5) == ("frame", {"id": 1, "result": {}})


def test_ccstream_bad_json_dropped(monkeypatch):
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    p.feed(b'not json at all\n')
    assert s.next_frame(0.3) == ("none", None)        # JsonRpcStream drops it → none


def test_ccstream_blank_line_dropped(monkeypatch):
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    p.feed(b'\n')
    assert s.next_frame(0.3) == ("none", None)        # blank skipped → none


def test_ccstream_eof_after_queued_frame(monkeypatch):
    """A frame then EOF in the buffer: frame delivered FIRST, then eof (queue drains
    before EOF surfaces — real bytes precede the 0-byte read)."""
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    p.feed(b'{"method":"x"}\n')
    os.close(p.w)                                     # EOF after the frame
    assert s.next_frame(0.5) == ("frame", {"method": "x"})
    assert s.next_frame(0.5) == ("eof", None)


def test_ccstream_eof_empty_queue(monkeypatch):
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    os.close(p.w)
    assert s.next_frame(0.5) == ("eof", None)


def test_ccstream_eof_is_sticky(monkeypatch):
    """Once EOF, every next_frame returns eof (no re-read)."""
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    os.close(p.w)
    assert s.next_frame(0.5) == ("eof", None)
    assert s.next_frame(0) == ("eof", None)


def test_ccstream_has_queued(monkeypatch):
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    s = cs.CCStream()
    assert s.has_queued() is False
    p.feed(b'{"a":1}\n{"b":2}\n')
    assert s.next_frame(0.5) == ("frame", {"a": 1})
    assert s.has_queued() is True                     # 2nd frame still buffered
    assert s.next_frame(0) == ("frame", {"b": 2})
    assert s.has_queued() is False


def test_reset_cc_stream_replaces_singleton(monkeypatch):
    """_reset_cc_stream() installs a fresh CCStream (clears queue/_buf/_eof)."""
    import codex_server as cs
    p = _cc_stdin(monkeypatch)
    cs._reset_cc_stream()
    first = cs._cc_stream
    p.feed(b'{"a":1}\n')
    assert cs._cc_stream.next_frame(0.5) == ("frame", {"a": 1})
    cs._reset_cc_stream()
    assert cs._cc_stream is not first                 # new instance
    assert cs._cc_stream.has_queued() is False        # fresh, empty


def test_pump_watch_cc_burst_two_frames_no_strand(monkeypatch):
    """#264 F4 REGRESSION: two CC frames in ONE os.write — pump(watch_cc=True) called twice
    must return BOTH (one per call). Under the old readline path the 2nd line stranded in the
    TextIOWrapper buffer (select saw the OS fd not-ready) and the 2nd pump returned no CC frame
    — a queued cancel was missed. This is the F4 hole; RED until pump drains CCStream."""
    import codex_server as cs
    cs._reset_cc_stream()
    child = _FakePipe()
    ccpipe = _FakePipe()
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(ccpipe.r))
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    # ONE write carrying two frames (ping + cancel), as a CC frame-batch would arrive:
    ccpipe.feed(b'{"method":"ping"}\n'
                b'{"method":"notifications/cancelled","params":{"requestId":7}}\n')
    f1 = [f["__cc__"] for f in r.pump(timeout=0.5, watch_cc=True) if "__cc__" in f]
    f2 = [f["__cc__"] for f in r.pump(timeout=0.5, watch_cc=True) if "__cc__" in f]
    assert f1 == [{"method": "ping"}]
    assert f2 == [{"method": "notifications/cancelled", "params": {"requestId": 7}}]


def test_no_readline_on_cc_stdin_path():
    """#264 grep-guard (AST-based, ignores docstrings/comments): no `sys.stdin.readline()` and
    no `for line in sys.stdin` remain in CODE — os.read and TextIOWrapper.readline must never
    both read the CC fd. The ONLY legitimate `sys.stdin` access is CCStream._fd()'s .fileno()."""
    import ast
    src = open(os.path.join(MCP_DIR, "codex_server.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    def _is_sys_stdin(n):
        return (isinstance(n, ast.Attribute) and n.attr == "stdin"
                and isinstance(n.value, ast.Name) and n.value.id == "sys")

    readline_calls, stdin_iter, stdin_attrs = [], [], []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "readline" and _is_sys_stdin(node.func.value)):
            readline_calls.append(node.lineno)
        if isinstance(node, ast.For) and _is_sys_stdin(node.iter):
            stdin_iter.append(node.lineno)
        if _is_sys_stdin(node):
            stdin_attrs.append(node.lineno)

    assert readline_calls == [], f"sys.stdin.readline() must not appear in code (lines {readline_calls})"
    assert stdin_iter == [], f"`for line in sys.stdin` must not appear in code (lines {stdin_iter})"
    # Exactly one sys.stdin touch in code — CCStream._fd() -> sys.stdin.fileno().
    assert len(stdin_attrs) == 1, f"sys.stdin should be accessed exactly once (CCStream._fd); found at {stdin_attrs}"


def test_shared_queue_handoff_cc_read_then_pump(monkeypatch):
    """#264 bonus property: a burst [elicitation-response, cancel] in one write is split across
    consumers — the approval-wait drain (next_frame) takes the response, and the still-queued
    cancel surfaces in the turn-loop pump. Under readline the trailing cancel stranded in the
    TextIOWrapper across the approval->turn-loop boundary; the shared queue hands it off."""
    import codex_server as cs
    cs._reset_cc_stream()
    child = _FakePipe()
    ccpipe = _FakePipe()
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(ccpipe.r))
    r = cs.Reactor(child.fileno(), os.open(os.devnull, os.O_WRONLY))
    ccpipe.feed(b'{"id":5,"result":{"action":"accept"}}\n'
                b'{"method":"notifications/cancelled","params":{"requestId":9}}\n')
    # consumer 1 (approval-wait style next_frame): takes the elicitation response
    kind, resp = cs._cc_stream.next_frame(0.5)
    assert kind == "frame" and resp == {"id": 5, "result": {"action": "accept"}}
    # consumer 2 (turn-loop pump): the cancel is still queued and surfaces here
    cc = [f["__cc__"] for f in r.pump(timeout=0.5, watch_cc=True) if "__cc__" in f]
    assert cc == [{"method": "notifications/cancelled", "params": {"requestId": 9}}]


def _mk_ts(**over):
    """Build a turn-state dict for _handle_child_frame / interrupt-routine unit tests."""
    ts = {"final_message_parts": [], "usage_snapshot": {}, "retries": 0,
          "interrupting": False, "interrupted_by": "cancel", "acc": [],
          "manager": None, "turn_start_t": 0.0, "mcp_mode": "isolated",
          "mcp_servers_enabled": [], "effort_val": None, "model_val": None,
          "mode": "implement", "thread_id": "t1", "review_target": None}
    ts.update(over)
    return ts


def test_handle_child_frame_accumulates_delta():
    import codex_server as cs
    ts = _mk_ts()
    out = cs._handle_child_frame(
        {"method": "item/agentMessage/delta", "params": {"delta": "hello"}}, ts)
    assert out is None and ts["final_message_parts"] == ["hello"]


def test_handle_child_frame_completed_returns_result():
    import codex_server as cs
    ts = _mk_ts(final_message_parts=["done"])
    out = cs._handle_child_frame(
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}, ts)
    assert out is not None and "error" not in out and out.get("result") == "done"


def test_handle_child_frame_failed_status_returns_error():
    import codex_server as cs
    ts = _mk_ts()
    out = cs._handle_child_frame(
        {"method": "turn/completed", "params": {"turn": {"status": "failed"}}}, ts)
    assert out is not None and "error" in out


def test_build_interrupted_result_no_error_key_and_partial():
    import codex_server as cs
    ts = _mk_ts(mode="implement", final_message_parts=["partial work"])
    res = cs._build_interrupted_result(ts, interrupted_by="cancel")
    assert "error" not in res                         # F7: isError must stay false
    assert res["status"] == "interrupted"
    assert res["interrupted_by"] == "cancel"
    assert res["partial_text"] == "partial work"
    assert res["thread_warm"] is True
    assert res["result"] == "partial work"            # implement mode shape preserved


def test_build_interrupted_result_review_mode_shape():
    import codex_server as cs
    ts = _mk_ts(mode="review", final_message_parts=['{"verdict":"x","findings":[]}'])
    res = cs._build_interrupted_result(ts, interrupted_by="timeout")
    assert "error" not in res and res["status"] == "interrupted"
    assert "schema_ok" in res or "verdict" in res     # review shape keys present


def test_build_interrupted_result_teardown_thread_cold():
    import codex_server as cs
    ts = _mk_ts(final_message_parts=[])
    res = cs._build_interrupted_result(ts, interrupted_by="cancel", thread_warm=False)
    assert res["thread_warm"] is False and res["partial_text"] == ""


def test_dispatcher_interrupted_result_not_marked_iserror():
    """An interrupted result has no 'error' key → the dispatcher's `if 'error' in res` rule
    keeps isError unset (a graceful partial, not a failure)."""
    res = {"status": "interrupted", "partial_text": "", "thread_id": "t"}
    assert ("error" in res) is False


class _ScriptedReactor:
    """A reactor whose pump() replays scripted frame-batches (for interrupt-routine tests)."""
    def __init__(self, batches):
        self._batches = list(batches)
    def pump(self, timeout=0.1, watch_cc=False):
        return self._batches.pop(0) if self._batches else []


class _FakeManager:
    def __init__(self, reactor):
        self._reactor = reactor
        self.writes = []
        self._idc = 0
        self._child = type("C", (), {"kill": lambda self: None})()
    def _write(self, frame):
        self.writes.append(frame)
    def _next_id(self):
        self._idc += 1
        return self._idc


def test_run_interrupt_sends_turn_interrupt_with_id_and_returns_graceful():
    import codex_server as cs
    # batch: the empty {} response to turn/interrupt (id==1) THEN turn/completed interrupted
    r = _ScriptedReactor([[{"id": 1, "result": {}},
                           {"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}}]])
    mgr = _FakeManager(r)
    ts = _mk_ts(final_message_parts=["half"]); ts["manager"] = mgr
    res = cs._run_interrupt(mgr, ts, turn_id="turn_1", interrupted_by="cancel")
    assert mgr.writes[0]["method"] == "turn/interrupt"
    assert mgr.writes[0]["id"] == 1                       # R2-F1: request, not notification
    assert mgr.writes[0]["params"] == {"threadId": "t1", "turnId": "turn_1"}
    assert res["status"] == "interrupted" and res["partial_text"] == "half"
    assert res["thread_warm"] is True and "error" not in res
    assert "_drift" not in res                            # the {} response is consumed, not drift


def test_run_interrupt_no_completion_tears_down_cold(monkeypatch):
    import codex_server as cs
    monkeypatch.setattr(cs, "_INTERRUPT_COMPLETE_TIMEOUT", 0.05)
    r = _ScriptedReactor([])                              # child never completes
    mgr = _FakeManager(r)
    killed = {"n": 0}
    mgr._child = type("C", (), {"kill": lambda self: killed.__setitem__("n", killed["n"] + 1)})()
    ts = _mk_ts(); ts["manager"] = mgr
    res = cs._run_interrupt(mgr, ts, turn_id="turn_1", interrupted_by="timeout")
    assert killed["n"] == 1 and mgr._child is None
    assert res["status"] == "interrupted" and res["thread_warm"] is False


def test_run_interrupt_no_turn_id_tears_down_without_sending():
    import codex_server as cs
    r = _ScriptedReactor([]); mgr = _FakeManager(r)
    ts = _mk_ts(); ts["manager"] = mgr
    res = cs._run_interrupt(mgr, ts, turn_id=None, interrupted_by="cancel")
    assert mgr.writes == []                               # nothing sent — no turnId
    assert res["thread_warm"] is False and res["status"] == "interrupted"


def test_route_cc_cancel_for_our_id_returns_interrupt():
    import codex_server as cs
    replies = []
    f = {"method": "notifications/cancelled", "params": {"requestId": 7}}
    assert cs._route_cc_frame(f, cc_id=7, reply_fn=lambda *a, **k: replies.append((a, k))) == "interrupt"
    assert replies == []                        # a notification gets no reply


def test_route_cc_cancel_for_other_id_continues():
    import codex_server as cs
    f = {"method": "notifications/cancelled", "params": {"requestId": 99}}
    assert cs._route_cc_frame(f, cc_id=7, reply_fn=lambda *a, **k: None) == "continue"


def test_route_cc_second_tools_call_gets_calltoolresult_busy():
    import codex_server as cs
    seen = {}
    def reply_fn(mid, result=None, error=None):
        seen.update(id=mid, result=result, error=error)
    f = {"id": 12, "method": "tools/call", "params": {"name": "codex_run"}}
    assert cs._route_cc_frame(f, cc_id=7, reply_fn=reply_fn) == "continue"
    assert seen["id"] == 12 and seen["error"] is None
    assert seen["result"]["isError"] is True
    assert "already in flight" in seen["result"]["content"][0]["text"]


def test_route_cc_ping_and_tools_list_get_valid_results():
    import codex_server as cs
    out = []
    def reply_fn(mid, result=None, error=None):
        out.append((mid, result, error))
    assert cs._route_cc_frame({"id": 1, "method": "ping"}, 7, reply_fn) == "continue"
    assert out[-1] == (1, {}, None)
    assert cs._route_cc_frame({"id": 2, "method": "tools/list"}, 7, reply_fn) == "continue"
    assert out[-1][0] == 2 and "tools" in out[-1][1]


def test_route_cc_unparseable_or_notification_continues():
    import codex_server as cs
    assert cs._route_cc_frame(None, 7, lambda *a, **k: None) == "continue"
    assert cs._route_cc_frame({"method": "notifications/foo"}, 7, lambda *a, **k: None) == "continue"


def test_route_cc_response_shaped_frame_is_ignored():
    """A response-shaped CC frame mid-turn (id + result, no method) is not ours to answer (R1-F3)."""
    import codex_server as cs
    out = []
    assert cs._route_cc_frame({"id": 5, "result": {"action": "accept"}}, 7,
                              lambda *a, **k: out.append(a)) == "continue"
    assert out == []                            # no reply written to a response


def test_route_cc_eof_marker_returns_teardown():
    """CC stdin EOF marker → teardown (CC gone) (R1-F1)."""
    import codex_server as cs
    assert cs._route_cc_frame({"__eof__": True}, 7, lambda *a, **k: None) == "teardown"


def test_interrupts_enabled_env_gate(monkeypatch):
    import codex_server as cs
    monkeypatch.delenv("BULLDOZER_CODEX_NO_INTERRUPT", raising=False)
    assert cs._interrupts_enabled() is True
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    assert cs._interrupts_enabled() is False


# (#218 turn-pump interrupt INTEGRATION tests live after ExtendedFakeChild / call_codex_run,
#  since InterruptFakeChild subclasses ExtendedFakeChild — see "Task 6 integration" below.)


# InterruptFakeChild + _drive_interrupt + the #218 integration tests are defined below,
# after ExtendedFakeChild / call_codex_run (see "Task 6 integration").


def test_python_version_error_guard():
    """#256: tomllib needs py3.11+, but .mcp.json launches a bare `python3` — the guard returns a
    clear message (not a cryptic ModuleNotFoundError) on an old interpreter."""
    from codex_server import _python_version_error
    assert _python_version_error((3, 10)) is not None
    assert "3.11" in _python_version_error((3, 10))
    assert _python_version_error((3, 11)) is None
    assert _python_version_error((3, 14)) is None


def test_initialize_result_includes_routing_instructions():
    """#256: the initialize reply carries an `instructions` routing manifest — CC injects
    InitializeResult.instructions into the model's context on connect (CC #30135), so the model
    can discover/choose codex_review / codex_run / codex_info (the latter two are MCP-only)."""
    from codex_server import _initialize_result, SERVER_INSTRUCTIONS, PROTO
    r = _initialize_result({"protocolVersion": "2025-06-18"})
    assert r["instructions"] == SERVER_INSTRUCTIONS
    for tok in ("codex_review", "codex_run", "codex_info", "isolated"):
        assert tok in r["instructions"], tok
    assert r["serverInfo"]["name"] == "bulldozer-codex"
    assert r["protocolVersion"] == "2025-06-18"
    assert r["capabilities"] == {"tools": {}}
    assert _initialize_result({})["protocolVersion"] == PROTO  # falls back when caller omits it


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
    from codex_server import AppServerManager, STERILE_INSTRUCTIONS
    m = AppServerManager(bin=fake_child)
    m.ensure()
    m.start_thread(sandbox="read-only", approval_policy="on-request",
                   base_instructions=STERILE_INSTRUCTIONS,
                   config={"model_reasoning_effort": "high"}, cwd=str(tmp_path))
    p = fake_child.received("thread/start")["params"]
    assert p.get("ephemeral") in (None, False)
    assert p["baseInstructions"] == STERILE_INSTRUCTIONS
    assert p["config"] == {"model_reasoning_effort": "high"}  # benign passthrough; no injected mcp_servers
    assert p["sandbox"] == "read-only"
    assert p["cwd"] == str(tmp_path)


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


def test_permissions_accept_echoes_requested_profile():
    """#4: accepting item/permissions/requestApproval must GRANT what codex asked
    for — echo params['permissions'] into the response — not an empty {} (a silent
    no-op). Schema: request/response profiles share the {fileSystem?,network?} shape."""
    from codex_server import handle_server_request, LBL_GRANT_TURN, LBL_GRANT_SESSION, LBL_DONT_GRANT
    requested = {"network": {"enabled": True},
                 "fileSystem": {"entries": [{"access": "read",
                                             "path": {"type": "path", "path": "/x"}}]}}

    def run(label):
        cc = FakeCC()
        cc.set_answer("accept", {"label": label})
        msg = {"id": "perm", "method": "item/permissions/requestApproval",
               "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                          "startedAtMs": 1, "cwd": "/tmp", "reason": None,
                          "permissions": requested}}
        return handle_server_request(msg, cc.write, cc.read)["result"]

    grant_turn = run(LBL_GRANT_TURN)
    assert grant_turn == {"permissions": requested, "scope": "turn"}, grant_turn

    grant_session = run(LBL_GRANT_SESSION)
    assert grant_session == {"permissions": requested, "scope": "session"}, grant_session

    # Decline still grants nothing (safe default preserved).
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_DONT_GRANT})
    msg = {"id": "perm2", "method": "item/permissions/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                      "startedAtMs": 1, "cwd": "/tmp", "reason": None,
                      "permissions": requested}}
    declined = handle_server_request(msg, cc.write, cc.read)["result"]
    assert declined == {"permissions": {}, "scope": "turn"}, declined


def test_permissions_dialog_surfaces_requested_profile():
    """#4 safety (codex_review P1): once an accept GRANTS the requested fs/network
    profile (not empty {}), the approval dialog MUST render what is being granted —
    otherwise the user clicks Grant blind. Mirrors the command dialog, which shows the
    authoritative command/cwd. The path/host are authoritative → never translated."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("accept", {"label": "Grant for this turn"})
    requested = {"network": {"enabled": True},
                 "fileSystem": {"entries": [{"access": "write",
                                             "path": {"type": "path", "path": "/etc/hosts"}}]}}
    msg = {"id": "perm", "method": "item/permissions/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                      "startedAtMs": 1, "cwd": "/tmp", "reason": None,
                      "permissions": requested}}
    handle_server_request(msg, cc.write, cc.read)
    dialog = _last_elicit_message(cc)
    assert "/etc/hosts" in dialog, f"requested path not surfaced in dialog: {dialog!r}"
    assert "write" in dialog, f"requested access mode not surfaced: {dialog!r}"
    assert "network" in dialog.lower(), f"requested network not surfaced: {dialog!r}"


def test_permissions_dialog_empty_profile_no_detail_line():
    """An empty requested profile (codex asks with permissions={}) adds no payload
    summary line — the dialog degrades to the prior header+reason shape, no crash."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("accept", {"label": "Grant for this turn"})
    msg = {"id": "perm", "method": "item/permissions/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                      "startedAtMs": 1, "cwd": "/tmp", "reason": None, "permissions": {}}}
    r = handle_server_request(msg, cc.write, cc.read)["result"]
    assert r == {"permissions": {}, "scope": "turn"}, r
    # message still well-formed (no exception, has the permissions header)
    assert _last_elicit_message(cc)


def _perm_msg(permissions, label="Grant for this turn"):
    """Drive a permissions approval with a given requested profile + chosen label;
    return (result_dict, dialog_message)."""
    from codex_server import handle_server_request
    cc = FakeCC()
    cc.set_answer("accept", {"label": label} if label is not None else {})
    msg = {"id": "perm", "method": "item/permissions/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                      "startedAtMs": 1, "cwd": "/tmp", "reason": None,
                      "permissions": permissions}}
    res = handle_server_request(msg, cc.write, cc.read)["result"]
    return res, _last_elicit_message(cc)


def test_permissions_dialog_network_visible_even_with_large_filesystem():
    """B (review): a large fileSystem profile must NOT truncate the security-sensitive
    network grant off the dialog. Network appended last + a single-line summary + a
    head-only char cap hid it; the user would grant egress they never saw."""
    requested = {"fileSystem": {"entries": [
        {"access": "write", "path": {"type": "path", "path": "/very/long/path/number/{:02d}/file".format(i)}}
        for i in range(25)]},
        "network": {"enabled": True}}
    res, dialog = _perm_msg(requested)
    assert res == {"permissions": requested, "scope": "turn"}, res   # grant still full+exact
    assert "network" in dialog.lower(), f"network grant truncated off the dialog: {dialog!r}"


def test_permissions_unknown_label_fails_closed_to_decline():
    """C (review): #4 made grant meaningful, so an accept with an UNRECOGNIZED label
    must fail CLOSED (grant nothing) — not silently grant the full profile. Bare accept
    (no label) is the legitimate plain-Accept and still grants for the turn."""
    requested = {"network": {"enabled": True}}
    # present-but-unknown label → decline (grant nothing)
    res_unknown, _ = _perm_msg(requested, label="Totally Bogus Label")
    assert res_unknown == {"permissions": {}, "scope": "turn"}, res_unknown
    # bare accept (no label key) → still grants for the turn (CC's plain Accept)
    res_bare, _ = _perm_msg(requested, label=None)
    assert res_bare == {"permissions": requested, "scope": "turn"}, res_bare


def test_permissions_non_dict_requested_not_echoed():
    """D (review): a malformed truthy non-dict permissions (e.g. a list) must NOT be
    echoed verbatim into the grant (a schema-violating response); fail open to {}."""
    res, _ = _perm_msg(["fileSystem", "network"])   # truthy non-dict
    assert res == {"permissions": {}, "scope": "turn"}, res


def test_summarize_permissions_legacy_read_write_lists():
    """E5 (review): legacy fileSystem.read/write path-lists are summarized (not just the
    entries form), so the user sees those paths in the dialog."""
    from codex_server import _summarize_permissions
    s = _summarize_permissions({"fileSystem": {"read": ["/a/r"], "write": ["/a/w"]}})
    assert "/a/r" in s and "/a/w" in s, s


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


# ── #239 / #224: approval-dialog truncation + context surfacing ───────────
# #239: a huge heredoc command must not flood the elicitation dialog (300-line wall,
# decision buttons scroll off). #224: surface codex's reason / commandActions /
# networkApprovalContext / agentMessage narrative in the SAME dialog.

def test_truncate_for_display_short_text_unchanged():
    from codex_server import _truncate_for_display
    s = "echo hi\nls -la"
    assert _truncate_for_display(s) == s  # fits → returned verbatim, no marker


def test_truncate_for_display_none_is_empty():
    from codex_server import _truncate_for_display
    assert _truncate_for_display(None) == ""


def test_truncate_for_display_caps_many_lines():
    from codex_server import _truncate_for_display
    s = "\n".join(f"line{i}" for i in range(50))
    out = _truncate_for_display(s, max_lines=12)
    assert out.count("\n") <= 13  # ≤12 kept lines + 1 marker line
    assert "line0" in out and "line11" in out
    assert "line40" not in out  # dropped tail not shown
    assert "…" in out and "more line" in out


def test_truncate_for_display_caps_single_long_line_by_chars():
    from codex_server import _truncate_for_display
    s = "a" * 5000  # single line, no newlines → must still be capped by chars
    out = _truncate_for_display(s, max_chars=800)
    assert len(out) < 5000
    assert "…" in out and "more char" in out


def test_truncate_for_display_keeps_tail_when_char_capped():
    """codex_review P2 (round 2): a command too long by CHARS (few lines, or a huge head)
    must STILL show its tail when tail_lines>0 — an op appended after a huge generated
    line (rm / uv run / && …) is exactly what the truncation must keep visible."""
    from codex_server import _truncate_for_display
    s = "echo " + "A" * 5000 + " && rm -rf /important"
    out = _truncate_for_display(s, max_chars=800, tail_lines=4)
    assert out.startswith("echo ")            # head preserved
    assert "rm -rf /important" in out          # TAIL preserved despite char-driven cap
    assert "…" in out
    assert len(out) < 1400                     # still bounded


def test_summarize_command_actions_friendly_kinds():
    from codex_server import _summarize_command_actions
    actions = [
        {"type": "read", "name": "foo.py", "path": "/p/foo.py", "command": "cat foo.py"},
        {"type": "search", "query": "TODO", "command": "rg TODO"},
        {"type": "listFiles", "path": "/p", "command": "ls /p"},
    ]
    out = _summarize_command_actions(actions)
    assert "read foo.py" in out
    assert "search 'TODO'" in out
    assert "list /p" in out


def test_summarize_command_actions_skips_unknown_and_nonlist():
    from codex_server import _summarize_command_actions
    assert _summarize_command_actions(None) == ""
    assert _summarize_command_actions("nope") == ""
    # 'unknown' adds nothing over the raw command → skipped
    assert _summarize_command_actions([{"type": "unknown", "command": "weird"}]) == ""


def _cmd_approval_msg_with(command, **extra):
    params = {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
              "startedAtMs": 1000, "command": command, "cwd": "/tmp"}
    params.update(extra)
    return {"id": "req-x", "method": "item/commandExecution/requestApproval", "params": params}


def _last_elicit_message(cc):
    elicit = next(r for r in cc._requests if r.get("method") == "elicitation/create")
    return elicit["params"]["message"]


def test_truncate_for_display_head_and_tail_keeps_both_ends():
    """#239 security review: tail_lines keeps the FIRST and LAST lines (middle dropped) so a
    dangerous op appended after a benign heredoc stays visible at approval time."""
    from codex_server import _truncate_for_display
    s = "\n".join(f"line{i}" for i in range(50))
    out = _truncate_for_display(s, max_lines=12, tail_lines=4)
    assert "line0" in out          # head shown
    assert "line49" in out         # tail shown (the security point)
    assert "line25" not in out     # middle dropped
    assert "…" in out and "more line" in out
    assert out.count("\n") <= 14   # head(8) + marker + tail(4) bounded


def test_command_approval_message_truncates_long_command():
    """#239 (grounded in the real 04ad23aa incident: cat>...<<PY ~230 lines PY; uv run pytest):
    the heredoc body is bounded (head+tail), but the executable TAIL (uv run pytest) stays
    visible — head-only would have hidden exactly the command being run."""
    from codex_server import handle_server_request
    big = ("cat > tests/t.py <<'PY'\n"
           + "\n".join(f"x{i} = {i}" for i in range(300))
           + "\nPY\nuv run pytest tests/t.py -v")
    cc = FakeCC()
    handle_server_request(_cmd_approval_msg_with(big), cc.write, cc.read)
    msg = _last_elicit_message(cc)
    assert msg.count("\n") < 40, f"dialog still a wall: {msg.count(chr(10))} lines"
    assert "…" in msg and "more line" in msg
    assert "CWD: /tmp" in msg
    assert "cat > tests/t.py" in msg          # head shown
    assert "uv run pytest tests/t.py -v" in msg  # TAIL shown — the executable action
    assert "x150 = 150" not in msg            # heredoc middle dropped


def test_command_approval_message_bounds_oversized_context_fields():
    """#239 completeness: a huge reason / commandActions / cwd must NOT bloat the dialog
    in spite of command truncation (panel finding E)."""
    from codex_server import handle_server_request
    cc = FakeCC()
    handle_server_request(_cmd_approval_msg_with(
        "echo hi",
        cwd="/" + "d/" * 500,                              # pathological cwd
        reason="R" * 5000,                                  # huge reason
        commandActions=[{"type": "read", "name": "n" * 5000, "command": "cat"}],
    ), cc.write, cc.read)
    msg = _last_elicit_message(cc)
    assert msg.count("\n") < 40, f"context fields bloated the dialog: {msg.count(chr(10))} lines"
    assert len(msg) < 4000, f"dialog too large: {len(msg)} chars"


def test_command_approval_message_short_command_no_marker():
    """#239: a short command is shown verbatim — no truncation marker."""
    from codex_server import handle_server_request
    cc = FakeCC()
    handle_server_request(_cmd_approval_msg_with("echo hi"), cc.write, cc.read)
    msg = _last_elicit_message(cc)
    assert "echo hi" in msg
    assert "…" not in msg and "more line" not in msg


def test_command_approval_message_surfaces_reason_actions_network():
    """#224: reason + commandActions summary + networkApprovalContext host appear in the dialog."""
    from codex_server import handle_server_request
    cc = FakeCC()
    handle_server_request(_cmd_approval_msg_with(
        "curl https://api.example.com",
        reason="needs network access",
        commandActions=[{"type": "read", "name": "secrets.env",
                         "path": "/p/secrets.env", "command": "cat"}],
        networkApprovalContext={"host": "api.example.com", "protocol": "https"},
    ), cc.write, cc.read)
    msg = _last_elicit_message(cc)
    assert "needs network access" in msg
    assert "read secrets.env" in msg
    assert "api.example.com" in msg


def test_command_approval_message_includes_narrative_when_provided():
    """#224: a narrative passed to bridge_approval is surfaced as 'Codex explained: …'."""
    from codex_server import bridge_approval
    cc = FakeCC()
    params = {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
              "startedAtMs": 1, "command": "echo hi", "cwd": "/tmp"}
    bridge_approval("item/commandExecution/requestApproval", params, cc.write, cc.read,
                    narrative="I'll run a quick sanity check first.")
    msg = _last_elicit_message(cc)
    assert "Codex explained:" in msg
    assert "sanity check" in msg


def test_filechange_approval_includes_narrative():
    """#224: narrative is surfaced in the fileChange dialog too."""
    from codex_server import bridge_approval
    cc = FakeCC()
    params = {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
              "startedAtMs": 1, "reason": "edit file"}
    bridge_approval("item/fileChange/requestApproval", params, cc.write, cc.read,
                    narrative="Patching the config to add the flag.")
    msg = _last_elicit_message(cc)
    assert "Codex explained:" in msg
    assert "Patching the config" in msg


def test_permissions_approval_includes_narrative():
    """#224: narrative is surfaced in the permissions dialog too."""
    from codex_server import bridge_approval
    cc = FakeCC()
    params = {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
              "startedAtMs": 1, "reason": "need perms"}
    bridge_approval("item/permissions/requestApproval", params, cc.write, cc.read,
                    narrative="Requesting write access to apply the change.")
    msg = _last_elicit_message(cc)
    assert "Codex explained:" in msg
    assert "write access" in msg


def test_command_approval_no_narrative_no_explained_line():
    """Back-compat: no narrative → no 'Codex explained:' line."""
    from codex_server import handle_server_request
    cc = FakeCC()
    handle_server_request(_cmd_approval_msg_with("echo hi"), cc.write, cc.read)
    msg = _last_elicit_message(cc)
    assert "Codex explained:" not in msg


# ── #251 step-0: approval-event logging ───────────────────────────────────

_APPROVAL_PARAMS = {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                    "startedAtMs": 1, "command": "echo hi", "cwd": "/tmp"}


def _approval_log_fields(line):
    """Parse ' | '-delimited key=value segments of an APPROVAL log line."""
    return dict(seg.split("=", 1) for seg in line.split(" | ") if "=" in seg)


def test_bridge_approval_logs_accept_event(tmp_path, monkeypatch):
    """#251 step-0: a completed approval writes ONE APPROVAL line carrying
    method / decision / wait_ms / timed_out."""
    from codex_server import bridge_approval
    logf = tmp_path / "codex.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    cc = FakeCC()  # default answer: accept
    bridge_approval("item/commandExecution/requestApproval", dict(_APPROVAL_PARAMS),
                    cc.write, cc.read)
    assert logf.exists(), "approval event was not logged"
    lines = [l for l in logf.read_text().splitlines() if "| APPROVAL |" in l]
    assert len(lines) == 1, lines
    f = _approval_log_fields(lines[0])
    assert f["method"] == "item/commandExecution/requestApproval"
    assert f["decision"] == "accept"
    assert f["timed_out"] == "false"
    assert int(f["wait_ms"]) >= 0


def test_bridge_approval_logs_timeout_event(tmp_path, monkeypatch):
    """#251 step-0: a timed-out approval logs timed_out=true plus the safe-default decision."""
    from codex_server import bridge_approval
    logf = tmp_path / "codex.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    cc = FakeCC()
    cc.never_answer_elicitation()
    decision = bridge_approval("item/commandExecution/requestApproval",
                               dict(_APPROVAL_PARAMS), cc.write, cc.read, timeout=0.05)
    assert decision == "decline"  # safe default on no-reply
    assert logf.exists(), "timed-out approval event was not logged"
    lines = [l for l in logf.read_text().splitlines() if "| APPROVAL |" in l]
    assert len(lines) == 1, lines
    f = _approval_log_fields(lines[0])
    assert f["timed_out"] == "true"
    assert f["decision"] == "decline"


def test_bridge_approval_log_best_effort_never_raises(monkeypatch):
    """#251 step-0: logging is best-effort — an unwritable log path never breaks the approval."""
    from codex_server import bridge_approval
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", "/nonexistent-root/x/y.log")
    cc = FakeCC()  # default accept
    decision = bridge_approval("item/commandExecution/requestApproval",
                               dict(_APPROVAL_PARAMS), cc.write, cc.read)
    assert decision == "accept"  # returned despite the unwritable log


def test_log_approval_event_sanitizes_delimiters(tmp_path, monkeypatch):
    """#251 step-0: a decision/method value carrying the log delimiter or a newline must NOT
    corrupt the greppable single-line format the #251 miner parses. The CC 'action' passthrough
    (mcpServer/elicitation/request) is free text → defend at the write boundary (review P3)."""
    from codex_server import _log_approval_event
    logf = tmp_path / "codex.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    _log_approval_event("m | x\ninjected", {"action": "a | b\nfake"}, 5, False)
    physical = [l for l in logf.read_text().splitlines() if l.strip()]
    assert len(physical) == 1, physical          # a newline in a value must not add lines
    f = _approval_log_fields(physical[0])
    assert "fake" in f["decision"]               # the whole value survived in ONE field
    assert f["wait_ms"] == "5"
    assert f["timed_out"] == "false"


def test_approval_decision_label_amendment_dicts():
    """#251 step-0: amendment-accept decisions log their kind, not the generic 'other' —
    they ARE accepts ('Allow & always permit' / network-rule), and losing that defeats the
    #251 mining this step exists to enable (codex_review P2)."""
    from codex_server import _approval_decision_label
    assert _approval_decision_label(
        {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": {}}}) == "accept:execpolicy"
    assert _approval_decision_label(
        {"applyNetworkPolicyAmendment": {"network_policy_amendment": {}}}) == "accept:network"


def test_narrative_streamed_before_approval_reaches_dialog():
    """#224 main work: an agentMessage narrative streamed BEFORE an approval is threaded
    through the pump into the elicitation dialog ('Codex explained: …')."""
    from codex_server import codex_run_v2, AppServerManager

    NARRATIVE = "I'll first read the config to understand the layout."

    class _NarrativeThenApproval(ExtendedFakeChild):
        def _dispatch(self, msg):
            method = msg.get("method")
            mid = msg.get("id")
            params = msg.get("params") or {}
            # The bridge's reply to our approval (id=APPROVE1, has result) → finish the turn.
            if mid == "APPROVE1" and "result" in msg:
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": "T1",
                    "turn": {"id": "TURN1", "items": [], "itemsView": "loaded",
                             "status": "completed", "error": None,
                             "startedAt": 0, "completedAt": 0, "durationMs": 10}}})
                return
            if method == "turn/start":
                self.turn_start_params = params
                # 1. ACK
                self._write_msg({"id": mid, "result": {"turn": {
                    "id": "TURN1", "items": [], "itemsView": "loaded",
                    "status": "running", "error": None,
                    "startedAt": 0, "completedAt": None, "durationMs": None}}})
                # 2. narrative streamed BEFORE the approval (codex flushes it first)
                self._write_msg({"method": "item/agentMessage/delta", "params": {
                    "delta": NARRATIVE, "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1"}})
                # 3. approval request
                self._write_msg({"id": "APPROVE1",
                                 "method": "item/commandExecution/requestApproval",
                                 "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                                            "startedAtMs": 1, "command": "cat config.toml",
                                            "cwd": "/tmp"}})
                return
            super()._dispatch(msg)

    fc = _NarrativeThenApproval()
    try:
        m = AppServerManager(bin=fc)
        written: list = []

        def cc_write(f):
            written.append(f)

        def cc_read(timeout=10.0):
            eid = next((w["id"] for w in reversed(written)
                        if w.get("method") == "elicitation/create"), 1)
            return {"id": eid, "result": {"action": "accept", "content": None}}

        r = codex_run_v2({"prompt": "hi", "mcp": "isolated", "mode": "implement"},
                         manager=m, cc_write_fn=cc_write, cc_read_fn=cc_read)
        assert "error" not in r, r
        elicit = next(w for w in written if w.get("method") == "elicitation/create")
        assert "Codex explained:" in elicit["params"]["message"]
        assert "read the config" in elicit["params"]["message"]
    finally:
        fc.kill()


def test_non_narrative_request_does_not_consume_narrative(monkeypatch):
    """Panel finding (Grok#2): the narrative offset must advance ONLY for narrative-bearing
    approvals. A non-narrative request (tool/requestUserInput) arriving between the narrative
    and a commandExecution approval must NOT consume the narrative — else the command approval
    would show nothing."""
    from codex_server import codex_run_v2, AppServerManager

    NARRATIVE = "I'll inspect the layout before editing."

    class _UserInputThenApproval(ExtendedFakeChild):
        def _dispatch(self, msg):
            method = msg.get("method")
            mid = msg.get("id")
            params = msg.get("params") or {}
            if mid == "USERINPUT1" and "result" in msg:
                # after answering the (non-narrative) user-input, emit the command approval
                self._write_msg({"id": "APPROVE1",
                                 "method": "item/commandExecution/requestApproval",
                                 "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                                            "startedAtMs": 1, "command": "vi config", "cwd": "/tmp"}})
                return
            if mid == "APPROVE1" and "result" in msg:
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": "T1",
                    "turn": {"id": "TURN1", "items": [], "itemsView": "loaded",
                             "status": "completed", "error": None,
                             "startedAt": 0, "completedAt": 0, "durationMs": 10}}})
                return
            if method == "turn/start":
                self.turn_start_params = params
                self._write_msg({"id": mid, "result": {"turn": {
                    "id": "TURN1", "items": [], "itemsView": "loaded",
                    "status": "running", "error": None,
                    "startedAt": 0, "completedAt": None, "durationMs": None}}})
                # narrative streamed FIRST
                self._write_msg({"method": "item/agentMessage/delta", "params": {
                    "delta": NARRATIVE, "threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1"}})
                # then a NON-narrative request (must not consume the narrative offset)
                self._write_msg({"id": "USERINPUT1",
                                 "method": "item/tool/requestUserInput",
                                 "params": {"threadId": "T1", "turnId": "TURN1", "itemId": "ITEM1",
                                            "startedAtMs": 1}})
                return
            super()._dispatch(msg)

    fc = _UserInputThenApproval()
    try:
        m = AppServerManager(bin=fc)
        written: list = []

        def cc_write(f):
            written.append(f)

        def cc_read(timeout=10.0):
            eid = next((w["id"] for w in reversed(written)
                        if w.get("method") == "elicitation/create"), 1)
            return {"id": eid, "result": {"action": "accept", "content": {"label": "ok"}}}

        r = codex_run_v2({"prompt": "hi", "mcp": "isolated", "mode": "implement"},
                         manager=m, cc_write_fn=cc_write, cc_read_fn=cc_read)
        assert "error" not in r, r
        # The command approval (2nd elicitation) must still carry the narrative.
        cmd_elicits = [w for w in written if w.get("method") == "elicitation/create"
                       and "Command: vi config" in w["params"]["message"]]
        assert cmd_elicits, "command approval elicitation not found"
        assert "Codex explained:" in cmd_elicits[-1]["params"]["message"]
        assert "inspect the layout" in cmd_elicits[-1]["params"]["message"]
    finally:
        fc.kill()


# ── #247: opt-in approval-dialog localization via LiteLLM translation ──────
# Translate codex's reason + narrative into the user's language (default off) via the
# LiteLLM gateway; codex itself stays English. Fail-open, lazy key, batched, cached.

def _clear_tr_cache():
    import codex_server as cs
    cs._translate_cached.cache_clear()


def test_translate_off_when_no_lang(monkeypatch):
    import codex_server as cs
    monkeypatch.delenv("BULLDOZER_APPROVAL_LANG", raising=False)
    called = []
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: called.append(1) or '["x"]')
    assert cs._translate_texts(["hello"], cs._approval_lang()) == ["hello"]
    assert not called, "no HTTP when lang is off"


def test_translate_off_when_no_key(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.delenv("BULLDOZER_TRANSLATE_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    called = []
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: called.append(1) or '["x"]')
    assert cs._translate_texts(["hello"], "ru") == ["hello"]
    assert not called, "fail-open with no key, no HTTP"


def test_translate_success(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    monkeypatch.setattr(cs, "_translate_http",
                        lambda endpoint, model, key, prompt, timeout: '["Привет", "Мир"]')
    assert cs._translate_texts(["Hello", "World"], "ru") == ["Привет", "Мир"]


def test_translate_fail_open_on_error(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    def boom(*a, **k):
        raise TimeoutError("slow")
    monkeypatch.setattr(cs, "_translate_http", boom)
    assert cs._translate_texts(["Hello", "World"], "ru") == ["Hello", "World"]


def test_translate_fail_open_on_count_mismatch(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: '["только одна"]')
    assert cs._translate_texts(["Hello", "World"], "ru") == ["Hello", "World"]


def test_translate_handles_multiline_via_json(monkeypatch):
    """A multi-line narrative must round-trip (JSON batch, not line-numbering)."""
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    monkeypatch.setattr(cs, "_translate_http",
                        lambda *a, **k: '["строка один\\nстрока два"]')
    assert cs._translate_texts(["line one\nline two"], "ru") == ["строка один\nстрока два"]


def test_translate_lazy_key_read(monkeypatch):
    """Key resolved FRESH per real (cache-miss) call, never import-frozen."""
    import codex_server as cs
    _clear_tr_cache()
    seen = []
    monkeypatch.setattr(cs, "_translate_http",
                        lambda endpoint, model, key, prompt, timeout: seen.append(key) or '["да"]')
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "first")
    cs._translate_texts(["Yes"], "ru")
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "second")
    _clear_tr_cache()  # force a miss so the key is re-read
    cs._translate_texts(["Yes"], "ru")
    assert seen == ["first", "second"]


def test_translate_caches_identical(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    n = []
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: n.append(1) or '["кэш"]')
    cs._translate_texts(["Cache"], "ru")
    cs._translate_texts(["Cache"], "ru")
    assert len(n) == 1, "second identical request is a cache hit"


def test_dialog_labels_fallback_english():
    import codex_server as cs
    assert cs._dialog_labels(None)["reason"] == "Reason"
    assert cs._dialog_labels("")["reason"] == "Reason"
    assert cs._dialog_labels("xx")["reason"] == "Reason"   # unknown lang → EN labels
    assert cs._dialog_labels("ru")["reason"] == "Причина"


def test_command_message_translates_when_lang_set(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_APPROVAL_LANG", "ru")
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    monkeypatch.setattr(cs, "_translate_http",
                        lambda *a, **k: '["Удалить временный файл", "Я создам файл"]')
    params = {"command": "rm /tmp/x", "cwd": "/tmp", "reason": "Delete the temp file"}
    msg = cs._build_command_approval_message(params, narrative="I will create a file")
    assert "Удалить временный файл" in msg          # reason translated
    assert "Я создам файл" in msg                   # narrative translated
    assert "Запрос codex на одобрение" in msg       # localized header
    assert "Команда:" in msg and "Причина:" in msg  # localized labels
    assert "rm /tmp/x" in msg                       # command NEVER translated


def test_command_message_english_when_lang_unset(monkeypatch):
    """Back-compat: lang off → English, no HTTP."""
    import codex_server as cs
    monkeypatch.delenv("BULLDOZER_APPROVAL_LANG", raising=False)
    called = []
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: called.append(1) or '["x"]')
    params = {"command": "echo hi", "cwd": "/tmp", "reason": "because"}
    msg = cs._build_command_approval_message(params, narrative="doing it")
    assert "Codex approval request" in msg and "Reason: because" in msg
    assert "Codex explained: doing it" in msg
    assert not called, "no translation HTTP when lang is off"


def test_simple_message_translates_when_lang_set(monkeypatch):
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_APPROVAL_LANG", "ru")
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: '["Изменить файл конфигурации"]')
    msg = cs._build_simple_approval_message("filechange", "Edit the config file", None)
    assert "Изменить файл конфигурации" in msg
    assert "Codex: одобрение изменения файла" in msg  # localized header


def test_translate_failure_not_cached_retries_after_recovery(monkeypatch):
    """codex_review P3: a transient failure must NOT be cached — a later identical call retries
    (lru_cache must not memoize the failure)."""
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    calls = {"n": 0}
    def flaky(endpoint, model, key, prompt, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("transient")
        return '["восстановлено"]'
    monkeypatch.setattr(cs, "_translate_http", flaky)
    assert cs._translate_texts(["recover"], "ru") == ["recover"]        # 1st: fail-open
    assert cs._translate_texts(["recover"], "ru") == ["восстановлено"]  # 2nd: retried, not cached


def test_translate_never_raises_on_weird_http_return(monkeypatch):
    """Defensive: even a non-string _translate_http return must fail-open, never raise into
    the approval path."""
    import codex_server as cs
    _clear_tr_cache()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: {"not": "a string"})
    assert cs._translate_texts(["x"], "ru") == ["x"]


def test_translate_timeout_clamped(monkeypatch):
    import codex_server as cs
    monkeypatch.setenv("BULLDOZER_TRANSLATE_TIMEOUT", "9999")
    assert cs._translate_timeout() <= 10.0
    monkeypatch.setenv("BULLDOZER_TRANSLATE_TIMEOUT", "0.001")
    assert cs._translate_timeout() >= 0.5
    monkeypatch.setenv("BULLDOZER_TRANSLATE_TIMEOUT", "garbage")
    assert cs._translate_timeout() == 2.5


def test_dialog_labels_normalizes_region():
    import codex_server as cs
    assert cs._dialog_labels("ru-RU")["reason"] == "Причина"
    assert cs._dialog_labels("RU")["reason"] == "Причина"
    assert cs._dialog_labels("ru_RU")["reason"] == "Причина"


def test_extract_json_array_tolerates_preamble():
    import codex_server as cs
    assert cs._extract_json_array('Sure, here: ["a", "b"]') == ["a", "b"]
    assert cs._extract_json_array('["a", "b"]') == ["a", "b"]
    assert cs._extract_json_array('```json\n["a"]\n```') == ["a"]


def test_simple_message_safe_on_unknown_kind(monkeypatch):
    """Review: an unexpected `kind` must NOT KeyError into the approval path — degrade to a
    plain header (every other path is fail-open; this one must be too)."""
    import codex_server as cs
    monkeypatch.delenv("BULLDOZER_APPROVAL_LANG", raising=False)
    msg = cs._build_simple_approval_message("bogus_kind", "do it", None)  # must not raise
    assert "Reason: do it" in msg


def test_translate_failure_is_logged(monkeypatch, tmp_path):
    """Review: fail-open is silent → write a best-effort diagnostic line on failure so an
    operator can tell WHY localization isn't working."""
    import codex_server as cs
    cs._translate_cached.cache_clear()
    logf = tmp_path / "codex.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    def boom(*a, **k):
        raise TimeoutError("slow")
    monkeypatch.setattr(cs, "_translate_http", boom)
    assert cs._translate_texts(["x"], "ru") == ["x"]   # fail-open
    assert "TRANSLATE_FAILED" in logf.read_text()


def test_translate_cache_ignores_timeout_change(monkeypatch):
    """Review: timeout is transport tuning, not a translation determinant → excluded from the
    cache key. Changing it between identical calls must still hit cache (no re-call)."""
    import codex_server as cs
    cs._translate_cached.cache_clear()
    monkeypatch.setenv("BULLDOZER_TRANSLATE_API_KEY", "k")
    n = []
    monkeypatch.setattr(cs, "_translate_http", lambda *a, **k: n.append(1) or '["кэш"]')
    monkeypatch.setenv("BULLDOZER_TRANSLATE_TIMEOUT", "2")
    cs._translate_texts(["Cache"], "ru")
    monkeypatch.setenv("BULLDOZER_TRANSLATE_TIMEOUT", "8")
    cs._translate_texts(["Cache"], "ru")
    assert len(n) == 1   # cache hit despite timeout change (timeout not in cache key)


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


def test_elicitation_reply_id_correlation_answers_unrelated_request():
    """bridge_approval correlates the elicitation reply by id, and (#269) ANSWERS an unrelated
    id-bearing CC request (e.g. a `ping`) that arrives first — while the real reply (id == eid)
    is still honored.

    Pre-#269 the unrelated frame was silently skipped; #269 routes it through _route_cc_frame so
    CC doesn't block on its request. The id correlation of the real reply is unchanged.
    """
    from codex_server import handle_server_request

    cc_written: list = []

    def cc_write(msg: dict):
        assert msg.get("jsonrpc") == "2.0"
        cc_written.append(msg)

    def _eid():
        # the elicitation/create frame's id — NOT cc_written[-1] (a #269 pong is written too)
        return next(f["id"] for f in cc_written if f.get("method") == "elicitation/create")

    # Deliver: (1) an unrelated id-bearing request (a real ping: method + id, NO result),
    # then (2) the real elicitation reply (id == eid, action=accept).
    call_count = [0]

    def cc_read(timeout=10.0):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"jsonrpc": "2.0", "id": _eid() + 1000, "method": "ping"}
        return {"jsonrpc": "2.0", "id": _eid(), "result": {"action": "accept",
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
    # Real accept reply honored (id-correlated) → decision is "accept"
    assert resp["result"]["decision"] == "accept"
    assert call_count[0] == 2  # 1 unrelated (answered), 1 real
    # #269: the unrelated ping was ANSWERED with a {} pong, not dropped
    pongs = [f for f in cc_written if f.get("id") == _eid() + 1000 and f.get("result") == {}]
    assert pongs, f"unrelated ping not answered — written: {cc_written}"


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

        if method == "review/start":
            tid = params.get("threadId", "T1")
            turn_id = "RTURN1"
            self._write_msg({"id": mid, "result": {
                "turn": {"id": turn_id, "items": [], "status": "running"},
                "reviewThreadId": tid}})
            # Review output is a COMPLETED agentMessage item (not deltas).
            self._write_msg({"method": "item/completed", "params": {
                "item": {"id": "RI1", "type": "agentMessage",
                         "text": "REVIEW: minus should be plus"},
                "threadId": tid, "turnId": turn_id}})
            self._write_msg({"method": "turn/completed", "params": {
                "threadId": tid,
                "turn": {"id": turn_id, "items": [], "itemsView": "loaded",
                         "status": "completed", "error": None,
                         "startedAt": 0, "completedAt": 0, "durationMs": 10}}})
            return

        if method == "turn/start":
            # Record params for assertion
            self.turn_start_params = params
            turn_id = "TURN1"
            item_id = "ITEM1"
            thread_id = params.get("threadId", "T1")
            if self._turn_variant == "pre_ack_terminal_error":
                # Emit a TERMINAL error BEFORE the ACK (no TurnStartResponse) — tests #4:
                # a pre-ACK terminal error must be surfaced, not masked as an ACK timeout.
                self._write_msg({"method": "error", "params": {
                    "error": {"message": "model unavailable"}, "willRetry": False,
                    "threadId": thread_id, "turnId": turn_id}})
                return
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
            if self._turn_variant == "with_usage":
                # Real wire shape: params.tokenUsage = {last, total}, camelCase breakdown (spec 2a).
                _bd = {"inputTokens": 100, "cachedInputTokens": 0, "outputTokens": 23,
                       "reasoningOutputTokens": 0, "totalTokens": 123}
                self._write_msg({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "tokenUsage": {"last": _bd, "total": _bd},
                }})
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
            if self._turn_variant == "transient_error":
                # transient stream reconnect (willRetry) → must NOT drift; turn completes
                self._write_msg({"method": "error", "params": {
                    "error": {"message": "Reconnecting... 2/5"}, "willRetry": True,
                    "threadId": thread_id, "turnId": turn_id}})
                self._write_msg({"method": "item/agentMessage/delta", "params": {
                    "delta": self._final_message, "threadId": thread_id,
                    "turnId": turn_id, "itemId": item_id}})
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [], "itemsView": "loaded",
                             "status": "completed", "error": None,
                             "startedAt": 0, "completedAt": 0, "durationMs": 10}}})
                return
            if self._turn_variant == "terminal_error":
                # non-retry error → surface as a structured failure, NOT UNKNOWN_NOTIFICATION
                self._write_msg({"method": "error", "params": {
                    "error": {"message": "fatal boom"}, "willRetry": False,
                    "threadId": thread_id, "turnId": turn_id}})
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
                   base_instructions=None, developer_instructions=None, config=None,
                   mcp="isolated", timeout=None):
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
    args["mcp"] = mcp
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
    if timeout is not None:
        args["timeout"] = timeout

    return codex_run_v2(args, manager=manager, cc_write_fn=cc_write, cc_read_fn=cc_read)


# ── Task 6 integration: #218 turn-pump interrupt (InterruptFakeChild + sys.stdin CC) ──

class InterruptFakeChild(ExtendedFakeChild):
    """#218 integration child: ACKs a turn (with turn.id) + one delta, then does NOT
    auto-complete — it completes ONLY on turn/interrupt (→ status=interrupted). Variants:
    'wait' (default), 'no_ack' (never ACK), 'review_no_turnid' (review/start ACK w/o turn.id)."""
    def __init__(self, variant="wait"):
        super().__init__()
        self._variant = variant

    def _dispatch(self, msg):
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "turn/start":
            self.turn_start_params = params
            if self._variant == "no_ack":
                return                                       # never ACK (pre-ACK / ack-timeout tests)
            tid = params.get("threadId", "T1")
            self._write_msg({"id": mid, "result": {"turn": {
                "id": "TURN1", "items": [], "status": "running", "error": None}}})
            self._write_msg({"method": "item/agentMessage/delta",
                             "params": {"delta": "partial", "threadId": tid, "turnId": "TURN1"}})
            return                                           # then WAIT (no turn/completed)
        if method == "review/start":
            if self._variant == "review_no_turnid":
                self._write_msg({"id": mid, "result": {
                    "reviewThreadId": params.get("threadId", "T1")}})   # ACK without turn.id
                return
            super()._dispatch(msg)
            return
        if method == "turn/interrupt":
            self._write_msg({"id": mid, "result": {}})       # empty {} response
            self._write_msg({"method": "turn/completed", "params": {
                "threadId": params.get("threadId", "T1"),
                "turn": {"id": params.get("turnId", "TURN1"), "items": [],
                         "status": "interrupted", "error": None}}})
            return
        super()._dispatch(msg)


def _drive_interrupt(monkeypatch, fc, *, cc_frames=(), close_stdin=False, timeout=None,
                     ack_timeout=None, mode="implement", review=False, omit_cc_id=False):
    """Drive codex_run_v2 with a controllable sys.stdin (CC side) + a scripted child.
    Keeps the stdin write end OPEN (no spurious EOF) unless close_stdin=True."""
    import json
    import codex_server as cs
    if ack_timeout is not None:
        monkeypatch.setattr(cs, "_ACK_TIMEOUT", ack_timeout)
    r, w = os.pipe()
    monkeypatch.setattr(cs.sys, "stdin", os.fdopen(r))
    for fr in cc_frames:
        os.write(w, (json.dumps(fr) + "\n").encode())
    if close_stdin:
        os.close(w)
    m = cs.AppServerManager(bin=fc)
    sm = cs.TurnStateMachine()
    args = {"prompt": "hi", "mode": mode, "mcp": "isolated"}
    if not omit_cc_id:
        args["_cc_id"] = "CCID"
    if timeout is not None:
        args["timeout"] = timeout
    if review:
        args["_review_target"] = {"type": "uncommitted"}
    res = cs.codex_run_v2(args, manager=m, cc_write_fn=cs.reply,
                          cc_read_fn=lambda timeout=10: None, state_machine=sm)
    if not close_stdin:
        try:
            os.close(w)
        except OSError:
            pass
    return res, sm


class _NonDictFrameChild(ExtendedFakeChild):
    """Emits a bare JSON scalar (`42`) — a NON-dict frame — between the ACK and turn/completed,
    to exercise the non-dict-frame guards in the turn loop / EOF scan (reviewer1 F3)."""
    def _dispatch(self, msg):
        if msg.get("method") == "turn/start":
            self.turn_start_params = msg.get("params") or {}
            mid = msg.get("id")
            tid = (msg.get("params") or {}).get("threadId", "T1")
            self._write_msg({"id": mid, "result": {"turn": {"id": "TURN1", "items": [], "status": "running"}}})
            self._srv_stdout.write(b"42\n"); self._srv_stdout.flush()   # bare scalar (non-dict) frame
            self._write_msg({"method": "item/agentMessage/delta",
                             "params": {"delta": self._final_message, "threadId": tid, "turnId": "TURN1"}})
            self._write_msg({"method": "turn/completed", "params": {"threadId": tid,
                             "turn": {"id": "TURN1", "items": [], "status": "completed", "error": None}}})
            return
        super()._dispatch(msg)


_CANCEL = {"method": "notifications/cancelled", "params": {"requestId": "CCID"}}


def test_turn_midstream_cancel_interrupts_with_partial(monkeypatch):
    fc = InterruptFakeChild("wait")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, cc_frames=[_CANCEL], timeout=8)
    finally:
        fc.kill()
    assert res["status"] == "interrupted"
    assert res["interrupted_by"] == "cancel"
    assert res["partial_text"] == "partial"
    assert "error" not in res
    assert sm.is_busy() is False                              # state cleared


def test_turn_optin_timeout_returns_graceful(monkeypatch):
    monkeypatch.delenv("BULLDOZER_CODEX_NO_INTERRUPT", raising=False)
    fc = InterruptFakeChild("wait")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, timeout=0.6)   # ACKs, never completes → opt-in timeout
    finally:
        fc.kill()
    assert res["status"] == "interrupted" and res["interrupted_by"] == "timeout"
    assert sm.is_busy() is False


def test_turn_no_cc_id_completes_normally(monkeypatch):
    """R5-F1: without _cc_id, watch=False → no stdin read → a normal turn completes."""
    monkeypatch.delenv("BULLDOZER_CODEX_NO_INTERRUPT", raising=False)
    fc = ExtendedFakeChild()
    fc.script_final_message("ok done")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, omit_cc_id=True)
    finally:
        fc.kill()
    assert res.get("status") != "interrupted"
    assert res.get("result") == "ok done"


def test_turn_pre_ack_cancel_no_ack_tears_down_cold(monkeypatch):
    """Cancel pre-ACK + ACK never arrives → graceful COLD teardown (R1-F2)."""
    fc = InterruptFakeChild("no_ack")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, cc_frames=[_CANCEL], ack_timeout=0.6, timeout=8)
    finally:
        fc.kill()
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    assert sm.is_busy() is False


def test_turn_stdin_eof_tears_down(monkeypatch):
    """CC stdin EOF mid-turn → FORCED cold teardown, no turn/interrupt sent (R1-F1)."""
    fc = InterruptFakeChild("wait")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, close_stdin=True, timeout=8)
    finally:
        fc.kill()
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    methods = [m.get("method") for m in fc.received_msgs]
    assert "turn/interrupt" not in methods                    # EOF teardown sends NO interrupt
    assert sm.is_busy() is False


def test_turn_review_missing_turn_id_interrupt_cold(monkeypatch):
    """review/start ACK without turn.id, then cancel → cold teardown + not busy (R1-F2/R4-F1)."""
    fc = InterruptFakeChild("review_no_turnid")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, cc_frames=[_CANCEL], review=True,
                                   mode="review", timeout=8)
    finally:
        fc.kill()
    assert res["status"] == "interrupted" and res["thread_warm"] is False
    assert sm.is_busy() is False


# ── Task 7: #252 approval-wait child drain + cancel/EOF/terminal during approval ──

def _drain_ctx(reactor, ts, cc_id="CCID"):
    return {"reactor": reactor, "ts": ts, "cc_id": cc_id}


_CMD_APPROVAL = {"threadId": "T1", "turnId": "TURN1", "itemId": "I1",
                 "startedAtMs": 1, "command": "echo hi", "cwd": "/tmp"}


def test_approval_wait_drains_child_no_deadlock_keeps_deltas():
    """#252: a child delta arriving DURING the approval wait is drained + accumulated (no
    deadlock), and the approval reply still resolves."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([[{"method": "item/agentMessage/delta",
                                  "params": {"delta": "X" * 5000}}]])
    cc = FakeCC(); cc.set_answer("accept", None)
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), cc.write, cc.read,
        drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "accept"                                  # reply resolved (no deadlock)
    assert "".join(ts["final_message_parts"]) == "X" * 5000      # drained delta accumulated


def test_cancel_during_approval_sets_flag_and_declines():
    """A cancel (our cc_id) during the approval wait → ts['cancel_during_approval']=True and the
    per-method decline (the existing read_correlated-None branch) is returned (F2)."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([])
    cancel = {"method": "notifications/cancelled", "params": {"requestId": "CCID"}}
    state = {"n": 0}
    def cc_read(timeout=10.0):
        state["n"] += 1
        return cancel if state["n"] == 1 else None
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), lambda m: None, cc_read,
        timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "decline"
    assert ts.get("cancel_during_approval") is True


def test_eof_during_approval_sets_flag_and_declines():
    """CC stdin EOF (cc_read → _CC_EOF) during the approval wait → ts['eof_during_approval']=True
    and a per-method decline (R6-F1)."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([])
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), lambda m: None,
        lambda timeout=10.0: cs._CC_EOF, timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "decline"
    assert ts.get("eof_during_approval") is True


def test_terminal_error_during_approval_sets_flag_and_declines():
    """A terminal child error during the approval wait → ts['terminal_during_approval'] holds the
    result, and a per-method decline is returned (R1-F5)."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([[{"method": "error", "params": {"error": {"message": "boom"}}}]])
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), lambda m: None,
        lambda timeout=10.0: None, timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "decline"
    assert ts.get("terminal_during_approval") is not None
    assert "boom" in ts["terminal_during_approval"]["error"]


def test_id_bearing_ping_during_approval_is_answered():
    """#269: an id-bearing CC request (e.g. `ping` keepalive) arriving during the approval wait
    MUST be answered (`{}` pong), not dropped — else CC blocks on it and may tear down the
    session. The turn-pump path already answers id-bearing requests; the approval wait must too.
    RED until read_correlated routes the otherwise-skipped frame through _route_cc_frame."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([])
    sent = []
    def cc_write(frame):
        sent.append(frame)
    ping = {"jsonrpc": "2.0", "id": 4242, "method": "ping"}
    state = {"n": 0}
    def cc_read(timeout=10.0):
        state["n"] += 1
        return ping if state["n"] == 1 else None      # ping once, then nothing → wait times out
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), cc_write, cc_read,
        timeout=1.0, drain_ctx=_drain_ctx(reactor, ts))
    pongs = [f for f in sent if f.get("id") == 4242 and f.get("result") == {}]
    assert pongs, f"ping not answered (dropped) — sent frames: {sent}"
    assert decision == "decline"                       # ping did NOT falsely resolve the elicitation


def test_id_bearing_toolslist_during_approval_is_answered():
    """#269: a `tools/list` request mid-approval is answered with the tools (not dropped)."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([])
    sent = []
    def cc_write(frame):
        sent.append(frame)
    req = {"jsonrpc": "2.0", "id": 77, "method": "tools/list"}
    state = {"n": 0}
    def cc_read(timeout=10.0):
        state["n"] += 1
        return req if state["n"] == 1 else None
    cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), cc_write, cc_read,
        timeout=1.0, drain_ctx=_drain_ctx(reactor, ts))
    answers = [f for f in sent if f.get("id") == 77 and isinstance(f.get("result"), dict)
               and "tools" in f["result"]]
    assert answers, f"tools/list not answered — sent frames: {sent}"


def test_cancel_during_approval_ignored_under_killswitch(monkeypatch):
    """Kill-switch set → a cancel during approval is IGNORED (no flag), but the drain still runs
    and the reply resolves (F8 — #252 drain is independent of the interrupt kill-switch)."""
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([[{"method": "item/agentMessage/delta", "params": {"delta": "Y" * 5000}}]])
    cc = FakeCC(); cc.set_answer("accept", None)
    cancel = {"method": "notifications/cancelled", "params": {"requestId": "CCID"}}
    state = {"n": 0}
    def cc_read(timeout=10.0):
        state["n"] += 1
        return cancel if state["n"] == 1 else cc.read(timeout)
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), cc.write, cc_read,
        timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "accept"                              # cancel ignored, reply resolves
    assert ts.get("cancel_during_approval") is not True      # no interrupt flag under kill-switch
    assert "".join(ts["final_message_parts"]) == "Y" * 5000  # but the drain still ran


# ── Dogfood fixes (codex_review + reviewers): approval-drain hardening ──

def test_approval_drain_buffers_non_notification_frames():
    """codex P1: a response/request frame (e.g. a turn/start ACK) drained during an approval is
    BUFFERED in ts['drained_frames'] (not dropped via _handle_child_frame), so the turn loop can
    still process it — otherwise a pre-ACK approval would falsely time out."""
    import codex_server as cs
    ts = _mk_ts()
    ack = {"id": 99, "result": {"turn": {"id": "TURN1", "status": "running"}}}
    reactor = _ScriptedReactor([[ack]])
    cc = FakeCC(); cc.set_answer("accept", None)
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), cc.write, cc.read,
        timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "accept"
    assert ts.get("drained_frames") == [ack]                 # ACK buffered, NOT dropped


def test_approval_drain_eof_beats_same_iteration_terminal():
    """codex P2: a terminal child frame + CC EOF in the SAME drain iteration → EOF wins
    (eof_during_approval set; terminal NOT surfaced — it would be undeliverable to a closed CC)."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([[{"method": "error", "params": {"error": {"message": "boom"}}}]])
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), lambda m: None,
        lambda timeout=10.0: cs._CC_EOF, timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "decline"
    assert ts.get("eof_during_approval") is True
    assert ts.get("terminal_during_approval") is None        # EOF won; terminal held, not surfaced


def test_approval_drain_terminal_surfaced_when_no_eof():
    """The held terminal IS surfaced when the CC side has no EOF/reply that iteration (R1-F5)."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([[{"method": "error", "params": {"error": {"message": "boom"}}}]])
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), lambda m: None,
        lambda timeout=10.0: None, timeout=2.0, drain_ctx=_drain_ctx(reactor, ts))
    assert decision == "decline"
    assert ts.get("terminal_during_approval") is not None
    assert "boom" in ts["terminal_during_approval"]["error"]


def test_approval_drain_cancel_requires_cc_id():
    """reviewer2 obs1: with cc_id=None, a cancel-without-requestId must NOT false-trigger
    the cancel flag via `None == None`."""
    import codex_server as cs
    ts = _mk_ts()
    reactor = _ScriptedReactor([])
    bad_cancel = {"method": "notifications/cancelled", "params": {}}   # no requestId
    state = {"n": 0}
    def cc_read(timeout=10.0):
        state["n"] += 1
        return bad_cancel if state["n"] == 1 else None
    decision = cs._bridge_approval_dispatch(
        "item/commandExecution/requestApproval", dict(_CMD_APPROVAL), lambda m: None, cc_read,
        timeout=1.0, drain_ctx=_drain_ctx(reactor, ts, cc_id=None))
    assert decision == "decline"                             # timed out (cancel skipped)
    assert ts.get("cancel_during_approval") is not True


def test_turn_loop_tolerates_non_dict_child_frame(monkeypatch):
    """reviewer1 F3: a non-dict child frame (bare JSON scalar) in a pump batch must not crash
    the EOF scan / __cc__ check — the turn completes normally."""
    fc = _NonDictFrameChild()
    fc.script_final_message("ok")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc)
    finally:
        fc.kill()
    assert "error" not in res                                # no crash / no turn-execution-error
    assert res.get("status") != "interrupted"


# ── Task 8: kill-switch matrix + log-once (F8) ──────────────────────────────

def test_kill_switch_disables_cancel_interrupt(monkeypatch):
    """Kill-switch set → watch=False → a mid-turn cancel is never read → the turn completes
    normally (no interrupt). The #252 drain is unaffected (tested separately)."""
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    fc = ExtendedFakeChild()
    fc.script_final_message("z")
    try:
        res, sm = _drive_interrupt(monkeypatch, fc, cc_frames=[_CANCEL])
    finally:
        fc.kill()
    assert res.get("status") != "interrupted"
    assert res.get("result") == "z"


def test_kill_switch_logs_once(monkeypatch, tmp_path):
    """The kill-switch is logged ONCE per process to the stable codex log; no-op when enabled."""
    import codex_server as cs
    logf = tmp_path / "codex.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    if hasattr(cs._log_kill_switch_once, "_done"):
        delattr(cs._log_kill_switch_once, "_done")
    # enabled → no-op
    monkeypatch.delenv("BULLDOZER_CODEX_NO_INTERRUPT", raising=False)
    cs._log_kill_switch_once()
    assert not logf.exists() or "INTERRUPT_DISABLED" not in logf.read_text()
    # disabled → logs exactly once
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    cs._log_kill_switch_once()
    cs._log_kill_switch_once()
    assert logf.read_text().count("INTERRUPT_DISABLED") == 1


# ── Surface additions (2026-06-21): config-doc / codex_info / codex_review ──

def test_config_param_documents_passthrough_keys():
    """Q1: the config param MUST document the passthrough-reachable keys so CC knows
    what it can pass (spec 2026-06-21 Item 1; #204 §95 required this, never shipped)."""
    import codex_server
    tool = next(t for t in codex_server.TOOLS if t["name"] == "codex_run")
    desc = (tool["inputSchema"]["properties"]["config"].get("description") or "").lower()
    for key in ("web_search", "review_model", "model_verbosity", "model_reasoning_summary"):
        assert key in desc, f"config param description must mention {key!r}; got: {desc!r}"


class InfoFakeChild(FakeChild):
    """FakeChild that answers the connection-level read methods (codex_info tests)."""
    _CANNED = {
        "model/list": {"data": [{"id": "gpt-5.5"}], "nextCursor": None},
        "getAuthStatus": {"authMethod": "chatgpt", "requiresOpenaiAuth": True},
        "config/read": {
            "config": {"model": "gpt-5.5", "web_search": None, "approvals_reviewer": "user",
                       "projects": {"big": "x" * 200}, "tui": {"theme": "dark"},
                       "marketplaces": {"a": 1}},
            "origins": {"model": "x" * 500},
        },
        "account/rateLimits/read": {"rateLimits": {"primary": {"usedPercent": 4}}},
        "account/usage/read": {"summary": {"lifetimeTokens": 1}},
        "mcpServerStatus/list": {"data": [{"name": "dash"}], "nextCursor": None},
        "experimentalFeature/list": {"data": [{"name": "shell_tool"}], "nextCursor": None},
        "permissionProfile/list": {"data": [{"id": ":read-only"}], "nextCursor": None},
    }

    def _dispatch(self, msg):
        method = msg.get("method")
        if method in self._CANNED:
            self._write_msg({"id": msg.get("id"), "result": self._CANNED[method]})
            return
        super()._dispatch(msg)


def test_codex_info_tool_registered():
    import codex_server
    tool = next((t for t in codex_server.TOOLS if t["name"] == "codex_info"), None)
    assert tool is not None, "codex_info tool must be registered"
    enum = set(tool["inputSchema"]["properties"]["query"].get("enum") or [])
    assert enum == {"models", "auth", "config", "limits", "usage",
                    "servers", "features", "profiles"}


def test_codex_info_maps_query_to_method():
    from codex_server import codex_info_v2, AppServerManager
    fake = InfoFakeChild()
    r = codex_info_v2({"query": "models"}, manager=AppServerManager(bin=fake))
    assert r["query"] == "models"
    assert r["result"]["data"][0]["id"] == "gpt-5.5"
    assert fake.received("model/list") is not None, "must send model/list to the app-server"
    fake.kill()


def test_codex_info_paramless_query_maps_to_method():
    """limits/usage take NO params (undefined on the wire) — still routed correctly."""
    from codex_server import codex_info_v2, AppServerManager
    fake = InfoFakeChild()
    r = codex_info_v2({"query": "limits"}, manager=AppServerManager(bin=fake))
    assert r["result"]["rateLimits"]["primary"]["usedPercent"] == 4
    sent = fake.received("account/rateLimits/read")
    assert sent is not None and "params" not in sent, "paramless query must omit params"
    fake.kill()


def test_codex_info_config_is_compact_projection():
    """query='config' must return a WHITELIST projection of operational knobs + an
    `omitted` list of the other top-level config keys (so new codex keys are visible,
    not silent), and DROP the huge `origins` map. Avoids the 71K token blowout
    (consult MINOR-FIXES, 2026-06-21)."""
    from codex_server import codex_info_v2, AppServerManager
    fake = InfoFakeChild()
    r = codex_info_v2({"query": "config"}, manager=AppServerManager(bin=fake))
    res = r["result"]
    assert "origins" not in res, "origins map must be dropped"
    cfg = res["config"]
    # whitelisted operational knobs kept
    assert cfg.get("model") == "gpt-5.5" and "web_search" in cfg and "approvals_reviewer" in cfg
    # bulky non-operational sections dropped from config
    assert "projects" not in cfg and "tui" not in cfg and "marketplaces" not in cfg
    # but visible in `omitted` (hole closed: new keys are not silently hidden)
    assert set(["projects", "tui", "marketplaces"]).issubset(set(res["omitted"]))
    fake.kill()


def test_codex_info_unknown_query_errors():
    from codex_server import codex_info_v2, AppServerManager
    fake = InfoFakeChild()
    r = codex_info_v2({"query": "bogus"}, manager=AppServerManager(bin=fake))
    assert "error" in r and "bogus" in r["error"]
    fake.kill()


def test_transient_error_notification_does_not_drift(ext_child):
    """A `willRetry:true` error (e.g. 'Reconnecting N/5') is a transient stream
    reconnect — codex retries on its own. It must NOT produce _drift and the turn
    must still complete normally (spec 2026-06-21 Item 4)."""
    r = call_codex_run(ext_child, "p", mode="implement", turn_variant="transient_error")
    assert "error" not in r, r
    assert r["result"] == "fake result", "turn must complete normally after a transient retry"
    drift_codes = {d["code"] for d in r.get("_drift", [])}
    assert "UNKNOWN_NOTIFICATION" not in drift_codes, \
        f"transient willRetry error must NOT drift; got: {r.get('_drift')}"


def test_terminal_error_notification_is_surfaced(ext_child):
    """A non-retry error must be surfaced as a structured failure (the #204
    parking-lot signal), NOT routed to UNKNOWN_NOTIFICATION drift."""
    r = call_codex_run(ext_child, "p", mode="implement", turn_variant="terminal_error", timeout=5)
    assert "error" in r and "fatal boom" in r["error"], r
    drift_codes = {d["code"] for d in r.get("_drift", [])}
    assert "UNKNOWN_NOTIFICATION" not in drift_codes, \
        "terminal error must be surfaced, not UNKNOWN_NOTIFICATION"


def test_codex_review_tool_registered():
    import codex_server
    tool = next((t for t in codex_server.TOOLS if t["name"] == "codex_review"), None)
    assert tool is not None, "codex_review tool must be registered"
    assert "target" in tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["required"] == ["mcp"]


def test_parse_review_target_variants():
    from codex_server import _parse_review_target
    assert _parse_review_target("uncommitted") == {"type": "uncommittedChanges"}
    assert _parse_review_target("branch:main") == {"type": "baseBranch", "branch": "main"}
    assert _parse_review_target("commit:abc123")["sha"] == "abc123"
    assert _parse_review_target("custom:check the auth")["instructions"] == "check the auth"
    assert _parse_review_target("bogus:x") is None


def test_codex_review_routes_effort_and_model_via_thread_config(ext_child):
    """#12: review/start carries NO effort/model, and start_thread sends neither, so
    codex_review must route effort→config.model_reasoning_effort and model→config.model
    (the thread-level config that start_thread DOES send) — otherwise both are silently
    ignored and the review always runs at the codex config default."""
    from codex_server import codex_review_v2, AppServerManager
    codex_review_v2(
        {"target": "uncommitted", "mcp": "isolated", "cwd": "/tmp",
         "effort": "xhigh", "model": "gpt-5.5"},
        manager=AppServerManager(bin=ext_child),
        cc_write_fn=lambda m: None, cc_read_fn=lambda timeout=10.0: None,
    )
    sent = ext_child.received("thread/start")
    assert sent is not None, "thread/start must be sent"
    cfg = sent["params"]["config"]
    assert cfg.get("model_reasoning_effort") == "xhigh", f"effort must reach thread config; got {cfg}"
    assert cfg.get("model") == "gpt-5.5", f"model must reach thread config; got {cfg}"


def test_codex_review_routes_model_to_review_model(ext_child):
    """#225 P2: codex's native review (review/start) prefers the review-specific
    `review_model` config over the thread `model`. Routing the public `model` arg only
    to config.model means it is silently ignored whenever `review_model` is configured.
    codex_review must ALSO set config.review_model so `model` is authoritative for native
    reviews."""
    from codex_server import codex_review_v2, AppServerManager
    codex_review_v2(
        {"target": "uncommitted", "mcp": "isolated", "cwd": "/tmp", "model": "gpt-5.5"},
        manager=AppServerManager(bin=ext_child),
        cc_write_fn=lambda m: None, cc_read_fn=lambda timeout=10.0: None,
    )
    cfg = ext_child.received("thread/start")["params"]["config"]
    assert cfg.get("model") == "gpt-5.5", f"model must reach thread config; got {cfg}"
    assert cfg.get("review_model") == "gpt-5.5", \
        f"model must ALSO route to review_model (native review prefers it); got {cfg}"


def _review_cfg(ext_child, args):
    from codex_server import codex_review_v2, AppServerManager
    codex_review_v2({**args, "target": "uncommitted", "mcp": "isolated", "cwd": "/tmp"},
                    manager=AppServerManager(bin=ext_child),
                    cc_write_fn=lambda m: None, cc_read_fn=lambda timeout=10.0: None)
    return ext_child.received("thread/start")["params"]["config"]


def test_codex_review_model_arg_overrides_config_model(ext_child):
    """#226 A (model authoritative): model arg + caller config.model must NOT diverge — the
    arg wins for BOTH model and review_model so native review uses the requested model."""
    cfg = _review_cfg(ext_child, {"model": "A", "config": {"model": "B"}})
    assert cfg.get("model") == "A", f"model arg must win over config.model; got {cfg}"
    assert cfg.get("review_model") == "A", f"review_model must match the authoritative model; got {cfg}"


def test_codex_review_config_model_only_sets_review_model(ext_child):
    """#226 A: a caller who passes ONLY config.model (no model arg) still gets review_model
    set to that effective model — closes the config-only hole (native review would otherwise
    fall back to the user's configured review_model)."""
    cfg = _review_cfg(ext_child, {"config": {"model": "X"}})
    assert cfg.get("model") == "X", cfg
    assert cfg.get("review_model") == "X", f"config.model must propagate to review_model; got {cfg}"


def test_codex_review_model_arg_overrides_explicit_review_model(ext_child):
    """#226 A: model arg is authoritative even over an explicit caller config.review_model —
    no divergence (the chosen semantics: arg wins)."""
    cfg = _review_cfg(ext_child, {"model": "A", "config": {"review_model": "R"}})
    assert cfg.get("review_model") == "A", f"model arg must override explicit review_model; got {cfg}"
    assert cfg.get("model") == "A", cfg


def test_codex_review_collects_findings_from_item_completed(ext_child):
    """codex_review starts via review/start and collects findings from the completed
    agentMessage item (review output is NOT streamed as deltas)."""
    from codex_server import codex_review_v2, AppServerManager
    r = codex_review_v2(
        {"target": "uncommitted", "mcp": "isolated", "cwd": "/tmp"},
        manager=AppServerManager(bin=ext_child),
        cc_write_fn=lambda m: None, cc_read_fn=lambda timeout=10.0: None,
    )
    assert "error" not in r, r
    assert "REVIEW: minus should be plus" in r["review"], r
    assert "result" not in r, "implement-shape `result` must be renamed to `review`"
    assert ext_child.received("review/start") is not None, "must send review/start"


def test_codex_review_invalid_target_errors(ext_child):
    from codex_server import codex_review_v2, AppServerManager
    r = codex_review_v2({"target": "bogus:x", "mcp": "isolated"},
                        manager=AppServerManager(bin=ext_child))
    assert "error" in r and "target" in r["error"]


def test_pre_ack_terminal_error_is_surfaced(ext_child):
    """#4: a terminal error arriving BEFORE the start ACK must surface as the codex error,
    not be silently dropped and masked as a generic 'response timed out'."""
    r = call_codex_run(ext_child, "p", mode="implement",
                       turn_variant="pre_ack_terminal_error", timeout=5)
    assert "error" in r and "model unavailable" in r["error"], r
    assert "timed out" not in r["error"], f"must not mask the real error as an ACK timeout: {r}"


def test_project_config_failclosed_on_bad_shape():
    """#6: _project_config must NOT return raw config/read on an unexpected shape (that
    re-introduces the ~71K origins blowout) — fail closed to a small marker."""
    from codex_server import _project_config
    raw = {"origins": {"x": "y" * 1000}}   # no "config" key → unexpected shape
    out = _project_config(raw)
    assert out.get("config") == {} and "origins" not in out, out
    assert "note" in out


def test_codex_info_no_codex_binary_guard(monkeypatch):
    """#16: codex_info must use the same filesystem check as codex_run — `CODEX` is a path
    string that's always truthy, so the old `if not CODEX` guard never fired."""
    import codex_server as cs
    # Isolate the module singleton: with the #225 P3 reorder, the manager=None path now
    # calls _get_manager() before the binary check, which would otherwise cache a manager
    # bound to this fake CODEX and leak it (bin=/nonexistent) into later tests (e.g. the
    # slow live reads). monkeypatch restores _v2_manager after the test.
    monkeypatch.setattr(cs, "_v2_manager", None)
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: "/nonexistent/codex-bin-xyz")
    r = cs.codex_info_v2({"query": "models"})   # manager=None → guard path
    assert "error" in r and "not found" in r["error"], r


def test_codex_info_reuses_live_child_when_binary_missing(monkeypatch):
    """#225 P3: codex_info is documented to reuse a live app-server child without a
    cold start. The no-codex binary guard must therefore fire ONLY when a fresh spawn is
    actually needed (no live child) — not unconditionally. Otherwise a removed/broken
    codex symlink mid-session (e.g. an upgrade) wrongly rejects a connection-level read
    that a warm child from a prior codex_run could still answer. Exercises the real
    dispatch path (manager=None → _get_manager singleton with a live child)."""
    import codex_server as cs
    fake = InfoFakeChild()
    mgr = cs.AppServerManager(bin=fake)
    mgr.ensure([])                                   # bring a (fake) child alive
    assert cs._is_child_alive(mgr._child)
    monkeypatch.setattr(cs, "_v2_manager", mgr)      # dispatch singleton has a live child
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: "/nonexistent/codex-bin-xyz")  # binary gone mid-session
    r = cs.codex_info_v2({"query": "models"})        # manager=None → real guard path
    assert "error" not in r, f"must reuse live child, not reject on missing binary: {r}"
    assert r["result"]["data"][0]["id"] == "gpt-5.5"
    fake.kill()


def test_codex_info_explicit_manager_not_blocked_by_global_codex(monkeypatch):
    """#226 D: with an EXPLICIT manager (its own _bin), codex_info must NOT reject based on
    the global CODEX path. The spawn-time binary guard is only for the singleton (manager
    was None) path, whose _bin IS the global CODEX; an explicit manager owns its bin. Here a
    dead child forces a respawn — which must use the manager's own bin, not global CODEX."""
    import codex_server as cs
    mgr = cs.AppServerManager(bin=InfoFakeChild())
    mgr._child = type("Dead", (), {"poll": lambda self: 1})()   # dead → ensure must respawn
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: "/nonexistent/codex-bin-xyz")
    r = cs.codex_info_v2({"query": "models"}, manager=mgr)
    assert "error" not in r, f"explicit-manager call must not be blocked by global CODEX: {r}"
    assert r["result"]["data"][0]["id"] == "gpt-5.5"


# ---------------------------------------------------------------------------
# #227a: lazy CODEX binary resolution (item 1) + warm-child reconnect (item 2)
# ---------------------------------------------------------------------------

def test_resolve_codex_bin_reads_env_lazily(monkeypatch):
    """#227 item 1a: the codex binary is resolved from the CURRENT env per call, not frozen
    at import — a mid-session JAINE_CODEX_BIN change is picked up."""
    import codex_server as cs
    monkeypatch.setenv("JAINE_CODEX_BIN", "/first/codex")
    assert cs._resolve_codex_bin() == "/first/codex"
    monkeypatch.setenv("JAINE_CODEX_BIN", "/second/codex")   # changed mid-session
    assert cs._resolve_codex_bin() == "/second/codex"        # re-read, not frozen


def test_resolve_codex_bin_searches_path_for_fresh_install(monkeypatch):
    """#227 item 1b: with no JAINE_CODEX_BIN override, resolution searches PATH by bare name
    ('codex'), so a binary installed mid-session anywhere on PATH is found — where a frozen
    absolute fallback would miss it."""
    import codex_server as cs
    monkeypatch.delenv("JAINE_CODEX_BIN", raising=False)
    monkeypatch.setattr(cs.shutil, "which",
                        lambda name: "/freshly/installed/codex" if name == "codex" else None)
    assert cs._resolve_codex_bin() == "/freshly/installed/codex"


def test_get_manager_singleton_spawns_from_lazy_resolution(monkeypatch):
    """#227 item 1c: the module singleton manager spawns the app-server from the CURRENT
    lazy resolution, not a path frozen at construction."""
    import codex_server as cs
    captured = {}

    class _Stub:
        def poll(self): return None
        def kill(self): pass

    def fake_spawn(codex_bin, isolation_argv=None):
        captured["bin"] = codex_bin
        return _Stub()

    monkeypatch.setattr(cs, "_v2_manager", None)
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: "/lazy/resolved/codex")
    monkeypatch.setattr(cs, "_spawn_appserver", fake_spawn)
    # #227b: ensure() builds a reactor from the child; stub _make_reactor (the _Stub has no
    # real streams) — ensure() commits self._child itself on success, no _adopt anymore.
    monkeypatch.setattr(cs.AppServerManager, "_make_reactor", lambda self, child: None)
    monkeypatch.setattr(cs.AppServerManager, "_do_initialize", lambda *a, **k: None)
    cs._get_manager().ensure([])
    assert captured["bin"] == "/lazy/resolved/codex"


def test_codex_info_respawns_and_retries_on_warm_child_crash(monkeypatch):
    """#227 item 2: a warm child that dies DURING connection_request self-heals via ONE
    respawn+retry, so connection-level reads survive a mid-read crash."""
    import codex_server as cs

    class _Child:
        def __init__(self): self._dead = False
        def poll(self): return 1 if self._dead else None
        def kill(self): self._dead = True

    mgr = cs.AppServerManager(bin="/unused/codex")   # explicit → binary guard not in play
    mgr._child = _Child()                             # a warm (alive) child
    calls = {"req": 0, "ensure": 0}

    def fake_ensure(argv=None):
        calls["ensure"] += 1
        mgr._child = _Child()                         # respawn → fresh live child
        return mgr._child

    def fake_request(method, params=None, timeout=30.0):
        calls["req"] += 1
        if calls["req"] == 1:
            mgr._child._dead = True                   # crashed during the read
            raise RuntimeError("broken pipe during read")
        return {"data": [{"id": "gpt-5.5"}], "nextCursor": None}

    monkeypatch.setattr(mgr, "ensure", fake_ensure)
    monkeypatch.setattr(mgr, "connection_request", fake_request)
    r = cs.codex_info_v2({"query": "models"}, manager=mgr)
    assert "error" not in r, r
    assert r["result"]["data"][0]["id"] == "gpt-5.5"
    assert calls["req"] == 2, "must retry the read once after a warm-child crash"
    assert calls["ensure"] == 1, "must respawn exactly once"


def test_codex_info_live_child_error_not_retried(monkeypatch):
    """#227 item 2 boundary: an error from a still-ALIVE child is a real protocol/timeout
    error — surface it, do NOT respawn+retry (avoid masking real failures / over-retrying)."""
    import codex_server as cs

    class _Child:
        def poll(self): return None       # stays alive throughout
        def kill(self): pass

    mgr = cs.AppServerManager(bin="/unused/codex")
    mgr._child = _Child()
    calls = {"req": 0, "ensure": 0}

    def fake_ensure(argv=None):
        calls["ensure"] += 1
        return mgr._child

    def fake_request(method, params=None, timeout=30.0):
        calls["req"] += 1
        raise RuntimeError("model/list error: boom")

    monkeypatch.setattr(mgr, "ensure", fake_ensure)
    monkeypatch.setattr(mgr, "connection_request", fake_request)
    r = cs.codex_info_v2({"query": "models"}, manager=mgr)
    assert "error" in r and "boom" in r["error"]
    assert calls["req"] == 1, "live-child error must NOT trigger a retry"
    assert calls["ensure"] == 0


def test_codex_info_initial_spawn_failure_not_retried(monkeypatch):
    """#227 item 2 scope (panel finding B): the respawn+retry is for a WARM child that dies
    mid-read — NOT for an initial spawn that fails (no warm child ever existed). An initial
    ensure() failure must surface once, with no pointless second cold-start attempt and no
    'after respawn-retry' relabel of the original error."""
    import codex_server as cs

    class _Dead:
        def poll(self): return 1       # no live child → initial spawn needed
        def kill(self): pass

    mgr = cs.AppServerManager(bin="/unused/codex")
    mgr._child = _Dead()
    calls = {"ensure": 0}

    def fake_ensure(argv=None):
        calls["ensure"] += 1
        raise RuntimeError("cold-start timeout")

    def fake_request(*a, **k):
        raise AssertionError("must not reach connection_request when initial spawn fails")

    monkeypatch.setattr(mgr, "ensure", fake_ensure)
    monkeypatch.setattr(mgr, "connection_request", fake_request)
    r = cs.codex_info_v2({"query": "models"}, manager=mgr)
    assert "error" in r and "cold-start timeout" in r["error"], r
    assert "after respawn-retry" not in r["error"], "initial spawn failure must not be relabeled as a retry"
    assert calls["ensure"] == 1, "initial spawn failure must NOT trigger a second ensure"


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
    """If codex binary is absent AND no warm child exists, codex_run_v2 returns a clean error
    result (no manager → binary check) ahead of mcp validation. The explicit singleton reset
    pins the 'no warm child' precondition: #227 part-2 relaxed the gate to skip the binary
    requirement when a warm child could serve, so this no-codex contract now needs a guaranteed
    childless singleton (a prior test must not leave one live)."""
    import codex_server
    monkeypatch.setattr(codex_server, "_v2_manager", None)   # no warm child → spawn unavoidable
    monkeypatch.setattr(codex_server, "_resolve_codex_bin", lambda: "/nonexistent/codex")
    # Call codex_run_v2 directly without an explicit manager so the binary check runs
    r = codex_server.codex_run_v2({"prompt": "test"})
    assert "error" in r
    assert "codex binary not found" in r["error"]   # no-codex branch precedes mcp validation


def test_codex_run_warm_reuse_when_binary_missing(monkeypatch):
    """#227 part-2: a 2nd+ codex_run reuses a warm app-server child of the SAME isolation
    signature WITHOUT the codex binary on disk (mirrors codex_info #225 P3). The binary is
    needed only to SPAWN; a same-signature warm child left by a prior codex_run serves the
    call even if the codex symlink was removed mid-session (an upgrade). Design C (#227b)
    makes admitting the call safe. Exercises the real singleton dispatch path (manager=None)."""
    import codex_server as cs
    fake = InfoFakeChild()
    mgr = cs.AppServerManager(bin=fake)
    mgr.ensure([])                                   # warm child, isolation signature ()
    assert cs._is_child_alive(mgr._child) and mgr._isolation_sig == ()
    monkeypatch.setattr(cs, "_v2_manager", mgr)      # singleton has a live child
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: "/nonexistent/codex")  # binary gone mid-session

    def _sentinel_start(**kw):
        # Raised AFTER ensure() returns the warm child → proves the binary gate was skipped
        # and the warm reuse reached thread setup (no respawn, no binary needed).
        raise RuntimeError("REACHED_THREAD_START")
    monkeypatch.setattr(mgr, "start_thread", _sentinel_start)

    r = cs.codex_run_v2({"prompt": "x", "mcp": "all"})   # mcp='all' → signature () → warm reuse
    assert "binary not found" not in r.get("error", ""), f"warm child must serve without binary: {r}"
    assert "REACHED_THREAD_START" in r.get("error", ""), f"must reach thread setup via warm reuse: {r}"
    fake.kill()


def test_codex_run_mismatch_signature_no_binary_fails_safe(monkeypatch):
    """#227 part-2 boundary: a DIFFERENT mcp signature needs a respawn → the binary IS
    required. With it gone the call fails honestly, but design C (#227b) keeps the ORIGINAL
    warm child alive — the old kill-then-spawn would have destroyed a still-usable server.

    The warm child is brought up via a fake, then `_bin` is set to None so the mismatch
    respawn takes the production singleton path (`_resolve_codex_bin()` — the monkeypatched
    missing binary). With `bin=fake` the respawn would clone the fake class and never consult
    the binary, replacing+killing the warm child and passing for the wrong reason (codex
    review P2)."""
    import codex_server as cs
    fake = InfoFakeChild()
    mgr = cs.AppServerManager(bin=fake)
    mgr.ensure([])                                   # warm child, signature () == mcp 'all'
    warm = mgr._child
    mgr._bin = None                                  # next spawn resolves the binary lazily (real path)
    monkeypatch.setattr(cs, "_v2_manager", mgr)
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: "/nonexistent/codex")  # binary gone mid-session
    r = cs.codex_run_v2({"prompt": "x", "mcp": "isolated"})   # signature differs → respawn → binary needed
    assert "error" in r and "ensure failed" in r["error"], \
        f"a mismatch respawn without the binary must fail at ensure(), not the top gate: {r}"
    assert mgr._child is warm and cs._is_child_alive(warm), \
        "the ORIGINAL warm child must survive a failed mismatch respawn (design C)"
    fake.kill()


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
            # #256: initialize carries the routing manifest end-to-end (CC injects it on connect).
            assert "instructions" in result, f"Missing instructions in {result}"
            assert "codex_review" in result["instructions"]
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
            assert "mcp" in schema_props, f"inputSchema missing 'mcp': {list(schema_props)}"
            required = codex_tool.get("inputSchema", {}).get("required", [])
            assert "mcp" in required, f"mcp must be REQUIRED, required={required}"
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

    def test_malformed_tools_call_params_does_not_crash_dispatcher(self):
        """A tools/call with truthy NON-dict params (e.g. a list) must NOT crash the
        long-lived dispatcher — it must return a guarded error reply and keep serving.
        Regression guard: `req.get('params',{}) or {}` only coerces FALSY non-dicts — a
        truthy list/str passes through, so every params access (arguments, name) must stay
        INSIDE the try, or .get() raises OUTSIDE it and kills main()."""
        proc = self._start_server()
        try:
            self._send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self._recv(proc)
            # params is a truthy list → not coerced to {} → .get() would raise
            self._send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": [1, 2, 3]})
            resp = self._recv(proc)   # must REPLY (not crash, not hang)
            assert resp.get("id") == 2, f"expected a reply for the malformed frame: {resp}"
            assert resp.get("result", {}).get("isError"), \
                f"malformed params must yield a guarded error reply: {resp}"
            # dispatcher must still be alive afterwards
            self._send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            assert self._recv(proc).get("id") == 3, "dispatcher must survive the malformed frame"
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
        "mcp": "isolated",
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
        "mcp": "isolated",
    })
    assert "error" not in r, f"codex_run_v2 returned error: {r.get('error')}"
    assert r.get("thread_id"), f"thread_id must be non-empty, got: {r}"
    assert r.get("result"), f"result must be non-empty for implement mode, got: {r}"


@skip_if_no_codex
@pytest.mark.slow
def test_e2e_turn_interrupt_real_appserver():
    """#218 LIVE: start a long real turn, let deltas flow, then _run_interrupt → status=interrupted
    and the app-server session stays WARM (a new thread starts on the SAME child, no process kill).
    Mirrors /tmp/turn_interrupt_probe.py through the production interrupt routine. Allow 30-150 s."""
    import time as _t
    import codex_server as cs
    mgr = cs.AppServerManager()
    try:
        mgr.ensure([])
        tid = mgr.start_thread(sandbox="read-only", approval_policy="never")
        ts = {"final_message_parts": [], "usage_snapshot": {}, "retries": 0,
              "interrupting": False, "interrupted_by": "cancel", "acc": [],
              "manager": mgr, "turn_start_t": _t.time(), "mcp_mode": "isolated",
              "mcp_servers_enabled": [], "effort_val": None, "model_val": None,
              "mode": "implement", "thread_id": tid, "review_target": None}
        prompt = ("Produce a thorough numbered list of 80 distinct edge cases for parsing "
                  "ISO-8601 timestamps, each with a one-sentence explanation. End with: done")
        mid = mgr._next_id()
        mgr._write({"id": mid, "method": "turn/start",
                    "params": {"threadId": tid, "input": cs._turn_input(prompt)}})
        turn_id = None
        deltas = 0
        t0 = _t.time()
        while _t.time() - t0 < 150:
            for f in mgr._reactor.pump(timeout=0.2):
                if f.get("id") == mid and "result" in f:
                    turn_id = ((f.get("result") or {}).get("turn") or {}).get("id")
                if "delta" in (f.get("method") or ""):
                    deltas += 1
            if turn_id and deltas > 0:
                break
        assert turn_id, "no turn ACK within 150s"
        res = cs._run_interrupt(mgr, ts, turn_id, "cancel")
        assert res["status"] == "interrupted", f"expected interrupted, got: {res}"
        assert "error" not in res
        # WARM: a new thread starts on the SAME live child (no cold-start kill needed)
        tid2 = mgr.start_thread(sandbox="read-only", approval_policy="never")
        assert tid2 and tid2 != tid, "session not warm after interrupt"
    finally:
        try:
            if mgr._child is not None:
                mgr._child.kill()
        except Exception:
            pass


@skip_if_no_codex
@pytest.mark.slow
def test_e2e_optin_timeout_graceful_real_appserver():
    """#218 LIVE full stack: a heavy real turn + a small opt-in timeout → graceful, resumable
    result (no crash/hang), via codex_run_v2's production interrupt path. The session is usable
    afterward (codex_info has no cold-start crash). Allow 30-120 s."""
    from codex_server import codex_run_v2, codex_info_v2
    r = codex_run_v2({
        "prompt": "Write a detailed 60-step data-migration plan, elaborating each step in a full paragraph.",
        "mode": "implement", "mcp": "isolated", "timeout": 3,
    })
    assert "error" not in r, f"opt-in timeout must be graceful (no error), got: {r.get('error')}"
    assert r.get("thread_id"), f"must carry a resumable thread_id: {r}"
    if r.get("status") == "interrupted":
        assert r.get("interrupted_by") == "timeout", f"interrupted_by must be 'timeout': {r}"
    info = codex_info_v2({"query": "models"})
    assert "error" not in info or info.get("result") is not None


@skip_if_no_codex
@pytest.mark.slow
def test_e2e_usage_is_populated_by_real_appserver():
    """Real codex emits thread/tokenUsage/updated; the result's usage.total_tokens must be a
    real positive int — proves the params.tokenUsage.total.<camelCase> wire mapping (spec 2a)."""
    from codex_server import codex_run_v2
    r = codex_run_v2({"prompt": "Say OK.", "mode": "implement", "mcp": "isolated"})
    assert "error" not in r, f"turn errored: {r.get('error')}"
    assert isinstance(r.get("usage"), dict), f"no usage block: {r}"
    assert isinstance(r["usage"].get("total_tokens"), int) and r["usage"]["total_tokens"] > 0, \
        f"usage.total_tokens not populated (wire-key drift? reading params.tokenUsage.total?): {r['usage']}"


@skip_if_no_codex
@pytest.mark.slow
def test_e2e_control_knobs_echoed_by_real_appserver():
    """F5b — prove the camelCase forwarding mechanism works end-to-end against real codex.

    approvals_reviewer is an ENUM (ApprovalsReviewer) that codex resolves and ECHOES on
    ThreadStartResponse → a strong round-trip assertion catches a wrong wire key.

    service_tier rides the IDENTICAL forwarding mechanism, BUT its echo is account-dependent:
    ThreadStartResponse.serviceTier is a free nullable string (`["string","null"]`, NO enum —
    schema-confirmed codex 0.141) that codex resolves to the EFFECTIVE tier. A ChatGPT-
    subscription account returns null even when 'flex' is sent (API service tiers don't apply
    to subscription billing — empirically verified). So serviceTier's wire key is guarded by
    the OFFLINE test (test_approvals_reviewer_and_service_tier_reach_thread_start asserts it
    lands in thread/start params) + the schema; here we only assert it is ACCEPTED without
    error. (If run against an API-key account with real tiers, serviceTier may echo non-null;
    this test stays correct either way.)"""
    from codex_server import codex_run_v2
    r = codex_run_v2({"prompt": "Say OK.", "mode": "implement", "mcp": "isolated",
                      "approvals_reviewer": "user", "service_tier": "flex"})
    assert "error" not in r, f"control knobs rejected: {r.get('error')}"
    assert r.get("thread_id")
    # approvals_reviewer round-trips → proves the camelCase forwarding mechanism end-to-end (F5b)
    assert r["codex"]["approvals_reviewer"] == "user", f"approvalsReviewer not echoed: {r['codex']}"
    # service_tier accepted (no error above); its effective echo is account-dependent (see docstring)
    assert "service_tier" in r["codex"]


@skip_if_no_codex
@pytest.mark.slow
def test_e2e_long_turn_completes_without_self_cap():
    """A real review turn that may exceed 120s must still complete (no self-imposed cap).
    Uses a deliberately heavier prompt; default = no timeout."""
    from codex_server import codex_run_v2
    r = codex_run_v2({
        "prompt": ("Carefully review this for correctness, security, performance, and edge "
                   "cases, enumerating every issue: def parse(s): return eval(s)"),
        "mode": "review", "mcp": "isolated", "effort": "xhigh",
    })
    assert "error" not in r, f"long turn errored (regression: self-cap?): {r.get('error')}"
    assert r.get("verdict") in ("GO", "NO-GO", "MINOR-FIXES")


@pytest.mark.slow
def test_e2e_codex_info_reads():
    """Live: codex_info connection-level reads return the expected shapes (no cold-start)."""
    import json
    from codex_server import codex_info_v2
    r = codex_info_v2({"query": "models"})
    assert "error" not in r, r
    assert isinstance(r["result"].get("data"), list) and r["result"]["data"], "model/list → data[]"
    r2 = codex_info_v2({"query": "auth"})
    assert "error" not in r2 and "authMethod" in r2["result"], r2
    r3 = codex_info_v2({"query": "config"})
    assert "error" not in r3 and "config" in r3["result"], r3
    # compact projection: origins dropped, omitted list present, no token blowout
    assert "origins" not in r3["result"], r3
    assert isinstance(r3["result"].get("omitted"), list), r3
    assert len(json.dumps(r3["result"])) < 4000, "config projection must be compact"
    r4 = codex_info_v2({"query": "limits"})   # paramless query
    assert "error" not in r4 and "rateLimits" in r4["result"], r4


@pytest.mark.slow
def test_e2e_codex_review_uncommitted(tmp_path):
    """Live: native codex_review on a tmp repo with an uncommitted change returns
    free-text findings (review/start → item/completed agentMessage path)."""
    import subprocess as sp
    from codex_server import codex_review_v2
    repo = tmp_path / "repo"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "config", "user.email", "t@t.io"], cwd=repo, check=True)
    sp.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    sp.run(["git", "add", "-A"], cwd=repo, check=True)
    sp.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    # uncommitted change: a divide with no zero-check
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef div(a, b):\n    return a / b\n")
    r = codex_review_v2({"target": "uncommitted", "mcp": "isolated",
                         "cwd": str(repo), "effort": "low", "timeout": 240})
    assert "error" not in r, r
    assert isinstance(r.get("review"), str) and len(r["review"]) > 20, r
    assert r.get("thread_id"), r


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
    """Deny-keys are scrubbed; benign keys pass; no per-thread mcp_servers injection."""
    sent = _started_params(config={"mcp_servers": {"evil": 1}, "mcpServers": {"evil": 1},
                                   "baseInstructions": "x", "developerInstructions": "y",
                                   "model_reasoning_effort": "high"})["config"]
    assert "mcp_servers" not in sent                    # caller injection scrubbed (no re-inject)
    assert "mcpServers" not in sent                     # alias scrubbed
    assert "baseInstructions" not in sent and "developerInstructions" not in sent
    assert sent["model_reasoning_effort"] == "high"     # benign key passes


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
    assert "mcp_servers" not in p["config"]            # caller injection scrubbed, not re-injected
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
    cs.codex_run_v2({"prompt": "ping", "mode": "review", "mcp": "isolated"})   # drives ensure()+initialize on the singleton
    assert cs._get_manager()._codex_version == cs.LAST_VERIFIED_CODEX_VERSION


def test_tools_list_exposes_parity_fields_and_drift():
    import codex_server as cs
    props = cs.TOOLS[0]["inputSchema"]["properties"]
    for f in ("base_instructions", "developer_instructions", "config"):
        assert f in props
    assert "_drift" in cs.TOOLS[0]["description"]


# ---------------------------------------------------------------------------
# Task 1: isolation resolution primitives (#204 Group 1)
# ---------------------------------------------------------------------------

def test_enumerate_config_mcp_servers_reads_table(tmp_path, monkeypatch):
    import codex_server as cs
    (tmp_path / "config.toml").write_text(
        'model = "gpt-5.5"\n\n'
        '[mcp_servers.dash]\ncommand = "dash-mcp"\n\n'
        '[mcp_servers.deepwiki]\nurl = "https://example/mcp"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cs._enumerate_config_mcp_servers() == ["dash", "deepwiki"]  # sorted

def test_enumerate_config_mcp_servers_missing_file_is_empty(tmp_path, monkeypatch):
    import codex_server as cs
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))   # no config.toml
    assert cs._enumerate_config_mcp_servers() == []

def test_enumerate_config_mcp_servers_malformed_is_empty(tmp_path, monkeypatch):
    import codex_server as cs
    (tmp_path / "config.toml").write_text("this is = = not valid toml [[[")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cs._enumerate_config_mcp_servers() == []   # never raises

def test_build_isolation_argv_all_disables_nothing():
    import codex_server as cs
    assert cs._build_isolation_argv("all", ["dash", "deepwiki"]) == []

def test_build_isolation_argv_isolated_disables_every_server_and_apps():
    import codex_server as cs
    argv = cs._build_isolation_argv("isolated", ["dash", "deepwiki"])
    # BARE keys (no quotes): codex's -c parser splits on '.' naively and does NOT honor
    # TOML quoting — a quoted mcp_servers key CRASHES app-server startup on 0.141.
    assert argv == [
        "-c", "mcp_servers.dash.enabled=false",
        "-c", "mcp_servers.deepwiki.enabled=false",
        "--disable", "apps",
    ]

def test_build_isolation_argv_subset_keeps_named_disables_rest():
    import codex_server as cs
    # keep deepwiki + apps; disable dash
    argv = cs._build_isolation_argv(["deepwiki", "apps"], ["dash", "deepwiki"])
    assert argv == ["-c", "mcp_servers.dash.enabled=false"]   # apps kept → not disabled

def test_build_isolation_argv_subset_disables_apps_when_not_listed():
    import codex_server as cs
    argv = cs._build_isolation_argv(["dash"], ["dash", "deepwiki"])
    assert argv == ["-c", "mcp_servers.deepwiki.enabled=false", "--disable", "apps"]

def test_build_isolation_argv_rejects_unknown_mode():
    import codex_server as cs
    import pytest
    with pytest.raises(ValueError):
        cs._build_isolation_argv("nonsense", ["dash"])

def test_build_isolation_argv_skips_untargetable_server_name(capsys):
    """A server name containing '.', '"', or '=' cannot be targeted by
    `-c mcp_servers.<name>.enabled=false`: codex's CLI parser splits the key path on '.'
    naively, splits key/value on the FIRST '=', and does NOT honor TOML quoting (a quoted
    mcp_servers key even CRASHES app-server startup on 0.141 — empirically verified). Such a
    name is SKIPPED with a stderr warning (left enabled), never silently mis-targeted/crashed.
    F1 (#215 review): '=' must be guarded too — `mcp_servers.foo=bar.enabled=false` mis-targets
    the key `mcp_servers.foo` (splitn(2,'=')) and silently fails to disable `foo=bar`."""
    import codex_server as cs
    for bad in ("weird.name", 'has"quote', "evil=injected"):
        argv = cs._build_isolation_argv("isolated", [bad])
        err = capsys.readouterr().err
        assert argv == ["--disable", "apps"], f"{bad!r} should be skipped, got {argv}"
        assert bad in err and "WARNING" in err, f"{bad!r} should emit a WARNING"


# ---------------------------------------------------------------------------
# Task 2: child env allowlist (#204 1c — secret-leak fix)
# ---------------------------------------------------------------------------

def test_build_child_env_keeps_essentials():
    import codex_server as cs
    parent = {"PATH": "/usr/bin", "HOME": "/Users/x", "CODEX_HOME": "/Users/x/.codex",
              "TMPDIR": "/tmp", "LANG": "en_US.UTF-8", "LC_ALL": "C", "TERM": "xterm"}
    env = cs._build_child_env(parent)
    for k in parent:
        assert env.get(k) == parent[k], f"{k} must pass the allowlist"

def test_build_child_env_drops_secrets():
    import codex_server as cs
    parent = {"PATH": "/usr/bin", "FORGEJO_API_TOKEN": "secret",
              "ANTHROPIC_API_KEY": "sk-xxx", "MY_CUSTOM_TOKEN": "leak"}
    env = cs._build_child_env(parent)
    assert "PATH" in env
    assert "FORGEJO_API_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "MY_CUSTOM_TOKEN" not in env

def test_build_child_env_keeps_codex_own_credentials_and_proxy():
    import codex_server as cs
    parent = {"OPENAI_API_KEY": "ok", "OPENAI_BASE_URL": "https://api",
              "HTTPS_PROXY": "http://p", "https_proxy": "http://p",
              "SSL_CERT_FILE": "/c.pem", "CODEX_CA_CERTIFICATE": "/corp-ca.pem"}
    env = cs._build_child_env(parent)
    for k in parent:
        assert k in env, f"codex needs {k}"

def test_shell_env_policy_argv_is_a_c_override():
    """F1: layer-2 shell_environment_policy is a `-c` spawn override (defense-in-depth)."""
    import codex_server as cs
    assert cs._SHELL_ENV_POLICY_ARGV[0] == "-c"
    assert cs._SHELL_ENV_POLICY_ARGV[1].startswith("shell_environment_policy.inherit=")


# ---------------------------------------------------------------------------
# Task 3: isolation-aware spawn + signature respawn
# ---------------------------------------------------------------------------

def test_spawn_appserver_appends_isolation_argv_and_scrubs_env(monkeypatch):
    """_spawn_appserver builds `app-server <isolation_argv>` and passes a scrubbed env."""
    import codex_server as cs
    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["env"] = kw.get("env")
            self.stdin = self.stdout = self.stderr = None
            self.returncode = None
        def poll(self): return None
        def kill(self): pass

    monkeypatch.setattr(cs.subprocess, "Popen", _FakeProc)
    monkeypatch.setenv("FORGEJO_API_TOKEN", "secret")
    cs._spawn_appserver("/bin/codex", ["-c", "mcp_servers.dash.enabled=false", "--disable", "apps"])
    # layer-2 shell_environment_policy (F1) is prepended, then the isolation argv
    assert captured["argv"] == ["/bin/codex", "app-server",
                                *cs._SHELL_ENV_POLICY_ARGV,
                                "-c", "mcp_servers.dash.enabled=false", "--disable", "apps"]
    assert "FORGEJO_API_TOKEN" not in captured["env"]   # scrubbed (layer 1)
    assert "PATH" in captured["env"]

def test_manager_warm_reuse_same_isolation_signature(fake_child):
    from codex_server import AppServerManager
    m = AppServerManager(bin=fake_child)
    c1 = m.ensure(["-c", "a=b"])
    c2 = m.ensure(["-c", "a=b"])      # same signature → warm reuse
    assert c2 is c1

def test_manager_respawns_on_isolation_signature_change():
    from codex_server import AppServerManager
    fc = FakeChild()
    try:
        m = AppServerManager(bin=fc)
        c1 = m.ensure(["-c", "x=1"])
        c2 = m.ensure(["-c", "y=2"])   # different signature → respawn
        assert c2 is not c1
    finally:
        fc.kill()

def test_manager_ensure_clears_state_when_initialize_fails(monkeypatch):
    """F2 (#215 review): if _do_initialize() raises (e.g. cold-start timeout), ensure() must
    NOT leave an alive-but-uninitialised child committed — else the next same-sig call would
    warm-reuse a server with no session (hang on start_thread). Both _child AND _isolation_sig
    must be cleared on failure, and the exception re-raised."""
    import pytest
    import codex_server as cs
    from codex_server import AppServerManager
    fc = FakeChild()
    try:
        m = AppServerManager(bin=fc)
        monkeypatch.setattr(cs.AppServerManager, "_do_initialize",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("initialize timed out")))
        with pytest.raises(RuntimeError):
            m.ensure(["-c", "x=1"])
        assert m._child is None, "child must be cleared on initialize failure (no stale wedge)"
        assert m._isolation_sig is None, "signature must be cleared on initialize failure"
    finally:
        fc.kill()

def test_ensure_failed_respawn_preserves_warm_child(monkeypatch):
    """#227b transactional respawn: a different-signature respawn whose initialize FAILS must
    leave the existing warm child + its signature INTACT — the old kill-then-spawn killed the
    warm child first, so a spawn/init failure left no usable server at all."""
    import pytest
    import codex_server as cs
    from codex_server import AppServerManager, _is_child_alive
    fc = FakeChild()
    try:
        m = AppServerManager(bin=fc)
        m.ensure(["-c", "x=1"])                       # establish a warm child (real init)
        warm = m._child
        assert warm is not None and m._isolation_sig == ("-c", "x=1")
        # A different-sig respawn whose initialize fails:
        monkeypatch.setattr(cs.AppServerManager, "_do_initialize",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("init timed out")))
        with pytest.raises(RuntimeError):
            m.ensure(["-c", "y=2"])
        assert m._child is warm, "warm child must survive a failed respawn (was killed-first)"
        assert m._isolation_sig == ("-c", "x=1"), "signature must be unchanged on failed respawn"
        assert _is_child_alive(warm), "the surviving warm child must still be alive"
    finally:
        fc.kill()


def test_ensure_make_reactor_failure_kills_temp_child(monkeypatch):
    """#227b: if _make_reactor raises AFTER the child is spawned, the temp child must be killed —
    no leaked/zombie codex process. Requires _make_reactor to sit INSIDE the transactional try."""
    import pytest
    import codex_server as cs
    killed = []

    class _Stub:
        def poll(self): return None
        def kill(self): killed.append(True)

    monkeypatch.setattr(cs, "_spawn_appserver", lambda b, a=None: _Stub())
    monkeypatch.setattr(cs.AppServerManager, "_make_reactor",
                        lambda self, child: (_ for _ in ()).throw(RuntimeError("no fd")))
    m = cs.AppServerManager(bin="/bin/codex")
    with pytest.raises(RuntimeError):
        m.ensure([])
    assert killed == [True], "temp child must be killed when _make_reactor fails (no zombie)"


def test_manager_respawn_passes_new_isolation_argv_to_spawn(monkeypatch):
    """F6 (#215 review): a string-bin manager that respawns on signature change must call
    _spawn_appserver with the NEW isolation argv. The FakeChild respawn test above takes the
    __class__() path (no argv), so this is the only coverage of the ensure()→_spawn_appserver
    argv hand-off on respawn."""
    import codex_server as cs
    calls = []

    class _Stub:
        def poll(self): return None
        def kill(self): pass

    def fake_spawn(codex_bin, isolation_argv=None):
        calls.append(list(isolation_argv or []))
        return _Stub()

    monkeypatch.setattr(cs, "_spawn_appserver", fake_spawn)
    # #227b: stub _make_reactor (the _Stub has no real streams); ensure() commits self._child
    # itself on a successful init — _adopt is gone.
    monkeypatch.setattr(cs.AppServerManager, "_make_reactor", lambda self, child: None)
    monkeypatch.setattr(cs.AppServerManager, "_do_initialize", lambda *a, **k: None)
    m = cs.AppServerManager(bin="/bin/codex")
    m.ensure(["-c", "x=1"])            # first spawn
    m.ensure(["-c", "y=2"])            # sig change → respawn must carry NEW argv
    assert calls == [["-c", "x=1"], ["-c", "y=2"]], f"respawn must pass new argv: {calls}"


# ---------------------------------------------------------------------------
# Task 5: the REQUIRED mcp knob + list discovery
# ---------------------------------------------------------------------------

def test_mcp_required_missing_is_error(ext_child):
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi"}, manager=m, cc_write_fn=lambda f: None,
                     cc_read_fn=lambda timeout=10: None)   # no mcp
    assert "error" in r and "mcp" in r["error"].lower()

def test_mcp_invalid_value_is_error(ext_child):
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi", "mcp": "wat"}, manager=m,
                     cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert "error" in r

def test_mcp_list_returns_available_without_spawn(tmp_path, monkeypatch):
    """mcp='list' enumerates config servers + builtins and returns WITHOUT a turn."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # No manager needed — list never spawns. But the no-codex check precedes it,
    # so point CODEX at a real-enough path via monkeypatch.
    monkeypatch.setattr(cs, "_resolve_codex_bin", lambda: sys.executable)   # any existing file passes the binary check
    # codex_run_v2(manager=None) creates the module singleton _v2_manager with bin=CODEX.
    # Register it with monkeypatch so teardown restores it — else this test's sys.executable
    # manager would poison a later singleton-using (slow) test.
    monkeypatch.setattr(cs, "_v2_manager", None)
    r = cs.codex_run_v2({"prompt": "ignored", "mcp": "list"})
    assert r.get("available_mcp_servers") == ["dash"]
    assert "apps" in r["builtins"] and "computer-use" in r["builtins"]
    assert "thread_id" not in r or r["thread_id"] is None   # never ran a turn

def test_mcp_isolated_resolves_argv_and_passes_to_ensure(tmp_path, monkeypatch, ext_child):
    """codex_run_v2 with mcp='isolated' calls ensure() with the disable argv."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    captured = {}
    m = cs.AppServerManager(bin=ext_child)
    orig = m.ensure
    def spy(argv=None):
        captured["argv"] = argv
        return orig(argv)
    m.ensure = spy
    cs.codex_run_v2({"prompt": "hi", "mcp": "isolated"}, manager=m,
                    cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert captured["argv"] == ['-c', 'mcp_servers.dash.enabled=false', '--disable', 'apps']


# ---------------------------------------------------------------------------
# Task 6: tokenUsage + metadata (additive)
# ---------------------------------------------------------------------------

def test_result_carries_usage_and_metadata(ext_child):
    ext_child.script_turn_variant("with_usage")     # emits thread/tokenUsage/updated
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp="isolated")
    assert "error" not in r
    assert r["usage"]["total_tokens"] == 123
    assert r["codex"]["mcp_mode"] == "isolated"
    # F2b: computer-use is bundled (never disabled) → always present, even in isolated
    assert r["codex"]["mcp_servers_enabled"] == ["computer-use"]
    assert "duration_ms" in r["timing"]
    assert r["status"] == "completed"
    # additive: existing keys still present
    assert "result" in r

def test_metadata_absent_keys_do_not_break_review_shape(ext_child):
    r = call_codex_run(ext_child, prompt="hi", mode="review", mcp="all")
    assert "error" not in r
    assert {"thread_id", "verdict", "findings", "schema_ok"} <= set(r)  # unchanged core
    assert r["codex"]["mcp_mode"] == "all"
    assert "computer-use" in r["codex"]["mcp_servers_enabled"]   # F2b

def test_subset_unknown_names_rejected_pre_spawn(ext_child, monkeypatch, tmp_path):
    """F2: a subset name matching no config server / builtin fails loud BEFORE spawn —
    a typo must not silently disable the server the caller meant to keep."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp=["dahs"])  # typo of dash
    assert "error" in r and "dahs" in r["error"]
    assert ext_child.turn_start_params is None   # never ran a turn

def test_mcp_list_value_does_not_crash(ext_child, monkeypatch, tmp_path):
    """F2: a valid list subset must NOT raise TypeError (`list in frozenset` is unhashable).
    F4 (#215 review): also assert the SUBSET `mcp_servers_enabled` output — the subset branch
    (kept server + bundled computer-use; 'apps' not listed → not enabled) had no output
    assertion, so an inverted filter would not be caught. dash is in the subset, apps is not."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp=["dash"])
    assert "error" not in r   # no TypeError; dash kept
    assert r["codex"]["mcp_servers_enabled"] == ["dash", "computer-use"], r["codex"]

def test_mcp_dict_value_is_error(ext_child):
    """F2: a dict mcp is rejected cleanly (not a TypeError crash)."""
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi", "mcp": {"x": 1}}, manager=m,
                     cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert "error" in r and "invalid mcp" in r["error"].lower()

def test_failed_turn_carries_metadata(ext_child):
    """F11: a terminal turn failure still returns usage/codex/timing/status (token cost
    visibility on failure)."""
    ext_child.script_turn_variant("failed")
    r = call_codex_run(ext_child, prompt="hi", mcp="isolated")
    assert "error" in r and "turn failed" in r["error"]
    assert r["status"] == "failed"
    assert r["codex"]["mcp_mode"] == "isolated"
    assert "timing" in r and "usage" in r


# ---------------------------------------------------------------------------
# Task 7: first-class control knobs (#204 2b)
# ---------------------------------------------------------------------------

def test_approvals_reviewer_and_service_tier_reach_thread_start():
    p = _started_params(approvals_reviewer="auto_review", service_tier="flex")
    assert p["approvalsReviewer"] == "auto_review"
    assert p["serviceTier"] == "flex"

def test_control_knobs_omitted_when_none():
    p = _started_params()
    assert "approvalsReviewer" not in p
    assert "serviceTier" not in p

def test_verbosity_is_config_passthrough():
    # R1-F5 demote: verbosity is NOT first-class; it rides the existing config passthrough.
    p = _started_params(config={"model_verbosity": "low"})
    assert p["config"]["model_verbosity"] == "low"   # benign key, not scrubbed by _CONFIG_DENY

def test_codex_run_v2_forwards_control_knobs(ext_child):
    # F5a: exactly ONE run on a fresh ext_child so received("thread/start") returns THIS
    # call's thread/start, not a stale earlier one (the prior version's spurious first
    # call_codex_run made `received` return the wrong, knob-less thread/start → false pass).
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    codex_run_v2({"prompt": "hi", "mcp": "isolated", "approvals_reviewer": "auto_review",
                  "service_tier": "flex", "config": {"model_verbosity": "high"}},
                 manager=m, cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    p = ext_child.received("thread/start")["params"]
    assert p["approvalsReviewer"] == "auto_review"
    assert p["serviceTier"] == "flex"
    assert p["config"]["model_verbosity"] == "high"   # via config passthrough

def test_control_knobs_on_resume_fail_loud(ext_child):
    """F4: thread-level knobs are set at thread/start; on resume there is no thread/start,
    so they must fail loud (not silently no-op)."""
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi", "mcp": "isolated", "thread_id": "T1",
                      "service_tier": "flex"},
                     manager=m, cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert "error" in r
    assert "resum" in r["error"].lower() or "new thread" in r["error"].lower()


# ---------------------------------------------------------------------------
# Task 8: no self-imposed turn cap; opt-in timeout
# ---------------------------------------------------------------------------

def test_default_has_no_work_duration_deadline(ext_child):
    """With no timeout arg, codex_run_v2 must not impose a work-duration deadline
    (the loop condition is unbounded; the turn ends on turn/completed).
    F5 (#215 review): the old check only matched the literal '120.0' — a DIFFERENT hardcoded
    cap (e.g. 180.0) would have slipped through. Guard against ANY hardcoded numeric
    work-duration deadline, and assert the deadline is opt-in (derived from the timeout arg)."""
    import inspect, re, codex_server as cs
    src = inspect.getsource(cs.codex_run_v2)
    assert not re.search(r"deadline\s*=\s*time\.time\(\)\s*\+\s*\d", src), \
        "no HARDCODED numeric work-duration cap may exist — the cap must be opt-in (timeout arg)"
    # the work-duration deadline must be derived from the opt-in timeout (None when unset):
    assert "turn_timeout" in src and "deadline is None" in src, \
        "deadline must be opt-in (turn_timeout) and unbounded (None) by default"
    # behavioral: a normal turn still completes fine with no timeout arg:
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp="isolated")
    assert "error" not in r and r.get("result") is not None

def test_opt_in_timeout_fires_when_set(monkeypatch):
    """With the kill-switch set, a never-completing turn + a tiny timeout → LEGACY bare timeout
    error (no hang). #218 makes the DEFAULT a graceful interrupt instead — that path is covered
    by test_turn_optin_timeout_returns_graceful; this guards the kill-switch legacy arm (F8)."""
    monkeypatch.setenv("BULLDOZER_CODEX_NO_INTERRUPT", "1")
    from codex_server import codex_run_v2, AppServerManager

    class _NeverCompletes(ExtendedFakeChild):
        def _dispatch(self, msg):
            method = msg.get("method"); mid = msg.get("id")
            params = msg.get("params") or {}
            if method == "turn/start":
                self.turn_start_params = params
                # ACK the turn but NEVER send turn/completed
                self._write_msg({"id": mid, "result": {"turn": {"id": "T", "items": [], "status": "inProgress"}}})
                return
            super()._dispatch(msg)

    fc = _NeverCompletes()
    try:
        m = AppServerManager(bin=fc)
        r = codex_run_v2({"prompt": "hi", "mcp": "isolated", "timeout": 0.3},
                         manager=m, cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
        assert "error" in r and "timed out" in r["error"]
    finally:
        fc.kill()


class _PreAckApprovalChild(ExtendedFakeChild):
    """Emits an approval REQUEST before the TurnStartResponse (pre-ACK), then waits for the
    bridge's decision reply before ACKing — exercises F6 (the human approval wait must be
    credited to ack_deadline, not counted as a setup stall)."""
    def __init__(self):
        super().__init__()
        self._reply_event = _threading.Event()

    def _dispatch(self, msg):
        # a decision reply (id + result, no method) unblocks the pending pre-ACK approval
        if msg.get("id") is not None and "method" not in msg and ("result" in msg or "error" in msg):
            self._reply_event.set()
            return
        if msg.get("method") == "turn/start":
            params = msg.get("params") or {}
            self.turn_start_params = params
            tid = params.get("threadId", "T1")
            turn_mid = msg.get("id")
            def _flow():
                self._write_msg({"id": "PREACK-1",
                                 "method": "item/commandExecution/requestApproval",
                                 "params": {"threadId": tid, "turnId": "TURN1", "itemId": "I1",
                                            "command": "echo hi", "cwd": "/tmp",
                                            "availableDecisions": ["accept", "cancel"]}})
                self._reply_event.wait(timeout=5.0)   # block until the bridge replies (human time)
                self._write_msg({"id": turn_mid, "result": {"turn": {"id": "TURN1", "items": [], "status": "inProgress"}}})
                self._write_msg({"method": "item/agentMessage/delta",
                                 "params": {"delta": "ok", "threadId": tid, "turnId": "TURN1", "itemId": "I1"}})
                self._write_msg({"method": "turn/completed",
                                 "params": {"threadId": tid, "turn": {"id": "TURN1", "status": "completed",
                                                                       "error": None, "durationMs": 1}}})
            _threading.Thread(target=_flow, daemon=True).start()
            return
        super()._dispatch(msg)


def test_pre_ack_approval_does_not_trip_ack_deadline(monkeypatch):
    """F6: an approval arriving BEFORE the turn/start ACK, with a human reply slower than the
    ACK window, must NOT cause an ACK timeout — the wait is credited to ack_deadline."""
    import codex_server as cs
    monkeypatch.setattr(cs, "_ACK_TIMEOUT", 0.3)   # shrink the setup window
    fc = _PreAckApprovalChild()
    try:
        m = cs.AppServerManager(bin=fc)
        written = []
        def cc_write(msg): written.append(msg)
        def cc_read(timeout=10.0):
            import time as _t; _t.sleep(0.5)        # human takes longer than _ACK_TIMEOUT
            eid = written[-1]["id"] if written else 1
            return {"id": eid, "result": {"action": "accept", "content": {"label": "Allow once"}}}
        r = cs.codex_run_v2({"prompt": "hi", "mcp": "isolated", "mode": "implement"},
                            manager=m, cc_write_fn=cc_write, cc_read_fn=cc_read)
        assert "error" not in r, f"pre-ACK approval tripped a timeout: {r}"
        assert r.get("result") == "ok"
    finally:
        fc.kill()


# ---------------------------------------------------------------------------
# Task 9: schema-generated notification allowlist (#204 3b)
# ---------------------------------------------------------------------------

def test_known_notifications_loaded_from_fixture_excludes_error_warning():
    import json, os
    import codex_server as cs
    # F7: fixture lives in mcp/ (sibling of codex_server.py), NOT tests/fixtures/, so
    # it ships in the plugin cache. Read it from MCP_DIR — the SAME path the runtime uses.
    fixture_path = os.path.join(MCP_DIR, "codex-notifications.json")
    assert os.path.isfile(fixture_path), (
        "fixture must live in mcp/ so it ships in the plugin cache (F7)")
    fp = json.load(open(fixture_path))
    fixture = set(fp["server_notifications"])
    # Tight range catches generator pollution: the buggy whole-doc walk yields 162 (it also
    # swept ClientRequest/ServerRequest method names); the correct ServerNotification-only
    # walk yields 66 in codex 0.141. `> 30` passed vacuously for BOTH — too loose.
    assert 60 <= len(fixture) <= 100, (
        f"fixture has {len(fixture)} methods — expected ~66 (codex 0.141 ServerNotification set). "
        "Far from 66 => re-run gen_notifications.py; the walk must be restricted to the "
        "ServerNotification union only (not whole-doc).")
    # runtime constant == fixture minus error/warning (NOT the 14-name fallback)
    assert cs._KNOWN_NOTIFICATIONS == frozenset(fixture - {"error", "warning"})
    assert cs._KNOWN_NOTIFICATIONS != cs._NOTIFICATION_FALLBACK, (
        "must load the generated fixture, not the stdlib fallback (F7 prod-fallback guard)")
    assert "error" not in cs._KNOWN_NOTIFICATIONS
    assert "warning" not in cs._KNOWN_NOTIFICATIONS
    # the previously-spurious ones are now known
    assert "turn/plan/updated" in cs._KNOWN_NOTIFICATIONS

def test_missing_notification_fixture_warns_and_falls_back(tmp_path, monkeypatch):
    """F7: a missing fixture (prod cache miss) must LOG NOTIFICATION_FIXTURE_MISSING and
    degrade to the fallback — not silently regress without a trace."""
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    result = cs._load_known_notifications(path=str(tmp_path / "nope.json"))
    assert result == cs._NOTIFICATION_FALLBACK
    assert "NOTIFICATION_FIXTURE_MISSING" in logf.read_text()

@pytest.mark.parametrize("bad", [
    '["a","b"]',                         # top-level list, not dict → data.get would AttributeError
    '{"server_notifications": "abc"}',   # string, not list → set("abc") char-soup
    '{"server_notifications": []}',      # empty list
    '{"server_notifications": [1, 2]}',  # non-string entries
    'not json at all {',                 # decode error
])
def test_malformed_notification_fixture_warns_and_falls_back(bad, tmp_path, monkeypatch):
    """F7: a valid-JSON-but-wrong-shape (or undecodable) fixture must warn+fallback, never
    crash at import or load a bogus allowlist."""
    import codex_server as cs
    logf = tmp_path / "d.log"; monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    fx = tmp_path / "codex-notifications.json"; fx.write_text(bad)
    result = cs._load_known_notifications(path=str(fx))   # must not raise
    assert result == cs._NOTIFICATION_FALLBACK
    assert "NOTIFICATION_FIXTURE_MISSING" in logf.read_text()


# ---------------------------------------------------------------------------
# Task 10: fake fidelity — real availableDecisions shape + param keys (#204 3c)
# ---------------------------------------------------------------------------

def test_fake_appserver_approval_uses_real_available_decisions():
    """The fake's approval request must match real codex: string + amendment dict + cancel."""
    import json, subprocess, sys, os, time
    fake = os.path.join(FIXTURES_DIR, "fake_appserver.py")
    env = os.environ.copy(); env["FAKE_SCRIPT"] = "with_approval"
    proc = subprocess.Popen([sys.executable, fake], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        for m in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}},
                  {"id": 2, "method": "thread/start", "params": {"cwd": "/tmp"}},
                  {"id": 3, "method": "turn/start",
                   "params": {"threadId": "T1", "input": [{"type": "text", "text": "hi", "text_elements": []}]}}):
            proc.stdin.write((json.dumps(m) + "\n").encode()); proc.stdin.flush()
        deadline = time.time() + 10
        approval = None
        buf = b""
        while time.time() < deadline and approval is None:
            import select
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not r:
                continue
            buf += os.read(proc.stdout.fileno(), 65536)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                f = json.loads(line)
                if f.get("method") == "item/commandExecution/requestApproval":
                    approval = f
                    # reply so the fake can finish
                    proc.stdin.write((json.dumps({"id": f["id"], "result": {"decision": "cancel"}}) + "\n").encode())
                    proc.stdin.flush()
        assert approval is not None
        params = approval["params"]
        avail = params["availableDecisions"]
        assert "accept" in avail and "cancel" in avail
        assert any(isinstance(x, dict) and "acceptWithExecpolicyAmendment" in x for x in avail)
        # F8 / spec 3c: phantom approvalId gone; real param keys present
        assert "approvalId" not in params, "phantom approvalId must be removed (spec 3c)"
        assert "commandActions" in params, "real codex sends commandActions (spec 3c)"
        assert "proposedExecpolicyAmendment" in params, "real codex sends proposedExecpolicyAmendment (spec 3c)"
    finally:
        proc.stdin.close(); proc.terminate(); proc.wait()


# ---------------------------------------------------------------------------
# Task 11: live-codex invariants (#204) — tools-count isolation, env, auth
# ---------------------------------------------------------------------------

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_env_secret_does_not_leak_to_codex_shell(monkeypatch):
    """A CC-env var must NOT be visible to codex's shell (env allowlist).
    F3 (#215 review): use an EXPLICIT FRESH manager so the child is spawned AFTER the canary
    setenv. Otherwise, in the full `-m slow` suite, the module singleton (already spawned by an
    earlier slow test, BEFORE the canary existed) is warm-reused → the canary was never in the
    child env regardless of whether the allowlist works → the test passes VACUOUSLY.
    #238: the canary is named/valued NEUTRALLY and the prompt avoids 'secret'/'leak'/'reveal'
    framing — the old `CANARY_SECRET_204=leak-me-if-you-can` + 'report verbatim' wording
    intermittently tripped gpt-5.5's safety refusal ('I can't reveal environment secret values'),
    making this LLM-nondeterministic. The allowlist check only needs a specific var to be ABSENT —
    no secret-revealing is required."""
    import codex_server as cs
    from codex_server import codex_run_v2, AppServerManager
    monkeypatch.setenv("CC_ENV_CANARY_204", "cc-env-canary-204-value")
    m = AppServerManager(bin=cs.CODEX)   # fresh child spawns NOW, after the canary setenv
    try:
        r = codex_run_v2({
            "prompt": "Run the shell command `printenv CC_ENV_CANARY_204 || echo ABSENT` "
                      "and report exactly the single line it printed.",
            "mode": "implement", "mcp": "isolated", "sandbox": "workspace-write",
            "approval_policy": "never", "cwd": "/tmp",
        }, manager=m)
        assert "error" not in r, f"turn errored: {r.get('error')}"
        out = r.get("result") or ""
        # prove the command ACTUALLY RAN (else the absence check below is vacuously true).
        assert "ABSENT" in out, f"printenv did not run / report — env test is vacuous: {out!r}"
        assert "cc-env-canary-204-value" not in out, "CC-env var leaked into codex shell"
    finally:
        try:
            if m._child is not None:
                m._child.kill()
        except Exception:
            pass

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_auth_files_untouched_by_a_run():
    """A run must not write ~/.codex/auth.json or config.toml (no relocation/mutation)."""
    import os
    from codex_server import codex_run_v2
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    def _snap(p):
        try: return os.stat(p).st_mtime_ns
        except OSError: return None
    before = {f: _snap(os.path.join(home, f)) for f in ("auth.json", "config.toml")}
    r = codex_run_v2({"prompt": "Say OK.", "mode": "implement", "mcp": "isolated"})
    # R2-F2: prove a REAL run happened — else unchanged mtimes are a vacuous pass.
    assert "error" not in r and r.get("thread_id"), f"run did not really execute: {r}"
    after = {f: _snap(os.path.join(home, f)) for f in ("auth.json", "config.toml")}
    assert before == after, f"auth/config mtimes changed: {before} → {after}"


def _server_tool_counts(manager) -> dict:
    """name → tool count via connection-level mcpServerStatus/list (no thread/start).

    codex 0.141 shape: {"data": [{"name": str, "tools": {<toolName>: Tool}, ...}]}.
    `tools` is a MAP (object), so its tool count is len(dict); the list lives under `data`.
    """
    import codex_server as cs
    mid = manager._next_id()
    manager._write({"id": mid, "method": "mcpServerStatus/list", "params": {}})
    resp = manager._pump_until(
        lambda f: cs.classify(f) == "response" and f.get("id") == mid, timeout=60.0)
    assert resp is not None, "mcpServerStatus/list timed out"
    assert "error" not in resp, f"mcpServerStatus/list errored: {resp.get('error')}"
    # codex 0.141 ListMcpServerStatusResponse: {"data": [McpServerStatus...]}. FAIL LOUD on a
    # shape change rather than silently returning {} (a tolerant `.get("servers")`/`toolCount`
    # fallback would make the F9 isolation test pass vacuously). `name` is required;
    # McpServerStatus.tools is an object MAP (len = tool count).
    raw = resp.get("result") or {}
    servers = raw.get("data") if isinstance(raw, dict) else None
    assert isinstance(servers, list), f"mcpServerStatus/list result.data missing/not-a-list: {raw}"
    counts = {}
    for s in servers:
        name = s.get("name")
        assert name, f"McpServerStatus entry missing required 'name': {s}"
        tools = s.get("tools")
        counts[name] = len(tools) if isinstance(tools, (dict, list)) else 0
    return counts

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_isolated_disables_user_servers_by_tools_count():
    """F9: mcp='isolated' → every user config.toml server reports tools==0. Verify by
    TOOLS-COUNT, NOT name-absence (a disabled server stays in the list)."""
    import codex_server as cs
    config_servers = cs._enumerate_config_mcp_servers()
    if not config_servers:
        pytest.skip("no user MCP servers configured to disable")
    m = cs.AppServerManager(bin=cs.CODEX)
    m.ensure(cs._build_isolation_argv("isolated", config_servers))
    counts = _server_tool_counts(m)
    for name in config_servers:
        # F9: verify by TOOLS-COUNT, not name-absence — a disabled server STAYS in the list.
        # A missing entry is a parse/response failure, NOT "disabled" → must fail, not pass.
        assert name in counts, f"{name} absent from mcpServerStatus/list (parse failure?) — not proof of disable"
        assert counts[name] == 0, f"{name} not disabled: tools={counts[name]} (isolated)"

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_all_loads_user_servers_by_tools_count():
    """F9 counterpart: mcp='all' → at least one user server loads tools (>0), proving the
    isolated test's tools:0 is real disabling, not servers that never had tools."""
    import codex_server as cs
    config_servers = cs._enumerate_config_mcp_servers()
    if not config_servers:
        pytest.skip("no user MCP servers configured")
    m = cs.AppServerManager(bin=cs.CODEX)
    m.ensure(cs._build_isolation_argv("all", config_servers))
    counts = _server_tool_counts(m)
    assert any(counts.get(n, 0) > 0 for n in config_servers), (
        f"no user server loaded tools under 'all' — tools-count test would be vacuous: {counts}")
