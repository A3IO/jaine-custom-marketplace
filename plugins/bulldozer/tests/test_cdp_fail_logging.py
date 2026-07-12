#!/usr/bin/env python3
"""PR1b (#322 B6): dispatcher fail-logging + previously-unlogged commands.

Behavioral, subprocess-based (test_cdp.py convention). The dispatcher guarantees
every non-zero exit produces a final `event=<cmd> | ... | ok=no | exit=N` line,
so a command's success is inferable from the absence of a fail line.
"""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CDP_SCRIPT = str(Path(__file__).parent.parent / "skills" / "look" / "scripts" / "cdp.py")


def run_cdp(args, log_path, extra_env=None, timeout=15):
    env = os.environ.copy()
    env.pop("CDP_PORT", None)
    env["BULLDOZER_LOOK_LOG"] = str(log_path)
    env["CLAUDE_CODE_SESSION_ID"] = "cafebabe99"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, CDP_SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def log_lines(log_path):
    p = Path(log_path)
    return p.read_text().splitlines() if p.exists() else []


class StubHandler(BaseHTTPRequestHandler):
    response_body = (
        b'[{"id":"tab1","type":"page","url":"http://localhost","title":"Test",'
        b'"webSocketDebuggerUrl":"ws://localhost:1/tab1"}]'
    )

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, *a):
        pass


def with_stub(fn):
    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        return fn(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()  # release the socket fd
        t.join(timeout=5)


# ── dispatcher fail-logging ──


def test_usage_error_writes_fail_line(tmp_path):
    log = tmp_path / "look.log"
    # click without a selector → usage error before any network; port 9399 is
    # empty so the test can never touch the real daily browser.
    r = run_cdp(["click"], log, extra_env={"CDP_PORT": "9399"})
    assert r.returncode == 1
    fails = [l for l in log_lines(log) if " | ok=no | exit=1" in l]
    assert len(fails) == 1
    assert " | event=click | " in fails[0]
    assert " | port=9399 | " in fails[0]


def test_arg_error_writes_fail_line(tmp_path):
    log = tmp_path / "look.log"
    r = run_cdp(
        ["screenshot", str(tmp_path / "s.jpg"), "--clip", "bad"], log,
        extra_env={"CDP_PORT": "9399"},
    )
    assert r.returncode == 1
    assert any(
        " | event=screenshot | " in l and " | ok=no | exit=1" in l
        for l in log_lines(log)
    )


def test_offline_command_writes_fail_line(tmp_path):
    log = tmp_path / "look.log"
    # port 9399: nothing listens → status prints OFFLINE, exits 1
    r = run_cdp(["status"], log, extra_env={"CDP_PORT": "9399"})
    assert r.returncode == 1
    assert any(
        " | event=status | " in l and "ok=no | exit=1" in l for l in log_lines(log)
    )


def test_systemexit_path_writes_fail_line(tmp_path):
    # get_tab() fails loud via sys.exit(1) — that termination path must still
    # leave the ok=no line (codex review #324 P1). title on an empty port hits it.
    log = tmp_path / "look.log"
    r = run_cdp(["title"], log, extra_env={"CDP_PORT": "9399"})
    assert r.returncode == 1
    assert any(
        " | event=title | " in l and "ok=no | exit=1" in l for l in log_lines(log)
    )


def test_successful_command_writes_no_fail_line(tmp_path):
    log = tmp_path / "look.log"

    def go(port):
        return run_cdp(["tabs"], log, extra_env={"CDP_PORT": str(port)})

    r = with_stub(go)
    assert r.returncode == 0
    assert not any("ok=no" in l for l in log_lines(log))


# ── previously-unlogged commands (audit: 5 commands logged nothing) ──


def test_status_logs_online_summary(tmp_path):
    log = tmp_path / "look.log"

    def go(port):
        return run_cdp(["status"], log, extra_env={"CDP_PORT": str(port)})

    r = with_stub(go)
    assert r.returncode == 0
    (line,) = [l for l in log_lines(log) if " | event=status | " in l]
    assert "tabs=1" in line


def test_tabs_offline_is_failure_not_zero_tabs(tmp_path):
    # cdp_get None (unreachable endpoint) must NOT read as 'count=0 success' —
    # offline and zero-tabs are different answers (codex review #324 r3).
    log = tmp_path / "look.log"
    r = run_cdp(["tabs"], log, extra_env={"CDP_PORT": "9399"})
    assert r.returncode == 1
    lines = log_lines(log)
    assert any(" | event=tabs | " in l and "ok=no | exit=1" in l for l in lines)
    assert not any("count=0" in l for l in lines)


def test_tabs_logs_count(tmp_path):
    log = tmp_path / "look.log"

    def go(port):
        return run_cdp(["tabs"], log, extra_env={"CDP_PORT": str(port)})

    r = with_stub(go)
    assert r.returncode == 0
    (line,) = [l for l in log_lines(log) if " | event=tabs | " in l]
    assert "count=1" in line


def test_title_and_html_success_paths_log():
    # title/html success needs a live websocket (no stub) — pin the log() calls
    # structurally, test_cdp.py-style, so they can't be silently removed.
    src = Path(CDP_SCRIPT).read_text()
    title_body = src.split("def cmd_title(")[1].split("\ndef ")[0]
    html_body = src.split("def cmd_html(")[1].split("\ndef ")[0]
    assert 'log("title"' in title_body
    assert 'log("html"' in html_body
