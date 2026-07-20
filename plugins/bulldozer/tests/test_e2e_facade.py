"""Live e2e for the facade multiplexer (#344) — REAL codex engine, REAL workers.

The offline suite (test_codex_facade.py) drives scripted fake workers. This one
runs the facade as a subprocess whose workers are the UNCHANGED codex_server.py,
and proves the thing the whole feature exists for: two codex turns issued
concurrently on ONE connection actually OVERLAP (wall ≈ max, not sum).

Self-skips when codex is not installed. Marked slow — it pays codex's cold start
(28-80 s), so allow several minutes.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

MCP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp")
FACADE = os.path.join(MCP_DIR, "codex_facade.py")


def _has_codex():
    return bool(os.environ.get("JAINE_CODEX_BIN") or shutil.which("codex"))


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _has_codex(), reason="codex not installed"),
]


class FacadeProc:
    """Drives the facade over real stdio, exactly as CC would."""

    def __init__(self, extra_env=None):
        # extra_env (NOT env): overrides merged onto the redirect-carrying base
        # via test_env — the helper stopped accepting a caller env (#357 D3b).
        from conftest import test_env
        self.proc = subprocess.Popen(
            [sys.executable, FACADE], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, env=test_env(set_vars=extra_env or {}))
        self.frames = queue.Queue()
        self._stash = []
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                self.frames.put(json.loads(raw))
            except json.JSONDecodeError:
                pass

    def send(self, frame):
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()

    def wait_for_id(self, mid, timeout=300):
        for i, f in enumerate(self._stash):
            if f.get("id") == mid and "method" not in f:
                return self._stash.pop(i)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                f = self.frames.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if f.get("id") == mid and "method" not in f:
                return f
            self._stash.append(f)
        raise AssertionError(f"no reply for id {mid} within {timeout}s")

    def initialize(self):
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18",
                              "capabilities": {},
                              "clientInfo": {"name": "facade-e2e"}}})
        r = self.wait_for_id(0, timeout=30)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _payload(frame):
    return json.loads(frame["result"]["content"][0]["text"])


def _run_frame(mid, prompt, **extra):
    args = {"prompt": prompt, "mcp": "isolated", "mode": "implement",
            "sandbox": "read-only", "approval_policy": "never",
            "effort": "low"}
    args.update(extra)
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": "codex_run", "arguments": args}}


@pytest.fixture
def facade():
    f = FacadeProc()
    yield f
    f.close()


def test_initialize_advertises_the_facade(facade):
    r = facade.initialize()
    assert "FACADE" in r["result"]["instructions"]
    assert r["result"]["serverInfo"]["name"]


def test_two_real_codex_turns_overlap_on_one_connection(facade):
    """THE discriminator: the legacy single bridge serializes two turns (its busy
    guard even REJECTS the second — see the test below). Through the facade the
    two run on separate workers and OVERLAP: wall ≈ max, not sum.

    NB: the engine reports `duration_ms` WITHOUT the cold start — the real cost
    of a call is setup_ms + duration_ms.
    """
    facade.initialize()
    t0 = time.monotonic()
    facade.send(_run_frame(1, "Reply with exactly: ALPHA. Nothing else."))
    facade.send(_run_frame(2, "Reply with exactly: BETA. Nothing else."))
    a = _payload(facade.wait_for_id(1, timeout=420))
    b = _payload(facade.wait_for_id(2, timeout=420))
    wall = time.monotonic() - t0

    assert a.get("status") == "completed", a
    assert b.get("status") == "completed", b
    assert a["thread_id"] != b["thread_id"]

    cost_a = (a["timing"]["setup_ms"] + a["timing"]["duration_ms"]) / 1000.0
    cost_b = (b["timing"]["setup_ms"] + b["timing"]["duration_ms"]) / 1000.0
    print(f"\nfacade e2e: cost1={cost_a:.1f}s cost2={cost_b:.1f}s "
          f"wall={wall:.1f}s sum={cost_a + cost_b:.1f}s max={max(cost_a, cost_b):.1f}s")
    # Overlap: the wall clock tracks the SLOWER call, not the sum of both.
    assert wall < cost_a + cost_b - min(cost_a, cost_b) * 0.5, (
        f"no overlap: wall={wall:.1f}s vs sum={cost_a + cost_b:.1f}s")
    assert wall < max(cost_a, cost_b) * 1.6, (
        f"wall {wall:.1f}s is far above max {max(cost_a, cost_b):.1f}s")


def test_legacy_bridge_rejects_the_second_concurrent_turn():
    """The baseline the facade exists to fix: on the legacy single bridge the
    SECOND concurrent tools/call hits the busy guard. Same two frames, same
    connection — only the kill switch differs."""
    f = FacadeProc(extra_env={"BULLDOZER_FACADE_OFF": "1"})
    try:
        f.initialize()
        f.send(_run_frame(1, "Reply with exactly: ALPHA. Nothing else."))
        time.sleep(0.5)                      # let the first turn take the engine
        f.send(_run_frame(2, "Reply with exactly: BETA. Nothing else."))
        second = _payload(f.wait_for_id(2, timeout=420))
        first = _payload(f.wait_for_id(1, timeout=420))
        assert first.get("status") == "completed", first
        assert "already in flight" in str(second.get("error", "")), second
    finally:
        f.close()


def test_kill_switch_execs_the_legacy_engine():
    """BULLDOZER_FACADE_OFF=1 → the legacy single bridge, byte-identical path."""
    f = FacadeProc(extra_env={"BULLDOZER_FACADE_OFF": "1"})
    try:
        r = f.initialize()
        assert "FACADE" not in (r["result"].get("instructions") or "")
        f.send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        tools = {t["name"] for t in f.wait_for_id(9, timeout=30)["result"]["tools"]}
        assert {"codex_run", "codex_review", "codex_info",
                "codex_approve"} <= tools
    finally:
        f.close()


def test_engine_audit_lines_carry_the_worker_id(facade, tmp_path):
    """The ONE engine touch: BULLDOZER_WORKER=N → `worker=N` on every audit line."""
    log = tmp_path / "codex.log"
    f = FacadeProc(extra_env={"BULLDOZER_CODEX_LOG": str(log)})
    try:
        f.initialize()
        f.send(_run_frame(1, "Reply with exactly: GAMMA. Nothing else."))
        assert _payload(f.wait_for_id(1, timeout=420))["status"] == "completed"
    finally:
        f.close()
    text = log.read_text()
    turn_ok = [ln for ln in text.splitlines() if "TURN_OK" in ln]
    assert turn_ok, text
    assert any("worker=" in ln for ln in turn_ok), turn_ok
