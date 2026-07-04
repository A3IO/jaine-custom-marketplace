#!/usr/bin/env python3
"""Unit tests for the shared git_lexer module.

The per-detector behaviour tests live in their own test_guard_*.py; this file
pins the SHARED front-end directly — in particular the `context_args` capture
that repo-targeting (#541) relies on: -C/--git-dir/--work-tree/--namespace/--bare
must be threaded into the branch-delete guard's git calls, while -c/--config-env/
--exec-path/--attr-source/--super-prefix must NOT (they don't change WHICH repo
is targeted). It also pins the `_skip_global_opts` 3-tuple back-compat contract.

Run: python3 test_git_lexer.py   (exit 0 = all pass)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "hooks"))
import git_lexer as gl


def _first_segment(cmd: str) -> list:
    toks = gl.tokenize(cmd)
    assert toks is not None, f"tokenize failed: {cmd!r}"
    return gl.split_segments(toks)[0]


# (command, start, expected_sub_token, expected_context_args)
# start = index just past the `git` token in the first segment.
CONTEXT_CASES = [
    ("git -C /repo branch -D b",                       1, "branch", ["-C", "/repo"]),
    ("git --git-dir=/r/.git status",                   1, "status", ["--git-dir=/r/.git"]),
    ("git --work-tree /w branch",                      1, "branch", ["--work-tree", "/w"]),
    ("git --namespace ns branch -D b",                 1, "branch", ["--namespace", "ns"]),
    ("git --bare branch -D b",                         1, "branch", ["--bare"]),
    ("git -c core.x=y -C /r reset --hard",             1, "reset",  ["-C", "/r"]),
    ("git --exec-path=/x --attr-source HEAD status",   1, "status", []),
    ("git -c a=b --git-dir /d -C /r branch -D b",      1, "branch", ["--git-dir", "/d", "-C", "/r"]),
    ("git --work-tree=/w --namespace=ns branch -D b",  1, "branch", ["--work-tree=/w", "--namespace=ns"]),
]

# A terminating global (print-and-exit) makes the segment inert: sub_index == len,
# and nothing is threaded (git never runs a sub-command).
TERMINATING_CASES = [
    "git --html-path -C /r branch -D b",
    "git --version -C /r branch",
]

# _skip_global_opts must keep its (sub_index, c_values, env_values) 3-tuple contract
# — it is now a thin wrapper over parse_global_opts.
WRAPPER_CASES = [
    ("git -c core.hooksPath=/dev/null commit -m x",     1, "commit", ["core.hooksPath=/dev/null"], []),
    ("git --config-env core.hooksPath=HP commit -m x",  1, "commit", [], ["core.hooksPath=HP"]),
]

# --- #302: _command_prefix_end — transparent wrapper prefixes before the command ---
# (command, token expected at the returned index). Must skip leading VAR=val
# assignments AND transparent wrappers (command/env/xargs/sudo/nice/nohup) with
# their flags; arg-taking wrapper flags CONSUME their argument (guardrail 1 —
# else the arg is misread as the command and the segment silently allows).
CPE_CASES = [
    # superset of _env_prefix_end: unchanged behaviour on plain/env-assign forms
    ("git branch -D x", "git"),
    ("GIT_TRACE=1 git status", "git"),
    ("FOO=1 BAR=2 git push", "git"),
    ("ls -la", "ls"),
    # bare transparent wrappers
    ("command git branch -D x", "git"),
    ("env git push --delete origin x", "git"),
    ("xargs git branch -D x", "git"),
    ("sudo git reset --hard", "git"),
    ("nice git gc", "git"),
    ("nohup git fetch", "git"),
    # wrapper via absolute path — basename match, same rule as the git token itself
    ("/usr/bin/env git branch -D x", "git"),
    ("/usr/bin/sudo git reset --hard", "git"),
    # argless wrapper flags
    ("command -p git branch -D x", "git"),
    ("env -i git branch -D x", "git"),
    # `env - cmd` is the historic spelling of `env -i cmd` — env clears the
    # environment and RUNS the next token (verified live on macOS); the scan
    # must not stop at the bare dash (#299 red-team adjacent)
    ("env - git reset --hard", "git"),
    ("sudo -n -E git branch -D x", "git"),
    # guardrail 1: arg-taking wrapper flags EAT their argument
    ("sudo -u user git branch -D x", "git"),
    ("xargs -I {} git branch -D x", "git"),
    ("env -u PATH git branch -D x", "git"),
    ("nice -n 10 git branch -D x", "git"),
    # glued short forms and --long=value forms stay self-contained
    ("xargs -I{} git branch -D x", "git"),
    ("xargs -n1 git branch -D x", "git"),
    ("xargs --max-args=5 git branch -D x", "git"),
    # GNU optional-arg long opts bind their value with `=` ONLY — a bare form must
    # NOT eat the next token (else the real command is misread as the value)
    ("xargs --replace git branch -D x", "git"),
    ("xargs --eof git branch -D x", "git"),
    ("xargs --max-lines git branch -D x", "git"),
    # red-team (#302): spaced `env -S` split-string IS the command start (macOS env
    # runs it), and BSD xargs -S replsize is a required-arg flag that must eat 999
    ("env -S git branch -D x", "git"),
    ("xargs -I{} -S 999 git branch -D x", "git"),
    # red-team (#299 r3): a short BUNDLE whose LAST char is an arg-taking flag
    # consumes the next token (getopt binds the value to the trailing opt) —
    # `env -iC /repo git …` really runs git in /repo (verified live)
    ("env -iC /repo git branch -D x", "git"),
    ("sudo -nD /repo git branch -D x", "git"),
    # …but a mid-bundle arg-taker binds the bundle REMAINDER, not the next token:
    # `env -uC git …` unsets variable C and git is the very next token
    ("env -uC git branch -D x", "git"),
    # red-team (#302): only env/sudo accept VAR=val operands; nice would exec
    # `FOO=1` (ENOENT) and git never runs — the scan must stop there, not skip it
    ("nice FOO=1 git reset --hard", "FOO=1"),
    ("nice -n10 git branch -D x", "git"),
    ("nice --adjustment=10 git branch -D x", "git"),
    ("sudo --user=root git branch -D x", "git"),
    # VAR=val operands AFTER a wrapper (env/sudo accept them) are skipped too
    ("env -i VAR=val git branch -D x", "git"),
    ("env VAR=val git branch -D x", "git"),
    ("sudo VAR=val git branch -D x", "git"),
    # `0` glued to arg-taking -u is the VARIABLE NAME, not the -0 print-mode:
    # env unsets `0` and RUNS git (verified live) — must still land on git
    ("env -u0 git reset --hard", "git"),
    # lowercase -k resets the timestamp AND RUNS the command — not terminating
    ("sudo -k git reset --hard", "git"),
    # `--` ends wrapper option parsing
    ("sudo -- git branch -D x", "git"),
    ("env -- git branch -D x", "git"),
    # nested wrappers, interleaved with assignments
    ("sudo env VAR=1 git branch -D x", "git"),
    ("VAR=1 sudo nice -n 5 git branch -D x", "git"),
    ("command sudo git reset --hard", "git"),
    # NOT transparent: git as a mere argument must not be reached (guards philosophy —
    # the command is the first significant token; no loose whole-array scan)
    ("grep git file", "grep"),
    ("foo bar git branch -D x", "foo"),
    ("echo sudo git reset --hard", "echo"),
    ("find . -name git", "find"),
]

# Inert wrapper forms: the wrapper prints (resolutions, help, version, sudo -V/-l
# listings) and exits — the wrapped command NEVER runs. Contract: return
# len(segment), mirroring git's own _TERMINATING_OPTS convention.
CPE_INERT = [
    "command -v git",
    "command -v git branch -D x",
    "command -V git branch -D x",
    "command -pv git branch -D x",           # red-team: clustered -v still only prints
    # codex-review P2: wrapper --help/--version terminate (GNU prints, BSD errors
    # on the illegal option — either way the command never executes)
    "env --help git reset --hard",
    "xargs --version git branch -D x",
    "nohup --help git reset --hard",
    "sudo -V git reset --hard",              # version print, exits
    "sudo -l git reset --hard",              # permission LISTING — does not run the command
    "sudo --list git reset --hard",          # long form of -l
    "sudo -ll git reset --hard",             # clustered verbose listing
    "sudo -v git reset --hard",              # validate: refreshes timestamp, runs nothing
    "sudo --validate git reset --hard",
    # env -0/--null: print-mode, "cannot specify command with -0" (verified live)
    "env -0 git reset --hard",
    "env --null git branch -D x",
    "env -0i git reset --hard",              # clustered with argless i/v — still print-mode
    # sudo -e edits FILES (never runs a command); -K "may not be specified with a command"
    "sudo -e git reset --hard",
    "sudo --edit git reset --hard",
    "sudo -K git reset --hard",
    "sudo --remove-timestamp git reset --hard",
]


def main() -> None:
    fails = []

    for cmd, start, want_sub, want_ctx in CONTEXT_CASES:
        seg = _first_segment(cmd)
        g = gl.parse_global_opts(seg, start)
        got_sub = seg[g.sub_index] if g.sub_index < len(seg) else None
        if got_sub != want_sub:
            fails.append(f"{cmd!r}: sub={got_sub!r} want {want_sub!r}")
        if list(g.context_args) != want_ctx:
            fails.append(f"{cmd!r}: context_args={list(g.context_args)!r} want {want_ctx!r}")

    for cmd in TERMINATING_CASES:
        seg = _first_segment(cmd)
        g = gl.parse_global_opts(seg, 1)
        if g.sub_index != len(seg):
            fails.append(f"{cmd!r}: terminating sub_index={g.sub_index} want {len(seg)}")
        if list(g.context_args) != []:
            fails.append(f"{cmd!r}: terminating context_args={list(g.context_args)!r} want []")

    for cmd, start, want_sub, want_c, want_env in WRAPPER_CASES:
        seg = _first_segment(cmd)
        idx, c_values, env_values = gl._skip_global_opts(seg, start)
        got_sub = seg[idx] if idx < len(seg) else None
        if got_sub != want_sub:
            fails.append(f"{cmd!r}: wrapper sub={got_sub!r} want {want_sub!r}")
        if c_values != want_c:
            fails.append(f"{cmd!r}: wrapper c_values={c_values!r} want {want_c!r}")
        if env_values != want_env:
            fails.append(f"{cmd!r}: wrapper env_values={env_values!r} want {want_env!r}")

    for cmd, want_tok in CPE_CASES:
        seg = _first_segment(cmd)
        i = gl._command_prefix_end(seg)
        got = seg[i] if i < len(seg) else None
        if got != want_tok:
            fails.append(f"{cmd!r}: _command_prefix_end -> {got!r} want {want_tok!r}")

    for cmd in CPE_INERT:
        seg = _first_segment(cmd)
        i = gl._command_prefix_end(seg)
        if i < len(seg):
            fails.append(f"{cmd!r}: inert form must return len(segment), got index {i} ({seg[i]!r})")

    total = (len(CONTEXT_CASES) * 2 + len(TERMINATING_CASES) * 2 + len(WRAPPER_CASES) * 3
             + len(CPE_CASES) + len(CPE_INERT))
    for f in fails:
        print(f"FAIL  {f}")
    print(f"\n{total - len(fails)}/{total} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
