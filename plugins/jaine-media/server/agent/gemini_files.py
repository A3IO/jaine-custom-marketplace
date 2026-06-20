"""Gemini Files API client for video understanding (GP-008).

Uploads a local video to the Gemini Files API and returns a ``fileUri`` usable
in a ``fileData`` content part — no ffmpeg, no inline base64. This works on
both sisters (Harley's container has no ffmpeg) and supports cheap multi-turn
re-querying of the same video (the ``fileUri`` is valid ~48h).

Everything here is async / non-blocking: the upload body is streamed (never the
whole file in memory) and the cache hash is computed off the event loop.

Endpoint construction note (bulldozer R4-F2/R5-F1): the Files API upload path is
``/upload/v1beta/files`` — ``/upload`` comes BEFORE ``/v1beta``. The configured
Gemini base already ends in ``/v1beta``, so a naive ``{base}/upload/v1beta/files``
would double the version. Endpoints are therefore derived from the *host*. The
returned ``File.name`` is already ``files/{id}``, so the get/delete URL is
``{host}/v1beta/{name}`` — never ``…/files/{name}`` (which would double ``files/``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from agent.gemini_native_adapter import (
    DEFAULT_GEMINI_BASE_URL,
    is_native_gemini_base_url,
)
from agent.paths import data_dir

# 8 MB streaming chunks for both upload body and hashing.
_UPLOAD_CHUNK = 8 * 1024 * 1024
_DEFAULT_ACTIVE_TIMEOUT = 120.0
_UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0)


class GeminiFilesError(RuntimeError):
    """Raised on any Files API failure (HTTP error, FAILED state, timeout)."""


@dataclass
class FileRef:
    uri: str            # canonical fileUri (the value for fileData.fileUri)
    name: str           # "files/{id}"
    mime_type: str
    expires_at: float   # epoch seconds; 0.0 when unknown
    state: str = "PROCESSING"
    cached: bool = False  # True iff served from cache (NOT freshly uploaded) — telemetry (#15)


def _files_endpoints(base_url: str) -> Tuple[str, str]:
    """Return ``(upload_url, files_base)`` derived from the base_url *host*.

    - ``upload_url`` = ``{scheme}://{host}/upload/v1beta/files``
    - ``files_base`` = ``{scheme}://{host}/v1beta`` (get/delete: ``f"{files_base}/{name}"``)

    Raises :class:`GeminiFilesError` for a non-native (openai-compat) base — the
    Files API only exists on the native Gemini REST endpoint (R4-F6).
    """
    base = (base_url or DEFAULT_GEMINI_BASE_URL).strip()
    if not is_native_gemini_base_url(base):
        raise GeminiFilesError(
            "Gemini Files API requires the native endpoint "
            f"(generativelanguage.googleapis.com, not openai-compat); got base_url={base!r}"
        )
    parsed = urlparse(base if "://" in base else f"https://{base}")
    root = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return f"{root}/upload/v1beta/files", f"{root}/v1beta"


def _auth_headers(api_key: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"x-goog-api-key": (api_key or "").strip()}
    if extra:
        headers.update(extra)
    return headers


def _parse_file_resource(payload: Any) -> Dict[str, Any]:
    """Unwrap the file resource.

    The resumable *finalize* (and the create) response wraps the resource in a
    top-level ``"file"`` key; ``GET files/{name}`` returns it at the top level.
    Tolerate both (R6-F1).
    """
    if isinstance(payload, dict) and isinstance(payload.get("file"), dict):
        return payload["file"]
    return payload if isinstance(payload, dict) else {}


def _expires_at_epoch(resource: Dict[str, Any]) -> float:
    exp = resource.get("expirationTime")
    if not exp:
        return 0.0
    try:
        return datetime.fromisoformat(str(exp).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _hash_file_sync(local_path: Path) -> str:
    h = hashlib.sha256()
    with open(local_path, "rb") as fp:
        for chunk in iter(lambda: fp.read(_UPLOAD_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


async def compute_file_hash(local_path: Any) -> str:
    """sha256 of the file bytes, computed off the event loop (R4-F3).

    A 292 MB read must never block the agent turn — delegate to a worker thread.
    """
    return await asyncio.to_thread(_hash_file_sync, Path(local_path))


async def upload_video(
    api_key: str,
    base_url: str,
    local_path: Any,
    mime: str,
    *,
    timeout: Any = None,
) -> FileRef:
    """Resumable-upload a local video; return a :class:`FileRef`.

    The returned ref's ``state`` may be ``PROCESSING`` — call :func:`wait_active`
    before using the ``fileUri``. The request body is streamed from disk, never
    read whole into memory (R4-F3/R5-F2).
    """
    path = Path(local_path)
    size = path.stat().st_size
    upload_url, _files_base = _files_endpoints(base_url)
    client_timeout = timeout or _UPLOAD_TIMEOUT

    async with httpx.AsyncClient(timeout=client_timeout) as client:
        # 1) start the resumable session
        start_headers = _auth_headers(api_key, {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
            "Content-Type": "application/json",
        })
        start = await client.post(
            upload_url, headers=start_headers,
            json={"file": {"display_name": path.name}},
        )
        if start.status_code // 100 != 2:
            raise GeminiFilesError(
                f"Files API start failed: HTTP {start.status_code} {getattr(start, 'text', '')[:200]}"
            )
        session_url = (start.headers.get("X-Goog-Upload-URL")
                       or start.headers.get("x-goog-upload-url"))
        if not session_url:
            raise GeminiFilesError("Files API start: response missing X-Goog-Upload-URL")

        # 2) upload + finalize — stream the body straight off disk
        upload_headers = _auth_headers(api_key, {
            "X-Goog-Upload-Command": "upload, finalize",
            "X-Goog-Upload-Offset": "0",
            "Content-Length": str(size),
        })

        async def _body_iter():
            # Known limitation (review #8): if the upload is cancelled while a
            # `to_thread(fp.read, …)` is in flight, generator cleanup closes `fp`
            # while the worker thread is still inside the read syscall — a benign
            # spurious error on CPython/Linux (the Future is already cancelled),
            # no data corruption. Accepted as a known limitation; cancellation
            # mid-upload is rare and the consequence is a stray log line.
            with open(path, "rb") as fp:
                while True:
                    chunk = await asyncio.to_thread(fp.read, _UPLOAD_CHUNK)
                    if not chunk:
                        break
                    yield chunk

        resp = await client.post(session_url, headers=upload_headers, content=_body_iter())
        if resp.status_code // 100 != 2:
            raise GeminiFilesError(
                f"Files API upload failed: HTTP {resp.status_code} {getattr(resp, 'text', '')[:200]}"
            )
        resource = _parse_file_resource(resp.json())

    name = str(resource.get("name") or "")
    uri = str(resource.get("uri") or "")
    if not name or not uri:
        raise GeminiFilesError(f"Files API upload: malformed resource {resource!r}")
    return FileRef(
        uri=uri,
        name=name,
        mime_type=mime,
        expires_at=_expires_at_epoch(resource),
        state=str(resource.get("state") or "PROCESSING"),
    )


async def get_file(api_key: str, base_url: str, name: str) -> Dict[str, Any]:
    """GET the file resource (``files/{id}``). Raises on non-2xx with the HTTP
    status in the message so callers can detect 403/404 → re-upload."""
    _upload, files_base = _files_endpoints(base_url)
    url = f"{files_base}/{name}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=_auth_headers(api_key))
        if resp.status_code // 100 != 2:
            raise GeminiFilesError(
                f"Files API get failed for {name}: HTTP {resp.status_code}",
            )
        return _parse_file_resource(resp.json())


async def wait_active(
    api_key: str,
    base_url: str,
    name: str,
    *,
    timeout: float = _DEFAULT_ACTIVE_TIMEOUT,
    poll_interval: float = 2.0,
) -> Dict[str, Any]:
    """Poll ``GET files/{name}`` until ``state == ACTIVE``.

    Raises :class:`GeminiFilesError` on a ``FAILED`` state, a non-2xx response,
    or when *timeout* elapses. Fully async (``asyncio.sleep`` between polls).
    """
    _upload, files_base = _files_endpoints(base_url)
    url = f"{files_base}/{name}"
    max_polls = max(1, int(timeout / poll_interval))
    last_state = "UNKNOWN"
    async with httpx.AsyncClient(timeout=30.0) as client:
        for _ in range(max_polls):
            resp = await client.get(url, headers=_auth_headers(api_key))
            if resp.status_code // 100 != 2:
                raise GeminiFilesError(
                    f"Files API get failed for {name}: HTTP {resp.status_code} "
                    f"{getattr(resp, 'text', '')[:200]}"
                )
            resource = _parse_file_resource(resp.json())
            last_state = str(resource.get("state") or "")
            if last_state == "ACTIVE":
                return resource
            if last_state == "FAILED":
                raise GeminiFilesError(f"Files API processing FAILED for {name}")
            await asyncio.sleep(poll_interval)
    raise GeminiFilesError(
        f"Files API timeout waiting for {name} to become ACTIVE (last state={last_state})"
    )


async def delete_file(api_key: str, base_url: str, name: str) -> None:
    """Best-effort DELETE of a Files API resource (optional cleanup)."""
    _upload, files_base = _files_endpoints(base_url)
    url = f"{files_base}/{name}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.delete(url, headers=_auth_headers(api_key))


# ---------------------------------------------------------------------------
# fileUri cache (multi-turn reuse) — GP-008 Part C
# ---------------------------------------------------------------------------
#
# goat runs async tools on MULTIPLE event loops (model_tools.py: a persistent
# _tool_loop + disposable worker loops for delegated subagents). An asyncio
# Future/Lock is bound to ONE loop, so the cache dict is guarded by a
# threading.Lock (thread-safe across loops) and the in-flight dedup map is keyed
# per running loop — each loop dedups its own concurrent uploads by awaiting a
# Future created on that same loop (R4-F4 / R5-F4).

_CACHE_LOCK = threading.Lock()
_URI_CACHE: Dict[str, FileRef] = {}                      # sha256 → FileRef (ACTIVE)
_SESSIONS: Dict[str, Any] = {}                           # reserved: session_id → turns (v1 unused, preserved verbatim)
_CACHE_LOADED = False                                    # disk hydrated this process?
_CACHE_SCHEMA_VERSION = 1
_INFLIGHT: Dict[Tuple[int, str], "asyncio.Future"] = {}  # (loop_id, sha256) → Future
_REUSE_MARGIN = 300.0                                    # 5 min before hard expiry


# ---------------------------------------------------------------------------
# Disk persistence (AGENTS.md grab #1)
# ---------------------------------------------------------------------------
# The MCP stdio server dies with the session, so an in-memory-only cache would
# lose every uploaded fileUri. The cache is mirrored to a JSON file: hydrated
# once per process (lazy, on first access) and rewritten atomically on every
# put/evict. The disk schema reserves a top-level "sessions" key so stateful
# multi-turn (session_id → turns) can land later with no migration.

def _cache_file() -> Path:
    return data_dir() / "cache.json"


def _ensure_loaded_locked() -> None:
    """Hydrate the in-memory cache from disk once per process. Caller holds the lock."""
    global _CACHE_LOADED, _SESSIONS
    if _CACHE_LOADED:
        return
    _CACHE_LOADED = True   # set first: a missing/corrupt file must not retry-load every call
    try:
        raw = json.loads(_cache_file().read_text())
    except (FileNotFoundError, ValueError, OSError):
        return
    if not isinstance(raw, dict):
        return
    files = raw.get("files")
    if isinstance(files, dict):
        for digest, d in files.items():
            try:
                _URI_CACHE[digest] = FileRef(
                    uri=d["uri"], name=d["name"], mime_type=d["mime_type"],
                    expires_at=float(d.get("expires_at") or 0.0),
                    state=str(d.get("state") or "ACTIVE"),
                )
            except (KeyError, TypeError, ValueError):
                continue   # skip a malformed entry, keep the rest
    sessions = raw.get("sessions")
    if isinstance(sessions, dict):
        _SESSIONS = sessions


def _persist_locked() -> None:
    """Atomically write the cache to disk. Caller holds the lock; best-effort."""
    path = _cache_file()
    payload = {
        "version": _CACHE_SCHEMA_VERSION,
        "files": {
            digest: {
                "uri": ref.uri, "name": ref.name, "mime_type": ref.mime_type,
                "expires_at": ref.expires_at, "state": ref.state,
            }
            for digest, ref in _URI_CACHE.items()
        },
        "sessions": _SESSIONS,   # reserved; preserved verbatim across writes
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, path)   # atomic on POSIX
    except OSError:
        pass   # a cache that can't persist still works in-memory for this session


def _cache_get(file_hash: str) -> Optional[FileRef]:
    with _CACHE_LOCK:
        _ensure_loaded_locked()
        return _URI_CACHE.get(file_hash)


def _cache_put(file_hash: str, ref: FileRef) -> None:
    with _CACHE_LOCK:
        _ensure_loaded_locked()
        _URI_CACHE[file_hash] = ref
        _persist_locked()


def _cache_evict(file_hash: str) -> None:
    with _CACHE_LOCK:
        _ensure_loaded_locked()
        _URI_CACHE.pop(file_hash, None)
        _persist_locked()


def _is_reusable(ref: FileRef) -> bool:
    """The cache never serves a uri past its expiry (minus a safety margin)."""
    if not ref.uri:
        return False
    if ref.expires_at <= 0:
        return True  # unknown expiry → trust until a GET says otherwise
    return (ref.expires_at - time.time()) >= _REUSE_MARGIN


async def get_or_upload(
    api_key: str,
    base_url: str,
    local_path: Any,
    mime: str,
    *,
    file_hash: Optional[str] = None,
) -> FileRef:
    """Return a cached, still-valid, ACTIVE fileUri for this file's *content*, or
    upload fresh and cache it.

    Keyed by sha256 of the file bytes (not path): identical content reuses;
    different content at the same path uploads anew. On expiry / 403 / FAILED the
    entry is evicted and re-uploaded. Single-flight per event loop.
    """
    path = Path(local_path)
    digest = file_hash or await compute_file_hash(path)

    # 1) fast path — cached, comfortably-unexpired, still ACTIVE server-side
    cached = _cache_get(digest)
    if cached is not None and _is_reusable(cached):
        try:
            resource = await get_file(api_key, base_url, cached.name)
            state = str(resource.get("state"))
            if state == "ACTIVE":
                cached.cached = True
                return cached
            if state == "PROCESSING":
                # still uploading server-side — WAIT for it, don't re-upload the same
                # bytes and orphan the in-flight file until 48h expiry (review #7).
                active = await wait_active(api_key, base_url, cached.name)
                cached.state = "ACTIVE"
                new_exp = _expires_at_epoch(active)
                if new_exp:
                    cached.expires_at = new_exp
                cached.cached = True
                return cached
            # any other state (FAILED) → fall through to evict + re-upload
        except GeminiFilesError:
            pass  # 403 / 404 / gone / wait-timeout → fall through to re-upload
        _cache_evict(digest)

    # 2) single-flight, scoped to THIS loop (a Future can't be awaited cross-loop)
    loop = asyncio.get_running_loop()
    key = (id(loop), digest)
    own_future: Optional[asyncio.Future] = None
    with _CACHE_LOCK:
        existing = _INFLIGHT.get(key)
        if existing is None:
            own_future = loop.create_future()
            _INFLIGHT[key] = own_future
    if own_future is None:
        return await existing  # another coroutine on this loop is already uploading

    try:
        ref = await upload_video(api_key, base_url, path, mime)
        if ref.state != "ACTIVE":
            active = await wait_active(api_key, base_url, ref.name)
            ref.state = "ACTIVE"
            # Refresh expiry from the ACTIVE resource — the PROCESSING-state
            # upload response may omit expirationTime (→ 0.0), which would make
            # _is_reusable trust the entry forever (review #6).
            new_exp = _expires_at_epoch(active)
            if new_exp:
                ref.expires_at = new_exp
        _cache_put(digest, ref)
        own_future.set_result(ref)
        return ref
    except Exception as exc:
        own_future.set_exception(exc)
        raise
    finally:
        with _CACHE_LOCK:
            _INFLIGHT.pop(key, None)
        # On CancelledError (a BaseException, not caught by `except Exception` above)
        # the Future is still pending. Don't CANCEL it — that would propagate
        # CancelledError into a concurrent same-loop waiter (review #8), which never
        # asked to be cancelled. Give the waiter a normal retryable error instead, so it
        # surfaces as a structured {success:false} rather than an uncaught cancellation.
        if not own_future.done():
            own_future.set_exception(
                GeminiFilesError("upload was cancelled before completion — retry"))
