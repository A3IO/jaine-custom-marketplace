"""#322 PR6 (tails): D2 redaction (cdp.py js/url values), D7 (pivot exit-10 +
E1 audit-effectiveness durable lines).

Behavioral, subprocess-based where a writer exists; unit-level for the pure
redaction helpers (loaded from cdp.py via importlib — test_e2e.py convention).
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

from test_check_round_wrapper import _seed_state_and_run

from conftest import test_env

PLUGIN_ROOT = Path(__file__).parent.parent
CDP_SCRIPT = PLUGIN_ROOT / "skills" / "look" / "scripts" / "cdp.py"
VERIFY_AUDIT = PLUGIN_ROOT / "skills" / "check" / "scripts" / "verify-audit-findings.py"


def _load_cdp():
    spec = importlib.util.spec_from_file_location("_cdp_pr6", str(CDP_SCRIPT))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── D2: URL/JS redaction helpers ──


class TestRedactUrl:
    def test_strips_userinfo_query_fragment(self):
        cdp = _load_cdp()
        out = cdp._redact_url(
            "https://user:hunter2@h.example:8443/p?token=SECRET#access_token=X")
        assert "hunter2" not in out
        assert "SECRET" not in out
        assert "access_token" not in out
        assert out == "https://h.example:8443/p?<redacted>"

    def test_plain_urls_pass_through(self):
        cdp = _load_cdp()
        assert cdp._redact_url("about:blank") == "about:blank"
        assert cdp._redact_url("http://localhost:9333/page.html") == \
            "http://localhost:9333/page.html"
        assert cdp._redact_url("file:///tmp/x.html") == "file:///tmp/x.html"

    def test_fragment_only_gets_marker(self):
        cdp = _load_cdp()
        out = cdp._redact_url("https://h.example/cb#id_token=eyJSECRET")
        assert "eyJSECRET" not in out
        assert out.endswith("?<redacted>")

    def test_long_url_capped(self):
        cdp = _load_cdp()
        out = cdp._redact_url("http://h.example/" + "a" * 500)
        assert len(out) <= 120

    def test_payload_schemes_fully_redacted(self):
        # codex #329 r1: data:/javascript: put the whole payload in .path —
        # urlsplit-based origin+path keeps it verbatim. Payload-bearing /
        # unknown schemes must never reach the log.
        cdp = _load_cdp()
        out = cdp._redact_url("data:text/html,SECRETPAYLOAD")
        assert "SECRETPAYLOAD" not in out
        assert out.startswith("data:")
        out = cdp._redact_url("javascript:alert(document.cookie)")
        assert "cookie" not in out and "alert" not in out
        out = cdp._redact_url("blob:https://h.example/uuid")
        assert out.startswith("blob:")
        # codex #329 r2: view-source nests a FULL url (userinfo/query incl.)
        # in .path — allowlisting it leaked nested credentials
        out = cdp._redact_url("view-source:https://user:hunter2@h.example/p?tok=SECRET")
        assert "hunter2" not in out and "SECRET" not in out

    def test_marker_survives_truncation(self):
        # codex #329 r1: the [:120] cap used to eat the appended marker,
        # making a redacted-query URL indistinguishable from a truncated path
        cdp = _load_cdp()
        out = cdp._redact_url("http://h.example/" + "a" * 300 + "?tok=SECRET")
        assert out.endswith("?<redacted>")
        assert len(out) <= 120
        assert "SECRET" not in out


class TestRedactTarget:
    def test_secret_bearing_selector_hashed(self):
        # codex #329 r3 (P1): --target may be a URL substring — ?/#/@ mark
        # query/fragment/userinfo territory; such selectors are hashed wholesale
        cdp = _load_cdp()
        out = cdp._redact_target("https://h/cb?access_token=SECRET")
        assert "SECRET" not in out and "access_token" not in out
        out = cdp._redact_target("user:hunter2@h.example/path")
        assert "hunter2" not in out

    def test_common_selectors_stay_readable(self):
        cdp = _load_cdp()
        assert cdp._redact_target("0FBA34C21A6F") == "0FBA34C21A6F"  # id-prefix
        assert cdp._redact_target("localhost:9333/page.html") == "localhost:9333/page.html"

    def test_target_field_redacted_in_fail_line(self, tmp_path):
        # behavioral: dead port 9399 → dispatcher fail line still carries
        # target=, but never the selector's secret
        log = tmp_path / "look.log"
        env = test_env()
        env.update({"BULLDOZER_LOOK_LOG": str(log), "CDP_PORT": "9399"})
        r = subprocess.run(
            [sys.executable, str(CDP_SCRIPT),
             "--target", "https://h/cb?access_token=SECRET", "title"],
            capture_output=True, text=True, timeout=15, env=env,
        )
        assert r.returncode == 1
        text = log.read_text()
        assert " | target=" in text
        assert "SECRET" not in text


class TestSha12:
    def test_sha12_is_sha256_prefix(self):
        import hashlib
        cdp = _load_cdp()
        expr = "document.title + 'x'"
        expected = hashlib.sha256(expr.encode("utf-8")).hexdigest()[:12]
        assert cdp._sha12(expr) == expected

    def test_surrogateescaped_argv_does_not_raise(self):
        # codex #329 r1: undecodable argv bytes surface as lone surrogates;
        # strict utf-8 encode raised mid-log and turned a completed command
        # into an exit=crash
        cdp = _load_cdp()
        out = cdp._sha12("\udc80x")
        assert len(out) == 12
        int(out, 16)  # hex digest prefix


class TestNoVerbatimValuesInLogCalls:
    """Structural: no log() call ships verbatim JS source or unredacted URLs.

    The success paths need a live browser to exercise (e2e covers navigate/js);
    this pins the source so a new call site can't silently regress to verbatim.
    """

    def test_no_verbatim_expr_or_url(self):
        src = CDP_SCRIPT.read_text()
        assert "expr=expr[" not in src, "js log must not carry verbatim JS source"
        assert "url=url[" not in src, "navigate/open log must use _redact_url"
        assert 'url=tab.get("url", "?")[' not in src, "screenshot log must use _redact_url"

    def test_redaction_helpers_used(self):
        src = CDP_SCRIPT.read_text()
        assert "_redact_url(" in src
        assert "expr_sha=" in src and "expr_len=" in src

    def test_assert_js_path_logs_redacted_what(self):
        # the --js assert carries user JS in `what`; the log value must be the
        # redacted twin (what_log), while stdout keeps the readable original
        src = CDP_SCRIPT.read_text()
        assert "what=what_log" in src


# ── D7: pivot (exit 10) durable line ──


class TestPivotLine:
    def test_pivot_exit10_writes_pivot_line(self, tmp_path):
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 2, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        result = _seed_state_and_run(tmp_path, seed, round_num=3, depth="standard")
        assert result.returncode == 10, result.stderr
        lines = (tmp_path / "bulldozer.log").read_text().splitlines()
        pivots = [l for l in lines if " | event=pivot | " in l]
        assert len(pivots) == 1, lines
        line = pivots[0]
        assert " | trigger=max_rounds_reached | " in line
        assert " | round=3 | " in line
        assert " | depth=standard | " in line
        assert " | artifact=test-artifact | " in line

    def test_no_pivot_line_below_max(self, tmp_path):
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        result = _seed_state_and_run(tmp_path, seed, round_num=2, depth="standard")
        assert result.returncode == 0, result.stderr
        lines = (tmp_path / "bulldozer.log").read_text().splitlines()
        assert not any(" | event=pivot | " in l for l in lines)


# ── D7: E1 audit-effectiveness durable line ──


def run_verify(tmp_path, findings_obj, log_path):
    findings = tmp_path / "findings.json"
    out = tmp_path / "out.json"
    if isinstance(findings_obj, str):
        findings.write_text(findings_obj)
    else:
        findings.write_text(json.dumps(findings_obj))
    env = test_env()
    env["BULLDOZER_LOG"] = str(log_path)
    env["CLAUDE_CODE_SESSION_ID"] = "cafebabe99"
    r = subprocess.run(
        [sys.executable, str(VERIFY_AUDIT), "--findings", str(findings),
         "--out", str(out), "--project-root", str(tmp_path)],
        capture_output=True, text=True, timeout=10, env=env,
    )
    return r, out


class TestAuditEffectivenessLine:
    def test_proposed_vs_survived_logged(self, tmp_path):
        (tmp_path / "doc.md").write_text("alpha beta gamma")
        log = tmp_path / "bulldozer.log"
        r, out = run_verify(tmp_path, {"findings": [
            {"class": "dead_ref", "file": "doc.md", "quote": "alpha beta"},
            {"class": "dead_ref", "file": "doc.md", "quote": "NOT PRESENT"},
        ]}, log)
        assert r.returncode == 0, r.stderr
        assert len(json.loads(out.read_text())["findings"]) == 1
        (line,) = [l for l in log.read_text().splitlines()
                   if " | event=audit | " in l]
        assert " | proposed=2 | " in line
        assert " | survived=1 | " in line
        assert " | session=cafebabe | " in line

    def test_malformed_findings_logs_zero_zero(self, tmp_path):
        log = tmp_path / "bulldozer.log"
        r, out = run_verify(tmp_path, "not json{{{", log)
        assert r.returncode == 0, r.stderr
        (line,) = [l for l in log.read_text().splitlines()
                   if " | event=audit | " in l]
        assert " | proposed=0 | " in line
        assert " | survived=0 | " in line


# ── F5: invoke-hook helper-import failure warns on stderr, never blocks ──


class TestInvokeHookImportFailureWarns:
    def test_broken_layout_warns_and_exits_zero(self, tmp_path):
        # copy the hook into a layout with NO ../lib → append_line import fails;
        # the hook must warn (not silently swallow, #322 F5) and still exit 0
        import shutil
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        shutil.copy(PLUGIN_ROOT / "hooks" / "log_skill_invoke.py",
                    hooks_dir / "log_skill_invoke.py")
        env = test_env()
        env["BULLDOZER_INVOKE_LOG_DIR"] = str(tmp_path)
        payload = json.dumps({"prompt": "/bulldozer:look http://x", "cwd": str(tmp_path)})
        r = subprocess.run(
            [sys.executable, str(hooks_dir / "log_skill_invoke.py")], input=payload,
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert r.returncode == 0
        assert "helper unavailable" in r.stderr
        assert not (tmp_path / "bulldozer-look.log").exists()
