"""Unit tests for skills/check/scripts/parse-ledger-patch.py.

The parser is invoked as a subprocess so the tests exercise the actual CLI
contract — exit codes, stderr lines, stdout JSON, malformed-file side-effects —
the same surface a wrapper script in PR1b would use.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import PLUGIN_ROOT

SCRIPT = PLUGIN_ROOT / "skills" / "check" / "scripts" / "parse-ledger-patch.py"
FIXTURES = Path(__file__).parent / "fixtures" / "check"

pytest.importorskip("yaml", reason="parse-ledger-patch.py needs PyYAML; skip parser tests if absent")


def run_parser(
    *,
    stdin_text: str | None = None,
    file: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the parser. Supply either stdin_text OR file."""
    cmd: list[str] = [sys.executable, str(SCRIPT)]
    if file is not None:
        cmd.extend(["--file", str(file)])
    return subprocess.run(
        cmd,
        input=stdin_text if stdin_text is not None else "",
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=10,
    )


def load_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Decode the stdout JSON; fail with a useful message on parse error."""
    assert result.returncode == 0, (
        f"parser exited {result.returncode}\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}"
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"parser stdout is not valid JSON: {exc}\nstdout:\n{result.stdout}")


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_minimal_go_exits_zero(self):
        result = run_parser(file=FIXTURES / "verdict-minimal-go.txt")
        assert result.returncode == 0, result.stderr

    def test_minimal_nogo_exits_zero(self):
        result = run_parser(file=FIXTURES / "verdict-minimal-nogo.txt")
        assert result.returncode == 0, result.stderr

    def test_no_block_exits_one(self):
        result = run_parser(file=FIXTURES / "verdict-no-block.txt")
        assert result.returncode == 1
        assert "no ledger_patch block found" in result.stderr.lower()

    def test_malformed_yaml_exits_two(self, tmp_path: Path):
        # Copy fixture into tmp_path so the .malformed.yml side-effect is sandboxed.
        target = tmp_path / "verdict-r1.txt"
        target.write_text((FIXTURES / "verdict-malformed-yaml.txt").read_text())
        result = run_parser(file=target)
        assert result.returncode == 2, result.stderr
        # R1-F4 fix: .with_suffix(".malformed.yml") replaces `.txt`, doesn't append.
        malformed = tmp_path / "verdict-r1.malformed.yml"
        legacy_appended = tmp_path / "verdict-r1.txt.malformed.yml"
        assert malformed.exists(), (
            f"expected malformed-block dump at {malformed} after exit 2 "
            f"(SKILL.md Step 4 contract: 'verdict-r{{N}}.malformed.yml')"
        )
        assert not legacy_appended.exists(), (
            f"legacy '.txt.malformed.yml' path should not be used; got {legacy_appended}"
        )
        assert "LEDGER_PATCH" in malformed.read_text()

    def test_missing_file_exits_five(self, tmp_path: Path):
        # Issue #100 case #5: was exit 3 (conflated with schema violation).
        # Now exit 5 — file-not-found is a caller mistake (wrong path), not a
        # structurally wrong patch. Different actions: caller fixes path / retry,
        # not "STOP and ask user about the patch body".
        result = run_parser(file=tmp_path / "does-not-exist.txt")
        assert result.returncode == 5, (
            f"missing file must exit 5 (file error), not 3 (schema violation). "
            f"stderr:\n{result.stderr}"
        )
        assert "file not found" in result.stderr.lower()

    def test_directory_passed_as_file_exits_five(self, tmp_path: Path):
        """Hotfix R3-F1: `--file <dir>` must exit 5, not traceback to exit 1.

        Before fix: `args.file.read_text()` was outside try/except. Passing a
        directory raised IsADirectoryError, which Python printed as a
        traceback and exited 1 — collides with "no LEDGER_PATCH" semantics,
        breaking the SKILL.md table promise that "unreadable verdict file"
        is exit 5. Reproduced by codex dogfood round 2 via:
            python3 parse-ledger-patch.py --file .

        After: read_text() wrapped in try/except OSError → exits 5 with a
        diagnostic identifying the unreadable path.
        """
        result = run_parser(file=tmp_path)  # tmp_path is a directory, not a file
        assert result.returncode == 5, (
            f"directory as --file must exit 5 (IO failure), got "
            f"{result.returncode}.\nstderr:\n{result.stderr}"
        )
        # No raw Python traceback — wrapper expects a clean diagnostic.
        assert "Traceback" not in result.stderr, (
            f"OSError traceback should be caught and reformatted.\n"
            f"stderr:\n{result.stderr}"
        )

    def test_stdin_io_error_exits_five(self, tmp_path: Path, monkeypatch):
        # Code-review AL5: symmetric exit-5 coverage for stdin. A closed/broken
        # stdin pipe previously surfaced as an uncaught OSError → Python
        # traceback + exit 1, conflating with "no LEDGER_PATCH block found" so
        # SKILL.md Step 4 routed to manual prose extraction for an actual
        # pipe-wiring problem.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "parser", PLUGIN_ROOT / "skills" / "check" / "scripts" / "parse-ledger-patch.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Force sys.stdin.read to raise OSError (simulates broken pipe).
        class _BrokenStdin:
            def read(self):
                raise OSError("[Errno 9] Bad file descriptor")

        monkeypatch.setattr(mod.sys, "stdin", _BrokenStdin())
        monkeypatch.setattr(mod.sys, "argv", ["parser"])  # stdin mode

        rc = mod.main()
        assert rc == 5, (
            f"stdin io error must exit 5 (symmetric with --file not found), "
            f"got {rc}"
        )


class TestLedgerPatchNonMappingValue:
    """Issue #100 case #6: `LEDGER_PATCH: <prose>` (key holds a scalar, not a
    mapping) is "no usable structured block" — exit 1 (fall back to manual
    extraction), not exit 3 (structurally-wrong-patch STOP-and-ask).

    Distinct from "block exists but missing required fields" — that's still
    exit 3 because the block IS a mapping.
    """

    def test_ledger_patch_prose_value_exits_one(self):
        text = "foo\nLEDGER_PATCH: TBD\nbar\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 1, (
            f"`LEDGER_PATCH: TBD` (scalar value) must exit 1 (no usable block), "
            f"not 3 (schema violation). stderr:\n{result.stderr}"
        )

    def test_ledger_patch_integer_value_exits_one(self):
        text = "LEDGER_PATCH: 42\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 1

    def test_ledger_patch_null_value_still_exits_one(self):
        # `LEDGER_PATCH:` with no value → yaml loads to {LEDGER_PATCH: None}.
        # body = None, not a mapping → exit 1 (was: exit 3).
        text = "LEDGER_PATCH:\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 1

    def test_ledger_patch_proper_mapping_with_schema_violation_still_exits_three(self):
        # Regression guard: when LEDGER_PATCH key holds a real mapping but the
        # body violates schema (e.g. findings missing), exit 3 stays exit 3.
        text = "LEDGER_PATCH:\n  verdict: no_go\n"  # no `findings:` key
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr


class TestMultiBlockMalformedFallback:
    """Issue #100 case #11: if a verdict has multiple LEDGER_PATCH blocks and
    only the LAST one has a YAML syntax error, prior valid blocks were silently
    discarded — exit 2 with no usable output.

    New behavior: fall back to earlier valid blocks rather than lose the prior
    reviewer work. Last block is still the canonical source — fallback only
    triggers on malformed YAML, not on schema violations (which signal "patch
    structurally wrong, stop", same intent regardless of position).
    """

    def test_last_malformed_falls_back_to_previous_valid(self, tmp_path: Path):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-PRIOR-VALID\n"
            "      severity: high\n"
            "      title: \"prior valid block — must survive last-malformed\"\n"
            "\n"
            "Mid-text noise to keep blocks separate.\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-NEW\n"
            "      severity: [unclosed list\n"  # malformed YAML
        )
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 0, (
            f"earlier valid block must be used when last is malformed; "
            f"got exit {result.returncode}. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        ids = [f["id"] for f in payload["findings"]]
        assert ids == ["R1-F-PRIOR-VALID"], (
            f"expected fallback to prior valid block; got ids {ids}"
        )
        # Caller needs to know fallback happened — emit warning on stderr.
        assert "fallback" in result.stderr.lower() or "earlier" in result.stderr.lower()

    def test_fallback_provenance_surfaces_in_json_payload(self, tmp_path: Path):
        # Dogfood R1-F2: fallback was only on stderr — JSON consumers reading
        # parsed-rN.json had no way to know they're applying a stale earlier
        # block instead of the latest one. Surface via payload.warnings and
        # payload.meta for in-band provenance.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-PRIOR\n"
            "      severity: high\n"
            "      title: \"prior valid\"\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-NEW\n"
            "      severity: [unclosed\n"  # malformed
        )
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)

        # warnings array must include the fallback signal — not only stderr.
        warnings_blob = " ".join(payload.get("warnings", []))
        assert "malformed" in warnings_blob.lower() and "earlier" in warnings_blob.lower(), (
            f"fallback warning must be in payload.warnings (not only stderr). "
            f"got warnings: {payload.get('warnings')}"
        )

        # Meta records which block index was used and total blocks, so a
        # downstream consumer can reconstruct provenance.
        meta = payload.get("meta", {})
        assert meta.get("used_block_index") == 0, (
            f"meta.used_block_index must point at the fallback block (index 0 of 2); "
            f"got meta: {meta}"
        )
        assert meta.get("total_blocks") == 2

    def test_all_blocks_malformed_exits_two(self, tmp_path: Path):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-BAD-A\n"
            "      severity: [unclosed\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-BAD-B\n"
            "      severity: [also unclosed\n"
        )
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 2, (
            f"all-blocks-malformed must exit 2, got {result.returncode}. "
            f"stderr:\n{result.stderr}"
        )

    def test_last_schema_violation_does_NOT_fall_back(self, tmp_path: Path):
        # Regression guard: schema violation in last block (e.g. missing
        # findings) is "patch structurally wrong, stop and ask" — same intent
        # whether it's the only block or the last of several. Falling back
        # would silently substitute stale reviewer output.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F-PRIOR\n"
            "      severity: high\n"
            "      title: \"earlier valid\"\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  verdict: no_go\n"  # missing findings key — schema violation, exit 3
        )
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 3, (
            f"last block with schema violation must exit 3 (not silently fall back). "
            f"got {result.returncode}. stderr:\n{result.stderr}"
        )

    def test_single_block_malformed_still_exits_two(self, tmp_path: Path):
        # Regression guard: single malformed block — no fallback to consider.
        text = "LEDGER_PATCH:\n  findings:\n    - id: foo\n      severity: [bad\n"
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 2

    def test_meta_collision_with_fallback_keys_warns(self, tmp_path: Path):
        # Code-review C4: when the earlier-valid block (which becomes the
        # active fallback) supplies meta keys named used_block_index or
        # total_blocks, parser previously silently overwrote them with the
        # fallback-injected provenance values. Now: warn so the loss is
        # visible. The collision-bearing keys must live in the earlier
        # block (the one fallback uses), not in the malformed last block.
        text = (
            "LEDGER_PATCH:\n"
            "  total_blocks: 999\n"
            "  used_block_index: 42\n"
            "  findings:\n"
            "    - id: F-OK\n"
            "      severity: high\n"
            "      title: \"valid earlier with colliding meta\"\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: F-BAD\n"
            "      severity: [unclosed\n"
        )
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        # Fallback-injected values win (callers need reliable provenance).
        assert payload["meta"]["used_block_index"] == 0
        assert payload["meta"]["total_blocks"] == 2
        # But the original values (999 and 42) MUST be flagged in warnings.
        warnings_blob = " ".join(payload.get("warnings", []))
        assert "999" in warnings_blob and "42" in warnings_blob, (
            f"meta key collision must surface a warning naming the original values; "
            f"got warnings: {payload.get('warnings')}"
        )
        assert "overwrot" in warnings_blob.lower(), (
            f"warning should say 'overwrote'; got: {warnings_blob}"
        )

    def test_fallback_does_not_mention_devnull_in_stderr(self, tmp_path: Path):
        # Code-review E1: previously, fallback iterations over multiple
        # malformed blocks passed Path("/dev/null") to parse() which wrote into
        # it and emitted a misleading "WARN: malformed YAML saved to /dev/null"
        # — claiming the content was saved while actually discarding it. Now
        # fallback iterations pass None (no target) so the misleading message
        # is suppressed entirely.
        #
        # Scenario: 3 blocks [VALID, MALFORMED-1, MALFORMED-2]. Loop tries
        # idx=2 (writes real target, exit 2), idx=1 (would write devnull, exit 2),
        # idx=0 (valid, exit 0). After fix: idx=1 dump is silently dropped.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: F-OK\n"
            "      severity: high\n"
            "      title: \"valid earlier\"\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: F-BAD-MIDDLE\n"
            "      severity: [unclosed-middle\n"
            "\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: F-BAD-LAST\n"
            "      severity: [unclosed-last\n"
        )
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "/dev/null" not in result.stderr, (
            f"fallback must not leak /dev/null path to stderr; got:\n{result.stderr}"
        )
        # The legitimate "malformed yaml saved to <real-path>" for the LAST
        # block (in cwd, since stdin mode) still fires — that's the user's
        # primary diagnostic signal.
        assert "stdin-ledger.malformed.yml" in result.stderr


class TestBareGoSynthesis:
    """Issue #100 case #17 (ALTITUDE): a reviewer that emits bare 'GO' instead
    of the structured LEDGER_PATCH block previously triggered exit 1, falling
    back to fragile manual prose extraction — the exact discipline failure
    PR1a (#101) was meant to eliminate.

    Parser now synthesizes {verdict: go, findings: []} when verdict body has
    a line matching `^\\s*GO\\s*$` AND no LEDGER_PATCH block. Warning emitted
    on stderr and embedded in the payload so the caller knows synthesis
    happened. NO-GO + findings cannot be safely synthesized — only the
    unambiguous GO path qualifies.

    The Round-N prompt directive added in PR #106 (issue #104) becomes
    belt-and-suspenders: it still asks for the structured block, but a model
    that ignores it no longer corrupts the workflow.
    """

    def test_bare_go_alone_synthesizes_empty_findings(self):
        text = "Verdict body prose here.\n\nGO\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"bare GO line must synthesize empty findings (exit 0 with warning), "
            f"got exit {result.returncode}. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert payload["findings"] == []
        assert payload["meta"].get("verdict") == "go"
        # Caller needs an explicit signal that synthesis happened.
        assert "synthes" in result.stderr.lower()
        assert payload.get("source") == "synthesized_bare_go", (
            f"source must signal synthesis, got {payload.get('source')!r}"
        )

    def test_bare_go_with_surrounding_whitespace_synthesizes(self):
        text = "Some prose.\n   GO   \nMore prose.\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["meta"].get("verdict") == "go"

    def test_no_go_does_not_synthesize(self):
        # Regression guard: NO-GO must NOT synthesize (it's not safe — there
        # should be findings the reviewer didn't structure).
        text = "Some prose.\nVerdict: NO-GO\nFindings would go here.\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 1, (
            f"NO-GO without LEDGER_PATCH must exit 1 (manual extraction), "
            f"not synthesize. got {result.returncode}"
        )

    def test_go_inside_prose_does_not_synthesize(self):
        text = "Let's GO ahead with this approach.\nDecision: pending.\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 1

    def test_ledger_patch_takes_precedence_over_bare_go(self):
        # If both a real LEDGER_PATCH block AND a stray "GO" line exist,
        # the structured block wins — synthesis is the fallback, not an override.
        text = (
            "GO\n"
            "Then later...\n"
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"real finding\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        # Structured block delivers a finding — synthesis would have given empty.
        assert payload["findings"] and payload["findings"][0]["id"] == "R1-F1"
        assert "synthes" not in result.stderr.lower()

    def test_no_go_prose_with_bare_go_line_does_not_synthesize(self):
        # R1-F1 from dogfood: NO-GO prose containing a bare "GO" line later
        # (e.g. instructional "the reviewer should not write: GO") previously
        # synthesized {verdict: go, findings: []}, silently losing real findings.
        text = (
            "Review verdict: NO-GO\n"
            "\n"
            "The reviewer should not write:\n"
            "GO\n"
            "\n"
            "because there are findings above.\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 1, (
            f"NO-GO prose with stray bare 'GO' must NOT synthesize (loses findings); "
            f"got exit {result.returncode}. stderr:\n{result.stderr}"
        )
        # stdout should be empty (no synthesized JSON)
        assert not result.stdout.strip(), (
            f"NO-GO precedence: no synthesized JSON should be emitted. stdout:\n{result.stdout}"
        )

    def test_no_underscore_go_also_suppresses_synthesis(self):
        # Defensive: NO_GO (underscore) and "NO GO" (space) are alternate spellings.
        for nogo in ("NO_GO", "no_go", "NO GO", "no-go"):
            text = f"Verdict: {nogo}\n\nGO\n"
            result = run_parser(stdin_text=text)
            assert result.returncode == 1, (
                f"{nogo!r} verdict + bare GO must NOT synthesize, got {result.returncode}"
            )

    def test_no_dash_unicode_variants_suppress_synthesis(self):
        # Code-review D1: em-dash (U+2014) and en-dash (U+2013) are common in
        # codex prose formatting. _NO_GO_RE must recognize NO–GO and NO—GO.
        for sep in ("–", "—"):  # en-dash, em-dash
            text = f"Verdict: NO{sep}GO\n\nGO\n"
            result = run_parser(stdin_text=text)
            assert result.returncode == 1, (
                f"NO{sep}GO ({hex(ord(sep))}) verdict + bare GO must NOT synthesize, "
                f"got {result.returncode}"
            )

    def test_no_multi_space_go_suppresses_synthesis(self):
        # Code-review A4: multi-space separator (column-aligned output) and
        # other multi-char separators.
        for sep in ("  ", "   ", "\t ", " \t", "--", "__"):
            text = f"Verdict: NO{sep}GO\n\nGO\n"
            result = run_parser(stdin_text=text)
            assert result.returncode == 1, (
                f"NO{sep!r}GO verdict + bare GO must NOT synthesize, got {result.returncode}"
            )

    def test_no_go_identifier_suffix_does_NOT_suppress(self):
        # Code-review E3: NOGO_FLAG, NO_GO_STATE etc. — identifier-like
        # strings that happen to contain NO+GO+_ shouldn't fire suppression.
        # Stray bare GO line should still synthesize in this case.
        for ident in ("NOGO_FLAG", "NO_GO_STATE", "NO_GO_MODE"):
            text = f"Avoid {ident} = True.\nThe build should print:\nGO\nto succeed.\n"
            result = run_parser(stdin_text=text)
            assert result.returncode == 0, (
                f"identifier {ident!r} (followed by underscore) should NOT block "
                f"synthesis when a real bare GO appears; got {result.returncode}. "
                f"stderr:\n{result.stderr}"
            )

    def test_bare_go_inside_fenced_code_does_not_synthesize(self):
        # Code-review D4: a GO line inside a ``` fenced code block is
        # illustrative (showing the directive format) — not the actual verdict.
        # Synthesis MUST NOT fire on this case.
        text = (
            "Reviewer's prose explanation.\n"
            "\n"
            "The directive should look like:\n"
            "```\n"
            "GO\n"
            "```\n"
            "\n"
            "But this verdict isn't actually GO.\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 1, (
            f"GO inside fenced code must NOT synthesize (it's illustrative); "
            f"got {result.returncode}. stderr:\n{result.stderr}"
        )

    def test_bare_go_outside_fence_with_no_go_inside_fence_synthesizes(self):
        # Mirror: NO-GO inside a fenced example block, real bare GO outside.
        # The fenced NO-GO is illustrative, not the actual verdict — so the
        # real bare GO outside the fence should synthesize.
        text = (
            "Don't write this:\n"
            "```\n"
            "Verdict: NO-GO\n"
            "```\n"
            "\n"
            "Actually:\n"
            "GO\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"NO-GO inside fence is illustrative; real bare GO outside fence "
            f"should still synthesize. got {result.returncode}"
        )

    def test_bare_go_outside_fence_with_real_no_go_outside_fence_does_not_synthesize(self):
        # Regression guard: real NO-GO outside any fence still suppresses
        # synthesis (covered by earlier tests, but worth pinning explicitly
        # alongside the fence-handling tests).
        text = (
            "Verdict: NO-GO\n"
            "\n"
            "```\n"
            "Example only:\n"
            "GO\n"
            "```\n"
            "\n"
            "GO\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 1

    def test_no_and_go_on_adjacent_lines_does_NOT_suppress(self):
        # Code-review C1: "NO" at end of one line followed immediately by
        # "GO" at the start of the next is a false-positive trigger of the
        # original \s-permissive regex (newline counted as a single
        # separator char). After the rewrite, the separator class excludes
        # newlines — same-line separators only. A real bare-GO line still
        # synthesizes.
        text = (
            "Choice: answer NO\n"
            "GO ahead anyway is wrong\n"
            "\n"
            "Final:\n"
            "GO\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"adjacent-line NO/GO (cross-paragraph false-positive) must NOT "
            f"suppress synthesis; got {result.returncode}. stderr:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Empty / GO verdicts
# ---------------------------------------------------------------------------

class TestEmptyFindings:
    def test_minimal_go_has_empty_findings(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-go.txt"))
        assert payload["findings"] == []
        assert payload["source"] == "empty_findings"

    def test_minimal_go_meta_contains_verdict(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-go.txt"))
        assert payload["meta"].get("verdict") == "go"


# ---------------------------------------------------------------------------
# Single-finding extraction
# ---------------------------------------------------------------------------

class TestMinimalNogo:
    def test_one_finding_extracted(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-nogo.txt"))
        assert len(payload["findings"]) == 1

    def test_finding_fields_present(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-nogo.txt"))
        finding = payload["findings"][0]
        assert finding["id"] == "R1-F1"
        assert finding["severity"] == "high"
        assert finding["status"] == "open"
        assert finding["title"] == "example bug for fixture testing"

    def test_finding_files_parsed_as_dict(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-nogo.txt"))
        files = payload["findings"][0]["files"]
        assert files == [{"path": "src/example.py", "lines": "42"}]

    def test_source_marked_ledger_patch_for_nonempty(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-nogo.txt"))
        assert payload["source"] == "ledger_patch"


# ---------------------------------------------------------------------------
# Schema drift — summary↔title, severity aliases, files as strings
# ---------------------------------------------------------------------------

class TestSchemaDrift:
    def test_summary_field_becomes_title(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-schema-drift.txt"))
        f1 = next(f for f in payload["findings"] if f["id"] == "R1-F1")
        assert f1["title"] == "drift: summary field instead of title"

    def test_summary_to_title_emits_warning(self):
        result = run_parser(file=FIXTURES / "verdict-schema-drift.txt")
        assert result.returncode == 0
        assert "using 'summary' as title" in result.stderr.lower()

    def test_severity_critical_normalized_to_blocker(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-schema-drift.txt"))
        f1 = next(f for f in payload["findings"] if f["id"] == "R1-F1")
        assert f1["severity"] == "blocker"

    def test_severity_warning_normalized_to_low(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-schema-drift.txt"))
        f2 = next(f for f in payload["findings"] if f["id"] == "R1-F2")
        assert f2["severity"] == "low"

    def test_severity_alias_emits_warning(self):
        result = run_parser(file=FIXTURES / "verdict-schema-drift.txt")
        assert "normalized" in result.stderr.lower()

    def test_files_path_colon_lines_string_form(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-schema-drift.txt"))
        f1 = next(f for f in payload["findings"] if f["id"] == "R1-F1")
        assert f1["files"] == [{"path": "src/example.py", "lines": "42"}]

    def test_files_path_colon_lines_with_range(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-schema-drift.txt"))
        f2 = next(f for f in payload["findings"] if f["id"] == "R1-F2")
        assert f2["files"] == [{"path": "tests/test_example.py", "lines": "10-20"}]

    def test_meta_preserves_unknown_top_level_fields(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-schema-drift.txt"))
        meta = payload["meta"]
        assert meta.get("review_type") == "standard_adversarial"
        assert meta.get("branch") == "feat/example"


# ---------------------------------------------------------------------------
# Multiple blocks → last one wins
# ---------------------------------------------------------------------------

class TestMultipleBlocks:
    def test_last_block_is_used(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-multiple-blocks.txt"))
        ids = [f["id"] for f in payload["findings"]]
        assert ids == ["R1-F-NEW"], f"expected last block (R1-F-NEW), got {ids}"

    def test_stale_block_not_present(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-multiple-blocks.txt"))
        ids = [f["id"] for f in payload["findings"]]
        assert "R1-F-OLD" not in ids


# ---------------------------------------------------------------------------
# Input modes (stdin vs --file)
# ---------------------------------------------------------------------------

class TestInputModes:
    def test_stdin_input(self):
        text = (FIXTURES / "verdict-minimal-nogo.txt").read_text()
        payload = load_payload(run_parser(stdin_text=text))
        assert len(payload["findings"]) == 1
        assert payload["findings"][0]["id"] == "R1-F1"

    def test_file_input(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-minimal-nogo.txt"))
        assert len(payload["findings"]) == 1


# ---------------------------------------------------------------------------
# Real-world smoke tests — verdicts from session 51e16e7b (PR #30 in fdmon)
# ---------------------------------------------------------------------------

class TestRealWorldSmoke:
    def test_r1_has_three_findings_with_summary_drift(self):
        result = run_parser(file=FIXTURES / "verdict-real-51e16e7b-r1.txt")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert len(payload["findings"]) == 3
        ids = [f["id"] for f in payload["findings"]]
        assert ids == ["R1-F1", "R1-F2", "R1-F3"]
        assert "using 'summary' as title" in result.stderr.lower()

    def test_r6_go_has_no_findings(self):
        result = run_parser(file=FIXTURES / "verdict-real-51e16e7b-r6-go.txt")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"] == []
        assert payload["source"] == "empty_findings"
        assert payload["meta"].get("verdict") == "go"

    def test_r1_meta_preserves_review_type_and_branch(self):
        payload = load_payload(run_parser(file=FIXTURES / "verdict-real-51e16e7b-r1.txt"))
        meta = payload["meta"]
        assert meta.get("review_type") == "standard_adversarial"
        assert meta.get("branch") == "feat/snap-grid"
        assert meta.get("base") == "master"


# ---------------------------------------------------------------------------
# Regression tests for findings from session 93392b64 (dogfood on parser itself)
# ---------------------------------------------------------------------------

class TestR1F1IndentedFenceInBlockScalar:
    """R1-F1: indented ``` inside a YAML block scalar must not terminate extraction."""

    def test_indented_fence_inside_excerpt_preserved(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"test indented fence\"\n"
            "      original_verdict_excerpt: |\n"
            "        Problem:\n"
            "        ```python\n"
            "        x = 1\n"
            "        ```\n"
            "      files:\n"
            "        - path: \"src/x.py\"\n"
            "          lines: \"1\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert len(payload["findings"]) == 1
        finding = payload["findings"][0]
        # The critical assertion: fields AFTER the indented fence survive.
        assert finding["files"] == [{"path": "src/x.py", "lines": "1"}]
        # And the block scalar content itself is preserved verbatim.
        assert "```python" in finding["raw"]["original_verdict_excerpt"]
        assert "x = 1" in finding["raw"]["original_verdict_excerpt"]


class TestR1F2FindingsMissingOrNull:
    """R1-F2: missing or null `findings` key must exit 3, not silently produce GO."""

    def test_missing_findings_key_exits_three(self):
        text = "LEDGER_PATCH:\n  verdict: no_go\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr
        assert "'findings' key missing" in result.stderr.lower() or "missing" in result.stderr.lower()

    def test_findings_typo_exits_three(self):
        # `findingz` (typo) used to silently produce empty findings — a NO-GO turned GO.
        text = (
            "LEDGER_PATCH:\n"
            "  findingz:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"this MUST NOT silently disappear\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr

    def test_findings_null_exits_three(self):
        text = "LEDGER_PATCH:\n  findings: null\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr

    def test_findings_empty_list_is_valid_go(self):
        # GO must be expressed as an explicit empty list — this is the GO contract.
        text = "LEDGER_PATCH:\n  findings: []\n"
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"] == []
        assert payload["source"] == "empty_findings"


class TestR1F3PerFindingValidation:
    """R1-F3: required per-finding fields fail with exit 3 instead of being coerced."""

    def test_finding_without_id_exits_three(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - severity: high\n"
            "      title: \"no id field\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr
        assert "id" in result.stderr.lower()

    def test_finding_non_mapping_exits_three(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - \"plain string finding\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr
        assert "not a mapping" in result.stderr.lower() or "mapping" in result.stderr.lower()

    def test_files_scalar_string_coerced_to_one_item_list(self):
        # SCHEMA DRIFT: codex sometimes emits `files: "src/x.py:42"` as a bare string.
        # Parser should coerce it to a one-item list with a warning (not drop it).
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"scalar files\"\n"
            "      files: \"src/x.py:42\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["files"] == [{"path": "src/x.py", "lines": "42"}]
        assert "files" in result.stderr.lower() and "coerced" in result.stderr.lower()

    def test_finding_missing_severity_exits_three(self):
        # R2 escalation of R1-F3: missing required `severity` must not default silently.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      title: \"no severity\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr
        assert "severity" in result.stderr.lower()

    def test_finding_no_title_no_summary_no_problem_exits_three(self):
        # R2 escalation: one of title/summary/problem MUST be present.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      status: open\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, result.stderr
        assert "title" in result.stderr.lower() or "problem" in result.stderr.lower()

    def test_files_single_mapping_coerced_to_one_item_list(self):
        # SCHEMA DRIFT: `files: {path: ..., lines: ...}` (not wrapped in a list).
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"dict files\"\n"
            "      files:\n"
            "        path: \"src/x.py\"\n"
            "        lines: \"99\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["files"] == [{"path": "src/x.py", "lines": "99"}]


class TestR2F1ProblemAsTitleFallback:
    """R2-F1: codex's round-1 prompt asks for `problem:` field, not `title:`.

    SKILL.md Step 'Round 1 — standard' template (lines ~343-350) literally says:
        For every finding output:
        ...
        - Problem
        - Impact
        - Required fix
    Parser must accept this shape.
    """

    def test_problem_field_used_as_title(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      problem: \"this should become the title\"\n"
            "      impact: \"users break\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["title"] == "this should become the title"

    def test_problem_to_title_emits_drift_warning(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      problem: \"problem-only finding\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        assert "problem" in result.stderr.lower() and "drift" in result.stderr.lower()

    def test_title_wins_over_problem_when_both_present(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"explicit title\"\n"
            "      problem: \"detailed problem text\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert payload["findings"][0]["title"] == "explicit title"


class TestMetaDogfoodR1F1ImplicitDateScalars:
    """Regression: unquoted YAML date scalars (`reviewed_at: 2026-05-26`) must not
    crash the parser. PyYAML's safe_load promotes implicit YAML timestamp tags to
    Python `datetime.date`, which `json.dumps` rejects with TypeError.

    Surfaced by meta-dogfood (bulldozer-check-dev reviewing its own parser) —
    the failure mode was exit-1-via-traceback, which Step 4 reserves for "no
    LEDGER_PATCH block found", forcing the caller into the wrong recovery path.
    """

    def test_unquoted_date_in_meta_does_not_crash(self):
        text = (
            "LEDGER_PATCH:\n"
            "  verdict: no_go\n"
            "  reviewed_at: 2026-05-26\n"
            "  findings: []\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"parser crashed on unquoted date in meta. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        # date object → string "2026-05-26" via default=str fallback
        assert payload["meta"]["reviewed_at"] == "2026-05-26"

    def test_unquoted_date_in_finding_raw_does_not_crash(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"date in raw\"\n"
            "      observed_on: 2026-05-26\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"parser crashed on unquoted date in finding. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["raw"]["observed_on"] == "2026-05-26"

    def test_unquoted_date_as_meta_key_does_not_crash(self):
        # Found by second dogfood round (after commit 5ac4fc0 fix only covered
        # values). default=str doesn't fire for dict keys — root-cause fix is
        # _StringTimestampLoader (disables timestamp resolver at YAML layer).
        text = (
            "LEDGER_PATCH:\n"
            "  verdict: go\n"
            "  2026-05-26: reviewed\n"
            "  findings: []\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"parser crashed on unquoted date AS KEY. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert payload["meta"]["2026-05-26"] == "reviewed"

    def test_unquoted_date_as_finding_raw_key_does_not_crash(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"date-key in raw\"\n"
            "      2026-05-26: reviewed\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"parser crashed on unquoted date AS KEY in finding raw. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["raw"]["2026-05-26"] == "reviewed"


class TestR1F4StdinMalformedPath:
    """R1-F4 stdin coverage: stdin-ledger.malformed.yml must be written in cwd."""

    def test_stdin_malformed_writes_stdin_ledger_yml(self, tmp_path: Path):
        # Run parser with stdin in a tmp cwd so the side-effect is sandboxed.
        text = (FIXTURES / "verdict-malformed-yaml.txt").read_text()
        result = run_parser(stdin_text=text, cwd=tmp_path)
        assert result.returncode == 2, result.stderr
        target = tmp_path / "stdin-ledger.malformed.yml"
        assert target.exists(), (
            f"expected stdin malformed-block dump at {target} (per docstring stdin MODE contract)"
        )
        assert "LEDGER_PATCH" in target.read_text()


class TestSchemaLift:
    """Issue #105: original_verdict_excerpt, required_recheck, introduced_round,
    last_seen_round must surface at top level — SKILL.md Review Ledger Format
    schema example lists them as top-level finding fields.
    """

    def test_original_verdict_excerpt_lifted_to_top_level(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"schema lift test\"\n"
            "      original_verdict_excerpt: \"the ACL check runs after the write\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        finding = payload["findings"][0]
        assert finding.get("original_verdict_excerpt") == "the ACL check runs after the write", (
            "schema-promised field 'original_verdict_excerpt' must surface at top level "
            "(SKILL.md Review Ledger Format), not only inside raw.*"
        )

    def test_required_recheck_lifted_to_top_level(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"recheck lift test\"\n"
            "      required_recheck:\n"
            "        instructions: \"Verify ACL check happens before write\"\n"
            "        commands:\n"
            "          - \"grep -n 'check_acl' src/a.py\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        finding = payload["findings"][0]
        recheck = finding.get("required_recheck")
        assert isinstance(recheck, dict), (
            f"required_recheck must surface at top level as dict, got {type(recheck).__name__}"
        )
        assert recheck.get("instructions") == "Verify ACL check happens before write"
        assert recheck.get("commands") == ["grep -n 'check_acl' src/a.py"]

    def test_introduced_and_last_seen_round_lifted(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"round tracking lift\"\n"
            "      introduced_round: 1\n"
            "      last_seen_round: 3\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        finding = payload["findings"][0]
        assert finding.get("introduced_round") == 1
        assert finding.get("last_seen_round") == 3

    def test_raw_preserved_after_lift(self):
        """Forensic-completeness invariant: raw.* still holds the original payload
        after lift — schema-promised fields are DUPLICATED to top level, not moved.
        """
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"raw preservation test\"\n"
            "      original_verdict_excerpt: \"forensic content\"\n"
            "      required_recheck:\n"
            "        instructions: \"do something\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        finding = payload["findings"][0]
        assert finding["raw"].get("original_verdict_excerpt") == "forensic content", (
            "raw.* must retain schema-promised fields after lift (duplicate, not move)"
        )
        assert finding["raw"].get("required_recheck", {}).get("instructions") == "do something"

    def test_original_verdict_excerpt_wrong_type_warns_omits_lift(self):
        # Code-review C2: schema lift was not type-validating. A dict-typed
        # excerpt (instead of string) lifted verbatim, violating SKILL.md
        # contract that the field is a string. Now: warn + skip lift; raw.*
        # still preserves the original payload for forensic inspection.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"wrong excerpt type\"\n"
            "      original_verdict_excerpt:\n"
            "        nested: \"should be string\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        finding = payload["findings"][0]
        # Top-level lift should be skipped (wrong type).
        assert "original_verdict_excerpt" not in finding, (
            f"non-string excerpt must NOT lift; got top-level: {finding.get('original_verdict_excerpt')!r}"
        )
        # raw.* still has the original (forensic completeness).
        assert isinstance(finding["raw"]["original_verdict_excerpt"], dict)
        # Warning surfaces the type mismatch.
        warnings_blob = " ".join(payload.get("warnings", []))
        assert "original_verdict_excerpt" in warnings_blob and "str" in warnings_blob.lower()

    def test_required_recheck_wrong_type_warns_omits_lift(self):
        # required_recheck must be a mapping; a bare string is drift.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"wrong recheck type\"\n"
            "      required_recheck: \"please verify manually\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        finding = payload["findings"][0]
        assert "required_recheck" not in finding
        assert finding["raw"]["required_recheck"] == "please verify manually"
        warnings_blob = " ".join(payload.get("warnings", []))
        assert "required_recheck" in warnings_blob

    def test_introduced_round_wrong_type_warns_omits_lift(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"wrong round type\"\n"
            "      introduced_round: \"r1\"\n"
            "      last_seen_round: \"r2\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        finding = payload["findings"][0]
        assert "introduced_round" not in finding
        assert "last_seen_round" not in finding
        warnings_blob = " ".join(payload.get("warnings", []))
        assert "introduced_round" in warnings_blob
        assert "last_seen_round" in warnings_blob

    def test_correct_types_still_lifted(self):
        # Regression guard: when all four fields have correct types, lift fires.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"correct types\"\n"
            "      original_verdict_excerpt: \"the cited prose\"\n"
            "      required_recheck:\n"
            "        instructions: \"do X\"\n"
            "        commands: [\"cmd1\"]\n"
            "      introduced_round: 1\n"
            "      last_seen_round: 3\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        finding = payload["findings"][0]
        assert finding["original_verdict_excerpt"] == "the cited prose"
        assert isinstance(finding["required_recheck"], dict)
        assert finding["introduced_round"] == 1
        assert finding["last_seen_round"] == 3

    def test_lifted_dict_is_independent_of_raw(self):
        # Code-review E4: lifted dict/list values (required_recheck has both)
        # were shared references with raw.*; downstream mutation of either
        # alias bled into the other, undermining the "raw preserves original"
        # invariant. Now: deep-copy on lift.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "parser", PLUGIN_ROOT / "skills" / "check" / "scripts" / "parse-ledger-patch.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        warnings_list = []
        raw_in = {
            "id": "R1-F1",
            "severity": "high",
            "title": "alias test",
            "required_recheck": {"instructions": "x", "commands": ["one"]},
        }
        finding = mod.normalize_finding(raw_in, warnings_list)
        assert finding is not None
        # Top-level and raw.* must point at different Python objects.
        assert finding["required_recheck"] is not finding["raw"]["required_recheck"], (
            "lifted dict must be a copy, not an alias of raw[key]"
        )
        assert finding["required_recheck"]["commands"] is not finding["raw"]["required_recheck"]["commands"], (
            "deepcopy required — nested list must also be independent"
        )
        # Mutation of top-level must NOT bleed into raw.
        finding["required_recheck"]["commands"].append("INJECTED")
        assert "INJECTED" not in finding["raw"]["required_recheck"]["commands"], (
            f"raw must remain forensic-pristine; got "
            f"{finding['raw']['required_recheck']['commands']}"
        )

    def test_unrelated_raw_field_not_lifted(self):
        """Whitelist invariant: only the 4 documented schema fields lift to top
        level — arbitrary raw fields stay in raw.*.
        """
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"whitelist test\"\n"
            "      random_unknown_field: \"should stay in raw\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        finding = payload["findings"][0]
        assert "random_unknown_field" not in finding, (
            "lift must be whitelisted to schema-promised fields only "
            "(SKILL.md Review Ledger Format) — arbitrary raw keys stay in raw.*"
        )
        assert finding["raw"].get("random_unknown_field") == "should stay in raw"


class TestYamlBoolResolverStripped:
    """Issue #100 case #7: PyYAML's bool resolver inherited from SafeLoader
    promotes 'no/yes/true/false/on/off' to Python booleans. For our domain
    (status/verdict/title — user-facing string fields) this is data corruption.

    Fix: strip the bool resolver alongside the timestamp resolver in
    _StringTimestampLoader so these tokens survive as strings.
    """

    def test_status_no_stays_string_not_false(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"status bool test\"\n"
            "      status: no\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        finding = payload["findings"][0]
        # Without bool-resolver-strip, finding["status"] is Python False → JSON false.
        # With strip, it stays the string "no".
        assert finding["status"] == "no", (
            f"unquoted 'no' in status field must survive as string, "
            f"got {finding['status']!r} ({type(finding['status']).__name__}) — "
            f"PyYAML bool resolver leak from SafeLoader corrupts user-facing data."
        )

    def test_meta_verdict_no_stays_string(self):
        text = (
            "LEDGER_PATCH:\n"
            "  verdict: no\n"
            "  findings: []\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert payload["meta"].get("verdict") == "no", (
            f"unquoted 'no' in meta.verdict must survive as string, "
            f"got {payload['meta'].get('verdict')!r}"
        )

    def test_status_yes_stays_string_not_true(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"status yes test\"\n"
            "      status: yes\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert payload["findings"][0]["status"] == "yes"

    def test_status_off_stays_string(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"status off test\"\n"
            "      status: off\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert payload["findings"][0]["status"] == "off"

    def test_quoted_bool_strings_still_strings(self):
        """Regression guard: explicitly-quoted 'no' was always a string;
        verify the resolver strip doesn't break that path."""
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"quoted bool\"\n"
            "      status: \"no\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert payload["findings"][0]["status"] == "no"


class TestSeverityStrictValidation:
    """Issue #100 cases #1 + #12: unknown/null severity must reject (exit 3),
    not silently default to 'medium'. Symmetric with the existing 'severity
    key missing → exit 3' contract.

    Recognized aliases (critical → blocker, warning → low, …) still pass.
    """

    def test_unknown_severity_exits_three(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: urgent\n"
            "      title: \"unknown severity test\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"unknown severity 'urgent' must exit 3 (strict), "
            f"got {result.returncode}. stderr:\n{result.stderr}"
        )
        assert "severity" in result.stderr.lower() and "urgent" in result.stderr.lower()

    def test_severity_yaml_null_exits_three(self):
        # severity: ~  → explicit YAML null. Asymmetric with missing-key before
        # this fix: missing exits 3, null was warn + 'medium' default.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: ~\n"
            "      title: \"null severity test\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"explicit null severity must exit 3 (symmetric with missing key), "
            f"got {result.returncode}. stderr:\n{result.stderr}"
        )

    def test_severity_non_string_exits_three(self):
        # severity: 5 — integer, not in any valid set
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: 5\n"
            "      title: \"int severity test\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_severity_alias_still_normalized(self):
        # Regression guard: critical → blocker via SEVERITY_ALIASES.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: critical\n"
            "      title: \"alias preserved\"\n"
        )
        payload = load_payload(run_parser(stdin_text=text))
        assert payload["findings"][0]["severity"] == "blocker"


class TestTitleStrictValidation:
    """Issue #100 cases #2 + #3 + #10:
    - whitespace-only title → reject (no useful content for ledger display)
    - non-string title (dict/int/etc.) → reject (schema violation)
    - empty-but-present title + valid summary → fallback works AND warns
      (drift warning previously gated on title-key-MISSING, ignored falsy)
    """

    def test_whitespace_only_title_alone_rejected(self):
        # title is "   " — currently truthy, accepted with empty content.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"   \"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"whitespace-only title with no fallback must exit 3, got {result.returncode}. "
            f"stderr:\n{result.stderr}"
        )

    def test_mapping_valued_title_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title:\n"
            "        nested_key: \"nested value\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3
        assert "title" in result.stderr.lower() and "string" in result.stderr.lower()

    def test_integer_title_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: 42\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_empty_title_falls_through_to_summary_with_warning(self):
        # Case #10: title key present but empty → previously short-circuited the
        # or-chain WITHOUT emitting the schema-drift warning (gated on
        # 'title' NOT in raw). Now drift warning fires regardless.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"\"\n"
            "      summary: \"real description from summary\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["title"] == "real description from summary"
        # Drift warning must fire even though 'title' key WAS present.
        assert "drift" in result.stderr.lower() and "summary" in result.stderr.lower(), (
            f"empty-but-present title must emit drift warning when summary fills in; "
            f"stderr:\n{result.stderr}"
        )

    def test_whitespace_title_falls_through_to_problem_with_warning(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"   \"\n"
            "      problem: \"real problem text\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["title"] == "real problem text"
        assert "drift" in result.stderr.lower() and "problem" in result.stderr.lower()

    def test_normal_string_title_no_warning_no_strip(self):
        # Regression guard: a normal explicit title field does NOT trigger drift warning.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"normal title\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["title"] == "normal title"
        assert "drift" not in result.stderr.lower()

    def test_title_null_falls_through_to_summary(self):
        # Code-review A1 regression: `title: ~` (explicit YAML null) + a valid
        # summary previously exited 3 — the new strict isinstance(value, str)
        # check on the source field aborted before falling through. Old
        # behavior (raw.get('title') or raw.get('summary')) accepted summary.
        # Now: explicit null is treated like missing-key, fallback chain
        # continues. (Non-string non-null values like dict still reject.)
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: ~\n"
            "      summary: \"real description from summary\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, (
            f"explicit null title + valid summary must fall through (not exit 3); "
            f"got {result.returncode}. stderr:\n{result.stderr}"
        )
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["title"] == "real description from summary"
        # Drift warning should fire — summary used instead of title.
        assert "drift" in result.stderr.lower() and "summary" in result.stderr.lower()

    def test_title_null_to_problem_fallback(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: ~\n"
            "      problem: \"the actual problem\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["title"] == "the actual problem"

    def test_title_null_no_fallback_still_rejects(self):
        # Regression guard: when title is null AND no summary/problem present,
        # still exit 3 (no usable content).
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: ~\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_title_dict_still_rejects(self):
        # Regression guard: explicit non-string non-null (dict) still rejects.
        # Null is the only case that falls through (mirrors missing key).
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title:\n"
            "        nested: \"value\"\n"
            "      summary: \"this should NOT save it\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"explicit dict title must reject regardless of fallback; "
            f"got {result.returncode}"
        )


class TestFilesStrictType:
    """Issue #100 case #4: files of unexpected type (int, bool, None) must
    reject (exit 3), not silently produce empty files: [] with only a warning.

    Recognized shapes still coerce:
    - list of dicts                  → canonical
    - list of "path:line" strings    → coerced per entry
    - single "path:line" string      → coerced to one-item list (warning)
    - single mapping {path, lines}   → coerced to one-item list (warning)
    """

    def test_files_integer_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"int files\"\n"
            "      files: 123\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"files: 123 (int) must exit 3, got {result.returncode}. "
            f"stderr:\n{result.stderr}"
        )
        assert "files" in result.stderr.lower()

    def test_files_boolean_rejected(self):
        # Once bool resolver is stripped (#7), `files: yes` is the string 'yes'
        # which falls under string coercion. But `files: !!bool true` (explicit
        # bool tag) is truly a bool — must reject.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"bool files\"\n"
            "      files: !!bool true\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_files_null_explicit_rejected(self):
        # files: ~ — explicit YAML null distinct from missing key.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"null files\"\n"
            "      files: ~\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_files_missing_key_still_ok(self):
        # Regression guard: omitting files entirely yields empty list, exit 0.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"no files key\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["files"] == []

    def test_files_list_with_integer_entry_rejected(self):
        # Dogfood R1-F4: `files: [123, {path: src/x.py, lines: "9"}]` previously
        # silently dropped the int and applied a partial finding. Same evidence-
        # loss path as case #4 (files-as-int top-level), just nested one level.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"int entry inside list\"\n"
            "      files: [123, {path: \"src/x.py\", lines: \"9\"}]\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"non-string/non-mapping entry inside files list must exit 3 "
            f"(no silent skipping of partial data); got {result.returncode}"
        )
        assert "files" in result.stderr.lower()

    def test_files_list_with_bool_entry_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"bool entry\"\n"
            "      files: [!!bool true]\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_files_list_with_null_entry_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"null entry\"\n"
            "      files:\n"
            "        - ~\n"
            "        - {path: \"src/x.py\", lines: \"1\"}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_files_list_of_valid_strings_still_ok(self):
        # Regression guard: list of "path:line" strings still coerces normally.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"two file paths\"\n"
            "      files:\n"
            "        - \"src/a.py:1\"\n"
            "        - \"src/b.py:42-50\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        files = payload["findings"][0]["files"]
        assert files == [
            {"path": "src/a.py", "lines": "1"},
            {"path": "src/b.py", "lines": "42-50"},
        ]

    def test_files_mapping_int_lines_stringified_with_warning(self):
        # Dogfood R2-F1: `lines: 318` (unquoted int — common YAML mistake)
        # previously emitted `"lines": 318` in canonical files, violating the
        # SKILL.md type contract that path/lines are strings.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"int lines\"\n"
            "      files:\n"
            "        - {path: \"src/x.py\", lines: 318}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        file_entry = payload["findings"][0]["files"][0]
        assert file_entry == {"path": "src/x.py", "lines": "318"}, (
            f"int lines must be stringified per SKILL.md type contract; "
            f"got {file_entry}"
        )
        # Caller needs the signal that we stringified.
        assert "stringif" in result.stderr.lower() or "files" in result.stderr.lower()

    def test_files_mapping_non_string_path_stringified(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"int path\"\n"
            "      files:\n"
            "        - {path: 123, lines: \"1\"}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["files"][0]["path"] == "123"

    def test_files_mapping_list_lines_stringified(self):
        # `lines: [4, 5]` — list as YAML literal. Per R2-F1 evidence.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"list lines\"\n"
            "      files:\n"
            "        - {path: \"src/x.py\", lines: [4, 5]}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        # Stringified — exact form (str(list)) acceptable; the contract is
        # "JSON-string-typed", not "human-readable line spec".
        lines = payload["findings"][0]["files"][0]["lines"]
        assert isinstance(lines, str), f"lines must be a string, got {type(lines).__name__}: {lines!r}"

    def test_files_mapping_string_path_lines_no_warning(self):
        # Regression guard: when path/lines are already strings, no extra
        # warning fires — stringification path is opt-in.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"string mapping\"\n"
            "      files:\n"
            "        - {path: \"src/y.py\", lines: \"99\"}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        assert "stringif" not in result.stderr.lower()

    def test_files_mapping_null_lines_becomes_empty_string(self):
        # Code-review A2: `lines: ~` (explicit YAML null) previously
        # produced "lines": "None" (str(None)) in canonical files. Now: empty
        # string, with a quieter warning that distinguishes null from typed
        # non-string values (int / list etc.).
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"null lines\"\n"
            "      files:\n"
            "        - {path: \"src/x.py\", lines: ~}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        file_entry = payload["findings"][0]["files"][0]
        assert file_entry == {"path": "src/x.py", "lines": ""}, (
            f"explicit null lines must become empty string (not 'None' literal); "
            f"got {file_entry}"
        )

    def test_files_mapping_null_path_becomes_empty_string(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"null path\"\n"
            "      files:\n"
            "        - {path: ~, lines: \"42\"}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        file_entry = payload["findings"][0]["files"][0]
        assert file_entry == {"path": "", "lines": "42"}

    def test_files_mapping_null_both_becomes_empty(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"null both\"\n"
            "      files:\n"
            "        - {path: ~, lines: ~}\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["files"][0] == {"path": "", "lines": ""}


class TestDuplicateIdDetection:
    """Issue #100 case #13: two findings with identical id silently passed.
    Downstream ledger keyed by id would overwrite the first with the second,
    violating SKILL.md invariant 'findings never deleted, only status changes'.

    Strict: exit 3 on duplicate id within a single LEDGER_PATCH block.
    """

    def test_duplicate_id_exits_three(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"first finding\"\n"
            "    - id: R1-F1\n"
            "      severity: medium\n"
            "      title: \"second finding overwrites silently today\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"two findings with id R1-F1 must exit 3, got {result.returncode}. "
            f"stderr:\n{result.stderr}"
        )
        assert "duplicate" in result.stderr.lower() and "r1-f1" in result.stderr.lower()

    def test_distinct_ids_pass(self):
        # Regression guard: distinct ids parse normally.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"first\"\n"
            "    - id: R1-F2\n"
            "      severity: low\n"
            "      title: \"second\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        ids = [f["id"] for f in payload["findings"]]
        assert ids == ["R1-F1", "R1-F2"]

    def test_three_findings_third_duplicate_exits_three(self):
        # Duplicate emerges mid-stream — should still trigger.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      title: \"a\"\n"
            "    - id: R1-F2\n"
            "      severity: low\n"
            "      title: \"b\"\n"
            "    - id: R1-F1\n"
            "      severity: medium\n"
            "      title: \"c — dup of first\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3


class TestIdStrictValidation:
    """Dogfood R1-F3: whitespace-only finding ids passed required-id
    validation because `not fid` rejects only falsy strings (None, ''),
    not "   ". Downstream ledger keyed by id would carry blank/whitespace
    keys and duplicate detection could be bypassed with whitespace variants
    of the same semantic id.

    Strict fix: require id to be a non-empty string after strip(), and
    normalize to the stripped form so duplicate detection compares
    semantic identity (" R1-F1" vs "R1-F1 " are dupes).
    """

    def test_whitespace_only_id_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: \"   \"\n"
            "      severity: high\n"
            "      title: \"whitespace id\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"whitespace-only id must exit 3, got {result.returncode}. "
            f"stderr:\n{result.stderr}"
        )

    def test_empty_string_id_rejected(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: \"\"\n"
            "      severity: high\n"
            "      title: \"empty string id\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3

    def test_id_surrounding_whitespace_normalized(self):
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: \"  R1-F1  \"\n"
            "      severity: high\n"
            "      title: \"padded id\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["findings"][0]["id"] == "R1-F1", (
            "id with surrounding whitespace must be normalized (stripped) "
            "to prevent dedup bypass and blank ledger keys."
        )

    def test_whitespace_variant_caught_by_dedup(self):
        # " R1-F1" and "R1-F1 " differ only in whitespace — must be detected
        # as duplicates after normalization.
        text = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: \"  R1-F1\"\n"
            "      severity: high\n"
            "      title: \"first\"\n"
            "    - id: \"R1-F1  \"\n"
            "      severity: medium\n"
            "      title: \"second\"\n"
        )
        result = run_parser(stdin_text=text)
        assert result.returncode == 3, (
            f"whitespace-only variant of an id must trip duplicate detection; "
            f"got {result.returncode}"
        )
