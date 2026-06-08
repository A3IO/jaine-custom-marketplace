#!/usr/bin/env python3
"""Emit the bulldozer max-rounds pivot file (B3 extraction, #110).

Usage: emit-pivot.py <round> <max_rounds> <open_findings> <depth> <artifact> <pivot_path> [trigger_reason]

Writes an AskUserQuestion-compatible pivot JSON to <pivot_path> (continue /
restructure / accept-with-TODO). Exits non-zero on bad arity or an unwritable
path; the wrapper maps a non-zero exit (or a missing pivot file) to its
_emit_stop 70 path, which suppresses the exit-10 pivot signal.

The option set is loaded from data/pivot-options.yaml (B4, #110) so a 4th
option is a data edit, not a code change. If that file is missing, unreadable,
PyYAML is absent, or the schema is wrong, _load_options falls back to the
built-in set (with a stderr warning) — the pivot dialog must never fail to
render because the data file drifted. The success-path pivot JSON shape is
asserted byte-identical by the TestB3Characterization golden + TestEmitPivotScript
(so the shipped YAML must match _BUILTIN_OPTIONS); failure-path stderr is not
contracted (the wrapper discards it and emits its own _emit_stop 70 message, so
only the nonzero exit is load-bearing).

CLI is guarded under __main__ so _load_options / _BUILTIN_OPTIONS are importable
for unit testing without triggering the arity check.
"""
import json
import sys
from pathlib import Path

# Built-in fallback option set — kept identical to data/pivot-options.yaml so a
# missing/corrupt data file degrades to the same dialog the file would render.
_BUILTIN_OPTIONS = [
    {
        "label": "continue",
        "description": "Run another round (exceeds max for this depth)",
    },
    {
        "label": "restructure",
        "description": "Pause review, restructure the artifact, re-launch /bulldozer:check",
    },
    {
        "label": "accept-with-TODO",
        "description": "Accept current state, log open findings as project TODOs",
    },
]


def _load_options(cfg=None):
    """Return the pivot option list from data/pivot-options.yaml, falling back
    to _BUILTIN_OPTIONS (with a stderr warning) if the file is missing,
    unreadable, PyYAML is absent, or the schema is wrong. Never raises — the
    pivot dialog must always render.
    """
    if cfg is None:
        cfg = Path(__file__).resolve().parent.parent / "data" / "pivot-options.yaml"
    try:
        import yaml
    except ImportError:
        print("warning: PyYAML missing; using built-in pivot options", file=sys.stderr)
        return _BUILTIN_OPTIONS
    try:
        with open(cfg, encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as exc:
        # R1-F2 (PR-3b dogfood): UnicodeDecodeError is a ValueError subclass, NOT
        # OSError/YAMLError — a non-UTF8 data file would otherwise propagate
        # uncaught and the wrapper would map it to _emit_stop 70, suppressing the
        # very pivot dialog this fallback exists to preserve. Catch it explicitly
        # so a corrupt-encoding data file degrades to the built-in option set.
        print(f"warning: cannot read {cfg} ({exc}); using built-in pivot options",
              file=sys.stderr)
        return _BUILTIN_OPTIONS
    options = data.get("options") if isinstance(data, dict) else None
    if not isinstance(options, list) or not options:
        print(f"warning: {cfg} has no valid 'options' list; using built-in",
              file=sys.stderr)
        return _BUILTIN_OPTIONS
    for opt in options:
        if not (isinstance(opt, dict)
                and isinstance(opt.get("label"), str)
                and isinstance(opt.get("description"), str)):
            print(f"warning: {cfg} option malformed; using built-in", file=sys.stderr)
            return _BUILTIN_OPTIONS
    return options


def main(argv):
    # 7 args = flat pivot (trigger defaults to max_rounds_reached). The optional
    # 8th arg is the trigger reason — B6 (#128) passes "calibrated_nonconvergence"
    # for the exhaustive early-pivot. The default preserves the byte-identical
    # flat-pivot output the golden tests pin (TestEmitPivotScript / B3 golden).
    if len(argv) not in (7, 8):
        print(
            "usage: emit-pivot.py <round> <max_rounds> <open_findings> "
            "<depth> <artifact> <pivot_path> [trigger_reason]",
            file=sys.stderr,
        )
        return 2

    round_num, max_rounds, open_findings, depth, artifact, pivot_path = (
        int(argv[1]), int(argv[2]), int(argv[3]),
        argv[4], argv[5], argv[6],
    )
    trigger_reason = argv[7] if len(argv) == 8 else "max_rounds_reached"
    if trigger_reason == "calibrated_nonconvergence":
        question = (
            f"Exhaustive review not converging by round {round_num} — "
            f"{open_findings} finding(s) open and the last 3 rounds aren't "
            f"shrinking. Pivot now instead of continuing toward round {max_rounds}?"
        )
    else:
        question = (
            f"Reached max rounds ({max_rounds}) without GO — "
            f"{open_findings} finding(s) open. How to proceed?"
        )
    pivot = {
        "trigger": trigger_reason,
        "round": round_num,
        "max_rounds": max_rounds,
        "depth": depth,
        "artifact": artifact,
        "open_findings": open_findings,
        # AskUserQuestion-compatible fields below: caller can pass these
        # directly to the tool without renaming or synthesizing missing keys.
        "question": question,
        "header": "Pivot",  # chip label, ≤12 chars per AskUserQuestion schema
        "multiSelect": False,
        "options": _load_options(),
    }
    with open(pivot_path, "w") as fp:
        json.dump(pivot, fp, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
