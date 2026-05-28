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
                "--scale branch should not call cdp_js (which opens a separate WS via get_tab()); "
                "use ws_send(ws_url, 'Runtime.evaluate', …) instead."
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
