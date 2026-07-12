#!/usr/bin/env python3
"""log_skill_invoke — UserPromptSubmit invoke-logger for the bulldozer plugin (#318).

Replaces three inline-echo hooks.json entries whose `matcher` regexes were silently
ignored on UserPromptSubmit (matchers filter tool names, not prompts) — every prompt
in every project wrote an invoke line into all three logs (~95% of log volume).

The filter lives HERE instead: CC delivers the hook JSON on stdin; only a prompt that
actually starts with /bulldozer:check|look|consult logs one line, in the ORIGINAL
line format, to the original per-skill log.

Fail-safe: a logger must never block a prompt — any failure exits 0 silently.
Log dir override for test isolation: BULLDOZER_INVOKE_LOG_DIR (default ~/.claude/hooks).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
try:
    from bulldozer_log import append_line  # canonical writer (Copilot #327)
except Exception:  # incomplete/stale layout: the hook must NEVER block a prompt (#327 r7)
    append_line = None

SKILL_RE = re.compile(r"^/bulldozer:(check|look|consult|drive)\b")

# skill -> (log filename, event, ordered payload kv builder). session= is added
# by the canonical writer itself (env-derived, token-normalized — #322 B8).
FORMATS = {
    "check": ("bulldozer.log", "invoke",
              lambda project: {"round": 0, "artifact": "", "verdict": "", "findings": 0,
                               "fixed": 0, "fp": 0, "reviewer": "", "project": project}),
    "look": ("bulldozer-look.log", "look-invoke",
             lambda project: {"url": "", "project": project}),
    "consult": ("bulldozer-consult.log", "consult-invoke",
                lambda project: {"project": project}),
    "drive": ("bulldozer-drive.log", "drive-invoke",
              lambda project: {"project": project}),
}


def resolve_project(cwd):
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,  # comfortably under the 5s hooks.json timeout — fail-safe exit must win
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return cwd


def main():
    data = json.load(sys.stdin)
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return
    m = SKILL_RE.match(prompt)
    if not m:
        return
    if append_line is None:
        print("warning: bulldozer_log helper unavailable — invoke line dropped", file=sys.stderr)
        return
    log_name, event, payload = FORMATS[m.group(1)]
    cwd = data.get("cwd") or os.getcwd()
    log_dir = os.environ.get("BULLDOZER_INVOKE_LOG_DIR") or os.path.expanduser(
        "~/.claude/hooks"
    )
    log_path = os.path.join(log_dir, log_name)
    if m.group(1) == "drive" and os.environ.get("BULLDOZER_DRIVE_LOG"):
        # one documented channel, one override — every drive producer honors it (#328 r3)
        log_path = os.environ["BULLDOZER_DRIVE_LOG"]
    append_line(log_path, event, **payload(resolve_project(cwd)))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-safe: never block the prompt over a logging failure
    sys.exit(0)
