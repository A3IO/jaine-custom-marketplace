"""#322 PR4: check-pipeline observability — wrapper-fail lines, event=round
discriminator, depth=/duration_s= fields, reconciliation lines, invoke session=.

Behavioral, subprocess-based (log-round / wrapper / hook run for real against
tmp logs).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import test_env

PLUGIN_ROOT = Path(__file__).parent.parent
LOG_ROUND = PLUGIN_ROOT / "skills" / "check" / "scripts" / "log-round.sh"
WRAPPER = PLUGIN_ROOT / "skills" / "check" / "scripts" / "bulldozer-round.sh"
UPDATE_STATE = PLUGIN_ROOT / "skills" / "check" / "scripts" / "update-state.py"
INVOKE_HOOK = PLUGIN_ROOT / "hooks" / "log_skill_invoke.py"


def _env(tmp_path, review_dir, **extra):
    # pinned env-builder (#357 CENTRAL_ALLOWLIST) — single test_env call
    return test_env(set_vars={
        "BULLDOZER_REVIEW_DIR": str(review_dir),
        "BULLDOZER_LOG": str(tmp_path / "bulldozer.log"),
        "CLAUDE_CODE_SESSION_ID": "cafebabe99",
        **extra,
    })


@pytest.fixture
def review_dir(tmp_path):
    d = tmp_path / "review"
    d.mkdir()
    return d


def log_lines(tmp_path):
    p = tmp_path / "bulldozer.log"
    return p.read_text().splitlines() if p.exists() else []


class TestRoundLine:
    def _run(self, tmp_path, review_dir, args=None, depth="quick"):
        base = ["1", "art.md", "NO-GO", "3", "1", "0", "codex/gpt-5.6-sol",
                str(tmp_path)]
        return subprocess.run(
            ["bash", str(LOG_ROUND)] + (args if args is not None else base),
            capture_output=True, text=True, timeout=10,
            env=_env(tmp_path, review_dir, BULLDOZER_DEPTH=depth),
        )

    def test_round_line_has_event_depth_and_normalized_session(self, tmp_path, review_dir):
        r = self._run(tmp_path, review_dir)
        assert r.returncode == 0, r.stderr
        (line,) = log_lines(tmp_path)
        assert " | event=round | " in line
        assert " | session=cafebabe | " in line
        assert " | depth=quick | " in line

    def test_round_line_duration_from_10th_positional(self, tmp_path, review_dir):
        args = ["1", "art.md", "NO-GO", "3", "1", "0", "codex/gpt-5.6-sol",
                str(tmp_path), "", "42"]
        r = self._run(tmp_path, review_dir, args=args)
        assert r.returncode == 0, r.stderr
        (line,) = log_lines(tmp_path)
        assert " | duration_s=42 | " in line

    def test_reviewer_without_slash_normalized_to_codex_prefix(self, tmp_path, review_dir):
        args = ["1", "art.md", "GO", "0", "0", "0", "gpt-5.5", str(tmp_path)]
        r = self._run(tmp_path, review_dir, args=args)
        assert r.returncode == 0, r.stderr
        assert "reviewer=codex/gpt-5.5" in log_lines(tmp_path)[0]


class TestWrapperFailLine:
    def test_preflight_exit64_writes_wrapper_fail_line(self, tmp_path, review_dir):
        # unknown flag → _emit_stop 64; the failure must leave a durable line
        r = subprocess.run(
            ["bash", str(WRAPPER), "--bogus-flag", "x"],
            capture_output=True, text=True, timeout=15,
            env=_env(tmp_path, review_dir),
        )
        assert r.returncode == 64
        lines = log_lines(tmp_path)
        assert any(" | event=wrapper-fail | " in l and "exit=64" in l for l in lines), lines


class TestReconciledLine:
    def test_replace_extraction_appends_reconciled_line(self, tmp_path, review_dir):
        env = _env(tmp_path, review_dir)
        # seed a round via update-state (manual-extraction pending)
        seed = subprocess.run(
            [sys.executable, str(UPDATE_STATE), "--manual-extraction-pending",
             "1", "UNKNOWN", "0", "0", "0", "art.md", "quick", "codex/test"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert seed.returncode == 0, seed.stderr
        rec = subprocess.run(
            [sys.executable, str(UPDATE_STATE), "--review-dir", str(review_dir),
             "--mode=replace-extraction", "1", "4", "NO-GO"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert rec.returncode == 0, rec.stderr
        lines = log_lines(tmp_path)
        assert any(
            " | event=reconciled | " in l and "round=1" in l and "findings=4" in l
            and "verdict=NO-GO" in l for l in lines
        ), lines


class TestInvokeSession:
    def test_invoke_marker_carries_session(self, tmp_path):
        env = test_env()
        env.update({
            "BULLDOZER_INVOKE_LOG_DIR": str(tmp_path),
            "CLAUDE_CODE_SESSION_ID": "cafebabe99",
        })
        payload = json.dumps({"prompt": "/bulldozer:check quick foo.md", "cwd": str(tmp_path)})
        r = subprocess.run(
            [sys.executable, str(INVOKE_HOOK)], input=payload,
            capture_output=True, text=True, timeout=10, env=env,
        )
        assert r.returncode == 0, r.stderr
        line = (tmp_path / "bulldozer.log").read_text().strip()
        assert " | session=cafebabe | " in line, line
