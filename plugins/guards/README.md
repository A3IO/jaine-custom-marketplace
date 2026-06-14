# guards

Interactive **Allow / Deny / Объясни** gate on destructive Bash operations, built on one
shared confirm-dialog engine so a fix lands once for every guard (no copy-paste drift).

## What it guards

| Guard | Blocks (prompts the dialog) | Allows |
|-------|------------------------------|--------|
| **git-destructive** | `git reset --hard`, `restore`, `clean -f`, `stash drop/clear`, `checkout <file>`/`-- <file>`; hook-bypass: `--no-verify`, `-c core.hooksPath=…`, force-push/`+refspec`, `JAINE_SKIP_*=` | `git status/log/commit`, `stash`/`stash pop`, `push -n`, `checkout -b`, quoted mentions |
| **process-kill** | `kill <literal PID>` (incl. `kill -9 -1`), `pkill <name>`, `killall <name>` — a PID the agent *observed*, or an unscopable by-name kill | `kill $!` / `kill "$PID"` / `kill %1` / `kill -0` (the agent's OWN, freshly-spawned processes), quoted/echoed mentions |

**process-kill threat model:** twice (b09485a0; 2026-06-14) a session pattern-matched a
long-running `agy` the **user owns** as a "zombie" and nearly killed it. A literal numeric
PID is one the agent *observed* (e.g. from `ps`) → can't be known to be its own → confirm.
A `$!`/`$VAR`/`%job` target is one the agent *spawned* → safe.

## Architecture

```
hooks/
  hooks.json                       PreToolUse:Bash → guard-dispatch.sh ×2 (timeout 60)
  guard-dispatch.sh                generic: read tool JSON → run <detector> → on hit, exec engine
  guard-confirm-dialog.sh          ENGINE: Basso + osascript Allow/Deny/Объясни + fail-safe + # WHY:
  guard-git-destructive-detect.py  shlex detector (verbatim from the proven ~/.claude hook)
  guard-process-kill-detect.py     shlex detector (kill/pkill/killall policy above)
```

- **One engine, one dispatcher.** Adding a guard = one detector + one `hooks.json` line
  (`guard-dispatch.sh <detector> '<title>'`). The dialog lives in exactly one place.
- **shlex, not regex** (a regex can't tell `git reset --hard` from `grep "git reset --hard"`
  or `kill 1234` from `echo kill 1234`). Both detectors tokenize with quote/operator awareness.
- **Fail-safe = block.** Esc / no-GUI / 55 s timeout → DENY (the 55 s auto-dismiss stays below
  the 60 s hook timeout so the hook returns DENY itself instead of being killed → fail-open).
- **Fail-open = allow** only for *detection*: unparseable JSON / missing detector / parse error
  → allow (these guards prevent accidental harm; a false block on a live session is the worse bug).

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

- `test_guard_git_destructive_detect.py` — detector cases (block/allow/FP), verbatim from the proven hook
- `test_guard_process_kill_detect.py` — kill/pkill/killall policy + the safe own-process forms
- `test_guard_confirm_dialog.sh` — the engine's branches via an `osascript` stub (no real dialog)
- `test_guard_dispatch.sh` — the dispatch→detector→engine chain for both detectors + fail-open

## Install

Distributed via the `jaine-custom` marketplace (so the guards reach every machine, versioned):

```
jaine-sync plugins install guards -m jaine-custom
```

Migration note: `guard-git-destructive` previously lived loose in `~/.claude/hooks/` +
`~/.claude/settings.json`. When this plugin is enabled, **remove that loose registration** —
otherwise both fire and every git-destructive command shows **two** dialogs.
