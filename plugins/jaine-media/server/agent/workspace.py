"""The per-media working folder: a symlink back to the source and (for
extract_frame) extracted frames — under one dir keyed by the file's content hash,
so everything about one video sits together. Tool-call logging is centralized
elsewhere (agent/tool_log.py), not per-workspace."""
from __future__ import annotations

from pathlib import Path

from agent.paths import workspace_dir


def prepare(digest: str, source: Path) -> Path:
    """Ensure the workspace exists with a ``source<ext>`` symlink to the file."""
    ws = workspace_dir(digest)
    link = ws / f"source{source.suffix.lower()}"
    if not link.exists() and not link.is_symlink():
        try:
            link.symlink_to(source.resolve())
        except OSError:
            pass   # best-effort: a workspace without the symlink is still usable
    return ws
