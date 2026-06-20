"""SPIKE — full Gemini model capability matrix.

Two questions per model, both decision-relevant for jaine-media's defaults:
  1) Does it HEAR audio?   (audio_tokens > 0 + transcribes "Привет мир")
  2) How accurately does it LOCALIZE timecodes?  (color flashes at known seconds)

Enumerates live via models.list (no 404-guessing), so it re-runs as new models
ship. Sequential + retry/backoff to respect free-tier limits (AGENTS.md grab #6).
Drives JAINE_MEDIA_MODEL (audio/analyze) and JAINE_MEDIA_LOCATE_MODEL (timecode).

Run:  GEMINI_API_KEY=... JAINE_MEDIA_DATA_DIR=/.../.aitemp/e2e-data \
        .venv/bin/python reference/model_matrix_eval.py
"""
import asyncio
import json
import os
import re
import sys

import httpx

BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY = os.environ["GEMINI_API_KEY"]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
from agent import gemini_files  # noqa: E402  — reuse upload + on-disk content cache

VIDEO = os.path.join(HERE, "timecode_test.mp4")
AUDIO = os.path.join(HERE, "audio_test.mp3")
GT = {"red": 3.5, "cyan": 8.5, "green": 14.5, "yellow": 19.5}   # flash seconds
TOL = 1.0
RESULT = os.path.join(HERE, "..", ".aitemp", "model_matrix_result.json")

# multimodal flash/pro families that take media; skip image/tts/music/embedding/tool variants
# and the gemini-2.0-* family (models.list still lists them but generateContent 404s —
# "no longer available"). max_tokens must be generous: "thinking" models (2.5-pro,
# 3.x-preview, 3.5-flash) spend output budget on reasoning and return EMPTY text if starved.
_SKIP = ("image", "tts", "embedding", "aqa", "native-audio", "dialog",
         "customtools", "nano", "lyria", "deep-research", "banana", "2.0")
_MAX_TOKENS = 2048


async def discover(client):
    r = await client.get(f"{BASE}/models?pageSize=1000", headers={"x-goog-api-key": KEY})
    out = []
    for m in r.json().get("models", []):
        name = m["name"].split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if not any(k in name for k in ("flash", "pro")):
            continue
        if any(k in name for k in _SKIP):
            continue
        out.append(name)
    return out


async def gen(client, model, parts, max_tokens):
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0,
                                 "mediaResolution": "MEDIA_RESOLUTION_HIGH"}}
    url = f"{BASE}/models/{model}:generateContent"
    for attempt in range(5):
        try:
            r = await client.post(url, headers={"x-goog-api-key": KEY,
                                                "Content-Type": "application/json"}, json=body)
        except Exception as e:
            if attempt == 4:
                return None, 0, f"EXC {type(e).__name__}"
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if r.status_code in (429, 503):
            await asyncio.sleep(5 * (attempt + 1))   # 5,10,15,20
            continue
        if r.status_code // 100 != 2:
            return None, 0, f"HTTP {r.status_code}"
        d = r.json()
        cand = (d.get("candidates") or [{}])[0]
        txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
        audio = sum(int(x.get("tokenCount", 0))
                    for x in d.get("usageMetadata", {}).get("promptTokensDetails", [])
                    if str(x.get("modality")).upper() == "AUDIO")
        return txt, audio, None
    return None, 0, "throttled"


async def audio_probe(client, model, aref):
    txt, audio, err = await gen(client, model, [
        {"fileData": {"mimeType": aref.mime_type, "fileUri": aref.uri}},
        {"text": "Transcribe the speech. Answer with only the transcription."}], _MAX_TOKENS)
    return {"audio_tokens": audio, "hears": audio > 0, "transcript": (txt or "")[:30], "audio_err": err}


async def timecode_probe(client, model, vref):
    hits, errs = 0, []
    for color, truth in GT.items():
        txt, _, err = await gen(client, model, [
            {"fileData": {"mimeType": vref.mime_type, "fileUri": vref.uri}},
            {"text": f"At what time in seconds from the start does the screen flash "
                     f"{color}? Answer with ONLY a number."}], _MAX_TOKENS)
        m = re.search(r"(\d+(?:\.\d+)?)", txt or "")
        if m:
            e = abs(float(m.group(1)) - truth)
            errs.append(e)
            hits += e <= TOL
        await asyncio.sleep(1.2)
    return {"tc_hits": hits, "tc_answered": len(errs),
            "tc_mae": round(sum(errs) / len(errs), 2) if errs else None}


async def main():
    async with httpx.AsyncClient(timeout=240) as client:
        models = await discover(client)
        print(f"probing {len(models)} models\n", flush=True)
        aref = await gemini_files.get_or_upload(KEY, BASE, AUDIO, "audio/mp3")
        vref = await gemini_files.get_or_upload(KEY, BASE, VIDEO, "video/mp4")

        hdr = f"{'model':34s} {'hears':5s} {'atok':5s} {'transcript':16s} {'tc':5s} {'mae':5s}"
        print(hdr + "\n" + "-" * len(hdr), flush=True)
        rows = []
        for model in models:
            a = await audio_probe(client, model, aref)
            await asyncio.sleep(1.5)
            t = await timecode_probe(client, model, vref)
            await asyncio.sleep(1.5)
            row = {"model": model, **a, **t}
            rows.append(row)
            hears = a["audio_err"] or ("YES" if a["hears"] else "no")
            mae = f"{t['tc_mae']:.2f}" if t["tc_mae"] is not None else "-"
            print(f"{model:34s} {hears:9s} {a['audio_tokens']:<5} {a['transcript']:16s} "
                  f"{str(t['tc_hits']) + '/4':5s} {mae:5s}", flush=True)

        os.makedirs(os.path.dirname(RESULT), exist_ok=True)
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"\nsaved → {RESULT}", flush=True)


asyncio.run(main())
