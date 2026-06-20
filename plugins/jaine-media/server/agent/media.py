"""ffmpeg/ffprobe helpers for the media-processing tools (extract_frame first;
prepare_media / fetch_media later). Pure timecode math lives here too so it can be
tested without spawning ffmpeg."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path


def has_tool(name: str) -> bool:
    """True if `name` (ffmpeg / ffprobe / yt-dlp) is on PATH."""
    return shutil.which(name) is not None


def probe_duration(path: Path | str) -> float:
    """Media duration in seconds via ffprobe. Raises on failure (caller guards)."""
    out = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    raw = out.stdout.strip()
    if out.returncode != 0 or not raw or raw == "N/A":
        # corrupt/unsupported file (non-zero, empty) or a duration-less container
        # (live segment / fragmented MP4 → 'N/A') — raise a CLEAR error, not float()'s
        # opaque ValueError; surface ffprobe's own stderr when present (review #10).
        detail = (out.stderr or "").strip()[:200] or f"got {raw!r}"
        raise RuntimeError(f"ffprobe could not read duration: {detail}")
    return float(raw)


def extract_png(input_path: Path | str, timecode: float, output_path: Path | str) -> bool:
    """Extract a single still frame at `timecode` (seconds) to `output_path` (PNG).

    Input-seek (`-ss` before `-i`) is fast and frame-accurate enough for our ±window
    bracketing. Returns True on success."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-ss", str(timecode),
         "-i", str(input_path), "-frames:v", "1", "-q:v", "2", str(output_path)],
        capture_output=True, timeout=60,
    )
    return r.returncode == 0 and output_path.exists()


def probe_dimensions(path: Path | str) -> tuple[int, int]:
    """(width, height) of the first video stream via ffprobe."""
    out = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    w, h = out.stdout.strip().split("x")
    return int(w), int(h)


# Gemini Files API size cap — DRIFT-PRONE, like the model catalog. NEVER bury a
# magic number in fit logic: it lives in env (override via JAINE_MEDIA_MAX_FILE_MB),
# and analyze_media's structured API error is the reactive backstop if it's stale.
def _max_file_mb() -> float:
    return float(os.environ.get("JAINE_MEDIA_MAX_FILE_MB", "2000"))   # Files API ~2GB


def fits(path: Path | str) -> dict:
    """Does this file fit Gemini's Files API size limit? Returns a verdict + reason.
    Reusable by fetch_media (Phase 4) for its fetch → fit-check → auto-prepare flow."""
    size_mb = Path(path).stat().st_size / 1_000_000
    limit = _max_file_mb()
    ok = size_mb <= limit
    return {
        "fits": ok,
        "size_mb": round(size_mb, 2),
        "limit_mb": limit,
        "reason": None if ok else
        f"{size_mb:.0f}MB exceeds the {limit:.0f}MB Files API limit — compress to fit",
    }


def trim(input_path: Path | str, output_path: Path | str, *, start: float, end: float) -> bool:
    """Cut [start, end] seconds (re-encoded, frame-accurate). The range is EXPLICIT —
    prepare_media never silently drops content; the caller reports what was kept."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(input_path),
         "-ss", str(start), "-to", str(end),
         "-c:v", "libx264", "-c:a", "aac", str(output_path)],
        capture_output=True, timeout=600,
    )
    return r.returncode == 0 and output_path.exists()


def compress(input_path: Path | str, output_path: Path | str, *,
             height: int = 480, video_bitrate: str = "800k") -> bool:
    """Downscale resolution (+ cap video bitrate) to shrink file size for the size
    limit — content is preserved, only fidelity drops. Width keeps aspect (scale=-2)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(input_path),
         "-vf", f"scale=-2:{height}", "-b:v", video_bitrate,
         "-c:v", "libx264", "-c:a", "aac", str(output_path)],
        capture_output=True, timeout=600,
    )
    return r.returncode == 0 and output_path.exists()


# Input bounds for the media tools — argv is injection-safe, but unbounded numeric
# params are a resource-DoS (a tiny `step` = thousands of ffmpeg calls). Tools reject
# out-of-bounds requests with a structured error BEFORE spawning any subprocess.
_MAX_FRAMES = 200
_MIN_PREP_HEIGHT, _MAX_PREP_HEIGHT = 16, 4320


def validate_frame_request(timecode: float, *, window: float, step: float) -> str | None:
    """Error string if an extract_frame request is out of bounds, else None."""
    # Reject NaN/inf FIRST: every comparison with NaN is False (so it slips past the bounds
    # below) and int((2*window)/step) then raises ValueError(NaN)/OverflowError(inf) — which,
    # called outside extract_frame's try, would escape to FastMCP (never-crash break).
    for _name, _v in (("timecode", timecode), ("window", window), ("step", step)):
        if not math.isfinite(_v):
            return f"{_name} must be a finite number"
    if timecode < 0:
        return "timecode must be >= 0"
    if window < 0:
        return "window must be >= 0"
    if step <= 0:
        return "step must be > 0"
    est = int((2 * window) / step) + 2
    if est > _MAX_FRAMES:
        return (f"window/step would extract ~{est} frames (max {_MAX_FRAMES}) — "
                "increase step or reduce window")
    return None


def validate_prepare_request(height: int | None, start: float | None,
                             end: float | None) -> str | None:
    """Error string if a prepare_media request is out of bounds, else None."""
    for _name, _v in (("start", start), ("end", end)):   # NaN/inf slip past the < / >= checks
        if _v is not None and not math.isfinite(_v):       # below and reach ffmpeg as `-ss nan`
            return f"{_name} must be a finite number"
    if height is not None and not (_MIN_PREP_HEIGHT <= height <= _MAX_PREP_HEIGHT):
        return f"height must be in [{_MIN_PREP_HEIGHT}, {_MAX_PREP_HEIGHT}]"
    if start is not None and start < 0:
        return "start must be >= 0"
    if end is not None and end < 0:
        return "end must be >= 0"
    if start is not None and end is not None and start >= end:
        return "start must be < end"
    return None


def frame_timecodes(timecode: float, *, window: float, step: float, duration: float) -> list[float]:
    """The list of timecodes (seconds) to grab around `timecode`.

    Gemini timecode localization is only ~±0.5-1s accurate, so extract_frame brackets
    the moment with a ±`window` of frames spaced by `step`. The window is clamped to
    [0, duration] and the (clamped) center timecode is always included."""
    start = max(0.0, timecode - window)
    end = min(duration, timecode + window)
    times = []
    n = int((end - start) / step) if step > 0 else 0   # floor: start + n*step never exceeds end
    for i in range(n + 1):
        t = round(start + i * step, 3)
        if t <= end:                                   # guard float drift past the window end (#5)
            times.append(t)
    center = round(min(max(timecode, 0.0), duration), 3)
    if center not in times:
        times.append(center)
        times.sort()
    return times
