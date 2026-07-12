# guards

Interactive **Allow / Deny / Объясни** gate on destructive Bash operations, built on one
shared confirm-dialog engine so a fix lands once for every guard (no copy-paste drift).

Plus a **Stop / SubagentStop** guard (no dialog) that recovers the Opus-4.8 *dropped
tool-call* bug — when the model emits a tool call as plain text (namespaceless `<invoke>`),
the harness runs nothing and the turn ends silently; this guard detects that and forces one
clean re-emit. See [Dropped-tool-call recovery](#dropped-tool-call-recovery-stopsubagentstop).

## What it guards

| Guard | Blocks (prompts the dialog) | Allows |
|-------|------------------------------|--------|
| **git-destructive** | `git reset --hard`, `restore`, `clean -f`, `stash drop/clear`, `checkout <file>`/`-- <file>`; hook-bypass: `--no-verify`, `-c core.hooksPath=…`, force-push/`+refspec`, `JAINE_SKIP_*=` | `git status/log/commit`, `stash`/`stash pop`, `push -n`, `checkout -b`, quoted mentions |
| **process-kill** | `kill <literal PID>` (incl. `kill -9 -1`), `pkill <name>`, `killall <name>` — a PID the agent *observed*, or an unscopable by-name kill | `kill $!` / `kill "$PID"` / `kill %1` / `kill -0` (the agent's OWN, freshly-spawned processes), quoted/echoed mentions |
| **branch-delete / force-move** | deleting or force-moving a branch with **unmerged commits** (semantic merge-check): `branch -D/-d`, `push :b`/`+:b`/`--delete`, `branch -f` / `checkout -B` / `switch -C`, incl. wrapper- (`sudo`/`env`/…), env- (`GIT_DIR=…`) and chdir- (`env -C`) targeted forms, `@{-N}` (#296, #302, #299) | merged branches, nonexistent branches, `-r` remote-tracking deletes, non-forcing `-b`/`-c`, escape `GUARD_BRANCH_DELETE_OK=1` |

**process-kill threat model:** twice (b09485a0; 2026-06-14) a session pattern-matched a
long-running `agy` the **user owns** as a "zombie" and nearly killed it. A literal numeric
PID is one the agent *observed* (e.g. from `ps`) → can't be known to be its own → confirm.
A `$!`/`$VAR`/`%job` target is one the agent *spawned* → safe.

## Known limits (by design)

These are **lexical** guards — they read the literal command string, never execute it —
so two classes stay deliberately out of scope. Closing either would need to run arbitrary
code or put a git-config subprocess on *every* command, and a determined caller can always
step past a lexical speed-bump anyway:

- **Indirection that hides the git token.** A destructive call reached through a shell/git
  **alias**, a function body, `eval` / `sh -c "…"`, or an `env -S` / `--split-string` opaque
  quoted string isn't seen — e.g. `git -c alias.co=checkout co -B <unmerged>`, or a
  persistent `co=checkout` in your gitconfig. The command name (or operand) isn't literally
  present, so no lexer resolves it without executing code. Same accepted class as the
  original bash guard's quoted-token limit.
- **Self-contradictory flag pairs.** `git branch --force --no-force …` (set force, then
  cancel it) may still prompt; git itself errors or no-ops on these, so no work is lost.

The escape hatch `GUARD_BRANCH_DELETE_OK=1 <command>` covers any intentional exception.

## Architecture

```
hooks/
  hooks.json                       PreToolUse:Bash → guard-dispatch.sh ×3 (timeout 60)
                                   Stop + SubagentStop → guard-dropped-toolcall-detect.py (timeout 15)
  guard-dispatch.sh                generic: read tool JSON → run <detector> → on hit, exec engine
  guard-confirm-dialog.sh          ENGINE: Basso + osascript Allow/Deny/Объясни + fail-safe + # WHY:
  git_lexer.py                     SHARED lexical front-end: tokenize/segments/redirects, git
                                   global opts, env-assign + transparent-wrapper prefix skip
  guard-git-destructive-detect.py  shlex detector (adapted from the proven ~/.claude hook,
                                   extended by #294/#297/#302)
  guard-process-kill-detect.py     shlex detector (kill/pkill/killall policy above)
  guard-git-branch-delete-detect.py  lexical parse + SEMANTIC merge-check via real git calls (#296)
  guard-dropped-toolcall-detect.py Stop-hook detector (namespaceless tool-call → decision:block, one retry)
```

- **One engine, one dispatcher.** Adding a guard = one detector + one `hooks.json` line
  (`guard-dispatch.sh <detector> '<title>'`). The dialog lives in exactly one place.
- **shlex, not regex** (a regex can't tell `git reset --hard` from `grep "git reset --hard"`
  or `kill 1234` from `echo kill 1234`). All detectors tokenize with quote/operator awareness
  through the one shared `git_lexer.py` (#300) — a lexer fix lands once for every guard.
- **Fail-safe = block.** Esc / no-GUI / 55 s timeout → DENY (the 55 s auto-dismiss stays below
  the 60 s hook timeout so the hook returns DENY itself instead of being killed → fail-open).
- **Fail-open = allow** only for *detection*: unparseable JSON / missing detector / parse error
  → allow (these guards prevent accidental harm; a false block on a live session is the worse bug).

## Dropped-tool-call recovery (Stop/SubagentStop)

A distinct guard, no dialog. `claude-opus-4-8` at large context intermittently emits a tool
call as **plain text** in its final turn — a stray bare word on its own line (`court` / `call`
/ `county`) then `<invoke name="...">` / `<parameter ...>` **without** the `antml:` namespace.
The harness sees text, runs no tool, and the turn ends — the work is silently skipped.

Forensics (2026-06-28, 27707 sessions / 29G): 34 drops in 9 sessions, **all** opus-4-8 (zero on
opus-4-6/4-7/sonnet/fable/haiku); median context 507K tokens; drops cluster consecutively
(lock-in). A **Stop hook** is the only automated interception point — PreToolUse/PostToolUse
never fire (there was no tool_use event).

```
hooks/guard-dropped-toolcall-detect.py
  reads Stop stdin JSON (transcript_path, stop_hook_active)
  → last assistant text, strip code fences/inline-code (so a QUOTED tag in docs is inert)
  → match namespaceless ^<invoke name=...> / ^<parameter ...> / ^<function_calls>, or a
    stray-token-then-tag corroborator
  → {"decision":"block","reason":...}  forces ONE clean re-emit
```

- **Loop guard.** Drops cluster, so blocking forever would livelock. If `stop_hook_active`
  is already true (we blocked last turn), the hook no-ops — exactly **one** forced retry, then
  it releases so the human regains control.
- **Damage-control, not a cure.** The root cause is the model; no hook fixes it. The durable
  fixes are *don't run Opus-4.8 on the 1M window for long sessions* and *start a fresh session*.
  This guard buys one auto-recovery attempt and a loud, accurate diagnostic.
- **Detection is validated** recall 34/34, false-positives 0/4 (real drops vs doc/quote/clean
  negatives). Fence-strip is the load-bearing FP-avoider; fails OPEN (allow stop) on any error.
- Fires on the audit log too: `~/.claude/hooks/guards.log` (`[dropped-toolcall] …`).

See memory `opus48-dropped-toolcall` for the full forensic metadata.

## The `# WHY:` protocol

On a block, Claude re-issues the command with a trailing `# WHY: <reason>`; the engine shows
that reason in the dialog. Pressing **Объясни** asks Claude to explain (plainly, in Russian)
before retrying. The reason is split off *before* detection, so a reason mentioning a flag or
PID can't trip the guard.

## Audit log

Every time a guard fires, the engine appends one line to **`~/.claude/hooks/guards.log`**:

```
2026-06-14T20:16:25+07:00 | guard=🛡 Убийство процесса | decision=DENY | command=kill 1234
```

Decisions: `ALLOW` / `DENY` / `EXPLAIN` (Объясни) / `BLOCKED-timeout` (55 s) / `BLOCKED-nogui`
(osascript failed / Esc). Only *fired* guards are logged — benign commands that pass detection
write nothing.

**Stable path, survives deploy.** Written to `~/.claude/hooks/` (machine-stable, same convention
as `bulldozer-*.log` / `jaine-delete-audit.log`), **not** `${CLAUDE_PLUGIN_ROOT}` — that is the
plugin cache, recreated on every update and cleaned ~7 days later, so logs there would be wiped on
each deploy. Override with `$GUARDS_LOG` (tests point it at a tmp). Best-effort: a write failure
never blocks the guard.

## Tests

```bash
./tests/run-all.sh
```

- `test_git_lexer.py` — the shared front-end: global opts, context args, wrapper-prefix skip
- `test_guard_git_destructive_detect.py` — detector cases (block/allow/FP), seeded from the proven
  hook's suite and extended by #294/#297/#302
- `test_guard_process_kill_detect.py` — kill/pkill/killall policy + the safe own-process forms +
  the #300 lexer-dedup contract
- `test_guard_git_branch_delete_detect.py` — semantic merge-check on live fixture repos: delete,
  push-refspec, force-move, env/chdir targeting (#296/#299)
- `test_guard_dropped_toolcall_detect.py` — drop signatures block, loop-guard + doc/quote negatives allow
- `test_guard_confirm_dialog.sh` — the engine's branches via an `osascript` stub (no real dialog)
- `test_guard_dispatch.sh` — the dispatch→detector→engine chain + fail-open

## Install

Distributed via the `jaine-custom` marketplace (so the guards reach every machine, versioned):

```
jaine-sync plugins install guards -m jaine-custom
```

Migration note: `guard-git-destructive` previously lived loose in `~/.claude/hooks/` +
`~/.claude/settings.json`. When this plugin is enabled, **remove that loose registration** —
otherwise both fire and every git-destructive command shows **two** dialogs.
