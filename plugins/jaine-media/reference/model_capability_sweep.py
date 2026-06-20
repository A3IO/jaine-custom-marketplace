"""Capability sweep over the LIVE Gemini catalog on REAL speech (not synthetic —
Chris: quality shows on real content). For each flash/pro generateContent model:
does it HEAR (transcribe the RU speech), finish_reason, latency, audio_tokens,
context limits. Enumerates via models.list so it self-updates as models ship.
Sequential + retry/backoff (free-tier rate limits). Feeds reference/gemini-models.md.

Run:  cd server && GEMINI_API_KEY=... uv run --no-sync python ../reference/model_capability_sweep.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "server"))
os.environ.setdefault("JAINE_MEDIA_DATA_DIR", str(PLUGIN / ".aitemp/dogfood-data"))

import httpx                                  # noqa: E402
from agent import gemini_files                # noqa: E402

KEY = os.environ["GEMINI_API_KEY"]
BASE = gemini_files.DEFAULT_GEMINI_BASE_URL
CLIP = PLUGIN / ".aitemp/playground-data/uploads/conv.mp4"   # 45s real RU speech
KNOWN_SPEECH = "крид"                          # GT word that must appear if it hears
SKIP = ("image", "tts", "embedding", "aqa", "native-audio", "dialog", "nano",
        "lyria", "banana", "deep-research", "2.0", "1.5", "1.0", "exp-", "-latest", "customtools")


async def catalog(client):
    r = await client.get(f"{BASE}/models?pageSize=1000", headers={"x-goog-api-key": KEY})
    out = []
    for m in r.json().get("models", []):
        n = m["name"].split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if not any(k in n for k in ("flash", "pro")) or any(k in n for k in SKIP):
            continue
        out.append({"id": n, "in": m.get("inputTokenLimit"), "out": m.get("outputTokenLimit"),
                    "preview": "preview" in n})
    return sorted(out, key=lambda x: x["id"])


async def probe(client, model, ref):
    body = {"contents": [{"role": "user", "parts": [
        {"fileData": {"mimeType": ref.mime_type, "fileUri": ref.uri}},
        {"text": "Транскрибируй произнесённую речь дословно на русском, затем опиши видео одной фразой."}]}],
        "generationConfig": {"maxOutputTokens": 2048, "temperature": 0,
                             "mediaResolution": "MEDIA_RESOLUTION_HIGH"}}
    for attempt in range(5):
        t0 = time.monotonic()
        try:
            r = await client.post(f"{BASE}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": KEY}, json=body, timeout=180)
        except Exception as e:
            if attempt == 4:
                return {"err": f"EXC {type(e).__name__}"}
            await asyncio.sleep(5 * (attempt + 1)); continue
        dt = time.monotonic() - t0
        if r.status_code in (429, 503):
            await asyncio.sleep(10 * (attempt + 1)); continue
        d = r.json()
        if r.status_code != 200:
            return {"err": f"{r.status_code} {str(d.get('error', {}).get('message', ''))[:70]}"}
        cands = d.get("candidates") or []
        if cands:
            text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])).strip()
            finish = str(cands[0].get("finishReason") or "STOP").upper()
        else:
            text, finish = "", "BLOCKED:" + str((d.get("promptFeedback") or {}).get("blockReason", "EMPTY"))
        audio = sum(int(x.get("tokenCount", 0)) for x in d.get("usageMetadata", {}).get("promptTokensDetails", [])
                    if str(x.get("modality")).upper() == "AUDIO")
        return {"hears": KNOWN_SPEECH in text.lower(), "finish": finish, "audio_tok": audio,
                "dt": round(dt, 1), "chars": len(text)}
    return {"err": "throttled"}


async def main():
    rows = []
    async with httpx.AsyncClient() as client:
        models = await catalog(client)
        print(f"catalog: {len(models)} flash/pro generateContent models\n", flush=True)
        ref = await gemini_files.get_or_upload(KEY, BASE, str(CLIP), "video/mp4")
        hdr = f"{'model':34} {'stable':7} {'hears':6} {'finish':12} {'atok':5} {'lat':5} {'ctx':>9}"
        print(hdr + "\n" + "-" * len(hdr), flush=True)
        for m in models:
            res = await probe(client, m["id"], ref)
            await asyncio.sleep(2)
            row = {**m, **res}
            rows.append(row)
            if "err" in res:
                print(f"{m['id']:34} {'stable' if not m['preview'] else 'preview':7} ERR {res['err']}", flush=True)
            else:
                print(f"{m['id']:34} {'stable' if not m['preview'] else 'preview':7} "
                      f"{('YES' if res['hears'] else 'no'):6} {res['finish']:12} "
                      f"{res['audio_tok']:<5} {res['dt']:<5} {str(m['in']):>9}", flush=True)
    (PLUGIN / ".aitemp/model_sweep_result.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\nsaved → .aitemp/model_sweep_result.json", flush=True)


asyncio.run(main())
