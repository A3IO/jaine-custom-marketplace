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


# ── B2: ws_send must catch connection errors, not just I/O errors ──

def test_b2_ws_send_catches_connection_errors():
    """ws_send must not let ConnectionRefusedError propagate as traceback.
    StubHandler returns tab list with ws://localhost:1 (unreachable WS port)."""
    server, port = start_stub_server()
    try:
        r = run_cdp(["navigate", "http://example.com"], env_override={"CDP_PORT": str(port)})
        assert "Traceback" not in r.stderr, (
            "ws_send should catch connection errors gracefully, got traceback:\n" + r.stderr
        )
        assert r.returncode != 0, "Should fail when WebSocket unreachable"
    finally:
        server.shutdown()


# ── B3+B4: cdp_js must propagate None, callers must not treat "?" as success ──

def test_b3_cdp_js_propagates_none():
    """cdp_js must return None (not {}) when ws_send fails."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cdp_js":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "return {}" not in func_src, (
                "cdp_js must return None on failure, not {} — "
                "callers can't distinguish CDP error from JS undefined"
            )
            assert "return None" in func_src, (
                "cdp_js must explicitly return None when ws_send fails"
            )
            return
    raise AssertionError("cdp_js function not found")


def test_b4_click_fails_on_cdp_error():
    """cmd_click must return nonzero when CDP fails (not print '?' and exit 0)."""
    server, port = start_stub_server()
    try:
        r = run_cdp(["click", "#test"], env_override={"CDP_PORT": str(port)})
        assert r.returncode != 0, (
            "click should fail when WebSocket is unreachable, got rc=0 stdout={}".format(r.stdout)
        )
    finally:
        server.shutdown()


# ── D4: screenshot log must include url= ──

def test_d4_screenshot_log_includes_url():
    """CDP screenshot log call must include url= for analytics."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            cdp_log_lines = [l for l in func_src.splitlines()
                             if 'log("screenshot"' in l and 'channel="cdp"' in l]
            assert cdp_log_lines, "cmd_screenshot must have a CDP log call"
            assert "url=" in cdp_log_lines[0], (
                "CDP screenshot log must include url= field for analytics"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


# ── B9: as_js_main_world must clear stale dataset before injection ──

def test_b9_as_js_main_world_clears_stale_result():
    """as_js_main_world must delete dataset._jresult before injecting new script."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "as_js_main_world":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "delete" in func_src and "_jresult" in func_src, (
                "as_js_main_world must clear dataset._jresult before injection "
                "to prevent stale data from previous calls"
            )
            return
    raise AssertionError("as_js_main_world function not found")


# ── B6: osascript must catch TimeoutExpired ──

def test_b6_osascript_catches_timeout():
    """osascript() must handle subprocess.TimeoutExpired, not let it propagate."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "osascript":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "TimeoutExpired" in func_src or "timeout" in func_src.lower().split("except")[1] if "except" in func_src else False, (
                "osascript must catch TimeoutExpired — Chrome hang should not crash with traceback"
            )
            return
    raise AssertionError("osascript function not found")


# ── B7: cmd_wait must handle non-integer timeout ──

def test_b7_wait_invalid_timeout_friendly_error():
    """cdp.py wait .foo abc must give clean error, not ValueError traceback."""
    r = run_cdp(["wait", ".foo", "abc"], env_override={"CDP_PORT": "19111"})
    assert "Traceback" not in r.stderr, (
        "Non-integer timeout should give friendly error:\n" + r.stderr
    )
    assert r.returncode != 0


# ── B8: AppleScript JS-disabled detection must work in English ──

def test_b8_applescript_detects_english_disabled():
    """osascript() must detect JS-disabled error in both Russian and English."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "osascript":
            src_lines = source.splitlines()[node.lineno - 1 : node.end_lineno]
            condition_lines = [l for l in src_lines if "in err" in l]
            condition_text = " ".join(condition_lines)
            has_russian = "отключено" in condition_text
            has_english = "turned off" in condition_text or "disabled" in condition_text or "not allowed" in condition_text
            assert has_russian and has_english, (
                "osascript error detection must check both Russian and English locale strings. "
                "Condition lines: {}".format(condition_text)
            )
            return
    raise AssertionError("osascript function not found")


# ── B5: native_screenshot must not print multiple window IDs ──

def test_b5_native_screenshot_single_window_id():
    """Quartz window lookup must return exactly one ID, not print() side-effects."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "native_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "[print(" not in func_src, (
                "native_screenshot must not use [print(w[...]) for w in ws][:1] — "
                "print() is a side-effect, [:1] slices the list of Nones, not the iteration. "
                "Use next() or explicit loop with break."
            )
            return
    raise AssertionError("native_screenshot function not found")


# ── B1: as_js_main_world must escape single quotes for AppleScript ──

def test_b1_as_js_main_world_escapes_single_quotes():
    """Single quotes in JS expressions must be escaped for AppleScript textContent='...'."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "as_js_main_world":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert ".replace(" in func_src and "'" in func_src, (
                "as_js_main_world must escape single quotes after json.dumps — "
                "unescaped ' breaks AppleScript textContent='...' wrapping"
            )
            return
    raise AssertionError("as_js_main_world function not found")


# ── NEW: AppleScript fallback and multi-channel tests ──


def test_has_websocket_detection():
    """has_websocket() must exist and return bool."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def has_websocket" in source, "Missing has_websocket() function"
    assert "HAS_WEBSOCKET" in source, "Missing HAS_WEBSOCKET cache variable"


def test_channel_function_exists():
    """channel() must return 'cdp' or 'applescript'."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def channel()" in source, "Missing channel() function"
    assert '"cdp"' in source and '"applescript"' in source, (
        "channel() must return 'cdp' or 'applescript'"
    )


def test_applescript_bridge_exists():
    """as_js_main_world() must exist for DOM injection bridge."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def as_js_main_world" in source, "Missing as_js_main_world() function"
    assert "createElement" in source, (
        "as_js_main_world must use DOM injection (createElement script)"
    )
    assert "dataset" in source, (
        "as_js_main_world must read result via dataset (DOM bridge)"
    )


def test_native_screenshot_exists():
    """native_screenshot() must exist for macOS screencapture fallback."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def native_screenshot" in source, "Missing native_screenshot() function"
    assert "screencapture" in source, "native_screenshot must use macOS screencapture"


def test_screenshot_has_fallback():
    """cmd_screenshot must try native screenshot when websocket unavailable."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "native_screenshot" in func_src, (
                "cmd_screenshot must fallback to native_screenshot when websocket unavailable"
            )
            assert "has_websocket" in func_src, (
                "cmd_screenshot must check has_websocket() to decide channel"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_cmd_click_exists():
    """click command must exist."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_click" in source, "Missing cmd_click() function"
    assert '"click"' in source and "cmd_click" in source, "click not in COMMANDS dict"


def test_cmd_fill_exists():
    """fill command must exist."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_fill" in source, "Missing cmd_fill() function"
    assert '"fill"' in source and "cmd_fill" in source, "fill not in COMMANDS dict"


def test_cmd_fill_dispatches_events():
    """fill must dispatch input+change events for React/Vue compatibility."""
    source = Path(CDP_SCRIPT).read_text()
    assert "dispatchEvent" in source, "cmd_fill must dispatch DOM events after setting value"
    assert "input" in source and "change" in source, (
        "cmd_fill must dispatch both 'input' and 'change' events"
    )


def test_cmd_console_exists():
    """console command must exist."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_console" in source, "Missing cmd_console()"
    assert '"console"' in source and "cmd_console" in source, "console not in COMMANDS"


def test_cmd_network_exists():
    """network command must exist."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_network" in source, "Missing cmd_network()"
    assert '"network"' in source and "cmd_network" in source, "network not in COMMANDS"


def test_cmd_pdf_exists():
    """pdf command must exist."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_pdf" in source, "Missing cmd_pdf()"
    assert "printToPDF" in source, "cmd_pdf must use Page.printToPDF CDP method"


def test_cmd_viewport_exists():
    """viewport command must exist."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_viewport" in source, "Missing cmd_viewport()"


def test_cmd_window_exists():
    """window command must exist with bounds/upper/lower/activate."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_window" in source, "Missing cmd_window()"
    for action in ["bounds", "upper", "lower", "activate"]:
        assert '"{}"'.format(action) in source, (
            "cmd_window must handle '{}' action".format(action)
        )


def test_status_shows_channel():
    """status must report which channel (cdp/applescript) is active."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_status":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "channel" in func_src, (
                "cmd_status must show active channel (cdp or applescript)"
            )
            return
    raise AssertionError("cmd_status function not found")


def test_all_commands_registered():
    """All 17 commands must be in COMMANDS dict."""
    expected = {
        "status", "tabs", "screenshot", "js", "navigate", "open",
        "title", "html", "reload", "wait", "click", "fill",
        "console", "network", "pdf", "viewport", "window",
    }
    source = Path(CDP_SCRIPT).read_text()
    for cmd in expected:
        assert '"{}"'.format(cmd) in source, (
            "'{}' not found in COMMANDS dict".format(cmd)
        )


def test_log_includes_channel():
    """Log entries must include channel= field."""
    source = Path(CDP_SCRIPT).read_text()
    assert 'channel=' in source, "Log calls must include channel= for analytics"


def test_js_fallback_to_applescript():
    """cmd_js must fallback to as_js_main_world when websocket unavailable."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_js":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "as_js_main_world" in func_src, (
                "cmd_js must fallback to as_js_main_world when websocket unavailable"
            )
            assert "has_websocket" in func_src, (
                "cmd_js must check has_websocket() to decide channel"
            )
            return
    raise AssertionError("cmd_js function not found")


def test_navigate_fallback_to_applescript():
    """cmd_navigate must fallback to AppleScript when websocket unavailable."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_navigate":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "as_navigate" in func_src or "has_websocket" in func_src, (
                "cmd_navigate must fallback to AppleScript"
            )
            return
    raise AssertionError("cmd_navigate function not found")


def test_help_shows_all_commands():
    """--help must list all 17 commands."""
    r = run_cdp(["--help"])
    for cmd in ["status", "tabs", "screenshot", "js", "navigate", "open",
                 "title", "html", "reload", "wait", "click", "fill",
                 "console", "network", "pdf", "viewport", "window"]:
        assert cmd in r.stdout, f"--help missing '{cmd}' command"


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
