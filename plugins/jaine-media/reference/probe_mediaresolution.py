"""Empirically settle MEDIA_RESOLUTION token cost (docs disagree: 70 vs 258 for
2.5 default). Reuses the plugin's real upload+cache. Reads the REAL
promptTokenCount + per-modality breakdown from usageMetadata — token counts are
deterministic, latency is the noisy free-tier median of 3 runs.

Run:  cd server && GEMINI_API_KEY=... uv run --no-sync python ../.aitemp/probe_mediaresolution.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "server"))
os.environ.setdefault("JAINE_MEDIA_DATA_DIR", str(PLUGIN / ".aitemp/playground-data"))

import httpx                                  # noqa: E402
from agent import gemini_files                # noqa: E402

KEY = os.environ.get("GEMINI_API_KEY", "")
BASE = gemini_files.DEFAULT_GEMINI_BASE_URL
QUESTION = "Describe what happens in this video in one sentence."

_ALL_FILES = [
    ("conv.mp4 (45s real)", PLUGIN / ".aitemp/playground-data/uploads/conv.mp4", "video/mp4"),
    ("video_with_audio (24s synth)", PLUGIN / "reference/video_with_audio.mp4", "video/mp4"),
]
# env overrides: JAINE_PROBE_MODELS=comma,sep   JAINE_PROBE_ONEFILE=1 (just conv.mp4)
FILES = _ALL_FILES[:1] if os.environ.get("JAINE_PROBE_ONEFILE") else _ALL_FILES
MODELS = os.environ.get("JAINE_PROBE_MODELS", "gemini-2.5-flash").split(",")
RES = [None, "MEDIA_RESOLUTION_LOW", "MEDIA_RESOLUTION_MEDIUM", "MEDIA_RESOLUTION_HIGH"]


async def gen(client, model, ref, res):
    part = {"fileData": {"mimeType": ref.mime_type, "fileUri": ref.uri}}
    gconf = {"maxOutputTokens": 512, "temperature": 0}
    if res:
        gconf["mediaResolution"] = res
    body = {"contents": [{"role": "user", "parts": [part, {"text": QUESTION}]}],
            "generationConfig": gconf}
    for attempt in range(4):                  # retry 429/503 free-tier bursts
        t0 = time.monotonic()
        r = await client.post(f"{BASE}/models/{model}:generateContent",
                              headers={"x-goog-api-key": KEY}, json=body, timeout=180)
        dt = time.monotonic() - t0
        if r.status_code in (429, 503):
            await asyncio.sleep(8 * (attempt + 1))
            continue
        d = r.json()
        if r.status_code != 200:
            return {"err": f"{r.status_code} {str(d.get('error', {}).get('message', ''))[:90]}", "dt": dt}
        um = d.get("usageMetadata", {})
        details = {x.get("modality"): x.get("tokenCount") for x in um.get("promptTokensDetails", [])}
        return {"prompt": um.get("promptTokenCount"), "total": um.get("totalTokenCount"),
                "details": details, "dt": dt}
    return {"err": "rate-limited after retries", "dt": 0}


async def main():
    if not KEY:
        print("GEMINI_API_KEY missing"); return
    async with httpx.AsyncClient() as client:
        for label, path, mime in FILES:
            if not path.exists():
                print(f"SKIP {label} (no file)"); continue
            ref = await gemini_files.get_or_upload(KEY, BASE, str(path), mime)
            print(f"\n### {label}  →  {ref.uri.split('/')[-1]}")
            print(f"  {'model':20} {'res':8} {'prompt':>7} {'VIDEO':>6} {'AUDIO':>6} {'TEXT':>5}  lat_median (runs)")
            for model in MODELS:
                for res in RES:
                    runs = []
                    for i in range(3):
                        runs.append(await gen(client, model, ref, res))
                        await asyncio.sleep(2)          # throttle free-tier
                    first = runs[0]
                    rname = res.replace("MEDIA_RESOLUTION_", "") if res else "default"
                    if "err" in first:
                        print(f"  {model:20} {rname:8} ERR {first['err']}")
                        continue
                    lats = sorted(round(r["dt"], 1) for r in runs if "dt" in r and "err" not in r)
                    med = lats[len(lats) // 2] if lats else None
                    de = first["details"]
                    print(f"  {model:20} {rname:8} {first['prompt']:>7} "
                          f"{de.get('VIDEO', '-'):>6} {de.get('AUDIO', '-'):>6} {de.get('TEXT', '-'):>5}  "
                          f"{med}s  {lats}")


asyncio.run(main())
