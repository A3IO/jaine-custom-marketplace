#!/usr/bin/env python3
"""Behavioral tests for cdp.py — error paths and edge cases.
Tests use subprocess to run cdp.py as a CLI tool (no import hacks).
Run: python3 -m pytest tests/test_cdp.py -v
  or: python3 tests/test_cdp.py
"""
import ast
import json
import os
import subprocess
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

CDP_SCRIPT = str(Path(__file__).parent.parent / "skills" / "look" / "scripts" / "cdp.py")


def run_cdp(args, env_override=None, timeout=10):
    env = os.environ.copy()
    env.pop("CDP_PORT", None)
    if env_override:
        env.update(env_override)
    result = subprocess.run(
        [sys.executable, CDP_SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    return result


class StubHandler(BaseHTTPRequestHandler):
    response_body = b'[{"id":"tab1","type":"page","url":"http://localhost","title":"Test","webSocketDebuggerUrl":"ws://localhost:1/tab1"}]'
    status_code = 200

    def do_GET(self):
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, *a):
        pass


def start_stub_server(handler_class=StubHandler, port=0):
    server = HTTPServer(("127.0.0.1", port), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# ── F2: cmd_open must return non-zero when browser is offline ──

def test_f2_cmd_open_returns_nonzero_when_offline():
    r = run_cdp(["open", "http://example.com"], env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, (
        f"cmd_open should return non-zero when browser offline, got rc={r.returncode}\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


# ── F3: invalid CDP_PORT gives friendly error, not ValueError traceback ──

def test_f3_invalid_cdp_port_friendly_error():
    r = run_cdp(["status"], env_override={"CDP_PORT": "not_a_number"})
    assert r.returncode != 0, "Should exit non-zero on bad CDP_PORT"
    assert "ValueError" not in r.stderr, (
        f"Should give friendly error, not raw ValueError traceback:\n{r.stderr}"
    )
    assert "Traceback" not in r.stderr, (
        f"Should not show Python traceback:\n{r.stderr}"
    )


# ── F4: cdp_get handles non-JSON response without crash ──

def test_f4_cdp_get_handles_non_json():
    class HtmlHandler(StubHandler):
        response_body = b"<html>captive portal</html>"

    server, port = start_stub_server(HtmlHandler)
    try:
        r = run_cdp(["status"], env_override={"CDP_PORT": str(port)})
        assert "Traceback" not in r.stderr, (
            f"Non-JSON response should not cause traceback:\n{r.stderr}"
        )
        assert r.returncode != 0 or "OFFLINE" in r.stdout, (
            "Non-JSON should be treated as offline or error"
        )
    finally:
        server.shutdown()


# ── F5: cmd_wait selector escaping must handle backslashes ──

def test_f5_wait_selector_with_backslash():
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_wait":
            src_lines = source.splitlines()
            func_source = "\n".join(
                src_lines[node.lineno - 1 : node.end_lineno]
            )
            assert "json.dumps" in func_source, (
                "cmd_wait should use json.dumps(selector) for safe JS escaping, "
                f"not naive replace. Found:\n{func_source}"
            )
            assert ".replace(" not in func_source, (
                "cmd_wait should NOT use .replace() for selector escaping — "
                "json.dumps handles all edge cases"
            )
            return

    raise AssertionError("cmd_wait function not found in cdp.py")


# ── F1: ws_send must use try/finally for WebSocket cleanup ──

def test_f1_ws_send_has_try_finally():
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ws_send":
            has_try_finally = any(
                isinstance(n, ast.Try) and n.finalbody
                for n in ast.walk(node)
            )
            assert has_try_finally, (
                "ws_send must wrap WebSocket operations in try/finally "
                "to ensure ws.close() is called on exception"
            )
            return

    raise AssertionError("ws_send function not found in cdp.py")


# ── F6: ws_send must check CDP error responses centrally ──

def test_f6_cdp_error_responses_checked():
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ws_send":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '"error"' in func_src, (
                "ws_send should check for CDP error field in response "
                "and surface error message — centralizes error handling for all callers"
            )
            return
    raise AssertionError("ws_send function not found in cdp.py")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"  PASS: {name}")
            passed += 1
        except (AssertionError, Exception) as e:
            print(f"  FAIL: {name} — {e}")
            failed += 1
    print(f"\n=== {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)
