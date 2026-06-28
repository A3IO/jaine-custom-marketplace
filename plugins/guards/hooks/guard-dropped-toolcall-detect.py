#!/usr/bin/env python3
"""Detect an Opus-4.8 dropped tool-call and force one clean re-emit.

Stop / SubagentStop hook. The Opus-4.8 model (large-context, June-2026 onward)
intermittently emits a tool call as PLAIN TEXT in its final turn: a stray bare
word on its own line (``court`` / ``call`` / ``county``) followed by
``<invoke name="...">`` / ``<parameter ...>`` WITHOUT the ``antml:`` namespace.
The harness sees text, runs no tool, and the turn ends — the work is silently
skipped (the "Form A" false-success risk).

This is the ONLY automated interception point: PreToolUse/PostToolUse never fire
(there was no tool_use event). A Stop hook fires at end-of-turn and can read the
just-finished assistant message from the transcript.

Forensics (2026-06-28, 27707 sessions / 29G): 34 drops in 9 sessions, ALL
claude-opus-4-8 (0 on opus-4-6/4-7/sonnet/fable/haiku); median context 507K
tokens; drops cluster consecutively (lock-in). Detection regex validated
recall 34/34, false-positives 0/4 against real drops + doc/quote negatives.
See memory: opus48-dropped-toolcall.

Behaviour:
  * reads the Stop-hook stdin JSON (``transcript_path``, ``stop_hook_active``)
  * detects the namespaceless-tag signature in the LAST assistant text message,
    AFTER stripping code fences/inline-code (so docs that QUOTE the tag don't trip)
  * emits ``{"decision":"block","reason":...}`` to force exactly ONE re-emit
  * LOOP GUARD: if ``stop_hook_active`` is already true (we blocked last turn),
    no-op — drops cluster, so blocking forever would livelock. One forced retry,
    then release so the human regains control.

Fails OPEN (exit 0, allow stop) on ANY error — a false block that wedges a live
session is worse than a missed exotic form. Logging is best-effort to a STABLE
path (``~/.claude/hooks/guards.log``; ``$GUARDS_LOG`` overrides for tests), never
under the plugin cache (wiped on update).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

# A namespaceless invoke/parameter/function_calls tag at the START of a line.
# The REAL protocol always carries ``antml:`` (``<invoke …>``), so a flush-left
# ``<invoke name="X">`` in a FINAL assistant text message is the dropped-call signature.
# Anchoring to ^ + requiring name="..." is what avoids matching ordinary prose.
_DROPPED_CALL = re.compile(
    r'^(?:<invoke\s+name="[^"]+"\s*>'
    r'|<parameter\s+name="[^"]+"\s*>'
    r'|<function_calls>)',
    re.MULTILINE,
)
# Corroborator: a known stray token alone on a line, immediately before a naked tag.
_STRAY_THEN_TAG = re.compile(
    r'^(?:court|call|county)\s*\n+<(?:invoke|parameter|function_calls)\b',
    re.MULTILINE,
)
# Fenced + inline code — stripped BEFORE matching so a quoted `<invoke>` in docs is inert.
_FENCE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)


def _log(msg: str) -> None:
    """Best-effort append to a stable log path; never raises."""
    path = os.environ.get("GUARDS_LOG") or os.path.expanduser("~/.claude/hooks/guards.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(path, "a") as fh:
            fh.write(f"{stamp} [dropped-toolcall] {msg}\n")
    except OSError:
        pass


def strip_code(text: str) -> str:
    """Remove fenced/inline code so documentation that QUOTES the tag can't trip us."""
    return _FENCE.sub("", text)


def is_dropped_call(text: str) -> bool:
    scrubbed = strip_code(text)
    return bool(_DROPPED_CALL.search(scrubbed) or _STRAY_THEN_TAG.search(scrubbed))


def last_assistant_text(transcript_path: str) -> str:
    """Return the concatenated text blocks of the LAST assistant message (JSONL)."""
    last = ""
    try:
        with open(transcript_path, errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                if msg.get("role") != "assistant":
                    continue
                parts = []
                content = msg.get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text", ""))
                elif isinstance(content, str):
                    parts.append(content)
                if parts:
                    last = "\n".join(parts)  # overwrite → ends as the final assistant turn
    except OSError:
        return ""
    return last


_REASON = (
    "DROPPED TOOL CALL DETECTED. Your previous message contained a tool invocation "
    "emitted as PLAIN TEXT — the `antml:` namespace was stripped (tags like "
    "`<invoke name=...>` / `<parameter ...>` appeared without it, sometimes preceded "
    "by a stray word such as 'court'/'call'/'county'). The harness therefore ran NO "
    "tool and your turn ended prematurely, so that work did NOT happen.\n\n"
    "RE-EMIT the intended tool call(s) now as a REAL tool invocation. Do not describe "
    "the call in prose — actually invoke the tool. If you cannot produce a well-formed "
    "call on this next attempt, STOP and tell the user to start a fresh session: this "
    "is a known large-context Opus-4.8 defect (it locks in once it starts)."
)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # can't parse input → allow stop

    # Loop guard: we already forced one retry last turn → release so the human steps in.
    if data.get("stop_hook_active") is True:
        sys.exit(0)

    transcript_path = data.get("transcript_path")
    if not transcript_path:
        sys.exit(0)

    text = last_assistant_text(transcript_path)
    if text and is_dropped_call(text):
        _log(f"blocked stop, forcing re-emit (session={data.get('session_id', '?')})")
        print(json.dumps({"decision": "block", "reason": _REASON}))
        sys.exit(0)

    sys.exit(0)  # clean → allow stop


if __name__ == "__main__":
    main()
