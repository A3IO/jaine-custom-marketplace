#!/usr/bin/env python3
"""Decide whether a Bash command kills a process the agent likely did NOT spawn.

Used by guard-dispatch.sh (PreToolUse:Bash hook). Reads the command string from
argv[1]; exits 2 if it contains a risky process-kill (prompt the confirm dialog),
0 otherwise.

Threat model — the b09485a0 / 2026-06-14 incident, where a long-running ``agy``
the USER owns was nearly killed by a session that pattern-matched it as a "zombie":

  * ``kill <literal numeric PID>`` — a PID the agent OBSERVED (e.g. from ``ps``) and
    decided to kill. This is the risk: it cannot be known to be the agent's own.
  * ``pkill <name>`` / ``killall <name>`` — by-name/pattern, UNSCOPABLE: it kills
    EVERY matching process, including the user's. Always confirm.

Allowed (the agent's OWN, freshly-spawned processes — not the threat):
  * ``kill $!`` / ``kill "$PID"`` / ``kill %1`` — referenced by the last-bg-pid,
    a shell variable the agent set, or a job spec → the agent spawned it.
  * ``kill -0 <pid>`` — signal 0 is an existence check, sends no signal.

Same shlex approach as guard-git-destructive-detect.py (the shared lexical
front-end lives in git_lexer.py): a regex matches text, not
grammar — it cannot tell a real ``kill`` from ``grep "kill 1234"`` or ``echo kill``.
Fails OPEN (exit 0, allow) on any parse error: a false block that breaks a live
session is worse than a missed exotic form (the threat is accidental harm).
"""
import re
import sys

from git_lexer import _command_prefix_end, split_segments, strip_redirects, tokenize

# A literal PID/PGID target: a (possibly negative) integer. `-1` / `-<pgid>` target a whole
# process group (e.g. `kill -9 -1` = signal everything) — also dangerous, not a signal flag.
_NUMERIC_PID = re.compile(r"^-?\d+$")
# pkill/killall flags that LIST/QUERY rather than kill — presence means "no kill happens".
_INFO_FLAGS = {"-l", "--list", "-h", "--help", "-V", "--version"}


def _kill_has_literal_pid(args: "list[str]") -> bool:
    """True if a ``kill`` command targets a LITERAL numeric PID/PGID (the incident shape).

    A leading signal spec (``-9`` / ``-TERM`` / ``-s SIG``) is stripped first; ``-0`` is an
    existence check (no kill) → never dangerous. After that, a numeric target means a PID the
    agent named explicitly; a ``$VAR`` / ``$!`` / ``%job`` target is the agent's own → allowed.
    """
    i = 0
    if i < len(args) and args[i] in ("-s", "--signal"):
        if i + 1 < len(args) and args[i + 1] == "0":
            return False  # `-s 0` = existence check (signal 0), sends nothing
        i += 2  # skip the signal spec value (a number here is the signal, not a PID)
    elif i < len(args) and args[i].startswith("-") and args[i] != "--":
        if args[i] in ("-0", "-s0", "--signal=0"):
            return False  # signal 0 = existence check (incl. joined -s0 / --signal=0 forms)
        i += 1  # a single leading signal flag (-9 / -TERM / -KILL / -SIGTERM / --signal=9)
    for token in args[i:]:
        if token == "--":
            continue
        if _NUMERIC_PID.match(token):
            return True
    return False


def _pkill_kills(args: "list[str]") -> bool:
    """True if a ``pkill`` / ``killall`` actually kills (has a name/pattern target and is not
    a list/help query). by-name is unscopable, so any real target → dangerous."""
    if any(a in _INFO_FLAGS for a in args):
        return False  # listing signals / help — no process killed
    after_ddash = False
    for a in args:
        if a == "--":
            after_ddash = True  # `--` ends options: everything after is a positional target
            continue
        if after_ddash or not a.startswith("-"):
            return True  # a name/pattern target (even `--foo` after `--`, e.g. pkill -f -- --user-data-dir=…)
    return False


def is_dangerous(segment: "list[str]") -> bool:
    """True if a single command segment is a risky process-kill."""
    segment = strip_redirects(segment)
    i = _command_prefix_end(segment)
    if i >= len(segment):
        return False
    cmd = segment[i].rsplit("/", 1)[-1]
    args = segment[i + 1:]
    if cmd == "kill":
        # A lone "$" arg is the shlex residue of `kill $(...)` (the `(` splits the segment): a
        # command-substitution target — `kill $(pgrep agy)`, `kill $(cat pidfile)` — resolves at
        # runtime and cannot be known to be the agent's own, so confirm. (`$!`/`$VAR`/`${VAR}`
        # stay as single tokens `$!`/`$VAR`/`${VAR}`, never a bare `$`, so they remain allowed.)
        if "$" in args:
            return True
        return _kill_has_literal_pid(args)
    if cmd in ("pkill", "killall"):
        return _pkill_kills(args)
    return False


def main() -> None:
    """Read command from argv[1], exit 2 if any segment is a risky kill."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    tokens = tokenize(command)
    if tokens is None:
        sys.exit(0)  # unparseable -> allow (see module docstring)
    for segment in split_segments(tokens):
        if is_dangerous(segment):
            sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
