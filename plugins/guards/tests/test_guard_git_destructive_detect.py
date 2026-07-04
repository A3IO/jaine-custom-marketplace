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
    ("JAINE_SKIP_PUSH_GUARD+=1 git push", 2),    # skip-env in += append form (#297)
    # --- NEW: bypass-shaped but SAFE (allow) ---
    ("git push -n", 0),                          # push -n = --dry-run, NOT no-verify
    ("git merge -n origin/x", 0),                # merge -n = --no-stat, NOT no-verify
    ("grep -n no-verify file", 0),               # grep -n must not trigger
    ("git log -n 5", 0),                         # log -n = count
    ("echo 'git commit --no-verify'", 0),        # quoted mention only
    # --- #294: global options before the sub-command must not hide it (block) ---
    ("git -C /tmp/x reset --hard", 2),
    ("git -C /tmp/x stash drop", 2),
    ("git -C /tmp/x checkout -f", 2),
    ("git -C /tmp/x restore file.py", 2),
    ("git -c k=v -C p reset --hard", 2),         # mixed globals, each with its argument
    ("git --git-dir=/p/.git reset --hard", 2),   # --opt=value form
    ("git --git-dir /p/.git reset --hard", 2),   # --opt value form
    ("git --work-tree=/p checkout -f", 2),
    ("git --work-tree /p checkout -f", 2),
    ("git --namespace ns reset --hard", 2),
    ("git -C /a -C /b reset --hard", 2),         # same option twice
    ("git --no-pager -C /p reset --hard", 2),    # argless flag mixed with -C
    ("git -P -C /p clean -fd", 2),               # short argless flag mixed with -C
    ("git -C /p commit --no-verify", 2),         # bypass class through -C
    ("git -C /p push --force", 2),
    ("git --git-dir=/p/.git push -f", 2),
    ("git -C /p -c core.hooksPath=/dev/null commit -m x", 2),  # hooksPath still caught after -C
    ("FOO=1 git -C /p push --force", 2),         # env prefix + global option
    # --- #294: global options on SAFE commands must not false-positive (allow) ---
    ("git -C /tmp/x status", 0),
    ("git -C /tmp/x log --oneline", 0),
    ("git -c user.name=x commit -m x", 0),       # plain -c value, not hooksPath
    ("git -C /p stash", 0),                      # stash push is safe
    ("git -C /p stash pop", 0),
    ("git -C /p push -n", 0),                    # dry-run stays dry behind -C
    ("git -C /p reset --soft HEAD~1", 0),
    ("git -C /p checkout -b feat/x", 0),
    ("git -C /p restore --staged file.py", 0),
    # print-and-exit globals: real git prints and exits, the "sub-command" after
    # them never runs (verified live: `git --html-path status` runs no status)
    ("git --exec-path reset --hard", 0),
    ("git --html-path reset --hard", 0),
    ("git --man-path reset --hard", 0),
    ("git --info-path reset --hard", 0),
    ("git --version reset --hard", 0),
    ("git -h reset --hard", 0),
    ("git --help reset --hard", 0),
    ("git -h commit --no-verify", 0),            # inert for the bypass class too
    ("git -C reset --hard", 0),                  # -C consumes "reset" as its path; git errors, runs nothing
    ("git -C /p -c", 0),                         # trailing option without argument — no crash, allow
    # --- red-team round (codex): case-insensitive config keys + --config-env ---
    ("git -c CORE.HOOKSPATH=/dev/null commit -m x", 2),   # git config keys are case-insensitive
    ("git -c core.hookspath= commit -m x", 2),
    ("git --config-env foo.bar=HP reset --hard", 2),      # separated form hides the sub-command; git RUNS reset
    ("git --config-env=foo.bar=HP reset --hard", 2),      # = form, self-contained token
    ("git --config-env core.hooksPath=HP commit -m x", 2),   # hooks source swapped to an env var
    ("git --config-env=core.hooksPath=HP commit -m x", 2),
    ("git --config-env foo.bar=HP status", 0),            # safe command behind config-env
    ("git -c core.hooksPathX=/dev/null commit -m x", 0),  # different key must not match
    # remaining arg-taking globals in git's SYNOPSIS — separate-token form must not hide the sub-command
    ("git --attr-source HEAD reset --hard", 2),           # verified live: git consumes HEAD, runs reset
    ("git --attr-source=HEAD reset --hard", 2),
    ("git --super-prefix x/ reset --hard", 2),            # rejected by git 2.54, but block for cross-version safety
    ("git --attr-source HEAD status", 0),                 # safe command behind attr-source
    # terminating global AFTER -c: git prints and exits, the -c never takes effect
    ("git -c core.hooksPath=/dev/null --help commit -m x", 0),
    ("git -c core.hooksPath=/dev/null --version commit -m x", 0),
    # --- #297: leading env-assignments must not hide a destructive sub-command ---
    # (is_bypass already skipped them; is_destructive now matches — real git runs
    #  the sub-command with those vars set, so the destruction happens for real)
    ("GIT_TRACE=1 git reset --hard", 2),
    ("GIT_TRACE=1 git clean -fd", 2),
    ("GIT_TRACE=1 git stash drop", 2),
    ("GIT_TRACE=1 git checkout -f", 2),
    ("FOO=1 git -C /p clean -fd", 2),             # env prefix + global option together
    ("FOO=1 BAR=2 git reset --hard", 2),          # multiple env assignments
    ("FOO+=bar git reset --hard", 2),             # bash/zsh += append-assignment prefix (verified: git runs)
    # --- #297: env-prefixed but SAFE — must stay allowed ---
    ("NOTGIT=1 ls -la", 0),                       # env prefix in front of a non-git command
    ("FOO=1 git status", 0),                      # env prefix, safe sub-command
    ("VERSION=1 git commit -m x", 0),             # env prefix, non-destructive non-bypass
    ("GIT_TRACE=1 git reset --soft HEAD~1", 0),   # env prefix, soft reset is safe
    # --- #302: transparent wrapper prefixes reach the real git (block) ---
    ("sudo git reset --hard", 2),                 # reviewer probe
    ("command git reset --hard", 2),
    ("nohup git clean -fd", 2),
    ("nice -n 10 git checkout -- file.py", 2),    # arg-taking -n eats its numeric arg
    ("sudo -u deploy git push --force origin main", 2),
    ("command git commit --no-verify -m x", 2),   # bypass path through a wrapper
    ("env JAINE_SKIP_PUSH_GUARD=1 git push", 2),  # skip-env as env's operand still sets it for git
    ("sudo env VAR=1 git reset --hard", 2),       # nested wrappers
    ("env -S git reset --hard", 2),               # red-team: spaced -S split-string runs git (macOS)
    ("env - git reset --hard", 2),                # #299 red-team adjacent: bare dash = historic -i, git RUNS
    ("xargs -I{} -S 999 git reset --hard", 2),    # red-team: BSD -S replsize must eat its arg
    # --- #302: wrapper-prefixed but SAFE — must stay allowed ---
    ("sudo git status", 0),
    ("nice FOO=1 git reset --hard", 0),           # red-team: nice execs `FOO=1` → ENOENT, git never runs
    ("command -pv git reset --hard", 0),          # red-team: clustered -v prints resolutions only
    ("env --help git reset --hard", 0),           # codex-review: help/version terminate the wrapper
    ("sudo -V git reset --hard", 0),              # version print — command never runs
    ("sudo -l git reset --hard", 0),              # permission listing — command never runs
    ("sudo --list git reset --hard", 0),          # long-form listing
    ("sudo -v git reset --hard", 0),              # validate mode runs nothing
    ("env -0 git reset --hard", 0),               # print-mode: "cannot specify command with -0"
    ("env -u0 git reset --hard", 2),              # but glued -u0 unsets var `0` and RUNS git
    ("sudo -e git reset --hard", 0),              # edit mode never runs a command
    ("sudo -K git reset --hard", 0),              # -K may not be specified with a command
    ("sudo -k git reset --hard", 2),              # but lowercase -k RUNS the command
    ("command -v git", 0),                        # prints a path, executes nothing
    ("command -v git reset --hard", 0),           # still only prints — inert
    ("echo sudo git reset --hard", 0),            # mention behind echo, not a wrapper chain
    ("xargs -0 grep git", 0),                     # wrapper, but the real command is not git
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
