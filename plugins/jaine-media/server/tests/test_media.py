"""ffmpeg helpers for extract_frame (and later prepare/fetch). The pure timecode-
window math is tested here; the ffmpeg/ffprobe wrappers run the real binaries
(present on the machine) against the synthetic test video."""
import shutil
from pathlib import Path

import pytest

from agent import media

_REF = Path(__file__).resolve().parents[2] / "reference"
_VIDEO = _REF / "timecode_test.mp4"   # silent color-flash clip, exactly 24.0s
_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# --- frame_timecodes (pure window math) ---

def test_symmetric_window_brackets_the_moment():
    # timecode localization is only ~±0.5-1s, so we grab a ±window of frames
    assert media.frame_timecodes(5.0, window=1.0, step=0.5, duration=20.0) == [4.0, 4.5, 5.0, 5.5, 6.0]


def test_center_timecode_always_included():
    assert 7.3 in media.frame_timecodes(7.3, window=1.0, step=0.5, duration=20.0)


def test_window_clamps_at_start_never_negative():
    out = media.frame_timecodes(0.3, window=1.0, step=0.5, duration=20.0)
    assert min(out) == 0.0
    assert 0.3 in out


def test_frame_timecodes_never_exceeds_window_end():
    # review #5: round() in n=round((end-start)/step) could push start+n*step PAST end,
    # emitting a timecode ffmpeg can't seek (silently dropped). All times must be in-range.
    out = media.frame_timecodes(19.7, window=0.5, step=0.5, duration=20.0)
    assert max(out) <= 20.0        # never beyond the clip/window end
    assert min(out) >= 0.0


def test_frame_timecodes_includes_window_end_with_nonround_step():
    # #214.2: int((end-start)/step) floors; tc=1.0,window=0.15,step=0.1 → 0.3/0.1=2.9999→2
    # silently drops the window-end frame (t=1.15==end) — the symmetric opposite of the #5
    # overshoot. An epsilon before the floor recovers it (the t<=end clamp keeps it in range).
    out = media.frame_timecodes(1.0, window=0.15, step=0.1, duration=20.0)
    assert 1.15 in out             # the window-end frame must be present


def test_probe_duration_na_raises_clear_error(monkeypatch):
    # review #10: a duration-less container (live segment → 'N/A') or a failed ffprobe
    # (empty stdout) must raise a CLEAR error, not an opaque float() ValueError.
    fake = type("R", (), {"returncode": 0, "stdout": "N/A\n", "stderr": ""})()
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match="duration"):
        media.probe_duration("x.mp4")


def test_window_clamps_at_end_never_past_duration():
    out = media.frame_timecodes(19.8, window=1.0, step=0.5, duration=20.0)
    assert max(out) <= 20.0


def test_zero_window_is_a_single_frame():
    assert media.frame_timecodes(7.0, window=0.0, step=0.5, duration=20.0) == [7.0]


# --- ffprobe / ffmpeg wrappers (real binaries against the test video) ---

@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_probe_duration_of_test_video():
    assert abs(media.probe_duration(_VIDEO) - 24.0) < 0.1


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_extract_png_creates_a_valid_png(tmp_path):
    out = tmp_path / "frame.png"
    assert media.extract_png(_VIDEO, 3.5, out) is True
    assert out.stat().st_size > 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"   # PNG magic bytes


# --- validate_frame_request (bound input BEFORE any ffmpeg — resource-DoS guard) ---

def test_validate_frame_rejects_tiny_step_dos():
    # step 0.001 over a 5s window would be ~10000 frames / ffmpeg calls
    assert media.validate_frame_request(5.0, window=5.0, step=0.001) is not None


def test_validate_frame_rejects_negative_timecode():
    assert media.validate_frame_request(-1.0, window=1.0, step=0.5) is not None


def test_validate_frame_rejects_nonpositive_step():
    assert media.validate_frame_request(5.0, window=1.0, step=0.0) is not None


def test_validate_frame_accepts_normal_request():
    assert media.validate_frame_request(5.0, window=1.0, step=0.5) is None
