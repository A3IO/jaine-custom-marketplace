"""Disk-backed fileUri cache — survives a process restart (AGENTS.md grab #1).

The MCP stdio server dies with the session; an in-memory cache would lose every
uploaded fileUri. The cache must persist to disk so a fresh process re-hydrates
it (and multi-turn re-query survives within the ~48h Files API window).
"""
import importlib
import json

from agent import gemini_files as gf


def _fresh_process(monkeypatch, data_dir):
    """Simulate a brand-new server process: point env at a data dir and reload
    the module so the in-memory cache starts empty and must load from disk."""
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(data_dir))
    return importlib.reload(gf)


def test_cache_survives_process_restart(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"

    # session 1: cache an entry, then the process "dies"
    g1 = _fresh_process(monkeypatch, data_dir)
    ref = g1.FileRef(uri="files/abc-uri", name="files/abc",
                     mime_type="video/mp4", expires_at=0.0, state="ACTIVE")
    g1._cache_put("sha-1", ref)

    # session 2: brand-new process — in-memory cache empty, must load from disk
    g2 = _fresh_process(monkeypatch, data_dir)
    got = g2._cache_get("sha-1")

    assert got is not None, "cache entry lost across process restart (not persisted to disk)"
    assert got.uri == "files/abc-uri"
    assert got.name == "files/abc"
    assert got.mime_type == "video/mp4"


def test_reserved_sessions_key_preserved(tmp_path, monkeypatch):
    """A future stateful feature's session data must survive a fileUri-cache write
    (the disk schema reserves "sessions" so stateful lands with no migration)."""
    data_dir = tmp_path / "data"
    cache_path = data_dir / "cache.json"

    g1 = _fresh_process(monkeypatch, data_dir)
    g1._cache_put("sha-A", g1.FileRef(uri="files/a", name="files/a",
                                      mime_type="video/mp4", expires_at=0.0))
    # simulate a future stateful feature dropping a session into the same file
    raw = json.loads(cache_path.read_text())
    raw["sessions"]["sess-1"] = [{"q": "hi", "a": "yo"}]
    cache_path.write_text(json.dumps(raw))

    # a fresh process making a new cache write must NOT clobber the reserved data
    g2 = _fresh_process(monkeypatch, data_dir)
    g2._cache_put("sha-B", g2.FileRef(uri="files/b", name="files/b",
                                      mime_type="video/mp4", expires_at=0.0))

    final = json.loads(cache_path.read_text())
    assert final["sessions"] == {"sess-1": [{"q": "hi", "a": "yo"}]}
    assert set(final["files"]) == {"sha-A", "sha-B"}


def test_evicted_entry_stays_gone_after_restart(tmp_path, monkeypatch):
    """Evicting (e.g. on a 403/expiry) must persist — a removed entry must not
    resurrect from disk on the next process, or we'd reuse a dead fileUri."""
    data_dir = tmp_path / "data"

    g1 = _fresh_process(monkeypatch, data_dir)
    g1._cache_put("sha-X", g1.FileRef(uri="files/x", name="files/x",
                                      mime_type="video/mp4", expires_at=0.0))
    g1._cache_evict("sha-X")

    g2 = _fresh_process(monkeypatch, data_dir)
    assert g2._cache_get("sha-X") is None, "evicted entry resurrected from disk"
