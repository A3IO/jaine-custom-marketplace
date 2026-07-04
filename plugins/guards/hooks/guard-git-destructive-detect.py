#!/usr/bin/env python3
"""Decide whether a Bash command contains a destructive git sub-command.

Used by guard-git-destructive.sh (PreToolUse:Bash hook). Reads the command
string from argv[1]; exits 2 if it contains a work-discarding git operation,
0 otherwise.

Why a parser instead of a regex: a regex matches text, not grammar. It cannot
tell a real git sub-command from the same words inside a quoted string
(`grep "git reset --hard"`), a redirect target (`git checkout b > x.txt`), or a
merge resolution (`git checkout --ours file.py`). shlex tokenizes with quote and
operator awareness, so we inspect actual command boundaries instead.

Fails OPEN (exit 0, allow) on any parse error: this guard catches accidental
work loss, not a motivated adversary, so a false block that breaks a live
session is worse than a missed exotic command.
"""
import re
import sys

from git_lexer import tokenize, split_segments, strip_redirects, _has_force_flag, _skip_global_opts, _command_prefix_end

# Source-file extensions: a positional arg to `git checkout` ending in one of
# these is treated as a file being discarded, not a branch/tag name.
_DANGER_EXT = {
    "py", "sh", "m", "swift", "h", "md", "json", "plist", "txt", "js", "ts",
    "tsx", "jsx", "go", "rs", "c", "cpp", "cc", "hpp", "rb", "java", "kt",
    "yaml", "yml", "toml", "cfg", "ini", "conf", "xml", "html", "css", "svg",
    "sql", "lua", "pl", "php", "vue",
}


def _looks_like_file(arg: str) -> bool:
    """True if a checkout arg looks like a source file (vs a branch/tag name)."""
    base = arg.rsplit("/", 1)[-1]
    if "." not in base:
        return False
    return base.rsplit(".", 1)[-1].lower() in _DANGER_EXT


def is_destructive(segment: list[str]) -> bool:
    """True if a single command segment is a work-discarding git operation."""
    segment = strip_redirects(segment)
    i = _command_prefix_end(segment)
    if i >= len(segment) or segment[i].rsplit("/", 1)[-1] != "git":
        return False
    j, _, _ = _skip_global_opts(segment, i + 1)
    if j >= len(segment):
        return False
    sub, rest = segment[j], segment[j + 1:]

    if sub == "reset":
        return "--hard" in rest
    if sub == "restore":
        staged = "--staged" in rest or "-S" in rest
        worktree = "--worktree" in rest or "-W" in rest
        # --staged alone touches only the index (safe unstage, no working-tree loss).
        # Anything that touches the working tree — default, --worktree, or --staged+--worktree — discards.
        return not (staged and not worktree)
    if sub == "clean":
        # -n/--dry-run lists what WOULD be deleted but deletes nothing; overrides -f.
        dry = "--dry-run" in rest or any(
            t.startswith("-") and not t.startswith("--") and "n" in t for t in rest
        )
        if dry:
            return False
        return _has_force_flag(rest)
    if sub == "stash":
        return bool(rest) and rest[0] in ("drop", "clear")
    if sub == "checkout":
        if _has_force_flag(rest):
            return True  # -f/--force discards local modifications — wins over any branch flag
        if "--ours" in rest or "--theirs" in rest:
            return False  # merge-conflict resolution, not work loss
        if any(f in rest for f in ("-b", "-B", "--orphan", "--track", "-t", "--detach")):
            return False  # positional is a branch/commit REF (created/tracked/detached), not a file
        if "--" in rest:
            return True  # explicit pathspec discard: git checkout -- <file>
        return any(
            not token.startswith("-") and (token in (".", "./") or _looks_like_file(token))
            for token in rest
        )
    return False


# --- safety-bypass detection (migrated from block-safety-bypass.sh) -----------
# These don't discard committed work directly, but they DISABLE the git hooks that
# enforce project doctrine (--no-verify, -c core.hooksPath=…) or force-push over
# history. shlex tokenization fixes the quoted-flag bypass that the old regex had
# (`git commit '--no-verify'` → token `--no-verify`).
_BYPASS_SUBS = {"commit", "push", "merge", "rebase", "cherry-pick"}
_SKIP_ENV = re.compile(r"^JAINE_SKIP_(PUSH_GUARD|AUTO_REBUILD|AUTO_PUBLISH)\+?=")
# Git config keys are case-insensitive (`-c CORE.HOOKSPATH=…` works), so both the
# `-c` and `--config-env` bypass checks compare the key lowercased against this.
_HOOKSPATH_KEY = "core.hookspath"


def _hookspath_disabled(val: str) -> bool:
    """True if a `-c` value disables git hooks: core.hooksPath=<empty|/dev/null>."""
    key, sep, v = val.partition("=")
    if not sep or key.lower() != _HOOKSPATH_KEY:
        return False
    return v == "" or v == "/dev/null" or v.endswith("/dev/null")


def is_bypass(segment: list[str]) -> bool:
    """True if a segment disables git safety hooks or force-pushes over history."""
    segment = strip_redirects(segment)
    # Leading env assignments and transparent wrappers before `git` — a JAINE_SKIP_*
    # among them (bare or as an `env` operand, #302) is itself the bypass signal;
    # the rest are just skipped to reach the `git` token.
    i = _command_prefix_end(segment)
    if any(_SKIP_ENV.match(t) for t in segment[:i]):
        return True
    if i >= len(segment) or segment[i].rsplit("/", 1)[-1] != "git":
        return False
    # Global git options between `git` and the sub-command. A `-c core.hooksPath=…`
    # that disables hooks is itself the bypass, whatever follows it.
    j, c_values, env_values = _skip_global_opts(segment, i + 1)
    if any(_hookspath_disabled(v) for v in c_values):
        return True
    # --config-env pointing core.hooksPath at ANY env var swaps the hooks source;
    # the variable's content is out of lexical reach, so the key alone is the signal.
    if any(v.partition("=")[0].lower() == _HOOKSPATH_KEY for v in env_values):
        return True
    if j >= len(segment):
        return False
    sub, rest = segment[j], segment[j + 1:]
    if sub not in _BYPASS_SUBS:
        return False
    if "--no-verify" in rest:
        return True
    # Short -n == --no-verify ONLY for commit (push -n = --dry-run, merge -n = --no-stat).
    if sub == "commit" and any(
        t.startswith("-") and not t.startswith("--") and "n" in t for t in rest
    ):
        return True
    if sub == "push":
        if any(t.startswith("+") for t in rest):  # force via +refspec (git push origin +x)
            return True
        if "--force-with-lease" in rest or _has_force_flag(rest):
            return True
    return False


def main() -> None:
    """Read command from argv[1], exit 2 if any segment is destructive or a bypass."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    tokens = tokenize(command)
    if tokens is None:
        sys.exit(0)  # unparseable -> allow (see module docstring)
    for segment in split_segments(tokens):
        if is_destructive(segment) or is_bypass(segment):
            sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
