#!/usr/bin/env python3
"""Test harness for guard-process-kill-detect.py.

Runs the real detector as a subprocess and checks its exit code (2 = prompt the
confirm dialog, 0 = allow). Covers the incident shape (literal-PID kill, by-name
pkill/killall), the safe own-process forms that must NOT false-positive (``$!`` /
``$VAR`` / ``%job`` / ``-0``), and quoted/echoed mentions.

Run: python3 test_guard_process_kill_detect.py   (exit 0 = all pass)
"""
import subprocess
import sys
from pathlib import Path

# plugin layout: detector lives in ../hooks/ (sibling of tests/)
DETECT = str(Path(__file__).parent.parent / "hooks" / "guard-process-kill-detect.py")


def detect(cmd: str) -> int:
    return subprocess.run([sys.executable, DETECT, cmd]).returncode


# (command, expected_exit) — 2 = should prompt the dialog, 0 = should allow.
CASES = [
    # --- DANGEROUS: literal-PID kill (the incident shape) -> block ---
    ("kill 1234", 2),
    ("kill -9 1234", 2),
    ("kill -TERM 47268", 2),
    ("kill -KILL 43515", 2),
    ("kill -- 1234", 2),
    ("kill 1234 5678", 2),                 # multiple literal PIDs
    ("kill -9 -1", 2),                      # -1 = signal EVERY process (catastrophic)
    ("kill -s KILL 1234", 2),              # signal via -s, then a literal PID
    ("/bin/kill 1234", 2),                 # absolute path still resolves to `kill`
    ("ps aux | grep agy; kill 47268", 2),  # kill in a command chain
    ("kill $(pgrep agy)", 2),              # command-substitution target — the incident shape (dynamic PID lookup)
    ("kill $(cat /tmp/pidfile)", 2),       # any $(...) target → confirm (resolves at runtime, not statically own)
    # --- DANGEROUS: by-name pkill/killall (unscopable, hits the user's too) -> block ---
    ("pkill agy", 2),
    ("pkill -f agy", 2),
    ("pkill -9 firefox", 2),
    ('pkill -f -- "--user-data-dir=/tmp/x"', 2),
    ("killall agy", 2),
    ("killall -9 Chrome", 2),
    # --- SAFE: the agent's OWN, freshly-spawned processes -> allow ---
    ("kill $!", 0),                         # last background pid the agent started
    ("kill -9 $AGY_PID", 0),               # a pid the agent saved in a var
    ('kill "$PID"', 0),                     # quoted var, same
    ("kill %1", 0),                         # job spec
    ("kill -0 1234", 0),                   # existence check (signal 0), no kill
    ("kill -s 0 1234", 0),                 # existence check via -s 0
    ("kill --signal=0 1234", 0),           # existence check, joined --signal=0 form
    ("kill -s0 1234", 0),                  # existence check, joined -s0 form
    ("kill", 0),                            # bare usage error, harmless
    ("pkill -l", 0),                        # list signals, no kill
    ("killall -l", 0),                      # list, no kill
    # --- SAFE: quoted / echoed mentions, not a real kill command -> allow ---
    ('grep "kill 1234" notes.txt', 0),
    ("echo kill 1234", 0),
    ("echo 'pkill agy'", 0),
    ("git commit -m 'kill the flaky test 1234'", 0),
    # --- #302: transparent wrapper prefixes reach the real kill -> block ---
    ("sudo kill 1234", 2),
    ("command kill 1234", 2),
    ("sudo -u root pkill -f agy", 2),      # arg-taking -u eats its arg
    ("nohup killall Chrome", 2),
    ("VAR=1 kill 1234", 2),                # leading env assignment no longer hides kill
    # --- #302: wrapper-prefixed but SAFE -> allow ---
    ("sudo kill $!", 0),                   # own-process target stays allowed through the wrapper
    ("echo sudo kill 1234", 0),            # mention behind echo, not a wrapper chain
    ("xargs --version kill 1234", 0),      # help/version terminates the wrapper — kill never runs
    ("sudo --help kill 1234", 0),
    ("sudo -ll kill 1234", 0),             # clustered listing — kill never runs
    ("sudo --validate kill 1234", 0),      # validate mode runs nothing
    ("env -0 kill 1234", 0),               # env print-mode errors out — kill never runs
    ("sudo -K kill 1234", 0),              # -K may not be specified with a command
    ("sudo -k kill 1234", 2),              # but lowercase -k RUNS the command
]


# #300 dedup contract: the kill detector must REUSE git_lexer's base trio (one lexer,
# one place to fix), not carry byte-identical local copies. `is`-identity pins the
# dedup against a future re-fork — a local redefinition breaks it immediately.
_LEXER_TRIO = ("tokenize", "split_segments", "strip_redirects")


def dedup_contract_failures() -> int:
    import importlib.util

    hooks = Path(__file__).parent.parent / "hooks"
    sys.path.insert(0, str(hooks))
    import git_lexer

    spec = importlib.util.spec_from_file_location(
        "kill_detect", hooks / "guard-process-kill-detect.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    failed = 0
    for name in _LEXER_TRIO:
        if getattr(mod, name) is not getattr(git_lexer, name):
            failed += 1
            print(f"FAIL: detector {name} is a local copy, not git_lexer.{name} (#300)")
    return failed


def main() -> int:
    failed = 0
    for cmd, expected in CASES:
        got = detect(cmd)
        ok = got == expected
        if not ok:
            failed += 1
            print(f"FAIL: {cmd!r} -> exit {got}, expected {expected}")
    failed += dedup_contract_failures()
    total = len(CASES) + len(_LEXER_TRIO)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
