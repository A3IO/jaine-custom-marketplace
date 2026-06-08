"""Regression: log-round.sh must work under bash 3.2 (macOS /bin/bash) when
the optional 9th positional MANUAL_EXTRACTION_PENDING is omitted.

The bug: set -euo pipefail + empty array + "${arr[@]}" expansion → bash 3.2
raises 'unbound variable' and exits 1. Bash 5.x relaxed this. The fix uses
the portable ${arr[@]+"${arr[@]}"} form.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
LOG_ROUND = PLUGIN_ROOT / "skills" / "check" / "scripts" / "log-round.sh"


def _run_log_round(bash_path: str, args: list[str], env_override=None, cwd=None):
    env = os.environ.copy()
    if env_override:
        for k, v in env_override.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [bash_path, str(LOG_ROUND)] + args,
        capture_output=True, text=True, timeout=10, env=env, cwd=cwd,
    )


class TestLogRoundBash32Compat:
    """log-round.sh must not crash on bash 3.2 when 9th positional is omitted.

    Regression for commit e76ce63 Task 3 of PR-1. The added update_flags array
    + set -u broke on bash 3.2 because empty-array expansion is "unbound".
    """

    BASH_32 = "/bin/bash"
    BASH_5 = "/opt/homebrew/bin/bash"

    @pytest.fixture
    def review_dir(self, tmp_path):
        d = tmp_path / "review"
        d.mkdir()
        return d

    def _common_args(self, review_dir):
        return ["1", "test-artifact", "GO", "0", "0", "0", "codex/test", str(review_dir.parent)]

    def _common_env(self, review_dir, tmp_path):
        return {
            "BULLDOZER_REVIEW_DIR": str(review_dir),
            "BULLDOZER_LOG": str(tmp_path / "bulldozer.log"),
        }

    @pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash 3.2 path missing")
    def test_bash32_succeeds_without_9th_positional(self, tmp_path, review_dir):
        """The headline regression: 8 args, no 9th, bash 3.2 must not crash."""
        result = _run_log_round(
            self.BASH_32, self._common_args(review_dir),
            env_override=self._common_env(review_dir, tmp_path),
        )
        assert result.returncode == 0, (
            f"bash 3.2 should not crash on omitted 9th positional; "
            f"stderr={result.stderr!r}"
        )
        # state.json should be written
        assert (review_dir / "state.json").exists()
        state = json.loads((review_dir / "state.json").read_text())
        assert state["history"][0]["verdict"] == "GO"
        # manual_extraction_pending column should NOT be set (no 9th arg)
        assert not state["history"][0].get("manual_extraction_pending", False)

    @pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash 3.2 path missing")
    def test_bash32_succeeds_with_9th_positional_true(self, tmp_path, review_dir):
        """9th positional 'true' must work on bash 3.2 too."""
        args = self._common_args(review_dir) + ["true"]
        result = _run_log_round(
            self.BASH_32, args,
            env_override=self._common_env(review_dir, tmp_path),
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
        state = json.loads((review_dir / "state.json").read_text())
        assert state["history"][0].get("manual_extraction_pending") is True

    @pytest.mark.skipif(not shutil.which("/opt/homebrew/bin/bash"), reason="bash 5+ not installed")
    def test_bash5_also_works_without_9th_positional(self, tmp_path, review_dir):
        """Sanity: bash 5 path (already known good) still works."""
        result = _run_log_round(
            self.BASH_5, self._common_args(review_dir),
            env_override=self._common_env(review_dir, tmp_path),
        )
        assert result.returncode == 0, f"stderr={result.stderr!r}"
