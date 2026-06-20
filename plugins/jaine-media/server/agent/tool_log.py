"""Centralized JSONL tool-call log for jaine-media.

One JSON object per line — ``json.loads(line)`` with no custom parser, so Claude
can read the log back to analyze dogfood runs. Free-form media text (questions,
Gemini answers, file paths with cyrillic/spaces/pipes/newlines) serializes
cleanly, which a pipe-delimited grammar could not. Written to a STABLE path that
survives the plugin-cache wipe, rotated by stdlib so it never grows unbounded.

Path:   JAINE_MEDIA_LOG env → ~/.claude/logs/jaine-media.jsonl
Line:   {ts, tool, ok, digest, **fields}   (ensure_ascii=False — cyrillic intact)

Best-effort: logging a tool outcome must NEVER raise or affect the tool's return,
identical contract to every hook in the ecosystem (|| true / except: pass).
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "jaine_media.tools"
_logger: logging.Logger | None = None
_handler_path: str | None = None


def _resolve_log_path() -> Path:
    return Path(os.environ.get("JAINE_MEDIA_LOG")
                or os.path.expanduser("~/.claude/logs/jaine-media.jsonl"))


def _reset() -> None:
    """Drop the cached logger + its handler (test isolation across env paths)."""
    global _logger, _handler_path
    if _logger is not None:
        for h in list(_logger.handlers):
            _logger.removeHandler(h)
            h.close()
    _logger, _handler_path = None, None


def _get_logger() -> logging.Logger:
    """Lazily build ONE named logger + RotatingFileHandler; reuse it. Rebuilds when
    the resolved path changes (tests repoint JAINE_MEDIA_LOG). Attached to a NAMED
    logger with propagate=False so FastMCP's root/stderr handler is untouched."""
    global _logger, _handler_path
    path = str(_resolve_log_path())
    if _logger is not None and _handler_path == path:
        return _logger
    if _logger is not None:                    # path changed → rebuild
        _reset()
    lg = logging.getLogger(_LOGGER_NAME)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(os.environ.get("JAINE_MEDIA_LOG_MAXBYTES", "5000000"))
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))   # message is pre-serialized JSON
    lg.addHandler(handler)
    _logger, _handler_path = lg, path
    return lg


def _now() -> str:
    try:
        return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
    except Exception:
        return "?"


def log_tool(tool: str, ok: bool, *, digest: str | None = None, **fields) -> None:
    """Append one JSONL line for a tool call. Best-effort — never raises.

    `digest` is the 8-char content hash (or None when no file exists yet, e.g.
    a fetch_media SSRF refusal). `fields` are the tool-specific payload; pass the
    same outcome the tool returns minus heavyweight blobs (fileUri, frames list)."""
    try:
        # sha8 to correlate a log line with its workspace/<sha8>/ folder
        record = {"ts": _now(), "tool": tool, "ok": ok, "digest": digest[:8] if digest else None}
        record.update(fields)
        _get_logger().info(json.dumps(record, ensure_ascii=False))
    except Exception:
        pass        # logging never breaks a tool or the MCP dispatch
