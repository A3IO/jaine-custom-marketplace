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
    env = test_env()
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


def test_native_screenshot_log_includes_size():
    """Native screenshot log must include size= for parity with CDP path."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            native_log_lines = [l for l in func_src.splitlines()
                                if 'log("screenshot"' in l and 'channel="native"' in l]
            assert native_log_lines, "cmd_screenshot must have a native log call"
            assert "size=" in native_log_lines[0], (
                "Native screenshot log must include size= — "
                "without it, the JPEG size savings are unobservable on the fallback path"
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
    """All commands must be in COMMANDS dict (look-facing + verify-core + internal + new)."""
    expected = {
        "status", "tabs", "screenshot", "js", "navigate", "open",
        "title", "html", "reload", "wait", "assert", "click", "fill",
        "console", "network", "pdf", "viewport", "window",
        "normalize-url",
        "ax", "hover", "key", "drag",
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
    """--help must list all agent-facing commands."""
    r = run_cdp(["--help"])
    for cmd in ["status", "tabs", "screenshot", "js", "navigate", "open",
                 "title", "html", "reload", "wait", "assert", "click", "fill",
                 "console", "network", "pdf", "viewport", "window",
                 "ax", "hover", "key", "drag"]:
        assert cmd in r.stdout, f"--help missing '{cmd}' command"


# ── #47: Screenshot optimization — JPEG q80 + deviceScaleFactor 1 ──


def test_screenshot_uses_jpeg_format():
    """CDP screenshot must use JPEG format (not PNG) for smaller file sizes."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '"format": "jpeg"' in func_src, (
                "cmd_screenshot must use format 'jpeg' for CDP capture, not 'png'. "
                "JPEG q80 is 3-5× smaller with zero visual quality loss for Claude."
            )
            assert '"format": "png"' not in func_src, (
                "cmd_screenshot CDP path must not use 'png' format"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_screenshot_jpeg_quality_80():
    """CDP screenshot must specify quality: 80 for JPEG compression."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '"quality": 80' in func_src, (
                "cmd_screenshot must pass quality: 80 to CDP captureScreenshot "
                "(Playwright MCP re-encodes at 80 after resize — proven sweet spot)"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_screenshot_default_path_is_jpg():
    """Default screenshot path must use .jpg extension (not .png)."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            default_lines = [l for l in func_src.splitlines() if "jaine-screenshot" in l]
            assert default_lines, "cmd_screenshot must have a default path"
            assert ".jpg" in default_lines[0], (
                "Default screenshot path must be .jpg (not .png) — "
                "CDP emits JPEG, native_screenshot passes -t to screencapture"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_native_screenshot_passes_format_flag():
    """native_screenshot must pass -t to screencapture so format matches extension.
    Without -t, screencapture defaults to PNG regardless of file extension."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "native_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '"-t"' in func_src, (
                "native_screenshot must pass -t flag to screencapture — "
                "without it, screencapture always writes PNG regardless of extension"
            )
            return
    raise AssertionError("native_screenshot function not found")


def test_viewport_device_scale_factor_1():
    """Viewport must use deviceScaleFactor 1 (not 2) to avoid Retina bloat.
    Claude's vision API downscales to ~1456×819 anyway — 2x wastes 4× pixels."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_viewport":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '"deviceScaleFactor": 1' in func_src, (
                "cmd_viewport must use deviceScaleFactor 1. "
                "Retina 2x produces 2880×1626 but Claude downscales to ~1456×819 — "
                "1440×813 at 1x is near-optimal."
            )
            assert '"deviceScaleFactor": 2' not in func_src, (
                "deviceScaleFactor must not be 2 — Retina bloat wastes 4× pixels"
            )
            return
    raise AssertionError("cmd_viewport function not found")


# ── #46: CDP improvements — console exceptions, wait JS expr, full-page ──


def test_console_captures_runtime_exceptions():
    """cmd_console must enable Runtime domain and listen for exceptionThrown.
    Without this, uncaught TypeError/ReferenceError at page load are invisible."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_console":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "Runtime.enable" in func_src, (
                "cmd_console must enable Runtime domain to capture uncaught exceptions"
            )
            assert "exceptionThrown" in func_src, (
                "cmd_console must listen for Runtime.exceptionThrown events — "
                "uncaught TypeError at parse time is not a Console.messageAdded event"
            )
            return
    raise AssertionError("cmd_console function not found")


def test_wait_js_flag_explicit():
    """cmd_wait must use --js flag for JS expressions (not heuristic).
    JAINE generates commands and always knows the type — no guessing."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_wait":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '"--js"' in func_src, (
                "cmd_wait must support --js flag for explicit JS expression mode"
            )
            assert "querySelector" in func_src, (
                "cmd_wait must use querySelector for CSS selectors (default path)"
            )
            assert "!!(" in func_src, (
                "cmd_wait must use !!(expr) for JS expression path (--js flag)"
            )
            assert "is_js_expr = any(" not in func_src, (
                "cmd_wait must NOT use character-based heuristic — "
                "it misclassifies CSS like 'div > span' and '[data-x=\"y\"]' as JS. "
                "Use explicit --js flag instead."
            )
            return
    raise AssertionError("cmd_wait function not found")


def test_console_exception_null_safe():
    """cmd_console must handle CDP exception: null without crashing.
    throw null / throw 42 can produce exception: null in CDP responses."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_console":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert '.get("exception") or {}' in func_src or \
                   ".get('exception') or {}" in func_src, (
                "cmd_console must use `exc.get('exception') or {}` (not default={}) — "
                "CDP returns exception: null for throw null/throw 42, "
                "dict.get('key', {}) returns None when value is null, not the default"
            )
            return
    raise AssertionError("cmd_console function not found")


def test_console_line_numbers_1_based():
    """cmd_console exception locations must use 1-based line numbers.
    CDP lineNumber is 0-based; JAINE uses line numbers for Read tool navigation."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_console":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "lineNumber" in func_src, "Must read lineNumber from CDP"
            assert "+ 1" in func_src or "+1" in func_src, (
                "cmd_console must convert CDP 0-based lineNumber to 1-based "
                "for consistency with IDE/DevTools display"
            )
            return
    raise AssertionError("cmd_console function not found")


def test_screenshot_fullpage_warns_on_fallback():
    """cmd_screenshot must warn on stderr when --full-page silently degrades.
    JAINE cannot visually tell viewport-only from full-page — needs explicit signal."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            if "--full-page" not in func_src:
                return
            warn_lines = [l for l in func_src.splitlines()
                          if "WARNING" in l or "WARN" in l]
            assert warn_lines, (
                "cmd_screenshot must print WARNING to stderr when --full-page "
                "cannot obtain page dimensions — silent fallback to viewport "
                "produces wrong results that JAINE cannot detect"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_screenshot_supports_full_page():
    """cmd_screenshot must support --full-page flag for below-fold content."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno]
            )
            assert "full" in func_src.lower(), (
                "cmd_screenshot must support a full-page option — "
                "without it, content below viewport is cut off"
            )
            assert "captureBeyondViewport" in func_src or "scrollHeight" in func_src, (
                "Full-page screenshot must use CDP captureBeyondViewport or "
                "viewport resize to scrollHeight"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


# --- Documentation content verification (Feedback Protocol) ---

from conftest import PLUGIN_ROOT  # noqa: E402
from conftest import test_env  # noqa: E402

MARKETPLACE_JSON = PLUGIN_ROOT.parent.parent / ".claude-plugin" / "marketplace.json"


def _get_marketplace_name():
    """Read actual marketplace name from jaine-plugins manifest."""
    with open(MARKETPLACE_JSON) as f:
        return json.load(f)["name"]


def test_claude_md_cache_path_uses_correct_marketplace():
    """CLAUDE.md cache-clear path must use actual marketplace name, not hardcoded."""
    marketplace = _get_marketplace_name()
    claude_md = (PLUGIN_ROOT / "CLAUDE.md").read_text()
    wrong = "jaine-plugins/bulldozer"
    correct = f"{marketplace}/bulldozer"
    assert wrong not in claude_md, (
        f"CLAUDE.md contains wrong cache path '{wrong}' — "
        f"should be '{correct}' (from marketplace.json)"
    )
    assert correct in claude_md, (
        f"CLAUDE.md missing correct cache path '{correct}'"
    )


def test_spec_cache_path_uses_correct_marketplace():
    """Design spec cache-clear path must match marketplace.json."""
    marketplace = _get_marketplace_name()
    specs_dir = PLUGIN_ROOT / "docs" / "superpowers" / "specs"
    spec = (specs_dir / "2026-05-13-skill-feedback-protocol-design.md").read_text()
    assert f"jaine-plugins/bulldozer" not in spec, (
        "Spec contains wrong cache path 'jaine-plugins/bulldozer'"
    )


def test_skill_md_heredoc_unquoted():
    """SKILL.md gh issue heredoc must be unquoted for $(jq...) expansion."""
    for skill in ("look", "check"):
        skill_md = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text()
        assert "<<'ISSUE'" not in skill_md, (
            f"{skill}/SKILL.md uses single-quoted heredoc <<'ISSUE' — "
            "this prevents $(jq...) and $(pwd) expansion. Use <<ISSUE instead."
        )


def test_spec_example_titles_not_stale():
    """Spec example titles should not reference features that already exist."""
    specs_dir = PLUGIN_ROOT / "docs" / "superpowers" / "specs"
    spec = (specs_dir / "2026-05-13-skill-feedback-protocol-design.md").read_text()
    assert "cmd_wait не поддерживает JS expressions" not in spec, (
        "Spec example title references cmd_wait JS support as missing — "
        "but --js flag was added in PR #49. Use a hypothetical example."
    )


# --- Feedback issues #54, #55, #56 ---


def test_issue_54_launch_sh_drops_chrome_dark_mode_flags():
    """launch.sh must not pass --force-dark-mode or WebContentsForceDark (issue #54).

    WebContentsForceDark is Chrome's Auto Dark Mode — it algorithmically recolors
    page content, breaking 'see what the user sees' fidelity on already-dark or
    color-scheme-aware pages.

    --force-dark-mode is verifiably inert on Dark-OS for CDP screenshots
    (browser chrome is never captured) and carries a latent risk on Light-OS
    machines of overriding prefers-color-scheme. Net: drop both.
    """
    launch_sh = (PLUGIN_ROOT / "skills" / "look" / "scripts" / "launch.sh").read_text()
    assert "--force-dark-mode" not in launch_sh, (
        "launch.sh must not pass --force-dark-mode (issue #54). "
        "Zero benefit for CDP screenshots (no browser chrome capture) + "
        "latent prefers-color-scheme risk on Light-OS machines."
    )
    assert "WebContentsForceDark" not in launch_sh, (
        "launch.sh must not enable WebContentsForceDark (issue #54). "
        "Sole cause of content recoloring — algorithmically inverts page "
        "fills/text, so screenshots misrepresent real rendering."
    )


def test_issue_56_skill_md_does_not_pass_raw_arguments_as_url():
    """Quick Invoke must not substitute the full $ARGUMENTS into the URL slot (issue #56).

    launch.sh:11 reads URL as `${1:-about:blank}` — first positional arg verbatim.
    Passing `$ARGUMENTS` containing 'URL + description' produces a malformed URL.
    """
    skill_md = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    bad_patterns = [
        'launch.sh" "$ARGUMENTS"',
        'cdp.py" navigate "$ARGUMENTS"',
    ]
    for pat in bad_patterns:
        assert pat not in skill_md, (
            "SKILL.md must not pass raw $ARGUMENTS to scripts (issue #56). "
            "Found pattern: '{}'. "
            "Quick Invoke must instruct the agent to parse the URL token "
            "out of $ARGUMENTS before passing it to launch.sh/cdp.py.".format(pat)
        )


def test_issue_56_skill_md_instructs_url_parsing():
    """Quick Invoke must explicitly instruct parsing $ARGUMENTS into URL + task description (issue #56)."""
    skill_md = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    # Locate the Quick Invoke section
    import re
    m = re.search(r"## Quick Invoke.*?(?=^## )", skill_md, re.DOTALL | re.MULTILINE)
    assert m, "SKILL.md is missing the 'Quick Invoke' section"
    quick_invoke = m.group(0)
    # The section must explain URL parsing
    must_have = ["URL", "description"]
    for keyword in must_have:
        assert keyword in quick_invoke, (
            "Quick Invoke must mention '{}' to instruct URL parsing (issue #56)".format(keyword)
        )
    # And must describe URL-shape recognition
    url_schemes = ["http://", "https://", "file://"]
    schemes_found = sum(1 for s in url_schemes if s in quick_invoke)
    assert schemes_found >= 2, (
        "Quick Invoke must describe URL-shaped tokens (e.g. http://, https://, file://) "
        "to teach the agent how to recognize URL vs task description (issue #56)"
    )


def test_issue_60_quick_invoke_normalizes_bare_absolute_path():
    """Issue #60 §1: Quick Invoke must teach the agent that a bare absolute path
    that exists on disk is URL-shaped → normalize to file:// (else → about:blank)."""
    import re
    skill_md = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    m = re.search(r"## Quick Invoke.*?(?=^## )", skill_md, re.DOTALL | re.MULTILINE)
    assert m, "SKILL.md is missing the 'Quick Invoke' section"
    quick_invoke = m.group(0)
    assert "absolute" in quick_invoke and "path" in quick_invoke, (
        "Quick Invoke must describe how to handle a bare absolute filesystem path (#60)"
    )
    assert "file://" in quick_invoke, (
        "Quick Invoke must instruct normalizing a bare absolute path to file:// (#60)"
    )
    assert "exist" in quick_invoke, (
        "Quick Invoke must condition normalization on the path existing on disk (#60)"
    )
    # R1-F1: must NOT teach the agent to hand-prefix file:// (that bypasses the
    # scripts' pathlib.as_uri percent-encoding and breaks paths with spaces/#/?).
    assert "as-is" in quick_invoke or "as is" in quick_invoke, (
        "Quick Invoke must tell the agent to pass a bare path through as-is "
        "(scripts normalize with proper encoding), not hand-prefix file:// (#60 R1-F1)"
    )
    assert re.search(r"percent[- ]?encod|as_uri", quick_invoke), (
        "Quick Invoke must note the scripts percent-encode the file:// URI (#60 R1-F1)"
    )


def test_issue_60_clip_origin_documented():
    """Issue #60 §1-minor: SKILL.md must document --clip coordinate origin.

    Empirically (this PR): clip X Y are document/page coordinates, but without
    captureBeyondViewport only the part within the current viewport renders —
    a below-fold region must be scrolled into view before clipping.
    """
    skill_md = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    import re as _re
    assert _re.search(r"document.{0,30}coordinate|document/page coordinate", skill_md), (
        "SKILL.md --clip docs must state X Y are document/page coordinates (#60)"
    )
    assert "captureBeyondViewport" in skill_md, (
        "SKILL.md --clip docs must name captureBeyondViewport as the viewport-limit reason (#60)"
    )
    assert _re.search(r"scroll.{0,40}(into view|viewport)", skill_md), (
        "SKILL.md --clip docs must tell the agent to scroll a below-fold region "
        "into view before clipping (#60)"
    )


def test_issue_55_screenshot_supports_clip_flag():
    """cmd_screenshot must parse --clip X Y W H for region capture (issue #55).

    Region capture is central to UI-detail verification (the skill's main use case).
    CSS-pixel units. Mutually exclusive with --full-page.
    """
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert '"--clip"' in func_src, (
                "cmd_screenshot must parse --clip flag (issue #55). "
                "Without it, region capture requires PIL post-processing."
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_issue_55_screenshot_supports_scale_flag():
    """cmd_screenshot must parse --scale N for output resolution control (issue #55).

    Default = native DPR (Retina 2× preserves detail — useful for UI-detail checks).
    --scale N produces output at N × CSS-pixel resolution via
    `clip.scale = N / window.devicePixelRatio`.

    Implementation note: Emulation.setDeviceMetricsOverride{deviceScaleFactor:1}
    does NOT change Page.captureScreenshot output size — empirically verified.
    The only CDP knob that affects capture pixel dimensions is clip.scale.
    """
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert '"--scale"' in func_src, (
                "cmd_screenshot must parse --scale flag (issue #55) for opt-in resolution control"
            )
            # Must read devicePixelRatio and adjust clip.scale accordingly.
            assert "devicePixelRatio" in func_src, (
                "cmd_screenshot --scale must read window.devicePixelRatio and set "
                "clip.scale = N / devicePixelRatio — this is the only CDP lever "
                "that affects capture output size (setDeviceMetricsOverride does not)."
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_issue_55_screenshot_prints_output_dimensions():
    """cmd_screenshot must print output dimensions to stdout (issue #55).

    Without this, the native-DPR multiplier (Retina 2× = 2880×1626 for a 1440 viewport)
    is a hidden surprise — agents compute crop coordinates against logical pixels and
    capture the wrong region.
    """
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    helper_in_module = "_image_dimensions" in source or "_jpeg_dimensions" in source
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert helper_in_module, (
                "cdp.py must define an image-dimension helper (e.g. _image_dimensions) "
                "to read output W×H without an external dependency (issue #55)"
            )
            assert "_image_dimensions" in func_src or "_jpeg_dimensions" in func_src, (
                "cmd_screenshot must call the image-dimension helper to print actual W×H "
                "to stdout (issue #55)"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


# --- Post-review polish for issue #55 (cmd_screenshot + _image_dimensions) ---
#
# These tests close coverage and silent-failure gaps surfaced by post-merge
# adversarial review. They guard error-path branches and the no-warning
# fallbacks that would silently degrade the "always prints PATH W×H" contract
# and the --scale "produce CSS-pixel output" contract.


# --- Behavioural tests for --clip / --scale arg parsing (run without browser) ---


def test_screenshot_clip_rejects_too_few_args():
    """`screenshot --clip 1 2 3` must exit non-zero with explicit stderr error."""
    r = run_cdp(["screenshot", "/tmp/never.jpg", "--clip", "1", "2", "3"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject 3-arg --clip"
    assert "--clip needs 4 numbers" in r.stderr, (
        "Expected friendly error 'needs 4 numbers', got stderr: {}".format(r.stderr)
    )


def test_screenshot_clip_rejects_non_numeric():
    """`screenshot --clip a b c d` must exit non-zero with friendly error (not ValueError traceback)."""
    r = run_cdp(["screenshot", "/tmp/never.jpg", "--clip", "a", "b", "c", "d"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject non-numeric --clip"
    assert "Traceback" not in r.stderr, "must not leak Python traceback"
    assert "--clip" in r.stderr, "must mention --clip in error"


def test_screenshot_clip_mutex_with_full_page():
    """`screenshot --full-page --clip ...` must exit non-zero with mutex error."""
    r = run_cdp(["screenshot", "/tmp/never.jpg", "--full-page", "--clip", "0", "0", "100", "100"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject --full-page + --clip"
    assert "mutually exclusive" in r.stderr, (
        "Expected mutex error, got: {}".format(r.stderr)
    )


def test_screenshot_scale_requires_value():
    """`screenshot --scale` (no value) must exit non-zero with friendly error."""
    r = run_cdp(["screenshot", "/tmp/never.jpg", "--scale"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject bare --scale"
    assert "--scale needs a numeric arg" in r.stderr, (
        "Expected friendly error, got: {}".format(r.stderr)
    )


def test_screenshot_scale_rejects_non_numeric():
    """`screenshot --scale abc` must exit non-zero with friendly error (not ValueError traceback)."""
    r = run_cdp(["screenshot", "/tmp/never.jpg", "--scale", "abc"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject non-numeric --scale"
    assert "Traceback" not in r.stderr, "must not leak Python traceback"
    assert "--scale" in r.stderr, "must mention --scale"


# --- Structural guards (regressions in invariants) ---


def test_screenshot_scale_zero_dpr_guard_present():
    """cmd_screenshot must guard `native_dpr <= 0` to prevent ZeroDivisionError on weird CDP responses."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "<= 0" in func_src, (
                "cmd_screenshot must include `if native_dpr <= 0` guard before "
                "computing `effective_scale = scale_override / native_dpr`"
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_native_screenshot_path_rejects_clip_and_scale():
    """When websocket is unavailable, --clip/--scale must be rejected explicitly (not silently dropped)."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "require CDP" in func_src, (
                "cmd_screenshot native-fallback path must explicitly reject --clip/--scale "
                "with 'require CDP' message — silent fallback would lose user's intent."
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_screenshot_warns_on_devicepixelratio_failure():
    """cmd_screenshot --scale must WARN on stderr when devicePixelRatio read fails (not silently use 1.0).

    Silent fallback to DPR=1 on Retina inverts the user's --scale intent (output 2× expected size)
    with no diagnostic. The fix must emit an explicit WARNING in the same code path that returns 1.0.
    """
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            # Look for explicit warning about devicePixelRatio fallback
            scale_section = func_src[func_src.find("scale_override is not None"):]
            assert "WARNING: --scale" in scale_section and "devicePixelRatio" in scale_section, (
                "cmd_screenshot --scale block must print 'WARNING: --scale ...devicePixelRatio...' "
                "to stderr when CDP read fails — current silent fallback to 1.0 is misleading."
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_screenshot_warns_on_unparseable_dimensions():
    """cmd_screenshot must warn on stderr when _image_dimensions returns None.

    SKILL.md promises 'always prints PATH W×H'. Silent degradation to bare path
    breaks that contract — agents using crop coords would mis-compute.
    """
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            # The else branch (dims is None) must contain a stderr warning
            dims_section = func_src[func_src.find("_image_dimensions"):]
            assert "WARNING" in dims_section and "dimensions" in dims_section.lower(), (
                "cmd_screenshot must print 'WARNING: ... dimensions ...' to stderr "
                "when _image_dimensions(path) returns None."
            )
            return
    raise AssertionError("cmd_screenshot function not found")


def test_screenshot_log_includes_clip_and_scale():
    """The CDP screenshot log() call must record clip/scale params for observability."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            cdp_log_lines = [l for l in func_src.splitlines()
                             if 'log("screenshot"' in l and 'channel="cdp"' in l]
            assert cdp_log_lines, "cmd_screenshot must have a CDP log call"
            line = cdp_log_lines[0]
            assert "clip=" in line, "CDP log must include clip= field for observability"
            assert "scale=" in line, "CDP log must include scale= field for observability"
            assert "bind=" in line, "CDP log must include bind= field for observability (SP2)"
            return
    raise AssertionError("cmd_screenshot function not found")


# --- Copilot review findings: input validation gates ---


def test_screenshot_clip_rejects_zero_or_negative_dimensions():
    """--clip with width or height <= 0 must exit non-zero at parse time (Copilot finding).

    CDP will accept and silently produce a 1×1 or empty capture; agent has no
    diagnostic. Fail fast at the CLI boundary, BEFORE any browser call —
    detectable by the specific stderr message even when CDP is unreachable.
    """
    r = run_cdp(["screenshot", "/tmp/never.jpg", "--clip", "0", "0", "0", "100"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject zero width"
    assert "positive" in r.stderr.lower() and "--clip" in r.stderr.lower(), (
        "Expected explicit validation error mentioning 'positive' and '--clip', got:\n{}".format(r.stderr)
    )

    r = run_cdp(["screenshot", "/tmp/never.jpg", "--clip", "0", "0", "100", "-5"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0, "must reject negative height"
    assert "positive" in r.stderr.lower() and "--clip" in r.stderr.lower(), (
        "Expected explicit validation error, got:\n{}".format(r.stderr)
    )


def test_screenshot_scale_rejects_zero_or_negative():
    """--scale 0 or negative must exit at parse time with specific stderr (Copilot finding).

    --scale 0 produces clip.scale = 0 → CDP returns empty/0×0 capture with no
    explanation. Negative scale is nonsense. Reject at parse time, BEFORE any
    browser call — detectable by the specific stderr message.
    """
    for bad in ("0", "-1", "-0.5"):
        r = run_cdp(["screenshot", "/tmp/never.jpg", "--scale", bad],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0, "must reject --scale {}".format(bad)
        assert "positive" in r.stderr.lower() and "--scale" in r.stderr.lower(), (
            "Expected explicit validation error for --scale {}, got:\n{}".format(bad, r.stderr)
        )


def test_screenshot_scale_rejects_non_finite():
    """--scale nan/inf must exit at parse time with specific stderr (Copilot finding).

    float('nan') and float('inf') silently propagate into clip.scale and produce
    confusing CDP errors. Reject at parse time, BEFORE any browser call.
    """
    for bad in ("nan", "inf", "-inf"):
        r = run_cdp(["screenshot", "/tmp/never.jpg", "--scale", bad],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0, "must reject --scale {}".format(bad)
        assert ("finite" in r.stderr.lower() or "positive" in r.stderr.lower()) \
            and "--scale" in r.stderr.lower(), (
            "Expected explicit validation error for --scale {}, got:\n{}".format(bad, r.stderr)
        )


def test_scale_reads_devicepixelratio_via_same_ws_connection():
    """--scale must read DPR via direct Runtime.evaluate on current ws_url (Copilot finding).

    Using cdp_js() internally calls get_tab() and opens a separate connection —
    in a multi-tab JAINE Browser the DPR could come from a different tab than
    the one captured. Direct ws_send(ws_url, 'Runtime.evaluate', ...) avoids drift.
    """
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_screenshot":
            func_src = "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
            scale_section = func_src[func_src.find("scale_override is not None"):]
            # End the scale section at next `if clip:` or `Page.captureScreenshot`
            end = scale_section.find("Page.captureScreenshot")
            if end > 0:
                scale_section = scale_section[:end]
            assert "Runtime.evaluate" in scale_section, (
                "--scale branch must use Runtime.evaluate directly (not cdp_js) to read "
                "window.devicePixelRatio — multi-tab consistency with capture ws_url."
            )
            assert "cdp_js" not in scale_section or scale_section.count("cdp_js") == 0, (
                "--scale branch should not call cdp_js; read DPR via ws_send(ws_url, "
                "'Runtime.evaluate', …) on the captured ws_url so it targets the capture's tab."
            )
            return
    raise AssertionError("cmd_screenshot function not found")


# --- Unit tests for _image_dimensions helper ---


def _import_image_dimensions():
    """Helper: import _image_dimensions from the production cdp.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_cdp_under_test", CDP_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._image_dimensions


def test_image_dimensions_returns_none_for_missing_file(tmp_path):
    """_image_dimensions returns None for missing files (OSError handled)."""
    fn = _import_image_dimensions()
    assert fn(str(tmp_path / "nonexistent.jpg")) is None


def test_image_dimensions_returns_none_for_non_image(tmp_path):
    """_image_dimensions returns None for files that are neither JPEG nor PNG."""
    fn = _import_image_dimensions()
    p = tmp_path / "plain.txt"
    p.write_bytes(b"not an image, just some text")
    assert fn(str(p)) is None


def test_image_dimensions_rejects_truncated_png(tmp_path):
    """_image_dimensions returns None for a PNG with only the 8-byte signature (no IHDR)."""
    fn = _import_image_dimensions()
    p = tmp_path / "trunc.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # signature only
    result = fn(str(p))
    assert result is None, (
        "Truncated PNG (signature only) must return None — current code returns (0,0) "
        "because int.from_bytes(b'', 'big') == 0, leading to misleading 'PATH 0×0' output."
    )


def test_image_dimensions_parses_valid_png(tmp_path):
    """_image_dimensions correctly parses a minimal valid PNG header."""
    fn = _import_image_dimensions()
    p = tmp_path / "valid.png"
    # 8-byte signature + IHDR length(4) + 'IHDR'(4) + W(4) + H(4) + 5 trailing bytes
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + b"\x00\x00\x00\x64"  # width = 100
        + b"\x00\x00\x00\x32"  # height = 50
        + b"\x08\x02\x00\x00\x00"
    )
    assert fn(str(p)) == (100, 50)


def test_image_dimensions_rejects_truncated_jpeg(tmp_path):
    """_image_dimensions returns None for a JPEG with the 3-byte signature only."""
    fn = _import_image_dimensions()
    p = tmp_path / "trunc.jpg"
    p.write_bytes(b"\xff\xd8\xff")  # signature, no SOF
    assert fn(str(p)) is None


# --- Self-ignoring .bulldozer/ directory pattern ---


def test_check_skill_uses_self_ignoring_bulldozer_dir():
    """check SKILL.md must contain an ACTIVE write to .bulldozer/.gitignore (self-ignoring pattern).

    A bare mention is not enough — `_in skill_md` would pass even for layout-comment
    references. We must see the actual redirect `> .bulldozer/.gitignore` in the bash
    snippet, so the test fails if Step 1c stops writing the self-ignore file.
    """
    skill_md = (PLUGIN_ROOT / "skills" / "check" / "SKILL.md").read_text()
    assert "> .bulldozer/.gitignore" in skill_md, (
        "check SKILL.md must contain an ACTIVE `echo '*' > .bulldozer/.gitignore` "
        "(self-ignoring pattern matching .remember/). Mention-only references in "
        "layout comments are not sufficient."
    )


def test_check_skill_step1c_path_is_cwd_relative():
    """Step 1c must use cwd-relative .bulldozer/.gitignore, matching REVIEW_DIR convention.

    REVIEW_DIR (Step 1, line ~152) is defined before PROJECT_ROOT is resolved
    (Step 1b). All paths in the flow follow this convention. Using $PROJECT_ROOT
    in Step 1c only creates inconsistency — Step 2 and downstream scripts
    (log-round.sh, update-state.py) all assume `cwd-relative .bulldozer/`.
    """
    skill_md = (PLUGIN_ROOT / "skills" / "check" / "SKILL.md").read_text()
    step_1c_start = skill_md.find("Self-ignoring `.bulldozer/`")
    step_1c_end = skill_md.find("**2. Build the round prompt**", step_1c_start)
    assert step_1c_start > 0 and step_1c_end > step_1c_start, (
        "Could not locate Step 1c block"
    )
    step_1c = skill_md[step_1c_start:step_1c_end]
    assert "$PROJECT_ROOT/.bulldozer" not in step_1c, (
        "Step 1c must not use $PROJECT_ROOT/.bulldozer — REVIEW_DIR is cwd-relative, "
        "so this creates a path mismatch when cwd != PROJECT_ROOT."
    )


def test_check_skill_warning_does_not_suggest_project_gitignore():
    """Step 1c WARNING fallback must not contradict the new self-ignoring flow.

    The previous WARNING said "Add '.bulldozer/' to your project's .gitignore
    manually" — this is exactly what the new pattern is designed to avoid (and
    what the Common Mistakes table explicitly forbids). Drop the suggestion.
    """
    skill_md = (PLUGIN_ROOT / "skills" / "check" / "SKILL.md").read_text()
    step_1c_start = skill_md.find("Self-ignoring `.bulldozer/`")
    step_1c_end = skill_md.find("**2. Build the round prompt**", step_1c_start)
    step_1c = skill_md[step_1c_start:step_1c_end]
    assert "project's .gitignore" not in step_1c, (
        "WARNING in Step 1c suggests editing the project's .gitignore — that "
        "contradicts the new self-ignoring flow and the Common Mistakes row."
    )


def test_check_skill_does_not_modify_project_gitignore():
    """check SKILL.md must NOT modify the consumer's project-level .gitignore.

    Previous behaviour appended `.bulldozer/` to PROJECT_ROOT/.gitignore. This is
    intrusive and the line lingers if the user deletes the plugin. Replaced
    by self-ignoring .bulldozer/.gitignore.
    """
    skill_md = (PLUGIN_ROOT / "skills" / "check" / "SKILL.md").read_text()
    # The exact append-to-project pattern from before
    bad_patterns = [
        "echo '.bulldozer/' >>",
        '"$PROJECT_ROOT/.gitignore"',
    ]
    for pat in bad_patterns:
        assert pat not in skill_md, (
            "check SKILL.md should not modify the project's top-level .gitignore. "
            "Found legacy pattern: {!r}".format(pat)
        )


# --- Unit tests for normalize_url helper (issue #60 §1) ---


def _import_normalize_url():
    """Helper: import normalize_url from the production cdp.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_cdp_under_test", CDP_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.normalize_url


def test_normalize_url_bare_existing_path_to_file_uri(tmp_path):
    """A bare absolute path that exists on disk becomes a file:// URI."""
    fn = _import_normalize_url()
    p = tmp_path / "page.html"
    p.write_text("<html></html>")
    result = fn(str(p))
    assert result == p.as_uri(), (
        "bare existing absolute path must normalize to its file:// URI "
        "(got {!r})".format(result)
    )
    assert result.startswith("file://")


def test_normalize_url_nonexistent_bare_path_verbatim(tmp_path):
    """A bare absolute path that does NOT exist is left verbatim (CDP will reject it)."""
    fn = _import_normalize_url()
    missing = str(tmp_path / "nope.html")
    assert fn(missing) == missing


def test_normalize_url_http_verbatim():
    """http:// URLs pass through unchanged."""
    fn = _import_normalize_url()
    assert fn("http://localhost:9401/x") == "http://localhost:9401/x"


def test_normalize_url_https_verbatim():
    """https:// URLs pass through unchanged."""
    fn = _import_normalize_url()
    assert fn("https://example.com/a") == "https://example.com/a"


def test_normalize_url_file_uri_idempotent():
    """An already-file:// URL is not double-normalized (does not start with '/')."""
    fn = _import_normalize_url()
    assert fn("file:///tmp/x.html") == "file:///tmp/x.html"


def test_normalize_url_host_port_verbatim():
    """A host:port/... token is left verbatim (does not start with '/')."""
    fn = _import_normalize_url()
    assert fn("localhost:9401/dashboard.html") == "localhost:9401/dashboard.html"


def test_normalize_url_relative_path_verbatim():
    """A relative path (no leading slash) is left verbatim — scope is absolute paths only."""
    fn = _import_normalize_url()
    assert fn("some/rel/path.html") == "some/rel/path.html"


def test_normalize_url_encodes_spaces(tmp_path):
    """A bare path with spaces is percent-encoded by as_uri() (naive concat would be invalid)."""
    fn = _import_normalize_url()
    p = tmp_path / "my page.html"
    p.write_text("x")
    result = fn(str(p))
    assert "%20" in result, "space must be percent-encoded, got {!r}".format(result)
    assert " " not in result


def test_normalize_url_directory_passes_through_verbatim(tmp_path):
    """A directory is NOT a viewable file — only regular files normalize (os.path.isfile).
    A bare dir passes through verbatim (CDP will reject it; we don't open dir listings)."""
    fn = _import_normalize_url()
    assert fn(str(tmp_path)) == str(tmp_path)


def test_normalize_url_special_file_passes_through_verbatim():
    """Device/FIFO/socket nodes exist but are not regular files — must NOT be normalized
    (a FIFO would hang Chrome, /dev/null renders blank). Guard on os.path.isfile, not exists."""
    fn = _import_normalize_url()
    import os
    if not os.path.exists("/dev/null"):
        import pytest
        pytest.skip("/dev/null not present")
    assert os.path.exists("/dev/null") and not os.path.isfile("/dev/null")
    assert fn("/dev/null") == "/dev/null", "special files must pass through verbatim, not become file://"


def _func_source(func_name):
    """Return the source text of a top-level function in cdp.py."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return "\n".join(source.splitlines()[node.lineno - 1 : node.end_lineno])
    raise AssertionError("{} function not found in cdp.py".format(func_name))


def test_cmd_navigate_calls_normalize_url():
    """cmd_navigate must route its URL arg through normalize_url (defense-in-depth, #60)."""
    src = _func_source("cmd_navigate")
    assert "normalize_url(" in src, (
        "cmd_navigate must normalize the bare-path URL via normalize_url(args[0]) "
        "so a raw /tmp/x.html doesn't reach Page.navigate as an invalid URL.\nFound:\n" + src
    )


def test_cmd_open_calls_normalize_url():
    """cmd_open must route its URL arg through normalize_url (#60)."""
    src = _func_source("cmd_open")
    assert "normalize_url(" in src, (
        "cmd_open must normalize the bare-path URL via normalize_url(args[0]).\nFound:\n" + src
    )


def test_normalize_url_registered_as_command():
    """#60 R1-F1/altitude: normalize_url is exposed as a CLI subcommand so launch.sh can
    call it (single source of truth) instead of re-implementing the rule in bash."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_normalize_url" in source, "cdp.py must define cmd_normalize_url"
    assert '"normalize-url"' in source or "'normalize-url'" in source, (
        "cdp.py COMMANDS must register the 'normalize-url' subcommand"
    )


def test_cmd_normalize_url_behavioral(tmp_path):
    """`cdp.py normalize-url PATH` prints the normalized URL (the exact value launch.sh uses)."""
    p = tmp_path / "a b#c.html"
    p.write_text("x")
    r = run_cdp(["normalize-url", str(p)])
    assert r.returncode == 0, "normalize-url failed: {}".format(r.stderr)
    assert r.stdout.strip() == p.as_uri(), (
        "normalize-url must print the percent-encoded file:// URI, got {!r}".format(r.stdout)
    )
    # non-file passes through verbatim
    r2 = run_cdp(["normalize-url", "http://localhost:9401/x"])
    assert r2.stdout.strip() == "http://localhost:9401/x"


def test_plugin_json_has_repository_and_homepage():
    """Issue #60 §2: plugin.json exposes repository + homepage so the feedback
    target (A3IO/jaine-plugins) is machine-discoverable, not buried in SKILL.md prose."""
    pj_path = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
    pj = json.loads(pj_path.read_text())  # also asserts valid JSON
    assert "repository" in pj, "plugin.json must declare a repository URL (#60 §2)"
    assert "A3IO/jaine-plugins" in pj["repository"], (
        "repository must point to the feedback/source repo A3IO/jaine-plugins, "
        "got {!r}".format(pj.get("repository"))
    )
    assert pj.get("homepage"), "plugin.json must declare a non-empty homepage URL (#60 §2)"
    # no regression on the pre-existing required fields
    for field in ("name", "version", "description", "author"):
        assert field in pj, "plugin.json lost pre-existing field {!r}".format(field)


def test_launch_sh_delegates_to_cdp_normalize_url():
    """#60 R1-F1/altitude: launch.sh must call `cdp.py normalize-url` (single source of
    truth) rather than re-implementing the as_uri rule inline. Guards on a leading slash
    (cheap skip for http URLs) and keeps the original URL if the call yields nothing."""
    launch = (PLUGIN_ROOT / "skills" / "look" / "scripts" / "launch.sh").read_text()
    assert "normalize-url" in launch, (
        "launch.sh must delegate to `cdp.py normalize-url` (single source of truth)"
    )
    assert '"$URL" == /*' in launch, "launch.sh should cheap-skip non-slash URLs"
    assert "as_uri()" not in launch, (
        "as_uri() now lives only in cdp.py normalize_url — launch.sh must not duplicate it"
    )
    # R1-F1 (#1): empty result from the python3 call must NOT blank the URL
    assert '-n "$normalized"' in launch or "-n \"${normalized}\"" in launch, (
        "launch.sh must keep the original URL if normalize-url returns empty "
        "(python3 missing/failed) instead of launching Chrome with a blank URL"
    )


def test_launch_sh_normalization_matches_cdp_subcommand(tmp_path):
    """R1-F1 behavioral (non-tautological): the REAL `cdp.py normalize-url` subcommand
    that launch.sh invokes must produce the same percent-encoded URI as normalize_url."""
    p = tmp_path / "a b#c.html"
    p.write_text("x")
    cdp_uri = _import_normalize_url()(str(p))
    subcommand_out = run_cdp(["normalize-url", str(p)]).stdout.strip()
    assert subcommand_out == cdp_uri == p.as_uri(), (
        "cdp.py normalize-url (what launch.sh calls) must equal normalize_url "
        "(got subcommand={!r} fn={!r})".format(subcommand_out, cdp_uri)
    )
    assert "%23" in cdp_uri and "%20" in cdp_uri, "reserved chars must be percent-encoded"


def test_ws_send_seq_single_connection():
    """ws_send_seq must exist and open exactly ONE websocket connection
    (press+release share it) while iterating the call sequence."""
    source = Path(CDP_SCRIPT).read_text()
    assert "def ws_send_seq" in source, "Missing ws_send_seq helper"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ws_send_seq":
            body = ast.get_source_segment(source, node)
            assert body.count("create_connection") == 1, \
                "ws_send_seq must open exactly one connection for the whole sequence"
            assert "for " in body, "ws_send_seq must iterate over the call sequence"
            return
    raise AssertionError("ws_send_seq not found in cdp.py")


def test_cmd_click_uses_trusted_input():
    """cmd_click must dispatch a trusted CDP Input event, force instant scroll,
    hit-test, and retain an el.click() fallback."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_click":
            body = ast.get_source_segment(source, node)
            assert "Input.dispatchMouseEvent" in body, "cmd_click must use CDP Input.dispatchMouseEvent"
            assert "ws_send_seq" in body, "cmd_click must use ws_send_seq for press+release"
            assert "behavior:'instant'" in body, "cmd_click must force instant scroll before measuring"
            assert "elementFromPoint" in body, "cmd_click must hit-test via elementFromPoint"
            assert "el.click()" in body, "cmd_click must retain the el.click() fallback"
            return
    raise AssertionError("cmd_click not found in cdp.py")


def test_cmd_click_uses_same_ws_url_for_measure_and_dispatch():
    """cmd_click's websocket path must capture ONE ws_url and use direct
    Runtime.evaluate (NOT cdp_js, which issues a single evaluate and can't express
    the press+release sequence) for measure + fallback. Mirrors the screenshot
    same-target guard test_scale_reads_devicepixelratio_via_same_ws_connection."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_click":
            # inspect CALLS (not raw source) so a comment mentioning cdp_js does
            # not self-defeat the assertion (R2-F1).
            called = {n.func.id for n in ast.walk(node)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "cdp_js" not in called, (
                "cmd_click must NOT call cdp_js (a single Runtime.evaluate can't express "
                "the press+release); use ws_send_seq on the captured ws_url instead."
            )
            assert "ws_send_seq" in called, "cmd_click must dispatch via ws_send_seq"
            body = ast.get_source_segment(source, node)
            assert "ws_send_seq(ws_url" in body, \
                "cmd_click must pass the captured ws_url to ws_send_seq"
            assert "Runtime.evaluate" in body, \
                "cmd_click must use direct Runtime.evaluate for measure/fallback"
            return
    raise AssertionError("cmd_click not found in cdp.py")


def _load_cdp_module():
    """Load cdp.py as a throwaway module (mirrors test_e2e.py's _load_image_dimensions
    — keeps unit tests in sync with the source of truth, and lets tests monkeypatch
    module globals like has_websocket/osascript/ws_send to drive cmd_* offline)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_cdp_under_test", CDP_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_applescript_bounds_normalizes_to_contract():
    """AppleScript `x1, y1, x2, y2` → CDP contract `left,top,width,height`."""
    fn = _load_cdp_module()._applescript_bounds
    assert fn("100, 100, 1540, 1000") == "100,100,1440,900"


def test_applescript_bounds_rejects_malformed():
    """Malformed AppleScript output fails loud (ValueError), no silent fallback."""
    fn = _load_cdp_module()._applescript_bounds
    raised = False
    try:
        fn("not bounds at all")
    except ValueError:
        raised = True
    assert raised, "_applescript_bounds must raise ValueError on malformed input"


def test_cmd_window_bounds_uses_cdp():
    """window bounds must use CDP Browser.getWindowForTarget (headless-capable),
    with the AppleScript fallback normalized via _applescript_bounds (sub-project B)."""
    source = Path(CDP_SCRIPT).read_text()
    assert "Browser.getWindowForTarget" in source, (
        "cmd_window bounds must call CDP Browser.getWindowForTarget"
    )
    assert "_applescript_bounds" in source, (
        "cmd_window AppleScript fallback must normalize bounds via _applescript_bounds"
    )


def test_cmd_window_applescript_fallback_normalizes():
    """cmd_window's AppleScript fallback (websocket absent) must ITSELF print the
    normalized left,top,width,height contract — not raw AppleScript output (B.2:
    both channels share one contract). The structural test above only proves the
    symbols exist; this drives cmd_window end-to-end with a stubbed osascript."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: False
    mod.osascript = lambda script: "100, 100, 1540, 1000"
    mod.log = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cmd_window(["bounds"])
    assert rc == 0, "fallback should succeed, got rc={}".format(rc)
    assert buf.getvalue().strip() == "100,100,1440,900", (
        "AppleScript fallback must print normalized contract, got: {!r}".format(buf.getvalue())
    )


def test_cmd_window_bounds_rejects_unexpected_cdp_response_shape():
    """A malformed CDP response (e.g. {"result": None}) must fail loud (rc 1), not
    raise an AttributeError traceback — the shape guard must be isinstance-safe."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: True
    mod.get_tab = lambda url_filter=None: {"webSocketDebuggerUrl": "ws://x", "id": "T"}
    mod.ws_send = lambda ws, method, params=None: {"result": None}
    mod.osascript = lambda script: "0, 0, 1, 1"  # pre-fix code falls here → rc 0 → RED
    mod.log = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cmd_window(["bounds"])
    assert rc == 1, "malformed CDP response must fail loud (rc 1), got rc={}".format(rc)


def test_cmd_window_bounds_cdp_happy_path():
    """The CDP success path prints exactly `left,top,width,height` from
    result.bounds — an offline lock on the format string (the structural test
    only greps symbols; the malformed test stubs an error shape; only the e2e,
    excluded from the default run, otherwise exercises the CDP happy path, so a
    field transposition like left<->top would ship undetected offline)."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: True
    mod.get_tab = lambda url_filter=None: {"webSocketDebuggerUrl": "ws://x", "id": "T"}
    mod.ws_send = lambda ws, method, params=None: {
        "result": {"windowId": 1,
                   "bounds": {"left": 10, "top": 20, "width": 1440, "height": 900,
                              "windowState": "normal"}}
    }
    mod.log = lambda *a, **k: None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.cmd_window(["bounds"])
    assert rc == 0, "CDP happy path should succeed, got rc={}".format(rc)
    assert buf.getvalue().strip() == "10,20,1440,900", (
        "CDP path must print left,top,width,height, got: {!r}".format(buf.getvalue())
    )


def test_applescript_bounds_rejects_wrong_count():
    """A wrong NUMBER of values (vs a non-integer value) hits the len guard with
    the 'expected 4' message — the count check runs before int conversion."""
    fn = _load_cdp_module()._applescript_bounds
    raised = False
    try:
        fn("1, 2, 3")
    except ValueError as e:
        raised = True
        assert "expected 4" in str(e), "wrong-count error should say 'expected 4', got: {}".format(e)
    assert raised, "_applescript_bounds must raise ValueError on wrong value count"


# ── sub-C: --target tab pinning ──

TWO_TABS = [
    {"id": "AAAaaa111222333", "type": "page", "url": "http://localhost/a?look=tgtA",
     "webSocketDebuggerUrl": "ws://x/A"},
    {"id": "BBBbbb444555666", "type": "page", "url": "http://localhost/b?look=tgtB",
     "webSocketDebuggerUrl": "ws://x/B"},
]


def _stub_tabs(mod, tabs):
    """Point a loaded cdp.py module's cdp_get at a fixed /json/list (no browser)."""
    mod.cdp_get = lambda path: tabs


def _expect_systemexit(fn):
    """Run fn(); return True iff it raised SystemExit with a non-zero code."""
    try:
        fn()
    except SystemExit as e:
        return e.code not in (0, None)
    return False


def test_get_tab_none_returns_first_page():
    """No selector → first page (backward-compat lock; passes pre-impl too)."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab()["id"] == "AAAaaa111222333"


def test_get_tab_exact_id():
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab("BBBbbb444555666")["id"] == "BBBbbb444555666"


def test_get_tab_unique_id_prefix():
    """A 12-char prefix (what tabs/status/open display) resolves to that tab."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab("BBBbbb444555")["id"] == "BBBbbb444555666"  # 12-char prefix


def test_get_tab_url_substring():
    """Backward-compat lock: a url substring resolves (subsumes old url_filter)."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert mod.get_tab("look=tgtB")["id"] == "BBBbbb444555666"


def test_get_tab_ambiguous_prefix_fails_loud():
    """A ≥12-char prefix matching ≥2 tabs exits non-zero (never silently picks one)."""
    mod = _load_cdp_module()
    _stub_tabs(mod, [
        {"id": "SHAREDPREFIX01aaaa", "type": "page", "url": "http://x/1", "webSocketDebuggerUrl": "ws://1"},
        {"id": "SHAREDPREFIX01bbbb", "type": "page", "url": "http://x/2", "webSocketDebuggerUrl": "ws://2"},
    ])
    assert _expect_systemexit(lambda: mod.get_tab("SHAREDPREFIX01")), \
        "ambiguous id-prefix must fail loud (SystemExit non-zero)"


def test_get_tab_short_prefix_not_an_id_match():
    """A <12-char selector is NOT treated as an id prefix (the displayed id is 12 chars)
    — it falls through to url-substring, so a 1-3 char string can't silently pin a tab
    by a too-short unique id prefix (R1-F4). "BBB" uniquely prefixes BBBbbb444555666's
    id but is <12 chars and not a url substring → fail loud (no match)."""
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert _expect_systemexit(lambda: mod.get_tab("BBB")), \
        "a <12-char id-prefix must NOT resolve as an id — fall through to url, then fail loud"


def test_get_tab_ambiguous_url_fails_loud():
    """A url substring matching ≥2 tabs is ambiguous → fail loud (never pick first)."""
    mod = _load_cdp_module()
    _stub_tabs(mod, [
        {"id": "id000000000001", "type": "page", "url": "http://x/p?dup=Z", "webSocketDebuggerUrl": "ws://1"},
        {"id": "id000000000002", "type": "page", "url": "http://x/q?dup=Z", "webSocketDebuggerUrl": "ws://2"},
    ])
    assert _expect_systemexit(lambda: mod.get_tab("dup=Z")), \
        "ambiguous url substring must fail loud"


def test_get_tab_no_match_fails_loud():
    mod = _load_cdp_module(); _stub_tabs(mod, TWO_TABS)
    assert _expect_systemexit(lambda: mod.get_tab("zzz-no-such")), \
        "unknown selector must fail loud (SystemExit non-zero)"


def test_main_target_requires_websocket():
    """--target on the AppleScript fallback (no websocket) fails loud BEFORE dispatch."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.main(["--target", "abc", "title"])
    assert rc == 1, "expected rc 1, got {}".format(rc)
    assert "--target requires" in err.getvalue(), \
        "must explain --target needs websocket, got: {!r}".format(err.getvalue())


def test_main_target_missing_selector():
    """`--target` with no following value fails loud (not an IndexError)."""
    import io, contextlib
    mod = _load_cdp_module()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.main(["--target"])
    assert rc == 1
    assert "requires a selector" in err.getvalue()


def test_main_target_strips_to_help():
    """--target is global: `--target X --help` consumes the flag and still prints help
    (the flag is NOT mistaken for the command). Named with the `main_target` prefix so
    `-k "main_target or main_tab"` selects it WITHOUT also matching the existing
    `test_*_as_js_main_world_*` tests (a bare `-k main_` would — R1-F3)."""
    import io, contextlib
    mod = _load_cdp_module()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(["--target", "X", "--help"])
    assert rc == 0
    assert "cdp.py" in out.getvalue(), "help text should print"


def test_main_tab_is_alias_for_target():
    """--tab is an accepted alias (the websocket gate proves it was parsed)."""
    import io, contextlib
    mod = _load_cdp_module()
    mod.has_websocket = lambda: False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = mod.main(["--tab", "abc", "title"])
    assert rc == 1 and "--target requires" in err.getvalue(), \
        "--tab must be parsed like --target"


def _capture_ws_url(mod):
    """Stub ws_send to record the ws_url it was called with; return a benign OK result."""
    seen = {}
    def fake_ws_send(ws_url, method, params=None):
        seen["ws_url"] = ws_url
        return {"result": {"result": {"value": "ok"}}}
    mod.ws_send = fake_ws_send
    return seen


def test_cdp_js_requires_ws_url():
    """cdp_js no longer self-resolves get_tab(); ws_url is a REQUIRED param (C.2 pin).
    Signature-based so the RED is offline-deterministic — a call-based check
    (`mod.cdp_js("1+1")`) would execute the OLD 1-arg body's `get_tab()`→`cdp_get()`,
    which hits the live CDP port and may `sys.exit(1)`, making the RED env-dependent
    (R1-F2). Inspecting the signature touches no network."""
    import inspect
    mod = _load_cdp_module()
    params = inspect.signature(mod.cdp_js).parameters
    assert "ws_url" in params, "cdp_js must take an explicit ws_url param"
    assert params["ws_url"].default is inspect.Parameter.empty, \
        "ws_url must be a REQUIRED arg (no default) so a caller can't forget to thread it"


def test_cmd_title_pins_target_tab():
    """cmd_title (an indirect cdp_js caller) must drive the PINNED tab, not the first."""
    import io, contextlib
    mod = _load_cdp_module()
    _stub_tabs(mod, TWO_TABS)
    mod.has_websocket = lambda: True
    mod.TARGET = "BBBbbb444555666"
    mod.log = lambda *a, **k: None
    seen = _capture_ws_url(mod)
    with contextlib.redirect_stdout(io.StringIO()):
        mod.cmd_title([])
    assert seen.get("ws_url") == "ws://x/B", \
        "cmd_title must use the pinned tab's ws_url, got {!r}".format(seen.get("ws_url"))


def test_no_unpinned_tab_resolution():
    """C-acceptance: every get_tab( CALL passes the global TARGET selector, and every
    cdp_js( CALL threads an explicit ws_url — no cmd_* resolves a tab without the pin.
    Arity alone is too weak: get_tab(None) or get_tab(<literal>) would satisfy a count
    check yet still drift to the wrong tab (R1-F1), so get_tab must be called with the
    Name `TARGET`. AST-based (not text) so a comment can't self-defeat it."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "get_tab":
                pinned = (len(node.args) >= 1
                          and isinstance(node.args[0], ast.Name)
                          and node.args[0].id == "TARGET")
                if not pinned:
                    offenders.append("get_tab() not threaded with TARGET @L{}".format(node.lineno))
            if node.func.id == "cdp_js" and (len(node.args) + len(node.keywords)) < 2:
                offenders.append("cdp_js(...) missing explicit ws_url @L{}".format(node.lineno))
    assert not offenders, "un-pinned tab resolution (selector not threaded): {}".format(offenders)


def test_cmd_navigate_pins_target_tab():
    """cmd_navigate (a direct get_tab caller) must drive the PINNED tab."""
    import io, contextlib
    mod = _load_cdp_module()
    _stub_tabs(mod, TWO_TABS)
    mod.has_websocket = lambda: True
    mod.TARGET = "BBBbbb444555666"
    mod.log = lambda *a, **k: None
    seen = _capture_ws_url(mod)
    with contextlib.redirect_stdout(io.StringIO()):
        mod.cmd_navigate(["http://localhost/x"])
    assert seen.get("ws_url") == "ws://x/B", \
        "cmd_navigate must use the pinned tab's ws_url, got {!r}".format(seen.get("ws_url"))


# ── SP1: CHROME_APP_NAME parameterization (spec §4.2) ──

def _import_cdp(env_override=None):
    """Import cdp.py as a fresh module in a child interpreter and print CHROME_APP.

    Constants like CHROME_APP are bound at import; mutating os.environ around an
    in-process exec_module would leak across tests — so run the import in a child
    interpreter (keeps the file's subprocess-CLI convention)."""
    code = (
        "import importlib.util; "
        "spec = importlib.util.spec_from_file_location('cdp_mod', {!r}); "
        "m = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(m); "
        "print(m.CHROME_APP)"
    ).format(CDP_SCRIPT)
    env = test_env()
    env.pop("CHROME_APP_NAME", None)
    if env_override:
        env.update(env_override)
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=10, env=env)


def test_chrome_app_name_env_overrides():
    r = _import_cdp({"CHROME_APP_NAME": "Google Chrome for Testing"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Google Chrome for Testing"


def test_chrome_app_default_is_stock():
    r = _import_cdp()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Google Chrome"


def test_chrome_app_name_with_quote_fails_loud():
    """cdp.py applies the same CHROME_APP_NAME guard launch.sh has — the value is
    spliced into AppleScript string literals (injection surface)."""
    r = _import_cdp({"CHROME_APP_NAME": 'Evil " App'})
    assert r.returncode != 0
    assert "CHROME_APP_NAME" in r.stderr


# ── SP1: hole B — native_screenshot owner match ──

class _FakePgrep:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_chrome_pid_for_port_returns_first_pid():
    m = _load_cdp_module()
    calls = {}

    def fake_runner(cmd, **kw):
        calls["cmd"] = cmd
        return _FakePgrep(returncode=0, stdout="4242\n4300\n")

    assert m._chrome_pid_for_port(9355, _runner=fake_runner) == 4242
    # Anchored ERE: port 9355 must not match 93550 (mirrors launch.sh KILL_MATCH).
    # The "--" separator is REQUIRED: the pattern starts with "--" and BSD pgrep
    # without it errors rc=2 (usage) → the helper would silently name-fallback.
    assert calls["cmd"][0] == "pgrep"
    assert calls["cmd"][1] == "-f"
    assert calls["cmd"][2] == "--"
    assert calls["cmd"][3] == r"--remote-debugging-port=9355($|[[:space:]])"


def test_chrome_pid_for_port_none_when_no_match():
    m = _load_cdp_module()
    assert m._chrome_pid_for_port(
        9355, _runner=lambda *a, **k: _FakePgrep(returncode=1)) is None


def test_chrome_pid_for_port_none_on_oserror():
    m = _load_cdp_module()

    def boom(*a, **k):
        raise OSError("pgrep missing")

    assert m._chrome_pid_for_port(9355, _runner=boom) is None


def test_native_screenshot_owner_match_is_not_prefix_substring():
    """Structural: the 'Google' substring collision (hole B) is gone."""
    text = Path(CDP_SCRIPT).read_text()
    assert "split()[0]" not in text
    assert "kCGWindowOwnerPID" in text


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


# ── #188: surrogate-safe stdout ──


def test_issue_188_surrogate_safe_reconfigure_in_main():
    """main() must reconfigure stdout for surrogate safety (§5.3)."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "reconfigure" in func_src or "errors" in func_src, (
                "main() must reconfigure stdout for surrogate safety (§5.3)")
            return
    raise AssertionError("main function not found")


# ── AX command (§3) ──


def test_cmd_ax_registered():
    """cmd_ax must be in COMMANDS dict."""
    source = Path(CDP_SCRIPT).read_text()
    assert '"ax"' in source, "ax not in COMMANDS"


def test_cmd_ax_exists():
    source = Path(CDP_SCRIPT).read_text()
    assert "def cmd_ax(" in source


def test_cmd_ax_uses_get_full_ax_tree():
    """§3: ax must call Accessibility.getFullAXTree, NOT enable."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_ax":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "getFullAXTree" in func_src, "must use getFullAXTree"
            assert "Accessibility.enable" not in func_src, (
                "must NOT call Accessibility.enable (perf cost)")
            return
    raise AssertionError("cmd_ax not found")


def test_cmd_ax_websocket_only():
    """§3: ax must check has_websocket() and refuse on AppleScript channel."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_ax":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "has_websocket" in func_src, (
                "cmd_ax must check has_websocket() (websocket-only command)")
            assert "ax requires" in func_src.lower() or "websocket" in func_src.lower(), (
                "cmd_ax must print websocket-related error on AppleScript channel")
            return
    raise AssertionError("cmd_ax not found")


def test_cmd_ax_docstring():
    """__doc__ must document ax command."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    docstring = ast.get_docstring(tree)
    assert "ax" in docstring, "__doc__ missing ax command"


def test_cmd_ax_pins_target():
    """ax must use get_tab(TARGET) — not unpinned."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_ax":
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id == "get_tab"):
                    pinned = (len(call.args) >= 1
                              and isinstance(call.args[0], ast.Name)
                              and call.args[0].id == "TARGET")
                    assert pinned, "get_tab() in cmd_ax must use TARGET"
                    return
    raise AssertionError("cmd_ax does not call get_tab")


# ── Drag command (§4.7) ──


def test_cmd_drag_registered():
    source = Path(CDP_SCRIPT).read_text()
    assert '"drag"' in source, "drag not in COMMANDS"


def test_cmd_drag_websocket_only():
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_drag":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "has_websocket" in func_src
            return
    raise AssertionError("cmd_drag not found")


# ── Hover command (§4.6) ──


def test_cmd_hover_registered():
    source = Path(CDP_SCRIPT).read_text()
    assert '"hover"' in source, "hover not in COMMANDS"


def test_cmd_hover_websocket_only():
    """hover requires websocket (Input domain)."""
    source = Path(CDP_SCRIPT).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_hover":
            func_src = "\n".join(
                source.splitlines()[node.lineno - 1 : node.end_lineno])
            assert "has_websocket" in func_src
            return
    raise AssertionError("cmd_hover not found")


# ── Key command (§4.5) ──


def test_cmd_key_registered():
    source = Path(CDP_SCRIPT).read_text()
    assert '"key"' in source, "key not in COMMANDS"


def test_cmd_key_ref_only():
    """key is ref-only — no global key without --ref."""
    r = run_cdp(["key", "Enter"], env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0
    assert "--ref" in r.stdout or "ref" in r.stderr.lower() or "Usage" in r.stdout


def test_cmd_key_unknown_key_is_error():
    r = run_cdp(["key", "--ref", "42", "F13"],
                env_override={"CDP_PORT": "19111"})
    assert r.returncode != 0
    assert "Enter" in r.stdout or "Enter" in r.stderr


# ── Doc-test: shadow routing in drive SKILL.md (§6) ──


class TestShadowRoutingDocTest:
    DRIVE_SKILL = Path(__file__).parent.parent / "skills" / "drive" / "SKILL.md"

    def test_shadow_routing_section_exists(self):
        content = self.DRIVE_SKILL.read_text()
        assert "shadow" in content.lower()
        assert "ax" in content
        assert "--ref" in content

    def test_shadow_three_routes(self):
        content = self.DRIVE_SKILL.read_text()
        assert "semantic" in content.lower() or "button" in content.lower()
        assert "canvas" in content.lower()
        assert "screenshot" in content

    def test_shadow_marker_documented(self):
        content = self.DRIVE_SKILL.read_text()
        assert "[shadow=" in content


# ── Parser matrix (§6 — per-command, NOT generic) ──


class TestParserMatrix:
    """§6: per-command --ref grammar. Generic 'ref+positional=error' was a bug."""

    def test_click_ref_plus_selector_is_error(self):
        r = run_cdp(["click", "--ref", "42", ".btn"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_click_ref_alone_accepted(self):
        r = run_cdp(["click", "--ref", "42"],
                    env_override={"CDP_PORT": "19111"})
        assert "Usage" not in r.stdout

    def test_fill_ref_needs_value(self):
        r = run_cdp(["fill", "--ref", "42"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_fill_ref_with_value_accepted(self):
        r = run_cdp(["fill", "--ref", "42", "hello"],
                    env_override={"CDP_PORT": "19111"})
        assert "Usage" not in r.stdout

    def test_js_ref_needs_expr(self):
        r = run_cdp(["js", "--ref", "42"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_js_ref_with_expr_accepted(self):
        r = run_cdp(["js", "--ref", "42", "el.tagName"],
                    env_override={"CDP_PORT": "19111"})
        assert "Usage" not in r.stdout

    def test_assert_ref_plus_selector_is_error(self):
        r = run_cdp(["assert", "--ref", "42", ".btn"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_hover_ref_plus_selector_is_error(self):
        r = run_cdp(["hover", "--ref", "42", ".btn"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_drag_mixed_ref_selector_is_error(self):
        r = run_cdp(["drag", "--ref", "42", ".dst"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_drag_cancel_plus_html5_is_error(self):
        r = run_cdp(["drag", "--cancel", "--html5", "src", "dst"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0

    def test_ref_non_numeric_is_error(self):
        r = run_cdp(["click", "--ref", "abc"],
                    env_override={"CDP_PORT": "19111"})
        assert r.returncode != 0


class TestAssertStructural:
    def test_assert_registered_in_commands(self):
        cdp = _load_cdp_module()
        assert "assert" in cdp.COMMANDS
        assert callable(cdp.COMMANDS["assert"])

    def test_docstring_documents_verify_core_surface(self):
        """Drift-guard: the module docstring (the agent-facing usage text) names
        every SP2 verify-core surface (full set since Task 6 — R1-F1/E1-r2-A1)."""
        cdp = _load_cdp_module()
        doc = cdp.__doc__
        for token in ("assert", "--actionable", "--gate", "--wait",
                      "--require-trusted", "--bind"):
            assert token in doc, "verify-core surface {!r} missing from cdp.py docstring".format(token)
