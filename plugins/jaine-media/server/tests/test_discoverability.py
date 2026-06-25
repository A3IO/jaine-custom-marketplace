"""#228 discoverability — MCP instructions=, plugin skill, plugin.json description.

When the MCP server is down the model is blind to the media capability; when up,
routing must be unambiguous. These assert the three ship-side artifacts exist and
stay client/backend-agnostic (no "Claude"/"Gemini" in what ships)."""
import json
from pathlib import Path

import server

_ROOT = Path(server.__file__).resolve().parent.parent


# --- MCP instructions= (routing manifest injected when the server connects) ---

def test_mcp_has_instructions():
    assert server.mcp.instructions, "FastMCP must carry a routing instructions manifest"


def test_instructions_within_2kb():
    assert len(server.mcp.instructions) <= 2048


def test_instructions_routing_anchors():
    text = server.mcp.instructions
    for anchor in ("analyze_media", "extract_frame", "fetch_media",
                   "prepare_media", "list_models"):
        assert anchor in text, f"instructions must route to {anchor}"


def test_instructions_keeps_negative_constraint():
    # consult-panel fix: keep the "cannot natively inspect" motivation or routing weakens.
    # Normalize whitespace — the phrase may span a line wrap in the manifest.
    normalized = " ".join(server.mcp.instructions.split()).lower()
    assert "cannot natively" in normalized


def test_instructions_client_and_backend_agnostic():
    low = server.mcp.instructions.lower()
    assert "claude" not in low, "shipped instructions must not name the client"
    assert "gemini" not in low, "shipped instructions must not name the backend"


# --- skills/media/SKILL.md (discoverability + glue + graceful degradation) ---

_SKILL = _ROOT / "skills" / "media" / "SKILL.md"


def _frontmatter(md: str) -> dict:
    """Extract the block between the first two '---' fences as key: value pairs.
    Skill descriptions are single-line, so a per-line split is sufficient here."""
    assert md.startswith("---"), "SKILL.md must open with a '---' frontmatter fence"
    parts = md.split("---", 2)
    assert len(parts) >= 3, "frontmatter must be closed with a second '---'"
    fm = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def test_skill_file_exists():
    assert _SKILL.is_file()


def test_skill_frontmatter_name_and_description():
    fm = _frontmatter(_SKILL.read_text(encoding="utf-8"))
    assert fm.get("name") == "media", "name should match the dir (command = /jaine-media:media)"
    assert fm.get("description"), "description drives auto-invocation — required"


def test_skill_frontmatter_is_valid_yaml():
    try:
        import yaml
    except ImportError:
        import pytest
        pytest.skip("PyYAML not installed")
    block = _SKILL.read_text(encoding="utf-8").split("---", 2)[1]
    data = yaml.safe_load(block)
    assert isinstance(data, dict) and data.get("name") == "media"


def test_skill_description_within_1024():
    # skill-descriptions.md hard rule: description ≤ 1024 chars
    fm = _frontmatter(_SKILL.read_text(encoding="utf-8"))
    assert len(fm["description"]) <= 1024


def test_skill_description_agnostic():
    low = _frontmatter(_SKILL.read_text(encoding="utf-8"))["description"].lower()
    assert "claude" not in low and "gemini" not in low


def test_skill_description_is_directive():
    # skill-descriptions.md: directive wording ("ALWAYS invoke … Do NOT … manually")
    # activates ~100% vs ~37% for passive "Use when" — and activation IS the #228 goal.
    low = _frontmatter(_SKILL.read_text(encoding="utf-8"))["description"].lower()
    assert "always" in low, "lead with a directive ALWAYS-invoke trigger"
    assert "do not" in low or "don't" in low, "state the anti-pattern (don't write ffmpeg)"


def test_skill_body_routes_to_tools():
    body = _SKILL.read_text(encoding="utf-8")
    assert "analyze_media" in body and "extract_frame" in body


def test_skill_does_not_grant_blanket_bash():
    # least-privilege (codex P2): an auto-invoked guidance skill must not pre-grant Bash —
    # it would hand arbitrary shell to ordinary media prompts (incl. untrusted URLs).
    fm = _frontmatter(_SKILL.read_text(encoding="utf-8"))
    assert "bash" not in fm.get("allowed-tools", "").lower()


def test_skill_body_has_graceful_degradation():
    low = _SKILL.read_text(encoding="utf-8").lower()
    # when tools are absent → advise enabling the plugin, do NOT fall back to ffmpeg
    assert "reload-plugins" in low or "mcp list" in low
    assert "ffmpeg" in low


# --- plugin.json description (human-facing, marketplace; concrete + agnostic) ---

_PLUGIN_JSON = _ROOT / ".claude-plugin" / "plugin.json"


def _plugin_description() -> str:
    return json.loads(_PLUGIN_JSON.read_text(encoding="utf-8"))["description"]


def test_plugin_description_concrete_outcomes():
    low = _plugin_description().lower()
    for word in ("video", "audio", "frames"):
        assert word in low, f"description should state the concrete outcome: {word}"


def test_plugin_description_mentions_url_ingestion():
    low = _plugin_description().lower()
    assert "url" in low or "youtube" in low


def test_plugin_description_agnostic():
    low = _plugin_description().lower()
    assert "gemini" not in low, "marketplace description must not name the backend"
    assert "claude" not in low, "keep it capability-focused, not client-coupled"
