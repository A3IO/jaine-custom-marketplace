"""Empirically answer: in Gemini generateContent, do THINKING tokens come out of
``maxOutputTokens`` (shared pool) or a SEPARATE budget? And does ``thinkingBudget``
actually cap thinking, or do models ignore it (python-genai #782/#1795)?

Chris's intent: the cap should protect the VISIBLE ANSWER size (so a big reply can't
blow Claude Code's context) while the model thinks freely. This probe checks whether
that's even achievable via the API, by dumping usageMetadata
(thoughtsTokenCount vs candidatesTokenCount) across a model × thinkingBudget matrix.

Text-only logic puzzle (no upload — thinking shows on text too; cheap/fast).
Sequential + backoff (free-tier). Re-runnable; catalog drifts, so do these limits.

Run:  cd server && GEMINI_API_KEY=... uv run --no-sync python ../reference/probe_thinking_budget.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN / "server"))

import httpx                                  # noqa: E402
from agent import gemini_files                # noqa: E402

KEY = os.environ["GEMINI_API_KEY"]
BASE = gemini_files.DEFAULT_GEMINI_BASE_URL

# A puzzle that reliably triggers reasoning; the correct answer is tiny ("$0.05"),
# so a small maxOutputTokens + thinking = the empty-response bug if the pool is shared.
PROMPT = ("Reason carefully step by step, then give ONLY the final answer. "
          "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
          "How much does the ball cost?")

# flash 2.5 = can disable thinking; 3.1-pro = thinking-heavy, cannot fully disable.
MODELS = ["gemini-2.5-flash", "gemini-3.1-pro-preview"]

# (label, maxOutputTokens, thinkingBudget or None=omit thinkingConfig)
# KEY question (Chris): can the model think freely while the VISIBLE answer stays
# inside a small cap? Test dynamic thinking (-1) against a tiny maxOutputTokens —
# if thinking is free AND a visible answer still comes back, the pools are separable.
CONFIGS = [
    ("maxout120 / no-cfg",      120,  None),
    ("maxout120 / budget0",     120,  0),
    ("maxout120 / dynamic-1",   120,  -1),     # free thinking + tiny visible cap
    ("maxout512 / dynamic-1",   512,  -1),     # free thinking + small cap
    ("maxout2048 / budget128",  2048, 128),
    ("maxout2048 / dynamic-1",  2048, -1),
]


async def probe(client, model, max_out, budget):
    gen = {"maxOutputTokens": max_out, "temperature": 0}
    if budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": budget}
    body = {"contents": [{"role": "user", "parts": [{"text": PROMPT}]}], "generationConfig": gen}
    for attempt in range(5):
        try:
            r = await client.post(f"{BASE}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": KEY}, json=body, timeout=180)
        except Exception as e:
            if attempt == 4:
                return {"err": f"EXC {type(e).__name__}"}
            await asyncio.sleep(5 * (attempt + 1)); continue
        if r.status_code in (429, 503):
            await asyncio.sleep(10 * (attempt + 1)); continue
        d = r.json()
        if r.status_code != 200:
            return {"err": f"{r.status_code} {str(d.get('error', {}).get('message', ''))[:80]}"}
        cands = d.get("candidates") or []
        if cands:
            text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])).strip()
            finish = str(cands[0].get("finishReason") or "STOP").upper()
        else:
            text, finish = "", "BLOCKED:" + str((d.get("promptFeedback") or {}).get("blockReason", "EMPTY"))
        u = d.get("usageMetadata", {})
        return {"finish": finish, "answer_chars": len(text), "has_answer": "0.05" in text or ".05" in text,
                "prompt_tok": u.get("promptTokenCount"), "cand_tok": u.get("candidatesTokenCount"),
                "thought_tok": u.get("thoughtsTokenCount"), "total_tok": u.get("totalTokenCount")}
    return {"err": "throttled"}


async def main():
    rows = []
    hdr = f"{'model':26} {'config':22} {'finish':14} {'ans?':5} {'thought':8} {'cand':6} {'total':6}"
    print(hdr + "\n" + "-" * len(hdr), flush=True)
    async with httpx.AsyncClient() as client:
        for model in MODELS:
            for label, max_out, budget in CONFIGS:
                res = await probe(client, model, max_out, budget)
                await asyncio.sleep(2)
                rows.append({"model": model, "config": label, "maxOut": max_out, "budget": budget, **res})
                if "err" in res:
                    print(f"{model:26} {label:22} ERR {res['err']}", flush=True)
                else:
                    print(f"{model:26} {label:22} {res['finish']:14} "
                          f"{('YES' if res['has_answer'] else 'no'):5} "
                          f"{str(res['thought_tok']):8} {str(res['cand_tok']):6} {str(res['total_tok']):6}",
                          flush=True)
    (PLUGIN / ".aitemp/thinking_budget_probe.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    print(f"\nsaved → .aitemp/thinking_budget_probe.json", flush=True)


asyncio.run(main())
