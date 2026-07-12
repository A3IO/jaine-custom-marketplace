#!/usr/bin/env python3
"""Update .bulldozer/state.json after a round completes."""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone


VALID_REPLACE_VERDICTS = {"GO", "NO-GO"}


def _session_token():
    """Token-normalized 8-char session id (canonical grammar rule) or NA."""
    import re
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
    return sid[:8] or "NA"


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
    # Bug #2: if multiple entries share round_num (e.g. a previously cleared
    # entry + a new pending entry from a wrapper re-run), first-match-wins
    # would pick the cleared one and falsely error "already cleared". Prefer
    # the entry whose manual_extraction_pending is strictly True.
    candidates = [e for e in history if e.get("round") == round_num]
    if not candidates:
        print(f"error: round {round_num} not found in {state_file} history", file=sys.stderr)
        return 1
    # Bug #3: strict `is True` identity comparison everywhere. Loose truthiness
    # would let string "true" satisfy the gate and string "false" trigger the
    # mutation. Canonical schema is Python bool True/False; anything else is
    # CORRUPT and must fail closed.
    pending_candidates = [e for e in candidates if e.get("manual_extraction_pending") is True]
    if not pending_candidates:
        # All matching entries have the flag cleared / absent / non-bool.
        # Surface what we actually saw so the operator can diagnose corruption
        # vs idempotency vs schema drift in one glance.
        seen = [repr(e.get("manual_extraction_pending")) for e in candidates]
        print(
            f"error: round {round_num} manual_extraction_pending flag must be exactly True "
            f"to replace-extract; got {seen!r} (replace-extraction is idempotent and "
            f"refuses double-mutation; non-bool values are corrupt)",
            file=sys.stderr,
        )
        return 1
    # R1-F2 (R2 dogfood, dup-pending edge): when duplicate round entries exist
    # with manual_extraction_pending=true (rare corruption: test harness
    # double-invocation or manual log-round call), clear them ONE AT A TIME
    # across multiple replace-extraction calls.
    # Rationale: clearing all at once would multiply findings_total by the
    # duplicate count, double-counting K. Sequential clearing keeps delta
    # arithmetic correct (each clear: +K - old_findings_of_that_entry = +K -0
    # = +K). WARN so the operator knows reruns are required to clear the rest.
    if len(pending_candidates) > 1:
        print(
            f"warning: round {round_num} has {len(pending_candidates)} pending entries — "
            f"clearing the first; rerun update-state.py --mode=replace-extraction with the same args "
            f"after the next bulldozer-round.sh invocation to clear remaining pendings",
            file=sys.stderr,
        )
    target = pending_candidates[0]
    # Bug #4: target.get("findings", 0) returns the actual value (None) when
    # the key exists with null. `k - None` → TypeError, propagating past the
    # try/except below (which only catches OSError on write). Validate type
    # explicitly and emit a clean error instead of a traceback.
    old_findings = target.get("findings", 0)
    if not isinstance(old_findings, int) or isinstance(old_findings, bool):
        print(
            f"error: round {round_num} has invalid 'findings' type "
            f"(expected int, got {type(old_findings).__name__})",
            file=sys.stderr,
        )
        return 1
    # Same defensive check for state-level findings_total: null/string would
    # otherwise crash the delta arithmetic two lines below.
    findings_total = state.get("findings_total", 0)
    if not isinstance(findings_total, int) or isinstance(findings_total, bool):
        print(
            f"error: state findings_total has invalid type "
            f"(expected int, got {type(findings_total).__name__})",
            file=sys.stderr,
        )
        return 1
    target["findings"] = k
    target["verdict"] = verdict
    target["manual_extraction_pending"] = False
    state["findings_total"] = findings_total + (k - old_findings)
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
    _log_reconciled_line(round_num, k, verdict, state.get("artifact") or "",
                         target.get("session"))
    print(json.dumps(state, indent=2))
    return 0


def _log_reconciled_line(round_num: int, k: int, verdict: str, artifact: str = "",
                         round_session: "str | None" = None) -> None:
    """#322 D6: the round's bulldozer.log line stays FROZEN at verdict=UNKNOWN/
    findings=0 (append-only audit trail) — append a CORRECTION line instead so a
    naive log miner can detect + supersede the stale entry. Best-effort.
    round_session = the session recorded on the ORIGINAL round entry (#327 r4):
    a reconciliation from a resumed session must join to the frozen line's key,
    not the current environment's. Entries created BEFORE session persistence
    shipped have no session key — the current-session fallback is the best
    available proxy (reconciliation almost always runs in the round's own
    session, right after the wrapper's exit 11) and is accepted (#327 r5)."""
    try:
        # canonical helper (lib/bulldozer_log.py): sanitization, rotation, one
        # writer for the stable log (Copilot #327). append_line never raises.
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
        from bulldozer_log import append_line
        lf = Path(os.environ.get("BULLDOZER_LOG")
                  or Path.home() / ".claude" / "hooks" / "bulldozer.log")
        sid = round_session if isinstance(round_session, str) and round_session else None
        append_line(lf, "reconciled", session=sid, round=round_num,
                    artifact=artifact, findings=k, verdict=verdict)
    except Exception as e:
        try:  # the warning itself is best-effort — a broken stderr must not turn a
            print(f"warning: could not write reconciled line: {e}", file=sys.stderr)
        except Exception:  # successful reconcile into a false failure (#327 r4)
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Update .bulldozer/state.json after a round completes.",
    )
    parser.add_argument("--review-dir", type=Path, default=None,
                        help="Target review directory (overrides BULLDOZER_REVIEW_DIR env var)")
    parser.add_argument("--mode", choices=["append", "replace-extraction"],
                        default="append",
                        help="append (default): standard add-round behavior. "
                             "replace-extraction: update existing history entry's "
                             "findings/verdict, clear manual_extraction_pending flag, "
                             "delta-correct findings_total.")
    parser.add_argument("--manual-extraction-pending", action="store_true",
                        help="Mark the new history entry with manual_extraction_pending=true "
                             "(append mode only; cleared via --mode=replace-extraction)")
    parser.add_argument("positional", nargs="*",
                        help="ROUND VERDICT FINDINGS FIXED [FP] [ARTIFACT] [DEPTH] [REVIEWER]")
    args = parser.parse_args()

    pos = args.positional

    # --review-dir flag wins over env var so Claude's shell-context invocation
    # of replace-extraction targets the per-review state.json explicitly.
    # Env var stays as default for the wrapper's log-round.sh subprocess path.
    if args.review_dir is not None:
        # R2-F2 (R2 dogfood): canonicalize to absolute path. Protects against
        # cwd shifts between argparse parse and downstream file ops. Caller
        # is STILL responsible for providing a path that resolves to the
        # correct review dir (e.g. Claude must use the absolute path from
        # the wrapper's exit-11 stderr block, not a relative path from a
        # Bash tool cwd that may not survive across tool invocations). See
        # SKILL.md Step 7 step 4. .resolve() works on non-existent paths
        # (PEP 428) — only the parent components need to exist, and even
        # then resolves lexically when strict=False (the default).
        state_dir = args.review_dir.resolve()
    else:
        state_dir = Path(os.environ.get("BULLDOZER_REVIEW_DIR", ".bulldozer"))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create {state_dir}: {e}", file=sys.stderr)
        sys.exit(1)
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

    verdict = pos[1]
    artifact = pos[5] if len(pos) > 5 else ""
    depth = pos[6] if len(pos) > 6 else "standard"
    reviewer = pos[7] if len(pos) > 7 else "codex/unknown"

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError as e:
            print(f"error: {state_file} corrupted: {e}", file=sys.stderr)
            print(f"hint: backup and remove {state_file} to start fresh", file=sys.stderr)
            sys.exit(1)
        state.setdefault("findings_total", 0)
        state.setdefault("fixed_total", 0)
        state.setdefault("false_positives", 0)
        state.setdefault("history", [])
        for key in ("findings_total", "fixed_total", "false_positives"):
            if not isinstance(state.get(key), int):
                print(f"error: {state_file} has invalid {key} (expected int, got {type(state.get(key)).__name__})", file=sys.stderr)
                print(f"hint: backup and remove {state_file} to start fresh", file=sys.stderr)
                sys.exit(1)
        if not isinstance(state.get("history"), list):
            print(f"error: {state_file} has invalid history (expected list)", file=sys.stderr)
            sys.exit(1)
    else:
        state = {
            "round": 0,
            "artifact": artifact,
            "depth": depth,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "reviewer": reviewer,
            "findings_total": 0,
            "fixed_total": 0,
            "false_positives": 0,
            "history": []
        }

    # #314: fixed/fp are dispositions of the PREVIOUS round's findings
    # (SKILL.md Step 6 sets BULLDOZER_FIXED when launching round N+1), so the
    # advisory desync check compares against the previous round's entry —
    # comparing against the CURRENT round's findings fired on every healthy
    # converging review (findings 3→2 with 3 fixes). Baseline = the HIGHEST
    # round < round_num, latest duplicate of it — NOT history[-1] (a re-run
    # of round N appends a second round-N entry, #330 r1) and NOT the last
    # appended prior-round entry (a rerun of an OLDER round lands after newer
    # ones — history rounds 1,2,3,2 — and would shadow round 3, #330 r2).
    # No such entry (round 1 / fresh state) or a non-int baseline
    # (legacy/corrupt entry) → nothing to compare, skip silently.
    prev = None
    for e in state["history"]:
        if (isinstance(e, dict)
                and isinstance(e.get("round"), int)
                and not isinstance(e.get("round"), bool)
                and e["round"] < round_num
                and (prev is None or e["round"] >= prev["round"])):
            prev = e
    prev_findings = prev.get("findings") if prev is not None else None
    if (isinstance(prev_findings, int) and not isinstance(prev_findings, bool)
            and fixed + fp > prev_findings):
        print(f"warning: fixed+fp ({fixed + fp}) exceeds previous round's findings ({prev_findings})", file=sys.stderr)

    state["round"] = round_num
    state["findings_total"] += findings
    state["fixed_total"] += fixed
    state["false_positives"] += fp
    if artifact:
        state["artifact"] = artifact
    history_entry = {
        "round": round_num,
        "verdict": verdict,
        "findings": findings,
        "fixed": fixed,
        "fp": fp,
        "session": _session_token(),  # #327 r4: the reconciled line reuses the ROUND's session
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if args.manual_extraction_pending:
        history_entry["manual_extraction_pending"] = True
    state["history"].append(history_entry)

    tmp = state_file.with_suffix('.json.tmp')
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
        sys.exit(1)

    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
