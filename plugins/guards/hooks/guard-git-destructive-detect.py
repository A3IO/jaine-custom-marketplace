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
import shlex
import sys

# Source-file extensions: a positional arg to `git checkout` ending in one of
# these is treated as a file being discarded, not a branch/tag name.
_DANGER_EXT = {
    "py", "sh", "m", "swift", "h", "md", "json", "plist", "txt", "js", "ts",
    "tsx", "jsx", "go", "rs", "c", "cpp", "cc", "hpp", "rb", "java", "kt",
    "yaml", "yml", "toml", "cfg", "ini", "conf", "xml", "html", "css", "svg",
    "sql", "lua", "pl", "php", "vue",
}

# Shell command-boundary punctuation passed to shlex. The default set (`();<>|&`,
# which includes `;`) PLUS backtick: a backtick command substitution `...` opens a new
# command context just like $( ), so splitting on it surfaces a destructive git hidden
# in `git clean -fd`. (Leading `VAR=val` env assignments before git ARE handled — see
# _env_prefix_end, #297. Quoted substitutions like "$(...)" and true wrapper forms that
# push git off the first token — `command`/`env`/`xargs` git … — stay OUT of scope: the
# threat model is accidental work loss, not adversarial evasion.)
_PUNCT = "();<>|&" + "`"

# Tokens that separate one shell command from the next.
_SEP = {";", ";;", "&", "&&", "|", "||", "|&", "(", ")", "`", "\n"}

# A token that begins a redirection (everything after it is not a git argument).
_REDIR = re.compile(r"^(\d*[<>]|&>|>&)")


def tokenize(command: str) -> list[str] | None:
    """Tokenize a shell command, respecting quotes and grouping operators.

    punctuation_chars (_PUNCT) makes shlex emit `;`, `|`, `&`, `<`, `>`, `(`, `)`
    and backtick as their own tokens even without surrounding whitespace (so
    `foo;git ...` splits correctly), while posix quote handling keeps `"git reset"`
    as one token. Returns a token list, or None if the command cannot be parsed.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
    lexer.whitespace_split = True
    # Keep shlex's default '#' comment handling: a shell comment never executes, so a
    # comment that merely MENTIONS a destructive command (e.g. `# see $(git reset --hard)`)
    # must NOT trigger. (`# WHY:` is already stripped upstream by the .sh wrapper.)
    try:
        return list(lexer)
    except ValueError:
        return None


def split_segments(tokens: list[str]) -> list[list[str]]:
    """Split a flat token list into per-command segments on separator tokens."""
    segment: list[str] = []
    segments: list[list[str]] = []
    for token in tokens:
        if token in _SEP:
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    return segments


def strip_redirects(tokens: list[str]) -> list[str]:
    """Drop a redirection operator and everything after it within a segment."""
    kept: list[str] = []
    for token in tokens:
        if _REDIR.match(token):
            break
        kept.append(token)
    return kept


def _has_force_flag(tokens: list[str]) -> bool:
    """True if a short flag bundle carries -f, or --force is present."""
    return any(
        token == "--force"
        or (token.startswith("-") and not token.startswith("--") and "f" in token)
        for token in tokens
    )


def _looks_like_file(arg: str) -> bool:
    """True if a checkout arg looks like a source file (vs a branch/tag name)."""
    base = arg.rsplit("/", 1)[-1]
    if "." not in base:
        return False
    return base.rsplit(".", 1)[-1].lower() in _DANGER_EXT


# Git global options that consume the FOLLOWING token as their argument when not
# written as --opt=value (#294: `git -C /p reset --hard` must not hide `reset`).
# The full arg-taking set from git's SYNOPSIS; `--super-prefix` is rejected by
# current git but kept for cross-version safety (blocking an inert command is fine).
_GLOBAL_ARG_OPTS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--config-env", "--attr-source", "--super-prefix",
}

# Globals that make real git print something and exit WITHOUT running any
# sub-command that may follow (verified live: `git --html-path status` prints the
# doc path and never runs status). Seeing one of these makes the segment inert.
# Bare --exec-path is print-and-exit too; --exec-path=<p> falls through to the
# generic argless branch and keeps the sub-command visible.
_TERMINATING_OPTS = {
    "--version", "-v", "--help", "-h",
    "--html-path", "--man-path", "--info-path", "--exec-path",
}


def _skip_global_opts(segment: list[str], start: int) -> tuple[int, list[str], list[str]]:
    """Skip git global options (with their arguments) up to the sub-command.

    Returns (index of the sub-command in segment, values passed via `-c`, values
    passed via `--config-env[= ]`). The index equals len(segment) when only
    options follow. `--opt=value` forms are single self-contained tokens; an
    unknown `-*` token before the sub-command is treated as an argless flag
    (fail-open, see module docstring).
    """
    c_values: list[str] = []
    env_values: list[str] = []
    j = start
    while j < len(segment):
        tok = segment[j]
        if tok in _TERMINATING_OPTS:
            # git prints and exits: nothing after runs, and already-collected
            # -c/--config-env values never take effect either
            return len(segment), [], []
        if tok in _GLOBAL_ARG_OPTS:
            if j + 1 < len(segment):
                if tok == "-c":
                    c_values.append(segment[j + 1])
                elif tok == "--config-env":
                    env_values.append(segment[j + 1])
            j += 2
            continue
        if tok.startswith("--config-env="):
            env_values.append(tok.split("=", 1)[1])
            j += 1
            continue
        if tok.startswith("-"):
            j += 1
            continue
        break
    # An arg-taking option at the very end can push j past len(segment); clamp so
    # the documented "== len(segment) when only options follow" contract holds.
    return min(j, len(segment)), c_values, env_values


# Leading `VAR=val` environment assignments before `git` (`GIT_TRACE=1 git …`) don't
# push git off the command — real git runs the sub-command with those vars set. Both
# destructive and bypass detection skip them to reach the real `git` token (#294/#297).
# `\+?=` also matches bash/zsh append form `VAR+=val git …` (verified: git still runs).
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")


def _env_prefix_end(segment: list[str]) -> int:
    """Index of the first token that is not a leading `VAR=val` env assignment."""
    i = 0
    while i < len(segment) and _ENV_ASSIGN.match(segment[i]):
        i += 1
    return i


def is_destructive(segment: list[str]) -> bool:
    """True if a single command segment is a work-discarding git operation."""
    segment = strip_redirects(segment)
    i = _env_prefix_end(segment)
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
    # Leading env assignments (VAR=val) before `git` — a JAINE_SKIP_* among them is
    # itself the bypass signal; the rest are just skipped to reach the `git` token.
    i = _env_prefix_end(segment)
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
