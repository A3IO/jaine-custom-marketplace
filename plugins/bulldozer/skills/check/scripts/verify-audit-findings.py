#!/usr/bin/env python3
"""E1 consistency-audit verifier (#94). Quote-presence only — anti-hallucination.

Reads a findings JSON (the consistency-auditor agent's output, written by Claude),
keeps only findings whose cited quote(s) are verbatim-present where claimed, and
writes the survivors. The ONE deterministic guarantee: a fabricated / absent quote
is dropped. It does NOT judge whether the cited text is a real defect of its class
— that is Claude's semantic call on the survivors.

Usage:
  verify-audit-findings.py --findings <in.json> --out <out.json> --project-root <dir>

Robust fail-open: the agent is an LLM, so its output can be valid JSON with wrong
shapes (top-level array, non-string quote/file, non-dict anchor, out-of-contract
class). Any such malformed input DROPS the offending finding (or the whole batch)
and exits 0 — a misbehaving auditor degrades to "no pre-clean this round", never
crashes the round.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# The four-class contract — mirror of skills/check/data/e1-evidence-schema.json
# (anchor_by_class keys). Drift-guarded by
# tests/test_verify_audit_findings.py::test_known_classes_survive_drift_guard.
VALID_CLASSES = frozenset({
    "dead_ref", "internal_contradiction", "cross_spec_drift", "stale_term",
})


def _present(text, quote):
    """quote is a non-empty (after strip) string verbatim-present (fixed-string) in text."""
    return isinstance(quote, str) and quote.strip() != "" and quote in text


def _read(root, rel):
    """Read rel under root, or None.

    Drops non-string / empty rel, and any path that resolves OUTSIDE project-root
    (absolute path or ../ traversal) — the verifier only ever reads the reviewed
    project. Both sides are resolved so symlinks and macOS /var↔/private don't leak.
    """
    if not isinstance(rel, str) or not rel:
        return None
    root_p = Path(root).resolve()
    try:
        target = (root_p / rel).resolve()
        target.relative_to(root_p)  # ValueError if outside root (abs path or ../)
    except (ValueError, OSError):
        return None
    try:
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def survives(finding, root):
    """True iff the finding's class is in-contract AND every cited quote is
    verbatim-present where claimed. Never raises on malformed field types."""
    cls = finding.get("class")
    # isinstance guard first: a non-string (e.g. list/dict) class is unhashable and
    # would raise TypeError in the `in VALID_CLASSES` membership test.
    if not isinstance(cls, str) or cls not in VALID_CLASSES:
        return False
    text = _read(root, finding.get("file", ""))
    quote = finding.get("quote", "")
    anchor = finding.get("anchor")
    if not isinstance(anchor, dict):
        anchor = {}
    if text is None or not _present(text, quote):
        return False
    if cls == "internal_contradiction":
        quote_b = anchor.get("quote_b", "")
        return quote_b != quote and _present(text, quote_b)
    if cls == "cross_spec_drift":
        other_text = _read(root, anchor.get("other_file", ""))
        return other_text is not None and _present(other_text, anchor.get("other_quote", ""))
    # dead_ref + stale_term: quote-presence (GATE-A) is the whole deterministic check;
    # whether `ref` resolves / the term is stale-vs-intentional is Claude's judgment.
    return True


def _warn(msg):
    """Best-effort stderr diagnostic — a write failure must never break the
    fail-open exit-0 contract (broken-pipe stderr reproduced exit 120 with no
    survivors file, PR #339)."""
    try:
        print(msg, file=sys.stderr)
        sys.stderr.flush()
    except (OSError, ValueError):
        # Swap in devnull: a caught EPIPE leaves the buffer dirty, and the
        # interpreter's SHUTDOWN flush would re-raise it → exit 120 anyway.
        try:
            sys.stderr = open(os.devnull, "w")
        except OSError:
            pass


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--findings", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-root", required=True)
    args = ap.parse_args(argv)

    try:
        data = json.loads(Path(args.findings).read_text(encoding="utf-8"))
        findings = data.get("findings", []) if isinstance(data, dict) else []
        if not isinstance(findings, list):
            findings = []
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        findings = []  # fail-open (UnicodeDecodeError = non-UTF-8 findings file)

    # #184: a string anchor on an anchor-requiring class is a guaranteed drop
    # (survives() coerces it to {} and the required sub-fields come up empty).
    # The drop stays fail-open, but it must be VISIBLE — the incident hid 5
    # verbatim-present findings behind this and the audit looked clean.
    for f in findings:
        if isinstance(f, dict) and f.get("class") in ("internal_contradiction", "cross_spec_drift") \
                and not isinstance(f.get("anchor"), dict):
            _warn("WARNING: {} (class {}) will be dropped: anchor must be a dict with "
                  "per-class sub-fields, got {} — see "
                  "skills/check/data/e1-evidence-schema.json (anchor_by_class)".format(
                      f.get("id", "<no id>"), f.get("class"), type(f.get("anchor")).__name__))

    survivors = [f for f in findings if isinstance(f, dict) and survives(f, args.project_root)]
    Path(args.out).write_text(json.dumps({"findings": survivors}, indent=2), encoding="utf-8")
    _log_effectiveness(len(findings), len(survivors), args.project_root)
    return 0


def _log_effectiveness(proposed, survived, project):
    """#322 D7: one durable line per E1 pre-clean — auditor effectiveness
    (proposed vs quote-verified survivors) becomes minable. Best-effort: a
    logging failure must never break this script's fail-open exit-0 contract."""
    try:
        # canonical helper (lib/bulldozer_log.py): sanitization, rotation, one
        # writer for the stable log (Copilot #327). append_line never raises.
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
        from bulldozer_log import append_line
        lf = os.environ.get("BULLDOZER_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer.log")
        append_line(lf, "audit", proposed=proposed, survived=survived, project=project)
    except Exception:
        print("warning: bulldozer_log helper unavailable — audit line dropped",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
