"""A self-healing skip-list of model ids that returned a retired-model 404.

`models.list` keeps advertising retired ids (a retired and a working model are byte-identical
in the catalog — there is NO retired-signal), so `list_models` would keep showing a model that
404s on use. `analyze_media` already detects that 404; we persist the dead id here so
`list_models` can hide it on the next call. Self-healing: the list grows as models retire, with
no hardcoded names to drift.

Best-effort + fail-open: a read/write error never blocks a tool (worst case `list_models` still
carries the catalog caveat). Separate from gemini_files' cache.json (that owns fileUri lifecycle)
so the two responsibilities don't entangle.
"""
from __future__ import annotations

import json
import os
import threading

from .paths import data_dir

_LOCK = threading.Lock()


def _path():
    return data_dir() / "dead-models.json"


def load() -> set[str]:
    """The recorded dead model ids. Fail-open: a missing/corrupt file → empty set. Keeps only
    str items so a syntactically-valid-but-malformed file (e.g. ``[{}]`` — an unhashable item
    that would crash ``set(raw)``) degrades to a clean subset instead of raising (codex P2)."""
    try:
        raw = json.loads(_path().read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {x for x in raw if isinstance(x, str)}


def record(model_id: str) -> None:
    """Add `model_id` to the skip-list (idempotent). Best-effort: a write error is swallowed
    so logging a dead model never crashes the analyze_media error path that calls this."""
    if not model_id:
        return
    with _LOCK:
        dead = load()
        if model_id in dead:
            return
        dead.add(model_id)
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(dead), ensure_ascii=False, indent=2))
            os.replace(tmp, path)            # atomic on POSIX
        except OSError:
            pass                             # fail-open — the caveat still warns
