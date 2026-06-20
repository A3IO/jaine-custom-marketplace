"""prepare_media building blocks: a config-driven fit-check (NOT hardcoded Gemini
limits) reusable by Phase 4, plus ffmpeg trim (explicit range) and compress
(resolution downscale) for the doesn't-fit case."""
import json
import shutil
from pathlib import Path

import pytest

import server
from agent import media

_VIDEO = Path(__file__).resolve().parents[2] / "reference" / "timecode_test.mp4"
_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# --- fits (limit from config/env, never a magic number baked into logic) ---

def test_fits_true_for_small_file(tmp_path, monkeypatch):
    monkeypatch.delenv("JAINE_MEDIA_MAX_FILE_MB", raising=False)
    f = tmp_path / "x.bin"
    f.write_bytes(b"0" * 1000)
    assert media.fits(f)["fits"] is True


def test_fits_false_when_over_configured_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_MAX_FILE_MB", "0.0005")   # 500 bytes
    f = tmp_path / "x.bin"
    f.write_bytes(b"0" * 1000)
    r = media.fits(f)
    assert r["fits"] is False
    assert r["reason"]   # must explain why it doesn't fit


def test_fits_limit_comes_from_env_not_hardcoded(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_MAX_FILE_MB", "123")
    f = tmp_path / "x.bin"
    f.write_bytes(b"0" * 10)
    assert media.fits(f)["limit_mb"] == 123


# --- trim / compress (real ffmpeg) ---

@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_trim_cuts_to_the_requested_range(tmp_path):
    out = tmp_path / "trimmed.mp4"
    assert media.trim(_VIDEO, out, start=5.0, end=10.0) is True
    assert abs(media.probe_duration(out) - 5.0) < 0.5   # ~5s window


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_compress_downscales_resolution(tmp_path):
    out = tmp_path / "small.mp4"
    assert media.compress(_VIDEO, out, height=120) is True
    assert media.probe_dimensions(out)[1] == 120          # 320x240 -> 160x120


# --- validate_prepare_request (bound input BEFORE any ffmpeg) ---

def test_validate_prepare_rejects_start_after_end():
    assert media.validate_prepare_request(None, start=10.0, end=5.0) is not None


def test_validate_prepare_rejects_negative_start():
    assert media.validate_prepare_request(None, start=-1.0, end=5.0) is not None


def test_validate_prepare_rejects_absurd_height():
    assert media.validate_prepare_request(99999, None, None) is not None


def test_validate_prepare_accepts_normal_request():
    assert media.validate_prepare_request(480, start=0.0, end=10.0) is None


# --- prepare_media tool ---

@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_prepare_media_trim_reports_what_was_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    d = json.loads(await server.prepare_media(str(_VIDEO), start=5.0, end=10.0))
    assert d["success"] is True and d["mode"] == "trim"
    assert abs(media.probe_duration(d["output"]) - 5.0) < 0.5
    assert d["dropped"] != "nothing"   # trim is never silent


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_prepare_media_compresses_when_over_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JAINE_MEDIA_MAX_FILE_MB", "0.001")   # force doesn't-fit
    d = json.loads(await server.prepare_media(str(_VIDEO)))
    assert d["success"] is True and d["mode"] == "compress"
    assert Path(d["output"]).exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
async def test_prepare_media_noop_when_already_fits(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JAINE_MEDIA_MAX_FILE_MB", raising=False)   # 2GB default
    d = json.loads(await server.prepare_media(str(_VIDEO)))
    assert d["mode"] == "none"   # an 8 KB clip already fits


async def test_prepare_media_rejects_start_after_end_before_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    d = json.loads(await server.prepare_media(str(_VIDEO), start=10.0, end=5.0))
    assert d["success"] is False
    assert "start" in d["error"].lower()
