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
# in `git clean -fd`. (Quoted substitutions like "$(...)" and wrapper/prefix forms that
# push git off the first token — `command`/`env`/`xargs` git …, `GIT_DIR=… git …` — stay
# OUT of scope: the threat model is accidental work loss, not adversarial evasion.)
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


def is_destructive(segment: list[str]) -> bool:
    """True if a single command segment is a work-discarding git operation."""
    segment = strip_redirects(segment)
    if len(segment) < 2:
        return False
    if segment[0].rsplit("/", 1)[-1] != "git":
        return False
    sub, rest = segment[1], segment[2:]

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
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SKIP_ENV = re.compile(r"^JAINE_SKIP_(PUSH_GUARD|AUTO_REBUILD|AUTO_PUBLISH)=")


def _hookspath_disabled(val: str) -> bool:
    """True if a `-c` value disables git hooks: core.hooksPath=<empty|/dev/null>."""
    if not val.startswith("core.hooksPath="):
        return False
    v = val.split("=", 1)[1]
    return v == "" or v == "/dev/null" or v.endswith("/dev/null")


def is_bypass(segment: list[str]) -> bool:
    """True if a segment disables git safety hooks or force-pushes over history."""
    segment = strip_redirects(segment)
    i = 0
    # Leading env assignments (VAR=val) before `git` — flag the JAINE_SKIP_* guards.
    while i < len(segment) and _ENV_ASSIGN.match(segment[i]):
        if _SKIP_ENV.match(segment[i]):
            return True
        i += 1
    if i >= len(segment) or segment[i].rsplit("/", 1)[-1] != "git":
        return False
    # Global git options between `git` and the sub-command (e.g. -c core.hooksPath=…).
    j = i + 1
    while j < len(segment):
        tok = segment[j]
        if tok == "-c" and j + 1 < len(segment):
            if _hookspath_disabled(segment[j + 1]):
                return True
            j += 2
            continue
        if tok.startswith("-"):
            j += 1  # some other global option
            continue
        break
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
