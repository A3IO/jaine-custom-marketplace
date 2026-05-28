"""End-to-end test: real `codex exec` → verdict → parse-ledger-patch.py.

Marked ``slow`` because it runs an actual codex round (~30-90 seconds depending
on model and load). Skipped on machines without `codex` in PATH or without
``codex login`` credentials.

This is the deepest dogfood: codex reviews a tiny spec, our parser reads
codex's actual output, and we assert the parser produces a valid JSON payload.
If codex's LEDGER_PATCH schema drifts in the future, this test will surface it
before users hit it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

PARSER = PLUGIN_ROOT / "skills" / "check" / "scripts" / "parse-ledger-patch.py"
FIXTURES = Path(__file__).parent / "fixtures" / "check"

pytest.importorskip("yaml", reason="parser requires PyYAML")

CODEX_AVAILABLE = shutil.which("codex") is not None


@pytest.mark.slow
@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not in PATH")
def test_real_codex_verdict_parses(tmp_path: Path):
    """Run codex exec on tiny-buggy-spec.md, then parse its verdict.

    The fixture intentionally contains a zero-division bug, so codex SHOULD find
    a finding and emit a LEDGER_PATCH block. We require parser exit 0 — exit 1
    (no block) would mean codex regressed to bare "GO" output, which the updated
    SKILL.md prompt template explicitly forbids. If that happens, this test
    fails loudly so we catch the regression in CI rather than in user reports.
    """
    verdict_file = tmp_path / "verdict-r1.txt"
    full_file = tmp_path / "full-r1.txt"

    prompt = (
        "SKIP SKILLS. You are reviewing tests/fixtures/check/tiny-buggy-spec.md. "
        "This is a tiny Python API spec with one obvious correctness gap. "
        "Find correctness bugs only — ignore style. "
        "Keep findings under 100 words.\n\n"
        "End your response with a LEDGER_PATCH YAML block — REQUIRED for both "
        "NO-GO and GO. Even a GO verdict must emit:\n\n"
        "LEDGER_PATCH:\n"
        "  verdict: go\n"
        "  findings: []\n\n"
        "NO-GO must emit findings with id/severity/title/files."
    )

    with full_file.open("w") as full_fp:
        result = subprocess.run(
            [
                "codex",
                "exec",
                "-s", "read-only",
                "-c", "model_reasoning_effort=medium",
                "--ephemeral",
                "-o", str(verdict_file),
                "-C", str(PLUGIN_ROOT),
                prompt,
            ],
            stdin=subprocess.DEVNULL,
            stdout=full_fp,
            stderr=subprocess.STDOUT,
            timeout=300,  # codex round can be slow
        )

    if result.returncode != 0:
        pytest.skip(
            f"codex exec failed with exit {result.returncode} — "
            f"likely auth/network issue, not a parser problem. "
            f"Last 500 chars of full output:\n{full_file.read_text()[-500:]}"
        )

    assert verdict_file.exists(), "codex -o flag must produce verdict file"
    assert verdict_file.read_text().strip(), "verdict file must not be empty"

    # Run parser on real verdict.
    parsed_file = tmp_path / "parsed-r1.json"
    with parsed_file.open("w") as out_fp:
        parse_result = subprocess.run(
            [sys.executable, str(PARSER), "--file", str(verdict_file)],
            stdout=out_fp,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )

    # Require exit 0: the prompt explicitly demands LEDGER_PATCH on both NO-GO
    # AND GO, so exit 1 (no block) would be a contract regression. The fixture
    # has a real bug, so the verdict will be NO-GO with at least one finding.
    assert parse_result.returncode == 0, (
        f"parser exit {parse_result.returncode} on real codex output — "
        f"either schema drift or codex regressed to bare 'GO' output "
        f"(SKILL.md prompt forbids this). stderr: {parse_result.stderr}\n"
        f"verdict head: {verdict_file.read_text()[:500]}"
    )

    payload = json.loads(parsed_file.read_text())
    # Sanity: payload has required canonical structure.
    assert "findings" in payload
    assert "meta" in payload
    assert "source" in payload
    assert isinstance(payload["findings"], list)
    # tiny-buggy-spec has a real bug — codex must find at least one finding.
    assert len(payload["findings"]) >= 1, (
        "codex found no findings on tiny-buggy-spec — either the spec is no "
        "longer buggy or codex regressed. Inspect verdict for details."
    )
    # Each finding has the canonical fields.
    for finding in payload["findings"]:
        assert "id" in finding
        assert "severity" in finding
        assert "title" in finding
