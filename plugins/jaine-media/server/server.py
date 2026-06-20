"""jaine-media MCP server — media understanding for Claude Code via Gemini.

Claude can't watch video or hear audio; this routes a local file to Gemini (which
does both) and returns the answer. Uploads are content-hash cached on disk (~48h),
so re-asking about the same file is cheap. Tool: analyze_media (extract_frame,
prepare_media, fetch_media to follow). Launched bundled via `uv run --frozen
--offline` (no npx). stdout is JSON-RPC only — all logs go to stderr.
"""
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from agent import fetch, gemini_files, media, paths as agent_paths, tool_log, workspace

BASE = gemini_files.DEFAULT_GEMINI_BASE_URL

# Model selection — single source of truth. Defaults live here, each role is
# overridable by env, an explicit per-call `model` arg wins over both.
# Eval (reference/*_eval.py, 2026-06-19, real RU speech clips): EVERY current
# Gemini flash/pro model HEARS audio (incl. video-embedded) and localizes timecodes
# to ~±0.5s. The old "only 2.5-flash hears" was an audio-token accounting artifact —
# 2.5-family itemizes an AUDIO modality; 3.x fold audio into VIDEO tokens (audio_tokens
# reads 0 yet they transcribe fine). So defaults are just a stable, proven pick — never
# preview models. gemini-2.5-flash itemizes AUDIO (meaningful audio_tokens) and emits
# clean output without a transcription preamble.
DEFAULT_MODELS = {
    "analyze": "gemini-2.5-flash",   # stable, hears, clean output, meaningful audio_tokens
    # stable; NO model has a reliable timecode edge — the 2.5-flash-lite "sub-second
    # edge" was refuted on n=3 videos (a GT-alignment artifact, not skill). Locate is
    # inherently ~±0.5-1s; extract_frame's ±window mitigates that, not the model choice.
    "locate": "gemini-2.5-flash",
}
_MODEL_ENV = {
    "analyze": "JAINE_MEDIA_MODEL",
    "locate": "JAINE_MEDIA_LOCATE_MODEL",
}


def _model_for(role: str, override: str | None = None) -> str:
    """Resolve the model for a role: explicit override → env → built-in default."""
    if override:
        return override
    return os.environ.get(_MODEL_ENV[role]) or DEFAULT_MODELS[role]


# detail → maxOutputTokens (the spike hardcoded 900 and got truncated).
_DETAIL_TOKENS = {"brief": 512, "normal": 2048, "full": 8192}
_DEFAULT_DETAIL = "normal"


def _effective_max_tokens(detail: str = _DEFAULT_DETAIL, max_tokens: int | None = None) -> int:
    """`max_tokens` wins if given, else map the detail level (unknown → normal)."""
    if max_tokens is not None:
        return max_tokens
    return _DETAIL_TOKENS.get(detail, _DETAIL_TOKENS[_DEFAULT_DETAIL])


def _language_instruction(language: str | None) -> str:
    """Soft language steer. The default follows the question's language; an explicit
    `language` forces it. Both protect verbatim transcripts/quotes from being
    translated away (Chris's carve-out)."""
    target = language if language else "the same language as the question above"
    return (f"Write your answer in {target}. When transcribing or quoting, keep the "
            "original language of that content.")


def _media_part(ref, fps: int | None) -> dict:
    part: dict = {"fileData": {"mimeType": ref.mime_type, "fileUri": ref.uri}}
    if fps:
        part["videoMetadata"] = {"fps": fps}
    return part


def _build_request_body(refs: list, question: str, *, max_tokens: int,
                        fps: int | None = None, language: str | None = None,
                        history: list | None = None) -> dict:
    """Assemble the generateContent body: ONE media part per ref (several refs =
    compare them in a single request, #202 — full resolution each, unlike an
    ffmpeg-hstack), then the question with a language steer, HIGH media resolution.
    fps (timecode accuracy) applies to every video part.

    history (caller-passed, #206 — Gemini multi-turn is client-side/stateless): a
    list of prior turns {role: 'user'|'model', text, refs: [FileRef] (user turns
    with media)} replayed as Gemini user/model contents — media rides the turn it
    was in, so you can add a DIFFERENT video mid-conversation. The new question is
    the final user turn carrying the current refs. The server stays stateless; the
    caller owns the history (validated by panel + Gemini docs + goat + agy).

    Invariant: history should ALTERNATE user/model and END on a 'model' turn, so the
    appended question (always 'user') keeps the alternation. We replay it VERBATIM — no
    normalize/merge (that mutates caller-owned history and masks caller bugs). A non-
    alternating history is tolerated by Gemini (empirically HTTP 200, not 400) but may
    answer poorly; that degradation surfaces via finish_reason (EMPTY/MAX_TOKENS →
    complete=False), never a silent success."""
    contents: list = []
    for turn in (history or []):
        parts = [_media_part(r, fps) for r in turn.get("refs", [])]
        if turn.get("text"):
            parts.append({"text": turn["text"]})
        if not parts:
            continue            # skip a content-less turn — Gemini rejects empty parts (HTTP 400) (#6)
        contents.append({"role": turn["role"], "parts": parts})
    new_parts = [_media_part(r, fps) for r in refs]
    new_parts.append({"text": f"{question}\n\n{_language_instruction(language)}"})
    contents.append({"role": "user", "parts": new_parts})
    return {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,
            "mediaResolution": "MEDIA_RESOLUTION_HIGH",
        },
    }


_MAX_INPUTS = 10


def _collect_targets(path: str | None, paths: list[str] | None) -> list[Path]:
    """Resolve path/paths into a validated list of existing files (raises ValueError).
    `paths` (several, compared in one request) wins over a single `path`."""
    raw = paths if paths else ([path] if path else [])
    if not raw:
        raise ValueError("give 'path' (one file) or 'paths' (several files to compare)")
    if len(raw) > _MAX_INPUTS:
        raise ValueError(f"too many inputs ({len(raw)}); max {_MAX_INPUTS} per request")
    out = []
    for t in raw:
        p = Path(os.path.expanduser(t))
        if not p.is_file():
            raise ValueError(f"file not found: {t}")
        out.append(p)
    return out


# image/tts/embedding/etc are not media-understanding; 2.0/1.5/1.0 are retired (404);
# *-latest are moving aliases; customtools is a tool-calling variant — drop all.
_MODEL_SKIP = ("image", "tts", "embedding", "aqa", "native-audio", "dialog", "nano",
               "lyria", "banana", "deep-research", "customtools", "2.0", "1.5", "1.0",
               "exp-", "-latest")


def _filter_models(raw: list) -> list:
    """Keep flash/pro generateContent models; drop image/tts/retired/alias variants.
    Each entry: id, preview flag, context limits. Sorted by id (#202 list_models)."""
    out = []
    for m in raw:
        name = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if not any(k in name for k in ("flash", "pro")) or any(k in name for k in _MODEL_SKIP):
            continue
        out.append({"id": name, "preview": "preview" in name,
                    "input_limit": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")})
    return sorted(out, key=lambda x: x["id"])


async def _list_models(key: str) -> list:
    """Fetch the live flash/pro generateContent catalog (no hardcoded names → survives
    model ships/retires)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/models?pageSize=1000", headers={"x-goog-api-key": key})
        if r.status_code // 100 != 2:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        return _filter_models(r.json().get("models", []))


def _parse_response(d: dict) -> tuple[str, int, str]:
    """Pull (answer text, AUDIO prompt-token count, finish_reason) out of a
    generateContent response. finish_reason is the candidate's finishReason ('STOP'
    on a complete answer); 'BLOCKED:<reason>' when the whole prompt was refused (no
    candidate — reason in promptFeedback.blockReason); 'EMPTY' when there's neither.
    A finish_reason != 'STOP' means the answer is partial/absent (MAX_TOKENS = ran
    out of output budget; SAFETY = content blocked) — the caller surfaces it instead
    of returning a silently-truncated answer as success.

    NOTE audio is model-dependent telemetry, NOT a deafness signal: the 2.5-family
    itemizes AUDIO, but 3.x fold audio into VIDEO tokens (reads 0 even when they hear)."""
    audio = sum(int(x.get("tokenCount", 0))
                for x in d.get("usageMetadata", {}).get("promptTokensDetails", [])
                if str(x.get("modality")).upper() == "AUDIO")
    cands = d.get("candidates") or []
    if cands:
        cand = cands[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
        return text, audio, str(cand.get("finishReason") or "STOP").upper()
    block = (d.get("promptFeedback") or {}).get("blockReason")
    return "", audio, (f"BLOCKED:{block}".upper() if block else "EMPTY")


def _finish_note(finish: str) -> str | None:
    """Human hint when an answer didn't finish cleanly (finish_reason != STOP), so a
    truncated/blocked/empty reply is never reported as a clean success."""
    if finish == "STOP":
        return None
    if finish == "MAX_TOKENS":
        return "answer truncated — Gemini hit the output limit; raise max_tokens or use detail='full'"
    if finish.startswith("BLOCKED") or finish == "SAFETY":
        return f"Gemini blocked the response ({finish}) — the content may be restricted"
    if finish == "EMPTY":
        return ("empty response — the model returned no text; a thinking model may have spent its "
                "output budget on reasoning, so raise max_tokens / use detail='full'")
    return f"incomplete response (finish_reason={finish})"


_MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mp3": "audio/mp3", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
}

mcp = FastMCP("jaine-media")


def _mime_for(p: Path) -> str:
    return _MIME.get(p.suffix.lower(), "application/octet-stream")


async def _resolve_history(key: str, history: list) -> list:
    """Resolve each caller-passed history turn's `paths` into FileRefs (re-uploads
    expired ones via get_or_upload — fileUri only lives ~48h), leaving model turns
    text-only. #206: the server is stateless; the caller owns the conversation."""
    out = []
    for turn in history:
        t = {"role": turn.get("role", "user"), "text": turn.get("text", "")}
        hpaths = turn.get("paths") or []
        if hpaths:
            refs = []
            for hp in hpaths:
                p = Path(os.path.expanduser(hp))
                d = await gemini_files.compute_file_hash(p)
                refs.append(await gemini_files.get_or_upload(key, BASE, str(p), _mime_for(p), file_hash=d))
            t["refs"] = refs
        out.append(t)
    return out


async def _generate(key: str, refs: list, question: str, *, model: str,
                    max_tokens: int, fps: int | None, language: str | None,
                    history: list | None = None):
    url = f"{BASE}/models/{model}:generateContent"
    body = _build_request_body(refs, question, max_tokens=max_tokens, fps=fps,
                               language=language, history=history)
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, headers={"x-goog-api-key": key,
                                       "Content-Type": "application/json"}, json=body)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
    return _parse_response(d)


@mcp.tool()
async def analyze_media(path: str = "", question: str = "", detail: str = "normal",
                        max_tokens: int | None = None, model: str | None = None,
                        language: str | None = None, fps: int | None = None,
                        session_id: str | None = None, paths: list[str] | None = None,
                        history: list | None = None) -> str:
    """See/hear local VIDEO or AUDIO file(s) via Gemini and answer `question`.

    Claude itself can't watch video or hear audio — this routes the file to Gemini
    (which can do both) and returns the answer. Asking again about the SAME file is
    cheap: the upload is content-hash cached (~48h), on disk.

    Compare several clips: pass `paths=[a, b, ...]` — they go into ONE Gemini request
    at full resolution each (better than an ffmpeg side-by-side, which downscales both),
    so the answer can point at "in the first clip … but the second …" (#202).

    Continue a CONVERSATION about media (#206): pass `history` — the prior turns YOU
    (the caller) hold. Gemini multi-turn is stateless/client-side, so the server just
    replays your history into the request; it does NOT keep session state. You can add
    a DIFFERENT video in a later turn and compare against earlier ones.

    Params (all optional except a file + question):
      path / paths: one file, or several files to compare in one request (paths wins).
      history:    prior turns [{role: 'user'|'model', text, paths: [files] (user turns)}].
                  The server re-uploads any expired files by content-hash. Caller-owned.
      detail:     'brief' | 'normal' | 'full' → output length (512 / 2048 / 8192 tokens).
      max_tokens: hard override for the output cap (wins over `detail`).
      model:      override the model (default JAINE_MEDIA_MODEL → gemini-2.5-flash).
      language:   force the answer language (e.g. 'ru'); default follows the question.
      fps:        sample video at this many frames/sec — raises timecode accuracy.
      session_id: RESERVED — server-side sessions are an anti-pattern for MCP (#206); use `history`.
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return json.dumps({"success": False, "error": "GEMINI_API_KEY not set in server env"})
    if not question:
        return json.dumps({"success": False, "error": "question is required"})
    if path or paths:
        try:
            targets = _collect_targets(path, paths)
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)})
    elif history:
        targets = []                         # follow-up: the media lives in the history
    else:
        return json.dumps({"success": False,
                           "error": "give 'path'/'paths', or 'history' to continue a conversation"})

    chosen_model = _model_for("analyze", model)
    eff_tokens = _effective_max_tokens(detail, max_tokens)
    anchor = None                              # set once hashing succeeds; None-safe in except
    try:
        digests = [await gemini_files.compute_file_hash(p) for p in targets]
        anchor = digests[0] if digests else None   # follow-up has no new file to key on
        resolved_history = await _resolve_history(key, history) if history else None
        refs = [await gemini_files.get_or_upload(key, BASE, str(p), _mime_for(p), file_hash=d)
                for p, d in zip(targets, digests)]
        cached_before = all(r.cached for r in refs) if refs else None   # post ACTIVE-verify (#15)
        answer, audio_tokens, finish = await _generate(key, refs, question, model=chosen_model,
                                                       max_tokens=eff_tokens, fps=fps,
                                                       language=language, history=resolved_history)
    except Exception as e:
        # Contract: never crash, always return a structured error. Besides the expected
        # RuntimeError (HTTP 404/429/503, upload errors), this also covers OSError (a file
        # deleted between validation and hashing — incl. a vanished history path) and httpx
        # transport errors (timeout/connect on a slow endpoint), neither a RuntimeError (#1/#2/#3).
        tool_log.log_tool("analyze_media", False, digest=anchor, model=chosen_model, error=str(e))
        err = {"success": False, "error": str(e),
               "inputs": [str(p) for p in targets], "model": chosen_model}
        # a bad model 404s — surface what IS available so the next call picks a real one (#202)
        if any(s in str(e).lower() for s in ("404", "not available", "not found")):
            try:
                err["available_models"] = [m["id"] for m in await _list_models(key)]
            except Exception:
                pass
        return json.dumps(err, ensure_ascii=False)

    result = {
        "success": True,
        "model": chosen_model, "detail": detail, "max_tokens": eff_tokens,
        "cached_before": cached_before,     # True only if EVERY input was already uploaded
        # raw AUDIO-modality token count — model-dependent telemetry, NOT a deafness
        # signal (3.x fold audio into VIDEO → 0 even when they hear); see _parse_response
        "audio_tokens": audio_tokens,
        # finish_reason != STOP ⇒ answer is partial/blocked/empty, not a clean success
        "finish_reason": finish, "complete": finish == "STOP",
        "analysis": answer,
    }
    if len(targets) == 1:                    # back-compat single-file shape
        result["file"] = str(targets[0])
        result["mime"] = _mime_for(targets[0])
        result["fileUri"] = refs[0].uri
    elif targets:
        result["inputs"] = [str(p) for p in targets]
        result["n_inputs"] = len(targets)
    else:
        result["continued"] = True           # follow-up turn — the media was in history
    if history:
        result["history_turns"] = len(history)
    note = _finish_note(finish)
    if note:
        result["note"] = note

    # Source symlink in the (anchor) workspace ONLY when there's a new file + one line
    # in the central tool log (best-effort). workspace.prepare both makes the dir and
    # symlinks the source, so use its return rather than agent_paths.workspace_dir alone.
    # Best-effort: this runs AFTER a successful (paid) Gemini call, so a workspace_dir().mkdir
    # OSError (full / read-only / perm-denied data-fs) must NOT sink the answer or escape to
    # FastMCP — unlike extract_frame/prepare_media, the symlink here is cosmetic (never-crash).
    if anchor:
        try:
            ws = await asyncio.to_thread(workspace.prepare, anchor, targets[0])
            result["workspace"] = str(ws)
        except Exception:
            pass
    tool_log.log_tool("analyze_media", True, digest=anchor, model=chosen_model,
                      detail=detail, max_tokens=eff_tokens, cached_before=cached_before,
                      audio_tokens=audio_tokens, finish_reason=finish, n_inputs=len(targets),
                      history_turns=(len(history) if history else None),
                      question=question, answer=answer)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_models() -> str:
    """List Gemini models available for analyze_media's `model` param (flash/pro,
    generateContent). Each entry: id, preview flag, context limits — pick a model
    without guessing names (a wrong name 404s). Retired/nonexistent ids (e.g.
    gemini-2.0-*, gemini-3.5-pro) are filtered out. Capability sweep (hears/speed/
    finish_reason per model): reference/gemini-models.md."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return json.dumps({"success": False, "error": "GEMINI_API_KEY not set in server env"})
    try:
        models = await _list_models(key)
    except (RuntimeError, httpx.HTTPError) as e:
        return json.dumps({"success": False, "error": str(e)})
    tool_log.log_tool("list_models", True, count=len(models))
    return json.dumps({"success": True, "models": models, "default": _model_for("analyze", None)},
                      ensure_ascii=False)


@mcp.tool()
async def extract_frame(path: str, timecode: float, window: float = 1.0,
                        step: float = 0.5) -> str:
    """Extract a window of still frames around `timecode` (seconds) as PNGs you can Read.

    analyze_media locates WHEN something happens but only to ~±0.5-1s, so this brackets
    the moment: it cuts a frame every `step` s across `timecode` ±`window` into the
    media's workspace `frames/` folder. The loop closes here — analyze_media finds the
    second, extract_frame cuts the frames, you Read the PNGs natively. Returns each
    frame's path + timecode (use window=0 for a single exact frame).
    """
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return json.dumps({"success": False, "error": f"file not found: {path}"})
    invalid = media.validate_frame_request(timecode, window=window, step=step)
    if invalid:                                    # bound input BEFORE any ffmpeg (DoS guard)
        return json.dumps({"success": False, "error": invalid})
    if not (media.has_tool("ffmpeg") and media.has_tool("ffprobe")):
        return json.dumps({"success": False, "error": "ffmpeg/ffprobe not on PATH"})
    try:
        duration = await asyncio.to_thread(media.probe_duration, p)
    except Exception as e:
        return json.dumps({"success": False, "error": f"ffprobe failed: {e}"})

    try:
        digest = await gemini_files.compute_file_hash(p)
        ws = await asyncio.to_thread(workspace.prepare, digest, p)   # symlink, off the loop (#11)
        frames_dir = ws / "frames"
        frames = []
        for t in media.frame_timecodes(timecode, window=window, step=step, duration=duration):
            out = frames_dir / f"frame_{t:06.2f}.png"
            if await asyncio.to_thread(media.extract_png, p, t, out):
                frames.append({"t": t, "path": str(out)})
    except Exception as e:                             # never crash on a fs/hash error (#1/#11)
        tool_log.log_tool("extract_frame", False, error=str(e))
        return json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"})

    tool_log.log_tool("extract_frame", True, digest=digest, timecode=timecode,
                      window=window, step=step, frame_count=len(frames))
    return json.dumps({
        "success": True,
        "file": str(p), "timecode": timecode, "window": window, "step": step,
        "frames_dir": str(frames_dir),
        "frames": frames,                              # [{t, path}] — Read these PNGs
    }, ensure_ascii=False)


@mcp.tool()
async def prepare_media(path: str, height: int | None = None,
                        start: float | None = None, end: float | None = None) -> str:
    """Shrink media to fit Gemini's Files API, or trim it to a time range.

    For a video that's too LONG (too many tokens) prefer analyze_media's `fps` param
    FIRST — it cuts tokens with no content loss and no re-encode. Use this tool for:
      - too BIG (size > Files API limit): compress (downscale resolution/bitrate) —
        content is preserved, only fidelity drops. Default: step down 720→480→360→240
        until it fits, or pass an explicit `height`.
      - explicit TRIM: pass `start`/`end` (seconds) to cut a range. NEVER silent — the
        result reports exactly what was kept and what was dropped.
    The prepared file lands in the media's workspace. Returns its path + what was done.
    """
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return json.dumps({"success": False, "error": f"file not found: {path}"})
    invalid = media.validate_prepare_request(height, start, end)
    if invalid:                                    # bound input BEFORE any ffmpeg
        return json.dumps({"success": False, "error": invalid})
    if not (media.has_tool("ffmpeg") and media.has_tool("ffprobe")):
        return json.dumps({"success": False, "error": "ffmpeg/ffprobe not on PATH"})

    try:
        digest = await gemini_files.compute_file_hash(p)
        ws = await asyncio.to_thread(workspace.prepare, digest, p)   # off the loop, use return (#11)
    except Exception as e:                             # never crash on a fs/hash error (#2/#11)
        tool_log.log_tool("prepare_media", False, error=str(e))
        return json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"})
    suffix = p.suffix.lower() or ".mp4"

    # --- explicit trim (never silent) ---
    if start is not None or end is not None:
        try:
            duration = await asyncio.to_thread(media.probe_duration, p)
        except Exception as e:
            return json.dumps({"success": False, "error": f"ffprobe failed: {e}"})
        s = max(0.0, start if start is not None else 0.0)
        e = min(duration, end if end is not None else duration)
        out = ws / f"trimmed_{s:g}-{e:g}{suffix}"
        if not await asyncio.to_thread(media.trim, p, out, start=s, end=e):
            tool_log.log_tool("prepare_media", False, digest=digest, mode="trim", error="ffmpeg trim failed")
            return json.dumps({"success": False, "error": "ffmpeg trim failed"})
        dropped = []
        if s > 0:
            dropped.append(f"0-{s:g}s")
        if e < duration:
            dropped.append(f"{e:g}-{duration:g}s")
        tool_log.log_tool("prepare_media", True, digest=digest, mode="trim",
                          kept=f"{s:g}-{e:g}s", dropped=dropped or "nothing")
        return json.dumps({
            "success": True, "mode": "trim", "input": str(p), "output": str(out),
            "kept": f"{s:g}-{e:g}s", "dropped": dropped or "nothing",
            "note": "trim is explicit — only the kept range will be sent to Gemini",
        }, ensure_ascii=False)

    # --- compress to fit (size) ---
    before = media.fits(p)
    if height is None and before["fits"]:
        tool_log.log_tool("prepare_media", True, digest=digest, mode="none", size_mb=before["size_mb"])
        return json.dumps({"success": True, "mode": "none", "input": str(p),
                           "note": "already fits the size limit", "fits": before},
                          ensure_ascii=False)

    out = None
    for h in ([height] if height else [720, 480, 360, 240]):
        cand = ws / f"compressed_{h}p{suffix}"
        if await asyncio.to_thread(media.compress, p, cand, height=h):
            out = cand
            if height is not None or media.fits(cand)["fits"]:
                break
    if out is None:
        tool_log.log_tool("prepare_media", False, digest=digest, mode="compress", error="ffmpeg compress failed")
        return json.dumps({"success": False, "error": "ffmpeg compress failed"})

    after = media.fits(out)
    tool_log.log_tool("prepare_media", True, digest=digest, mode="compress",
                      before_mb=before["size_mb"], after_mb=after["size_mb"], fits_now=after["fits"])
    return json.dumps({
        "success": True, "mode": "compress", "input": str(p), "output": str(out),
        "before_mb": before["size_mb"], "after_mb": after["size_mb"], "fits_now": after["fits"],
        "note": None if after["fits"] else "still over the limit — lower height or trim",
    }, ensure_ascii=False)


@mcp.tool()
async def fetch_media(url: str, max_height: int = 720, prepare: bool = True) -> str:
    """Download a remote video/audio URL (YouTube etc.) to a local file via yt-dlp,
    landing it on the same content-hash workspace as the other tools.

    Safety + efficiency (all enforced here):
      - SSRF guard: http(s) only; the URL's INITIAL host must not resolve to a private /
        loopback / link-local / non-global address. Covers only the URL you pass — NOT
        redirects or DNS-rebind (yt-dlp resolves + follows 30x itself). Personal-use tool:
        do not feed it untrusted URLs.
      - Resolution capped at `max_height` AT DOWNLOAD time (no pulling 4K / hours).
      - Size capped at JAINE_MEDIA_MAX_DOWNLOAD_MB (~8GB) AT DOWNLOAD time — a runaway
        direct URL (no format ladder for the resolution cap to bound) aborts before it
        fills the disk; the ceiling sits ABOVE the Gemini fit limit so compress still runs.
      - fit-check → auto-`prepare` (compress) backstop if the download still won't fit
        Gemini's Files API (`prepare=False` to skip). Then call analyze_media on `file`.
    """
    unsafe = fetch.validate_url(url)               # security first — reject before any work
    if unsafe:
        tool_log.log_tool("fetch_media", False, url=url, error=f"unsafe URL refused: {unsafe}")
        return json.dumps({"success": False, "error": f"unsafe URL refused: {unsafe}"})
    if not media.has_tool("yt-dlp"):
        return json.dumps({"success": False, "error": "yt-dlp not on PATH"})
    if not media.has_tool("ffmpeg"):
        return json.dumps({"success": False,
                           "error": "ffmpeg not on PATH (yt-dlp needs it to merge DASH streams)"})

    tmp = tempfile.mkdtemp(prefix="jaine-media-fetch-")
    try:
        dl = await asyncio.to_thread(fetch.download, url, tmp, max_height=max_height)
        if dl is None:
            tool_log.log_tool("fetch_media", False, url=url, error="yt-dlp download failed")
            return json.dumps({"success": False, "error": "yt-dlp download failed"})
        digest = await gemini_files.compute_file_hash(dl)
        final = agent_paths.workspace_dir(digest) / f"source{dl.suffix.lower()}"
        if final.exists():
            final.unlink()                          # replace a partial/corrupt leftover from an
                                                    # interrupted prior fetch — never reuse it (#14)
        await asyncio.to_thread(shutil.move, str(dl), str(final))   # temp → workspace, off loop (#12)

        verdict = media.fits(final)
        result = {"success": True, "url": url, "file": str(final),
                  "workspace": str(agent_paths.workspace_dir(digest)),
                  "fits": verdict["fits"], "size_mb": verdict["size_mb"]}
        if not verdict["fits"] and prepare:         # backstop — primary cap was at download
            for h in (720, 480, 360, 240):
                cand = agent_paths.workspace_dir(digest) / f"compressed_{h}p.mp4"
                if await asyncio.to_thread(media.compress, final, cand, height=h) and media.fits(cand)["fits"]:
                    result["prepared"] = str(cand)
                    result["prepared_mb"] = media.fits(cand)["size_mb"]
                    result["note"] = f"download exceeded the size limit — compressed to {h}p (analyze the 'prepared' file)"
                    break
        tool_log.log_tool("fetch_media", True, digest=digest, url=url, max_height=max_height,
                          size_mb=verdict["size_mb"], fits=verdict["fits"], prepared="prepared" in result)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:                          # full disk / cross-device / fs error (#4)
        tool_log.log_tool("fetch_media", False, url=url, error=str(e))
        return json.dumps({"success": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    mcp.run()
