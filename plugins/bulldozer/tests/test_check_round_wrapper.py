"""Tests for skills/check/scripts/bulldozer-round.sh — the composer wrapper.

The wrapper composes codex exec → parse-ledger-patch.py → log-round.sh →
update-state.py into a single invocation so the per-round Claude discipline
gap (Issue #98 / #102) becomes structurally impossible.

Test strategy mirrors test_check_pipeline_integration.py:
- Sandbox all side effects via BULLDOZER_REVIEW_DIR + BULLDOZER_LOG env
- Stub codex by intercepting PATH (real codex is too expensive for unit tests)
- Use subprocess.run with strict timeout for deterministic failure modes
"""
from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

WRAPPER = PLUGIN_ROOT / "skills" / "check" / "scripts" / "bulldozer-round.sh"
FIXTURES = Path(__file__).parent / "fixtures" / "check"

VALID_LEDGER_GO = """\
LEDGER_PATCH:
  verdict: go
  findings: []
"""

VALID_LEDGER_NOGO = """\
LEDGER_PATCH:
  findings:
    - id: R1-F1
      severity: high
      status: open
      title: "test finding"
      files: [{path: "a.py", lines: "1-5"}]
      original_verdict_excerpt: "snippet"
      required_recheck:
        instructions: "verify"
        commands: ["grep foo a.py"]
"""

# Reviewer explicitly says NO-GO with no enumerated findings — legitimate
# when an artifact is too broken to point at specific issues but must not
# ship. Parser preserves this as `meta.verdict: "no_go"` with empty findings.
EXPLICIT_NOGO_EMPTY = """\
LEDGER_PATCH:
  verdict: no_go
  findings: []
"""


def _install_codex_stub(stub_dir: Path, *, exit_code: int = 0,
                        verdict_body: str = "",
                        write_verdict: bool = True) -> Path:
    """Install a fake `codex` executable that writes `verdict_body` to the
    file passed via `-o` and exits with `exit_code`. Returns the directory
    that must go on PATH.

    Mimics the subset of real `codex exec` that the wrapper depends on:
    parses `-o PATH` to know where to drop the verdict, ignores everything
    else, exits with the requested code. Sufficient for crash-detection,
    arg-wiring, and end-to-end happy-path testing without a real LLM call.

    Set `write_verdict=False` to simulate codex exiting 0 without writing
    the `-o` file (real-world rare bug — covered by parser exit-5 branch).
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    # Write verdict body to a sidecar file so bash doesn't have to interpret
    # escape sequences (Python's repr() turns \n into literal '\n' which
    # printf '%s' would emit verbatim, breaking the parser).
    verdict_src = stub_dir / "verdict_body.txt"
    verdict_src.write_text(verdict_body)
    stub = stub_dir / "codex"
    stub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Test stub — extract -o PATH, copy verdict body sidecar there, dump
        # argv if CODEX_STUB_ARGS_FILE is set (so tests can assert wiring).
        if [[ -n "${{CODEX_STUB_ARGS_FILE:-}}" ]]; then
            printf '%s\\n' "$@" > "$CODEX_STUB_ARGS_FILE"
        fi
        verdict_path=""
        args=("$@")
        for ((i=0; i<${{#args[@]}}; i++)); do
            if [[ "${{args[$i]}}" == "-o" ]]; then
                verdict_path="${{args[$((i+1))]}}"
                break
            fi
        done
        if [[ -n "$verdict_path" && {1 if write_verdict else 0} == 1 ]]; then
            mkdir -p "$(dirname "$verdict_path")"
            cp {str(verdict_src)!r} "$verdict_path"
        fi
        exit {exit_code}
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub_dir


def _run_wrapper(tmp_path: Path, stub_dir: Path, *,
                 round_num: int = 1,
                 depth: str = "standard",
                 reviewer: str = "codex/test-model",
                 prompt: str = "review this") -> subprocess.CompletedProcess[str]:
    """Invoke wrapper with sandboxed paths and a stubbed codex on PATH."""
    review_dir = tmp_path / "review"
    review_dir.mkdir(exist_ok=True)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt)

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
    env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")

    return subprocess.run(
        [
            "bash", str(WRAPPER),
            "--round", str(round_num),
            "--review-dir", str(review_dir),
            "--artifact", "test-artifact",
            "--depth", depth,
            "--reviewer", reviewer,
            "--prompt-file", str(prompt_file),
            "--project-root", str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestSkeleton:
    """Skeleton-level guarantees: file present, executable, --help works."""

    def test_script_exists(self):
        assert WRAPPER.is_file(), f"wrapper not found at {WRAPPER}"

    def test_script_is_executable(self):
        assert os.access(WRAPPER, os.X_OK), f"{WRAPPER} is not executable (chmod +x missing)"

    def test_help_prints_usage_and_exits_zero(self):
        result = subprocess.run(
            ["bash", str(WRAPPER), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 0, (
            f"--help should exit 0, got {result.returncode}\nstderr: {result.stderr}"
        )
        combined = result.stdout + result.stderr
        # Usage block must mention the script name and all 7 required flags
        # so consumers (including future Claude instances) can discover the API.
        assert "bulldozer-round.sh" in combined, "usage must name the script"
        for flag in ("--round", "--review-dir", "--artifact", "--depth",
                     "--reviewer", "--prompt-file", "--project-root"):
            assert flag in combined, f"usage must document {flag}"

    def test_missing_required_args_fails_with_nonzero_exit(self):
        result = subprocess.run(
            ["bash", str(WRAPPER)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0, (
            "invocation without args must fail (exit non-zero)"
        )
        # stderr should explain what's missing so the operator can fix it
        assert result.stderr.strip(), "missing-args failure must emit diagnostic on stderr"


class TestCodexInvocation:
    """Composer step 1-2: invoke codex exec FOREGROUND, detect crash."""

    def test_codex_nonzero_exit_propagates_as_wrapper_failure(self, tmp_path: Path):
        """If codex crashes, wrapper must exit non-zero so the caller stops."""
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=1)
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode != 0, (
            f"codex exit 1 must propagate as wrapper failure, got {result.returncode}"
        )

    def test_codex_crash_emits_diagnostic_on_stderr(self, tmp_path: Path):
        """Operator needs to know WHY the round failed without grepping full-rN.txt.

        Stderr only (stdout may legitimately echo other things). Must mention
        both 'codex' AND the exit code so it's not confused with a wrapper bug.
        """
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=7)
        result = _run_wrapper(tmp_path, stub_dir)
        assert "codex" in result.stderr.lower(), (
            f"crash diagnostic missing 'codex'; stderr={result.stderr!r}"
        )
        assert "7" in result.stderr, (
            f"crash diagnostic should include codex exit code (7); stderr={result.stderr!r}"
        )

    def test_codex_success_writes_verdict_file_via_stub(self, tmp_path: Path):
        """On success path, codex (stub) writes verdict-rN.txt via -o flag.

        Proves the wrapper actually invoked codex with -o pointing at the
        review dir — not bypassed it.
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin",
            exit_code=0,
            verdict_body="LEDGER_PATCH:\n  verdict: go\n  findings: []\n",
        )
        _run_wrapper(tmp_path, stub_dir)
        verdict_file = tmp_path / "review" / "verdict-r1.txt"
        assert verdict_file.exists(), (
            f"verdict file not written — codex was not invoked with -o, "
            f"or wrapper used a different path. Looked at: {verdict_file}"
        )

    def test_skip_skills_prefix_appears_exactly_once_for_quick(self, tmp_path: Path):
        """BUG-4: wrapper prepends 'SKIP SKILLS. ' for quick depth, and the
        Round-1 quick template body in SKILL.md also starts with 'SKIP SKILLS.'.
        Combined, codex received 'SKIP SKILLS. SKIP SKILLS. ...' — double prefix.

        The fix: wrapper owns the prefix exclusively; template body must NOT
        start with 'SKIP SKILLS.' (other Round-1 templates already omit it).
        Verify by capturing codex argv: the prompt arg contains exactly one
        'SKIP SKILLS.' substring.
        """
        args_dump = tmp_path / "codex_args.txt"
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["CODEX_STUB_ARGS_FILE"] = str(args_dump)
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        review_dir = tmp_path / "review"; review_dir.mkdir()
        # Use the SKILL.md Round-1 quick template body VERBATIM as caller
        # would. If the template body still leads with "SKIP SKILLS.",
        # combined with wrapper's prepend we'd see two occurrences.
        from conftest import PLUGIN_ROOT
        skill_md = (PLUGIN_ROOT / "skills" / "check" / "SKILL.md").read_text()
        # Extract the Round-1 quick fenced block body
        start = skill_md.find("### Round 1 — quick")
        body_start = skill_md.find("```\n", start) + len("```\n")
        body_end = skill_md.find("```", body_start)
        round1_quick_body = skill_md[body_start:body_end]
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text(round1_quick_body)
        subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "quick",  # the depth that gets the prefix
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        # Multi-line prompt splits across multiple lines in args_dump; count
        # "SKIP SKILLS." across the whole dump (no codex flag contains it).
        argv_dump = args_dump.read_text()
        count = argv_dump.count("SKIP SKILLS.")
        assert count == 1, (
            f"prompt must contain exactly one 'SKIP SKILLS.' prefix, got "
            f"{count}. Either template body still leads with SKIP SKILLS. "
            f"(template owns it AND wrapper prepends → 2) or neither does → 0.\n"
            f"argv_dump:\n{argv_dump}"
        )

    def test_codex_invoked_with_model_from_reviewer(self, tmp_path: Path):
        """--reviewer 'codex/X' must produce '-m X' on the codex command line.

        The composer arg list (#102) only carries `--reviewer codex/MODEL`;
        the wrapper is responsible for extracting MODEL and threading it
        through to codex's `-m` flag.
        """
        args_dump = tmp_path / "codex_args.txt"
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)

        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["CODEX_STUB_ARGS_FILE"] = str(args_dump)
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")

        review_dir = tmp_path / "review"
        review_dir.mkdir()
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("review")

        subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "test",
                "--depth", "standard",
                "--reviewer", "codex/gpt-5.1",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert args_dump.exists(), "stub did not capture argv — wrapper never invoked codex"
        argv_lines = args_dump.read_text().splitlines()
        # Verify -m gpt-5.1 was passed as consecutive args (not just substring).
        for i, arg in enumerate(argv_lines):
            if arg == "-m" and i + 1 < len(argv_lines):
                assert argv_lines[i + 1] == "gpt-5.1", (
                    f"expected '-m gpt-5.1', got '-m {argv_lines[i + 1]}'"
                )
                break
        else:
            pytest.fail(
                f"'-m gpt-5.1' not found in codex argv: {argv_lines}"
            )


class TestParserHappyPath:
    """Composer step 3-4 (exit 0 branch): valid LEDGER_PATCH → parsed-rN.json."""

    def test_parser_invoked_after_codex_success(self, tmp_path: Path):
        """Real parser runs against codex stub's verdict, writes parsed-r1.json."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 0, (
            f"happy path should exit 0, got {result.returncode}\nstderr: {result.stderr}"
        )
        parsed = tmp_path / "review" / "parsed-r1.json"
        assert parsed.exists(), (
            f"parsed-r1.json should be written on exit 0; stderr: {result.stderr}"
        )
        import json
        payload = json.loads(parsed.read_text())
        assert payload["findings"] == [], f"GO verdict → empty findings list, got {payload}"

    def test_parser_invoked_with_nogo_verdict(self, tmp_path: Path):
        """NO-GO with one finding: parsed-rN.json must capture it for log-round."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 0, result.stderr
        import json
        payload = json.loads((tmp_path / "review" / "parsed-r1.json").read_text())
        assert len(payload["findings"]) == 1
        assert payload["findings"][0]["id"] == "R1-F1"


class TestParserExitOne:
    """Composer step 4 exit-1 branch: no LEDGER_PATCH block → manual fallback."""

    def test_prose_verdict_without_block_exits_one(self, tmp_path: Path):
        """Reviewer wrote prose but forgot LEDGER_PATCH — parser exits 1.

        Wrapper must mirror parser exit code (per AC: 'exit codes reflect
        parser exit codes') so the caller knows to fall back to manual
        extraction from the verdict file.
        """
        prose = "The code looks fine. NO-GO because of issue X, but I didn't include the structured block.\n"
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=prose,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 1, (
            f"parser exit 1 (no LEDGER_PATCH) must propagate as wrapper exit 1, "
            f"got {result.returncode}\nstderr: {result.stderr}"
        )

    def test_exit_one_diagnostic_names_manual_fallback(self, tmp_path: Path):
        """Operator/caller needs to know this is fallback-to-manual, not a crash."""
        prose = "Just GO. Sorry, no patch block.\n"  # bare GO with NO-GO absent
        # Note: bare-GO synthesis would actually trigger exit 0 here, so use
        # a verdict that has neither GO nor NO-GO + no block to force exit 1.
        prose = "I reviewed the file. Some issues. See above.\n"
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=prose,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 1, result.stderr
        # Diagnostic must mention manual extraction so the caller doesn't
        # confuse this with a parser crash.
        assert "manual" in result.stderr.lower(), (
            f"exit-1 diagnostic should mention manual extraction; "
            f"stderr={result.stderr!r}"
        )


class TestParserExitTwo:
    """Composer step 4 exit-2 branch: malformed YAML → STOP with context.

    Parser already exits 2 and writes a .malformed.yml sibling on its own;
    the wrapper's job is to add a wrapper-level STOP diagnostic that names
    the round, artifact, and the operator's recovery path so the caller
    doesn't have to grep parser stderr for context.
    """

    def test_exit_two_diagnostic_says_stop(self, tmp_path: Path):
        """Diagnostic must include 'STOP' so caller distinguishes from warnings."""
        malformed = (FIXTURES / "verdict-malformed-yaml.txt").read_text()
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=malformed,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 2, result.stderr
        assert "STOP" in result.stderr, (
            f"exit-2 wrapper diagnostic must include 'STOP' (not just rely on "
            f"parser's ERROR line); stderr={result.stderr!r}"
        )

    def test_exit_two_diagnostic_names_round_and_artifact(self, tmp_path: Path):
        """Round and artifact in diagnostic so caller knows which review died."""
        malformed = (FIXTURES / "verdict-malformed-yaml.txt").read_text()
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=malformed,
        )
        result = _run_wrapper(tmp_path, stub_dir, round_num=3)
        # Wrapper context: round number + artifact label, not just parser output.
        assert "round=3" in result.stderr or "round 3" in result.stderr, (
            f"diagnostic should name the round; stderr={result.stderr!r}"
        )
        assert "test-artifact" in result.stderr, (
            f"diagnostic should name the artifact; stderr={result.stderr!r}"
        )

    def test_exit_two_diagnostic_points_to_malformed_yml_sibling(self, tmp_path: Path):
        """Caller needs the .malformed.yml path so the operator can inspect."""
        malformed = (FIXTURES / "verdict-malformed-yaml.txt").read_text()
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=malformed,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert "verdict-r1.malformed.yml" in result.stderr, (
            f"diagnostic should point to the .malformed.yml sibling for "
            f"operator inspection; stderr={result.stderr!r}"
        )


# Valid YAML that's structurally wrong (no findings field) — triggers exit 3.
SCHEMA_VIOLATION_VERDICT = """\
LEDGER_PATCH:
  verdict: no_go
"""


class TestParserExitThree:
    """Composer step 4 exit-3 branch: schema violation → STOP, do NOT apply."""

    def test_schema_violation_exits_three(self, tmp_path: Path):
        """Missing `findings` field is a schema violation — parser exit 3."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=SCHEMA_VIOLATION_VERDICT,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 3, (
            f"schema violation → wrapper exit 3, got {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_exit_three_diagnostic_says_stop_and_schema(self, tmp_path: Path):
        """Distinct from exit 2 (malformed YAML) — diagnostic must say schema."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=SCHEMA_VIOLATION_VERDICT,
        )
        result = _run_wrapper(tmp_path, stub_dir, round_num=2)
        assert "STOP" in result.stderr, (
            f"exit-3 diagnostic must include 'STOP'; stderr={result.stderr!r}"
        )
        assert "schema" in result.stderr.lower(), (
            f"exit-3 diagnostic must say 'schema' (not 'malformed'); "
            f"stderr={result.stderr!r}"
        )
        # Round/artifact context like exit-2 for caller routing.
        assert "round=2" in result.stderr or "round 2" in result.stderr, (
            f"diagnostic should name round; stderr={result.stderr!r}"
        )

    def test_exit_three_warns_against_applying_patch(self, tmp_path: Path):
        """Schema-violating patch must NOT be applied (Issue #102 AC)."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=SCHEMA_VIOLATION_VERDICT,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        # Tell the caller explicitly: do not apply this patch to the ledger.
        # 'apply' or 'do not apply' wording — case-insensitive substring is fine.
        msg = result.stderr.lower()
        assert "do not apply" in msg or "must not apply" in msg, (
            f"exit-3 diagnostic must warn against applying the patch; "
            f"stderr={result.stderr!r}"
        )


class TestParserExitFive:
    """Composer step 4 exit-5 branch: file/stdin IO failure (Track 2 #108)."""

    def test_missing_verdict_file_exits_five(self, tmp_path: Path):
        """Codex exits 0 but didn't write the -o file → parser exit 5."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, write_verdict=False,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 5, (
            f"missing verdict file → parser exit 5 → wrapper exit 5, got "
            f"{result.returncode}\nstderr: {result.stderr}"
        )

    def test_exit_five_diagnostic_distinguishes_io_failure(self, tmp_path: Path):
        """IO failure is operationally different from schema/malformed — say so."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, write_verdict=False,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        # Wrapper diagnostic should let the caller distinguish this from
        # exit-2/exit-3 (which are reviewer-side bugs); exit-5 is "verdict
        # file unreadable/missing", usually transient or pipe-related.
        assert "STOP" in result.stderr, result.stderr
        # Mention the missing/IO failure mode explicitly
        msg = result.stderr.lower()
        assert ("verdict" in msg and ("missing" in msg or "unreadable" in msg
                                       or "io" in msg or "not written" in msg)), (
            f"exit-5 diagnostic should explain the IO failure mode; "
            f"stderr={result.stderr!r}"
        )

    def test_exit_five_suggests_retry(self, tmp_path: Path):
        """Exit 5 is often transient — diagnostic should suggest retrying.

        Filter to lines mentioning STOP or starting with whitespace
        (wrapper's STOP-block continuation) so this assertion doesn't
        accidentally match parser's stderr or pytest's tmp_path leak.
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, write_verdict=False,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        wrapper_lines = [
            line for line in result.stderr.splitlines()
            if "STOP" in line or line.startswith("      ")
        ]
        wrapper_block = " ".join(wrapper_lines).lower()
        assert ("retry" in wrapper_block or "re-run" in wrapper_block
                or "try again" in wrapper_block), (
            f"exit-5 wrapper diagnostic should suggest retrying the round; "
            f"wrapper_block={wrapper_block!r}"
        )


def _install_python3_stub(stub_dir: Path, *, exit_code: int) -> Path:
    """Install a fake `python3` that exits with the requested code.

    Used to simulate parser exit 4 (PyYAML missing) without touching the
    real Python environment. The wrapper calls `python3 PARSER ...` so a
    PATH-resident fake python3 short-circuits the whole invocation.
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "ERROR: PyYAML is not installed. Run: pip install pyyaml" >&2
        exit {exit_code}
    """))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return stub_dir


def _seed_state_and_run(tmp_path: Path, history_seed: list, *,
                         round_num: int, depth: str = "standard",
                         verdict_body: str = VALID_LEDGER_NOGO):
    """Pre-seed state.json with prior rounds, then run wrapper for round_num.

    Trajectory display reads state.json history (managed by log-round /
    update-state), so multi-round scenarios are simulated by seeding the
    file before invocation. The wrapper appends the current round on top.
    """
    import json
    review_dir = tmp_path / "review"
    review_dir.mkdir(exist_ok=True)
    seed_state = {
        "round": history_seed[-1]["round"] if history_seed else 0,
        "artifact": "test-artifact",
        "depth": depth,
        "started_at": "2026-05-27T00:00:00+00:00",
        "reviewer": "codex/test-model",
        "findings_total": sum(h["findings"] for h in history_seed),
        "fixed_total": sum(h.get("fixed", 0) for h in history_seed),
        "false_positives": sum(h.get("fp", 0) for h in history_seed),
        "history": history_seed,
    }
    (review_dir / "state.json").write_text(json.dumps(seed_state, indent=2))
    stub_dir = _install_codex_stub(
        tmp_path / "bin", exit_code=0, verdict_body=verdict_body,
    )
    return _run_wrapper(tmp_path, stub_dir, round_num=round_num, depth=depth)


class TestParserExitFour:
    """Composer step 4 exit-4 branch: PyYAML missing → user-actionable error."""

    def test_pyyaml_missing_exits_four(self, tmp_path: Path):
        """Fake python3 (returning 4) simulates the PyYAML-not-installed case."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        _install_python3_stub(stub_dir, exit_code=4)
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 4, (
            f"PyYAML missing → wrapper exit 4, got {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_exit_four_wrapper_emits_stop_block_with_remediation(self, tmp_path: Path):
        """Wrapper-controlled STOP block must surface remediation prominently.

        Like exit-2/3/5, wrapper adds a STOP block with round/artifact
        context plus the pip-install instruction so the operator doesn't
        have to scan parser stderr to find what to do.
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        _install_python3_stub(stub_dir, exit_code=4)
        result = _run_wrapper(tmp_path, stub_dir, round_num=2)
        # Inspect only wrapper-generated lines (STOP marker + indented
        # continuation), not parser/stub stderr that leaks through.
        wrapper_lines = [
            line for line in result.stderr.splitlines()
            if "STOP" in line or line.startswith("      ")
        ]
        wrapper_block = " ".join(wrapper_lines).lower()
        assert "stop" in wrapper_block, (
            f"exit-4 needs wrapper STOP block; stderr={result.stderr!r}"
        )
        assert "pyyaml" in wrapper_block, (
            f"wrapper STOP block must name PyYAML; "
            f"wrapper_block={wrapper_block!r}"
        )
        assert "pip install" in wrapper_block, (
            f"wrapper STOP block must include pip install remediation; "
            f"wrapper_block={wrapper_block!r}"
        )


class TestLogRoundComposition:
    """Composer steps 5-7: parse counts → log-round.sh → emit state.json.

    The wrapper's end-to-end job: forgetting to call log-round.sh after
    a successful parser run is the discipline failure #102 exists to
    eliminate. These tests assert the composition happens automatically.
    """

    def test_state_json_created_on_happy_path(self, tmp_path: Path):
        """After codex success + parser exit 0, state.json must exist."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 0, result.stderr
        state_file = tmp_path / "review" / "state.json"
        assert state_file.exists(), (
            f"state.json must be created by log-round.sh after happy path; "
            f"stderr={result.stderr}"
        )

    def test_state_json_records_round_and_findings_count(self, tmp_path: Path):
        """state.json must reflect the parsed findings count."""
        import json
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        _run_wrapper(tmp_path, stub_dir, round_num=2)
        state = json.loads((tmp_path / "review" / "state.json").read_text())
        assert state["round"] == 2, f"round should be 2, got {state['round']}"
        # VALID_LEDGER_NOGO has exactly 1 finding
        assert state["findings_total"] == 1, (
            f"findings_total should reflect parsed.findings length (1); "
            f"got {state['findings_total']}"
        )

    def test_verdict_go_when_findings_empty(self, tmp_path: Path):
        """Empty findings → verdict=GO recorded in state.json history."""
        import json
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        _run_wrapper(tmp_path, stub_dir)
        state = json.loads((tmp_path / "review" / "state.json").read_text())
        assert state["history"][-1]["verdict"].upper() == "GO", (
            f"verdict for empty findings should be GO, got {state['history']}"
        )

    def test_verdict_nogo_when_findings_present(self, tmp_path: Path):
        """At least one finding → verdict=NO-GO recorded."""
        import json
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        _run_wrapper(tmp_path, stub_dir)
        state = json.loads((tmp_path / "review" / "state.json").read_text())
        assert state["history"][-1]["verdict"].upper() == "NO-GO", (
            f"verdict with findings should be NO-GO, got {state['history']}"
        )

    def test_log_round_appended_to_bulldozer_log(self, tmp_path: Path):
        """log-round.sh appends a line to BULLDOZER_LOG with round metadata."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        _run_wrapper(tmp_path, stub_dir, round_num=3)
        log_file = tmp_path / "bulldozer.log"
        assert log_file.exists(), "BULLDOZER_LOG should be appended"
        log_line = log_file.read_text().strip()
        assert "round=3" in log_line
        assert "findings=1" in log_line
        assert "verdict=NO-GO" in log_line
        assert "reviewer=codex/test-model" in log_line

    def test_log_line_records_explicit_project_root(self, tmp_path: Path):
        """BUG-3: wrapper must pass --project-root through to log-round.sh as
        the 8th positional arg. Without it, log-round.sh falls back to
        `git rev-parse --show-toplevel || pwd` from the caller's CWD —
        recording the wrong path when Claude's CWD differs from PROJECT_ROOT.
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        _run_wrapper(tmp_path, stub_dir)
        log_line = (tmp_path / "bulldozer.log").read_text().strip()
        # _run_wrapper passes --project-root str(tmp_path); the log line's
        # `project=...` column must match (not fall back to pytest's CWD).
        assert f"project={tmp_path}" in log_line, (
            f"log line should record explicit project-root from wrapper; "
            f"got: {log_line!r}"
        )

    def test_stdout_emits_state_json(self, tmp_path: Path):
        """Caller receives the final state.json contents on stdout (composer
        step 7). Lets the caller derive trajectory without re-reading the
        file from disk.
        """
        import json
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 0, result.stderr
        # stdout must parse as JSON and look like state.json shape.
        payload = json.loads(result.stdout)
        assert payload["round"] == 1
        assert payload["findings_total"] == 1
        assert isinstance(payload["history"], list)

    def test_fixed_and_fp_from_env_vars(self, tmp_path: Path):
        """Caller passes per-round fixed/fp counts via env vars so the wrapper
        can record post-fix accounting without per-round CLI args.
        """
        import json
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        # Drop a stub directly via lower-level call to override env
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["BULLDOZER_FIXED"] = "1"
        env["BULLDOZER_FP"] = "0"
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("review")

        subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "test",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        state = json.loads((review_dir / "state.json").read_text())
        assert state["fixed_total"] == 1, (
            f"BULLDOZER_FIXED=1 should appear in state.json fixed_total; "
            f"got {state}"
        )
        assert state["false_positives"] == 0


class TestTrajectoryDisplay:
    """Composer step 8 (U7): trajectory printed after rounds >= 2.

    Format per Issue #102 design:
        [bulldozer/check] Round 3/3 — verdict: NO-GO — 1 finding open
        Trajectory: 4 → 3 → 1  (avg last 3: 2.7)

    Goes to stderr (informational), not stdout (which carries state.json).
    """

    def test_no_trajectory_on_round_one(self, tmp_path: Path):
        """Round 1 has nothing to plot — no trajectory line."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        result = _run_wrapper(tmp_path, stub_dir, round_num=1)
        assert result.returncode == 0, result.stderr
        assert "Trajectory:" not in result.stderr, (
            f"trajectory must not print on round 1; stderr={result.stderr!r}"
        )

    def test_trajectory_printed_round_two(self, tmp_path: Path):
        """Round 2 onwards: trajectory line goes to stderr."""
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        result = _seed_state_and_run(tmp_path, seed, round_num=2)
        assert result.returncode == 0, result.stderr
        assert "Trajectory:" in result.stderr, (
            f"trajectory missing on round 2; stderr={result.stderr!r}"
        )

    def test_trajectory_format_matches_spec(self, tmp_path: Path):
        """Trajectory line: 'Trajectory: A → B → C  (avg last 3: X.X)'.

        Uses exhaustive depth (max=10) so round 3 is well below the pivot
        threshold — keeps this test focused on trajectory format only.
        """
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 3, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        result = _seed_state_and_run(tmp_path, seed, round_num=3, depth="exhaustive")
        assert result.returncode == 0, result.stderr
        # VALID_LEDGER_NOGO adds 1 finding on round 3, so trajectory = 4 → 3 → 1
        # avg of [4, 3, 1] = 2.67 ≈ 2.7
        assert "4 → 3 → 1" in result.stderr, (
            f"trajectory format wrong; stderr={result.stderr!r}"
        )
        assert "avg last 3: 2.7" in result.stderr, (
            f"avg-last-3 wrong (expected 2.7); stderr={result.stderr!r}"
        )

    def test_round_header_shows_max_for_depth(self, tmp_path: Path):
        """'Round N/M' — M is depth-specific max (standard=3, exhaustive=10)."""
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        # standard depth → max 3
        result = _seed_state_and_run(tmp_path, seed, round_num=2, depth="standard")
        assert "Round 2/3" in result.stderr, (
            f"standard depth should show Round 2/3; stderr={result.stderr!r}"
        )

    def test_round_header_exhaustive_shows_max_10(self, tmp_path: Path):
        """Exhaustive depth: max rounds = 10."""
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        result = _seed_state_and_run(tmp_path, seed, round_num=2, depth="exhaustive")
        assert "Round 2/10" in result.stderr, (
            f"exhaustive depth should show Round 2/10; stderr={result.stderr!r}"
        )

    def test_trajectory_pluralizes_findings(self, tmp_path: Path):
        """'1 finding open' vs '3 findings open' — singular vs plural."""
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        # Round 2 produces 1 finding (VALID_LEDGER_NOGO) → singular "finding"
        result = _seed_state_and_run(tmp_path, seed, round_num=2)
        assert "1 finding open" in result.stderr, (
            f"singular 'finding'; stderr={result.stderr!r}"
        )


class TestAskUserPivot:
    """Composer step 9 (U5a): structured pivot signal at max rounds w/o GO.

    Trigger: round == max_rounds && verdict != GO. Effects:
      - exit code 10 (distinct from parser codes 0-5)
      - pivot JSON written to ${REVIEW_DIR}/pivot-rN.json
      - stderr diagnostic naming the pivot file

    Caller (Claude SKILL.md) reads exit 10 + pivot file and wraps in
    AskUserQuestion: continue / restructure / accept-with-TODO.
    """

    def test_no_pivot_when_round_below_max(self, tmp_path: Path):
        """Round 2/3 with NO-GO is not a pivot moment — exit 0, no pivot file."""
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        result = _seed_state_and_run(tmp_path, seed, round_num=2, depth="standard")
        assert result.returncode == 0, (
            f"round 2/3 NO-GO should be normal exit 0, got {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        pivot_file = tmp_path / "review" / "pivot-r2.json"
        assert not pivot_file.exists(), (
            "pivot file must not exist when round < max"
        )

    def test_no_pivot_when_verdict_go_at_max_round(self, tmp_path: Path):
        """Round 3/3 with GO is a normal success — no pivot."""
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 2, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        result = _seed_state_and_run(
            tmp_path, seed, round_num=3, depth="standard",
            verdict_body=VALID_LEDGER_GO,  # final round goes GO
        )
        assert result.returncode == 0, (
            f"GO at max round = success, got exit {result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert not (tmp_path / "review" / "pivot-r3.json").exists()

    def test_pivot_at_max_round_no_go_exits_ten(self, tmp_path: Path):
        """Round 3/3 with NO-GO → wrapper exits 10 (pivot signal)."""
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 2, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        result = _seed_state_and_run(
            tmp_path, seed, round_num=3, depth="standard",
            verdict_body=VALID_LEDGER_NOGO,  # final round still NO-GO
        )
        assert result.returncode == 10, (
            f"max round + NO-GO → exit 10 (pivot signal), got {result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_pivot_writes_structured_json_file(self, tmp_path: Path):
        """pivot-rN.json must contain trigger, round, max_rounds, options."""
        import json
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 2, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        _seed_state_and_run(
            tmp_path, seed, round_num=3, depth="standard",
            verdict_body=VALID_LEDGER_NOGO,
        )
        pivot_file = tmp_path / "review" / "pivot-r3.json"
        assert pivot_file.exists(), "pivot file must be written on exit 10"
        pivot = json.loads(pivot_file.read_text())
        assert pivot.get("trigger") == "max_rounds_reached", (
            f"trigger must be 'max_rounds_reached'; got {pivot}"
        )
        assert pivot.get("round") == 3
        assert pivot.get("max_rounds") == 3
        assert pivot.get("open_findings") == 1  # VALID_LEDGER_NOGO has 1
        # Options for AskUserQuestion: continue / restructure / accept-with-TODO
        options = pivot.get("options", [])
        labels = [opt.get("label") for opt in options]
        assert "continue" in labels, f"options missing 'continue': {labels}"
        assert "restructure" in labels, f"options missing 'restructure': {labels}"
        assert any("accept" in lbl for lbl in labels if lbl), (
            f"options missing accept-with-TODO variant: {labels}"
        )

    def test_pivot_json_maps_directly_to_askuserquestion(self, tmp_path: Path):
        """BUG-6: pivot JSON must contain all AskUserQuestion required fields
        directly so SKILL.md's 'wrap options in AskUserQuestion' instruction
        works literally — no caller-side field synthesis required.

        AskUserQuestion per-question schema: question (str), header (str
        max 12 chars), multiSelect (bool), options (list of {label,
        description}). Previous pivot JSON used 'suggested_question' and
        omitted header + multiSelect, forcing the caller to translate.
        """
        import json
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        _seed_state_and_run(
            tmp_path, seed, round_num=2, depth="quick",
            verdict_body=VALID_LEDGER_NOGO,
        )
        pivot = json.loads((tmp_path / "review" / "pivot-r2.json").read_text())
        # `question` (not `suggested_question`) — matches AskUserQuestion key
        assert "question" in pivot, (
            f"pivot must use 'question' (AskUserQuestion key), not "
            f"'suggested_question'. got keys: {list(pivot)}"
        )
        assert isinstance(pivot["question"], str) and pivot["question"]
        # `header` — chip label, max 12 chars
        assert "header" in pivot, f"pivot missing 'header'; keys: {list(pivot)}"
        assert isinstance(pivot["header"], str)
        assert len(pivot["header"]) <= 12, (
            f"AskUserQuestion header must be ≤12 chars; got "
            f"{len(pivot['header'])}: {pivot['header']!r}"
        )
        # `multiSelect` — defaults to false for pivot (single-choice)
        assert pivot.get("multiSelect") is False, (
            f"pivot multiSelect must be explicit False; got {pivot.get('multiSelect')!r}"
        )

    def test_pivot_diagnostic_names_pivot_file_on_stderr(self, tmp_path: Path):
        """Caller should see a PIVOT marker on stderr naming the pivot file."""
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 2, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        result = _seed_state_and_run(
            tmp_path, seed, round_num=3, depth="standard",
            verdict_body=VALID_LEDGER_NOGO,
        )
        assert "PIVOT" in result.stderr, (
            f"stderr should mention PIVOT marker; stderr={result.stderr!r}"
        )
        assert "pivot-r3.json" in result.stderr, (
            f"stderr should name pivot file; stderr={result.stderr!r}"
        )

    def test_pivot_state_json_still_emitted_on_stdout(self, tmp_path: Path):
        """Caller still needs state.json on stdout even when pivoting."""
        import json
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 2, "fixed": 0, "fp": 0,
             "timestamp": "2026-05-27T00:01:00+00:00"},
        ]
        result = _seed_state_and_run(
            tmp_path, seed, round_num=3, depth="standard",
            verdict_body=VALID_LEDGER_NOGO,
        )
        # Even with exit 10, stdout must parse as state.json so the caller
        # has all the data it needs in one place.
        state = json.loads(result.stdout)
        assert state["round"] == 3

    def test_pivot_fires_quick_depth_round_one_no_go(self, tmp_path: Path):
        """Quick depth has max_rounds=1, so round 1 IS the terminal round.

        Regression for BUG-1 (max_rounds scope): max_rounds was assigned only
        inside the `if (( ROUND >= 2 ))` trajectory block, so it stayed unset
        on round 1. The pivot guard then silently skipped. Quick-NO-GO never
        produced an exit-10 signal even though it's exactly the case where
        the caller most needs to know "no more rounds available".
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        result = _run_wrapper(tmp_path, stub_dir, round_num=1, depth="quick")
        assert result.returncode == 10, (
            f"quick depth max=1 + round 1 + NO-GO must pivot, got exit "
            f"{result.returncode}\nstderr: {result.stderr}"
        )
        pivot_file = tmp_path / "review" / "pivot-r1.json"
        assert pivot_file.exists(), (
            "pivot-r1.json must be written for quick depth terminal NO-GO"
        )


class TestVerdictRespectsMetaVerdict:
    """Composer must honor parser's `meta.verdict` field, not just findings count.

    Regression for BUG-2 (/code-review): when reviewer emits
    `LEDGER_PATCH: { verdict: no_go, findings: [] }`, the parser preserves
    `meta.verdict: "no_go"` in parsed-rN.json. Wrapper was deriving VERDICT
    purely from `len(findings) == 0 → GO`, silently flipping a legitimate
    explicit NO-GO into GO. Caller would then treat it as success and ship.
    """

    def test_explicit_no_go_empty_findings_records_no_go(self, tmp_path: Path):
        import json
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=EXPLICIT_NOGO_EMPTY,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        # Wrapper itself exits 0 (this is round 1 standard, not max), but the
        # recorded verdict must match the reviewer's explicit signal.
        assert result.returncode == 0, result.stderr
        state = json.loads(result.stdout)
        assert state["history"][-1]["verdict"].upper() == "NO-GO", (
            f"explicit verdict: no_go must be preserved, not flipped to GO "
            f"by structural findings_count==0 inference; got {state}"
        )

    def test_explicit_no_go_at_max_round_fires_pivot(self, tmp_path: Path):
        """Combine BUG-1 + BUG-2: explicit NO-GO + empty findings at max round
        must STILL fire pivot (it's NO-GO, not GO), proving meta.verdict
        flows into the pivot guard correctly.
        """
        seed = [{"round": 1, "verdict": "NO-GO", "findings": 4, "fixed": 0,
                 "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"}]
        result = _seed_state_and_run(
            tmp_path, seed, round_num=2, depth="quick",
            verdict_body=EXPLICIT_NOGO_EMPTY,
        )
        # depth=quick → max_rounds=1, ROUND=2 > max — pivot fires.
        assert result.returncode == 10, (
            f"explicit NO-GO at max round must pivot, got exit "
            f"{result.returncode}\nstderr: {result.stderr}"
        )


class TestExitCodeContract:
    """Exit codes 1-5 + 10 are RESERVED for parser/pivot outcomes.

    Dogfood R1-F1/F2/F3 (see issue #110 follow-up): wrapper paths that
    bubble codex/preflight/log-round failures into reserved codes mislead
    the caller into wrong recovery branches. New scheme:

      64 = wrapper preflight / usage error  (sysexits.h EX_USAGE)
      70 = wrapper-internal failure         (sysexits.h EX_SOFTWARE)
      71 = codex crash                      (sysexits.h EX_OSERR-ish)

    1-5 stay parser-only. 10 stays pivot-only.
    """

    def test_codex_exit_one_maps_to_71_not_collision_with_parser(self, tmp_path: Path):
        """codex exit 1 (auth expired / generic error) must NOT pass through raw.

        Before fix: wrapper exits 1 → caller thinks 'parser found no LEDGER_PATCH'
        and tries to extract findings from a garbage/empty verdict file.
        After fix: wrapper exits 71 → caller routes to 'codex crash' branch.
        """
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=1)
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 71, (
            f"codex exit 1 must map to 71, got {result.returncode}.\n"
            f"Collision with parser exit 1 (no LEDGER_PATCH) misleads caller.\n"
            f"stderr: {result.stderr}"
        )

    def test_codex_exit_ten_maps_to_71_not_collision_with_pivot(self, tmp_path: Path):
        """codex exit 10 must NOT collide with wrapper exit 10 (pivot signal).

        Before fix: caller searches for pivot-rN.json that was never written.
        After fix: wrapper exits 71 → caller knows codex crashed.
        """
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=10)
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 71, (
            f"codex exit 10 must map to 71, got {result.returncode}.\n"
            f"Collision with pivot exit 10 would make caller look for "
            f"non-existent pivot-rN.json.\nstderr: {result.stderr}"
        )

    def test_codex_crash_stderr_preserves_original_exit_code(self, tmp_path: Path):
        """Mapping codex N → wrapper 71 must NOT lose the original code.

        Operator needs to know which codex exit code occurred (1 vs 7 vs 10
        all have different remediations). Already implicitly tested by
        test_codex_crash_emits_diagnostic_on_stderr, but pinned here so
        the contract test class is self-contained.
        """
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=7)
        result = _run_wrapper(tmp_path, stub_dir)
        assert "7" in result.stderr, (
            f"original codex exit code missing from diagnostic.\n"
            f"stderr: {result.stderr}"
        )

    def test_empty_verdict_after_codex_success_exits_5_not_1(self, tmp_path: Path):
        """codex exit 0 + empty verdict file → wrapper exit 5 (IO failure).

        Before fix: parser sees empty file, returns 1 ("no LEDGER_PATCH
        block"), wrapper exits 1 → caller routes to manual prose extraction
        (the discipline failure #102 was meant to eliminate). SKILL.md error
        table documents empty verdict as the rerun-same-round path.

        After fix: wrapper guards with `[[ -s VERDICT_FILE ]]` after codex
        exit 0; empty/missing → exit 5 (parser IO failure semantics,
        operator retries the round).
        """
        # verdict_body="" makes the stub write an empty file (write_verdict
        # still True → file exists, just zero bytes).
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body="",
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 5, (
            f"empty verdict after codex success must exit 5 (IO), got "
            f"{result.returncode}. Exit 1 would mislead caller into manual "
            f"prose extraction.\nstderr: {result.stderr}"
        )

    def test_missing_verdict_after_codex_success_exits_5(self, tmp_path: Path):
        """codex exit 0 + no verdict file written → wrapper exit 5.

        Real-world rare bug: codex exits 0 but `-o` write silently failed
        (fs full, EACCES). Parser already covers this (exit 5 from file
        IO), but pin it here to lock the contract end-to-end after R1-F2.
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, write_verdict=False,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 5, (
            f"missing verdict after codex success must exit 5, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    # ----- R1-F3a: wrapper preflight exits 64 (sysexits.h EX_USAGE) -----
    # Before fix: all 5 preflight paths exited 2 — same code as parser
    # "malformed LEDGER_PATCH YAML". Caller searching for .malformed.yml
    # found nothing. After fix: preflight maps to 64; parser keeps 2.

    def _run_wrapper_raw(self, tmp_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
        """Invoke wrapper without the standard 7-flag setup — for preflight tests
        that intentionally provide bad/missing args.
        """
        env = os.environ.copy()
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        return subprocess.run(
            ["bash", str(WRAPPER), *extra_args],
            env=env, capture_output=True, text=True, timeout=5,
        )

    def test_unknown_flag_exits_64(self, tmp_path: Path):
        """Unknown CLI flag is a usage error, not a parser failure."""
        result = self._run_wrapper_raw(tmp_path, "--bogus-flag")
        assert result.returncode == 64, (
            f"unknown flag must exit 64 (EX_USAGE), got {result.returncode}. "
            f"Exit 2 collides with parser malformed-YAML.\nstderr: {result.stderr}"
        )

    def test_missing_required_flag_exits_64(self, tmp_path: Path):
        """Missing one of the 7 required flags is a usage error."""
        # Provide only some flags — missing --review-dir, --artifact, etc.
        result = self._run_wrapper_raw(tmp_path, "--round", "1")
        assert result.returncode == 64, (
            f"missing required flag must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_flag_at_end_of_argv_without_value_exits_64(self, tmp_path: Path):
        """Hotfix R2-F1: `--round` (no value) currently exits 1 under set -u.

        Before fix: arg-parse case statement reads `$2` directly. When the
        flag appears at end of argv without a value, `set -u` aborts with
        'unbound variable' and bash exits 1 — collides with parser-no-LEDGER.
        After fix: each value-taking flag checks `$# >= 2` before consuming;
        missing value exits 64 with a clear "--flag requires a value" message.

        Reported by Copilot inline review on PR #109 and by codex dogfood
        round 2 on the hotfix branch.
        """
        result = self._run_wrapper_raw(tmp_path, "--round")
        assert result.returncode == 64, (
            f"--round without value must exit 64, got {result.returncode}.\n"
            f"Exit 1 (set -u 'unbound variable') collides with parser-no-LEDGER.\n"
            f"stderr: {result.stderr}"
        )
        assert "--round" in result.stderr, (
            f"diagnostic should name the offending flag.\nstderr: {result.stderr}"
        )

    def test_other_value_taking_flags_at_end_of_argv_exit_64(self, tmp_path: Path):
        """Same bug, multiple flags — pin every value-taking flag.

        If only --round is guarded but --review-dir / --artifact / --depth /
        --reviewer / --prompt-file / --project-root keep raw `$2`, the bug
        partially survives.
        """
        for flag in ("--review-dir", "--artifact", "--depth", "--reviewer",
                     "--prompt-file", "--project-root"):
            result = self._run_wrapper_raw(tmp_path, flag)
            assert result.returncode == 64, (
                f"{flag} without value must exit 64, got {result.returncode}.\n"
                f"stderr: {result.stderr}"
            )

    # ----- R2-F2 (hotfix round 2): parser-adjacent leaks → 70 -----

    def _make_fake_plugin_root(self, tmp_path: Path, parser_script: str,
                               log_round_script: str | None = None) -> Path:
        """Build a fake CLAUDE_PLUGIN_ROOT with a custom parser stub.

        The real wrapper resolves PARSER via
        `${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/parse-ledger-patch.py`.
        Mirroring that layout lets us inject a stub that exits with any
        code or writes corrupted JSON. Reuses the real log-round.sh
        unless overridden so the wrapper progresses past the parser step.
        """
        root = tmp_path / "fake-plugin-root"
        scripts_dir = root / "skills" / "check" / "scripts"
        scripts_dir.mkdir(parents=True)
        parser_path = scripts_dir / "parse-ledger-patch.py"
        parser_path.write_text(parser_script)
        parser_path.chmod(parser_path.stat().st_mode | stat.S_IXUSR)
        log_round_path = scripts_dir / "log-round.sh"
        if log_round_script is None:
            # Symlink to real script so wrapper composes end-to-end.
            log_round_path.symlink_to(
                PLUGIN_ROOT / "skills" / "check" / "scripts" / "log-round.sh"
            )
            # Also need update-state.py in the same dir.
            (scripts_dir / "update-state.py").symlink_to(
                PLUGIN_ROOT / "skills" / "check" / "scripts" / "update-state.py"
            )
        else:
            log_round_path.write_text(log_round_script)
            log_round_path.chmod(log_round_path.stat().st_mode | stat.S_IXUSR)
        return root

    def test_unexpected_parser_exit_maps_to_70(self, tmp_path: Path):
        """Parser exits 6 (unknown to wrapper) → wrapper exit 70.

        Before fix: wrapper's parser case `*)` did `exit "$parser_exit"` —
        raw passthrough. Parser exits outside {0,1,2,3,4,5} leaked into
        whatever range the future parser used, breaking the contract.
        After fix: unexpected parser exits map to 70 (wrapper-internal)
        with diagnostic naming the exit code.
        """
        # Parser stub that exits 6 (deliberately outside the documented range).
        parser_stub = "#!/usr/bin/env python3\nimport sys\nsys.exit(6)\n"
        plugin_root = self._make_fake_plugin_root(tmp_path, parser_stub)
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"unexpected parser exit (6) must map to 70, got "
            f"{result.returncode}. Raw passthrough breaks contract.\n"
            f"stderr: {result.stderr}"
        )
        # Diagnostic should name the original parser exit code.
        assert "6" in result.stderr, (
            f"diagnostic should mention original parser exit code.\n"
            f"stderr: {result.stderr}"
        )

    # ----- R2-F3: post-log-round helpers (trajectory, cat, pivot) → 70 -----

    def test_missing_state_after_log_round_exits_70(self, tmp_path: Path):
        """log-round exits 0 but doesn't write state.json → cat fails → must exit 70.

        Before fix: `cat "${REVIEW_DIR}/state.json"` ran under set -e; a
        missing state.json (broken/buggy log-round helper, race delete)
        crashed with cat exit 1 — masked as parser-no-LEDGER to the caller.
        After fix: wrapper checks state.json exists or wraps cat with a
        failure handler that exits 70 with a clear diagnostic.
        """
        # log-round stub that exits 0 without writing state.json
        log_round_stub = "#!/usr/bin/env bash\nexit 0\n"
        parser_stub = (PLUGIN_ROOT / "skills" / "check" / "scripts"
                       / "parse-ledger-patch.py").read_text()
        plugin_root = self._make_fake_plugin_root(
            tmp_path, parser_stub, log_round_script=log_round_stub,
        )
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"missing state.json after log-round must exit 70, got "
            f"{result.returncode}. Exit 1 collides with parser-no-LEDGER.\n"
            f"stderr: {result.stderr}"
        )
        assert "state.json" in result.stderr, (
            f"diagnostic should name state.json.\nstderr: {result.stderr}"
        )

    def test_unwritable_full_log_exits_70(self, tmp_path: Path):
        """Hotfix R6-F1: pre-existing unwritable full-rN.txt → exit 70.

        Before fix: codex stdout redirected to `$FULL_LOG`. If FULL_LOG
        exists with chmod 000 (or any unwritable shape), bash redirect
        fails → codex never runs → codex_exit=1. Then the diagnostic
        `tail $FULL_LOG | sed` ALSO fails (file unreadable) → pipefail
        bails raw 1 before the wrapper can `exit 71`. Net: wrapper exits
        raw 1 (parser-no-LEDGER collision) for what is actually a
        wrapper-setup error. Reproduced by codex hotfix dogfood round 6.
        After fix: pre-write probe `: > "$FULL_LOG"` catches the
        redirection failure and exits 70 BEFORE codex invocation
        (symmetric with R4-F4 PARSED_FILE probe).
        """
        review_dir = tmp_path / "review"; review_dir.mkdir()
        full_log = review_dir / "full-r1.txt"
        full_log.write_text("")
        os.chmod(full_log, 0o400)  # read-only — bash `>` fails
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        try:
            result = subprocess.run(
                [
                    "bash", str(WRAPPER),
                    "--round", "1",
                    "--review-dir", str(review_dir),
                    "--artifact", "x",
                    "--depth", "standard",
                    "--reviewer", "codex/test",
                    "--prompt-file", str(prompt_file),
                    "--project-root", str(tmp_path),
                ],
                env=env, capture_output=True, text=True, timeout=10,
            )
        finally:
            os.chmod(full_log, 0o600)
        assert result.returncode == 70, (
            f"unwritable FULL_LOG must exit 70, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_devnull_prompt_file_exits_64(self, tmp_path: Path):
        """Hotfix R5-F1: /dev/null prompt-file → exit 64, not codex invocation.

        Before fix: R4-F3 swapped `-f` for `-r`, but `-r` alone passes for
        /dev/null (readable char device). `$(</dev/null)` returns "" with
        success → wrapper invokes codex with empty prompt → spends real
        $ on a doomed review. Reproduced by codex hotfix dogfood round 5.
        After fix: combined `-r && -f` check rejects non-regular paths
        (char/block devices, fifos, directories, sockets) with exit 64
        BEFORE codex invocation.
        """
        result = self._run_wrapper_raw(
            tmp_path,
            "--round", "1",
            "--review-dir", str(tmp_path / "review"),
            "--artifact", "x",
            "--depth", "standard",
            "--reviewer", "codex/test",
            "--prompt-file", "/dev/null",  # readable but not regular
            "--project-root", str(tmp_path),
        )
        assert result.returncode == 64, (
            f"/dev/null as --prompt-file must exit 64 (regular-file check), "
            f"got {result.returncode}. Wrapper would otherwise spend codex "
            f"$ on an empty-prompt review.\nstderr: {result.stderr}"
        )

    def test_unreadable_prompt_file_exits_64(self, tmp_path: Path):
        """Hotfix R4-F3: existing-but-unreadable --prompt-file → exit 64.

        Before fix: `[[ ! -f $PROMPT_FILE ]]` only checks existence.
        `prompt_body="$(<"$PROMPT_FILE")"` then fails on EACCES → exit 1
        under set -e → wrapper bails 1 (parser-no-LEDGER collision).
        Reproduced by codex hotfix dogfood round 4 via `--prompt-file
        /etc/sudoers`.
        After fix: `[[ ! -r $PROMPT_FILE ]]` check covers both missing
        and unreadable; exit 64 (usage error — caller passed an
        unreadable path).
        """
        unreadable = tmp_path / "unreadable.txt"
        unreadable.write_text("x")
        os.chmod(unreadable, 0o000)
        try:
            result = self._run_wrapper_raw(
                tmp_path,
                "--round", "1",
                "--review-dir", str(tmp_path / "review"),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(unreadable),
                "--project-root", str(tmp_path),
            )
        finally:
            os.chmod(unreadable, 0o644)
        assert result.returncode == 64, (
            f"unreadable --prompt-file must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_unwritable_parsed_file_exits_70(self, tmp_path: Path):
        """Hotfix R4-F4: existing-unwritable PARSED_FILE → exit 70.

        Before fix: line 230 `[[ -d "$PARSED_FILE" ]]` guard only catches
        the directory shape. A regular file at parsed-r1.json with
        chmod 400 → bash redirection `>` fails → parser never runs →
        parser_exit=1 → wrapper case 1 → manual fallback. Reproduced by
        codex hotfix dogfood round 4 (R3-F1 partial fix).
        After fix: pre-write probe `: > "$PARSED_FILE"` catches every
        redirection failure (directory, EACCES, ENOSPC, parent
        unwritable) and exits 70 uniformly.
        """
        review_dir = tmp_path / "review"; review_dir.mkdir()
        parsed_file = review_dir / "parsed-r1.json"
        parsed_file.write_text("")  # exists as regular file
        os.chmod(parsed_file, 0o400)  # read-only — bash > fails
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        try:
            result = subprocess.run(
                [
                    "bash", str(WRAPPER),
                    "--round", "1",
                    "--review-dir", str(review_dir),
                    "--artifact", "x",
                    "--depth", "standard",
                    "--reviewer", "codex/test",
                    "--prompt-file", str(prompt_file),
                    "--project-root", str(tmp_path),
                ],
                env=env, capture_output=True, text=True, timeout=10,
            )
        finally:
            os.chmod(parsed_file, 0o600)
        assert result.returncode == 70, (
            f"unwritable PARSED_FILE (regular file, chmod 400) must exit 70, "
            f"got {result.returncode}.\nstderr: {result.stderr}"
        )

    def test_unreadable_state_json_exits_70(self, tmp_path: Path):
        """Hotfix R4-F1: existing-but-unreadable state.json → exit 70 not 1.

        Before fix: `[[ -f state.json ]]` checks existence but not readability.
        log-round stub that writes state.json then chmod 000 it → file
        passes [[ -f ]] but `cat` raises EACCES → exit 1 under set -e →
        wrapper bails 1 (parser-no-LEDGER collision).
        Reproduced by codex hotfix dogfood round 3 R1-F3 still_open.
        After fix: cat wrapped or [[ -r ]] check; failure exits 70.
        """
        log_round_stub = (
            "#!/usr/bin/env bash\n"
            'state_file="${BULLDOZER_REVIEW_DIR}/state.json"\n'
            'echo \'{"round":1,"history":[]}\' > "$state_file"\n'
            'chmod 000 "$state_file"\n'  # exists but unreadable
            'echo "{}"\n'  # log-round stdout
            'exit 0\n'
        )
        parser_stub = (PLUGIN_ROOT / "skills" / "check" / "scripts"
                       / "parse-ledger-patch.py").read_text()
        plugin_root = self._make_fake_plugin_root(
            tmp_path, parser_stub, log_round_script=log_round_stub,
        )
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        try:
            result = subprocess.run(
                [
                    "bash", str(WRAPPER),
                    "--round", "1",
                    "--review-dir", str(review_dir),
                    "--artifact", "x",
                    "--depth", "standard",
                    "--reviewer", "codex/test",
                    "--prompt-file", str(prompt_file),
                    "--project-root", str(tmp_path),
                ],
                env=env, capture_output=True, text=True, timeout=10,
            )
        finally:
            # Restore perms on state.json so pytest can clean up tmp_path.
            try:
                os.chmod(review_dir / "state.json", 0o644)
            except OSError:
                pass
        assert result.returncode == 70, (
            f"unreadable state.json must exit 70, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_parser_stdout_redirect_failure_exits_70(self, tmp_path: Path):
        """Hotfix R4-F2: PARSED_FILE path unwritable → exit 70 not 1.

        Before fix: wrapper does `python3 "$PARSER" > "$PARSED_FILE"`.
        If PARSED_FILE path is a directory (or otherwise unopenable for
        write), bash redirection fails BEFORE python runs, parser never
        executes, but `parser_exit=1` from the bash error → wrapper
        case 1 → manual fallback. Misleading — parser didn't even run.
        Reproduced by codex hotfix dogfood round 3 R3-F1: pre-create
        parsed-r1.json as a directory.
        After fix: pre-validate writability or guard the redirection
        with explicit error mapping to 70.
        """
        review_dir = tmp_path / "review"; review_dir.mkdir()
        # Pre-create parsed-r1.json as a DIRECTORY — bash redirection
        # `> parsed-r1.json` will fail because target is a dir.
        (review_dir / "parsed-r1.json").mkdir()
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"unwritable PARSED_FILE must exit 70, got {result.returncode}. "
            f"Exit 1 falsely indicates parser-no-LEDGER when parser never ran.\n"
            f"stderr: {result.stderr}"
        )

    def test_pivot_write_failure_exits_70_not_10(self, tmp_path: Path):
        """Hotfix R3-F3: pivot file write failure → exit 70, NOT exit 10.

        Required-recheck from R1-F3 round 2 included pivot-write failure
        but the R1-F3 commit only tested missing/corrupt state.json. This
        test pins the pivot path: if python3 cannot write the pivot file
        (EACCES on review dir, fs full), the wrapper must NOT emit exit 10
        + 'see pivot file' (caller would search for a non-existent file).
        After fix (already in 4ef1aee but untested): pivot write failure
        exits 70 with diagnostic explaining why exit 10 was suppressed.

        Setup: depth=quick (max_rounds=1) + round=1 + NO-GO verdict → pivot
        gate fires. log-round stub writes state.json then `chmod 555` the
        review dir so the subsequent pivot python3 open(w) raises
        PermissionError.
        """
        # log-round stub: write valid state.json, then revoke write perms.
        log_round_stub = (
            "#!/usr/bin/env bash\n"
            'state_file="${BULLDOZER_REVIEW_DIR}/state.json"\n'
            'cat > "$state_file" <<EOF\n'
            '{"round":1,"artifact":"x","depth":"quick","started_at":"2026-05-28T00:00:00+00:00",'
            '"reviewer":"codex/test","findings_total":3,"fixed_total":0,"false_positives":0,'
            '"history":[{"round":1,"verdict":"NO-GO","findings":3,"fixed":0,"fp":0,'
            '"timestamp":"2026-05-28T00:00:00+00:00"}]}\n'
            'EOF\n'
            'echo "{}"  # mimic log-round stdout\n'
            'chmod 555 "${BULLDOZER_REVIEW_DIR}"\n'
            'exit 0\n'
        )
        parser_stub = (PLUGIN_ROOT / "skills" / "check" / "scripts"
                       / "parse-ledger-patch.py").read_text()
        plugin_root = self._make_fake_plugin_root(
            tmp_path, parser_stub, log_round_script=log_round_stub,
        )
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        try:
            result = subprocess.run(
                [
                    "bash", str(WRAPPER),
                    "--round", "1",
                    "--review-dir", str(review_dir),
                    "--artifact", "x",
                    "--depth", "quick",  # max_rounds=1, ROUND=1 → pivot fires
                    "--reviewer", "codex/test",
                    "--prompt-file", str(prompt_file),
                    "--project-root", str(tmp_path),
                ],
                env=env, capture_output=True, text=True, timeout=10,
            )
        finally:
            # Restore write perms so pytest tmp_path cleanup doesn't fail.
            os.chmod(review_dir, 0o755)
        assert result.returncode == 70, (
            f"pivot write failure must exit 70, got {result.returncode}. "
            f"Exit 10 with a missing pivot file would mislead caller.\n"
            f"stderr: {result.stderr}"
        )
        # Diagnostic should explain that exit 10 was suppressed.
        assert "pivot" in result.stderr.lower(), (
            f"diagnostic should mention pivot context.\nstderr: {result.stderr}"
        )

    def test_unmakeable_review_dir_exits_70(self, tmp_path: Path):
        """Hotfix R3-F2: `mkdir -p $REVIEW_DIR` failure → wrapper exit 70.

        Before fix: line 125 `mkdir -p "$REVIEW_DIR"` ran under set -e.
        Passing `--review-dir /dev/null/bd-review` (or any path whose
        parent is a non-directory) → mkdir exits 1 → wrapper bails with
        raw 1. Reproduced by codex hotfix dogfood round 2.
        After fix: mkdir wrapped with `|| { ... exit 70; }` diagnostic
        naming the unmakeable path.
        """
        # /dev/null/bd-review — /dev/null is a char device, not a dir, so
        # mkdir -p will fail no matter what umask/permissions are in play.
        # Real prompt file so we get past the (preflight) prompt-file check
        # and actually hit the mkdir.
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = self._run_wrapper_raw(
            tmp_path,
            "--round", "1",
            "--review-dir", "/dev/null/bd-review",
            "--artifact", "x",
            "--depth", "standard",
            "--reviewer", "codex/test",
            "--prompt-file", str(prompt_file),
            "--project-root", str(tmp_path),
        )
        assert result.returncode == 70, (
            f"unmakeable --review-dir must exit 70, got {result.returncode}. "
            f"Exit 1 from mkdir collides with parser-no-LEDGER.\n"
            f"stderr: {result.stderr}"
        )
        assert "/dev/null/bd-review" in result.stderr, (
            f"diagnostic should name the unmakeable path.\nstderr: {result.stderr}"
        )

    def test_corrupted_state_after_log_round_round2_exits_70(self, tmp_path: Path):
        """Round 2: log-round writes corrupt state.json → trajectory python crash → must exit 70.

        Trajectory rendering only fires on round >= 2. log-round stub
        writes invalid JSON; python3 heredoc raises json.JSONDecodeError
        → wrapper exits raw 1 under set -e.
        After fix: trajectory failure mapped to 70.
        """
        log_round_stub = (
            "#!/usr/bin/env bash\n"
            'state_file="${BULLDOZER_REVIEW_DIR}/state.json"\n'
            'echo "not valid json{{" > "$state_file"\n'
            'echo "{}"  # log-round prints state to stdout normally\n'
            'exit 0\n'
        )
        parser_stub = (PLUGIN_ROOT / "skills" / "check" / "scripts"
                       / "parse-ledger-patch.py").read_text()
        plugin_root = self._make_fake_plugin_root(
            tmp_path, parser_stub, log_round_script=log_round_stub,
        )
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_NOGO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "2",  # forces trajectory branch
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "exhaustive",  # max_rounds=10, ROUND=2 < max, no pivot
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"corrupted state.json on round>=2 (trajectory branch) must exit 70, "
            f"got {result.returncode}. Exit 1 collides with parser-no-LEDGER.\n"
            f"stderr: {result.stderr}"
        )

    def test_parser_out_subshell_failure_exits_70(self, tmp_path: Path):
        """Parser writes JSON-invalid output → parser_out subshell crashes.

        Wrapper's `parser_out=$(python3 -c '...')` block reads PARSED_FILE
        as JSON. If parser exits 0 but writes garbage instead of JSON,
        the inline json.load raises and python3 exits 1 — under set -e
        the wrapper bails with raw 1 (parser-no-LEDGER collision).
        After fix: subshell failure mapped to 70 with diagnostic.
        """
        # Parser stub that exits 0 but writes invalid JSON to stdout.
        parser_stub = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "sys.stdout.write('not valid json at all{')\n"
            "sys.exit(0)\n"
        )
        plugin_root = self._make_fake_plugin_root(tmp_path, parser_stub)
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"corrupted parsed-JSON must exit 70, got {result.returncode}. "
            f"Exit 1 from inline python3 collides with parser-no-LEDGER.\n"
            f"stderr: {result.stderr}"
        )

    def test_bad_reviewer_format_exits_64(self, tmp_path: Path):
        """--reviewer without provider/model slash is a usage error."""
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = self._run_wrapper_raw(
            tmp_path,
            "--round", "1",
            "--review-dir", str(tmp_path / "review"),
            "--artifact", "x",
            "--depth", "standard",
            "--reviewer", "codex-only-no-slash",
            "--prompt-file", str(prompt_file),
            "--project-root", str(tmp_path),
        )
        assert result.returncode == 64, (
            f"bad reviewer format must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_missing_prompt_file_exits_64(self, tmp_path: Path):
        """--prompt-file pointing at non-existent path is a usage error."""
        result = self._run_wrapper_raw(
            tmp_path,
            "--round", "1",
            "--review-dir", str(tmp_path / "review"),
            "--artifact", "x",
            "--depth", "standard",
            "--reviewer", "codex/test",
            "--prompt-file", str(tmp_path / "does-not-exist.txt"),
            "--project-root", str(tmp_path),
        )
        assert result.returncode == 64, (
            f"missing prompt file must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    # ----- R1-F3b: BULLDOZER_FIXED/FP boundary validation -----

    def test_invalid_bulldozer_fixed_exits_64(self, tmp_path: Path):
        """BULLDOZER_FIXED=abc → wrapper exit 64 (not 1 from update-state).

        Before fix: wrapper passes raw value to log-round.sh → update-state.py,
        which raises ValueError on int(\"abc\") and sys.exit(1). Under set -e
        that bubbles to wrapper exit 1 → collides with parser-no-LEDGER_PATCH.
        After fix: wrapper validates numeric at boundary, exits 64 (usage error).
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["BULLDOZER_FIXED"] = "abc"  # invalid
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 64, (
            f"BULLDOZER_FIXED=abc must exit 64 at wrapper boundary, got "
            f"{result.returncode}. Exit 1 would mask as parser-no-LEDGER_PATCH.\n"
            f"stderr: {result.stderr}"
        )

    # ----- R1-F3c: missing parser path exits 70 (wrapper-internal) -----

    # ----- R1-F3d: log-round.sh failure exits 70 (wrapper-internal) -----

    def test_log_round_failure_exits_70_not_1(self, tmp_path: Path):
        """Corrupted state.json → update-state.py exit 1 → wrapper must exit 70.

        Before fix: log-round.sh invoked under `set -e`; update-state.py's
        sys.exit(1) on json.JSONDecodeError bubbled to wrapper exit 1 →
        looked like parser-no-LEDGER_PATCH to the caller, sending it down
        the manual prose extraction path.
        After fix: wrapper traps log-round non-zero, exits 70 with a
        diagnostic naming log-round.sh and the original exit code.
        """
        # Pre-seed the review dir with a corrupted state.json so update-state.py
        # fails JSON decode → exits 1 → log-round.sh exits 1.
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        (review_dir / "state.json").write_text("{not json at all]")
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"log-round failure must exit 70 (wrapper-internal), got "
            f"{result.returncode}. Exit 1 would mask as parser-no-LEDGER_PATCH.\n"
            f"stderr: {result.stderr}"
        )
        # Diagnostic must name log-round.sh so operator knows which downstream failed.
        assert "log-round" in result.stderr, (
            f"diagnostic should name the failing helper.\nstderr: {result.stderr}"
        )

    def test_missing_parser_path_exits_70(self, tmp_path: Path):
        """Stale CLAUDE_PLUGIN_ROOT → wrapper exit 70 (not 2 from python3).

        Before fix: if CLAUDE_PLUGIN_ROOT points at a cache path that no
        longer contains parse-ledger-patch.py (jaine-sync update did not
        prune properly), python3 errors with FileNotFoundError → exit 2.
        Wrapper case 2 then printed 'STOP: malformed LEDGER_PATCH YAML'
        and the operator hunted for a non-existent .malformed.yml.
        After fix: wrapper checks parser path exists BEFORE invoking
        python3; exits 70 (EX_SOFTWARE — wrapper-internal failure) with
        a diagnostic naming the missing path so operator can fix
        CLAUDE_PLUGIN_ROOT.
        """
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        # CLAUDE_PLUGIN_ROOT pointing at a dir WITHOUT the parser script.
        bogus_root = tmp_path / "stale-cache"
        bogus_root.mkdir()
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["CLAUDE_PLUGIN_ROOT"] = str(bogus_root)
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"missing parser path must exit 70, got {result.returncode}.\n"
            f"Exit 2 would falsely indicate malformed LEDGER_PATCH.\n"
            f"stderr: {result.stderr}"
        )
        # Diagnostic must name the bogus path so operator knows what's stale.
        assert "parse-ledger-patch.py" in result.stderr, (
            f"diagnostic should name the missing parser script.\n"
            f"stderr: {result.stderr}"
        )

    def test_invalid_bulldozer_fp_exits_64(self, tmp_path: Path):
        """BULLDOZER_FP=xyz → wrapper exit 64 (not 1)."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=VALID_LEDGER_GO,
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        env["BULLDOZER_FP"] = "xyz"  # invalid
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", "standard",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 64, (
            f"BULLDOZER_FP=xyz must exit 64 at wrapper boundary, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    def test_invalid_depth_exits_64(self, tmp_path: Path):
        """--depth outside {quick, standard, exhaustive} is a usage error.

        Currently caught after codex invocation in the case statement
        (line 91-103); fix must validate at preflight time so codex isn't
        invoked for a doomed depth.
        """
        prompt_file = tmp_path / "p.txt"; prompt_file.write_text("x")
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(tmp_path / "review"),
                "--artifact", "x",
                "--depth", "ultraviolet",
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 64, (
            f"invalid depth must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )
