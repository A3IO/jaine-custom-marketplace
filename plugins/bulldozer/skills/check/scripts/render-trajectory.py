#!/usr/bin/env python3
"""Render the bulldozer review trajectory line (B3 extraction, #110).

Usage:
  render-trajectory.py <round> <max_rounds> <state_json_path>
      Print the 2-line trajectory summary to stdout (the wrapper redirects it
      to stderr — informational, stdout carries state.json). Exits non-zero on
      bad arity or an unreadable/corrupt state.json; the wrapper maps any
      non-zero here to its _emit_stop 70 path.

  render-trajectory.py --avg-meets <state_json_path> <threshold>
      Single source of the avg-last-3 metric (#133 F1). Print "1" if the mean
      of the last 3 rounds' findings >= threshold, else "0". Reads the SAME
      trajectory the display path plots, so the displayed "avg last 3" and the
      value that gates the B6 calibrated early-pivot can never diverge (before
      #133 bulldozer-round.sh recomputed this in its own inline `python3 -c`).
      Degrades gracefully on an unreadable/corrupt state.json (prints "0",
      exits 0) because this gate runs AFTER the display path has already
      validated state.json this round — a hard fail here would be redundant
      with the display path's exit-70 mapping.

Extracted verbatim from the inline heredoc that lived in bulldozer-round.sh
so the render logic is unit-testable. The success-path STDOUT (the trajectory
summary) MUST stay byte-identical — the black-box TestTrajectoryDisplay tests
assert the through-wrapper path. The failure-path stderr is NOT part of that
contract: on bad arity / corrupt state the script tracebacks (now naming this
file instead of the old heredoc's "<stdin>"), the wrapper discards it and
emits its own _emit_stop 70 message, so only the nonzero exit is load-bearing.
"""
import json
import sys

# The trajectory window the B6 calibrated pivot and the display share (#133 F1).
WINDOW = 3


def _trajectory(history):
    """Per-round findings counts (oldest→newest) from a state.json history list."""
    return [h.get("findings", 0) for h in history]


def _avg_last_n(trajectory, n=WINDOW):
    """Mean of the last n entries; 0.0 when the trajectory is empty."""
    window = trajectory[-n:]
    return sum(window) / len(window) if window else 0.0


# --avg-meets mode (#133 F1): the machine-readable gate the wrapper calls in
# place of its old inline recompute. Detected before the display-arity check
# because `--avg-meets <state> <threshold>` is also 4 argv but argv[1] is a flag.
if len(sys.argv) >= 2 and sys.argv[1] == "--avg-meets":
    if len(sys.argv) != 4:
        print(
            "usage: render-trajectory.py --avg-meets <state_json_path> <threshold>",
            file=sys.stderr,
        )
        sys.exit(2)
    avg_state_path = sys.argv[2]
    threshold = float(sys.argv[3])
    try:
        with open(avg_state_path) as fp:
            avg_history = json.load(fp).get("history", [])
    except (OSError, json.JSONDecodeError):
        print(0)
        sys.exit(0)
    print(1 if _avg_last_n(_trajectory(avg_history)) >= threshold else 0)
    sys.exit(0)

if len(sys.argv) != 4:
    print(
        "usage: render-trajectory.py <round> <max_rounds> <state_json_path>",
        file=sys.stderr,
    )
    sys.exit(2)

round_num = int(sys.argv[1])
max_rounds = int(sys.argv[2])
state_path = sys.argv[3]

with open(state_path) as fp:
    state = json.load(fp)

history = state.get("history", [])
trajectory = _trajectory(history)
last = history[-1] if history else {"verdict": "UNKNOWN", "findings": 0}
last_verdict = last.get("verdict", "UNKNOWN")
last_findings = last.get("findings", 0)

noun = "finding" if last_findings == 1 else "findings"
print(
    f"[bulldozer/check] Round {round_num}/{max_rounds} — "
    f"verdict: {last_verdict} — {last_findings} {noun} open"
)

traj_str = " → ".join(str(f) for f in trajectory)
avg = _avg_last_n(trajectory)
print(f"Trajectory: {traj_str}  (avg last 3: {avg:.1f})")
