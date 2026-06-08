# PR-1 Manual Fallback Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the discipline-failure gap where wrapper exit 1 (no LEDGER_PATCH) hands control back to Claude, re-creating issues #98/#102. Introduce a new exit-11 protocol that logs the round into `state.json` before prompting Claude to extract findings from prose, then provides an explicit `--mode=replace-extraction` path so the round's `findings`, `verdict`, and totals reconcile correctly.

**Architecture:** Two-stage protocol. (1) Wrapper detects parser exit 1, logs round to `state.json` with `verdict="UNKNOWN"`, `findings=0`, `manual_extraction_pending=true`, exits 11. (2) Claude reads `verdict-rN.txt`, extracts findings (count K + verdict GO/NO-GO), calls `update-state.py --review-dir $REVIEW_DIR --mode=replace-extraction ROUND K VERDICT`; if `ROUND == max_rounds && VERDICT == NO-GO`, Claude inlines the same AskUserQuestion pivot dialog the exit-10 path normally emits.

**Tech Stack:** Bash (wrapper + log-round), Python 3 (update-state.py + parser + tests), pytest (test suite).

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `skills/check/scripts/update-state.py` | Modify | Add `--review-dir PATH` flag (avoid wrong state.json when env not set). Add `--mode=replace-extraction` that takes ROUND K VERDICT and updates an existing `history[round=N]` entry's `findings`, `verdict`, `manual_extraction_pending`, plus delta-correct `findings_total`. Keep current append-only mode as default for backward compatibility. |
| `skills/check/scripts/log-round.sh` | Modify | Add 9th positional `MANUAL_EXTRACTION_PENDING` (default empty/false). Pass through to `update-state.py` via a new `--manual-extraction-pending` flag. |
| `skills/check/scripts/bulldozer-round.sh` | Modify | Replace current parser-exit-1 branch (echo warning + raw `exit 1`) with: invoke `log-round.sh` with `verdict="UNKNOWN"`, `findings=0`, `manual_extraction_pending=true`, then emit informational stderr message naming the verdict file and replace-extraction command, then `exit 11`. |
| `skills/check/SKILL.md` | Modify | Add `11` row to Step 3 exit-code table. Add Step 7 "manual-extraction" branch documenting the read-prose → call replace-extraction → terminal-pivot-check flow. Update digraph note (full digraph rewrite is PR-3 B7 scope). |
| `tests/test_update_state.py` | Create | TDD coverage for `--review-dir`, `--mode=replace-extraction` happy path (NO-GO + GO), idempotency errors, invalid VERDICT, missing round, cross-state-isolation regression. |
| `tests/test_check_round_wrapper.py` | Modify | Add `TestManualExtractionBranch` class: exit 11 fires on missing LEDGER_PATCH, state.json contains the round with `verdict="UNKNOWN"` and `manual_extraction_pending=true`, bulldozer.log appended, stderr names the verdict file + replace-extraction command. |

**Dependencies:** Task 1 (`--review-dir` flag) → Task 2 (`--mode=replace-extraction`). Task 3 (log-round flag pass-through) → Task 4 (wrapper exit 11). Tasks 5-7 (SKILL.md, tests, regression) build on 1-4. Task 8 (full-suite verification) is last.

**Out of scope (deferred to other PRs):**
- B3 (extract trajectory/pivot to separate scripts) — PR-3
- B7 (digraph rewrite) — PR-3
- B8 (parser emits canonical verdict) — PR-3
- E2 (two-channel pivot doc) — PR-6

---

## Task 1: Add `--review-dir PATH` flag to update-state.py

**Files:**
- Create: `tests/test_update_state.py`
- Modify: `skills/check/scripts/update-state.py`

- [ ] **Step 1: Create failing test for `--review-dir` flag**

Create `tests/test_update_state.py` with:

```python
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

PLUGIN_ROOT = Path(__file__).parent.parent
SCRIPT = PLUGIN_ROOT / "skills" / "check" / "scripts" / "update-state.py"


def run_script(args, env_override=None, cwd=None, timeout=10):
    env = os.environ.copy()
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py::TestReviewDirFlag -v`

Expected: 2 tests FAIL with `unrecognized arguments: --review-dir` (or similar argparse error).

- [ ] **Step 3: Add `--review-dir` flag to update-state.py**

Replace the top of `main()` in `skills/check/scripts/update-state.py` from `if len(sys.argv) < 5:` through `state_dir = Path(os.environ.get("BULLDOZER_REVIEW_DIR", ".bulldozer"))` with:

```python
def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Update .bulldozer/state.json after a round completes.",
    )
    parser.add_argument("--review-dir", type=Path, default=None,
                        help="Target review directory (overrides BULLDOZER_REVIEW_DIR env var)")
    parser.add_argument("positional", nargs="*",
                        help="ROUND VERDICT FINDINGS FIXED [FP] [ARTIFACT] [DEPTH] [REVIEWER]")
    args = parser.parse_args()

    pos = args.positional
    if len(pos) < 4:
        print("usage: update-state.py [--review-dir PATH] ROUND VERDICT FINDINGS FIXED [FP] [ARTIFACT] [DEPTH] [REVIEWER]", file=sys.stderr)
        sys.exit(1)

    try:
        round_num = int(pos[0])
        findings = int(pos[2])
        fixed = int(pos[3])
        fp = int(pos[4]) if len(pos) > 4 else 0
    except ValueError as e:
        print(f"error: numeric argument expected: {e}", file=sys.stderr)
        sys.exit(1)

    if findings < 0 or fixed < 0 or fp < 0:
        print(f"error: counts must be >= 0 (got findings={findings}, fixed={fixed}, fp={fp})", file=sys.stderr)
        sys.exit(1)
    if fixed + fp > findings:
        print(f"warning: fixed+fp ({fixed + fp}) exceeds findings ({findings})", file=sys.stderr)

    verdict = pos[1]
    artifact = pos[5] if len(pos) > 5 else ""
    depth = pos[6] if len(pos) > 6 else "standard"
    reviewer = pos[7] if len(pos) > 7 else "codex/unknown"

    # --review-dir flag wins over env var so Claude's shell-context invocation
    # of replace-extraction targets the per-review state.json explicitly.
    # Env var stays as default for the wrapper's log-round.sh subprocess path.
    if args.review_dir is not None:
        state_dir = args.review_dir
    else:
        state_dir = Path(os.environ.get("BULLDOZER_REVIEW_DIR", ".bulldozer"))
```

Keep the rest of `main()` (mkdir, file-load, mutation, write) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py::TestReviewDirFlag -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Run existing wrapper tests to verify no regression**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_check_round_wrapper.py -q`

Expected: all 76 tests still PASS (argparse refactor is backward compatible — positional usage unchanged).

- [ ] **Step 6: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add skills/check/scripts/update-state.py tests/test_update_state.py
git commit -m "feat(check): add --review-dir flag to update-state.py

Explicit flag overrides BULLDOZER_REVIEW_DIR env var. Required for
PR-1 manual-extraction flow where Claude invokes update-state.py
from its own shell context (no inherited env) and would otherwise
silently mutate project-root .bulldozer/state.json instead of the
per-review state.json. Wrapper's log-round.sh subprocess path keeps
using the env var (unchanged).

Refs #110, refs PR #113 (spec)"
```

---

## Task 2: Add `--mode=replace-extraction` to update-state.py

**Files:**
- Modify: `skills/check/scripts/update-state.py`
- Modify: `tests/test_update_state.py`

- [ ] **Step 1: Write failing tests for replace-extraction mode**

Append to `tests/test_update_state.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py::TestReplaceExtractionMode -v`

Expected: 6 tests FAIL with `unrecognized arguments: --mode=replace-extraction` (or similar).

- [ ] **Step 3: Implement `--mode=replace-extraction`**

In `skills/check/scripts/update-state.py`, extend the argparse block from Task 1:

```python
    parser.add_argument("--mode", choices=["append", "replace-extraction"],
                        default="append",
                        help="append (default): standard add-round behavior. "
                             "replace-extraction: update existing history entry's "
                             "findings/verdict, clear manual_extraction_pending flag, "
                             "delta-correct findings_total.")
```

Then after `state_dir = ...` resolution, branch on mode. Add a new helper function ABOVE `main()`:

```python
VALID_REPLACE_VERDICTS = {"GO", "NO-GO"}


def replace_extraction(state_file: Path, round_num: int, k: int, verdict: str) -> int:
    """Update existing history[round=N] entry: set findings=K, verdict=VERDICT,
    clear manual_extraction_pending; delta-correct findings_total.
    Returns process exit code."""
    if verdict not in VALID_REPLACE_VERDICTS:
        print(f"error: --mode=replace-extraction VERDICT must be one of {sorted(VALID_REPLACE_VERDICTS)} (got: {verdict!r})", file=sys.stderr)
        return 1
    if k < 0:
        print(f"error: K must be >= 0 (got: {k})", file=sys.stderr)
        return 1
    if not state_file.exists():
        print(f"error: state.json not found at {state_file} — cannot replace-extraction without prior round entry", file=sys.stderr)
        return 1
    try:
        state = json.loads(state_file.read_text())
    except json.JSONDecodeError as e:
        print(f"error: {state_file} corrupted: {e}", file=sys.stderr)
        return 1
    history = state.get("history", [])
    target = None
    for entry in history:
        if entry.get("round") == round_num:
            target = entry
            break
    if target is None:
        print(f"error: round {round_num} not found in {state_file} history", file=sys.stderr)
        return 1
    if not target.get("manual_extraction_pending"):
        print(f"error: round {round_num} manual_extraction_pending flag is already cleared or absent — replace-extraction is idempotent and refuses double-mutation", file=sys.stderr)
        return 1
    old_findings = target.get("findings", 0)
    target["findings"] = k
    target["verdict"] = verdict
    target["manual_extraction_pending"] = False
    state["findings_total"] = state.get("findings_total", 0) + (k - old_findings)
    tmp = state_file.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, state_file)
    except OSError as e:
        print(f"error: cannot write {state_file}: {e}", file=sys.stderr)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return 1
    print(json.dumps(state, indent=2))
    return 0
```

Then in `main()` after `state_dir` resolution, before the existing append-mode logic, add:

```python
    state_file = state_dir / "state.json"

    if args.mode == "replace-extraction":
        if len(pos) < 3:
            print("usage: update-state.py --mode=replace-extraction --review-dir PATH ROUND K VERDICT", file=sys.stderr)
            sys.exit(1)
        try:
            round_num = int(pos[0])
            k = int(pos[1])
        except ValueError as e:
            print(f"error: numeric argument expected: {e}", file=sys.stderr)
            sys.exit(1)
        verdict = pos[2]
        sys.exit(replace_extraction(state_file, round_num, k, verdict))

    # ... existing append-mode logic continues unchanged below ...
```

Move the existing `state_file = state_dir / "state.json"` line up to before the mode branch (delete the duplicate from append-mode block).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py -v`

Expected: all tests PASS (2 from Task 1 + 6 from Task 2 = 8).

- [ ] **Step 5: Run wrapper tests to confirm no regression**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_check_round_wrapper.py -q`

Expected: 76 wrapper tests still PASS (default `--mode=append` preserves all current behavior).

- [ ] **Step 6: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add skills/check/scripts/update-state.py tests/test_update_state.py
git commit -m "feat(check): add --mode=replace-extraction to update-state.py

Implements the spec's PR-1 step 3: Claude invokes this mode after
extracting findings from prose to update the round's findings, verdict,
and clear the manual_extraction_pending flag. Idempotency guard
prevents double-mutation. Delta-correct findings_total avoids the
append-only mode's double-counting. VERDICT must be GO or NO-GO
(strict).

Refs #110, refs PR #113"
```

---

## Task 3: Add `--manual-extraction-pending` flag to log-round.sh + update-state.py append mode

**Files:**
- Modify: `skills/check/scripts/log-round.sh`
- Modify: `skills/check/scripts/update-state.py`
- Modify: `tests/test_update_state.py`

- [ ] **Step 1: Write failing test for append-mode `--manual-extraction-pending` flag**

Append to `tests/test_update_state.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py::TestManualExtractionPendingFlag -v`

Expected: 1 FAIL (test_pending_flag_writes_history_column — unrecognized argument), 1 PASS (default-absent).

- [ ] **Step 3: Add `--manual-extraction-pending` flag to update-state.py**

In `update-state.py` `main()` argparse block, add:

```python
    parser.add_argument("--manual-extraction-pending", action="store_true",
                        help="Mark the new history entry with manual_extraction_pending=true "
                             "(append mode only; cleared via --mode=replace-extraction)")
```

In the append-mode logic (where `state["history"].append({...})` happens), include the column:

```python
    history_entry = {
        "round": round_num,
        "verdict": verdict,
        "findings": findings,
        "fixed": fixed,
        "fp": fp,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if args.manual_extraction_pending:
        history_entry["manual_extraction_pending"] = True
    state["history"].append(history_entry)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py -v`

Expected: 10 tests PASS (8 prior + 2 new).

- [ ] **Step 5: Add `--manual-extraction-pending` pass-through to log-round.sh**

Modify `skills/check/scripts/log-round.sh` — change the `update-state.py` invocation block to accept an optional 9th positional `MANUAL_EXTRACTION_PENDING` and forward as flag:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROUND="${1:?usage: log-round.sh ROUND ARTIFACT VERDICT FINDINGS FIXED FP REVIEWER [PROJECT] [MANUAL_EXTRACTION_PENDING]}"
ARTIFACT="${2:?}"
VERDICT="${3:?}"
FINDINGS="${4:?}"
FIXED="${5:?}"
FP="${6:?}"
REVIEWER="${7:-codex/unknown}"
PROJECT="${8:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
MANUAL_EXTRACTION_PENDING="${9:-}"
SESSION="${CLAUDE_CODE_SESSION_ID:-unknown}"
SESSION="${SESSION:0:8}"
LOG_FILE="${BULLDOZER_LOG:-$HOME/.claude/hooks/bulldozer.log}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPTH="${BULLDOZER_DEPTH:-standard}"

update_args=("$ROUND" "$VERDICT" "$FINDINGS" "$FIXED" "$FP" "$ARTIFACT" "$DEPTH" "$REVIEWER")
update_flags=()
if [[ "$MANUAL_EXTRACTION_PENDING" == "true" ]]; then
    update_flags+=("--manual-extraction-pending")
fi

BULLDOZER_REVIEW_DIR="${BULLDOZER_REVIEW_DIR:-.bulldozer}" \
python3 "$SCRIPT_DIR/update-state.py" "${update_flags[@]}" "${update_args[@]}" \
    > /dev/null

mkdir -p "$(dirname "$LOG_FILE")"
if ! echo "$(date -Iseconds) | session=${SESSION} | round=${ROUND} | artifact=${ARTIFACT} | verdict=${VERDICT} | findings=${FINDINGS} | fixed=${FIXED} | fp=${FP} | reviewer=${REVIEWER} | project=${PROJECT}" >> "$LOG_FILE"; then
    echo "warning: state.json updated but log append to $LOG_FILE failed — audit trail incomplete" >&2
fi
```

- [ ] **Step 6: Run log-round end-to-end check via wrapper tests**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_check_round_wrapper.py -q`

Expected: all 76 wrapper tests still PASS (log-round signature is backward compatible; new 9th positional is optional).

- [ ] **Step 7: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add skills/check/scripts/log-round.sh skills/check/scripts/update-state.py tests/test_update_state.py
git commit -m "feat(check): add --manual-extraction-pending flag

update-state.py (append mode) writes manual_extraction_pending=true
into the new history entry when the flag is set. log-round.sh
accepts an optional 9th positional and forwards as flag. Backward
compatible — default behavior unchanged.

Refs #110, refs PR #113"
```

---

## Task 4: Wrapper exit 11 on parser exit 1 + log round before exit

**Files:**
- Modify: `skills/check/scripts/bulldozer-round.sh`
- Modify: `tests/test_check_round_wrapper.py`

- [ ] **Step 1: Write failing test for exit 11 + state.json contents**

Append to `tests/test_check_round_wrapper.py` (after the existing class definitions, before the end of file):

```python
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
```

If `import json` is not already at the top of the test file, add it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_check_round_wrapper.py::TestManualExtractionBranch -v`

Expected: 4 of 5 tests FAIL (current wrapper exits raw 1 for missing-LEDGER; doesn't write state.json; doesn't append bulldozer.log; stderr doesn't name replace-extraction command). The 5th (`test_existing_LEDGER_PATCH_path_still_exits_0`) PASSES.

- [ ] **Step 3: Replace wrapper parser-exit-1 branch**

In `skills/check/scripts/bulldozer-round.sh`, find the `case "$parser_exit" in` block. Replace the `1)` branch from:

```bash
    1)
        # Reviewer narrated the verdict but skipped the LEDGER_PATCH block.
        # Caller (Claude) falls back to extracting findings from prose by
        # reading $VERDICT_FILE directly. Same exit code so the SKILL.md
        # branch can react without parsing our stderr.
        echo "warning: no LEDGER_PATCH block — caller must extract findings manually from ${VERDICT_FILE}" >&2
        exit 1
        ;;
```

to:

```bash
    1)
        # PR-1 manual-fallback discipline (issue #110 B5):
        # Reviewer narrated the verdict but skipped LEDGER_PATCH. Instead of
        # raw exit 1 (which re-creates #98/#102 discipline gap by handing
        # control to Claude with no state recorded), log the round to
        # state.json with verdict=UNKNOWN + manual_extraction_pending=true,
        # append to bulldozer.log, then exit 11 so caller knows to:
        #   1. Read $VERDICT_FILE
        #   2. Extract findings from prose (count K, determine VERDICT)
        #   3. Call update-state.py --review-dir $REVIEW_DIR \
        #          --mode=replace-extraction $ROUND $K $VERDICT
        # See SKILL.md Step 7 "manual-extraction branch" for the protocol.
        manual_log_exit=0
        BULLDOZER_REVIEW_DIR="$REVIEW_DIR" BULLDOZER_DEPTH="$DEPTH" \
            bash "$LOG_ROUND" "$ROUND" "$ARTIFACT" "UNKNOWN" \
                "0" "$FIXED" "$FP" "$REVIEWER" "$PROJECT_ROOT" "true" \
                > /dev/null || manual_log_exit=$?
        if (( manual_log_exit != 0 )); then
            _emit_stop 70 "log-round.sh failed during manual-extraction logging (exit ${manual_log_exit})." \
                "Helper script: ${LOG_ROUND}" \
                "Cannot proceed with exit 11 because state.json was not written."
        fi
        {
            echo "MANUAL_EXTRACTION_REQUIRED: round=${ROUND} artifact=${ARTIFACT}"
            echo "      Verdict file: ${VERDICT_FILE}"
            echo "      Extract findings from prose, then call:"
            echo "      update-state.py --review-dir ${REVIEW_DIR} \\"
            echo "          --mode=replace-extraction ${ROUND} <K> <GO|NO-GO>"
        } >&2
        exit 11
        ;;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_check_round_wrapper.py::TestManualExtractionBranch -v`

Expected: all 5 tests PASS.

- [ ] **Step 5: Run full wrapper test suite**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_check_round_wrapper.py -q`

Expected: 81 tests PASS (76 prior + 5 new). If any prior test referenced `exit 1` on missing-LEDGER, it must be updated (search for `returncode == 1` near `LEDGER_PATCH` to confirm none break).

- [ ] **Step 6: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add skills/check/scripts/bulldozer-round.sh tests/test_check_round_wrapper.py
git commit -m "feat(check): wrapper exit 11 on missing LEDGER_PATCH (manual fallback)

Replaces the raw exit 1 + warning on parser exit 1 with: log round to
state.json (verdict=UNKNOWN, findings=0, manual_extraction_pending=
true), append to bulldozer.log, emit MANUAL_EXTRACTION_REQUIRED
diagnostic naming the verdict file + replace-extraction command, then
exit 11. Closes the discipline gap that re-created issues #98/#102 in
PR1b — every round now writes state before control returns to Claude.

Caller (Claude in SKILL.md Step 7) handles exit 11 by reading the
verdict file, extracting findings from prose, and calling:
  update-state.py --review-dir REVIEW_DIR --mode=replace-extraction \\
      ROUND K VERDICT

Refs #110 (B5), refs PR #113"
```

---

## Task 5: SKILL.md Step 3 exit-code table includes exit 11

**Files:**
- Modify: `skills/check/SKILL.md`

- [ ] **Step 1: Add exit 11 row to Step 3 exit-code table**

In `skills/check/SKILL.md`, find the markdown table starting with `| Exit | Origin | Meaning | Your action |`. Insert a new row AFTER the `10` row and BEFORE the `64` row:

```
| `11` | wrapper | No LEDGER_PATCH block in verdict — round logged with `verdict="UNKNOWN"` + `manual_extraction_pending=true`; caller must extract findings from prose and reconcile | Read `${REVIEW_DIR}/verdict-r${ROUND}.txt`, count findings (K) and determine VERDICT (GO/NO-GO) from the prose. Then call: `update-state.py --review-dir ${REVIEW_DIR} --mode=replace-extraction ${ROUND} ${K} ${VERDICT}`. See Step 7 "manual-extraction branch" for the full flow including terminal-round pivot dispatch. |
```

Also delete the old `1` row description that says "Manual fallback: extract findings from prose..." because exit 1 is now the parser-only signal that the wrapper consumes internally — callers should never observe it post-PR-1. Replace it with:

```
| `1` | parser | (internal) No LEDGER_PATCH block — wrapper intercepts and converts to exit 11. Callers should never observe this exit directly. | If somehow surfaced (wrapper bug?), report to plugin maintainer. |
```

- [ ] **Step 2: Verify markdown renders correctly**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && grep -nE '^\| `(11\|1)`' skills/check/SKILL.md`

Expected: 2 rows visible with the new exit-11 description and the updated exit-1 (internal) description.

- [ ] **Step 3: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add skills/check/SKILL.md
git commit -m "docs(check): SKILL.md Step 3 — exit 11 manual-extraction row

Documents the new exit 11 protocol (manual-extraction branch) added
in the previous commit. Updates the exit 1 row to reflect that it's
now an internal parser signal the wrapper intercepts.

Refs #110 (B5), refs PR #113"
```

---

## Task 6: SKILL.md Step 7 flow — manual-extraction branch with terminal-pivot dispatch

**Files:**
- Modify: `skills/check/SKILL.md`

- [ ] **Step 1: Add manual-extraction branch to Step 7**

In `skills/check/SKILL.md`, find the section `**7. Loop or stop:**` and its bullet list. Replace the entire bullet block with:

```markdown
**7. Loop or stop:**
- Verdict GO (wrapper exit 0, parsed-rN.json has `findings: []`) → done, write summary
- Round < max, verdict NO-GO → build Round N prompt from ledger, go to Step 2
- Wrapper exited 10 (pivot signal) → act on the user's AskUserQuestion choice from Step 3 (at `ROUND >= max_rounds && verdict != GO` the wrapper always writes the pivot file and exits 10, so a `Round == max + NO-GO` case never reaches this step without a pivot signal — if it does, treat it as a wrapper-state bug and report the pivot file write failure to the operator)
- **Wrapper exited 11 (manual-extraction branch) — REQUIRED PROTOCOL:**
  1. Read `${REVIEW_DIR}/verdict-r${ROUND}.txt` — reviewer wrote prose but skipped the structured LEDGER_PATCH block
  2. Extract findings from prose: count `K` (number of real findings) and determine `VERDICT` (GO if no real findings; NO-GO if K > 0 OR reviewer narrated problems without enumerating cleanly)
  3. Append the extracted findings to `${REVIEW_DIR}/review-ledger.yml` with status `open` (use IDs `R${ROUND}-F${M}` matching wrapper convention)
  4. Reconcile state: `update-state.py --review-dir "${REVIEW_DIR}" --mode=replace-extraction ${ROUND} ${K} ${VERDICT}` — this updates `history[round=${ROUND}].findings`, `verdict`, and clears `manual_extraction_pending`; deltas `findings_total` correctly
  5. **Terminal-round pivot check (REQUIRED):** if `ROUND == max_rounds` AND `VERDICT == NO-GO`, fire the same AskUserQuestion pivot dialog the exit-10 path emits — options are `continue` / `restructure` / `accept-with-TODO`. Manual-extraction at terminal round MUST NOT silently exit without the pivot dialog (parity with non-manual flow).
  6. Continue per the standard flow: if `VERDICT == GO` → done; if `ROUND < max_rounds` → build Round N+1 prompt and go to Step 2; if pivot dialog fires → act on user choice.

  > **Why this protocol exists:** Issue #110 (B5) — pre-PR-1, wrapper exit 1 silently lost the round (no state.json, no bulldozer.log) and handed control to Claude with zero discipline. The exit 11 + replace-extraction pair restores the discipline invariant (every round writes state.json + bulldozer.log) while preserving the human-readable prose-extraction path for reviewers that skip LEDGER_PATCH.
```

- [ ] **Step 2: Verify renders correctly**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && grep -A30 '^\*\*7\. Loop or stop:\*\*' skills/check/SKILL.md`

Expected: the new 6-step protocol visible with the terminal-pivot requirement.

- [ ] **Step 3: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add skills/check/SKILL.md
git commit -m "docs(check): SKILL.md Step 7 — manual-extraction protocol

Documents the full 6-step protocol Claude follows on wrapper exit 11:
read verdict prose, extract findings, append to ledger, call
replace-extraction, fire terminal-round pivot if needed, continue per
standard flow. Closes the documentation gap for the discipline-
preserving exit 11 path.

Refs #110 (B5), refs PR #113"
```

---

## Task 7: Cross-state-isolation regression test (per spec R2-F1)

**Files:**
- Modify: `tests/test_update_state.py`

- [ ] **Step 1: Write the regression test**

Append to `tests/test_update_state.py`:

```python
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
```

- [ ] **Step 2: Run test to verify**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/test_update_state.py::TestCrossStateIsolation -v`

Expected: 2 tests PASS immediately (Task 1's `--review-dir` flag already provides this isolation; this test just locks the regression contract).

- [ ] **Step 3: Commit**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git add tests/test_update_state.py
git commit -m "test(check): cross-state-isolation regression (R2-F1)

Locks the spec contract: replace-extraction with --review-dir must
never create cwd-relative .bulldozer/state.json even when
BULLDOZER_REVIEW_DIR is unset. Two tests: (1) env unset + flag given
→ flag target only; (2) env set + flag given → flag wins.

Caught during dogfood Round 2 of the roadmap spec (R2-F1).

Refs #110, refs PR #113"
```

---

## Task 8: Full-suite verification + dogfood

**Files:** none modified — verification + pre-PR dogfood.

- [ ] **Step 1: Run full bulldozer test suite**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && python3 -m pytest tests/ -q --ignore=tests/test_check_e2e.py --ignore=tests/test_cdp.py --ignore=tests/test_e2e.py`

Expected: all PASS (76 wrapper + 13 update-state + 5 manual-extraction-branch + 2 cross-isolation = 96 tests minimum, plus existing parser tests). No regressions.

- [ ] **Step 2: shellcheck wrapper + log-round**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && shellcheck skills/check/scripts/bulldozer-round.sh skills/check/scripts/log-round.sh`

Expected: clean (or only warnings unrelated to PR-1 changes).

- [ ] **Step 3: Dogfood the implementation via bulldozer:check**

Invoke `/bulldozer:check standard skills/check/scripts/bulldozer-round.sh` from inside the bulldozer plugin dir. Reviewer should verify the exit-11 contract, exit-table consistency, SKILL.md Step 7 protocol, and the replace-extraction state machine.

Expected: GO within 1-3 rounds. Address any verified findings via TDD pattern (test → fix → test → commit).

- [ ] **Step 4: Push branch and open PR**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
git push -u origin bulldozer/feat/pr1-manual-fallback-discipline
gh pr create --repo A3IO/jaine-plugins --base bulldozer/main \
  --head bulldozer/feat/pr1-manual-fallback-discipline \
  --title "feat(check): PR-1 manual-fallback discipline (exit 11 protocol)" \
  --body "Implements PR-1 of issue #110 roadmap (see PR #113 for spec)..."
```

(Full PR body follows the PR #111 / #112 pattern — summary, items closed, test plan.)

---

## Self-Review

**1. Spec coverage:** Walked each PR-1 requirement from `docs/superpowers/specs/2026-05-28-issue-110-roadmap-design.md`:
- ✓ Exit 11 with verdict=UNKNOWN + manual_extraction_pending=true logged → Task 4
- ✓ `--review-dir PATH` flag for shell-context invocation → Task 1
- ✓ `--mode=replace-extraction ROUND K VERDICT` → Task 2
- ✓ Idempotency guard (refuse double-mutation when flag cleared) → Task 2
- ✓ Strict VERDICT validation (GO or NO-GO only) → Task 2
- ✓ Delta-correct `findings_total` → Task 2
- ✓ Cross-state-isolation regression → Task 7
- ✓ Terminal-round pivot dispatch → Task 6 (SKILL.md prescribes it)
- ✓ SKILL.md exit table + Step 7 flow → Tasks 5+6
- ✓ Tests for all 8 protocol cases per spec → Tasks 2+3+4+7 cover all

**2. Placeholder scan:** None. Each step has full code/commands. PR body in Task 8 Step 4 says "Full PR body follows the PR #111 / #112 pattern" — that's a deliberate handoff to the engineer (we'll match the pattern at PR-create time), not a content placeholder.

**3. Type consistency:**
- `update-state.py` signature: 8 positionals (ROUND VERDICT FINDINGS FIXED FP ARTIFACT DEPTH REVIEWER) for append mode, 3 positionals (ROUND K VERDICT) for replace-extraction mode. Consistent across Tasks 1-3.
- `log-round.sh` signature: 8 positionals + new optional 9th (MANUAL_EXTRACTION_PENDING). Consistent in Task 3 + used in Task 4.
- VERDICT values: "GO" and "NO-GO" strict (Task 2). Wrapper writes "UNKNOWN" for the exit-11 round (Task 4) — that's distinct from the post-replace VERDICT and only valid pre-replace, enforced by the idempotency guard.
- `manual_extraction_pending` column name: consistent across update-state.py (write + clear), log-round.sh (forward via flag), wrapper (request via positional), tests (assert).

No fixes needed.
