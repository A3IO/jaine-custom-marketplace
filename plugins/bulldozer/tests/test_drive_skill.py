#!/usr/bin/env python3
"""Structural drift-guards for skills/drive/SKILL.md (SP2). Offline."""
import os
import re

import pytest

SKILL = os.path.join(os.path.dirname(__file__), "..", "skills", "drive", "SKILL.md")
PLUGIN_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _text():
    with open(SKILL) as f:
        return f.read()


def test_skill_exists():
    assert os.path.isfile(SKILL)


def test_no_commands_dir_collision():
    """commands/ + skills/<same> silently drops one (2026-05-14 lesson)."""
    assert not os.path.isdir(os.path.join(PLUGIN_ROOT, "commands"))


def test_frontmatter_contract():
    text = _text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML frontmatter"
    fm = m.group(1)
    assert re.search(r"^name:\s*drive\s*$", fm, re.MULTILINE)
    dm = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    assert dm and len(dm.group(1)) <= 1024
    assert "argument-hint" in fm


def test_lane_contract_documented():
    """Both env keys on EVERY cdp.py call — the SP1 lane contract."""
    text = _text()
    assert "CDP_PORT" in text
    assert "CHROME_APP_NAME" in text
    assert "Google Chrome for Testing" in text


def test_verify_core_surface_documented():
    text = _text()
    for token in ("--wait", "--gate", "assert", "--require-trusted", "--bind",
                  "--stable", "--actionable", "cookie_seed.py"):
        assert token in text, "drive SKILL.md must document {!r}".format(token)


def test_console_gate_contract_documented():
    """The gate's two-leg contract (exceptions retro + console.* live window)
    must be stated — an agent that believes 'everything is replayed' will call
    the gate too late and miss console.error (the falsified morning claim)."""
    text = _text()
    assert re.search(r"exception", text, re.IGNORECASE)
    assert re.search(r"live|window", text, re.IGNORECASE)


def test_modes_and_breaker_documented():
    text = _text()
    assert "co-pilot" in text
    assert "autonomous" in text
    # structural separation (§4.4): subagents must never run co-pilot
    assert re.search(r"subagent.{0,200}autonomous", text, re.DOTALL | re.IGNORECASE)
    assert "circuit-breaker" in text.lower() or "circuit breaker" in text.lower()


def test_preflight_and_ports_documented():
    text = _text()
    assert "Chrome for Testing" in text           # endpoint pre-flight (hole D)
    assert "9340" in text                         # interactive lane range


class TestDelegationSection:
    """SP4 §2.2 — the delegation contract must be pinned in SKILL.md verbatim."""

    @pytest.fixture
    def skill_text(self):
        return _text()

    def test_placeholder_replaced(self, skill_text):
        assert "SP4 will automate" not in skill_text

    def test_ephemeral_launch_with_env_strip(self, skill_text):
        assert "CDP_PORT=0" in skill_text
        # the 7 stripped vars from the conftest LANE_ENV_VARS canon — CDP_PORT and
        # LOOK_HEADLESS are SET explicitly (set-after-strip), so they are not -u'd (check round 5)
        for var in ("LOOK_PROFILE_DIR", "LOOK_INSECURE", "LOOK_DRY_RUN",
                    "CHROME_BIN", "LOOK_AUTOMATION", "CHROME_APP_NAME", "LOOK_CERT_SPKI"):
            assert "-u " + var in skill_text

    def test_contract_keys_documented(self, skill_text):
        for key in ("CDP_PORT=", "LANE_PROFILE=", "LANE_KILL_MATCH=", "LANE_BROWSER_BIN="):
            assert key in skill_text

    def test_preflight_binary_identity(self, skill_text):
        assert "/0/.jaine/.browser/cft/" in skill_text
        assert "LANE_BROWSER_BIN" in skill_text

    def test_capture_form_documented(self, skill_text):
        assert 'EXIT=$?' in skill_text and "cmd-" in skill_text

    def test_no_bashisms_in_runnable_blocks(self, skill_text):
        # The delegation block is pasted into the AGENT'S shell, which may be zsh
        # (CC Bash tool): bash-only ${!var} indirection dies there with
        # "bad substitution" (live pilot wf_c33de294). POSIX-portable forms only.
        assert "${!" not in skill_text, "bash-only indirect expansion in a runnable block"

    def test_routing_table_present(self, skill_text):
        # SP4 calibration verdict pinned in SKILL.md (PR2): sonnet is the route,
        # opus no benefit, haiku not recommended for graded runs.
        assert "Model routing (SP4 calibration" in skill_text
        assert "sonnet" in skill_text and "opus buys nothing" in skill_text
        assert "haiku is NOT recommended" in skill_text

    def test_iter_complete_cycle_contract(self, skill_text):
        # The grader grades the highest COMPLETE cycle (PR2 fix) — the doc must not
        # promise the old "highest-K" rule that false-failed on an empty trailing dir.
        assert "highest-K **complete** cycle" in skill_text


def test_skill_md_resolves_plugin_without_requiring_plugin_root_env():
    """#236 (sibling of #221): the subagent-delegation block set PLUGIN to a "<plugin root>"
    placeholder hinting `$CLAUDE_PLUGIN_ROOT` — which is NOT exported to the Bash tool (empty),
    so `$PLUGIN/skills/look/scripts/launch.sh` would resolve to /skills/... and break. PLUGIN
    must SELF-RESOLVE from the cache (honor the var if set). Mirrors the consult fix (PR #235)."""
    text = _text()
    assert "ls -dt ~/.claude/plugins/cache/*/bulldozer/*/" in text
    assert '[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]' in text                # guarded honor-if-set
    # the placeholder that resolved to an empty PLUGIN must be gone:
    assert 'PLUGIN="<plugin root>"' not in text
    assert "# e.g. $CLAUDE_PLUGIN_ROOT" not in text
