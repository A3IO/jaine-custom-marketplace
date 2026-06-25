"""jaine-media MCP server — media understanding for Claude Code via Gemini.

Claude can't watch video or hear audio; this routes a local file to Gemini (which
does both) and returns the answer. Uploads are content-hash cached on disk (~48h),
so re-asking about the same file is cheap. Tools: analyze_media, extract_frame,
prepare_media, fetch_media, list_models. Launched bundled via `uv run --frozen`
(self-syncs the venv on launch; no npx). stdout is JSON-RPC only — logs go to stderr.
"""
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from agent import dead_models, fetch, gemini_files, media, paths as agent_paths, tool_log, workspace

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


# detail → visible-answer char cap. Separate from maxOutputTokens (which also feeds the
# thinking pool, python-genai #2062); this frames ONLY the text returned to Claude Code,
# enforced client-side in _frame_answer — the full text is dropped to a workspace file.
_DETAIL_CHARS = {"brief": 2000, "normal": 8000, "full": 32000}


def _answer_char_limit(detail: str = _DEFAULT_DETAIL, answer_chars: int | None = None) -> int:
    """Visible-answer char cap. Override wins, else map the detail level (unknown → normal)."""
    if answer_chars is not None:
        return answer_chars
    return _DETAIL_CHARS.get(detail, _DETAIL_CHARS[_DEFAULT_DETAIL])


def _language_instruction(language: str | None) -> str:
    """Soft language steer. The default follows the question's language; an explicit
    `language` forces it. Both protect verbatim transcripts/quotes from being
    translated away (Chris's carve-out)."""
    target = language if language else "the same language as the question above"
    return (f"Write your answer in {target}. When transcribing or quoting, keep the "
            "original language of that content.")


def _media_part(ref, fps: int | None) -> dict:
    fd: dict = {"fileUri": ref.uri}
    if ref.mime_type:                          # a native YouTube part (#229) carries NO mimeType —
        fd["mimeType"] = ref.mime_type         # Gemini ingests the URL itself; only uploads have one
    part: dict = {"fileData": fd}
    if fps:
        part["videoMetadata"] = {"fps": fps}
    return part


def _native_ref(url: str):
    """A FileRef for a native YouTube passthrough: the URL IS the fileUri, no upload and no
    PROCESSING wait (Gemini fetches it server-side). mime_type is empty so _media_part omits
    mimeType, and state=ACTIVE so the cache's ACTIVE-verify is a no-op (#229)."""
    return gemini_files.FileRef(uri=url, name=url, mime_type="", expires_at=0.0, state="ACTIVE")


def _is_native(raw_inputs: list[str], history: list | None) -> bool:
    """Whether to use the native YouTube passthrough (#229): a SINGLE public YouTube URL with no
    ongoing conversation. One-shot is faster end-to-end (no download/upload on our side). Multi-
    turn (history) re-pulls the URL every turn, and native supports at most one YouTube link per
    request — both fall back to download+upload."""
    return len(raw_inputs) == 1 and fetch.is_youtube_url(raw_inputs[0]) and not history


# mediaResolution per model family (reference/media-resolution-tokens.md, measured): on 2.5,
# HIGH == default (~263 tok/frame) so it's free — keep it. On 3.x, HIGH is ~3.4x their cheap
# default (~289 vs ~85) and only helps OCR/fine text → default to MEDIUM. Unknown family: keep
# HIGH (don't silently downgrade a model we haven't measured). #232
_MEDIA_RESOLUTION = {"high": "MEDIA_RESOLUTION_HIGH", "medium": "MEDIA_RESOLUTION_MEDIUM",
                     "low": "MEDIA_RESOLUTION_LOW"}


def _media_resolution_for(model: str) -> str:
    """Family-aware mediaResolution default: HIGH on 2.5 (free), MEDIUM on 3.x (HIGH there is a
    3.4x premium, OCR-only), HIGH for unknown families (conservative — never silently downgrade)."""
    if "gemini-2.5" in model:
        return "MEDIA_RESOLUTION_HIGH"
    if "gemini-3" in model:
        return "MEDIA_RESOLUTION_MEDIUM"
    return "MEDIA_RESOLUTION_HIGH"


def _build_request_body(refs: list, question: str, *, max_tokens: int,
                        fps: int | None = None, language: str | None = None,
                        history: list | None = None,
                        media_resolution: str = "MEDIA_RESOLUTION_HIGH") -> dict:
    """Assemble the generateContent body: ONE media part per ref (several refs =
    compare them in a single request, #202 — full resolution each, unlike an
    ffmpeg-hstack), then the question with a language steer, family-aware media
    resolution (#232 — HIGH on 2.5 where it's free, MEDIUM on 3.x where HIGH is a
    3.4x premium; caller resolves it, default HIGH).
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
    # NO length steer: the model answers freely so the full text dropped to disk is genuinely
    # full; the visible cap is applied ONLY client-side in _frame_answer (#223 — a soft steer
    # measurably shortened Gemini's output, defeating "think freely, frame the answer").
    qtext = f"{question}\n\n{_language_instruction(language)}"
    new_parts.append({"text": qtext})
    contents.append({"role": "user", "parts": new_parts})
    return {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,
            "mediaResolution": media_resolution,
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


async def _prepare_for_upload(p: Path, work_dir: str) -> Path:
    """Ensure a downloaded file fits Gemini's Files API AND is <=1080p before upload — parity
    with fetch_media's #230 backstop (codex P2). fetch.download caps at 720p, but its /w fallback
    can return an oversized/4K single-format/direct URL; that would time out Gemini's upload/
    generate. Returns a downscaled copy in `work_dir` when needed, else `p` unchanged. Fail-open:
    no ffprobe/ffmpeg → can't probe/compress → upload as-is (Gemini surfaces a structured error)."""
    try:
        try:
            _, height = media.probe_dimensions(p)
        except Exception:
            height = 0                             # unprobeable → don't force a downscale
        if media.fits(p)["fits"] and height <= fetch._MAX_HEIGHT:
            return p
        for h in (720, 480, 360, 240):
            cand = Path(work_dir) / f"compressed_{h}p.mp4"
            if await asyncio.to_thread(media.compress, p, cand, height=h) and media.fits(cand)["fits"]:
                return cand
    except Exception:
        pass                                        # ffmpeg/ffprobe missing or compress raised →
    return p                                         # fail-open: upload the original as-is


async def _localize_one(t: str, dest_dir: str) -> Path:
    """One raw input → a local Path (#229). A URL is SSRF-validated then downloaded (yt-dlp,
    720p/size-capped) into `dest_dir` — the caller passes a UNIQUE dir, since fetch.download
    writes a fixed dl.* name and a shared dir would collide (codex P2) — and downscaled if it
    still escaped the caps (_prepare_for_upload). A local path is expanded and existence-checked
    (`dest_dir` unused). Shared by current inputs AND history media."""
    if fetch.is_url(t):
        unsafe = fetch.validate_url(t)
        if unsafe:
            raise ValueError(f"unsafe URL refused: {unsafe}")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        dl = await asyncio.to_thread(fetch.download, t, dest_dir)
        if dl is None:
            raise ValueError(f"download failed (yt-dlp): {t}")
        return await _prepare_for_upload(dl, dest_dir)
    p = Path(os.path.expanduser(t))
    if not p.is_file():
        raise ValueError(f"file not found: {t}")
    return p


async def _localize(raw: list[str], tmpdir: str) -> list[Path]:
    """Current-turn inputs → local Paths for the upload pipeline (#229 download route): download
    each URL (into its own sub-dir of `tmpdir`) / validate each local path, raising on a bad
    input. The native YouTube fast-path is decided BEFORE this — it never reaches here."""
    return [await _localize_one(t, str(Path(tmpdir) / f"in{i}")) for i, t in enumerate(raw)]


# image/tts/embedding/etc are not media-understanding; 2.0/1.5/1.0 are retired (404);
# *-latest are moving aliases; customtools is a tool-calling variant — drop all.
_MODEL_SKIP = ("image", "tts", "embedding", "aqa", "native-audio", "dialog", "nano",
               "lyria", "banana", "deep-research", "customtools", "2.0", "1.5", "1.0",
               "exp-", "-latest")


def _filter_models(raw: list, dead: set[str] | None = None) -> list:
    """Keep flash/pro generateContent models; drop image/tts/retired/alias variants AND any id
    in `dead` (learned-from-404 skip-list, #233 — models.list still advertises retired ids).
    Each entry: id, preview flag, context limits. Sorted by id (#202 list_models)."""
    dead = dead or set()
    out = []
    for m in raw:
        name = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if not any(k in name for k in ("flash", "pro")) or any(k in name for k in _MODEL_SKIP):
            continue
        if name in dead:
            continue                          # 404'd on use before → hide it (#233 learn-from-404)
        out.append({"id": name, "preview": "preview" in name,
                    "input_limit": m.get("inputTokenLimit"), "output_limit": m.get("outputTokenLimit")})
    return sorted(out, key=lambda x: x["id"])


async def _list_models(key: str) -> list:
    """Fetch the live flash/pro generateContent catalog (no hardcoded names → survives
    model ships/retires), minus any id learned-dead from a prior 404 (#233)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/models?pageSize=1000", headers={"x-goog-api-key": key})
        if r.status_code // 100 != 2:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        return _filter_models(r.json().get("models", []), dead=dead_models.load())


def _model_is_dead(err: str, model: str) -> bool:
    """A 404 that specifically names `model` as retired/absent (#233 learn-from-404). Requires
    the model id IN the message so a 404 on a different resource (a stale fileUri / unrelated
    request) can't poison the skip-list with a still-working model. "no longer available" is the
    retired signature; "not found" covers a nonexistent id (harmless — not in the catalog anyway)."""
    e = err.lower()
    return bool(model) and model.lower() in e and ("no longer available" in e or "not found" in e)


def _parse_response(d: dict) -> tuple[str, int, str]:
    """Pull (answer text, AUDIO prompt-token count, finish_reason) out of a
    generateContent response. finish_reason is the candidate's finishReason ('STOP'
    on a complete answer); 'BLOCKED:<reason>' when the whole prompt was refused (no
    candidate — reason in promptFeedback.blockReason); 'EMPTY' when there's neither, or
    a candidate came back textless (a STOP with no text — thinking spent the whole output
    budget, #231) so an empty answer is never reported as a clean success.
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
        finish = str(cand.get("finishReason") or "STOP").upper()
        if not text and finish == "STOP":   # candidate present but textless = deceptive STOP (#231)
            finish = "EMPTY"                 # thinking ate the budget → flag it, never silent success
        return text, audio, finish
    block = (d.get("promptFeedback") or {}).get("blockReason")
    return "", audio, (f"BLOCKED:{block}".upper() if block else "EMPTY")


def _usage_tokens(d: dict) -> tuple[int, int]:
    """(thinking, visible-answer) token counts from usageMetadata — the split that exposes
    when thinking starved the shared output pool (Grok's observability point). Both 0 when
    absent (2.5-family without a thinkingConfig omits thoughtsTokenCount)."""
    u = d.get("usageMetadata", {})
    return int(u.get("thoughtsTokenCount", 0) or 0), int(u.get("candidatesTokenCount", 0) or 0)


def _finish_note(finish: str, thought: int = 0, cand: int = 0) -> str | None:
    """Human hint when an answer didn't finish cleanly (finish_reason != STOP), so a
    truncated/blocked/empty reply is never reported as a clean success. thought/cand are
    the thinking vs visible-answer token counts (usageMetadata) — they let us name the
    real cause: thinking and output share ONE maxOutputTokens pool (probe + python-genai
    #2062), so a thinking model can starve its own answer."""
    if finish == "STOP":
        return None
    if finish == "MAX_TOKENS":
        if thought > cand:          # thinking consumed the shared pool, not a long answer
            return ("answer truncated — the model's thinking consumed the output budget; "
                    "raise max_tokens, or switch to a non-thinking model")
        return "answer truncated — Gemini hit the output limit; raise max_tokens or use detail='full'"
    if finish.startswith("BLOCKED") or finish == "SAFETY":
        return f"Gemini blocked the response ({finish}) — the content may be restricted"
    if finish == "EMPTY":
        return ("empty response — the model returned no text; a thinking model may have spent its "
                "output budget on reasoning, so raise max_tokens / use detail='full'")
    return f"incomplete response (finish_reason={finish})"


def _frame_answer(text: str, limit: int, saved: bool = True) -> tuple[str, bool]:
    """Cap the VISIBLE answer at `limit` chars so the reply reaching Claude Code's context
    stays bounded (Chris: think freely, frame the answer). Gemini gives no separate visible-
    output limit — thinking shares the maxOutputTokens pool (python-genai #2062) — so a
    client-side cut is the only hard guarantee; the caller drops the FULL text to a workspace
    file so nothing is lost. The marker is HONEST: it points at full_answer_file only when the
    caller actually persisted it (`saved`) — a follow-up with no anchor, or a data-fs write
    failure, leaves `saved=False` so we don't promise a file that isn't there. Returns
    (framed_text, truncated)."""
    if len(text) <= limit:
        return text, False
    tail = "полный ответ в full_answer_file" if saved else "полный ответ сохранить не удалось"
    marker = f"\n\n[… обрезано jaine-media до {limit} симв.; {tail}]"
    return text[:limit].rstrip() + marker, True


_MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mp3": "audio/mp3", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
}

# #228: routing manifest injected into the model's context when this server connects
# (MCP InitializeResult.instructions — Claude Code injects it; verified via issue #30135).
# Deliberately client/backend-agnostic — MCP is client-agnostic and the backend may change,
# so nothing here names a client or a provider, only the capability + intent→tool routing.
# The "cannot natively inspect" negative constraint is load-bearing (consult-panel finding):
# without it the model guesses from filenames/metadata or writes its own ffmpeg.
INSTRUCTIONS = """\
Tools for understanding the CONTENTS of video and audio — a language model cannot
natively inspect media payloads. Do NOT answer media-content questions from a filename,
metadata, or the user's description, and do NOT write your own ffmpeg/python to process
media — delegate to these tools.

Route by intent:
- A video/audio file (.mp4/.mov/.webm/.mkv/.mp3/.wav/.m4a…) or a YouTube/web URL, and a
  question about what's in it ("what happens", "transcribe", "describe", "at what point…")
  → analyze_media(path=<file-or-URL>, question=...). A URL is passed directly and
  auto-routes (native YouTube vs download). Compare clips → paths=[...]. Continue the
  discussion or add clips mid-conversation → history=[...]. Tune with detail
  (brief|normal|full), model, language, fps.
- SEE exact frames at a moment → extract_frame(path, timecode, window) — returns image
  frames the agent reads directly.
- A URL/YouTube is large, or you want a local copy first → fetch_media(url) (SSRF-guarded,
  quality-capped) → workspace file.
- A file is too large/long to process → prepare_media(path) — compress or trim to fit.
- Picking a model, or a model errored → list_models() — live catalog of usable models.

Typical chain: fetch_media (if remote) → prepare_media (if oversized) → analyze_media →
extract_frame (exact moments). For most "what's in this video/audio" tasks call
analyze_media directly — it accepts files and URLs and fetches internally."""

mcp = FastMCP("jaine-media", instructions=INSTRUCTIONS)


def _mime_for(p: Path) -> str:
    return _MIME.get(p.suffix.lower(), "application/octet-stream")


async def _resolve_history(key: str, history: list, tmpdir: str | None = None) -> list:
    """Resolve each caller-passed history turn's `paths` into FileRefs (re-uploads
    expired ones via get_or_upload — fileUri only lives ~48h), leaving model turns
    text-only. #206: the server is stateless; the caller owns the conversation. A history
    path may be a URL (#229) — it's downloaded into `tmpdir` (set by the caller when any
    input/history path is a URL) just like a current-turn URL."""
    out = []
    for ti, turn in enumerate(history):
        t = {"role": turn.get("role", "user"), "text": turn.get("text", "")}
        hpaths = turn.get("paths") or []
        if hpaths:
            refs = []
            for pi, hp in enumerate(hpaths):
                # a history media path may be a URL too (#229) — download+upload it the same way,
                # so continuing a conversation about a YouTube video works (tmpdir is set whenever
                # ANY input/history path is a URL; local paths ignore dest).
                dest = str(Path(tmpdir) / f"h{ti}_{pi}") if tmpdir else ""
                p = await _localize_one(hp, dest)
                d = await gemini_files.compute_file_hash(p)
                refs.append(await gemini_files.get_or_upload(key, BASE, str(p), _mime_for(p), file_hash=d))
            t["refs"] = refs
        out.append(t)
    return out


async def _generate(key: str, refs: list, question: str, *, model: str,
                    max_tokens: int, fps: int | None, language: str | None,
                    history: list | None = None, media_resolution: str | None = None):
    url = f"{BASE}/models/{model}:generateContent"
    body = _build_request_body(refs, question, max_tokens=max_tokens, fps=fps,
                               language=language, history=history,
                               media_resolution=media_resolution or _media_resolution_for(model))
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(url, headers={"x-goog-api-key": key,
                                       "Content-Type": "application/json"}, json=body)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
    text, audio, finish = _parse_response(d)
    thought, cand = _usage_tokens(d)
    return text, audio, finish, thought, cand


@mcp.tool()
async def analyze_media(path: str = "", question: str = "", detail: str = "normal",
                        max_tokens: int | None = None, answer_chars: int | None = None,
                        model: str | None = None,
                        language: str | None = None, fps: int | None = None,
                        media_resolution: str | None = None,
                        session_id: str | None = None, paths: list[str] | None = None,
                        history: list | None = None) -> str:
    """See/hear a VIDEO or AUDIO — a local file OR a URL — via Gemini and answer `question`.

    Claude itself can't watch video or hear audio — this routes the media to Gemini
    (which can do both) and returns the answer. Asking again about the SAME file is
    cheap: the upload is content-hash cached (~48h), on disk.

    URLs Just Work (#229): a public YouTube link as a one-shot (no `history`) goes STRAIGHT
    to Gemini — no download, no upload (fastest end-to-end). Any other URL — or a YouTube URL
    inside a multi-turn `history` — is downloaded (SSRF-guarded, 720p-capped) then uploaded so
    the fileUri is reused. You never choose; just pass the URL or the path.

    Compare several clips: pass `paths=[a, b, ...]` — they go into ONE Gemini request
    at full resolution each (better than an ffmpeg side-by-side, which downscales both),
    so the answer can point at "in the first clip … but the second …" (#202).

    Continue a CONVERSATION about media (#206): pass `history` — the prior turns YOU
    (the caller) hold. Gemini multi-turn is stateless/client-side, so the server just
    replays your history into the request; it does NOT keep session state. You can add
    a DIFFERENT video in a later turn and compare against earlier ones.

    Params (all optional except a file/URL + question):
      path / paths: one local file OR a URL, or several to compare in one request (paths wins).
                  A single public YouTube URL with no `history` uses the native fast-path (#229).
      history:    prior turns [{role: 'user'|'model', text, paths: [files] (user turns)}].
                  The server re-uploads any expired files by content-hash. Caller-owned.
      detail:     'brief' | 'normal' | 'full' → both the model output cap (512 / 2048 /
                  8192 tokens) AND the VISIBLE-answer char frame (2000 / 8000 / 32000) that
                  bounds what reaches Claude Code's context. The model thinks/answers freely;
                  an over-long reply is cut client-side and the FULL text dropped to
                  workspace/<sha>/answer-<n>.md (Gemini has no separate visible-output limit
                  — thinking shares the token pool, python-genai #2062).
      max_tokens: hard override for the model output cap (wins over `detail`).
      answer_chars: hard override for the visible-answer char frame (wins over `detail`).
      model:      override the model (default JAINE_MEDIA_MODEL → gemini-2.5-flash).
      language:   force the answer language (e.g. 'ru'); default follows the question.
      fps:        sample video at this many frames/sec — raises timecode accuracy.
      media_resolution: 'high'|'medium'|'low' — per-frame detail override. Default is family-aware
                  (#232: HIGH on 2.5 where it's free, MEDIUM on 3.x where HIGH is a 3.4x premium);
                  force 'high' for OCR/fine text on a 3.x model.
      session_id: RESERVED — server-side sessions are an anti-pattern for MCP (#206); use `history`.
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return json.dumps({"success": False, "error": "GEMINI_API_KEY not set in server env"})
    if not question:
        return json.dumps({"success": False, "error": "question is required"})
    if media_resolution and media_resolution.lower() not in _MEDIA_RESOLUTION:
        return json.dumps({"success": False,
                           "error": f"media_resolution must be high/medium/low, got {media_resolution!r}"})
    raw_inputs = paths if paths else ([path] if path else [])
    if not raw_inputs and not history:
        return json.dumps({"success": False,
                           "error": "give 'path'/'paths' (file or URL), or 'history' to continue a conversation"})
    if len(raw_inputs) > _MAX_INPUTS:
        return json.dumps({"success": False,
                           "error": f"too many inputs ({len(raw_inputs)}); max {_MAX_INPUTS} per request"})
    chosen_model = _model_for("analyze", model)
    eff_tokens = _effective_max_tokens(detail, max_tokens)
    char_limit = _answer_char_limit(detail, answer_chars)
    mr_override = _MEDIA_RESOLUTION.get(media_resolution.lower()) if media_resolution else None
    anchor = None                              # set once a STABLE local file is hashed; None-safe in except
    native = had_url = False
    targets: list = []
    tmpdir = None
    try:
        # routing decisions run INSIDE the try: they call is_url/is_youtube_url over caller-owned
        # inputs/history, which raise on a malformed (non-str / non-dict) item — keep that a
        # structured error, never a crash (codex P2). #229
        native = bool(raw_inputs) and _is_native(raw_inputs, history)   # one-shot YouTube → no download
        had_url = any(fetch.is_url(r) for r in raw_inputs)
        # a URL can also live in a history turn's paths (continuing a YouTube conversation) — needs
        # the temp dir too, even when the current-turn inputs are local/absent.
        hist_has_url = any(fetch.is_url(p) for turn in (history or []) for p in (turn.get("paths") or []))
        if had_url or hist_has_url:            # any URL (current OR history) → a temp dir to download into
            tmpdir = tempfile.mkdtemp(prefix="jaine-media-analyze-")
        if native:
            refs = [_native_ref(raw_inputs[0])]   # the YouTube URL IS the fileUri — no download/upload
            cached_before = None
        else:
            if had_url:                           # download URLs (SSRF-guarded) to the temp dir, then upload
                assert tmpdir is not None         # had_url ⟹ tmpdir was created above
                targets = await _localize(raw_inputs, tmpdir)
            elif raw_inputs:
                targets = _collect_targets(path, paths)   # pure-local: existing validated path
            # hash inputs concurrently (local IO) — uploads below STAY sequential (free-tier burst, grab #6)
            digests = await asyncio.gather(*(gemini_files.compute_file_hash(p) for p in targets))
            # only a STABLE local file anchors the workspace symlink; a downloaded temp is ephemeral (cleaned below)
            anchor = digests[0] if (digests and not had_url) else None
            refs = [await gemini_files.get_or_upload(key, BASE, str(p), _mime_for(p), file_hash=d)
                    for p, d in zip(targets, digests)]
            cached_before = all(r.cached for r in refs) if refs else None   # post ACTIVE-verify (#15)
        resolved_history = await _resolve_history(key, history, tmpdir) if history else None
        gen = await _generate(key, refs, question, model=chosen_model,
                              max_tokens=eff_tokens, fps=fps,
                              language=language, history=resolved_history,
                              media_resolution=mr_override)
        answer, audio_tokens, finish = gen[0], gen[1], gen[2]
        thought_tok, cand_tok = (tuple(gen[3:]) + (0, 0))[:2]   # 0 for a legacy 3-tuple shape
    except Exception as e:
        # Contract: never crash, always return a structured error. Besides the expected
        # RuntimeError (HTTP 404/429/503, upload errors), this also covers OSError (a file
        # deleted between validation and hashing — incl. a vanished history path) and httpx
        # transport errors (timeout/connect on a slow endpoint), neither a RuntimeError (#1/#2/#3).
        tool_log.log_tool("analyze_media", False, digest=anchor, model=chosen_model, error=str(e))
        err = {"success": False, "error": str(e),
               "inputs": list(raw_inputs), "model": chosen_model}   # raw inputs (URLs/paths) the caller gave
        # a bad model 404s — surface what IS available so the next call picks a real one (#202)
        if any(s in str(e).lower() for s in ("404", "not available", "not found")):
            if _model_is_dead(str(e), chosen_model):
                dead_models.record(chosen_model)   # self-healing skip-list — hide it next time (#233)
            try:                                   # recorded FIRST, so it's already absent below
                err["available_models"] = [m["id"] for m in await _list_models(key)]
            except Exception:
                pass
        return json.dumps(err, ensure_ascii=False)
    finally:
        if tmpdir:                                 # #229 download route: a URL was fetched to a temp
            shutil.rmtree(tmpdir, ignore_errors=True)   # dir; Gemini has the fileUri now, drop the temp

    # Persist + frame (Chris: the model thinks/answers freely, but only `char_limit` chars
    # reach Claude Code's context; the FULL text is preserved on disk so nothing is lost).
    # A workspace is made for the source symlink (anchor only, cosmetic) AND/OR to hold an
    # over-long answer. The full answer is written whenever the visible reply overflows —
    # EVEN on a follow-up with no media anchor (keyed on the question hash) — so the marker
    # never promises a full_answer_file we didn't write. Best-effort: this runs AFTER a
    # successful (paid) Gemini call, so a data-fs OSError (full / read-only / perm-denied)
    # must NOT sink the answer or escape to FastMCP — the marker then honestly says it
    # couldn't save (never-crash; unlike extract_frame/prepare_media the dir is non-essential).
    over = len(answer) > char_limit
    workspace_path = None
    full_answer_file = None
    if anchor or over:
        # key on the question AND the conversation (history), so two different follow-up
        # conversations asking the same question text don't overwrite each other's file (#223).
        hist_key = "".join(f"{t.get('role', '')}\x1f{t.get('text', '')}\x1f" for t in (history or []))
        qhash = hashlib.sha256(f"{question}\x1e{hist_key}".encode()).hexdigest()[:8]
        try:
            if anchor:
                ws = await asyncio.to_thread(workspace.prepare, anchor, targets[0])
            else:                                # follow-up: no media to symlink, key on the question
                ws = await asyncio.to_thread(agent_paths.workspace_dir, qhash)
            workspace_path = str(ws)
            if over:                             # preserve the full (pre-frame) answer on disk
                full_file = ws / f"answer-{qhash}.md"
                await asyncio.to_thread(full_file.write_text, answer, encoding="utf-8")
                full_answer_file = str(full_file)
        except Exception:
            pass

    framed, truncated = _frame_answer(answer, char_limit, saved=full_answer_file is not None)
    result = {
        "success": True,
        "model": chosen_model, "detail": detail, "max_tokens": eff_tokens,
        "cached_before": cached_before,     # True only if EVERY input was already uploaded
        # raw AUDIO-modality token count — model-dependent telemetry, NOT a deafness
        # signal (3.x fold audio into VIDEO → 0 even when they hear); see _parse_response
        "audio_tokens": audio_tokens,
        # thinking vs visible-answer token split (shared maxOutputTokens pool) — diagnoses a
        # thinking model that starved its own answer (probe + python-genai #2062)
        "thinking_tokens": thought_tok, "answer_tokens": cand_tok,
        # finish_reason != STOP ⇒ answer is partial/blocked/empty, not a clean success
        "finish_reason": finish, "complete": finish == "STOP",
        "analysis": framed,
    }
    if truncated:
        result["truncated"] = True
    if workspace_path:
        result["workspace"] = workspace_path
    if full_answer_file:
        result["full_answer_file"] = full_answer_file
    # Report what was analyzed HONESTLY (#229 / codex P2): a URL has no stable local file —
    # native has none, a download's temp file is already cleaned — so report the URL as `source`,
    # NEVER a deleted temp path. A LOCAL file keeps `file` so follow-up tools (extract_frame /
    # prepare_media) still work; for a URL you'd use fetch_media when you want the file on disk.
    if native:
        result["native"] = True              # one-shot YouTube passthrough — a fresh analysis, not a follow-up
        result["source"] = raw_inputs[0]
        result["fileUri"] = refs[0].uri      # == the YouTube URL
    elif had_url:
        if len(raw_inputs) == 1:
            result["source"] = raw_inputs[0]
            result["fileUri"] = refs[0].uri
        else:
            result["sources"] = list(raw_inputs)
            result["n_inputs"] = len(raw_inputs)
    elif len(targets) == 1:                   # back-compat single LOCAL-file shape
        result["file"] = str(targets[0])
        result["mime"] = _mime_for(targets[0])
        result["fileUri"] = refs[0].uri
    elif targets:                             # several LOCAL files (#202)
        result["inputs"] = [str(p) for p in targets]
        result["n_inputs"] = len(targets)
    else:
        result["continued"] = True           # follow-up turn — the media was in history
    if history:
        result["history_turns"] = len(history)
    note = _finish_note(finish, thought_tok, cand_tok)
    if note:
        result["note"] = note
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
    # #233: models.list has NO retired-signal (a retired and a working model are byte-identical),
    # so a listed id can still 404 on use. Say so — and learn-from-404 hides ids that already did.
    note = ("catalog presence is not a generateContent guarantee — a listed model can still 404 "
            "(no retired-signal upstream); ids that 404'd before are already hidden (#233)")
    default = _model_for("analyze", None)
    result = {"success": True, "models": models, "note": note, "default": default}
    # codex P2: if the configured default got learned-dead it's filtered out of `models` — don't
    # silently advertise an unavailable default. Flag it (don't substitute — a wrong auto-pick
    # could be worse) so the caller passes an explicit `model` / sets JAINE_MEDIA_MODEL.
    if default not in {m["id"] for m in models}:
        result["default_note"] = (f"configured default '{default}' is NOT in the live catalog "
                                  "(retired/unavailable) — pass an explicit `model` or set "
                                  "JAINE_MEDIA_MODEL to one of `models`; analyze_media would 404 "
                                  "on the default until then (#233)")
    return json.dumps(result, ensure_ascii=False)


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
      - Resolution capped at `max_height` AT DOWNLOAD time, hard ceiling 1080p (no pulling
        4K / hours — >1080p is zero gain and breaks analyze_media timeouts, #230).
      - Size capped at JAINE_MEDIA_MAX_DOWNLOAD_MB (~8GB) AT DOWNLOAD time — a runaway
        direct URL (no format ladder for the resolution cap to bound) aborts before it
        fills the disk; the ceiling sits ABOVE the Gemini fit limit so compress still runs.
      - auto-`prepare` (compress) backstop when the download won't fit Gemini's Files API
        OR escaped the 1080p ceiling (a single-format/direct 4K URL the selector couldn't
        bound), `prepare=False` to skip. On a successful backstop `file` is REPOINTED to the
        safe downscaled copy (raw stays under `raw_file`), so always analyze_media on `file`.
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
        # Stage into a sibling temp in the DEST dir, then atomic os.replace onto `final`. A
        # cross-device/ENOSPC failure on the (copying) move leaves `final` untouched, so a prior
        # valid file is never orphaned (#214.1); os.replace also overwrites any stale leftover (#14).
        staged = final.with_name(final.name + ".incoming")
        await asyncio.to_thread(shutil.move, str(dl), str(staged))   # temp → dest dir, off loop (#12)
        try:
            await asyncio.to_thread(os.replace, str(staged), str(final))  # atomic same-fs rename
        except Exception:
            staged.unlink(missing_ok=True)   # don't orphan the .incoming on a replace failure (#223)
            raise

        verdict = media.fits(final)
        result = {"success": True, "url": url, "file": str(final),
                  "workspace": str(agent_paths.workspace_dir(digest)),
                  "fits": verdict["fits"], "size_mb": verdict["size_mb"]}
        # #230: warn on the ACTUAL downloaded height, not the requested cap. fetch.download caps
        # the format selector at <=1080p, but its /w fallback can still return an uncapped stream
        # for a single-format source (a direct 4K URL with no <=1080p rendition) — so probe the
        # real output (codex P2: a request-based note would falsely claim "capped" on those, or
        # falsely warn when the cap actually worked). >1080p risks analyze_media wait_active/
        # generate timeouts for zero token/quality gain (source pixels aren't in the VIDEO formula).
        try:
            _, out_height = media.probe_dimensions(final)
        except Exception:
            out_height = 0                      # fail-open: can't probe (audio-only / ffprobe err) → don't warn
        too_tall = out_height > fetch._MAX_HEIGHT
        if too_tall:
            result["height_note"] = (
                f"downloaded at {out_height}p — the source had no <=1080p rendition so the cap "
                "could not bound it. >1080p risks analyze_media timeouts for zero token/quality "
                "gain (#230); analyze the downscaled 'prepared' file below.")
        # backstop — downscale when EITHER the file is too big OR escaped the 1080p ceiling. A
        # ~191MB 4K fits by size yet still times analyze_media out, so height must trigger prepare
        # too, not just a warning (codex P2 round 2): otherwise the caller gets a 4K `file` path.
        if (not verdict["fits"] or too_tall) and prepare:
            reason = "exceeds the practical 1080p ceiling" if too_tall else "exceeded the size limit"
            for h in (720, 480, 360, 240):
                cand = agent_paths.workspace_dir(digest) / f"compressed_{h}p.mp4"
                cand_fits = media.fits(cand)["fits"] if await asyncio.to_thread(media.compress, final, cand, height=h) else False
                if cand_fits:
                    cf = media.fits(cand)
                    # `file` is the PRIMARY analyzable path (the docstring says "analyze `file`"),
                    # so after a successful backstop repoint it at the SAFE downscaled copy — the
                    # raw 4K/oversize path stays under `raw_file` (codex P2 r3: leaving `file` on
                    # the 4K original kept the timeout footgun for callers following the primary).
                    result["raw_file"] = result["file"]
                    result["file"] = str(cand)
                    result["fits"], result["size_mb"] = cf["fits"], cf["size_mb"]
                    result["prepared"] = str(cand)               # back-compat alias of `file`
                    result["prepared_mb"] = cf["size_mb"]
                    result["note"] = (f"download {reason} — compressed to {h}p; 'file' now points at "
                                      "the downscaled copy ('raw_file' is the original)")
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
