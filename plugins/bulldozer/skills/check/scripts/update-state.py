#!/usr/bin/env python3
"""Update .bulldozer/state.json after a round completes."""
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone


def main():
    if len(sys.argv) < 5:
        print("usage: update-state.py ROUND VERDICT FINDINGS FIXED [FP] [ARTIFACT] [DEPTH] [REVIEWER]", file=sys.stderr)
        sys.exit(1)

    try:
        round_num = int(sys.argv[1])
        findings = int(sys.argv[3])
        fixed = int(sys.argv[4])
        fp = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    except ValueError as e:
        print(f"error: numeric argument expected: {e}", file=sys.stderr)
        sys.exit(1)

    verdict = sys.argv[2]
    artifact = sys.argv[6] if len(sys.argv) > 6 else ""
    depth = sys.argv[7] if len(sys.argv) > 7 else "standard"
    reviewer = sys.argv[8] if len(sys.argv) > 8 else "codex/gpt-5.5"

    state_dir = Path(os.environ.get("BULLDOZER_REVIEW_DIR", ".bulldozer"))
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create {state_dir}: {e}", file=sys.stderr)
        sys.exit(1)
    state_file = state_dir / "state.json"

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError as e:
            print(f"error: {state_file} corrupted: {e}", file=sys.stderr)
            print(f"hint: backup and remove {state_file} to start fresh", file=sys.stderr)
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

    state["round"] = round_num
    state["findings_total"] += findings
    state["fixed_total"] += fixed
    state["false_positives"] += fp
    if artifact:
        state["artifact"] = artifact
    state["history"].append({
        "round": round_num,
        "verdict": verdict,
        "findings": findings,
        "fixed": fixed,
        "fp": fp,
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

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
