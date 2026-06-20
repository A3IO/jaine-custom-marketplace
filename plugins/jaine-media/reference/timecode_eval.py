# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27,<1"]
# ///
"""SPIKE — timecode accuracy eval. Throwaway.

Question: which model + fps-sampling most accurately localizes WHEN an on-screen
event happens? Drives the extract_frame design (if Gemini can't pin the second,
the whole frame-pipeline is shaky).

Synthetic video: black background, color flashes at known seconds (ground truth).
For each (model x fps): ask "at what second does the screen flash <color>?",
parse the number, compare to ground truth. Programmatic scoring (±tolerance).
"""
import asyncio
import json
import os
import re
import sys

import httpx
from agent import gemini_files

BASE = gemini_files.DEFAULT_GEMINI_BASE_URL
HERE = os.path.dirname(__file__)
VIDEO = os.path.join(HERE, "timecode_test.mp4")
GT = json.load(open(os.path.join(HERE, "timecode_gt.json")))
TOL = 1.0  # seconds — a hit if |pred - truth| <= TOL

CONFIGS = [
    ("gemini-2.5-flash", None),
    ("gemini-2.5-flash", 5),
    ("gemini-3-flash-preview", None),
    ("gemini-3-flash-preview", 5),
]


def parse_secs(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)", text or "")
    return float(m.group(1)) if m else None


async def ask(client, key, ref, color, model, fps):
    part = {"fileData": {"mimeType": "video/mp4", "fileUri": ref.uri}}
    if fps:
        part["videoMetadata"] = {"fps": fps}
    body = {
        "contents": [{"role": "user", "parts": [
            part,
            {"text": f"At what time, in seconds from the start, does the screen flash {color}? "
                     f"Answer with ONLY a number (e.g. 7.0)."},
        ]}],
        "generationConfig": {"maxOutputTokens": 200, "temperature": 0,
                             "mediaResolution": "MEDIA_RESOLUTION_HIGH"},
    }
    url = f"{BASE}/models/{model}:generateContent"
    # retry with backoff on transient throttle (429/503) — clean signal on free key
    for attempt in range(4):
        try:
            r = await client.post(url, headers={"x-goog-api-key": key,
                                                "Content-Type": "application/json"}, json=body)
            if r.status_code in (429, 503):
                await asyncio.sleep(2 * (attempt + 1) ** 2)  # 2,8,18s
                continue
            if r.status_code // 100 != 2:
                return color, None, f"HTTP {r.status_code}"
            d = r.json()
            cand = (d.get("candidates") or [{}])[0]
            txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
            return color, parse_secs(txt), txt.strip()
        except Exception as e:
            if attempt == 3:
                return color, None, f"ERR {type(e).__name__}"
            await asyncio.sleep(2 * (attempt + 1))
    return color, None, "throttled (429/503 x4)"


async def run_config(client, key, ref, model, fps):
    # sequential per-color (no burst) to stay under free-tier rate limits
    res = []
    for c in GT:
        res.append(await ask(client, key, ref, c, model, fps))
        await asyncio.sleep(1.0)
    hits, errs = 0, []
    rows = []
    for color, pred, raw in res:
        truth = GT[color]
        if pred is not None:
            err = abs(pred - truth)
            errs.append(err)
            hit = err <= TOL
            hits += hit
            rows.append(f"{color:>7}: pred={pred:5.1f} truth={truth:4.1f} err={err:4.1f} {'✓' if hit else '✗'}")
        else:
            rows.append(f"{color:>7}: pred=  ?   truth={truth:4.1f}  ({raw})")
    mae = sum(errs) / len(errs) if errs else float("nan")
    return hits, len(GT), mae, rows


async def main():
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        print("FATAL: GEMINI_API_KEY not set"); return
    print(f"\n{'='*60}\n TIMECODE ACCURACY EVAL  (tol=±{TOL}s, GT={GT})\n{'='*60}")
    ref = await gemini_files.get_or_upload(key, BASE, VIDEO, "video/mp4")
    print(f"uploaded once: {ref.uri}\n")
    async with httpx.AsyncClient(timeout=120) as client:
        for model, fps in CONFIGS:
            label = f"{model}  fps={fps or 'default'}"
            try:
                hits, total, mae, rows = await run_config(client, key, ref, model, fps)
                print(f"── {label}")
                for r in rows:
                    print(f"     {r}")
                print(f"     → accuracy {hits}/{total}  mean_abs_err={mae:.2f}s\n")
            except Exception as e:
                print(f"── {label}\n     SKIP/FAIL: {type(e).__name__}: {e}\n")


asyncio.run(main())
