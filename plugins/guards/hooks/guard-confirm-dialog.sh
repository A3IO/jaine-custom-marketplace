#!/bin/bash
# Shared confirm-dialog ENGINE for the `guards` plugin — the SINGLE source of truth for the
# interactive macOS Allow/Deny/Объясни gate. Guards detect a dangerous command (via
# guard-dispatch.sh), then `exec` this with a title + the command. Centralising the dialog
# means a fix lands once for every guard — no copy-paste drift between guards.
#
#   $1 = dialog title (e.g. "🛡 Опасная git-операция")
#   $2 = the command (may carry a trailing "# WHY: <reason>" that Claude appended)
#
# Buttons: Allow (default — highlighted, Enter-activated, #311) -> exit 0 (run the command) ·
# Deny -> exit 2 (block) · Объясни -> exit 2 (block; ask Claude to re-issue with a # WHY: reason).
# Fail-safe: osascript failure (no GUI / SSH) OR Esc/Cancel OR the 55s timeout -> exit 2.
# Security: the command + title are passed to osascript via `on run argv`, never spliced
# into an -e string, so $(...) / backticks inside them cannot execute.
#
# Extracted verbatim from the proven ~/.claude/hooks/guard-git-destructive.sh dialog block
# (only the title was parameterised). Covered by tests/test_guard_confirm_dialog.sh.

TITLE="$1"
CMD="$2"
[ -z "$CMD" ] && exit 0   # nothing to confirm

# Reason = text after the LAST "# WHY:". Command shown WITHOUT that tail (no duplicate).
REASON=$(printf '%s' "$CMD" | sed -n 's/.*# WHY:[[:space:]]*//p')
[ -z "$REASON" ] && REASON="⚠ причина не указана"
CMD_DISPLAY="${CMD% # WHY:*}"
BODY="${REASON}

Команда:
${CMD_DISPLAY}"

# Audit log — STABLE path. NOT ${CLAUDE_PLUGIN_ROOT}: that is the plugin's cache dir, recreated
# on every update and cleaned ~7 days later, so logs written there would vanish on deploy. The
# convention across our hooks (bulldozer-*.log, jaine-delete-audit.log) is ~/.claude/hooks/*.log
# — machine-stable, survives plugin updates. Override via $GUARDS_LOG (tests point it at a tmp).
# Best-effort: a logging failure must NEVER block the guard.
GUARDS_LOG="${GUARDS_LOG:-$HOME/.claude/hooks/guards.log}"
_audit() {  # $1 = decision
    printf '%s | guard=%s | decision=%s | command=%s\n' \
        "$(date -Iseconds)" "$TITLE" "$1" "$(printf '%s' "$CMD_DISPLAY" | tr '\n\r' '  ')" \
        >> "$GUARDS_LOG" 2>/dev/null || true
}

# Background beeper; trap stops it on normal exit AND on TERM/INT, so it never outlives the
# dialog even if the hook is signalled (e.g. CC timeout kill).
( while true; do afplay /System/Library/Sounds/Basso.aiff; sleep 0.5; done ) &
BEEPER=$!
trap 'kill "$BEEPER" 2>/dev/null' EXIT INT TERM

# Modal dialog. Title + command passed via argv (no shell/AppleScript injection). Default
# button "Allow" (#311): highlighted + Enter-activated, so a keyboard operator confirms fast
# instead of hunting the mouse and losing to the timeout. Deliberate tradeoff: a stray Enter
# now allows — Chris judged a stray-Enter DENY of a legit command the worse failure; silence
# still fail-safe DENIES. "giving up after 55" auto-dismisses BELOW the CC hook timeout (60s,
# set in hooks.json) so the hook returns a DENY itself instead of being killed by CC — which
# would fail-OPEN (allow). Sentinel "GAVEUP" = timed out.
BTN=$(osascript - "$BODY" "$TITLE" <<'APPLESCRIPT' 2>/dev/null
on run argv
    set bodyText to item 1 of argv
    set titleText to item 2 of argv
    set r to display dialog bodyText with title titleText buttons {"Deny", "Объясни", "Allow"} default button "Allow" with icon caution giving up after 55
    if gave up of r then
        return "GAVEUP"
    end if
    return button returned of r
end run
APPLESCRIPT
)
RC=$?
kill "$BEEPER" 2>/dev/null
trap - EXIT

if [ $RC -ne 0 ]; then
    # osascript failed (no GUI / SSH) or user pressed Esc/Cancel — block safely.
    _audit "BLOCKED-nogui"
    echo "[GUARD] Диалог не подтверждён (Esc/Cancel/нет GUI): $CMD" >&2
    echo "[GUARD] Заблокировано. Повтори с # WHY: <причина> или спроси Криса." >&2
    exit 2
fi

case "$BTN" in
    Allow)
        _audit "ALLOW"
        exit 0 ;;
    Объясни)
        _audit "EXPLAIN"
        echo "[GUARD] Крис нажал [Объясни] — он хочет понять, ЗАЧЕМ эта команда." >&2
        echo "[GUARD] Объясни ему ПРОСТО и ПО-РУССКИ (что делаешь + зачем), затем повтори команду, дописав: # WHY: <по-русски, простыми словами>" >&2
        exit 2 ;;
    GAVEUP)
        _audit "BLOCKED-timeout"
        echo "[GUARD] Нет ответа за 55с — заблокировано (fail-safe DENY): $CMD" >&2
        echo "[GUARD] Если команда нужна — запусти снова и ответь в диалоге." >&2
        exit 2 ;;
    *)
        _audit "DENY"
        echo "[GUARD] Крис отклонил: $CMD" >&2
        exit 2 ;;
esac
