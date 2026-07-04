"""Shared lexical front-end reused by every guards git detector.

Git-destructive today and branch-delete next share ONE parser and ONE
`-C`/global-option fix through this module.
"""
import collections
import re
import shlex

# Import contract: detectors run as `python3 <abs-path-to-detector>`, so
# sys.path[0] is the hooks/ dir and sibling detectors can use bare `import git_lexer`.

# Shell command-boundary punctuation passed to shlex. The default set (`();<>|&`,
# which includes `;`) PLUS backtick: a backtick command substitution `...` opens a new
# command context just like $( ), so splitting on it surfaces a destructive git hidden
# in `git clean -fd`. Leading `VAR=val` env assignments and transparent wrappers
# (`command`/`env`/`xargs`/`sudo`/`nice`/`nohup`) before the real command are handled
# by _command_prefix_end (#297/#302), without doing a loose whole-segment scan.
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


# Git global options that consume the FOLLOWING token as their argument when not
# written as --opt=value (#294: `git -C /p reset --hard` must not hide `reset`).
# The full arg-taking set from git's SYNOPSIS; `--super-prefix` is rejected by
# current git but kept for cross-version safety (blocking an inert command is fine).
_GLOBAL_ARG_OPTS = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--config-env", "--attr-source", "--super-prefix",
}

# Repo-locating global options threaded into every semantic git call for #541.
# Deliberately NOT threaded: -c/--config-env/--exec-path/--attr-source/
# --super-prefix, because they do not change WHICH repo is targeted.
_CONTEXT_ARG_OPTS = {"-C", "--git-dir", "--work-tree", "--namespace"}
_CONTEXT_EQ_PREFIXES = ("--git-dir=", "--work-tree=", "--namespace=")
_CONTEXT_FLAG_OPTS = {"--bare"}

# Globals that make real git print something and exit WITHOUT running any
# sub-command that may follow (verified live: `git --html-path status` prints the
# doc path and never runs status). Seeing one of these makes the segment inert.
# Bare --exec-path is print-and-exit too; --exec-path=<p> falls through to the
# generic argless branch and keeps the sub-command visible.
_TERMINATING_OPTS = {
    "--version", "-v", "--help", "-h",
    "--html-path", "--man-path", "--info-path", "--exec-path",
}


GlobalOpts = collections.namedtuple(
    "GlobalOpts", ("sub_index", "c_values", "env_values", "context_args")
)


def parse_global_opts(segment: list[str], start: int) -> GlobalOpts:
    """Parse git global options (with their arguments) up to the sub-command."""
    c_values: list[str] = []
    env_values: list[str] = []
    context_args: list[str] = []
    j = start
    while j < len(segment):
        tok = segment[j]
        if tok in _TERMINATING_OPTS:
            # git prints and exits: nothing after runs, and already-collected
            # -c/--config-env values never take effect either
            return GlobalOpts(len(segment), [], [], [])
        if tok in _GLOBAL_ARG_OPTS:
            if j + 1 < len(segment):
                if tok == "-c":
                    c_values.append(segment[j + 1])
                elif tok == "--config-env":
                    env_values.append(segment[j + 1])
                if tok in _CONTEXT_ARG_OPTS:
                    context_args.extend([tok, segment[j + 1]])
            j += 2
            continue
        if tok.startswith("--config-env="):
            env_values.append(tok.split("=", 1)[1])
            j += 1
            continue
        if tok.startswith(_CONTEXT_EQ_PREFIXES):
            context_args.append(tok)
            j += 1
            continue
        if tok in _CONTEXT_FLAG_OPTS:
            context_args.append(tok)
            j += 1
            continue
        if tok.startswith("-"):
            j += 1
            continue
        break
    # An arg-taking option at the very end can push j past len(segment); clamp so
    # the documented "== len(segment) when only options follow" contract holds.
    return GlobalOpts(min(j, len(segment)), c_values, env_values, context_args)


def _skip_global_opts(segment: list[str], start: int) -> tuple[int, list[str], list[str]]:
    """Skip git global options (with their arguments) up to the sub-command.

    Returns (index of the sub-command in segment, values passed via `-c`, values
    passed via `--config-env[= ]`). The index equals len(segment) when only
    options follow. `--opt=value` forms are single self-contained tokens; an
    unknown `-*` token before the sub-command is treated as an argless flag
    (fail-open, see module docstring).
    """
    g = parse_global_opts(segment, start)
    return g.sub_index, g.c_values, g.env_values


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


_TRANSPARENT_WRAPPERS = {"command", "env", "xargs", "sudo", "nice", "nohup"}

_WRAPPER_ARG_FLAGS = {
    "sudo": {
        "-u", "-g", "-p", "-C", "-D", "-U", "-R", "-T", "-t", "-h", "-r",
        "--user", "--group", "--host", "--prompt", "--chdir", "--role", "--type",
        "--other-user", "--close-from", "--command-timeout",
    },
    # GNU optional-arg long opts (--replace/--eof/--max-lines) bind their value
    # with `=` ONLY, so they are deliberately absent: a bare form is argless and
    # the next token is the real command (`xargs --replace git …` RUNS git).
    # BSD -S replsize IS required-arg (red-team: `xargs -I{} -S 999 git …`).
    "xargs": {
        "-I", "-J", "-n", "-P", "-L", "-s", "-S", "-a", "-E", "-d", "-R",
        "--arg-file", "--delimiter", "--max-args", "--max-chars",
        "--max-procs", "--process-slot-var",
    },
    # -S/--split-string is deliberately absent: its value IS the command start
    # (`env -S git reset --hard` RUNS git — verified on macOS), so the spaced
    # form must land on the next token. Quoted/glued values (`env -S'git …'`,
    # `--split-string=git`) stay single opaque tokens — a documented limit
    # shared with the original bash guard.
    "env": {
        "-u", "-C", "-P",
        "--unset", "--chdir",
    },
    "nice": {"-n", "--adjustment"},
    "command": set(),
    "nohup": set(),
}

# Wrappers that accept leading VAR=val operands of their own. The others (nice,
# nohup, command, xargs) would exec the assignment as a command name (ENOENT)
# and never reach git — the scan must stop there instead of skipping it.
_ASSIGN_WRAPPERS = {"env", "sudo"}

# sudo modes that never execute the wrapped command: version print, permission
# listing (-l/-ll/--list), credential validation (-v/--validate), file-edit mode
# (-e/--edit, edits FILES), timestamp removal (-K/--remove-timestamp, "may not be
# specified with a command"). NOT lowercase -k: `sudo -k cmd` resets the
# timestamp and then RUNS cmd. Matched as exact tokens only — clustered
# spellings (`-ln`, `-nv`, `-en`) fall through to the confirm dialog, accepted
# fail-safe noise on forms no agent realistically emits.
_SUDO_TERMINATING = {
    "-V", "-l", "-ll", "--list", "-v", "--validate",
    "-e", "--edit", "-K", "--remove-timestamp",
}


def _command_prefix_end(segment: list[str]) -> int:
    """Index of the first token after env assignments and transparent wrappers.

    Wrapper commands are matched by basename and only skipped while they are the
    next command token; a non-wrapper operand stops the scan, so `grep git file`
    and `foo bar git branch -D x` stay fail-open. `echo feat/x | xargs git branch -D`
    carries the branch name on stdin, lexically invisible — a DOCUMENTED allow,
    parity with the original STATUSLINE bash guard; red-team should not
    re-litigate it.
    """
    i = 0
    allow_assigns = True
    while i < len(segment):
        if allow_assigns and _ENV_ASSIGN.match(segment[i]):
            i += 1
            continue

        wrapper = segment[i].rsplit("/", 1)[-1]
        if wrapper not in _TRANSPARENT_WRAPPERS:
            break

        allow_assigns = wrapper in _ASSIGN_WRAPPERS
        i += 1
        arg_flags = _WRAPPER_ARG_FLAGS[wrapper]
        while i < len(segment):
            tok = segment[i]
            if tok == "--":
                i += 1
                break
            # Wrapper help/version (and sudo's non-exec modes) print and exit —
            # GNU shows the text, BSD errors on the illegal option; either way
            # the wrapped command never runs (same class as git's _TERMINATING_OPTS).
            if tok in ("--help", "--version") or (wrapper == "sudo" and tok in _SUDO_TERMINATING):
                return len(segment)
            # -v/-V (also clustered, e.g. -pv) prints resolutions, executes nothing
            if (wrapper == "command" and tok.startswith("-")
                    and not tok.startswith("--") and any(c in "vV" for c in tok[1:])):
                return len(segment)
            # env -0/--null is print-mode ("cannot specify command with -0") — the
            # command never runs. Only argless cluster chars (i/v) may ride along;
            # a `0` glued to arg-taking -u is a VARIABLE NAME, not this mode.
            if wrapper == "env" and (tok == "--null" or (
                    tok.startswith("-") and not tok.startswith("--")
                    and "0" in tok[1:] and all(c in "0iv" for c in tok[1:]))):
                return len(segment)
            if tok == "-":
                # `env - cmd` is the historic spelling of `env -i cmd`: env clears
                # the environment and RUNS cmd (verified live) — keep scanning.
                # For other wrappers a bare dash is an operand — stop.
                if wrapper == "env":
                    i += 1
                    continue
                break
            if not tok.startswith("-"):
                break
            if tok.startswith("--"):
                if "=" in tok:
                    i += 1
                elif tok in arg_flags and i + 1 < len(segment):
                    i += 2
                else:
                    i += 1
                continue

            if tok in arg_flags and i + 1 < len(segment):
                i += 2
                continue
            if any(tok.startswith(flag) and tok != flag for flag in arg_flags if flag.startswith("-") and not flag.startswith("--")):
                i += 1
                continue
            # short BUNDLE: getopt binds the value to the FIRST arg-taking char —
            # trailing (`env -iC /dir`) consumes the next token, mid-bundle
            # (`env -uC`) binds the remainder and the bundle is self-contained
            # (#299 red-team r3, verified live on macOS env/sudo)
            if len(tok) > 2:
                for pos in range(1, len(tok)):
                    if "-" + tok[pos] in arg_flags:
                        if pos == len(tok) - 1 and i + 1 < len(segment):
                            i += 1
                        break
            i += 1

    return i
