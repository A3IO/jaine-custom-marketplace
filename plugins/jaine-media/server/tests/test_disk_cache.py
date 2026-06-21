"""Disk-backed fileUri cache — survives a process restart (AGENTS.md grab #1).

The MCP stdio server dies with the session; an in-memory cache would lose every
uploaded fileUri. The cache must persist to disk so a fresh process re-hydrates
it (and multi-turn re-query survives within the ~48h Files API window).
"""
import asyncio
import importlib
import json

import pytest

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


async def test_processing_wait_shares_one_poll_across_concurrent_callers(tmp_path, monkeypatch):
    # #214.3: N concurrent same-loop callers on a cached entry the server still reports as
    # PROCESSING must share ONE wait_active poll (free-tier 429 risk), not each poll its own.
    # The PROCESSING wait now runs under the same _INFLIGHT single-flight as uploads.
    g = _fresh_process(monkeypatch, tmp_path / "data")
    g._cache_put("sha-proc", g.FileRef(uri="files/u", name="files/u", mime_type="video/mp4",
                                       expires_at=9_999_999_999.0, state="ACTIVE"))   # reusable

    waits = {"n": 0}

    async def fake_get_file(*_a, **_k):
        return {"state": "PROCESSING"}            # server still ingesting

    async def fake_wait_active(*_a, **_k):
        waits["n"] += 1
        await asyncio.sleep(0.02)                 # let concurrent callers pile up
        return {"state": "ACTIVE", "expirationTime": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(g, "get_file", fake_get_file)
    monkeypatch.setattr(g, "wait_active", fake_wait_active)

    async def call():
        return await g.get_or_upload("k", "https://x", "/tmp/x.mp4", "video/mp4", file_hash="sha-proc")

    results = await asyncio.gather(*[call() for _ in range(3)])
    assert all(r.state == "ACTIVE" for r in results)
    assert waits["n"] == 1                        # one shared poll, not three


async def test_processing_wait_failure_evicts_stale_entry(tmp_path, monkeypatch):
    # #223 review: when a cached entry the server reports PROCESSING fails wait_active
    # (timeout/FAILED), the stale entry MUST be evicted so the next call re-uploads — otherwise
    # it sticks for the 48h TTL, re-polling and re-failing (2+ min) on every call.
    g = _fresh_process(monkeypatch, tmp_path / "data")
    g._cache_put("sha-proc", g.FileRef(uri="files/u", name="files/u", mime_type="video/mp4",
                                       expires_at=9_999_999_999.0, state="ACTIVE"))   # reusable

    async def fake_get_file(*_a, **_k):
        return {"state": "PROCESSING"}            # server still ingesting

    async def fake_wait_active(*_a, **_k):
        raise g.GeminiFilesError("wait_active timed out")

    monkeypatch.setattr(g, "get_file", fake_get_file)
    monkeypatch.setattr(g, "wait_active", fake_wait_active)

    with pytest.raises(g.GeminiFilesError):
        await g.get_or_upload("k", "https://x", "/tmp/x.mp4", "video/mp4", file_hash="sha-proc")
    assert g._cache_get("sha-proc") is None       # evicted → next call does a fresh upload


async def test_cancelled_leader_hands_waiter_a_retryable_error(tmp_path, monkeypatch):
    # backfill (#8): if the upload leader is cancelled mid-flight, a concurrent same-loop waiter
    # must receive a retryable GeminiFilesError (→ structured {success:false}), NOT a propagated
    # CancelledError it never asked for (the finally block sets that on the still-pending Future).
    g = _fresh_process(monkeypatch, tmp_path / "data")
    in_upload = asyncio.Event()

    async def hanging_upload(*_a, **_k):
        in_upload.set()
        await asyncio.sleep(10)                    # leader hangs here until cancelled

    monkeypatch.setattr(g, "upload_video", hanging_upload)

    async def call():
        return await g.get_or_upload("k", "b", "/tmp/x.mp4", "video/mp4", file_hash="sha-cancel")

    leader = asyncio.create_task(call())
    await in_upload.wait()                          # leader owns the single-flight Future
    waiter = asyncio.create_task(call())
    await asyncio.sleep(0.01)                       # let the waiter attach to that Future
    leader.cancel()

    with pytest.raises(g.GeminiFilesError):         # retryable, not CancelledError
        await waiter
    with pytest.raises(asyncio.CancelledError):
        await leader


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
