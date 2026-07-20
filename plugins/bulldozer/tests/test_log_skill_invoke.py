"""Tests for hooks/log_skill_invoke.py — the UserPromptSubmit invoke-logger (#318).

The old hooks.json used three inline-echo entries with `matcher` regexes, but
UserPromptSubmit ignores matchers → a line was written on EVERY prompt (95% log
noise). The replacement is a single stdin-filtering script: it reads the hook
JSON, matches the prompt against ^/bulldozer:(check|look|consult)\\b and writes
one line in the ORIGINAL format to the matching log — or nothing at all.

Fail-safe contract: the script must always exit 0 (a logger must never block a
prompt), even on malformed stdin.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PLUGIN_ROOT / "hooks" / "log_skill_invoke.py"
HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"

LOG_NAMES = {
    "check": "bulldozer.log",
    "look": "bulldozer-look.log",
    "consult": "bulldozer-consult.log",
}


def run_hook(prompt_payload, log_dir, cwd=None):
    """Invoke the hook script as CC would: JSON on stdin, log dir via env override."""
    from conftest import test_env
    env = test_env(set_vars={"BULLDOZER_INVOKE_LOG_DIR": str(log_dir)})
    stdin = (
        prompt_payload
        if isinstance(prompt_payload, str)
        else json.dumps(prompt_payload)
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or str(log_dir),
        timeout=10,
    )


def log_lines(log_dir, skill):
    path = Path(log_dir) / LOG_NAMES[skill]
    if not path.exists():
        return []
    return path.read_text().splitlines()


class TestPromptFiltering:
    def test_check_prompt_logs_one_invoke_line(self, tmp_path):
        res = run_hook({"prompt": "/bulldozer:check quick", "cwd": str(tmp_path)}, tmp_path)
        assert res.returncode == 0
        lines = log_lines(tmp_path, "check")
        assert len(lines) == 1
        # original bulldozer.log invoke format, project resolved to cwd (non-git tmpdir)
        assert "| event=invoke |" in lines[0]
        assert "round=0" in lines[0]
        assert f"project={tmp_path}" in lines[0]
        assert log_lines(tmp_path, "look") == []
        assert log_lines(tmp_path, "consult") == []

    def test_look_prompt_logs_look_invoke(self, tmp_path):
        res = run_hook(
            {"prompt": "/bulldozer:look http://x task", "cwd": str(tmp_path)}, tmp_path
        )
        assert res.returncode == 0
        lines = log_lines(tmp_path, "look")
        assert len(lines) == 1
        assert "| event=look-invoke |" in lines[0]
        assert f"project={tmp_path}" in lines[0]
        assert log_lines(tmp_path, "check") == []

    def test_consult_prompt_logs_consult_invoke(self, tmp_path):
        res = run_hook({"prompt": "/bulldozer:consult should I X?", "cwd": str(tmp_path)}, tmp_path)
        assert res.returncode == 0
        lines = log_lines(tmp_path, "consult")
        assert len(lines) == 1
        assert "| event=consult-invoke |" in lines[0]

    def test_ordinary_prompt_writes_nothing(self, tmp_path):
        res = run_hook({"prompt": "привет, поправь тест", "cwd": str(tmp_path)}, tmp_path)
        assert res.returncode == 0
        for skill in LOG_NAMES:
            assert log_lines(tmp_path, skill) == []

    def test_word_boundary_no_match_on_prefix(self, tmp_path):
        # /bulldozer:checker must NOT count as /bulldozer:check
        res = run_hook({"prompt": "/bulldozer:checker foo", "cwd": str(tmp_path)}, tmp_path)
        assert res.returncode == 0
        for skill in LOG_NAMES:
            assert log_lines(tmp_path, skill) == []

    def test_mention_mid_prompt_no_match(self, tmp_path):
        # anchored at start — mentioning the command mid-sentence is not an invocation
        res = run_hook(
            {"prompt": "почему /bulldozer:check пишет мусор?", "cwd": str(tmp_path)}, tmp_path
        )
        assert res.returncode == 0
        for skill in LOG_NAMES:
            assert log_lines(tmp_path, skill) == []

    def test_project_resolves_git_toplevel(self, tmp_path):
        repo = tmp_path / "repo"
        sub = repo / "sub"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        res = run_hook({"prompt": "/bulldozer:check x", "cwd": str(sub)}, tmp_path)
        assert res.returncode == 0
        lines = log_lines(tmp_path, "check")
        assert len(lines) == 1
        assert f"project={repo.resolve()}" in lines[0]


class TestFailSafe:
    def test_malformed_stdin_exits_zero_writes_nothing(self, tmp_path):
        res = run_hook("this is not json{{{", tmp_path)
        assert res.returncode == 0
        for skill in LOG_NAMES:
            assert log_lines(tmp_path, skill) == []

    def test_missing_prompt_key_exits_zero(self, tmp_path):
        res = run_hook({"cwd": str(tmp_path)}, tmp_path)
        assert res.returncode == 0
        for skill in LOG_NAMES:
            assert log_lines(tmp_path, skill) == []

    def test_unwritable_log_dir_still_exits_zero(self, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            res = run_hook({"prompt": "/bulldozer:check x", "cwd": str(tmp_path)}, ro)
            assert res.returncode == 0
        finally:
            ro.chmod(0o755)


class TestHooksJsonWiring:
    def test_hooks_json_uses_script_not_inline_matchers(self):
        cfg = json.loads(HOOKS_JSON.read_text())
        entries = cfg["hooks"]["UserPromptSubmit"]
        assert len(entries) == 1, "expected the three broken matcher entries collapsed into one"
        entry = entries[0]
        assert "matcher" not in entry, "matcher is ignored on UserPromptSubmit — must not reappear"
        commands = [h["command"] for h in entry["hooks"]]
        assert any("log_skill_invoke.py" in c for c in commands)
        assert all("${CLAUDE_PLUGIN_ROOT}" in c for c in commands)

    def test_script_exists_and_covers_all_three_skills(self):
        src = SCRIPT.read_text()
        for name in ("check", "look", "consult"):
            assert name in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
