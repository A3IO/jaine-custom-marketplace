"""fetch_media internals — SSRF-guarded URL validation + a yt-dlp download wrapper.

yt-dlp will fetch literally anything you hand it: ``file://`` paths, ``http://localhost``,
the cloud-metadata endpoint ``http://169.254.169.254``. So every URL's INITIAL host is
validated before it reaches yt-dlp — http(s) only, host must not resolve to a private /
loopback / link-local / non-global address.

SCOPE (this is a PERSONAL-USE tool fed TRUSTED URLs): the guard covers only the URL you
pass. It does NOT cover HTTP redirects or DNS-rebinding — yt-dlp does its own resolution
and follows 30x redirects internally, so a public URL that 302-redirects to an internal
address would NOT be blocked. A real fix needs network-level isolation (an SSRF-filtering
proxy / namespace), deliberately out of scope. Do not feed this tool untrusted URLs.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def is_blocked_ip(ip: str) -> bool:
    """True if `ip` is unsafe to fetch — anything NOT publicly routable.

    Uses `is_global` (the canonical test) rather than a flag blocklist, so it also
    catches CGNAT (100.64.0.0/10) and other non-global ranges a flag list misses.
    IPv4-mapped IPv6 (::ffff:a.b.c.d) is unwrapped first so the embedded IPv4 is what
    gets classified. Fails closed: an unparseable address is treated as blocked."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return not addr.is_global or addr.is_unspecified


def validate_url(url: str) -> str | None:
    """Return an error reason if the URL is unsafe to fetch (SSRF guard), else None.
    Scope: the URL's INITIAL host only — redirects/rebind are not covered (see module doc)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) URLs are allowed (got scheme {parsed.scheme!r})"
    host = parsed.hostname
    if not host:
        return "URL has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return f"cannot resolve host {host!r}"
    for info in infos:
        ip = str(info[4][0])
        if is_blocked_ip(ip):
            return f"host {host!r} resolves to a non-public address ({ip}) — refusing to fetch"
    return None


_MIN_HEIGHT, _MAX_HEIGHT = 144, 2160


def _safe_height(max_height: int) -> int:
    """Clamp to [144, 2160]. A caller-supplied huge value (or 0/negative) must not be
    able to disable the download-time quality cap (resource exhaustion)."""
    try:
        h = int(max_height)
    except (TypeError, ValueError):
        return 720
    return max(_MIN_HEIGHT, min(_MAX_HEIGHT, h))


def _max_download_mb() -> int:
    """Disk-safety ceiling for a fetch, ABOVE the ~2GB Gemini fit limit
    (JAINE_MEDIA_MAX_FILE_MB) on purpose — so the download → fits() → compress backstop
    still works on an oversized video. It only aborts a RUNAWAY pull (a direct file URL the
    720p cap can't bound — no format ladder). Env knob, not a hardcode (AGENTS.md: limits
    live in env). Same style as media._max_file_mb."""
    return int(os.environ.get("JAINE_MEDIA_MAX_DOWNLOAD_MB", "8000"))   # 8GB headroom


def download(url: str, dest_dir: Path | str, *, max_height: int = 720,
             timeout: int = 600) -> Path | None:
    """yt-dlp `url` into `dest_dir`, capping resolution at `max_height` AND total size at
    `_max_download_mb()` AT DOWNLOAD time (don't pull 4K — or a runaway direct file — to
    compress later). Returns the resulting file path, or None on failure."""
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        return None
    dest = Path(dest_dir)
    h = _safe_height(max_height)
    # HARD cap (no `?` soft-preference); if nothing fits, fall back to the WORST/smallest
    # stream (`/w`), never the best/largest — so a >h-only video doesn't pull the 4K (review #9).
    fmt = f"bv*[height<={h}]+ba/b[height<={h}]/w"
    r = subprocess.run(
        # --max-filesize aborts a known-oversized download (direct URL w/ Content-Length)
        # BEFORE it fills the disk; the resolution cap can't bound a format-ladder-less URL.
        [ytdlp, "-f", fmt, "--max-filesize", f"{_max_download_mb()}M",
         "--merge-output-format", "mp4", "--no-playlist",
         "-o", str(dest / "dl.%(ext)s"), url],
        capture_output=True, timeout=timeout,
    )
    if r.returncode != 0:
        return None
    files = sorted(dest.glob("dl.*"))
    return files[0] if files else None
