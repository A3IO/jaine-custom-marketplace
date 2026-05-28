"""Integration tests for the check pipeline: parse-ledger-patch.py + log-round.sh + update-state.py.

These exercise the contract between the new parser and the existing logging
scripts — a wrapper script in PR1b will compose all three.

All side effects (state.json, malformed dumps, log file) are sandboxed via
``BULLDOZER_REVIEW_DIR`` and ``BULLDOZER_LOG`` env overrides — these tests
never touch the real ``~/.claude/hooks/bulldozer.log``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import PLUGIN_ROOT

PARSER = PLUGIN_ROOT / "skills" / "check" / "scripts" / "parse-ledger-patch.py"
LOG_ROUND = PLUGIN_ROOT / "skills" / "check" / "scripts" / "log-round.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "check"

pytest.importorskip("yaml", reason="parser requires PyYAML")


def run_parser_to_file(verdict_file: Path, output: Path) -> subprocess.CompletedProcess[str]:
    """Run parser, write JSON to output. Returns process for inspection."""
    with output.open("w") as fp:
        return subprocess.run(
            [sys.executable, str(PARSER), "--file", str(verdict_file)],
            stdout=fp,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )


def run_log_round(
    review_dir: Path,
    log_file: Path,
    *,
    round_num: int,
    artifact: str,
    verdict: str,
    findings: int,
    fixed: int,
    fp: int,
    reviewer: str = "codex/test",
    depth: str = "standard",
) -> subprocess.CompletedProcess[str]:
    """Run log-round.sh with sandboxed BULLDOZER_REVIEW_DIR + BULLDOZER_LOG."""
    env = os.environ.copy()
    env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
    env["BULLDOZER_LOG"] = str(log_file)
    env["BULLDOZER_DEPTH"] = depth
    return subprocess.run(
        [
            "bash",
            str(LOG_ROUND),
            str(round_num),
            artifact,
            verdict,
            str(findings),
            str(fixed),
            str(fp),
            reviewer,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def read_state(review_dir: Path) -> dict[str, Any]:
    return json.loads((review_dir / "state.json").read_text())


# ---------------------------------------------------------------------------
# Single-round pipeline: parser produces JSON, log-round.sh updates state.json
# ---------------------------------------------------------------------------

class TestSingleRoundPipeline:
    def test_parser_output_consumed_by_log_round(self, tmp_path: Path):
        """End-to-end: real verdict file → parser JSON → log-round writes state.json."""
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        log_file = tmp_path / "bulldozer.log"

        # Step 1: run parser
        parsed = review_dir / "parsed-r1.json"
        result = run_parser_to_file(FIXTURES / "verdict-real-51e16e7b-r1.txt", parsed)
        assert result.returncode == 0, result.stderr

        payload = json.loads(parsed.read_text())
        assert len(payload["findings"]) == 3
        findings_count = len(payload["findings"])

        # Step 2: feed parsed counts into log-round.sh
        log_result = run_log_round(
            review_dir,
            log_file,
            round_num=1,
            artifact="diff-snap-grid",
            verdict="NO-GO",
            findings=findings_count,
            fixed=0,
            fp=0,
        )
        assert log_result.returncode == 0, log_result.stderr

        # Step 3: verify state.json + log line
        state = read_state(review_dir)
        assert state["round"] == 1
        assert state["findings_total"] == 3
        assert len(state["history"]) == 1
        assert state["history"][0]["verdict"] == "NO-GO"

        log_lines = log_file.read_text().strip().splitlines()
        assert len(log_lines) == 1
        assert "round=1" in log_lines[0]
        assert "findings=3" in log_lines[0]
        assert "verdict=NO-GO" in log_lines[0]


class TestMultiRoundAccumulation:
    """state.json history accumulates across rounds; bulldozer.log appends."""

    def test_three_rounds_history_accumulates(self, tmp_path: Path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        log_file = tmp_path / "bulldozer.log"

        # Simulate 3 rounds: trajectory 4 → 3 → 0 (GO)
        rounds = [
            (1, "NO-GO", 4, 4, 0),
            (2, "NO-GO", 3, 3, 0),
            (3, "GO", 0, 0, 0),
        ]
        for round_num, verdict, findings, fixed, fp in rounds:
            result = run_log_round(
                review_dir,
                log_file,
                round_num=round_num,
                artifact="test-artifact",
                verdict=verdict,
                findings=findings,
                fixed=fixed,
                fp=fp,
            )
            assert result.returncode == 0, result.stderr

        state = read_state(review_dir)
        assert state["round"] == 3
        assert state["findings_total"] == 7  # 4+3+0
        assert state["fixed_total"] == 7  # 4+3+0
        assert state["false_positives"] == 0
        assert len(state["history"]) == 3

        # Trajectory derivable from history (this is the U7 enforcement signal).
        trajectory = [h["findings"] for h in state["history"]]
        assert trajectory == [4, 3, 0]

        log_lines = log_file.read_text().strip().splitlines()
        assert len(log_lines) == 3
        assert all("session=" in line for line in log_lines)


class TestSandboxIsolation:
    """Verify BULLDOZER_LOG override actually prevents writes to ~/.claude/hooks/bulldozer.log."""

    def test_log_does_not_touch_default_path(self, tmp_path: Path):
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        sandbox_log = tmp_path / "bulldozer.log"

        default_log = Path.home() / ".claude" / "hooks" / "bulldozer.log"
        before_size = default_log.stat().st_size if default_log.exists() else 0

        result = run_log_round(
            review_dir,
            sandbox_log,
            round_num=1,
            artifact="sandbox-check",
            verdict="NO-GO",
            findings=1,
            fixed=0,
            fp=0,
        )
        assert result.returncode == 0, result.stderr

        after_size = default_log.stat().st_size if default_log.exists() else 0
        assert after_size == before_size, (
            f"BULLDOZER_LOG override leaked — default log changed by {after_size - before_size} bytes"
        )
        assert sandbox_log.exists()
        assert "artifact=sandbox-check" in sandbox_log.read_text()


class TestParserExitCodesFlow:
    """Verify exit codes propagate so a wrapper can branch on them."""

    def test_exit_one_no_block_signals_manual_fallback(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, str(PARSER), "--file", str(FIXTURES / "verdict-no-block.txt")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 1, "exit 1 = no block = caller falls back to manual"

    def test_exit_two_malformed_dump_saved(self, tmp_path: Path):
        target = tmp_path / "verdict-r1.txt"
        target.write_text((FIXTURES / "verdict-malformed-yaml.txt").read_text())
        result = subprocess.run(
            [sys.executable, str(PARSER), "--file", str(target)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert (tmp_path / "verdict-r1.malformed.yml").exists(), (
            "exit 2 must persist malformed block for operator inspection"
        )

    def test_exit_three_schema_violation_no_state_corruption(self, tmp_path: Path):
        """Simulate a wrapper that correctly bails on exit 3.

        Sequence a wrapper MUST follow:
          1. Run parser
          2. If exit != 0, STOP (no log-round.sh, no state.json)
          3. Otherwise feed counts to log-round.sh

        This test runs only step 1+2 (parser exits 3, we DO NOT invoke log-round)
        and asserts the sandbox is untouched: no state.json, no log lines.
        That's the contract PR1b's wrapper must obey to keep ledger uncorrupted.
        """
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        log_file = tmp_path / "bulldozer.log"

        text = "LEDGER_PATCH:\n  verdict: no_go\n"  # missing 'findings' = schema violation
        result = subprocess.run(
            [sys.executable, str(PARSER)],
            input=text,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 3, result.stderr
        # Wrapper correctly does NOT invoke log-round.sh here.

        # Side-effect contract: NEITHER artifact should exist after exit-3 + bail.
        assert not (review_dir / "state.json").exists(), (
            "state.json should not exist when wrapper bails on exit 3 — "
            "if this fires, the integration contract changed and the wrapper "
            "design in PR1b must be updated."
        )
        assert not log_file.exists(), (
            "bulldozer.log should not exist when wrapper bails on exit 3"
        )
        # Sanity: no .malformed.yml dump for schema-violation case (exit 2 territory).
        for entry in tmp_path.rglob("*.malformed.yml"):
            pytest.fail(
                f"unexpected malformed dump on exit 3: {entry} "
                "(exit 2 = malformed YAML, exit 3 = schema violation — distinct paths)"
            )
