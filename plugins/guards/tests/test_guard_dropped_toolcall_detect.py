#!/usr/bin/env python3
"""Test harness for guard-dropped-toolcall-detect.py (Stop/SubagentStop hook).

Runs the real detector as a subprocess, feeding it Stop-hook stdin JSON and a
temp JSONL transcript. Asserts on whether it BLOCKS (stdout carries a
``decision:block`` to force a re-emit) or ALLOWS (empty stdout, exit 0).

Covers: the real drop signatures (court/call/county + namespaceless tags), the
loop guard (``stop_hook_active`` true → never block), and negatives that must NOT
false-positive (clean prose, the SAME tag quoted in a code fence / inline code,
the correctly-namespaced antml: form).

Run: python3 test_guard_dropped_toolcall_detect.py   (exit 0 = all pass)
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

DETECT = str(Path(__file__).parent.parent / "hooks" / "guard-dropped-toolcall-detect.py")


def _transcript(assistant_text: str) -> str:
    """Write a minimal JSONL transcript whose last assistant message has the text."""
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n")
    fh.write(json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
    }) + "\n")
    fh.close()
    return fh.name


def run(assistant_text: str, stop_hook_active: bool = False) -> bool:
    """Return True if the hook BLOCKED (forced a re-emit), False if it allowed stop."""
    tp = _transcript(assistant_text)
    stdin = json.dumps({
        "transcript_path": tp,
        "stop_hook_active": stop_hook_active,
        "session_id": "test",
        "hook_event_name": "Stop",
    })
    proc = subprocess.run([sys.executable, DETECT], input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    out = proc.stdout.strip()
    if not out:
        return False
    try:
        return json.loads(out).get("decision") == "block"
    except json.JSONDecodeError:
        return False


# Real drop shapes (from forensics) — every one MUST block.
DROP_BASH = 'court\n<invoke name="Bash">\n<parameter name="command">ls</parameter>\n</invoke>'
DROP_READ = 'call\n<invoke name="Read">\n<parameter name="file_path">/x</parameter>\n</invoke>'
DROP_COUNTY = 'county\n<invoke name="Edit">\n<parameter name="file_path">/x</parameter>\n</invoke>'
DROP_NO_STRAY = '<invoke name="Bash">\n<parameter name="command">git status</parameter>\n</invoke>'
DROP_FUNC = '<function_calls>\n<invoke name="Bash">\n</invoke>'

# Negatives — must NOT block.
CLEAN = "Готово. Все тесты прошли, изменения закоммичены."
DOC_FENCED = "Сигнатура дропа:\n```\ncourt\n<invoke name=\"Bash\">\n</invoke>\n```\nnamespace пропал."
DOC_INLINE = 'Баг даёт `<invoke name="Bash">` без antml: префикса в тексте.'
CORRECT_NS = 'Я вызову инструмент.'  # real calls are structured tool_use, never text

# (label, assistant_text, stop_hook_active, expect_block)
CASES = [
    ("drop-bash", DROP_BASH, False, True),
    ("drop-read", DROP_READ, False, True),
    ("drop-county", DROP_COUNTY, False, True),
    ("drop-no-stray", DROP_NO_STRAY, False, True),
    ("drop-function_calls", DROP_FUNC, False, True),
    # loop guard: even a real drop must NOT block when we already forced a retry
    ("loop-guard-on-drop", DROP_BASH, True, False),
    # negatives
    ("clean-prose", CLEAN, False, False),
    ("doc-fenced", DOC_FENCED, False, False),
    ("doc-inline", DOC_INLINE, False, False),
    ("correct-ns", CORRECT_NS, False, False),
]


def main() -> int:
    failed = 0
    for label, text, sha, expect in CASES:
        got = run(text, stop_hook_active=sha)
        if got != expect:
            failed += 1
            print(f"FAIL: {label!r} -> blocked={got}, expected {expect}")
    total = len(CASES)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
