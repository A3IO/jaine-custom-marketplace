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

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import PLUGIN_ROOT

WRAPPER = PLUGIN_ROOT / "skills" / "check" / "scripts" / "bulldozer-round.sh"
RENDER_TRAJECTORY = PLUGIN_ROOT / "skills" / "check" / "scripts" / "render-trajectory.py"
EMIT_PIVOT = PLUGIN_ROOT / "skills" / "check" / "scripts" / "emit-pivot.py"
READ_DEPTH_CONFIG = PLUGIN_ROOT / "skills" / "check" / "scripts" / "read-depth-config.py"
DEPTH_CONFIG = PLUGIN_ROOT / "skills" / "check" / "data" / "depth-config.json"
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


# C3 (#110): the stub `codex` binary is static — it reads its per-install
# behavior (exit code, write toggle, verdict body) from sidecar files in the
# directory the symlink lives in ($0's dirname), so the script bytes never
# change. Built ONCE per process (lazily, module-cached) and symlinked into each
# install dir, replacing ~80 per-test write+chmod calls. xdist-safe: each worker
# is its own process with its own module cache + its own template path.
_codex_stub_template_path: Path | None = None

# Static stub body. Per-install config comes from sibling files resolved via
# $0's directory (the install dir), NOT baked into these bytes:
#   stub_config      — two lines: "EXIT=<n>" and "WRITE=<0|1>"
#   verdict_body.txt — bytes to drop at the -o path when WRITE=1
_CODEX_STUB_BODY = textwrap.dedent("""\
    #!/usr/bin/env bash
    # C3 static codex stub — behavior read from sidecars next to this symlink.
    self_dir="$(cd "$(dirname "$0")" && pwd)"
    exit_code=0
    write_verdict=1
    if [[ -f "$self_dir/stub_config" ]]; then
        while IFS='=' read -r k v; do
            case "$k" in
                EXIT)  exit_code="$v" ;;
                WRITE) write_verdict="$v" ;;
            esac
        done < "$self_dir/stub_config"
    fi
    # Drain stdin (real codex reads the prompt from stdin since A4; capture it
    # when CODEX_STUB_STDIN_FILE is set, else discard to avoid SIGPIPE).
    if [[ -n "${CODEX_STUB_STDIN_FILE:-}" ]]; then
        cat > "$CODEX_STUB_STDIN_FILE"
    else
        cat > /dev/null
    fi
    if [[ -n "${CODEX_STUB_ARGS_FILE:-}" ]]; then
        printf '%s\\n' "$@" > "$CODEX_STUB_ARGS_FILE"
    fi
    verdict_path=""
    args=("$@")
    for ((i=0; i<${#args[@]}; i++)); do
        if [[ "${args[$i]}" == "-o" ]]; then
            verdict_path="${args[$((i+1))]}"
            break
        fi
    done
    if [[ -n "$verdict_path" && "$write_verdict" == 1 ]]; then
        mkdir -p "$(dirname "$verdict_path")"
        cp "$self_dir/verdict_body.txt" "$verdict_path"
    fi
    exit "$exit_code"
""")


def _codex_stub_template() -> Path:
    """Build the static stub binary once per process and cache it. Lives in a
    stable module-private dir (not any test's tmp_path) so installs can symlink
    to one shared file (C3).

    The final template path is shared across processes (fixed name under the
    system tempdir). To keep that safe under `pytest -n auto` — where multiple
    worker processes may build it concurrently — the body is written to a
    per-pid temp file and atomically `os.replace`d into place: a reader (the
    stub invoked by the wrapper) ever sees either the old complete file or the
    new complete file, never a half-written one (R1-F1, PR-4 dogfood). The
    module global still caches the path so each process builds at most once.
    """
    global _codex_stub_template_path
    if _codex_stub_template_path is None:
        import os
        import tempfile
        cache_dir = Path(tempfile.gettempdir()) / "bulldozer-codex-stub-template"
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmpl = cache_dir / "codex"
        # Write to a unique sibling, set the exec bit, then atomically rename
        # over the shared path. os.replace is atomic on the same filesystem, so
        # concurrent xdist workers never expose a partial template.
        tmp = cache_dir / f"codex.{os.getpid()}.tmp"
        tmp.write_text(_CODEX_STUB_BODY)
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, tmpl)
        _codex_stub_template_path = tmpl
    return _codex_stub_template_path


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

    C3 (#110): the executable is a symlink to one shared static template; the
    per-install behavior (exit code, write toggle, verdict body) is written as
    sidecar files in `stub_dir`, which the template reads via $0's dirname.
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    # Per-install behavior sidecars (read by the static template at run time).
    (stub_dir / "stub_config").write_text(
        f"EXIT={exit_code}\nWRITE={1 if write_verdict else 0}\n")
    # Verdict body in a sidecar so bash doesn't interpret escapes (Python's
    # repr() turns \n into literal '\n' which printf '%s' would emit verbatim,
    # breaking the parser).
    (stub_dir / "verdict_body.txt").write_text(verdict_body)
    stub = stub_dir / "codex"
    if stub.exists() or stub.is_symlink():
        stub.unlink()
    stub.symlink_to(_codex_stub_template())
    return stub_dir


def _run_wrapper(tmp_path: Path, stub_dir: Path, *,
                 round_num: int = 1,
                 depth: str = "standard",
                 reviewer: str = "codex/test-model",
                 prompt: str = "review this",
                 extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke wrapper with sandboxed paths and a stubbed codex on PATH.

    extra_env (D4, #110): vars merged over the base PATH/REVIEW_DIR/LOG env,
    applied LAST so they can add new vars (CODEX_STUB_*, BULLDOZER_FIXED/FP) or
    override a base default. None = base env only (unchanged behavior)."""
    review_dir = tmp_path / "review"
    review_dir.mkdir(exist_ok=True)
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(prompt)

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
    env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
    env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
    if extra_env:
        env.update(extra_env)

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
        Round-1 quick template body in SKILL.md must NOT also start with it,
        or codex would receive 'SKIP SKILLS. SKIP SKILLS. ...' — double prefix.

        The fix: wrapper owns the prefix exclusively; template body omits it.
        Since A4 (#110) the prompt reaches codex via STDIN (not argv), so
        capture stdin and count: exactly one 'SKIP SKILLS.' substring.
        """
        stdin_dump = tmp_path / "codex_stdin.txt"
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["CODEX_STUB_STDIN_FILE"] = str(stdin_dump)
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
        # The prompt (prefix + body) arrives on codex stdin since A4; count
        # "SKIP SKILLS." across the captured stdin.
        stdin_text = stdin_dump.read_text()
        count = stdin_text.count("SKIP SKILLS.")
        assert count == 1, (
            f"prompt must contain exactly one 'SKIP SKILLS.' prefix, got "
            f"{count}. Either template body still leads with SKIP SKILLS. "
            f"(template owns it AND wrapper prepends → 2) or neither does → 0.\n"
            f"stdin:\n{stdin_text}"
        )

    def test_codex_invoked_with_model_from_reviewer(self, tmp_path: Path):
        """--reviewer 'codex/X' must produce '-m X' on the codex command line.

        The composer arg list (#102) only carries `--reviewer codex/MODEL`;
        the wrapper is responsible for extracting MODEL and threading it
        through to codex's `-m` flag.
        """
        args_dump = tmp_path / "codex_args.txt"
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)

        # D4 (#110): base wrapper run + one extra var via extra_env, replacing
        # the manual env-copy + full subprocess.run boilerplate.
        _run_wrapper(tmp_path, stub_dir, reviewer="codex/gpt-5.1",
                     extra_env={"CODEX_STUB_ARGS_FILE": str(args_dump)})
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
    """Composer step 4 exit-1 branch: no LEDGER_PATCH block → manual fallback.

    PR-1 (issue #110 B5) repurposed parser exit 1 from raw wrapper exit 1
    (which re-created the #98/#102 discipline gap by handing control back
    to Claude with no state recorded) to wrapper exit 11 + state.json
    logged with verdict=UNKNOWN + manual_extraction_pending=true. The
    fact-pattern for these tests (parser sees no LEDGER_PATCH block)
    is unchanged; only the expected wrapper exit code + diagnostic
    phrasing moved. See TestManualExtractionBranch for the new contract.
    """

    def test_prose_verdict_without_block_exits_11(self, tmp_path: Path):
        """Reviewer wrote prose but forgot LEDGER_PATCH — parser exits 1.

        Wrapper now exits 11 (was raw exit 1 pre-PR-1) so the caller knows
        to extract findings from prose and call update-state.py with
        --mode=replace-extraction.
        """
        prose = "The code looks fine. NO-GO because of issue X, but I didn't include the structured block.\n"
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=prose,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11, (
            f"parser exit 1 (no LEDGER_PATCH) must map to wrapper exit 11, "
            f"got {result.returncode}\nstderr: {result.stderr}"
        )

    def test_exit_11_diagnostic_names_manual_fallback(self, tmp_path: Path):
        """Operator/caller needs to know this is fallback-to-manual, not a crash."""
        prose = "Just GO. Sorry, no patch block.\n"  # bare GO with NO-GO absent
        # Note: bare-GO synthesis would actually trigger exit 0 here, so use
        # a verdict that has neither GO nor NO-GO + no block to force exit 1.
        prose = "I reviewed the file. Some issues. See above.\n"
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=prose,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11, result.stderr
        # Diagnostic must mention manual extraction so the caller doesn't
        # confuse this with a parser crash. New phrasing uses
        # MANUAL_EXTRACTION_REQUIRED token (post-PR-1).
        assert "manual" in result.stderr.lower(), (
            f"exit-11 diagnostic should mention manual extraction; "
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
    """Install a fake `python3` that exits `exit_code` ONLY for the parser
    invocation (parse-ledger-patch.py), simulating parser exit 4 (PyYAML
    missing). Every other python3 call — the B1 depth-config read
    (read-depth-config.py) and the inline `-c` findings/verdict reader — is
    passed through to the real interpreter so the wrapper reaches the parser
    before failing. Must come AFTER codex on PATH (codex has its own shebang).
    """
    stub_dir.mkdir(parents=True, exist_ok=True)
    real = sys.executable
    stub = stub_dir / "python3"
    stub.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        for arg in "$@"; do
            case "$arg" in
                *parse-ledger-patch.py)
                    echo "ERROR: PyYAML is not installed. Run: pip install pyyaml" >&2
                    exit {exit_code}
                    ;;
            esac
        done
        exec {real!r} "$@"
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
        # B3 (#110): the wrapper resolves render-trajectory.py + emit-pivot.py
        # from CLAUDE_PLUGIN_ROOT and pre-validates their existence (exit 70 if
        # missing). A fake plugin root must carry the real scripts or every
        # round past log-round trips the guard before the test's real assertion.
        for _b3 in ("render-trajectory.py", "emit-pivot.py"):
            (scripts_dir / _b3).symlink_to(
                PLUGIN_ROOT / "skills" / "check" / "scripts" / _b3
            )
        # B1 (#110): the wrapper also resolves read-depth-config.py and reads
        # data/depth-config.json from CLAUDE_PLUGIN_ROOT (exit 70 if missing),
        # so the fake root must carry both or the depth preflight trips the
        # guard before the test's real assertion.
        (scripts_dir / "read-depth-config.py").symlink_to(
            PLUGIN_ROOT / "skills" / "check" / "scripts" / "read-depth-config.py"
        )
        data_dir = root / "skills" / "check" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "depth-config.json").symlink_to(
            PLUGIN_ROOT / "skills" / "check" / "data" / "depth-config.json"
        )
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
        # B1 (#110): provision depth-config.json + read-depth-config.py so the
        # depth preflight passes and the wrapper reaches the parser-path check
        # this test exercises (parse-ledger-patch.py stays intentionally absent).
        import shutil
        _bscripts = bogus_root / "skills" / "check" / "scripts"
        _bscripts.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            PLUGIN_ROOT / "skills" / "check" / "scripts" / "read-depth-config.py",
            _bscripts / "read-depth-config.py")
        _bdata = bogus_root / "skills" / "check" / "data"
        _bdata.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            PLUGIN_ROOT / "skills" / "check" / "data" / "depth-config.json",
            _bdata / "depth-config.json")
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


class TestManualExtractionBranch:
    """Parser exit 1 (no LEDGER_PATCH in verdict) is no longer raw exit 1.
    Wrapper logs the round to state.json with verdict=UNKNOWN +
    manual_extraction_pending=true, then exits 11 so caller knows to
    extract findings from prose and call --mode=replace-extraction."""

    _NO_LEDGER_VERDICT = "The reviewer wrote prose but no LEDGER_PATCH block.\nFindings appear inline.\n"

    def test_missing_ledger_patch_exits_11(self, tmp_path: Path):
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body=self._NO_LEDGER_VERDICT,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11, (
            f"exit 11 required for manual-extraction branch (was raw exit 1); "
            f"got {result.returncode}; stderr={result.stderr!r}"
        )

    def test_exit_11_logs_round_with_unknown_verdict(self, tmp_path: Path):
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body=self._NO_LEDGER_VERDICT,
        )
        review_dir = tmp_path / "review"
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11
        state_file = review_dir / "state.json"
        assert state_file.exists(), "state.json must exist after exit 11"
        state = json.loads(state_file.read_text())
        assert state["round"] == 1
        entry = state["history"][0]
        assert entry["verdict"] == "UNKNOWN"
        assert entry["findings"] == 0
        assert entry.get("manual_extraction_pending") is True

    def test_exit_11_appends_to_bulldozer_log(self, tmp_path: Path):
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body=self._NO_LEDGER_VERDICT,
        )
        log_file = tmp_path / "bulldozer.log"
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11
        assert log_file.exists(), "bulldozer.log must be appended even on exit 11"
        log_line = log_file.read_text().strip()
        assert "verdict=UNKNOWN" in log_line
        assert "round=1" in log_line

    def test_exit_11_stderr_names_verdict_file_and_command(self, tmp_path: Path):
        """Operator-facing diagnostic must say what to do next."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body=self._NO_LEDGER_VERDICT,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11
        assert "verdict-r1.txt" in result.stderr, (
            f"stderr must name the verdict file for prose extraction; got {result.stderr!r}"
        )
        assert "replace-extraction" in result.stderr, (
            f"stderr must name the replace-extraction command; got {result.stderr!r}"
        )

    def test_existing_LEDGER_PATCH_path_still_exits_0(self, tmp_path: Path):
        """Sanity: structured-LEDGER path is untouched."""
        verdict_body = "Some prose.\n\n```yaml\nLEDGER_PATCH:\n  findings: []\n  verdict: go\n```\n"
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=verdict_body,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 0, f"GO path must still exit 0; stderr={result.stderr!r}"

    def test_pre_round_guard_blocks_when_pending_unresolved(self, tmp_path: Path):
        """If prior state.json has manual_extraction_pending=true, wrapper must
        refuse to start a new round (exit 64) until --mode=replace-extraction
        clears the flag. Prevents stale-state appended over.

        R1-F1: without this guard, Claude exiting between wrapper-exit-11 and
        the replace-extraction call lets the next round overwrite the pending
        entry with fresh data — UNKNOWN/findings=0 placeholders pollute
        trajectory/pivot decisions and the discipline invariant is lost.
        """
        # Seed state.json with unresolved pending round 1
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        seed_state = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00",
            "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": 0,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                "manual_extraction_pending": True,
            }],
        }
        (review_dir / "state.json").write_text(json.dumps(seed_state))
        # Try to start round 2 — should be rejected
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                        verdict_body="LEDGER_PATCH:\n  findings: []\n  verdict: go\n")
        result = _run_wrapper(tmp_path, stub_dir, round_num=2)
        assert result.returncode == 64, (
            f"wrapper must refuse new round when prior pending unresolved; "
            f"got exit {result.returncode}; stderr={result.stderr!r}"
        )
        assert "manual_extraction_pending" in result.stderr
        assert "replace-extraction" in result.stderr

    def test_pre_round_guard_allows_when_pending_cleared(self, tmp_path: Path):
        """After replace-extraction clears the flag, next round runs normally.

        R1-F1: positive companion to the rejection test — ensures the guard
        does NOT misfire when manual_extraction_pending is False (the normal
        state after replace-extraction reconciliation).
        """
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        seed_state = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00",
            "reviewer": "codex/test",
            "findings_total": 5, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "NO-GO", "findings": 5,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                "manual_extraction_pending": False,  # cleared
            }],
        }
        (review_dir / "state.json").write_text(json.dumps(seed_state))
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                        verdict_body="LEDGER_PATCH:\n  findings: []\n  verdict: go\n")
        result = _run_wrapper(tmp_path, stub_dir, round_num=2)
        assert result.returncode == 0, f"stderr={result.stderr!r}"

    def test_pre_round_guard_handles_corrupt_pending_entry(self, tmp_path: Path):
        """Bug #1 regression: pending entry missing the `round` key (corrupt
        state, manual hand-edit, partial migration). Pre-fix: python printed
        the "?" default; bash arithmetic `$(( "?" + 1 ))` syntax-errored
        inside argument expansion (does NOT trigger set -e), so `_emit_stop
        64` never ran and control fell through to the next round — silent
        invariant violation. Post-fix: wrapper detects corruption and exits
        with a non-zero diagnostic instead of running the next round."""
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        # Corrupt: pending entry has no `round` key
        seed_state = {
            "history": [
                {"verdict": "UNKNOWN", "findings": 0,
                 "manual_extraction_pending": True},
            ],
        }
        (review_dir / "state.json").write_text(json.dumps(seed_state))
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="LEDGER_PATCH:\n  findings: []\n  verdict: go\n",
        )
        result = _run_wrapper(tmp_path, stub_dir, round_num=2)
        # Whatever exit-code class the fix uses, it MUST be non-zero so the
        # discipline invariant holds. Must NOT log round 2 over the corrupt
        # state.
        assert result.returncode != 0, (
            f"corrupt pending entry must NOT be silently overwritten by next "
            f"round; got exit {result.returncode}, stderr={result.stderr!r}"
        )
        # Specifically: bash arithmetic syntax error must not appear (means
        # the fix eliminated the arithmetic class entirely OR validated input
        # before reaching arithmetic).
        assert "arithmetic syntax error" not in result.stderr, (
            f"bug #1: arithmetic class still leaks; stderr={result.stderr!r}"
        )
        # state.json must still hold ONLY the original corrupt entry (no
        # round 2 silently appended).
        state_after = json.loads((review_dir / "state.json").read_text())
        rounds_logged = [
            h.get("round") for h in state_after.get("history", [])
        ]
        assert 2 not in rounds_logged, (
            f"round 2 must NOT be appended when pre-round guard fires; "
            f"history rounds={rounds_logged!r}"
        )

    def test_pre_round_guard_handles_non_bool_pending_flag(self, tmp_path: Path):
        """R1-F3 (R2 dogfood): pre-round guard helper uses
        `entry.get("manual_extraction_pending") is True`. If state.json has
        a non-bool truthy value (string "true" from hand-edit, corrupt
        migration), strict identity check skips it → guard doesn't fire →
        next round overwrites unresolved corrupt state.

        Post-fix: non-bool, non-None, non-False values are treated as
        corrupt pending — guard emits CORRUPT_NON_BOOL_FLAG sentinel,
        bash regex routes to _emit_stop 70 (corrupt diagnostic path)."""
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        seed_state = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00",
            "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": 0,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                # STRING "true" — not canonical bool True
                "manual_extraction_pending": "true",
            }],
        }
        (review_dir / "state.json").write_text(json.dumps(seed_state))
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="LEDGER_PATCH:\n  findings: []\n  verdict: go\n",
        )
        result = _run_wrapper(tmp_path, stub_dir, round_num=2)
        assert result.returncode == 70, (
            f"non-bool pending flag must route to corrupt-pending exit 70; "
            f"got exit {result.returncode}, stderr={result.stderr!r}"
        )
        assert "corrupt" in result.stderr.lower(), (
            f"stderr must surface corruption diagnostic; got {result.stderr!r}"
        )
        # Round 2 must NOT have been appended over the corrupt state
        state_after = json.loads((review_dir / "state.json").read_text())
        rounds_logged = [
            h.get("round") for h in state_after.get("history", [])
        ]
        assert 2 not in rounds_logged, (
            f"round 2 must NOT be appended when pre-round guard fires; "
            f"history rounds={rounds_logged!r}"
        )

    def test_exit_11_stderr_uses_absolute_review_dir_path(self, tmp_path: Path):
        """R3-F1: wrapper must canonicalize REVIEW_DIR to absolute path before
        printing the MANUAL_EXTRACTION_REQUIRED recovery command. Otherwise
        Claude's later Bash tool invocations (possibly from different cwd)
        resolve the relative path against wrong directory.

        Empirical: invoke wrapper with RELATIVE --review-dir from a working
        directory; assert exit-11 stderr's update-state.py command contains
        the absolute path, NOT the relative form."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body=self._NO_LEDGER_VERDICT,
        )
        # Pass RELATIVE review-dir name + run from tmp_path so absolute would
        # be /tmp_path/<reldir>
        rel_name = "rel_review"
        (tmp_path / rel_name).mkdir()
        prompt = tmp_path / "prompt.txt"; prompt.write_text("review")
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / rel_name)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            ["bash", str(WRAPPER),
             "--round", "1",
             "--review-dir", rel_name,  # RELATIVE
             "--artifact", "test",
             "--depth", "standard",
             "--reviewer", "codex/x",
             "--prompt-file", str(prompt),
             "--project-root", str(tmp_path)],
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 11, f"expected exit 11; got {result.returncode}; stderr={result.stderr!r}"
        # The recovery command in stderr should reference the ABSOLUTE path
        abs_path = str((tmp_path / rel_name).resolve())
        assert abs_path in result.stderr, (
            f"stderr must contain absolute path {abs_path!r}; got stderr={result.stderr!r}"
        )
        # And should NOT have the relative form alone as the --review-dir value
        # (i.e., 'update-state.py --review-dir rel_review ' would be wrong)
        assert f"--review-dir {rel_name} " not in result.stderr and \
               f"--review-dir {rel_name}\n" not in result.stderr, (
            f"stderr must NOT use relative path in recovery command; got {result.stderr!r}"
        )

    @staticmethod
    def _bash_pctq(path: str) -> str:
        """Return bash's `printf %q` shell-escaping of `path` — the exact form
        the wrapper must emit. Computed via the same bash so the assertion is
        robust across bash versions instead of hardcoding an escaping style."""
        return subprocess.run(
            ["bash", "-c", 'printf "%q" "$1"', "_", path],
            capture_output=True, text=True,
        ).stdout

    def test_exit_11_recovery_command_quotes_review_dir_with_spaces(self, tmp_path: Path):
        """R5-F1 (dogfood round 4/5): the MANUAL_EXTRACTION_REQUIRED recovery
        command must shell-escape the --review-dir value so a review dir whose
        absolute path contains spaces is copy-paste safe.

        SKILL.md Step 1 derives REVIEW_DIR from the artifact basename, which can
        legitimately contain spaces (e.g. an artifact 'Some File.md' →
        .bulldozer/SESSION-Some File). Without escaping, copying the stderr
        command splits the path on whitespace and sends update-state.py a broken
        argv (path truncated at the first space, the remainder parsed as a
        positional). The wrapper emits the path via `printf %q` (R6-F2), which
        backslash-escapes spaces."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body=self._NO_LEDGER_VERDICT,
        )
        review_dir = tmp_path / "review dir with spaces"
        review_dir.mkdir()
        prompt = tmp_path / "prompt.txt"; prompt.write_text("review")
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            ["bash", str(WRAPPER),
             "--round", "1",
             "--review-dir", str(review_dir),
             "--artifact", "test",
             "--depth", "standard",
             "--reviewer", "codex/x",
             "--prompt-file", str(prompt),
             "--project-root", str(tmp_path)],
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 11, (
            f"expected exit 11; got {result.returncode}; stderr={result.stderr!r}"
        )
        abs_path = str(review_dir.resolve())
        expected_q = self._bash_pctq(abs_path)
        # The recovery command must emit the shell-escaped path so it survives
        # copy-paste with spaces intact.
        assert f"--review-dir {expected_q}" in result.stderr, (
            f"exit-11 recovery command must shell-escape the review-dir path; "
            f"expected --review-dir {expected_q}; got stderr={result.stderr!r}"
        )

    def test_exit_11_recovery_command_shell_escapes_metacharacters(self, tmp_path: Path):
        """R6-F2 (dogfood round 6): double-quoting protects spaces but NOT
        `$`, backticks, or `"`. A review dir whose absolute path contains shell
        metacharacters (legal in filenames; REVIEW_DIR derives from artifact
        basenames) would trigger command substitution or break when the stderr
        recovery command is copy-pasted. The wrapper must emit a fully
        shell-escaped path (`printf %q`) at the exit-11 block — not bare
        double-quotes."""
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=self._NO_LEDGER_VERDICT,
        )
        # $(...) command substitution + space — the dangerous combination.
        review_dir = tmp_path / "rev $(touch PWNED) dir"
        review_dir.mkdir()
        prompt = tmp_path / "prompt.txt"; prompt.write_text("review")
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            ["bash", str(WRAPPER),
             "--round", "1", "--review-dir", str(review_dir),
             "--artifact", "test", "--depth", "standard",
             "--reviewer", "codex/x", "--prompt-file", str(prompt),
             "--project-root", str(tmp_path)],
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 11, (
            f"expected exit 11; got {result.returncode}; stderr={result.stderr!r}"
        )
        abs_path = str(review_dir.resolve())
        expected_q = self._bash_pctq(abs_path)
        assert f"--review-dir {expected_q}" in result.stderr, (
            f"exit-11 recovery command must shell-escape metacharacters via "
            f"printf %q; expected --review-dir {expected_q}; "
            f"got stderr={result.stderr!r}"
        )
        # The raw unescaped substitution must NOT appear as a copy-pasteable
        # token (it would execute $(touch PWNED) in the operator's shell).
        assert f"--review-dir {abs_path} " not in result.stderr, (
            "raw unescaped metachar path must not appear in recovery command"
        )

    def test_pre_round_guard_recovery_command_shell_escapes_metacharacters(self, tmp_path: Path):
        """R6-F2 sibling: the pre-round guard's recovery command (exit 64 when a
        prior manual_extraction_pending is unresolved) must shell-escape the
        review-dir path too — same emit contract as the exit-11 block, sharing
        one escaped value so the two sites cannot drift (the drift that produced
        R5-F1/R6-F1)."""
        review_dir = tmp_path / "guard $(id) dir"
        review_dir.mkdir()
        seed_state = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-30T00:00:00+00:00",
            "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": 0,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-30T00:00:00+00:00",
                "manual_extraction_pending": True,
            }],
        }
        (review_dir / "state.json").write_text(json.dumps(seed_state))
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="LEDGER_PATCH:\n  findings: []\n  verdict: go\n",
        )
        prompt = tmp_path / "prompt.txt"; prompt.write_text("review")
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            ["bash", str(WRAPPER),
             "--round", "2", "--review-dir", str(review_dir),
             "--artifact", "test", "--depth", "standard",
             "--reviewer", "codex/x", "--prompt-file", str(prompt),
             "--project-root", str(tmp_path)],
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 64, (
            f"pre-round guard must exit 64; got {result.returncode}; "
            f"stderr={result.stderr!r}"
        )
        abs_path = str(review_dir.resolve())
        expected_q = self._bash_pctq(abs_path)
        assert f"--review-dir {expected_q}" in result.stderr, (
            f"pre-round guard recovery command must shell-escape the review-dir "
            f"path; expected --review-dir {expected_q}; got stderr={result.stderr!r}"
        )

    def test_exit_11_recovery_command_is_fully_qualified(self, tmp_path: Path):
        """R7-F1 (dogfood round 7): the exit-11 recovery command must invoke
        update-state.py via `python3 <scripts-dir>/update-state.py`, not a bare
        `update-state.py` — the script is not on PATH, so SKILL.md's "copy the
        command verbatim" only works if the wrapper emits the runnable form. The
        exit-64 pre-round guard already does this; the exit-11 block must match."""
        import re
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=self._NO_LEDGER_VERDICT,
        )
        result = _run_wrapper(tmp_path, stub_dir)
        assert result.returncode == 11, (
            f"expected exit 11; got {result.returncode}; stderr={result.stderr!r}"
        )
        assert re.search(r"python3 \S+/update-state\.py --review-dir", result.stderr), (
            f"exit-11 recovery command must be fully qualified "
            f"(python3 <dir>/update-state.py --review-dir ...); "
            f"got stderr={result.stderr!r}"
        )

    def test_corrupt_pending_jq_diagnostic_shell_escapes_state_file(self, tmp_path: Path):
        """R7-F2 (dogfood round 7): the corrupt-pending branch (exit 70 when a
        manual_extraction_pending entry has no integer round key) prints a
        `jq '.history' <state.json>` diagnostic the operator runs. That path must
        be shell-escaped too — same class as R6-F2, on the corrupt-state branch.
        A review dir with spaces or metacharacters would otherwise split or inject
        when the diagnostic command is pasted. Empirically reproduced: an
        unescaped `jq '.history' /.../rev dir/state.json` splits on the space."""
        review_dir = tmp_path / "corrupt $(id) dir"
        review_dir.mkdir()
        # History entry with manual_extraction_pending=true but NO 'round' key →
        # wrapper's extractor prints CORRUPT_NO_ROUND_KEY → exit 70 branch.
        seed_state = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-30T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "verdict": "UNKNOWN", "findings": 0, "fixed": 0, "fp": 0,
                "timestamp": "2026-05-30T00:00:00+00:00",
                "manual_extraction_pending": True,  # no "round" key → corrupt
            }],
        }
        (review_dir / "state.json").write_text(json.dumps(seed_state))
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="LEDGER_PATCH:\n  findings: []\n  verdict: go\n",
        )
        prompt = tmp_path / "prompt.txt"; prompt.write_text("review")
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            ["bash", str(WRAPPER),
             "--round", "2", "--review-dir", str(review_dir),
             "--artifact", "test", "--depth", "standard",
             "--reviewer", "codex/x", "--prompt-file", str(prompt),
             "--project-root", str(tmp_path)],
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, (
            f"corrupt-pending entry must exit 70; got {result.returncode}; "
            f"stderr={result.stderr!r}"
        )
        state_file = str((review_dir / "state.json").resolve())
        expected_q = self._bash_pctq(state_file)
        assert f"jq '.history' {expected_q}" in result.stderr, (
            f"corrupt-state jq diagnostic must shell-escape the state.json path; "
            f"expected jq '.history' {expected_q}; got stderr={result.stderr!r}"
        )

    def test_exit_11_recovery_command_shell_escapes_script_dir(self, tmp_path: Path):
        """R8-F1 (dogfood round 8): the recovery command's update-state.py path
        (python3 <SCRIPT_DIR>/update-state.py) must itself be shell-escaped.
        SCRIPT_DIR resolves to the wrapper's own scripts dir, which on macOS can
        live under a spaces/metachar plugin-cache path. The --review-dir value
        was escaped (R6-F2) but the script path was left bare — a spaces path in
        SCRIPT_DIR would split (or `$(...)` inject) on copy-paste.

        Reproduce by copying the three scripts into a spaces-named dir and
        invoking the wrapper from there so its BASH_SOURCE-derived SCRIPT_DIR
        contains a space."""
        import re
        import shutil
        scripts_src = WRAPPER.parent
        scripts_dst = tmp_path / "scripts dir with spaces"
        scripts_dst.mkdir()
        for name in ("bulldozer-round.sh", "log-round.sh",
                     "update-state.py", "parse-ledger-patch.py",
                     # B3 (#110): wrapper pre-validates these two exist via its
                     # SCRIPT_DIR fallback (no CLAUDE_PLUGIN_ROOT here) — copy
                     # them too or the guard exits 70 before the exit-11 path.
                     "render-trajectory.py", "emit-pivot.py",
                     # B1 (#110): wrapper reads read-depth-config.py + the
                     # data/depth-config.json sibling via SCRIPT_DIR fallback.
                     "read-depth-config.py"):
            shutil.copy2(scripts_src / name, scripts_dst / name)
        _data = scripts_dst.parent / "data"
        _data.mkdir(exist_ok=True)
        shutil.copy2(scripts_src.parent / "data" / "depth-config.json",
                     _data / "depth-config.json")
        wrapper_copy = scripts_dst / "bulldozer-round.sh"
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, verdict_body=self._NO_LEDGER_VERDICT,
        )
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        prompt = tmp_path / "prompt.txt"; prompt.write_text("review")
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        result = subprocess.run(
            ["bash", str(wrapper_copy),
             "--round", "1", "--review-dir", str(review_dir),
             "--artifact", "test", "--depth", "standard",
             "--reviewer", "codex/x", "--prompt-file", str(prompt),
             "--project-root", str(tmp_path)],
            env=env, cwd=str(tmp_path),
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 11, (
            f"expected exit 11; got {result.returncode}; stderr={result.stderr!r}"
        )
        expected_q = self._bash_pctq(str(scripts_dst / "update-state.py"))
        assert f"python3 {expected_q}" in result.stderr, (
            f"exit-11 recovery command must shell-escape the update-state.py "
            f"path (SCRIPT_DIR may contain spaces); expected python3 {expected_q}; "
            f"got stderr={result.stderr!r}"
        )
        # The raw unescaped spaces path must NOT appear as a bare token.
        raw = str(scripts_dst / "update-state.py")
        assert f"python3 {raw} " not in result.stderr, (
            "raw unescaped script path must not appear in recovery command"
        )


class TestRoundPreflight:
    """A3 (#110): --round must be a positive integer (1-based, per the usage
    block). A non-numeric value previously reached update-state.py →
    ValueError → wrapper exit 70 with a misleading 'log-round.sh failed'
    diagnostic; '0' breaks verdict-r0 filenames and the
    (( ROUND >= max_rounds )) pivot guard. Now rejected at preflight with
    exit 64 (EX_USAGE), consistent with the other usage-error checks."""

    def test_non_numeric_round_exits_64(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, round_num="abc")
        assert result.returncode == 64, (
            f"non-numeric --round must exit 64 (EX_USAGE), got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    def test_zero_round_exits_64(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, round_num=0)
        assert result.returncode == 64, (
            f"--round 0 must exit 64 (1-based; verdict-r0 filenames and the "
            f"pivot guard don't handle it), got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_negative_round_exits_64(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, round_num=-1)
        assert result.returncode == 64, (
            f"negative --round must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )

    def test_valid_round_one_not_rejected(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, round_num=1)
        assert result.returncode != 64, (
            f"valid --round 1 must NOT be rejected as a usage error, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    def test_valid_large_round_not_rejected(self, tmp_path: Path):
        # 999 >= max_rounds, but verdict is GO so no pivot fires; the point
        # is the numeric preflight does NOT reject a well-formed integer.
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, round_num=999)
        assert result.returncode != 64, (
            f"valid --round 999 must NOT be rejected as a usage error, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )


class TestReviewerPreflight:
    """A2 (#110): --reviewer must be exactly 'provider/model' — one slash,
    both segments non-empty. The old `#*/` + (MODEL==REVIEWER || -z MODEL)
    test admitted multi-slash (codex/openrouter/gpt-5.1 →
    MODEL=openrouter/gpt-5.1, silently routed) and leading-slash (/gpt →
    MODEL=gpt). The ^[^/]+/[^/]+$ regex rejects all four bad shapes."""

    def test_valid_reviewer_not_rejected(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, reviewer="codex/gpt")
        assert result.returncode != 64, (
            f"valid 'codex/gpt' must NOT be rejected, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    def test_multi_segment_model_accepted_and_extracted(self, tmp_path: Path):
        """R1-F3 (dogfood): a provider/model with extra slashes is a routed
        model slug (e.g. codex/openai/gpt-4o -> MODEL=openai/gpt-4o), not an
        error. ^[^/]+/.+$ accepts it; ${REVIEWER#*/} extracts the full model.
        (No real codex slug contains a slash today, but the wrapper should not
        wrongly reject a legitimate multi-segment model id.)"""
        args_dump = tmp_path / "codex_args.txt"
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["CODEX_STUB_ARGS_FILE"] = str(args_dump)
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        review_dir = tmp_path / "review"; review_dir.mkdir()
        prompt_file = tmp_path / "prompt.txt"; prompt_file.write_text("review")
        result = subprocess.run(
            ["bash", str(WRAPPER), "--round", "1", "--review-dir", str(review_dir),
             "--artifact", "x", "--depth", "standard",
             "--reviewer", "codex/openai/gpt-4o",
             "--prompt-file", str(prompt_file), "--project-root", str(tmp_path)],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 64, (
            f"multi-segment model (codex/openai/gpt-4o) must NOT be rejected as "
            f"a usage error, got {result.returncode}.\nstderr: {result.stderr}"
        )
        argv = args_dump.read_text().splitlines()
        for i, a in enumerate(argv):
            if a == "-m" and i + 1 < len(argv):
                assert argv[i + 1] == "openai/gpt-4o", (
                    f"expected '-m openai/gpt-4o', got '-m {argv[i + 1]}'"
                )
                break
        else:
            pytest.fail(f"'-m' not found in codex argv: {argv}")

    def test_leading_slash_reviewer_exits_64(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, reviewer="/gpt")
        assert result.returncode == 64, (
            f"leading-slash reviewer (empty provider) must exit 64, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    def test_trailing_slash_reviewer_exits_64(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, reviewer="codex/")
        assert result.returncode == 64, (
            f"trailing-slash reviewer (empty model) must exit 64, got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )

    def test_no_slash_reviewer_exits_64(self, tmp_path: Path):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        result = _run_wrapper(tmp_path, stub_dir, reviewer="codex")
        assert result.returncode == 64, (
            f"no-slash reviewer must exit 64, got {result.returncode}.\n"
            f"stderr: {result.stderr}"
        )


class TestEmptyParserOutGuard:
    """A1 (#110): defensive guard. If the inline parser_out python ever
    returns 0 with empty stdout, findings_count/VERDICT would be empty and
    reach log-round with a misleading diagnostic. The guard converts that
    into a clear exit 70. Unreachable via normal PARSED_FILE content (the
    inline python always prints 'COUNT|VERDICT', and any read error exits
    non-zero → the existing parser_out_exit branch), so it is verified
    structurally — the established pattern for this suite's source-level
    defensive guards (cf. the R7/R8 recovery-command tests)."""

    def test_empty_parser_out_guard_present(self):
        import re
        src = WRAPPER.read_text()
        i = src.index("parser_out_exit != 0")
        j = src.index('findings_count="${parser_out%|*}"')
        assert i < j, "unexpected wrapper layout: split before exit check"
        region = src[i:j]
        assert re.search(r'-z\s+"\$parser_out".*?_emit_stop\s+70',
                         region, re.DOTALL), (
            "A1 guard missing: expected `[[ -z \"$parser_out\" ]]` → "
            "_emit_stop 70 between the parser_out_exit check and the "
            "findings_count split (empty parser output must exit 70, not "
            "flow to log-round with a misleading 'log-round.sh failed')"
        )


class TestPromptViaStdin:
    """A4 (#110): the prompt reaches codex via STDIN (codex exec '-'), not as
    a positional argv. Positional risked E2BIG/ARG_MAX (Linux 128KB) on large
    round-N prompts (ledger + full previous verdict), and $(<file) stripped
    trailing newlines. stdin delivery preserves the bytes verbatim."""

    def _run(self, tmp_path: Path, *, prompt_body: str,
             stdin_dump: Path | None = None, args_dump: Path | None = None,
             depth: str = "standard"):
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0,
                                       verdict_body=VALID_LEDGER_GO)
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        if stdin_dump is not None:
            env["CODEX_STUB_STDIN_FILE"] = str(stdin_dump)
        if args_dump is not None:
            env["CODEX_STUB_ARGS_FILE"] = str(args_dump)
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text(prompt_body)
        return subprocess.run(
            [
                "bash", str(WRAPPER),
                "--round", "1",
                "--review-dir", str(review_dir),
                "--artifact", "x",
                "--depth", depth,
                "--reviewer", "codex/test",
                "--prompt-file", str(prompt_file),
                "--project-root", str(tmp_path),
            ],
            env=env, capture_output=True, text=True, timeout=10,
        )

    def test_prompt_delivered_via_stdin_preserves_trailing_newlines(self, tmp_path: Path):
        stdin_dump = tmp_path / "codex_stdin.txt"
        body = "review this artifact\n\n\n"  # trailing newlines must survive
        self._run(tmp_path, prompt_body=body, stdin_dump=stdin_dump)
        assert stdin_dump.exists(), (
            "codex received no stdin — wrapper still passes the prompt as argv"
        )
        assert stdin_dump.read_text() == body, (
            f"prompt must reach codex via stdin verbatim with trailing "
            f"newlines preserved; got {stdin_dump.read_text()!r}, "
            f"want {body!r}"
        )

    def test_prompt_not_passed_as_argv_positional(self, tmp_path: Path):
        args_dump = tmp_path / "codex_args.txt"
        sentinel = "UNIQUE_PROMPT_SENTINEL_BODY"
        self._run(tmp_path, prompt_body=sentinel + "\n", args_dump=args_dump)
        argv = args_dump.read_text()
        assert sentinel not in argv, (
            f"prompt body must NOT appear in codex argv (A4 moved it to "
            f"stdin); argv:\n{argv}"
        )
        assert any(line == "-" for line in argv.splitlines()), (
            f"codex must receive a '-' positional to read the prompt from "
            f"stdin; argv:\n{argv}"
        )

    def test_large_prompt_no_spurious_crash_when_codex_skips_stdin(self, tmp_path: Path):
        """R1-F1 (dogfood): with a pipe + `set -o pipefail`, codex exiting 0
        without draining a large prompt gives `cat` SIGPIPE → the pipeline
        goes non-zero → a successful review is mislabeled as a codex crash
        (exit 71). Feeding stdin via a file redirect removes the pipe, so a
        clean codex exit stays clean. Uses a custom non-draining stub (the
        shared _install_codex_stub drains stdin and would mask this)."""
        stub_dir = tmp_path / "bin"
        stub_dir.mkdir()
        verdict_src = stub_dir / "verdict_body.txt"
        verdict_src.write_text(VALID_LEDGER_GO)
        stub = stub_dir / "codex"
        # Writes the verdict and exits 0 WITHOUT reading stdin (simulates
        # codex closing its stdin read end early on a large prompt).
        stub.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            args=("$@")
            vp=""
            for ((i=0; i<${{#args[@]}}; i++)); do
                if [[ "${{args[$i]}}" == "-o" ]]; then vp="${{args[$((i+1))]}}"; break; fi
            done
            if [[ -n "$vp" ]]; then
                mkdir -p "$(dirname "$vp")"
                cp {str(verdict_src)!r} "$vp"
            fi
            exit 0
        """))
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        prompt_file = tmp_path / "prompt.txt"
        # > 64KB pipe buffer: on a pipe, `cat` would hit SIGPIPE once the
        # reader exits without draining.
        prompt_file.write_text("X" * 200_000)
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["BULLDOZER_REVIEW_DIR"] = str(review_dir)
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
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
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, (
            f"large prompt + codex-exits-without-draining must NOT spuriously "
            f"crash (pipe + pipefail SIGPIPE → exit 71 regression); got "
            f"{result.returncode}.\nstderr: {result.stderr}"
        )


class TestRenderTrajectoryScript:
    """B3 (#110): trajectory render extracted to a standalone script.

    The wrapper calls render-trajectory.py as a subprocess; these tests
    invoke it directly so the render logic is unit-testable. Output must
    stay byte-identical to the old inline heredoc (TestTrajectoryDisplay
    asserts the black-box-through-wrapper path).
    """

    def _state(self, tmp_path, history):
        sp = tmp_path / "state.json"
        sp.write_text(json.dumps({"history": history}))
        return sp

    def test_renders_round_line_and_trajectory(self, tmp_path: Path):
        sp = self._state(tmp_path, [
            {"round": 1, "verdict": "NO-GO", "findings": 4},
            {"round": 2, "verdict": "NO-GO", "findings": 2},
        ])
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(sp)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0, r.stderr
        assert "Round 2/3" in r.stdout
        assert "verdict: NO-GO" in r.stdout
        assert "2 findings open" in r.stdout
        assert "Trajectory: 4 → 2" in r.stdout
        assert "avg last 3: 3.0" in r.stdout

    def test_singular_finding_noun(self, tmp_path: Path):
        sp = self._state(tmp_path, [
            {"round": 1, "verdict": "NO-GO", "findings": 2},
            {"round": 2, "verdict": "NO-GO", "findings": 1},
        ])
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(sp)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0, r.stderr
        assert "1 finding open" in r.stdout

    def test_corrupt_state_exits_nonzero(self, tmp_path: Path):
        sp = tmp_path / "state.json"
        sp.write_text("{not json")
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(sp)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode != 0  # wrapper maps this to _emit_stop 70

    def test_bad_arity_exits_nonzero(self, tmp_path: Path):
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2"],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode != 0


class TestRenderTrajectoryAvgMeets:
    """#133 F1: render-trajectory.py is the single source of the avg-last-3
    metric. The B6 calibrated-pivot gate in bulldozer-round.sh used to recompute
    `mean(trajectory[-3:])` in its own inline `python3 -c`; it now delegates to
    `render-trajectory.py --avg-meets <state_json> <threshold>`, which prints
    "1" if the mean of the last 3 rounds' findings >= threshold else "0".

    Behavioural contract (must match the old inline recompute exactly):
      - boundary is `>=` (avg == threshold → "1")
      - only the last 3 rounds count (windowing), oldest dropped
      - unreadable/corrupt state → "0", exit 0 (graceful — the pivot gate runs
        after the display path already validated state this round)
    """

    def _state(self, tmp_path, history):
        sp = tmp_path / "state.json"
        sp.write_text(json.dumps({"history": history}))
        return sp

    def _avg_meets(self, state_path, threshold="3.0"):
        return subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "--avg-meets",
             str(state_path), threshold],
            capture_output=True, text=True, timeout=10,
        )

    def test_emits_one_when_avg_at_threshold(self, tmp_path: Path):
        # (4 + 4 + 1) / 3 == 3.0; boundary is `>=` → "1".
        sp = self._state(tmp_path, [
            {"round": 1, "findings": 4},
            {"round": 2, "findings": 4},
            {"round": 3, "findings": 1},
        ])
        r = self._avg_meets(sp)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "1", f"avg 3.0 >= 3.0 must emit 1; got {r.stdout!r}"

    def test_emits_zero_when_avg_below_threshold(self, tmp_path: Path):
        sp = self._state(tmp_path, [
            {"round": 1, "findings": 1},
            {"round": 2, "findings": 1},
            {"round": 3, "findings": 1},
        ])
        r = self._avg_meets(sp)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "0", f"avg 1.0 < 3.0 must emit 0; got {r.stdout!r}"

    def test_windowing_drops_old_rounds_below(self, tmp_path: Path):
        # 4 rounds: last-3 = [0,0,0] → avg 0.0 → "0". A whole-history mean would
        # be 12/4 = 3.0 → "1": this asserts the window is the last 3, not all.
        sp = self._state(tmp_path, [
            {"round": 1, "findings": 12},
            {"round": 2, "findings": 0},
            {"round": 3, "findings": 0},
            {"round": 4, "findings": 0},
        ])
        r = self._avg_meets(sp)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "0", (
            f"windowing must use last 3 (avg 0.0 → 0), not all rounds "
            f"(would be 3.0 → 1); got {r.stdout!r}"
        )

    def test_windowing_drops_old_rounds_above(self, tmp_path: Path):
        # 6 rounds: last-3 = [0,0,9] → avg 3.0 → "1". Whole-history mean would
        # be 9/6 = 1.5 → "0": asserts the window again, opposite direction.
        sp = self._state(tmp_path, [
            {"round": 1, "findings": 0},
            {"round": 2, "findings": 0},
            {"round": 3, "findings": 0},
            {"round": 4, "findings": 0},
            {"round": 5, "findings": 0},
            {"round": 6, "findings": 9},
        ])
        r = self._avg_meets(sp)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "1", (
            f"windowing must use last 3 (avg 3.0 → 1), not all rounds "
            f"(would be 1.5 → 0); got {r.stdout!r}"
        )

    def test_empty_history_emits_zero(self, tmp_path: Path):
        sp = self._state(tmp_path, [])
        r = self._avg_meets(sp)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "0", f"empty history → avg 0 → 0; got {r.stdout!r}"

    def test_corrupt_state_emits_zero_exit_zero(self, tmp_path: Path):
        # Graceful: matches the old inline gate's `except (OSError,
        # json.JSONDecodeError): print(0); sys.exit(0)`.
        sp = tmp_path / "state.json"
        sp.write_text("{not json")
        r = self._avg_meets(sp)
        assert r.returncode == 0, (
            f"corrupt state must degrade gracefully (exit 0), not crash; "
            f"rc={r.returncode} stderr={r.stderr!r}"
        )
        assert r.stdout.strip() == "0", f"corrupt state → 0; got {r.stdout!r}"

    def test_missing_file_emits_zero_exit_zero(self, tmp_path: Path):
        r = self._avg_meets(tmp_path / "does-not-exist.json")
        assert r.returncode == 0, (
            f"missing state must degrade gracefully (exit 0); "
            f"rc={r.returncode} stderr={r.stderr!r}"
        )
        assert r.stdout.strip() == "0", f"missing file → 0; got {r.stdout!r}"

    def test_bad_arity_exits_two_with_mode_usage(self, tmp_path: Path):
        # `--avg-meets` without a threshold → exit 2 with a mode-specific usage
        # message (distinguishes the new mode from the default usage line).
        sp = self._state(tmp_path, [{"round": 1, "findings": 1}])
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "--avg-meets", str(sp)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 2, f"bad --avg-meets arity → exit 2; got {r.returncode}"
        assert "--avg-meets" in r.stderr, (
            f"usage message must name the --avg-meets mode; got {r.stderr!r}"
        )

    def test_wrapper_pivot_single_sources_the_metric(self):
        # Drift guard (#133 F1 core): the wrapper's pivot gate must delegate to
        # render-trajectory.py --avg-meets, NOT carry a second inline recompute
        # of the trajectory mean. Pins the single-source invariant against
        # regression (the whole point of the issue).
        src = WRAPPER.read_text()
        assert "--avg-meets" in src, (
            "wrapper pivot gate must call render-trajectory.py --avg-meets"
        )
        assert 'for h in json.load' not in src, (
            "wrapper must not recompute the trajectory inline — the metric is "
            "single-sourced in render-trajectory.py (#133 F1)"
        )


class TestWrapperVerdictFailsafe:
    """B8 (#110): wrapper reads the parser's canonical TOP-LEVEL verdict.

    Post-B8 the parser emits a top-level `verdict` ("go"/"no_go") on every
    exit-0 parse, so the wrapper drops its findings-based fallback inference
    and reads it directly. A missing verdict key records NO-GO (fail-safe —
    never a false GO). The decision is read from top level, NOT meta (meta
    keeps the reviewer's raw token per Issue #100 case #7).
    """

    def _run_with_parser_emitting(self, tmp_path: Path, parsed_json: str):
        import shutil
        plugin_root = tmp_path / "plugin"
        scripts = plugin_root / "skills" / "check" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "parse-ledger-patch.py").write_text(
            "import sys\n"
            f"sys.stdout.write({parsed_json!r})\n"
        )
        real = PLUGIN_ROOT / "skills" / "check" / "scripts"
        # B3 (#110): the wrapper pre-validates render-trajectory.py +
        # emit-pivot.py exist under CLAUDE_PLUGIN_ROOT (exit 70 if missing),
        # so the fake plugin root must carry them too. B1 (#110) extended that
        # contract with read-depth-config.py + data/depth-config.json.
        for name in ("log-round.sh", "update-state.py",
                     "render-trajectory.py", "emit-pivot.py",
                     "read-depth-config.py"):
            shutil.copy(real / name, scripts / name)
        (scripts / "log-round.sh").chmod(0o755)
        data = plugin_root / "skills" / "check" / "data"
        data.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            PLUGIN_ROOT / "skills" / "check" / "data" / "depth-config.json",
            data / "depth-config.json",
        )

        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="LEDGER_PATCH:\n  findings: []\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("review")
        return subprocess.run(
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

    def test_missing_verdict_records_no_go_failsafe(self, tmp_path: Path):
        # No top-level verdict and empty findings → fail-safe NO-GO (the
        # wrapper no longer infers GO from empty findings).
        result = self._run_with_parser_emitting(
            tmp_path, '{"findings": [], "meta": {}}')
        assert result.returncode == 0, result.stderr
        state = json.loads((tmp_path / "review" / "state.json").read_text())
        assert state["history"][-1]["verdict"].upper() == "NO-GO", (
            f"empty findings + no top-level verdict must record NO-GO "
            f"(fail-safe); got {state['history']}"
        )

    def test_top_level_verdict_go_records_go(self, tmp_path: Path):
        result = self._run_with_parser_emitting(
            tmp_path, '{"findings": [], "verdict": "go", "meta": {}}')
        assert result.returncode == 0, result.stderr
        state = json.loads((tmp_path / "review" / "state.json").read_text())
        assert state["history"][-1]["verdict"].upper() == "GO", (
            f"top-level verdict=go must record GO; got {state['history']}"
        )

    def test_top_level_verdict_beats_raw_meta_token(self, tmp_path: Path):
        # meta carries a raw uppercase "GO" token but the canonical top-level
        # verdict says no_go — the wrapper must trust the canonical field.
        result = self._run_with_parser_emitting(
            tmp_path,
            '{"findings": [], "verdict": "no_go", "meta": {"verdict": "GO"}}')
        assert result.returncode == 0, result.stderr
        state = json.loads((tmp_path / "review" / "state.json").read_text())
        assert state["history"][-1]["verdict"].upper() == "NO-GO", (
            f"canonical top-level verdict must win over raw meta token; "
            f"got {state['history']}"
        )

    def test_bare_go_synthesis_records_go_through_real_parser(self, tmp_path: Path):
        # B8 regression (#110 PR-3a dogfood R1-F1→R3-F1), end-to-end: when the
        # reviewer emits a bare "GO" line with no LEDGER_PATCH block, the REAL
        # parser synthesizes the payload in main(). Before the fix that payload
        # carried verdict only in meta, so the wrapper (reading top-level
        # data.get("verdict"), missing → NO-GO) logged a bare-GO reply as
        # NO-GO. This drives codex → real parser → log-round with no stub
        # parser, so it catches the bug the meta-only unit assertion missed.
        import shutil
        plugin_root = tmp_path / "plugin"
        scripts = plugin_root / "skills" / "check" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        real = PLUGIN_ROOT / "skills" / "check" / "scripts"
        for name in ("parse-ledger-patch.py", "log-round.sh", "update-state.py",
                     "render-trajectory.py", "emit-pivot.py",
                     "read-depth-config.py"):
            shutil.copy(real / name, scripts / name)
        (scripts / "log-round.sh").chmod(0o755)
        # B1 (#110): wrapper reads data/depth-config.json from the plugin root.
        _data = plugin_root / "skills" / "check" / "data"
        _data.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            PLUGIN_ROOT / "skills" / "check" / "data" / "depth-config.json",
            _data / "depth-config.json")

        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="Verdict body prose here.\n\nGO\n",
        )
        env = os.environ.copy()
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        env["BULLDOZER_REVIEW_DIR"] = str(tmp_path / "review")
        env["BULLDOZER_LOG"] = str(tmp_path / "bulldozer.log")
        review_dir = tmp_path / "review"
        review_dir.mkdir()
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("review")
        result = subprocess.run(
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
        assert result.returncode == 0, result.stderr
        parsed = json.loads((review_dir / "parsed-r1.json").read_text())
        assert parsed.get("source") == "synthesized_bare_go", (
            f"expected bare-GO synthesis path; got source={parsed.get('source')!r}"
        )
        assert parsed.get("verdict") == "go", (
            f"synthesized bare-GO must carry top-level verdict=go; "
            f"got {parsed.get('verdict')!r}"
        )
        state = json.loads((review_dir / "state.json").read_text())
        assert state["history"][-1]["verdict"].upper() == "GO", (
            f"bare-GO reviewer reply must be logged as GO, not NO-GO; "
            f"got {state['history']}"
        )


class TestB3Characterization:
    """B3 (#110) R2-F1: lock the success-path byte-identical contract.

    After PR-3a narrowed the extracted scripts' contract to "success-path
    stdout/JSON byte-identical" (failure stderr explicitly excluded), no test
    pinned the exact success bytes — TestTrajectoryDisplay / TestAskUserPivot
    assert shape, not byte equality. A future edit could silently change the
    wrapper-relied-on output while the suite stays green.

    These tests compare live script output against committed golden fixtures
    (tests/fixtures/check/b3-char-*.golden) that were generated by the scripts
    themselves at extraction time and verified byte-identical to the original
    heredocs. Any byte change to render-trajectory.py / emit-pivot.py success
    output now fails loudly.
    """

    def test_render_trajectory_success_bytes_match_golden(self):
        golden = (FIXTURES / "b3-char-trajectory.golden").read_bytes()
        state = FIXTURES / "b3-char-state.json"
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(state)],
            capture_output=True, timeout=10,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout == golden, (
            "render-trajectory.py success stdout drifted from the committed "
            "golden — the B3 success-path byte-identical contract is broken."
        )

    def test_emit_pivot_success_bytes_match_golden(self, tmp_path: Path):
        golden = (FIXTURES / "b3-char-pivot.golden").read_bytes()
        out = tmp_path / "pivot.json"
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3", "5", "standard",
             "src/x.py", str(out)],
            capture_output=True, timeout=10,
        )
        assert r.returncode == 0, r.stderr
        assert out.read_bytes() == golden, (
            "emit-pivot.py success JSON drifted from the committed golden — "
            "the B3 success-path byte-identical contract is broken."
        )


class TestEmitPivotScript:
    """B3 (#110): pivot JSON emit extracted to a standalone script.

    The wrapper calls emit-pivot.py as a subprocess; these tests invoke it
    directly so the pivot JSON shape is unit-testable. The black-box
    TestAskUserPivot tests assert the through-wrapper path stays identical.
    """

    def test_writes_pivot_json_with_expected_shape(self, tmp_path: Path):
        pivot = tmp_path / "pivot-r3.json"
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3", "5", "standard",
             "src/x.py", str(pivot)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode == 0, r.stderr
        data = json.loads(pivot.read_text())
        assert data["trigger"] == "max_rounds_reached"
        assert data["round"] == 3
        assert data["max_rounds"] == 3
        assert data["open_findings"] == 5
        assert data["depth"] == "standard"
        assert data["artifact"] == "src/x.py"
        assert data["header"] == "Pivot"
        assert data["multiSelect"] is False
        labels = [o["label"] for o in data["options"]]
        assert labels == ["continue", "restructure", "accept-with-TODO"]

    def test_bad_arity_exits_nonzero(self, tmp_path: Path):
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3"],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode != 0

    def test_unwritable_pivot_path_exits_nonzero(self, tmp_path: Path):
        bad = tmp_path / "nope" / "pivot.json"  # parent dir missing
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3", "5", "standard",
             "a", str(bad)],
            capture_output=True, text=True, timeout=10,
        )
        assert r.returncode != 0


class TestReadDepthConfigScript:
    """B1 (#110): depth parameters externalized to data/depth-config.json.

    read-depth-config.py is the single reader the wrapper calls to derive
    max_rounds / reasoning / ephemeral / prompt_prefix for a depth. Output is
    TAB-delimited with prompt_prefix LAST so its significant trailing space
    survives `IFS=$'\\t' read` in the wrapper. Exit 2 = unknown depth (wrapper
    maps to usage error 64); exit 3 = corrupt/unreadable config (wrapper 70).
    """

    def _run(self, depth, config=None):
        cfg = str(config) if config is not None else str(DEPTH_CONFIG)
        return subprocess.run(
            [sys.executable, str(READ_DEPTH_CONFIG), cfg, depth],
            capture_output=True, text=True, timeout=10,
        )

    def test_quick_preserves_trailing_space_prefix(self):
        r = self._run("quick")
        assert r.returncode == 0, r.stderr
        # prompt_prefix "SKIP SKILLS. " keeps its trailing space (significant —
        # it separates the prefix from the prompt body the wrapper concatenates).
        assert r.stdout == "1\tmedium\ttrue\tSKIP SKILLS. \n"

    def test_standard_empty_prefix(self):
        r = self._run("standard")
        assert r.returncode == 0, r.stderr
        assert r.stdout == "3\txhigh\tfalse\t\n"

    def test_exhaustive_max_rounds_ten(self):
        r = self._run("exhaustive")
        assert r.returncode == 0, r.stderr
        assert r.stdout == "10\txhigh\tfalse\t\n"

    def test_unknown_depth_exits_2(self):
        r = self._run("bogus")
        assert r.returncode == 2

    def test_corrupt_config_exits_3(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        r = self._run("standard", config=bad)
        assert r.returncode == 3

    def test_missing_config_exits_nonzero_not_unknown_depth(self, tmp_path: Path):
        r = self._run("standard", config=tmp_path / "absent.json")
        assert r.returncode != 0
        assert r.returncode != 2  # IO/config error, NOT "unknown depth" (2)

    def test_string_ephemeral_fails_closed_not_silent_true(self, tmp_path: Path):
        # R1-F1 (PR-3b dogfood): bool("false") is True in Python, so the old
        # str(bool(...)) path silently flipped a STRING "false" to ephemeral=true
        # — the wrapper would add --ephemeral for standard depth with no error
        # (silent wrong-value behavior change). A non-bool ephemeral must fail
        # closed (exit 3), not coerce.
        cfg = tmp_path / "stringbool.json"
        cfg.write_text('{"standard":{"max_rounds":3,"reasoning":"xhigh",'
                       '"ephemeral":"false","prompt_prefix":""}}')
        r = self._run("standard", config=cfg)
        assert r.returncode == 3, (
            f"string ephemeral must fail closed (exit 3), got rc={r.returncode} "
            f"stdout={r.stdout!r}"
        )

    def test_bool_max_rounds_fails_closed(self, tmp_path: Path):
        # R1-F1 sibling: int(True) is 1 in Python — a bool max_rounds must not
        # silently coerce to 1. Strict-type validation fails it closed.
        cfg = tmp_path / "boolmax.json"
        cfg.write_text('{"standard":{"max_rounds":true,"reasoning":"xhigh",'
                       '"ephemeral":false,"prompt_prefix":""}}')
        r = self._run("standard", config=cfg)
        assert r.returncode == 3, (
            f"bool max_rounds must fail closed (exit 3), got rc={r.returncode} "
            f"stdout={r.stdout!r}"
        )

    def test_valid_bool_ephemeral_still_works(self, tmp_path: Path):
        # Regression guard: a proper JSON bool ephemeral must still pass (the
        # strict fix must not reject the shipped config shape). Uses the full
        # 3-depth config — R3-F1(b) now requires all built-in depths present, so
        # a quick-only fixture would (correctly) fail the completeness check.
        cfg = tmp_path / "ok.json"
        cfg.write_text(self._FULL_CONFIG)
        r = self._run("quick", config=cfg)
        assert r.returncode == 0, r.stderr
        assert r.stdout == "1\tmedium\ttrue\tSKIP SKILLS. \n"

    # --- R1-F1 completion (PR-3b dogfood R2): the strict isinstance fix landed
    # but left content-validation gaps the reviewer caught — all empirically
    # reproduced before this fix.

    def test_non_utf8_config_fails_closed_not_traceback(self, tmp_path: Path):
        # UnicodeDecodeError is a ValueError subclass, NOT OSError/JSONDecodeError
        # — a non-UTF8 config raised uncaught (rc 1 traceback) instead of the
        # documented exit-3 corruption contract. Same class as the emit-pivot
        # R1-F2 fix, missed here in R1.
        bad = tmp_path / "nonutf8.json"
        bad.write_bytes(b'{"standard":{"max_rounds":3,"reasoning":"\xff\xfe",'
                        b'"ephemeral":false,"prompt_prefix":""}}')
        r = self._run("standard", config=bad)
        assert r.returncode == 3, (
            f"non-UTF8 config must fail closed (exit 3), got rc={r.returncode}"
        )

    def test_tab_in_reasoning_rejected(self, tmp_path: Path):
        # A TAB in any field value corrupts the wrapper's `IFS=$'\t' read` (the
        # output gains an extra field) — must be rejected, not emitted.
        cfg = tmp_path / "tab.json"
        cfg.write_text(json.dumps({"standard": {
            "max_rounds": 3, "reasoning": "xh\tigh",
            "ephemeral": False, "prompt_prefix": ""}}))
        r = self._run("standard", config=cfg)
        assert r.returncode == 3, (
            f"TAB in reasoning must be rejected (exit 3), got rc={r.returncode} "
            f"stdout={r.stdout!r}"
        )

    def test_tab_in_prompt_prefix_rejected(self, tmp_path: Path):
        cfg = tmp_path / "tabp.json"
        cfg.write_text(json.dumps({"standard": {
            "max_rounds": 3, "reasoning": "xhigh",
            "ephemeral": False, "prompt_prefix": "bad\tprefix"}}))
        r = self._run("standard", config=cfg)
        assert r.returncode == 3, (
            f"TAB in prompt_prefix must be rejected (exit 3), got rc={r.returncode} "
            f"stdout={r.stdout!r}"
        )

    def test_newline_in_prompt_prefix_rejected(self, tmp_path: Path):
        # A newline breaks the single-line stdout protocol the wrapper reads.
        cfg = tmp_path / "nl.json"
        cfg.write_text(json.dumps({"standard": {
            "max_rounds": 3, "reasoning": "xhigh",
            "ephemeral": False, "prompt_prefix": "l1\nl2"}}))
        r = self._run("standard", config=cfg)
        assert r.returncode == 3, (
            f"newline in prompt_prefix must be rejected (exit 3), got rc={r.returncode} "
            f"stdout={r.stdout!r}"
        )

    def test_null_depth_entry_is_schema_error_not_unknown(self, tmp_path: Path):
        # A present-but-null entry is config corruption (exit 3 → wrapper 70),
        # NOT a user typo (exit 2 → wrapper 64 usage). Distinguish
        # `depth not in config` from `config[depth] is None`.
        cfg = tmp_path / "null.json"
        cfg.write_text('{"standard": null}')
        r = self._run("standard", config=cfg)
        assert r.returncode == 3, (
            f"present-null depth entry must be schema error (exit 3), "
            f"got rc={r.returncode}"
        )

    _FULL_CONFIG = (
        '{"quick":{"max_rounds":1,"reasoning":"medium","ephemeral":true,'
        '"prompt_prefix":"SKIP SKILLS. "},'
        '"standard":{"max_rounds":3,"reasoning":"xhigh","ephemeral":false,'
        '"prompt_prefix":""},'
        '"exhaustive":{"max_rounds":10,"reasoning":"xhigh","ephemeral":false,'
        '"prompt_prefix":""}}'
    )

    def test_unknown_requested_depth_is_usage_error(self, tmp_path: Path):
        # A COMPLETE config + a requested depth outside {quick,standard,
        # exhaustive} is a user typo in --depth → exit 2 (wrapper usage 64).
        cfg = tmp_path / "full.json"
        cfg.write_text(self._FULL_CONFIG)
        r = self._run("bogus", config=cfg)
        assert r.returncode == 2, (
            f"unknown requested depth (complete config) must be usage error "
            f"(exit 2), got rc={r.returncode}"
        )

    def test_missing_required_depth_is_corruption(self, tmp_path: Path):
        # R3-F1(b) (PR-3b dogfood R3): the shipped depth-config.json always
        # carries all three built-in depths; a config missing one is corrupt/
        # truncated (exit 3 → wrapper 70), NOT a user typo. Completeness is
        # checked before lookup so even requesting a present depth fails closed
        # when siblings are missing.
        cfg = tmp_path / "partial.json"
        cfg.write_text('{"quick":{"max_rounds":1,"reasoning":"medium",'
                       '"ephemeral":true,"prompt_prefix":"SKIP SKILLS. "}}')
        r = self._run("quick", config=cfg)  # present depth, missing siblings
        assert r.returncode == 3, (
            f"config missing required depths must be corruption (exit 3), "
            f"got rc={r.returncode}"
        )


class TestDepthConfigContract:
    """B1 (#110): data/depth-config.json is the single source of truth; the
    SKILL.md 'Depth Levels' table must mirror it. Guards drift between the
    wrapper's runtime config and the human-facing doc table.
    """

    def _skill_table_rows(self):
        import re
        skill = (PLUGIN_ROOT / "skills" / "check" / "SKILL.md").read_text()
        rows = {}
        for depth in ("quick", "standard", "exhaustive"):
            m = re.search(rf"^\|\s*`{depth}`\s*\|(.+)$", skill, re.MULTILINE)
            assert m, f"depth row for {depth} not found in SKILL.md table"
            # cells: [max_rounds, codex-config, prompt-prefix, when]
            rows[depth] = [c.strip() for c in m.group(1).split("|")]
        return rows

    def test_table_matches_depth_config_json(self):
        import re
        cfg = json.loads(DEPTH_CONFIG.read_text())
        rows = self._skill_table_rows()
        for depth, params in cfg.items():
            cells = rows[depth]
            max_cell, codex_cell, prefix_cell = cells[0], cells[1], cells[2]
            # max_rounds: last integer in the cell (handles "until GO (cap 10)")
            assert int(re.findall(r"\d+", max_cell)[-1]) == params["max_rounds"], depth
            rm = re.search(r"model_reasoning_effort=(\w+)", codex_cell)
            assert rm and rm.group(1) == params["reasoning"], depth
            assert ("--ephemeral" in codex_cell) == params["ephemeral"], depth
            # table can't render a trailing space → compare stripped;
            # "(none)" cell maps to empty prefix.
            table_prefix = "" if "(none)" in prefix_cell else prefix_cell.strip("` ")
            assert table_prefix == params["prompt_prefix"].strip(), depth


class TestParserExitContract:
    """B2 (#110): every parser exit code documented in parse-ledger-patch.py's
    'Exit codes:' block must have a corresponding wrapper handler. After B2 the
    2/3/4/5/unknown diagnostics live in `_emit_parser_exit_diagnostic`; 0
    (success) and 1 (manual extraction → exit 11) stay in the main parser
    `case`. Guards drift when a parser exit code is added without wiring the
    wrapper (the E3 'extending exit codes' contract).
    """

    def _documented_parser_exit_codes(self):
        import re
        src = (PLUGIN_ROOT / "skills" / "check" / "scripts"
               / "parse-ledger-patch.py").read_text()
        block = src.split("Exit codes:", 1)[1].split('"""', 1)[0]
        return {int(m) for m in re.findall(r"^\s{4}(\d)\s", block, re.MULTILINE)}

    def test_documented_codes_are_expected_set(self):
        assert self._documented_parser_exit_codes() == {0, 1, 2, 3, 4, 5}

    def test_every_diagnostic_code_has_wrapper_emit_stop(self):
        codes = self._documented_parser_exit_codes()
        wrapper = WRAPPER.read_text()
        # 0 + 1 are control-flow (success / manual-extraction), handled in the
        # main parser `case`; 2..5 map to `_emit_stop <code>` (via
        # _emit_parser_exit_diagnostic). A new documented code without its
        # `_emit_stop N` fails here.
        for c in sorted(codes - {0, 1}):
            assert f"_emit_stop {c} " in wrapper, \
                f"parser exit {c} documented but no wrapper _emit_stop {c}"

    def test_main_case_keeps_control_flow_codes(self):
        wrapper = WRAPPER.read_text()
        # exit 1 (no LEDGER_PATCH) maps to manual-extraction exit 11; this
        # control-flow branch must NOT fold into the diagnostic helper.
        assert "exit 11" in wrapper
        assert "_emit_parser_exit_diagnostic" in wrapper


class TestPivotOptionsLoader:
    """B4 (#110): pivot options externalized to data/pivot-options.yaml, loaded
    by emit-pivot.py with a built-in fallback. emit-pivot.py guards its CLI
    under __main__ so _load_options / _BUILTIN_OPTIONS are importable here.
    """

    def _load_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("emit_pivot_mod", EMIT_PIVOT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_reads_options_from_yaml(self, tmp_path: Path):
        mod = self._load_module()
        cfg = tmp_path / "opts.yaml"
        cfg.write_text(
            "options:\n"
            "  - label: a\n"
            "    description: desc-a\n"
            "  - label: b\n"
            "    description: desc-b\n"
        )
        opts = mod._load_options(cfg)
        assert [o["label"] for o in opts] == ["a", "b"]
        assert opts[0]["description"] == "desc-a"

    def test_fallback_when_missing(self, tmp_path: Path):
        mod = self._load_module()
        assert mod._load_options(tmp_path / "absent.yaml") == mod._BUILTIN_OPTIONS

    def test_fallback_when_corrupt(self, tmp_path: Path):
        mod = self._load_module()
        bad = tmp_path / "bad.yaml"
        bad.write_text("options: [unclosed\n")
        assert mod._load_options(bad) == mod._BUILTIN_OPTIONS

    def test_fallback_when_non_utf8(self, tmp_path: Path):
        # R1-F2 (PR-3b dogfood): UnicodeDecodeError is a ValueError subclass,
        # NOT OSError/YAMLError — a non-UTF8 data file must fall back to the
        # built-in set, not crash _load_options (which the wrapper would map to
        # _emit_stop 70, suppressing the pivot the fallback exists to preserve).
        mod = self._load_module()
        bad = tmp_path / "nonutf8.yaml"
        bad.write_bytes(b"options:\n  - label: \xff\xfe\n    description: x\n")
        assert mod._load_options(bad) == mod._BUILTIN_OPTIONS

    def test_fallback_when_no_options_list(self, tmp_path: Path):
        mod = self._load_module()
        bad = tmp_path / "noopts.yaml"
        bad.write_text("something_else: 5\n")
        assert mod._load_options(bad) == mod._BUILTIN_OPTIONS

    def test_fallback_when_option_malformed(self, tmp_path: Path):
        mod = self._load_module()
        bad = tmp_path / "malformed.yaml"
        bad.write_text("options:\n  - label: x\n")  # missing description
        assert mod._load_options(bad) == mod._BUILTIN_OPTIONS

    def test_builtin_has_three_canonical_options(self):
        mod = self._load_module()
        labels = [o["label"] for o in mod._BUILTIN_OPTIONS]
        assert labels == ["continue", "restructure", "accept-with-TODO"]

    def test_default_data_file_matches_builtin(self):
        # Shipped data/pivot-options.yaml must equal the built-in set so the
        # normal and fallback paths render identically (keeps the
        # TestB3Characterization golden valid).
        mod = self._load_module()
        assert mod._load_options() == mod._BUILTIN_OPTIONS


class TestCodexStubFixture:
    """C3 (#110): the codex stub binary is built ONCE (module-level template)
    and per-test installs reuse it via symlink + sidecar config, instead of
    writing a fresh bash script + chmod on each of the ~80 callsites. The public
    _install_codex_stub signature is unchanged; behavior must stay identical.
    """

    def test_stub_binary_is_shared_template_not_rewritten(self, tmp_path: Path):
        # Two independent installs must resolve to the SAME underlying template
        # file (the whole point of C3 — build once). Installed `codex` entries
        # are symlinks pointing at one shared template.
        d1 = _install_codex_stub(tmp_path / "a", exit_code=0)
        d2 = _install_codex_stub(tmp_path / "b", exit_code=0)
        c1 = (d1 / "codex")
        c2 = (d2 / "codex")
        assert c1.exists() and c2.exists()
        # Same real target → same template binary reused across installs.
        assert c1.resolve() == c2.resolve(), (
            "C3: each install should reuse the one shared stub template, "
            f"got distinct targets {c1.resolve()} vs {c2.resolve()}"
        )

    def test_template_built_once_across_installs(self, tmp_path: Path):
        # The shared template path must be stable (same path) regardless of how
        # many installs happen — i.e. lazily built once and cached.
        d1 = _install_codex_stub(tmp_path / "x", exit_code=0)
        first = (d1 / "codex").resolve()
        d2 = _install_codex_stub(tmp_path / "y", exit_code=3)
        second = (d2 / "codex").resolve()
        assert first == second
        # And it lives outside any single test's tmp_path (module-scoped cache).
        assert tmp_path not in first.parents, (
            f"C3 template must be module-cached, not under a test tmp_path: {first}"
        )

    def test_exit_code_still_honored_per_install(self, tmp_path: Path):
        # Behavior parity: each install's exit code is independent despite the
        # shared binary (carried via per-dir sidecar, not baked into the binary).
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=7)
        r = _run_wrapper(tmp_path, stub_dir)
        # codex exit 7 → wrapper maps to 71 (codex crash)
        assert r.returncode == 71, (
            f"per-install exit code must still drive wrapper exit; got {r.returncode}"
        )

    def test_verdict_body_still_written_per_install(self, tmp_path: Path):
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0,
            verdict_body="LEDGER_PATCH:\n  verdict: go\n  findings: []\n",
        )
        _run_wrapper(tmp_path, stub_dir)
        vf = tmp_path / "review" / "verdict-r1.txt"
        assert vf.exists() and "LEDGER_PATCH" in vf.read_text()

    def test_write_verdict_false_still_skips_output(self, tmp_path: Path):
        # write_verdict=False must still produce NO -o file (parser exit-5 path).
        stub_dir = _install_codex_stub(
            tmp_path / "bin", exit_code=0, write_verdict=False)
        r = _run_wrapper(tmp_path, stub_dir)
        # codex exits 0 but writes no verdict → wrapper exit 5 (IO failure)
        assert r.returncode == 5, (
            f"write_verdict=False must yield empty-verdict exit 5; got {r.returncode}"
        )

    def test_template_is_complete_and_executable(self, tmp_path: Path):
        # R1-F1 (PR-4 dogfood): the template must be built via atomic replace so
        # a reader never sees a partial file. Post-install the resolved template
        # is non-empty, ends with the final `exit` line (fully written), is
        # executable, and no per-pid temp scratch leaks beside it.
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)
        tmpl = (stub_dir / "codex").resolve()
        body = tmpl.read_text()
        assert body.startswith("#!/usr/bin/env bash"), "template head truncated"
        assert body.rstrip().endswith('exit "$exit_code"'), (
            "template tail truncated — atomic write not in effect")
        assert os.access(tmpl, os.X_OK), "template not executable"
        leftovers = list(tmpl.parent.glob("codex.*.tmp"))
        assert not leftovers, f"per-pid temp scratch leaked: {leftovers}"


class TestRunWrapperExtraEnv:
    """D4 (#110): _run_wrapper(extra_env=...) merges caller-supplied env vars
    over the base PATH/REVIEW_DIR/LOG defaults, so tests that need one extra var
    (CODEX_STUB_ARGS_FILE, CODEX_STUB_STDIN_FILE, BULLDOZER_FIXED/FP, …) can drop
    the manual os.environ.copy() + full subprocess.run boilerplate. The
    extra_env=None default leaves base behavior unchanged — already exercised by
    every existing _run_wrapper call in this module."""

    def test_extra_env_propagates_to_wrapper_child(self, tmp_path: Path):
        # The codex stub writes its argv to CODEX_STUB_ARGS_FILE when that var
        # is present in its env; the file existing proves extra_env merged into
        # the wrapper's child env. Independent of the round's exit code (the
        # stub writes argv before exiting).
        args_dump = tmp_path / "codex_args.txt"
        stub_dir = _install_codex_stub(tmp_path / "bin", exit_code=0)
        _run_wrapper(tmp_path, stub_dir,
                     extra_env={"CODEX_STUB_ARGS_FILE": str(args_dump)})
        assert args_dump.exists(), (
            "extra_env var did not propagate to the wrapper's child env")


class TestCalibratedEarlyPivot:
    """B6 (#128): exhaustive reviews pivot EARLY at round >= 5 when not converging
    (avg of the last 3 rounds' findings >= 3.0), instead of waiting for the flat
    round==max_rounds (10) trigger. Scoped to exhaustive only — see
    docs/superpowers/analysis/2026-06-01-b6-pivot-calibration.md (0 FP on the
    exhaustive corpus; widening to other depths produced false pivots on
    user-extended standard reviews). The flat max-rounds trigger stays as the
    round-10 backstop; this only moves the pivot dialog earlier on doomed runs.
    """

    def test_exhaustive_round5_nonconverging_fires_early_pivot(self, tmp_path: Path):
        # rounds 3,4 = 4 findings each; round 5 (VALID_LEDGER_NOGO) appends 1 →
        # avg last 3 = (4+4+1)/3 = 3.0 >= 3.0 → early pivot at round 5 (< max 10).
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 6, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 5, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:01:00+00:00"},
            {"round": 3, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:02:00+00:00"},
            {"round": 4, "verdict": "NO-GO", "findings": 4, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:03:00+00:00"},
        ]
        result = _seed_state_and_run(tmp_path, seed, round_num=5, depth="exhaustive",
                                     verdict_body=VALID_LEDGER_NOGO)
        assert result.returncode == 10, (
            f"exhaustive round 5 non-converging (avg last 3 = 3.0) → early pivot "
            f"exit 10, got {result.returncode}\nstderr: {result.stderr}"
        )

    def test_exhaustive_round5_converging_no_early_pivot(self, tmp_path: Path):
        # rounds 3,4 = 1 finding each; round 5 appends 1 → avg = 1.0 < 3.0 → no
        # early pivot. Round 5 < max 10 → exit 0 (converging — keep iterating).
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 6, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 3, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:01:00+00:00"},
            {"round": 3, "verdict": "NO-GO", "findings": 1, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:02:00+00:00"},
            {"round": 4, "verdict": "NO-GO", "findings": 1, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:03:00+00:00"},
        ]
        result = _seed_state_and_run(tmp_path, seed, round_num=5, depth="exhaustive",
                                     verdict_body=VALID_LEDGER_NOGO)
        assert result.returncode == 0, (
            f"exhaustive round 5 converging (avg last 3 = 1.0 < 3.0) → no early "
            f"pivot, exit 0, got {result.returncode}\nstderr: {result.stderr}"
        )

    def test_exhaustive_round4_below_min_round_no_early_pivot(self, tmp_path: Path):
        # Round 4 < 5: the calibrated trigger cannot fire regardless of findings
        # (avg last 3 here = (7+6+1)/3 = 4.67, well above 3.0). Round 4 < max 10
        # → exit 0. Guards the round >= 5 floor.
        seed = [
            {"round": 1, "verdict": "NO-GO", "findings": 8, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:00:00+00:00"},
            {"round": 2, "verdict": "NO-GO", "findings": 7, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:01:00+00:00"},
            {"round": 3, "verdict": "NO-GO", "findings": 6, "fixed": 0, "fp": 0, "timestamp": "2026-05-27T00:02:00+00:00"},
        ]
        result = _seed_state_and_run(tmp_path, seed, round_num=4, depth="exhaustive",
                                     verdict_body=VALID_LEDGER_NOGO)
        assert result.returncode == 0, (
            f"exhaustive round 4 (< 5) → no early pivot, exit 0, got "
            f"{result.returncode}\nstderr: {result.stderr}"
        )
