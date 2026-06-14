#!/usr/bin/env python3
"""Test harness for guard-git-destructive-detect.py.

Runs the real detector as a subprocess and checks its exit code (2 = block,
0 = allow). Covers the original work-discarding cases, the safe cases that must
NOT false-positive, and the new safety-bypass cases migrated from
block-safety-bypass.sh (--no-verify, -c core.hooksPath, +refspec, force, skip-env).

Run: python3 test_guard_git_destructive_detect.py   (exit 0 = all pass)
"""
import subprocess
import sys
from pathlib import Path

# plugin layout: detector lives in ../hooks/ (sibling of tests/), not the test's own dir
DETECT = str(Path(__file__).parent.parent / "hooks" / "guard-git-destructive-detect.py")


def detect(cmd: str) -> int:
    return subprocess.run([sys.executable, DETECT, cmd]).returncode


# (command, expected_exit) — 2 = should block, 0 = should allow.
CASES = [
    # --- original: work-discarding (block) ---
    ("git reset --hard", 2),
    ("git reset --hard HEAD~1", 2),
    ("git restore file.py", 2),
    ("git clean -fd", 2),
    ("git stash drop", 2),
    ("git stash clear", 2),
    ("git checkout -- file.py", 2),
    ("git checkout file.py", 2),
    # --- original: safe (allow) ---
    ("git status", 0),
    ("git log --oneline", 0),
    ("git commit -m 'feat: x'", 0),
    ("git reset --soft HEAD~1", 0),
    ("git stash", 0),
    ("git stash pop", 0),
    ("git checkout -b feat/x", 0),
    ("git restore --staged file.py", 0),
    ('grep "git reset --hard" notes.txt', 0),
    ("git clean -n", 0),
    ("echo git commit --no-verify", 0),   # mention in echo, not a real git command
    # --- NEW: safety bypass (block) ---
    ("git commit --no-verify -m x", 2),
    ("git commit '--no-verify' -m x", 2),       # #1 quoted flag — shlex strips quotes
    ('git commit "--no-verify" -m x', 2),
    ("git push --no-verify origin main", 2),
    ("git -c core.hooksPath=/dev/null commit -m x", 2),   # #2 hooks disabled via -c
    ("git -c core.hooksPath= commit -m x", 2),
    ("git commit -n -m x", 2),                   # #3 short -n == --no-verify (commit only)
    ("git commit -nm x", 2),                     # bundled
    ("git push origin +feat/x", 2),              # #4 force via +refspec
    ("git push --force origin main", 2),
    ("git push --force-with-lease", 2),
    ("git push -f", 2),
    ("JAINE_SKIP_PUSH_GUARD=1 git push", 2),     # skip-env before git
    # --- NEW: bypass-shaped but SAFE (allow) ---
    ("git push -n", 0),                          # push -n = --dry-run, NOT no-verify
    ("git merge -n origin/x", 0),                # merge -n = --no-stat, NOT no-verify
    ("grep -n no-verify file", 0),               # grep -n must not trigger
    ("git log -n 5", 0),                         # log -n = count
    ("echo 'git commit --no-verify'", 0),        # quoted mention only
]


def main() -> None:
    failures = []
    for cmd, expected in CASES:
        got = detect(cmd)
        ok = (got == expected) or (expected == 2 and got == 2) or (expected == 0 and got != 2)
        # normalise: detector returns 2 for block, anything-else for allow
        block = got == 2
        want_block = expected == 2
        if block != want_block:
            failures.append((cmd, "BLOCK" if want_block else "ALLOW", "BLOCK" if block else "ALLOW"))
    for cmd, want, got in failures:
        print(f"FAIL  want={want:5} got={got:5}  {cmd}")
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
