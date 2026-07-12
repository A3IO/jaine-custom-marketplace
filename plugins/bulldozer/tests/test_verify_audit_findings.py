"""Behavioral tests for the E1 consistency-audit verifier (#94).

The verifier's ONE deterministic guarantee: a finding survives iff every quote
it cites is verbatim-present where claimed (anti-hallucination). It does NOT
judge whether the cited text is a real defect — that is Claude's semantic call.
Fail-open: an unreadable/unparseable findings file => empty survivor set, exit 0.
"""
import json
import subprocess
import sys

from conftest import PLUGIN_ROOT

VERIFY = PLUGIN_ROOT / "skills" / "check" / "scripts" / "verify-audit-findings.py"
SCHEMA = PLUGIN_ROOT / "skills" / "check" / "data" / "e1-evidence-schema.json"


def _verify(fin, out, root):
    return subprocess.run(
        [sys.executable, str(VERIFY), "--findings", str(fin),
         "--out", str(out), "--project-root", str(root)],
        capture_output=True, text=True, timeout=10,
    )


def _run(tmp_path, findings):
    """Write findings to e1-findings.json, run the verifier, return (rc, survivors)."""
    fin = tmp_path / "e1-findings.json"
    fin.write_text(json.dumps({"findings": findings}))
    out = tmp_path / "e1-verified.json"
    r = _verify(fin, out, tmp_path)
    survivors = json.loads(out.read_text())["findings"] if out.exists() else None
    return r.returncode, survivors


def _run_raw(tmp_path, raw):
    """Like _run but writes raw findings-file text (for malformed-but-parseable cases)."""
    fin = tmp_path / "e1-findings.json"
    fin.write_text(raw)
    out = tmp_path / "e1-verified.json"
    r = _verify(fin, out, tmp_path)
    survivors = json.loads(out.read_text())["findings"] if out.exists() else None
    return r.returncode, survivors, r.stderr


def _doc(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return name


def test_present_quote_survives(tmp_path):
    _doc(tmp_path, "spec.md", "the default is paused\n...\nplayback begins immediately")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "stale_term", "file": "spec.md",
                             "quote": "the default is paused", "anchor": {"exclude_section": "Changelog"}}])
    assert rc == 0
    assert [f["id"] for f in s] == ["F1"]


def test_absent_quote_dropped(tmp_path):
    _doc(tmp_path, "spec.md", "real content only")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "stale_term", "file": "spec.md",
                             "quote": "hallucinated text not in file", "anchor": {}}])
    assert rc == 0
    assert s == []


def test_empty_quote_dropped(tmp_path):
    _doc(tmp_path, "spec.md", "anything")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "stale_term", "file": "spec.md",
                             "quote": "   ", "anchor": {}}])
    assert rc == 0
    assert s == []


def test_internal_contradiction_needs_both_quotes(tmp_path):
    _doc(tmp_path, "spec.md", "status X finalizes ended")  # only quote_a present
    rc, s = _run(tmp_path, [{"id": "F1", "class": "internal_contradiction", "file": "spec.md",
                             "quote": "status X finalizes ended",
                             "anchor": {"quote_b": "status X is a RuntimeError"}}])
    assert rc == 0
    assert s == []  # quote_b absent -> dropped


def test_internal_contradiction_survives_when_both_present(tmp_path):
    _doc(tmp_path, "spec.md", "status X finalizes ended\n...\nstatus X is a RuntimeError")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "internal_contradiction", "file": "spec.md",
                             "quote": "status X finalizes ended",
                             "anchor": {"quote_b": "status X is a RuntimeError"}}])
    assert rc == 0
    assert [f["id"] for f in s] == ["F1"]


def test_cross_spec_drift_needs_other_file_quote(tmp_path):
    _doc(tmp_path, "a.md", "field optional")
    _doc(tmp_path, "b.md", "unrelated")  # other_quote absent here
    rc, s = _run(tmp_path, [{"id": "F1", "class": "cross_spec_drift", "file": "a.md",
                             "quote": "field optional",
                             "anchor": {"other_file": "b.md", "other_quote": "field required"}}])
    assert rc == 0
    assert s == []


def test_missing_findings_file_fail_open(tmp_path):
    out = tmp_path / "e1-verified.json"
    r = subprocess.run(
        [sys.executable, str(VERIFY), "--findings", str(tmp_path / "nope.json"),
         "--out", str(out), "--project-root", str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []


def test_unparseable_findings_fail_open(tmp_path):
    fin = tmp_path / "e1-findings.json"
    fin.write_text("{not json")
    out = tmp_path / "e1-verified.json"
    r = _verify(fin, out, tmp_path)
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []


def test_non_utf8_findings_file_fail_open(tmp_path):
    """A findings file with invalid UTF-8 bytes must fail-open, not crash on decode
    (UnicodeDecodeError is a ValueError, not OSError/JSONDecodeError)."""
    fin = tmp_path / "e1-findings.json"
    fin.write_bytes(b'{"findings":[{"id":"X","class":"stale_term","file":"spec.md",'
                    b'"quote":"\xff\xfe","anchor":{}}]}')
    _doc(tmp_path, "spec.md", "ok")
    out = tmp_path / "e1-verified.json"
    r = _verify(fin, out, tmp_path)
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []


def test_binary_cited_file_dropped(tmp_path):
    """A finding citing an in-tree binary (non-UTF-8) file must drop, not crash _read."""
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfebinary")
    rc, s = _run(tmp_path, [{"id": "Y", "class": "stale_term", "file": "img.png",
                             "quote": "binary", "anchor": {}}])
    assert rc == 0
    assert s == []


# --- R1-F2: malformed-but-parseable auditor output must FAIL-OPEN, never crash ---

def test_top_level_array_fail_open(tmp_path):
    """A top-level JSON array (not the {findings: [...]} object) must not crash .get."""
    _doc(tmp_path, "spec.md", "quote here")
    rc, s, err = _run_raw(
        tmp_path,
        '[{"id":"X","class":"stale_term","file":"spec.md","quote":"quote here","anchor":{}}]',
    )
    assert rc == 0, err
    assert s == []


def test_non_string_quote_dropped(tmp_path):
    """A non-string quote must drop the finding, not crash in _present (.strip)."""
    _doc(tmp_path, "spec.md", "quote here")
    rc, s = _run(tmp_path, [{"id": "X", "class": "stale_term", "file": "spec.md",
                             "quote": 123, "anchor": {}}])
    assert rc == 0
    assert s == []


def test_non_dict_anchor_dropped(tmp_path):
    """A non-dict anchor must drop the finding, not crash at anchor.get."""
    _doc(tmp_path, "spec.md", "quote here")
    rc, s = _run(tmp_path, [{"id": "X", "class": "internal_contradiction", "file": "spec.md",
                             "quote": "quote here", "anchor": "oops"}])
    assert rc == 0
    assert s == []


def test_non_string_file_dropped(tmp_path):
    """A non-string file must drop the finding, not crash in Path(root) / rel."""
    _doc(tmp_path, "spec.md", "quote here")
    rc, s = _run(tmp_path, [{"id": "X", "class": "stale_term", "file": ["spec.md"],
                             "quote": "quote here", "anchor": {}}])
    assert rc == 0
    assert s == []


# --- R1-F1: only the four schema classes may survive (contract guard) ---

def test_unknown_class_dropped(tmp_path):
    """An out-of-contract class survives quote-presence today; it must be dropped."""
    _doc(tmp_path, "spec.md", "quote here")
    rc, s = _run(tmp_path, [{"id": "X", "class": "style", "file": "spec.md",
                             "quote": "quote here", "anchor": {}}])
    assert rc == 0
    assert s == []


def test_unhashable_class_dropped(tmp_path):
    """A non-string (unhashable) class must drop, not crash the `cls not in
    VALID_CLASSES` membership test (regression from the R1-F2 whitelist fix)."""
    _doc(tmp_path, "spec.md", "quote here")
    rc, s = _run(tmp_path, [
        {"id": "L", "class": ["stale_term"], "file": "spec.md",
         "quote": "quote here", "anchor": {}},
        {"id": "D", "class": {"x": "stale_term"}, "file": "spec.md",
         "quote": "quote here", "anchor": {}},
    ])
    assert rc == 0
    assert s == []


def test_known_classes_survive_drift_guard(tmp_path):
    """All four schema classes (with present quotes + valid anchors) survive.

    Pins the verifier whitelist to the schema's anchor_by_class keys — the
    schema test alone (TestE1SchemaContract) never exercised the verifier.
    """
    schema = json.loads(SCHEMA.read_text())
    assert set(schema["anchor_by_class"]) == {
        "dead_ref", "internal_contradiction", "cross_spec_drift", "stale_term"
    }
    _doc(tmp_path, "spec.md", "alpha\nbeta")
    _doc(tmp_path, "sib.md", "gamma")
    findings = [
        {"id": "dr", "class": "dead_ref", "file": "spec.md",
         "quote": "alpha", "anchor": {"ref": "alpha"}},
        {"id": "ic", "class": "internal_contradiction", "file": "spec.md",
         "quote": "alpha", "anchor": {"quote_b": "beta"}},
        {"id": "cd", "class": "cross_spec_drift", "file": "spec.md",
         "quote": "alpha", "anchor": {"other_file": "sib.md", "other_quote": "gamma"}},
        {"id": "st", "class": "stale_term", "file": "spec.md",
         "quote": "beta", "anchor": {"exclude_section": "X"}},
    ]
    rc, s = _run(tmp_path, findings)
    assert rc == 0
    assert {f["id"] for f in s} == {"dr", "ic", "cd", "st"}


# --- R1-F3: reads must stay under project-root ---

def test_parent_traversal_dropped(tmp_path):
    """A ../ file escaping project-root must be dropped (no out-of-tree read)."""
    (tmp_path / "secret.md").write_text("TOPSECRET")  # sibling of proj, outside root
    proj = tmp_path / "proj"
    proj.mkdir()
    fin = proj / "e1-findings.json"
    fin.write_text(json.dumps({"findings": [
        {"id": "X", "class": "stale_term", "file": "../secret.md",
         "quote": "TOPSECRET", "anchor": {}}]}))
    out = proj / "e1-verified.json"
    r = _verify(fin, out, proj)
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []


def test_absolute_path_outside_dropped(tmp_path):
    """An absolute file outside project-root must be dropped."""
    secret = tmp_path / "secret.md"
    secret.write_text("ABSSECRET")
    proj = tmp_path / "proj"
    proj.mkdir()
    fin = proj / "e1-findings.json"
    fin.write_text(json.dumps({"findings": [
        {"id": "X", "class": "stale_term", "file": str(secret),
         "quote": "ABSSECRET", "anchor": {}}]}))
    out = proj / "e1-verified.json"
    r = _verify(fin, out, proj)
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []


def test_in_tree_subdir_still_read(tmp_path):
    """Containment must not over-restrict: a real in-tree subdir file still survives."""
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "x.md").write_text("present here")
    rc, s = _run(tmp_path, [{"id": "X", "class": "stale_term", "file": "docs/x.md",
                             "quote": "present here", "anchor": {}}])
    assert rc == 0
    assert [f["id"] for f in s] == ["X"]


def test_string_anchor_drop_reason_on_stderr(tmp_path):
    """#184: an anchor-requiring class with a STRING anchor is dropped (fail-open
    stays intact) but must SAY so on stderr — the incident hid 5 verbatim-present
    findings behind the silent isinstance coercion and the audit looked clean."""
    _doc(tmp_path, "spec.md", "A says up\nB says down")
    fin = tmp_path / "e1-findings.json"
    fin.write_text(json.dumps({"findings": [
        {"id": "F1", "class": "internal_contradiction", "file": "spec.md",
         "quote": "A says up", "anchor": "Section 3"},
        {"id": "F2", "class": "cross_spec_drift", "file": "spec.md",
         "quote": "B says down", "anchor": "see other spec"},
    ]}))
    out = tmp_path / "e1-verified.json"
    r = _verify(fin, out, tmp_path)
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []
    for fid in ("F1", "F2"):
        assert fid in r.stderr, "no drop reason for {} on stderr".format(fid)
    assert "anchor" in r.stderr and "str" in r.stderr


def test_string_anchor_tolerated_where_not_required(tmp_path):
    """#184 companion: dead_ref/stale_term need no anchor sub-fields — a string
    anchor there must neither drop the finding nor spam stderr."""
    _doc(tmp_path, "spec.md", "see section 9 for details")
    fin = tmp_path / "e1-findings.json"
    fin.write_text(json.dumps({"findings": [
        {"id": "F1", "class": "dead_ref", "file": "spec.md",
         "quote": "see section 9", "anchor": "section 9"}]}))
    out = tmp_path / "e1-verified.json"
    r = _verify(fin, out, tmp_path)
    assert r.returncode == 0
    assert [f["id"] for f in json.loads(out.read_text())["findings"]] == ["F1"]
    assert "WARNING" not in r.stderr, "tolerated class must not warn: " + r.stderr


def test_warning_write_failure_stays_fail_open(tmp_path):
    """PR #339 codex P2, reproduced live: with stderr a broken pipe, the #184
    warning print raised EPIPE before the survivors file was written — exit 120,
    no out file. The diagnostic must be best-effort: fail-open exit-0 + out file
    ALWAYS, whatever stderr is."""
    import os
    _doc(tmp_path, "spec.md", "A says up")
    fin = tmp_path / "e1-findings.json"
    fin.write_text(json.dumps({"findings": [
        {"id": "F1", "class": "internal_contradiction", "file": "spec.md",
         "quote": "A says up", "anchor": "Section 3"}]}))
    out = tmp_path / "e1-verified.json"
    r, w = os.pipe()
    os.close(r)  # reader gone → child's stderr writes hit EPIPE
    try:
        p = subprocess.run(
            [sys.executable, str(VERIFY), "--findings", str(fin),
             "--out", str(out), "--project-root", str(tmp_path)],
            stderr=w, stdout=subprocess.PIPE, timeout=10)
    finally:
        os.close(w)
    assert p.returncode == 0
    assert json.loads(out.read_text())["findings"] == []
