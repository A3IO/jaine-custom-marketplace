"""extract_frame MCP tool — brackets a Gemini-located timecode with a ±window of
real ffmpeg frames in the media's workspace, returning paths Claude can Read."""
import json
import shutil
from pathlib import Path

import pytest

import server

_VIDEO = str(Path(__file__).resolve().parents[2] / "reference" / "timecode_test.mp4")
_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_extract_frame_creates_window_of_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    d = json.loads(await server.extract_frame(_VIDEO, 3.5, window=1.0, step=0.5))

    assert d["success"] is True
    assert len(d["frames"]) == 5            # 2.5, 3.0, 3.5, 4.0, 4.5
    assert all(Path(f["path"]).exists() and Path(f["path"]).stat().st_size > 0
               for f in d["frames"])
    assert "frames" in d["frames_dir"]      # lives under workspace/<sha8>/frames/


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_extract_frame_zero_window_is_single_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    d = json.loads(await server.extract_frame(_VIDEO, 8.5, window=0.0))
    assert d["success"] is True
    assert len(d["frames"]) == 1


async def test_extract_frame_missing_file_returns_structured_error():
    d = json.loads(await server.extract_frame("/no/such/file.mp4", 3.5))
    assert d["success"] is False
    assert "not found" in d["error"]


async def test_extract_frame_rejects_dos_step_before_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    d = json.loads(await server.extract_frame(_VIDEO, 3.5, window=5.0, step=0.001))
    assert d["success"] is False
    assert "frame" in d["error"].lower() or "step" in d["error"].lower()


async def test_extract_frame_never_crashes_on_hash_oserror(tmp_path, monkeypatch):
    # backfill (#1/#11): an OSError during hashing/workspace (read-only or full data-fs)
    # must be caught — {success:false}, never propagated to FastMCP.
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x")

    async def _boom(_p):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(server.gemini_files, "compute_file_hash", _boom)
    d = json.loads(await server.extract_frame(str(f), 3.5))
    assert d["success"] is False
