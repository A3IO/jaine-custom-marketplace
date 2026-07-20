"""Tests for skills/check/scripts/update-state.py.

Coverage: --review-dir flag (override env), --mode=replace-extraction
(happy paths + error cases), cross-state-isolation regression.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import test_env

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT = PLUGIN_ROOT / "skills" / "check" / "scripts" / "update-state.py"


def run_script(args, env_override=None, cwd=None, timeout=10):
    env = test_env()
    if env_override is not None:
        # Caller passes None for keys they want unset
        for k, v in env_override.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True, timeout=timeout,
        env=env, cwd=cwd,
    )


class TestReviewDirFlag:
    """--review-dir PATH overrides BULLDOZER_REVIEW_DIR env var.

    Motivation: when Claude invokes update-state.py from its own shell context
    (manual-extraction flow), BULLDOZER_REVIEW_DIR may not be set and would
    fall back to .bulldozer cwd-relative — wrong file. Explicit flag fixes.
    """

    def test_review_dir_flag_targets_specified_directory(self, tmp_path):
        target = tmp_path / "review"
        # No BULLDOZER_REVIEW_DIR in env, no cwd-relative .bulldozer
        result = run_script(
            ["--review-dir", str(target), "1", "NO-GO", "3", "0", "0", "artifact", "standard", "codex/x"],
            env_override={"BULLDOZER_REVIEW_DIR": None},
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (target / "state.json").exists(), "state.json must be in --review-dir target"
        # AND no stray project-root .bulldozer/ created
        assert not (tmp_path / ".bulldozer" / "state.json").exists(), \
            "must NOT create cwd-relative .bulldozer/state.json when --review-dir given"

    def test_review_dir_relative_path_canonicalized(self, tmp_path):
        """R2-F2 (R2 dogfood): when caller passes a relative --review-dir
        and the script is run from a different cwd than expected (e.g.
        Claude's Bash tool cwd shifts between messages), Path-only handling
        keeps the path relative → resolves against the WRONG cwd at file-op
        time. Post-fix: argparse path is `.resolve()`-canonicalized to
        absolute against THE cwd-at-invocation, so even if cwd later shifts,
        the path the script uses remains absolute and stable."""
        # Caller's cwd at invocation
        caller_cwd = tmp_path / "caller_cwd"
        caller_cwd.mkdir()
        # Relative path the caller passes
        relative_arg = "myreview"
        # Expected resolved location (resolved against caller_cwd)
        expected = caller_cwd / relative_arg
        # A DIFFERENT directory that we want to ensure is NOT touched
        other_cwd = tmp_path / "other_cwd"
        other_cwd.mkdir()
        stray = other_cwd / relative_arg / "state.json"

        result = run_script(
            ["--review-dir", relative_arg, "1", "NO-GO", "3", "0", "0",
             "artifact", "standard", "codex/x"],
            env_override={"BULLDOZER_REVIEW_DIR": None},
            cwd=str(caller_cwd),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # state.json must be at the resolved-from-caller_cwd path
        assert (expected / "state.json").exists(), (
            f"relative --review-dir must resolve against invocation cwd "
            f"({caller_cwd}); expected state at {expected / 'state.json'}"
        )
        # And NOT under the unrelated other_cwd
        assert not stray.exists(), (
            f"relative path must NOT resolve against an unrelated cwd; "
            f"stray detected at {stray}"
        )

    def test_review_dir_flag_overrides_env_var(self, tmp_path):
        env_target = tmp_path / "env_review"
        flag_target = tmp_path / "flag_review"
        result = run_script(
            ["--review-dir", str(flag_target), "1", "GO", "0", "0", "0", "artifact", "standard", "codex/x"],
            env_override={"BULLDOZER_REVIEW_DIR": str(env_target)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (flag_target / "state.json").exists(), "flag must win over env"
        assert not (env_target / "state.json").exists(), "env target must NOT be written"


def _bootstrap_state(review_dir: Path, *, verdict="UNKNOWN", findings=0,
                     manual_pending=True, round_num=1):
    """Seed a state.json mimicking what the wrapper's exit-11 path writes."""
    review_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "round": round_num,
        "artifact": "test-artifact",
        "depth": "standard",
        "started_at": "2026-05-28T00:00:00+00:00",
        "reviewer": "codex/test",
        "findings_total": findings,
        "fixed_total": 0,
        "false_positives": 0,
        "history": [{
            "round": round_num,
            "verdict": verdict,
            "findings": findings,
            "fixed": 0,
            "fp": 0,
            "timestamp": "2026-05-28T00:00:00+00:00",
            "manual_extraction_pending": manual_pending,
        }],
    }
    (review_dir / "state.json").write_text(json.dumps(state, indent=2))
    return state


class TestReplaceExtractionMode:
    """--mode=replace-extraction ROUND K VERDICT updates an existing history
    entry's findings + verdict + clears manual_extraction_pending; deltas
    findings_total by (K - old_findings)."""

    def test_replace_no_go_with_findings(self, tmp_path):
        """Happy path: UNKNOWN(findings=0) → NO-GO(findings=5)."""
        review = tmp_path / "review"
        _bootstrap_state(review, verdict="UNKNOWN", findings=0, manual_pending=True)
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "5", "NO-GO"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        state = json.loads((review / "state.json").read_text())
        entry = state["history"][0]
        assert entry["findings"] == 5
        assert entry["verdict"] == "NO-GO"
        assert entry.get("manual_extraction_pending") is False
        assert state["findings_total"] == 5  # delta from 0

    def test_replace_go_with_zero_findings(self, tmp_path):
        """UNKNOWN→GO when prose-extraction found no real findings."""
        review = tmp_path / "review"
        _bootstrap_state(review, verdict="UNKNOWN", findings=0, manual_pending=True)
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "0", "GO"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        state = json.loads((review / "state.json").read_text())
        entry = state["history"][0]
        assert entry["findings"] == 0
        assert entry["verdict"] == "GO"
        assert entry.get("manual_extraction_pending") is False
        assert state["findings_total"] == 0

    def test_replace_no_go_with_zero_findings(self, tmp_path):
        """Explicit NO-GO with K=0 (reviewer found problems but couldn't enumerate)."""
        review = tmp_path / "review"
        _bootstrap_state(review, verdict="UNKNOWN", findings=0, manual_pending=True)
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "0", "NO-GO"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        state = json.loads((review / "state.json").read_text())
        entry = state["history"][0]
        assert entry["findings"] == 0
        assert entry["verdict"] == "NO-GO"
        assert state["findings_total"] == 0

    def test_replace_errors_on_missing_round(self, tmp_path):
        review = tmp_path / "review"
        _bootstrap_state(review, round_num=1)
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "5", "3", "NO-GO"],
        )
        assert result.returncode == 1
        assert "round 5" in result.stderr.lower() or "not found" in result.stderr.lower()

    def test_replace_errors_when_flag_already_cleared(self, tmp_path):
        """Idempotency guard: replace-extraction must NOT silently double-mutate."""
        review = tmp_path / "review"
        _bootstrap_state(review, manual_pending=False)  # already cleared
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "3", "NO-GO"],
        )
        assert result.returncode == 1
        assert "manual_extraction_pending" in result.stderr.lower() or "already" in result.stderr.lower()

    def test_replace_errors_on_unrecognized_verdict(self, tmp_path):
        review = tmp_path / "review"
        _bootstrap_state(review)
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "3", "MAYBE"],
        )
        assert result.returncode == 1
        assert "verdict" in result.stderr.lower()

    def test_replace_extraction_handles_duplicate_round_entries(self, tmp_path):
        """Bug #2 regression: when history has multiple entries for the same
        round (e.g. cleared NO-GO entry + later UNKNOWN pending entry from a
        wrapper re-run), replace-extraction must target the pending one, not
        the first match. Pre-fix: first-match-wins picked the cleared entry
        and falsely errored "already cleared"."""
        review = tmp_path / "review"
        review.mkdir(parents=True, exist_ok=True)
        seed = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 3, "fixed_total": 0, "false_positives": 0,
            "history": [
                {"round": 1, "verdict": "NO-GO", "findings": 3, "fixed": 0, "fp": 0,
                 "timestamp": "t1", "manual_extraction_pending": False},
                {"round": 1, "verdict": "UNKNOWN", "findings": 0, "fixed": 0, "fp": 0,
                 "timestamp": "t2", "manual_extraction_pending": True},
            ],
        }
        (review / "state.json").write_text(json.dumps(seed, indent=2))
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "5", "NO-GO"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        state = json.loads((review / "state.json").read_text())
        # Pending entry (index 1) must be mutated, cleared entry (index 0) untouched.
        cleared = state["history"][0]
        pending = state["history"][1]
        assert cleared["verdict"] == "NO-GO"
        assert cleared["findings"] == 3
        assert cleared.get("manual_extraction_pending") is False
        assert pending["verdict"] == "NO-GO"
        assert pending["findings"] == 5
        assert pending.get("manual_extraction_pending") is False
        # findings_total delta: 3 + (5 - 0) = 8
        assert state["findings_total"] == 8

    def test_replace_extraction_warns_on_multiple_pendings(self, tmp_path):
        """R1-F2 (R2 dogfood, dup-pending edge): when state.json contains
        2+ history entries with the same round AND manual_extraction_pending=
        true (rare corruption: test harness double-invocation, manual
        log-round call), replace-extraction clears ONE at a time. Pre-fix:
        silent — operator could miss that subsequent reruns are required.
        Post-fix: stderr WARN names the count and recovery action; first
        pending cleared, exit 0."""
        review = tmp_path / "review"
        review.mkdir(parents=True, exist_ok=True)
        seed = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [
                {"round": 1, "verdict": "UNKNOWN", "findings": 0, "fixed": 0, "fp": 0,
                 "timestamp": "t1", "manual_extraction_pending": True},
                {"round": 1, "verdict": "UNKNOWN", "findings": 0, "fixed": 0, "fp": 0,
                 "timestamp": "t2", "manual_extraction_pending": True},
            ],
        }
        (review / "state.json").write_text(json.dumps(seed, indent=2))
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "4", "NO-GO"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # Stderr must surface multi-pending WARN
        assert "warning" in result.stderr.lower(), (
            f"stderr must surface WARN about multiple pendings; got {result.stderr!r}"
        )
        assert "2" in result.stderr, (
            f"stderr WARN must name pending count (2); got {result.stderr!r}"
        )
        assert "rerun" in result.stderr.lower() or "again" in result.stderr.lower(), (
            f"stderr WARN must hint operator to rerun; got {result.stderr!r}"
        )
        state = json.loads((review / "state.json").read_text())
        # First pending cleared; second still pending
        first = state["history"][0]
        second = state["history"][1]
        assert first["findings"] == 4
        assert first["verdict"] == "NO-GO"
        assert first.get("manual_extraction_pending") is False
        assert second.get("manual_extraction_pending") is True, (
            "second pending must remain unchanged (one-at-a-time semantics)"
        )
        # Delta arithmetic: 0 + (4 - 0) = 4
        assert state["findings_total"] == 4

    def test_replace_errors_on_null_findings(self, tmp_path):
        """Bug #4 regression: target entry has findings=null (corrupt state).
        Pre-fix: TypeError on `k - None` arithmetic. Post-fix: clean error
        message + exit 1 (no traceback)."""
        review = tmp_path / "review"
        review.mkdir(parents=True, exist_ok=True)
        seed = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": None,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                "manual_extraction_pending": True,
            }],
        }
        (review / "state.json").write_text(json.dumps(seed))
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "5", "NO-GO"],
        )
        assert result.returncode == 1
        assert "Traceback" not in result.stderr, (
            f"must not leak Python traceback; got stderr={result.stderr!r}"
        )
        assert "findings" in result.stderr.lower()
        assert "nonetype" in result.stderr.lower() or "invalid" in result.stderr.lower()

    def test_replace_errors_on_null_findings_total(self, tmp_path):
        """Bug #4 regression (sibling): state.findings_total=null corrupts the
        delta arithmetic. Pre-fix: TypeError on `None + int`. Post-fix: clean
        error message + exit 1."""
        review = tmp_path / "review"
        review.mkdir(parents=True, exist_ok=True)
        seed = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": None, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": 0,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                "manual_extraction_pending": True,
            }],
        }
        (review / "state.json").write_text(json.dumps(seed))
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "5", "NO-GO"],
        )
        assert result.returncode == 1
        assert "Traceback" not in result.stderr, (
            f"must not leak Python traceback; got stderr={result.stderr!r}"
        )
        assert "findings_total" in result.stderr.lower()
        assert "nonetype" in result.stderr.lower() or "invalid" in result.stderr.lower()


class TestReplaceExtractionFlagTypeStrictness:
    """Bug #3 regression: the manual_extraction_pending flag check must use
    strict identity comparison (`is True`) consistently. Loose truthiness
    (`not target.get(...)`) lets string `"true"` bypass the guard AND string
    `"false"` trigger replace mutation (state corruption).

    Canonical schema is Python bool True/False; anything else is CORRUPT and
    must fail closed with a clear error."""

    def test_string_true_flag_rejected(self, tmp_path):
        """String "true" (not bool True) must NOT satisfy replace-extraction
        precondition. Pre-fix: `not target.get("manual_extraction_pending")`
        sees truthy "true" → proceeds → state corruption. Post-fix: strict
        `is True` check rejects with idempotency error."""
        review = tmp_path / "review"
        review.mkdir(parents=True, exist_ok=True)
        seed = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": 0,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                "manual_extraction_pending": "true",  # STRING, not bool
            }],
        }
        (review / "state.json").write_text(json.dumps(seed))
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "5", "NO-GO"],
        )
        assert result.returncode == 1, (
            f"string 'true' must be rejected as non-canonical flag; got "
            f"exit {result.returncode}, stderr={result.stderr!r}"
        )
        assert "manual_extraction_pending" in result.stderr.lower() or \
               "true" in result.stderr.lower()

    def test_string_false_flag_rejected(self, tmp_path):
        """String "false" must NOT trigger replace-extraction mutation
        (loose-truthiness pre-fix would treat as False → "not False" → True
        → proceed → mutate the entry). Post-fix: strict `is True` rejects."""
        review = tmp_path / "review"
        review.mkdir(parents=True, exist_ok=True)
        seed = {
            "round": 1, "artifact": "test", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [{
                "round": 1, "verdict": "UNKNOWN", "findings": 0,
                "fixed": 0, "fp": 0,
                "timestamp": "2026-05-28T00:00:00+00:00",
                "manual_extraction_pending": "false",  # STRING, not bool
            }],
        }
        (review / "state.json").write_text(json.dumps(seed))
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "5", "NO-GO"],
        )
        assert result.returncode == 1, (
            f"string 'false' must be rejected as non-canonical flag; got "
            f"exit {result.returncode}, stderr={result.stderr!r}"
        )
        # And the state must be UNTOUCHED (no silent mutation)
        state_after = json.loads((review / "state.json").read_text())
        entry = state_after["history"][0]
        assert entry["findings"] == 0, "state must NOT be mutated when flag rejected"
        assert entry["verdict"] == "UNKNOWN", "verdict must NOT be mutated"


class TestManualExtractionPendingFlag:
    """append-mode `--manual-extraction-pending` flag writes the boolean
    column into the new history[] entry. Wrapper uses this on parser exit 1."""

    def test_pending_flag_writes_history_column(self, tmp_path):
        review = tmp_path / "review"
        result = run_script(
            ["--review-dir", str(review), "--manual-extraction-pending",
             "1", "UNKNOWN", "0", "0", "0", "artifact", "standard", "codex/x"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        state = json.loads((review / "state.json").read_text())
        entry = state["history"][0]
        assert entry.get("manual_extraction_pending") is True

    def test_pending_flag_default_absent_or_false(self, tmp_path):
        """Without the flag, the column must not be true."""
        review = tmp_path / "review"
        result = run_script(
            ["--review-dir", str(review),
             "1", "NO-GO", "3", "0", "0", "artifact", "standard", "codex/x"],
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        state = json.loads((review / "state.json").read_text())
        entry = state["history"][0]
        # Either absent or explicitly False — neither shall be True
        assert not entry.get("manual_extraction_pending", False)


class TestCrossStateIsolation:
    """Regression: replace-extraction with --review-dir must NEVER touch
    project-root .bulldozer/state.json (the wrong file). Covers R2-F1
    from spec dogfood Round 2."""

    def test_replace_extraction_does_not_create_cwd_relative_state(self, tmp_path):
        review = tmp_path / "review"
        _bootstrap_state(review)
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
        result = run_script(
            ["--review-dir", str(review), "--mode=replace-extraction", "1", "3", "NO-GO"],
            env_override={"BULLDOZER_REVIEW_DIR": None},
            cwd=str(cwd),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (review / "state.json").exists()
        # Must NOT create .bulldozer/state.json in cwd
        stray = cwd / ".bulldozer" / "state.json"
        assert not stray.exists(), (
            f"replace-extraction must NOT create cwd-relative state at {stray}; "
            "BULLDOZER_REVIEW_DIR env was unset and --review-dir given"
        )

    def test_replace_extraction_does_not_use_env_when_flag_given(self, tmp_path):
        """Even if both env and flag set, flag wins; env target untouched."""
        review_via_flag = tmp_path / "flag_review"
        _bootstrap_state(review_via_flag)
        review_via_env = tmp_path / "env_review"
        # env target has NO state.json — replace-extraction would error if it
        # accidentally used env path
        result = run_script(
            ["--review-dir", str(review_via_flag), "--mode=replace-extraction", "1", "2", "NO-GO"],
            env_override={"BULLDOZER_REVIEW_DIR": str(review_via_env)},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert (review_via_flag / "state.json").exists()
        assert not (review_via_env / "state.json").exists()


class TestFixedFpInvariant:
    """#314: `fixed`/`fp` on round N are dispositions of round N-1's findings
    (SKILL.md Step 6 sets BULLDOZER_FIXED when launching the NEXT round), so
    the sanity warning must compare against the PREVIOUS history entry.
    The old same-round comparison fired on every healthy converging review
    (findings 3→2 with 3 fixes = cry-wolf)."""

    def _append_round(self, review_dir, round_num, findings, fixed, fp=0,
                      verdict="NO-GO"):
        return run_script(
            ["--review-dir", str(review_dir), str(round_num), verdict,
             str(findings), str(fixed), str(fp), "artifact", "standard",
             "codex/x"],
            env_override={"BULLDOZER_REVIEW_DIR": None},
        )

    def test_converging_review_emits_no_warning(self, tmp_path):
        """Issue #314 repro: r1 findings=3 → 3 fixed → r2 findings=2.
        fixed+fp (3) <= previous round's findings (3) → healthy, silent."""
        review = tmp_path / "review"
        r1 = self._append_round(review, 1, findings=3, fixed=0)
        assert r1.returncode == 0, f"stderr: {r1.stderr}"
        r2 = self._append_round(review, 2, findings=2, fixed=3)
        assert r2.returncode == 0, f"stderr: {r2.stderr}"
        assert "warning: fixed+fp" not in r2.stderr, (
            "converging review must not warn (cry-wolf, #314); "
            f"stderr: {r2.stderr!r}"
        )

    def test_genuine_overcount_still_warns(self, tmp_path):
        """fixed+fp exceeding the PREVIOUS round's findings is a real desync
        — the warning must survive the #314 fix."""
        review = tmp_path / "review"
        r1 = self._append_round(review, 1, findings=2, fixed=0)
        assert r1.returncode == 0, f"stderr: {r1.stderr}"
        r2 = self._append_round(review, 2, findings=2, fixed=2, fp=1)
        assert r2.returncode == 0, f"stderr: {r2.stderr}"
        assert "warning: fixed+fp" in r2.stderr, (
            f"fixed+fp=3 > previous findings=2 must warn; stderr: {r2.stderr!r}"
        )
        assert "previous round" in r2.stderr, (
            "warning must name its baseline (previous round's findings); "
            f"stderr: {r2.stderr!r}"
        )

    def test_first_round_has_no_baseline_no_warning(self, tmp_path):
        """Round 1 has no previous entry — nothing to compare against."""
        review = tmp_path / "review"
        r1 = self._append_round(review, 1, findings=0, fixed=5)
        assert r1.returncode == 0, f"stderr: {r1.stderr}"
        assert "warning: fixed+fp" not in r1.stderr, (
            f"no previous round → no baseline → no warning; stderr: {r1.stderr!r}"
        )

    def test_same_round_rerun_uses_prior_round_baseline(self, tmp_path):
        """Codex review #330 r1 (P2): a wrapper re-run of round N appends a
        SECOND round-N entry — history[-1] is then the current round itself,
        not round N-1. The baseline must be the latest entry with
        round < round_num, or the rerun re-fires the cry-wolf warning."""
        review = tmp_path / "review"
        r1 = self._append_round(review, 1, findings=3, fixed=0)
        assert r1.returncode == 0, f"stderr: {r1.stderr}"
        r2 = self._append_round(review, 2, findings=2, fixed=3)
        assert r2.returncode == 0, f"stderr: {r2.stderr}"
        rerun = self._append_round(review, 2, findings=2, fixed=3)
        assert rerun.returncode == 0, f"stderr: {rerun.stderr}"
        assert "warning: fixed+fp" not in rerun.stderr, (
            "round-2 rerun must compare against round 1 (findings=3), not the "
            f"prior round-2 entry (findings=2); stderr: {rerun.stderr!r}"
        )

    def test_same_round_rerun_genuine_overcount_still_warns(self, tmp_path):
        """The rerun path must not silence a REAL desync either."""
        review = tmp_path / "review"
        r1 = self._append_round(review, 1, findings=2, fixed=0)
        assert r1.returncode == 0, f"stderr: {r1.stderr}"
        r2 = self._append_round(review, 2, findings=2, fixed=3)
        assert "warning: fixed+fp" in r2.stderr, f"stderr: {r2.stderr!r}"
        rerun = self._append_round(review, 2, findings=2, fixed=3)
        assert rerun.returncode == 0, f"stderr: {rerun.stderr}"
        assert "warning: fixed+fp" in rerun.stderr, (
            f"fixed+fp=3 > round-1 findings=2 must warn on rerun too; "
            f"stderr: {rerun.stderr!r}"
        )

    def test_out_of_order_rerun_baseline_is_highest_prior_round(self, tmp_path):
        """Codex review #330 r2 (P2): a rerun of an OLDER round appends after
        newer rounds (history rounds 1,2,3,2) — an append-order reverse scan
        would baseline round 4 against the trailing round-2 entry instead of
        round 3. Baseline must be the HIGHEST round < round_num (latest
        duplicate of it). Both failure directions covered."""
        # Direction 1: missed real overcount (trailing r2 findings=2 masks
        # r3 findings=1).
        review = tmp_path / "missed"
        for rnd, f, fx in ((1, 3, 0), (2, 2, 3), (3, 1, 2), (2, 2, 3)):
            r = self._append_round(review, rnd, findings=f, fixed=fx)
            assert r.returncode == 0, f"stderr: {r.stderr}"
        r4 = self._append_round(review, 4, findings=0, fixed=2)
        assert r4.returncode == 0, f"stderr: {r4.stderr}"
        assert "warning: fixed+fp" in r4.stderr, (
            "fixed+fp=2 > round-3 findings=1 must warn even after an "
            f"out-of-order round-2 rerun; stderr: {r4.stderr!r}"
        )
        # Direction 2: false warning (trailing r2 findings=2 masks r3
        # findings=5).
        review = tmp_path / "false_warn"
        for rnd, f, fx in ((1, 3, 0), (2, 2, 3), (3, 5, 2), (2, 2, 3)):
            r = self._append_round(review, rnd, findings=f, fixed=fx)
            assert r.returncode == 0, f"stderr: {r.stderr}"
        r4 = self._append_round(review, 4, findings=1, fixed=3)
        assert r4.returncode == 0, f"stderr: {r4.stderr}"
        assert "warning: fixed+fp" not in r4.stderr, (
            "fixed+fp=3 <= round-3 findings=5 must stay silent even after an "
            f"out-of-order round-2 rerun; stderr: {r4.stderr!r}"
        )

    def test_corrupt_previous_findings_skips_check_without_crash(self, tmp_path):
        """Previous entry with non-int findings (legacy/corrupt) — skip the
        advisory check cleanly instead of crashing or false-warning."""
        review = tmp_path / "review"
        review.mkdir(parents=True)
        state = {
            "round": 1, "artifact": "test-artifact", "depth": "standard",
            "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
            "findings_total": 0, "fixed_total": 0, "false_positives": 0,
            "history": [
                {"round": 1, "verdict": "NO-GO", "findings": None,
                 "fixed": 0, "fp": 0, "timestamp": "2026-05-28T00:00:00+00:00"},
            ],
        }
        (review / "state.json").write_text(json.dumps(state))
        r2 = self._append_round(review, 2, findings=1, fixed=2)
        assert r2.returncode == 0, f"stderr: {r2.stderr}"
        assert "Traceback" not in r2.stderr, f"stderr: {r2.stderr!r}"
        assert "warning: fixed+fp" not in r2.stderr, (
            f"non-int baseline → check skipped, no warning; stderr: {r2.stderr!r}"
        )
