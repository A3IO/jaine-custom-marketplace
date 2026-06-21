"""fetch_media — URL → local file via yt-dlp. The SSRF guard is the critical piece:
yt-dlp will fetch ANYTHING (file://, localhost, cloud-metadata), so URLs are validated
before they ever reach it. yt-dlp itself is mocked in unit tests (no network)."""
import json
import shutil
from pathlib import Path

import server
from agent import fetch

_VIDEO = Path(__file__).resolve().parents[2] / "reference" / "timecode_test.mp4"


# --- is_blocked_ip (pure ipaddress classification) ---

def test_loopback_is_blocked():
    assert fetch.is_blocked_ip("127.0.0.1") is True


def test_cloud_metadata_ip_is_blocked():
    assert fetch.is_blocked_ip("169.254.169.254") is True   # link-local — AWS/GCP metadata


def test_private_10_is_blocked():
    assert fetch.is_blocked_ip("10.0.0.5") is True


def test_private_192_168_is_blocked():
    assert fetch.is_blocked_ip("192.168.1.1") is True


def test_public_ip_is_allowed():
    assert fetch.is_blocked_ip("8.8.8.8") is False


def test_unparseable_is_blocked():
    assert fetch.is_blocked_ip("not-an-ip") is True   # fail closed


def test_cgnat_100_64_is_blocked():
    assert fetch.is_blocked_ip("100.64.0.1") is True   # CGNAT shared space — non-global


def test_ipv6_ula_is_blocked():
    assert fetch.is_blocked_ip("fc00::1") is True


def test_ipv6_link_local_is_blocked():
    assert fetch.is_blocked_ip("fe80::1") is True


def test_ipv4_mapped_loopback_is_blocked():
    assert fetch.is_blocked_ip("::ffff:127.0.0.1") is True   # mapped IPv4 must unwrap


def test_unspecified_is_blocked():
    assert fetch.is_blocked_ip("0.0.0.0") is True


def test_ipv6_loopback_is_blocked():
    assert fetch.is_blocked_ip("::1") is True


# --- validate_url (scheme + DNS-resolved address checks; numeric hosts need no network) ---

def test_rejects_file_scheme():
    assert fetch.validate_url("file:///etc/passwd") is not None


def test_rejects_non_http_scheme():
    assert fetch.validate_url("ftp://example.com/x") is not None


def test_rejects_loopback_literal():
    assert fetch.validate_url("http://127.0.0.1/video.mp4") is not None


def test_rejects_cloud_metadata():
    assert fetch.validate_url("http://169.254.169.254/latest/meta-data/") is not None


def test_rejects_localhost():
    assert fetch.validate_url("http://localhost:8080/x") is not None


def test_allows_public_literal_ip():
    assert fetch.validate_url("https://8.8.8.8/video.mp4") is None


# --- _safe_height (clamp so a huge value can't disable the download-time quality cap) ---

def test_safe_height_clamps_absurdly_large():
    assert fetch._safe_height(999999) == 2160


def test_safe_height_clamps_zero_and_negative():
    assert fetch._safe_height(0) == 144
    assert fetch._safe_height(-720) == 144


def test_safe_height_passes_reasonable_value():
    assert fetch._safe_height(720) == 720


def test_download_format_string_uses_clamped_height(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        (tmp_path / "dl.mp4").write_bytes(b"x")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(fetch.shutil, "which", lambda _n: "/usr/bin/yt-dlp")
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.download("https://8.8.8.8/v.mp4", tmp_path, max_height=999999)
    fmt = captured["cmd"][captured["cmd"].index("-f") + 1]
    assert "2160" in fmt and "999999" not in fmt


def test_download_format_hard_caps_no_soft_match(tmp_path, monkeypatch):
    # review #9: height<=? is a SOFT preference and a bare /b fallback is uncapped — a
    # >max-only video would pull the largest stream, defeating the cap. Hard-cap + worst.
    captured = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        (tmp_path / "dl.mp4").write_bytes(b"x")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(fetch.shutil, "which", lambda _n: "/usr/bin/yt-dlp")
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.download("https://8.8.8.8/v.mp4", tmp_path, max_height=720)
    fmt = captured["cmd"][captured["cmd"].index("-f") + 1]
    assert "height<=?" not in fmt        # no soft preference — a real cap
    assert not fmt.endswith("/b")        # fallback isn't the best/largest (uncapped)


def test_download_caps_filesize(tmp_path, monkeypatch):
    # security round-2 #3: a direct URL the resolution cap can't bound (no format ladder)
    # would pull an unbounded file to disk. yt-dlp must carry --max-filesize so a runaway
    # (e.g. a 50GB direct URL) aborts before filling the disk. Default ceiling is generous
    # (8GB) — ABOVE the ~2GB Gemini fit limit, so the fits→compress backstop still works.
    captured = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        (tmp_path / "dl.mp4").write_bytes(b"x")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(fetch.shutil, "which", lambda _n: "/usr/bin/yt-dlp")
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.download("https://8.8.8.8/v.mp4", tmp_path)
    cmd = captured["cmd"]
    assert "--max-filesize" in cmd
    assert cmd[cmd.index("--max-filesize") + 1] == "8000M"   # default 8GB ceiling


def test_download_filesize_cap_env_override(tmp_path, monkeypatch):
    # the ceiling is an env knob (same style as JAINE_MEDIA_MAX_FILE_MB), not a hardcode.
    captured = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        (tmp_path / "dl.mp4").write_bytes(b"x")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setenv("JAINE_MEDIA_MAX_DOWNLOAD_MB", "3000")
    monkeypatch.setattr(fetch.shutil, "which", lambda _n: "/usr/bin/yt-dlp")
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.download("https://8.8.8.8/v.mp4", tmp_path)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--max-filesize") + 1] == "3000M"


# --- download (yt-dlp mocked — no network) ---

def test_download_returns_none_without_ytdlp(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.shutil, "which", lambda _n: None)
    assert fetch.download("https://8.8.8.8/v.mp4", tmp_path) is None


def test_download_returns_the_downloaded_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.shutil, "which", lambda _n: "/usr/bin/yt-dlp")

    def fake_run(_cmd, **_kw):
        (tmp_path / "dl.mp4").write_bytes(b"fake-video-bytes")
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    out = fetch.download("https://8.8.8.8/v.mp4", tmp_path)
    assert out is not None and out.name == "dl.mp4"


# --- fetch_media tool (download mocked; SSRF guard real) ---

async def test_fetch_media_rejects_unsafe_url():
    d = json.loads(await server.fetch_media("file:///etc/passwd"))
    assert d["success"] is False
    assert "http" in d["error"].lower()


async def test_fetch_media_downloads_into_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.media, "has_tool", lambda _n: True)
    monkeypatch.setattr(server.fetch, "validate_url", lambda _u: None)

    def fake_download(_url, dest, **_kw):
        f = Path(dest) / "dl.mp4"
        shutil.copy(_VIDEO, f)
        return f

    monkeypatch.setattr(server.fetch, "download", fake_download)
    d = json.loads(await server.fetch_media("https://8.8.8.8/v.mp4"))
    assert d["success"] is True
    assert Path(d["file"]).exists()
    assert "workspace" in d["file"]   # landed on the unified content-hash path
    assert "fits" in d                # fit-check ran


async def test_fetch_media_preserves_prior_file_when_move_fails(tmp_path, monkeypatch):
    # #214.1: final.unlink()-then-shutil.move destroyed BOTH a prior valid file and the fresh
    # download when the cross-device move raised (ENOSPC). Staged-then-os.replace keeps the
    # prior file untouched, since `final` is never unlinked before the atomic rename.
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.media, "has_tool", lambda _n: True)
    monkeypatch.setattr(server.fetch, "validate_url", lambda _u: None)

    def fake_download(_url, dest, **_kw):
        f = Path(dest) / "dl.mp4"
        shutil.copy(_VIDEO, f)
        return f

    monkeypatch.setattr(server.fetch, "download", fake_download)
    d1 = json.loads(await server.fetch_media("https://8.8.8.8/v.mp4"))   # lands a valid file
    final = Path(d1["file"])
    prior = final.read_bytes()

    def boom_move(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(server.shutil, "move", boom_move)
    d2 = json.loads(await server.fetch_media("https://8.8.8.8/v.mp4"))
    assert d2["success"] is False
    assert final.read_bytes() == prior   # prior valid file NOT orphaned
