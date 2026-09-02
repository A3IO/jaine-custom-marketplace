"""Every ffmpeg/ffprobe spawn must be cut off from our stdin (issue #369).

The plugin runs as an MCP **stdio** server, so stdin is the JSON-RPC pipe. A child
that inherits it reads protocol bytes as keyboard input; for ffmpeg a stray `q`
means "quit now". Measured before the fix: one such byte cut a 20-second clip to
8.2 seconds while ffmpeg still exited 0 — `trim`/`compress` returned a truncated
file that looked successful by every signal we check.

Nothing else in the suite would notice: the spawns are mocked everywhere, so the
defect and its fix are invisible to behaviour-level tests. This file guards the
two flags directly, and fails if a new spawn is added without them.
"""
from pathlib import Path

import pytest

from agent import media


class _Spawn:
    """Records every subprocess.run call instead of spawning anything."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})

        class _R:
            returncode = 0
            stdout = "1.0"      # parses as duration
            stderr = ""
        return _R()


def _run_all_spawners(tmp_path: Path) -> _Spawn:
    """Call every helper that spawns a binary, with a recording double in place."""
    spawn = _Spawn()
    src, dst = tmp_path / "in.mp4", tmp_path / "out.mp4"
    src.write_bytes(b"\0")

    original = media.subprocess.run
    media.subprocess.run = spawn
    try:
        media.probe_duration(src)
        media.extract_png(src, 1.0, tmp_path / "f.png")
        try:
            media.probe_dimensions(src)   # parses "WxH"; our double returns a duration
        except Exception:
            pass                          # the spawn is recorded before parsing
        media.trim(src, dst, start=0.0, end=1.0)
        media.compress(src, dst, height=480)
    finally:
        media.subprocess.run = original

    assert spawn.calls, "no spawn recorded — the double was not installed"
    return spawn


def test_every_spawn_detaches_stdin(tmp_path):
    """DEVNULL on both binaries: an inherited pipe is what feeds ffmpeg the bytes."""
    import subprocess as real

    for call in _run_all_spawners(tmp_path).calls:
        binary = Path(call["argv"][0]).name
        assert call["kwargs"].get("stdin") is real.DEVNULL, (
            f"{binary} spawned without stdin=DEVNULL: {call['argv'][:3]}"
        )


def test_every_ffmpeg_spawn_passes_nostdin(tmp_path):
    """`-nostdin` for ffmpeg specifically: DEVNULL alone leaves the flag's own
    interactive handling in place on some builds, and the flag is the documented
    way to say "this is not a terminal session"."""
    seen = 0
    for call in _run_all_spawners(tmp_path).calls:
        if Path(call["argv"][0]).name != "ffmpeg":
            continue
        seen += 1
        assert "-nostdin" in call["argv"], (
            f"ffmpeg spawned without -nostdin: {call['argv'][:4]}"
        )
    assert seen >= 3, f"expected ffmpeg spawns to be exercised, saw {seen}"
