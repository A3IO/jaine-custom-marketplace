"""#334: miner-facing log-shape contract. Exercises the REAL producers (not the
helper) into temp logs and validates EVERY emitted line against the ONE canonical
regex — the gap that let #322 close as 53/53 while unmigrated producers drifted
(the issue named two; the prep surfaced a third shape).

Redaction invariants ride the same lines: a secret-bearing URL fed to a producer
must come out with `?<redacted>` and without the secret/userinfo.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "skills" / "consult" / "scripts"))

CANON = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"
    r" \| event=[A-Za-z0-9_-]+ \| session=[A-Za-z0-9_-]{1,8}"
    r"( \| [A-Za-z0-9_-]+=[^|]*)*$")

SECRET_URL = "https://u:p@x.test/api?token=SECRET"


def _assert_all_canon(path: Path, expect_lines=None):
    lines = path.read_text().splitlines()
    assert lines, "producer wrote nothing"
    if expect_lines is not None:
        assert len(lines) == expect_lines, lines
    for ln in lines:
        assert CANON.match(ln), ln
    return lines


class TestCodexChannelContract:
    """Every codex_server audit writer, fired for real into a temp log."""

    @pytest.fixture(autouse=True)
    def _log(self, tmp_path, monkeypatch):
        self.path = tmp_path / "codex.log"
        monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(self.path))

    def test_all_writers_emit_canonical_lines(self):
        import codex_server as cs
        ts = {"model_val": "m1", "effort_val": "high", "mcp_mode": "isolated",
              "retries": 0}
        cs._turn_ok_log(dict(ts, setup_ms=5, cold_spawn=True),
                        {"timing": {"duration_ms": 12}, "usage": {"total_tokens": 9}})
        cs._turn_error_log(ts, "boom " + SECRET_URL)
        cs._interrupt_log(ts, "esc", True)
        cs._log_approval_event("m", "accept", 3, False)
        cs._log_approval_event("m", "accept", 0, False, unattended=True, rule="model_resume")
        cs._log_approval_event("m", "accept", 1, False, ui="dialog")
        cs._warning_log({"message": "w1 " + SECRET_URL})
        cs._warning_log({"warning": {"message": "nested"}})
        cs._warning_log({"odd": 1})
        cs._info_error_log("models", "fail " + SECRET_URL)
        cs._drift_warn([], "UNKNOWN_NOTIFICATION", "some/method")
        cs._drift_warn(None, "TRANSLATE_FAILED", "openai: HTTPError: " + SECRET_URL)
        cs.build_awaiting_payload(
            "item/commandExecution/requestApproval", {}, {}, "", "tok-abcdefgh")
        lines = _assert_all_canon(self.path, expect_lines=13)
        text = "\n".join(lines)
        assert "SECRET" not in text and "u:p" not in text
        assert text.count("?<redacted>") >= 4  # TURN_ERROR, WARNING, INFO_ERROR, generic
        park = [ln for ln in lines if "event=PARK" in ln]
        assert len(park) == 1
        assert "token8=abcdefgh" in park[0] and "tok-abcdefgh" not in park[0]

    def test_worker_stamp_stays_last(self, monkeypatch):
        import codex_server as cs
        monkeypatch.setenv("BULLDOZER_WORKER", "2")
        cs._turn_ok_log({"model_val": "m"}, {})
        line = _assert_all_canon(self.path, expect_lines=1)[0]
        assert line.endswith(" | worker=2")


class TestConsultChannelContract:
    def test_panel_completion_is_canonical(self, tmp_path, monkeypatch):
        import consult_panel as panel
        monkeypatch.setattr(panel, "CONSULT_LOG", tmp_path / "consult.log", raising=False)

        def runner(cmd, env, cwd, timeout):
            return panel.ModelResult(True, "findings", None)
        panel.run_panel("Q", runner=runner)
        lines = _assert_all_canon(tmp_path / "consult.log", expect_lines=1)
        assert "event=consult-complete" in lines[0]
        assert "| models=" in lines[0]      # panel shape: plural

    def test_cli_shim_line_is_canonical(self, tmp_path):
        # This is EXACTLY what the SKILL.md inline template runs now.
        log = tmp_path / "consult.log"
        r = subprocess.run(
            [sys.executable, str(ROOT / "lib" / "bulldozer_log.py"), str(log),
             "consult-complete", "round=1", "verdict=GO", "tokens=NA",
             "time=4.3s", "model=gpt-5.6-sol", "project=/tmp/x"],
            capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        lines = _assert_all_canon(log, expect_lines=1)
        assert "event=consult-complete" in lines[0]
        assert "| model=gpt-5.6-sol |" in lines[0]   # inline shape: singular


class TestSkillMdStructuralContract:
    """R1-F3 / R5-F2: the consult SKILL template must stay on the shim path."""

    def _template(self):
        src = (ROOT / "skills" / "consult" / "SKILL.md").read_text()
        return src.split("## Quick Reference — Full Invocation Template", 1)[1]

    def test_no_legacy_echo_writer_remains(self):
        src = (ROOT / "skills" / "consult" / "SKILL.md").read_text()
        assert 'echo "$(date -Iseconds)' not in src, \
            "a raw echo completion writer is the third legacy shape #334 removed"

    def test_template_uses_shim_with_env_override_and_warning(self):
        t = self._template()
        assert t.count("${BULLDOZER_CONSULT_LOG:-") >= 2, \
            "both shim calls must honor the env override (tmpfile smokes)"
        assert "consult completion line NOT logged" in t, \
            "resolver failure must WARN, never silently drop"
        assert 'consult-complete' in t

    def test_docs_show_event_key_on_completion_lines(self):
        src = (ROOT / "skills" / "consult" / "SKILL.md").read_text()
        assert src.count("event=consult-complete") >= 2  # inline + panel examples
