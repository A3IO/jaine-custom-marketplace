#!/usr/bin/env python3
"""Unit tests for _render_ax_tree — pure function, no browser needed.
Run: pytest tests/test_ax_renderer.py -v
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

CDP_SCRIPT = Path(__file__).parent.parent / "skills" / "look" / "scripts" / "cdp.py"


def _load_cdp():
    """Import cdp.py as a module."""
    spec = importlib.util.spec_from_file_location("cdp_mod", str(CDP_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    old = os.environ.get("CDP_PORT")
    os.environ["CDP_PORT"] = "1"
    try:
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("CDP_PORT", None)
        else:
            os.environ["CDP_PORT"] = old
    return mod


@pytest.fixture(scope="module")
def cdp():
    return _load_cdp()


def _node(node_id, role, name="", parent_id=None, child_ids=None,
          ignored=False, backend_dom_node_id=None, properties=None):
    """Build a synthetic AX node matching CDP Accessibility.getFullAXTree shape."""
    n = {
        "nodeId": str(node_id),
        "role": {"value": role},
        "name": {"value": name},
    }
    if parent_id is not None:
        n["parentId"] = str(parent_id)
    if child_ids:
        n["childIds"] = [str(c) for c in child_ids]
    if ignored:
        n["ignored"] = True
    if backend_dom_node_id is not None:
        n["backendDOMNodeId"] = backend_dom_node_id
    if properties:
        n["properties"] = properties
    return n


def _prop(name, value):
    return {"name": name, "value": {"value": value}}


class TestRendererFilters:
    """§3.2: default filters (order matters, children survive parent skip)."""

    def test_ignored_node_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1, ignored=True)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("OK" in l for l in lines)

    def test_inline_text_box_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "InlineTextBox", "hello", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("hello" in l for l in lines)

    def test_generic_without_name_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "", parent_id=1, child_ids=[3]),
                 _node(3, "button", "OK", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("button" in l and "OK" in l for l in lines)
        assert not any(l.strip().startswith("- generic") for l in lines)

    def test_generic_shadow_host_survives(self, cdp):
        """§3.2 exception: shadow-host generic survives with marker."""
        shadow_map = {10: "open"}
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "", parent_id=1, child_ids=[3],
                       backend_dom_node_id=10),
                 _node(3, "button", "OK", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, shadow_map)
        host_lines = [l for l in lines if "[shadow=open]" in l]
        assert len(host_lines) == 1

    def test_static_text_duplicate_name_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1, child_ids=[3]),
                 _node(3, "StaticText", "OK", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        text_lines = [l for l in lines if "text:" in l]
        assert len(text_lines) == 0

    def test_static_text_different_name_kept(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "Submit", parent_id=1, child_ids=[3]),
                 _node(3, "StaticText", "Send now", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        text_lines = [l for l in lines if "text:" in l and "Send now" in l]
        assert len(text_lines) == 1

    def test_static_text_empty_name_skipped(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "StaticText", "", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("text:" in l for l in lines)

    def test_children_of_skipped_parent_at_parent_depth(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "", parent_id=1, child_ids=[3]),
                 _node(3, "button", "Deep", parent_id=2)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        btn = [l for l in lines if "Deep" in l][0]
        assert btn.startswith("- "), f"child of skipped generic should be at depth 0, got: {btn!r}"


class TestRendererOutput:
    """§3.1: output grammar invariants."""

    def test_ref_on_interactive_only(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3]),
                 _node(2, "button", "OK", parent_id=1, backend_dom_node_id=42),
                 _node(3, "heading", "Title", parent_id=1, backend_dom_node_id=43)]
        lines, meta = cdp._render_ax_tree([nodes], 500, False, {})
        btn_line = [l for l in lines if "button" in l][0]
        hdg_line = [l for l in lines if "heading" in l][0]
        assert "[ref=42]" in btn_line
        assert "[ref=" not in hdg_line

    def test_all_interactive_roles_get_ref(self, cdp):
        for i, role in enumerate(cdp.INTERACTIVE_ROLES):
            nodes = [_node(1, "RootWebArea", child_ids=[2]),
                     _node(2, role, "Test", parent_id=1, backend_dom_node_id=100+i)]
            lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
            ref_lines = [l for l in lines if "[ref=" in l]
            assert len(ref_lines) == 1, f"role {role} should get [ref=]"

    def test_disabled_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "X", parent_id=1,
                       properties=[_prop("disabled", True)])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[disabled]" in l for l in lines)

    def test_checked_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "checkbox", "Agree", parent_id=1,
                       properties=[_prop("checked", "true")])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[checked]" in l for l in lines)

    def test_checked_mixed_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "checkbox", "Mix", parent_id=1,
                       properties=[_prop("checked", "mixed")])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[checked=mixed]" in l for l in lines)

    def test_level_attr(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "heading", "Title", parent_id=1,
                       properties=[_prop("level", 2)])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("[level=2]" in l for l in lines)

    def test_value_rendered(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "slider", "Vol", parent_id=1)]
        nodes[1]["value"] = {"value": "50"}
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any(": 50" in l for l in lines)

    def test_name_whitespace_collapsed(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "Hello\n  World\t!", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any('"Hello World !"' in l for l in lines)

    def test_name_quotes_replaced(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", 'Say "hi"', parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("'hi'" in l for l in lines)

    def test_name_truncated_at_200(self, cdp):
        long_name = "A" * 250
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", long_name, parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        name_line = [l for l in lines if "button" in l][0]
        assert "…" in name_line

    def test_rootwebarea_unnamed_not_rendered(self, cdp):
        nodes = [_node(1, "RootWebArea", "", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert not any("RootWebArea" in l for l in lines)

    def test_rootwebarea_named_rendered(self, cdp):
        nodes = [_node(1, "RootWebArea", "My Page", child_ids=[2]),
                 _node(2, "button", "OK", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert any("RootWebArea" in l and "My Page" in l for l in lines)

    def test_indent_2_spaces_per_level(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "navigation", "Nav", parent_id=1, child_ids=[3]),
                 _node(3, "button", "OK", parent_id=2)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        btn = [l for l in lines if "OK" in l][0]
        assert btn.startswith("  - "), f"Expected 1 level indent, got: {btn!r}"

    def test_shadow_marker_open(self, cdp):
        shadow_map = {10: "open"}
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "Host", parent_id=1, backend_dom_node_id=10)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, shadow_map)
        assert any("[shadow=open]" in l for l in lines)

    def test_shadow_marker_closed(self, cdp):
        shadow_map = {10: "closed"}
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "generic", "Host", parent_id=1, backend_dom_node_id=10)]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, shadow_map)
        assert any("[shadow=closed]" in l for l in lines)


class TestRendererTruncation:
    """§3.1: truncation behavior."""

    def test_truncation_mid_branch(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3, 4])]
        for i in (2, 3, 4):
            nodes.append(_node(i, "button", f"Btn{i}", parent_id=1))
        lines, meta = cdp._render_ax_tree([nodes], 2, False, {})
        assert meta["truncated"]
        assert any("truncated" in l for l in lines)
        assert meta["shown"] == 2

    def test_truncation_marker_text(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3, 4]),
                 _node(2, "button", "A", parent_id=1),
                 _node(3, "button", "B", parent_id=1),
                 _node(4, "button", "C", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 1, False, {})
        marker = [l for l in lines if "truncated" in l]
        assert len(marker) == 1
        assert "--max-nodes 0" in marker[0]

    def test_no_limit_shows_all(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3]),
                 _node(2, "button", "A", parent_id=1),
                 _node(3, "button", "B", parent_id=1)]
        lines, meta = cdp._render_ax_tree([nodes], 0, False, {})
        assert not meta["truncated"]
        assert meta["shown"] == 2


class TestRendererRaw:
    """§3.1: --raw mode."""

    def test_raw_shows_ignored_with_marker(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "button", "X", parent_id=1, ignored=True)]
        lines, _ = cdp._render_ax_tree([nodes], 500, True, {})
        assert any("[ignored]" in l and "X" in l for l in lines)

    def test_raw_shows_inline_text_box(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2]),
                 _node(2, "InlineTextBox", "hi", parent_id=1)]
        lines, _ = cdp._render_ax_tree([nodes], 500, True, {})
        assert any("InlineTextBox" in l for l in lines)


class TestRendererMultiFrame:
    """Multi-frame rendering."""

    def test_multi_frame_second_block(self, cdp):
        main_nodes = [_node(1, "RootWebArea", child_ids=[2]),
                      _node(2, "button", "Main", parent_id=1)]
        child_nodes = [_node(10, "RootWebArea", child_ids=[11]),
                       _node(11, "button", "Child", parent_id=10)]
        lines, meta = cdp._render_ax_tree(
            [main_nodes, child_nodes], 500, False, {},
            frame_urls=["", "about:srcdoc"])
        assert meta["frames"] == 2
        frame_lines = [l for l in lines if l.startswith("frame:")]
        assert len(frame_lines) == 1

    def test_cycle_protection(self, cdp):
        nodes = [_node(1, "button", "A", child_ids=[2]),
                 _node(2, "button", "B", parent_id=1, child_ids=[1])]
        lines, _ = cdp._render_ax_tree([nodes], 500, False, {})
        assert len(lines) <= 3


class TestOOPIFWarnLogic:
    """§3.1: OOPIF warning counter (pure function)."""

    def test_more_iframe_roles_than_frames_warns(self, cdp):
        nodes = [_node(1, "RootWebArea", child_ids=[2, 3]),
                 _node(2, "Iframe", "f1", parent_id=1),
                 _node(3, "Iframe", "f2", parent_id=1)]
        _, meta = cdp._render_ax_tree([nodes], 500, False, {})
        assert meta.get("oopif_count", 0) >= 1
