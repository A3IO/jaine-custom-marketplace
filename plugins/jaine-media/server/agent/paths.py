"""Single source of truth for where jaine-media writes runtime data.

Resolution order: explicit dev/test override → installed-plugin data dir → a
local ``.aitemp`` fallback (gitignored). Both the fileUri cache and the per-media
workspace derive from here, so there is exactly one place that decides the root.
"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("JAINE_MEDIA_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if env:
        return Path(os.path.expanduser(env))
    return Path(__file__).resolve().parents[2] / ".aitemp" / "jaine-media"


def workspace_dir(digest: str) -> Path:
    """The working folder for one media file, keyed by an 8-char content-hash
    prefix (readable, collision-safe enough). Created on demand."""
    d = data_dir() / "workspace" / digest[:8]
    d.mkdir(parents=True, exist_ok=True)
    return d
