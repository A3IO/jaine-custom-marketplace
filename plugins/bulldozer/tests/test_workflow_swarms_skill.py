#!/usr/bin/env python3
"""Structural drift-guards for skills/workflow-swarms/SKILL.md. Offline."""
import os

SKILL = os.path.join(
    os.path.dirname(__file__), "..", "skills", "workflow-swarms", "SKILL.md"
)


def _text():
    with open(SKILL) as f:
        return f.read()


def test_skill_exists():
    assert os.path.isfile(SKILL)


def test_feedback_version_resolves_without_requiring_plugin_root_env():
    """#236 (sibling of #221): $CLAUDE_PLUGIN_ROOT is NOT exported to the Bash tool
    (empirically empty in CC 2.1.185), so the feedback template's
    `jq -r .version "$CLAUDE_PLUGIN_ROOT/..."` records an empty version. Resolve plugin.json
    from the cache instead (honor the var if set). Mirrors the consult fix (PR #235)."""
    text = _text()
    assert (
        "ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json" in text
    )
    assert 'jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json"' not in text
