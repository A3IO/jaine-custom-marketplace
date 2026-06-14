#!/bin/bash
# Generic guard dispatcher for the `guards` plugin. ONE dispatcher for every guard — the
# only per-guard knowledge is the detector script + the dialog title (both passed as args
# from hooks.json), so adding a guard never copies the read-JSON / call-engine plumbing.
#
#   $1 = detector script name (in this dir), e.g. guard-git-destructive-detect.py
#   $2 = dialog title, e.g. "🛡 Опасная git-операция"
#
# Flow: read the Bash tool JSON from stdin -> extract the command -> run the detector on it
# (with any trailing "# WHY:" reason split off first, so a reason mentioning a flag/PID can't
# trip detection) -> on a hit (detector exit 2) delegate to the shared confirm-dialog engine.
#
# Fails OPEN (exit 0, allow) on a missing detector / unparseable JSON / python error: these
# guards prevent accidental harm, so a false block that breaks a live session is the worse bug.

DIR="$(dirname "$0")"
DETECTOR="$DIR/$1"
TITLE="$2"

INPUT=$(cat)
[ -z "$INPUT" ] && exit 0

CMD=$(printf '%s' "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" 2>/dev/null)
[ -z "$CMD" ] && exit 0

# Split off the trailing "# WHY:" reason before detection — from the LAST " # WHY:" (with a
# leading space, the form Claude appends), matching the engine's CMD_DISPLAY strip exactly. Using
# `% ` (suffix, space-anchored) instead of `%%` (first match) avoids cutting a "# WHY:" that lives
# inside an earlier quoted argument, which would break shlex and silently fail-open.
CMD_CHECK="${CMD% # WHY:*}"

# A missing detector must FAIL OPEN, not dialog: `python3 <nonexistent.py>` exits 2 (ENOENT),
# which is indistinguishable from the detector's own "dangerous" exit 2 — without this guard a
# wiped/partial plugin cache would pop a dialog on EVERY Bash command.
[ -f "$DETECTOR" ] || exit 0

# Detector exit code: 2 = dangerous (show the dialog), anything else = safe (allow).
python3 "$DETECTOR" "$CMD_CHECK" 2>/dev/null
[ "$?" -eq 2 ] || exit 0

exec "$DIR/guard-confirm-dialog.sh" "$TITLE" "$CMD"
