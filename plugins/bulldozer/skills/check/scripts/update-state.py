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

    round_num = int(sys.argv[1])
    verdict = sys.argv[2]
    findings = int(sys.argv[3])
    fixed = int(sys.argv[4])
    fp = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    artifact = sys.argv[6] if len(sys.argv) > 6 else ""
    depth = sys.argv[7] if len(sys.argv) > 7 else "standard"
    reviewer = sys.argv[8] if len(sys.argv) > 8 else "codex/gpt-5.5"

    state_dir = Path(os.environ.get("BULLDOZER_REVIEW_DIR", ".bulldozer"))
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "state.json"

    if state_file.exists():
        state = json.loads(state_file.read_text())
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

    state_file.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state, indent=2))

if __name__ == "__main__":
    main()
