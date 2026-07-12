"""Canonical log-line writer for every bulldozer stable log (issue #322, PR1).

Grammar (spec: docs/superpowers/specs/2026-07-11-bulldozer-log-grammar-design.md):

    {ts} | event={event} | session={sid} | k1=v1 | k2=v2 ...

One writer, one timestamp form, sanitized tokens/values, 5MB rotation, best-effort
with a single stderr warning per process. stdlib-only, py3.9+.

CLI shim (for .sh writers): python3 lib/bulldozer_log.py <log_path> <event> [k=v ...]
Always exits 0 — logging never fails the calling hook/wrapper.
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import fcntl  # POSIX-only; on other platforms rotation falls back to unserialized
except ImportError:  # pragma: no cover
    fcntl = None

_TOKEN_BAD = re.compile(r"[^A-Za-z0-9_-]")
_MAX_LOG_BYTES = 5 * 1024 * 1024
_VALUE_MAX = 500
_RESERVED = ("event", "session")
_WARNED = False  # once-per-process write-failure warning


def _token(v, max_len=64):
    """str()-coerce then normalize to ^[A-Za-z0-9_-]{1,max_len}$; empty → 'invalid'."""
    return _TOKEN_BAD.sub("_", str(v))[:max_len] or "invalid"


def _key_token(v):
    """Field-KEY normalization: _token plus the reserved-name rewrite. The rewrite
    applies to kv keys ONLY — an event VALUE legitimately named 'event'/'session'
    keeps its identity (codex review #323 P2). Reserved keys are unreachable via
    the Python API (**kv can't bind 'event'/'session') — this is the CLI-shim rule."""
    s = _token(v)
    return s + "_" if s in _RESERVED else s


def _value(v):
    s = str(v).replace("\n", " ").replace("\r", " ").replace("|", "/")
    if len(s) > _VALUE_MAX:
        s = s[: _VALUE_MAX - 1] + "…"
    return s


def _session_token(session):
    if session is None:
        session = os.environ.get("CLAUDE_CODE_SESSION_ID") or "NA"
    s = _TOKEN_BAD.sub("_", str(session))[:8]
    return s or "NA"


def _warn_once(log_path, exc):
    global _WARNED
    if not _WARNED:
        _WARNED = True
        try:  # the warning is best-effort too — a closed stderr (detached process)
            print(  # must not break the never-raises contract (codex review #323)
                "warning: could not write {}: {}".format(
                    os.path.basename(str(log_path)), exc
                ),
                file=sys.stderr,
            )
        except Exception:
            pass


def _append(log_path, event, session, pairs):
    """Core writer; pairs = iterable of raw (key, value). Never raises."""
    try:
        log_path = Path(log_path)
        fields = {}  # insertion-ordered; post-normalization collision → last wins
        for k, v in pairs:
            fields[_key_token(k)] = _value(v)
        parts = [
            datetime.now().astimezone().isoformat(timespec="seconds"),
            "event=" + _token(event),
            "session=" + _session_token(session),
        ]
        parts += ["{}={}".format(k, v) for k, v in fields.items()]
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Rotation + append are one flock-serialized critical section: without it a
        # second concurrent writer can os.replace() a freshly-rotated tiny log over
        # .1 and discard the entire rotated history (codex review #323 P2).
        with open(str(log_path) + ".lock", "w") as lk:
            if fcntl is not None:
                fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                if log_path.stat().st_size > _MAX_LOG_BYTES:
                    os.replace(str(log_path), str(log_path) + ".1")
            except FileNotFoundError:
                pass
            with open(log_path, "a", encoding="utf-8") as f:  # explicit: the … suffix
                f.write(" | ".join(parts) + "\n")             # must survive any locale
        return True
    except Exception as e:  # best-effort: observability must never break the tool
        _warn_once(log_path, e)
        return False


def append_line(log_path, event, session=None, **kv):
    """Append one canonical line. Returns True on success, False on failure."""
    return _append(log_path, event, session, kv.items())


def _main(argv):
    if len(argv) < 2:
        print("usage: bulldozer_log.py <log_path> <event> [k=v ...]", file=sys.stderr)
        return 0  # fail-open even on misuse
    log_path, event = argv[0], argv[1]
    pairs = []
    for arg in argv[2:]:
        k, _, v = arg.partition("=")  # no '=' → empty value; empty key → 'invalid'
        pairs.append((k, v))
    _append(log_path, event, None, pairs)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
