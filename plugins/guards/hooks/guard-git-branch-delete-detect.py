#!/usr/bin/env python3
"""Detect unsafe git branch deletion/overwrite commands for PreToolUse:Bash.

Covers delete forms (branch -d/-D, push :branch/--delete/+:branch) and the
force-move class (#299: branch -f, checkout -B, switch -C) — moving an existing
unmerged branch discards its old tip exactly like deleting it.

Command text is read from argv[1]. Exit 2 blocks; every other exit allows.
The detector fails open on unexpected errors. Ported from STATUSLINE's
hooks/guard-delete/guard_branch_delete.sh: the lexical front is shared via
hooks/git_lexer.py, while the semantic merge-check is preserved.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Cap every git subprocess so a pathological repo context can never hang the hook
# past Claude Code's 60s budget (#309: a --git-dir/GIT_DIR/env -C context whose HEAD
# is a writer-less FIFO blocks git's open() forever). Overridable for fast tests.
_GIT_TIMEOUT_S = float(os.environ.get("GUARD_GIT_TIMEOUT_S", "10"))


class _GitTimeout(Exception):
    """A verification git call exceeded _GIT_TIMEOUT_S (#309).

    Raised out of run_git so main() can fail CLOSED for the matched destructive
    command: a merely-slow (not hung) repo would let the real branch delete/reset
    complete and destroy work, so a timeout must block, not allow.
    """

from git_lexer import (
    tokenize,
    split_segments,
    strip_redirects,
    _command_prefix_end,
    _ENV_ASSIGN,
    _WRAPPER_ARG_FLAGS,
    parse_global_opts,
)

# Repo-locating environment variables and their exact CLI equivalents (#299 item 2):
# GIT_DIR=<p> git … retargets the repo exactly like git --git-dir=<p> …, so the
# semantic merge-check is threaded the same way as -C/--git-dir context args.
_ENV_CONTEXT_VARS = {
    "GIT_DIR": "--git-dir=",
    "GIT_WORK_TREE": "--work-tree=",
    "GIT_NAMESPACE": "--namespace=",
}


def env_context_args(segment: list[str], end: int) -> tuple[list[str], bool]:
    """CLI equivalents of repo-locating env assignments in segment[:end].

    Walks the command prefix skipped by _command_prefix_end (leading VAR=val plus
    assignments carried by env/sudo wrappers) STATEFULLY, because wrappers change
    what actually reaches git (#299 red-team):

      * ``env -i`` / ``env -`` / ``--ignore-environment`` clear everything set so
        far (assignments given as env's own operands after the flag still apply);
      * ``env -u NAME`` / ``--unset[= ]NAME`` drops one variable;
      * ``sudo`` env_reset drops GIT_* vars set before it by default; ``-E`` /
        ``--preserve-env`` restores them (``--preserve-env=LIST`` restores the
        listed ones); assignments as sudo operands always apply;
      * ``env -C``/``sudo -D``/``--chdir`` change the cwd git runs in (red-team
        r2, verified live) — threaded as a leading ``git -C <dir>``.

    Returns (context_args, unverifiable): a $-expansion or VAR+= append in a
    repo-locating value cannot be resolved lexically -> unverifiable (fail
    closed, same policy as $ in -C paths). Env-derived args go BEFORE CLI global
    opts so a real --git-dir wins (git's CLI-overrides-environment precedence).
    """
    assignments: dict[str, str] = {}
    unverifiable = False
    wrapper = None
    sudo_snapshot: dict[str, str] = {}
    pending_unset = False
    pending_chdir = False
    pending_skip = False
    chdir: str | None = None

    def set_chdir(value: str) -> None:
        nonlocal chdir, unverifiable
        if "$" in value:
            unverifiable = True
        else:
            chdir = value

    for tok in segment[:end]:
        if pending_skip:
            # the previous token was an arg-taking wrapper flag — this token is
            # its VALUE (xargs -I <replstr>, sudo -p <prompt>), never an env
            # assignment even if it looks like one (codex-review P1)
            pending_skip = False
            continue
        if pending_unset:
            pending_unset = False
            assignments.pop(tok, None)
            continue
        if pending_chdir:
            pending_chdir = False
            set_chdir(tok)
            continue
        if _ENV_ASSIGN.match(tok):
            name, value = tok.split("=", 1)
            append = name.endswith("+")
            if append:
                name = name[:-1]
            if name not in _ENV_CONTEXT_VARS:
                continue
            if append or "$" in value:
                unverifiable = True
                continue
            assignments[name] = value
            continue
        base = tok.rsplit("/", 1)[-1]
        if base in ("env", "sudo", "command", "xargs", "nice", "nohup"):
            wrapper = base
            if base == "sudo":
                sudo_snapshot = dict(assignments)
                assignments.clear()
            continue
        if tok.startswith("-"):
            if wrapper == "env":
                if tok in ("-", "--ignore-environment"):
                    assignments.clear()
                elif tok == "--unset":
                    pending_unset = True
                elif tok == "--chdir":
                    pending_chdir = True
                elif tok.startswith("--unset="):
                    assignments.pop(tok.split("=", 1)[1], None)
                elif tok.startswith("--chdir="):
                    set_chdir(tok.split("=", 1)[1])
                elif tok in _WRAPPER_ARG_FLAGS["env"] and tok not in ("-u", "-C"):
                    pending_skip = True  # e.g. spaced -P <dir>: next token is its value
                    # (-u/-C handled above with their own pending states)
                elif not tok.startswith("--"):
                    # short bundle: getopt binds the value to the FIRST arg-taking
                    # char — `-iC /dir` clears env then chdirs (trailing C takes the
                    # next token), `-uC` unsets variable C (mid-bundle remainder)
                    for pos in range(1, len(tok)):
                        ch = tok[pos]
                        if ch == "i":
                            assignments.clear()
                            continue
                        if ch == "u":
                            rest_val = tok[pos + 1:]
                            if rest_val:
                                assignments.pop(rest_val, None)
                            else:
                                pending_unset = True
                            break
                        if ch == "C":
                            rest_val = tok[pos + 1:]
                            if rest_val:
                                set_chdir(rest_val)
                            else:
                                pending_chdir = True
                            break
                        if ch == "P":
                            if pos == len(tok) - 1:
                                pending_skip = True  # trailing: next token is its value
                            break
                        if ch == "S":
                            break  # its value is the command start — stop here
            elif wrapper == "sudo":
                if tok == "--preserve-env":
                    assignments.update(sudo_snapshot)
                elif tok.startswith("--preserve-env="):
                    for name in tok.split("=", 1)[1].split(","):
                        if name in sudo_snapshot:
                            assignments[name] = sudo_snapshot[name]
                elif tok == "--chdir":
                    pending_chdir = True
                elif tok.startswith("--chdir="):
                    set_chdir(tok.split("=", 1)[1])
                elif tok != "-D" and tok in _WRAPPER_ARG_FLAGS["sudo"]:
                    pending_skip = True  # spaced -p <prompt>/-u <user>/…: next token is a value
                    # (-D handled above with pending_chdir; sudo -C is close-from, skipped here)
                elif not tok.startswith("--"):
                    for pos in range(1, len(tok)):
                        ch = tok[pos]
                        if ch == "E":
                            assignments.update(sudo_snapshot)
                            continue
                        if ch == "D":
                            rest_val = tok[pos + 1:]
                            if rest_val:
                                set_chdir(rest_val)
                            else:
                                pending_chdir = True
                            break
                        if ch in "ugpChrtURT":
                            if pos == len(tok) - 1:
                                pending_skip = True  # trailing: next token is its value
                            break
            elif wrapper in _WRAPPER_ARG_FLAGS:
                flags = _WRAPPER_ARG_FLAGS[wrapper]
                if tok in flags:
                    pending_skip = True  # e.g. xargs -I <replstr>, nice -n <adj>
                elif (not tok.startswith("--") and len(tok) > 2
                        and "-" + tok[-1] in flags):
                    pending_skip = True  # bundle with a trailing arg-taker
            continue
        # anything else is a wrapper's flag argument (e.g. sudo -u root) — skip
    args = [_ENV_CONTEXT_VARS[name] + value for name, value in assignments.items()]
    if chdir is not None:
        args = ["-C", chdir] + args
    return args, unverifiable


def _strip_heads(branch: str) -> str:
    """Strip a leading local-head ref prefix from a LOCAL branch operand.

    Deliberately does NOT strip a bare `heads/` prefix: a local branch can be
    literally named heads/x and `git branch -D heads/x` deletes exactly it
    (codex-review P2, verified live).
    """
    if branch.startswith("refs/heads/"):
        return branch[len("refs/heads/"):]
    return branch


def _strip_push_ref(branch: str) -> str:
    """Strip head-ref prefixes from a PUSH refspec operand.

    On push deletes the REMOTE resolves the refname, so the `heads/<b>`
    abbreviation also means refs/heads/<b> (#299 red-team r5, verified live).
    """
    if branch.startswith("refs/heads/"):
        return branch[len("refs/heads/"):]
    if branch.startswith("heads/"):
        return branch[len("heads/"):]
    return branch


def branch_delete_operands(rest: list[str]) -> list[str] | None:
    """Return branch operands for `git branch` delete forms, else None.

    `-r`/`--remotes` (incl. clustered `-Dr`) makes the delete target REMOTE-TRACKING
    refs, not local work — re-fetchable, so never a work-loss (#299 item 4). A local
    branch that merely LOOKS like `origin/x` must not false-block that form.
    """
    delete_seen = False
    operands: list[str] = []

    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-r", "--remotes"):
            return None
        if tok in ("-d", "-D", "--delete"):
            delete_seen = True
            i += 1
            continue
        if tok in ("-f", "--force"):
            i += 1
            continue
        if tok in _BRANCH_ARG_OPTS:
            # required-arg option consumes its value, exactly as the force-move path
            # does (#309): `git branch -D --sort <key> X` deletes X, not <key>.
            i += 2
            continue
        if tok.startswith("--"):
            i += 1
            continue
        if tok.startswith("-"):
            if "r" in tok[1:]:
                return None
            if "d" in tok or "D" in tok:
                delete_seen = True
            i += 1
            continue

        if delete_seen:
            operands.append(_strip_heads(tok))
            i += 1
            continue
        return None

    return operands if delete_seen else None


# `git branch` REQUIRED-arg options: the spaced form consumes the next token, so the
# operand scan must eat it too (#299 red-team: `git branch -f --sort committerdate X`
# really force-moves X — verified live — and the key must not displace the operand).
# Optional-arg opts (--track/--abbrev/--color/--column) bind via `=` only and are
# deliberately absent: their bare form leaves the next token as a real positional.
_BRANCH_ARG_OPTS = {
    "-u", "--set-upstream-to", "--sort", "--format", "--contains", "--no-contains",
    "--merged", "--no-merged", "--points-at",
}

# `git branch` modes that only edit tracking config / description — the branch tip is
# NEVER reset, so `-f` is inert and the command is not a force-move (#309, verified live:
# `git branch -f -u <u> <b>` leaves <b>'s tip UNCHANGED). Their presence (spaced, glued
# `--set-upstream-to=`, or the clustered short `-u`, e.g. `-fu`) means "not a force-move".
_BRANCH_SET_UPSTREAM_MODES = {
    "-u", "--set-upstream-to", "--unset-upstream", "--edit-description",
}


def branch_force_move_operand(rest: list[str]) -> str | None:
    """Branch operand of a force-MOVE `git branch -f/--force NAME [start]` (#299 item 1).

    Force-moving an EXISTING branch resets its tip and discards the old commits —
    the same work-loss as a delete. Delete forms are handled by
    branch_delete_operands (consulted first); rename/copy/list/remotes forms
    (-m/-M/-c/-C/-l/-r and longs, incl. clustered) return None — rename-overwrite
    is documented out-of-scope (#299 names branch -f / checkout -B / switch -C).
    """
    force_seen = False
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-d", "-D", "--delete", "-m", "-M", "-c", "-C",
                   "--move", "--copy", "--list", "-l", "-r", "--remotes"):
            return None
        if tok in _BRANCH_SET_UPSTREAM_MODES or tok.startswith("--set-upstream-to="):
            return None  # set-upstream/edit mode never resets the tip (#309)
        if tok in ("-f", "--force"):
            force_seen = True
            i += 1
            continue
        if tok in _BRANCH_ARG_OPTS:
            i += 2
            continue
        if tok.startswith("--"):
            i += 1
            continue
        if tok.startswith("-"):
            if any(ch in "dDmMcClr" for ch in tok[1:]):
                return None
            if "u" in tok[1:]:
                return None  # clustered set-upstream (e.g. -fu <u> <b>) — not a force-move
            if "f" in tok[1:]:
                force_seen = True
            i += 1
            continue
        positionals.append(tok)
        i += 1
    if force_seen and positionals:
        return _strip_heads(positionals[0])
    return None


def force_create_operand(rest: list[str], short: str, long: "str | None") -> str | None:
    """Branch operand of `git checkout -B NAME` / `git switch -C NAME` (#299 item 1).

    Both reset an existing NAME to <start>/HEAD, discarding its old tip like a
    force-move. Handles the spaced, glued (`-Bname`), clustered (`-qB name` /
    `-fBname` — parse-options binds the value to the LAST short opt of a bundle;
    red-team r2, verified live) and, for switch, `--force-create[=NAME]`
    spellings. Returns None when no force-create flag is present (`-b`/plain
    forms create-or-fail — nothing is lost). Within a bundle it is first-arg-taker-
    wins: in `-bB feat/x` the lowercase -b binds 'B' as ITS branch name (git creates
    branch B, verified live) — non-forcing, so the scan stops there.

    ACROSS repeated flags it is LAST-wins: git's parse-options resolves a repeated
    value-flag to the last occurrence (`git checkout -B a -B b` resets b — verified
    live), so a decoy safe/new first operand must not hide the real last one (#309). A
    lowercase create flag anywhere (`-b`, or mixed with `-B`) makes git create-or-error
    and reset nothing — return None.
    """
    flag_char = short[1]
    soft_char = flag_char.lower()
    last: str | None = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if long is not None:
            if tok == long:
                if i + 1 < len(rest):
                    last = _strip_heads(rest[i + 1])
                    i += 2
                    continue
                i += 1
                continue
            if tok.startswith(long + "="):
                last = _strip_heads(tok.split("=", 1)[1])
                i += 1
                continue
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            for pos in range(1, len(tok)):
                ch = tok[pos]
                if ch == soft_char:
                    return None
                if ch == flag_char:
                    glued = tok[pos + 1:]
                    if glued:
                        last = _strip_heads(glued)
                    elif i + 1 < len(rest):
                        last = _strip_heads(rest[i + 1])
                    break
        i += 1
    return last


def push_delete_operands(rest: list[str]) -> list[str] | None:
    """Return branch operands for `git push` delete forms, else None."""
    delete_seen = False
    colon_seen = False
    operands: list[str] = []

    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--delete", "-d"):
            delete_seen = True
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        break

    if i >= len(rest):
        return None

    # First non-flag token is the remote.
    i += 1

    while i < len(rest):
        tok = rest[i]
        if tok in ("--delete", "-d"):
            delete_seen = True
        elif tok.startswith(":") and len(tok) > 1:
            operands.append(_strip_push_ref(tok[1:]))
            colon_seen = True
        elif tok.startswith("+:") and len(tok) > 2:
            # `+:branch` IS a delete: `+` is the force marker on an empty-source
            # refspec (#299 item 3). `+src:dst` (non-empty source) is a force
            # UPDATE — the destructive detector's domain, not matched here.
            operands.append(_strip_push_ref(tok[2:]))
            colon_seen = True
        elif tok.startswith("-"):
            pass
        elif delete_seen:
            operands.append(_strip_push_ref(tok))
        i += 1

    return operands if (delete_seen or colon_seen) else None


def run_git(context_args: list[str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the matched repository context.

    Decode with errors="replace": git normally re-encodes log output to UTF-8, but
    be defensive so a non-UTF-8 byte in a commit message can never raise
    UnicodeDecodeError — that would be swallowed by main()'s fail-open catch and
    silently allow an unmerged-branch delete the guard exists to block.
    """
    argv = ["git", *context_args, *args]
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # A context/repo that never yields in time (FIFO HEAD, or a genuinely slow
        # repo, #309) must not stall the hook. Surface it so main() fails CLOSED for
        # the matched destructive command — allowing here would let a slow-but-valid
        # check pass while the real branch delete/reset destroys unmerged work.
        raise _GitTimeout


def _git_ok(context_args: list[str], *args: str) -> bool:
    return run_git(context_args, *args).returncode == 0


def _git_stdout(context_args: list[str], *args: str) -> str | None:
    result = run_git(context_args, *args)
    if result.returncode != 0:
        return None
    return result.stdout


def block_unverifiable_operand(branch: str) -> None:
    print(f"BLOCKED: cannot verify branch operand '{branch}' before deletion.", file=sys.stderr)
    print("Use literal branch names so the guard can check git history.", file=sys.stderr)
    print("If you already verified this is safe, re-run prefixed:", file=sys.stderr)
    print("  GUARD_BRANCH_DELETE_OK=1 <same command>", file=sys.stderr)
    sys.exit(2)


def block_verify_timed_out(branch: str) -> None:
    print(
        f"BLOCKED: could not verify branch '{branch}' within {_GIT_TIMEOUT_S:.0f}s "
        "(git check timed out).",
        file=sys.stderr,
    )
    print("Retry once the repository responds, or if you verified this is safe:", file=sys.stderr)
    print("  GUARD_BRANCH_DELETE_OK=1 <same command>", file=sys.stderr)
    sys.exit(2)


def block_unverifiable_context() -> None:
    print("BLOCKED: cannot verify repository context path before branch deletion.", file=sys.stderr)
    print(
        "Use literal paths for -C/--git-dir/--work-tree/--namespace (or the GIT_DIR/"
        "GIT_WORK_TREE/GIT_NAMESPACE env vars) so the guard can check git history.",
        file=sys.stderr,
    )
    print("If you already verified this is safe, re-run prefixed:", file=sys.stderr)
    print("  GUARD_BRANCH_DELETE_OK=1 <same command>", file=sys.stderr)
    sys.exit(2)


def _numeric_count(text: str | None) -> int | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped.isdigit():
        return None
    return int(stripped)


# Git's previous-branch shorthand: `git branch -D @{-1}` deletes the branch you
# just left (#299 red-team r4, verified live) — resolve it before the ref lookup.
_PREV_BRANCH = re.compile(r"^@\{-\d+\}$")


def _resolve_prev_branch(branch: str, context_args: list[str]) -> str | None:
    out = _git_stdout(context_args, "rev-parse", "--symbolic-full-name", branch)
    if out is None:
        return None
    ref = out.strip()
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return None


def check_branch_delete(branch: str, context_args: list[str]) -> bool:
    """Return True after printing a block message if branch has unmerged commits."""
    if _PREV_BRANCH.match(branch):
        resolved = _resolve_prev_branch(branch, context_args)
        if resolved is None:
            return False  # no such history entry — git itself errors, nothing lost
        branch = resolved

    if run_git(context_args, "rev-parse", "--verify", f"refs/heads/{branch}").returncode != 0:
        return False

    base = ""
    force_root = False

    if "/" in branch:
        prefix = branch.split("/", 1)[0]
        if branch == f"{prefix}/main":
            force_root = True
        elif _git_ok(context_args, "rev-parse", "--verify", f"refs/heads/{prefix}/main"):
            base = f"{prefix}/main"

    if not base and not force_root:
        root_base = ""
        if _git_ok(context_args, "rev-parse", "--verify", "refs/heads/main"):
            root_base = "main"
        elif _git_ok(context_args, "rev-parse", "--verify", "refs/heads/master"):
            root_base = "master"

        product_out = _git_stdout(
            context_args,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/*/main",
        )
        product_bases = product_out.splitlines() if product_out is not None else []
        candidates = ([root_base] if root_base else []) + product_bases

        best_base = ""
        best_count: int | None = None
        for candidate in candidates:
            if not candidate or candidate == branch:
                continue
            merge_base = _git_stdout(context_args, "merge-base", candidate, branch)
            if merge_base is None or not merge_base.strip():
                continue
            count = _numeric_count(_git_stdout(context_args, "rev-list", "--count", f"{candidate}..{branch}"))
            if count is None:
                continue
            if best_count is None or count < best_count:
                best_base = candidate
                best_count = count

        if best_base:
            base = best_base

    if not base:
        if _git_ok(context_args, "rev-parse", "--verify", "refs/heads/main"):
            base = "main"
        elif _git_ok(context_args, "rev-parse", "--verify", "refs/heads/master"):
            base = "master"
        else:
            return False

    unmerged = _numeric_count(_git_stdout(context_args, "rev-list", "--count", f"{base}..{branch}"))
    if unmerged is None:
        return False

    commit_log = _git_stdout(context_args, "log", f"{base}..{branch}", "--oneline")
    if commit_log is None:
        return False

    if unmerged > 0:
        print(f"BLOCKED: branch '{branch}' has {unmerged} unmerged commit(s) not on {base}:", file=sys.stderr)
        for line in commit_log.splitlines()[:5]:
            print(line, file=sys.stderr)
        if unmerged > 5:
            print(f"  ... and {unmerged - 5} more", file=sys.stderr)
        print("", file=sys.stderr)
        print("If this work is NOT merged: preserve it first (git cherry-pick / git branch -m).", file=sys.stderr)
        print("If it IS merged but invisible to git log (squash-merge): VERIFY first", file=sys.stderr)
        print(f"  (gh pr list --state all --head '{branch}' shows MERGED), then re-run prefixed:", file=sys.stderr)
        print("  GUARD_BRANCH_DELETE_OK=1 <same command>", file=sys.stderr)
        return True

    return False


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""

    if "git" not in command or (
        "branch" not in command and "push" not in command
        and "checkout" not in command and "switch" not in command
    ):
        sys.exit(0)

    if "GUARD_BRANCH_DELETE_OK=1" in command:
        sys.exit(0)

    command = command.replace("\\\n", " ")
    tokens = tokenize(command)
    if tokens is None:
        sys.exit(0)

    for segment in split_segments(tokens):
        segment = strip_redirects(segment)
        i = _command_prefix_end(segment)
        if i >= len(segment) or segment[i].rsplit("/", 1)[-1] != "git":
            continue

        g = parse_global_opts(segment, i + 1)
        if g.sub_index >= len(segment):
            continue

        sub = segment[g.sub_index]
        rest = segment[g.sub_index + 1:]
        if sub == "branch":
            operands = branch_delete_operands(rest)
            if operands is None:
                move = branch_force_move_operand(rest)
                operands = None if move is None else [move]
        elif sub == "push":
            operands = push_delete_operands(rest)
        elif sub == "checkout":
            move = force_create_operand(rest, "-B", None)
            operands = None if move is None else [move]
        elif sub == "switch":
            move = force_create_operand(rest, "-C", "--force-create")
            operands = None if move is None else [move]
        else:
            continue

        if operands is None:
            continue

        env_args, env_unverifiable = env_context_args(segment, i)
        if env_unverifiable or any("$" in t for t in g.context_args):
            block_unverifiable_context()

        context_args = env_args + g.context_args
        for br in operands:
            if "$" in br:
                block_unverifiable_operand(br)
            try:
                unsafe = check_branch_delete(br, context_args)
            except _GitTimeout:
                block_verify_timed_out(br)  # matched destructive, unverifiable -> fail closed
            if unsafe:
                sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
