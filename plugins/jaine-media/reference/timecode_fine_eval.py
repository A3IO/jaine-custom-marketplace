"""SPIKE — FINE timecode accuracy, to pick JAINE_MEDIA_LOCATE_MODEL for extract_frame.

The coarse matrix (model_matrix_eval.py) can't differentiate: every model answers
in integer seconds, so against X.5 ground truth they all score MAE 0.50 / 4-of-4
at ±1s. extract_frame needs sub-second precision, so here we ASK for one decimal
place, tighten to report raw MAE + worst error, and test default fps vs fps=5
(the accuracy lever). max_tokens is generous so "thinking" models aren't starved.
"""
import asyncio
import os
import re
import sys

import httpx

BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY = os.environ["GEMINI_API_KEY"]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))
from agent import gemini_files  # noqa: E402

VIDEO = os.path.join(HERE, "timecode_test.mp4")
GT = {"red": 3.5, "cyan": 8.5, "green": 14.5, "yellow": 19.5}
MAX_TOKENS = 2048
LOCATE = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-flash-preview",
          "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-3.5-flash",
          "gemini-flash-latest", "gemini-flash-lite-latest"]


async def ask(client, model, vref, color, fps):
    part = {"fileData": {"mimeType": vref.mime_type, "fileUri": vref.uri}}
    if fps:
        part["videoMetadata"] = {"fps": fps}
    body = {"contents": [{"role": "user", "parts": [part, {
                "text": f"At what time, in seconds from the start, does the screen flash "
                        f"{color}? Estimate to one decimal place (e.g. 7.3). Answer with ONLY a number."}]}],
            "generationConfig": {"maxOutputTokens": MAX_TOKENS, "temperature": 0,
                                 "mediaResolution": "MEDIA_RESOLUTION_HIGH"}}
    for attempt in range(5):
        try:
            r = await client.post(f"{BASE}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": KEY}, json=body)
        except Exception:
            await asyncio.sleep(3 * (attempt + 1)); continue
        if r.status_code in (429, 503):
            await asyncio.sleep(5 * (attempt + 1)); continue
        if r.status_code // 100 != 2:
            return None
        cand = (r.json().get("candidates") or [{}])[0]
        txt = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
        m = re.search(r"(\d+(?:\.\d+)?)", txt or "")
        return float(m.group(1)) if m else None
    return None


async def run(client, model, vref, fps):
    errs, preds = [], []
    for color, truth in GT.items():
        p = await ask(client, model, vref, color, fps)
        preds.append(p)
        if p is not None:
            errs.append(abs(p - truth))
        await asyncio.sleep(1.2)
    mae = round(sum(errs) / len(errs), 2) if errs else None
    worst = round(max(errs), 2) if errs else None
    return mae, worst, preds


async def main():
    async with httpx.AsyncClient(timeout=240) as client:
        vref = await gemini_files.get_or_upload(KEY, BASE, VIDEO, "video/mp4")
        print(f"GT (seconds): {GT}\n", flush=True)
        hdr = f"{'model':32s} {'fps':4s} {'MAE':5s} {'worst':5s}  preds"
        print(hdr + "\n" + "-" * 76, flush=True)
        for model in LOCATE:
            for fps in (None, 5):
                mae, worst, preds = await run(client, model, vref, fps)
                ps = " ".join(f"{p}" if p is not None else "?" for p in preds)
                print(f"{model:32s} {str(fps or 'def'):4s} "
                      f"{str(mae):5s} {str(worst):5s}  [{ps}]  (gt 3.5/8.5/14.5/19.5)", flush=True)
                await asyncio.sleep(1.5)


asyncio.run(main())
