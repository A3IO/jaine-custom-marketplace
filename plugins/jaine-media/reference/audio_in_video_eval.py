"""SPIKE — the REAL audio question: which models hear the audio track INSIDE a
VIDEO (not a standalone audio file). Some Gemini models return 0 audio tokens for
a .mp4's embedded audio while still hearing a bare .mp3 — that is the deafness the
original AGENTS.md note was about. This drives JAINE_MEDIA_MODEL (analyze hears).

Fixture: reference/video_with_audio.mp4 = the silent timecode video with the
"Привет мир" audio muxed in. A model that hears reports audio_tokens > 0 and
transcribes it; a video-audio-deaf model reports 0 and describes only the visuals.

Enumerates live (no 404-guessing); generous max_tokens so thinking models aren't
starved; sequential + retry/backoff for free-tier limits.
"""
import asyncio
import os
import sys

import httpx

BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY = os.environ["GEMINI_API_KEY"]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
from agent import gemini_files  # noqa: E402

VIDEO_AUDIO = os.path.join(HERE, "video_with_audio.mp4")   # video carrying "Привет мир"
MAX_TOKENS = 2048
_SKIP = ("image", "tts", "embedding", "aqa", "native-audio", "dialog",
         "customtools", "nano", "lyria", "deep-research", "banana", "2.0")


async def discover(client):
    r = await client.get(f"{BASE}/models?pageSize=1000", headers={"x-goog-api-key": KEY})
    out = []
    for m in r.json().get("models", []):
        name = m["name"].split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if any(k in name for k in ("flash", "pro")) and not any(k in name for k in _SKIP):
            out.append(name)
    return out


async def probe(client, model, ref):
    body = {"contents": [{"role": "user", "parts": [
                {"fileData": {"mimeType": ref.mime_type, "fileUri": ref.uri}},
                {"text": "Transcribe any speech you HEAR in this video's audio track. "
                         "If you hear no speech, reply exactly 'NO AUDIO'."}]}],
            "generationConfig": {"maxOutputTokens": MAX_TOKENS, "temperature": 0,
                                 "mediaResolution": "MEDIA_RESOLUTION_HIGH"}}
    url = f"{BASE}/models/{model}:generateContent"
    for attempt in range(5):
        try:
            r = await client.post(url, headers={"x-goog-api-key": KEY}, json=body)
        except Exception as e:
            if attempt == 4:
                return None, f"EXC {type(e).__name__}"
            await asyncio.sleep(3 * (attempt + 1)); continue
        if r.status_code in (429, 503):
            await asyncio.sleep(5 * (attempt + 1)); continue
        if r.status_code // 100 != 2:
            return None, f"HTTP {r.status_code}"
        d = r.json()
        cand = (d.get("candidates") or [{}])[0]
        txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
        audio = sum(int(x.get("tokenCount", 0))
                    for x in d.get("usageMetadata", {}).get("promptTokensDetails", [])
                    if str(x.get("modality")).upper() == "AUDIO")
        return {"audio_tokens": audio, "text": txt[:40]}, None
    return None, "throttled"


async def main():
    async with httpx.AsyncClient(timeout=240) as client:
        models = await discover(client)
        ref = await gemini_files.get_or_upload(KEY, BASE, VIDEO_AUDIO, "video/mp4")
        print(f"VIDEO with embedded audio 'Привет мир' — probing {len(models)} models\n", flush=True)
        hdr = f"{'model':32s} {'hears_video_audio':17s} {'atok':5s} reply"
        print(hdr + "\n" + "-" * 78, flush=True)
        for model in models:
            res, err = await probe(client, model, ref)
            if err:
                print(f"{model:32s} {'ERR ' + err:17s} {'-':5s}", flush=True)
            else:
                verdict = "YES" if res["audio_tokens"] > 0 else "no (0 audio tok)"
                print(f"{model:32s} {verdict:17s} {res['audio_tokens']:<5} {res['text']!r}", flush=True)
            await asyncio.sleep(2)


asyncio.run(main())
