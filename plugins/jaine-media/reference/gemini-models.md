# Gemini models for jaine-media — live catalog + capability sweep

> Authoritative list of Gemini flash/pro `generateContent` models, **swept live on
> REAL RU speech** (conv.mp4, 45s) — `reference/model_capability_sweep.py`, **2026-06-20**.
> Re-run the sweep when models ship/retire; `models.list` self-updates the catalog.
> "Hears" = transcribed the known speech word verbatim. finish@2048 = finish_reason
> at the sweep's maxOutputTokens=2048.

## Catalog (live `models.list`, flash/pro, generateContent)

| model | status | hears | finish@2048 | audio_tok | latency | context |
|-------|--------|-------|-------------|-----------|---------|---------|
| **gemini-2.5-flash** | stable ✅ default | YES | STOP | 1440 | 13.3s | 1M / 64k |
| gemini-2.5-flash-lite | stable | YES | STOP | 1440 | **6.6s** ⚡ | 1M / 64k |
| gemini-2.5-pro | stable | YES | STOP | 1440 | 15.1s | 1M / 64k |
| gemini-3-flash-preview | preview | YES | STOP | 0 | 8.9s | 1M / 64k |
| gemini-3.1-flash-lite | stable | YES | **MAX_TOKENS** | 0 | 8.8s | 1M / 64k |
| gemini-3.1-flash-lite-preview | preview | YES | **MAX_TOKENS** | 0 | 9.3s | 1M / 64k |
| gemini-3.1-pro-preview | preview | YES | **MAX_TOKENS** | 0 | 17.9s | 1M / 64k |
| gemini-3.5-flash | stable | YES | STOP | 0 | 13.1s | 1M / 64k |
| gemini-3-pro-preview | preview (вернулся 2026-06-21) | ? | ? | — | — | снова в `models.list`; sweep НЕ перезапускался — capability не замерен |

(`gemini-3.1-pro-preview-customtools` excluded — tool-calling variant, not for media.
`gemini-flash-latest`/`-lite-latest`/`-pro-latest` are moving aliases — pin a real id.)

## What the sweep settles

- **No deaf models.** All 8 working models transcribed the speech (incl. video-embedded
  audio). The old "only 2.5-flash hears" was an `audio_tokens` accounting artifact:
  2.5-family itemizes an AUDIO modality (1440 tok); 3.x fold audio into VIDEO (`audio_tokens=0`
  yet they transcribe). **`audio_tokens=0` is NOT deafness.**
- **`gemini-3.5-pro` and `gemini-2.0-*` DO NOT EXIST / are retired** — don't probe them
  (sister was guessing these; they 404). NB `gemini-3-pro-preview` was 404 on 2026-06-19 but
  **is listed again as of 2026-06-21** (live `list_models`) — the catalog drifts; trust
  `list_models`, not this snapshot.
- **3.1-family (flash-lite, pro) is thinking-heavy** → hit MAX_TOKENS at 2048 on a
  transcribe+describe task (reasoning ate the budget). Give them ≥4096–8192 (detail='full')
  or they truncate. 2.5-family + 3-flash-preview + 3.5-flash finished clean (STOP) at 2048.
- **Speed (this task, HIGH res): gemini-2.5-flash-lite fastest (6.6s)**, 3-flash-preview 8.9s,
  the rest 13–18s. NB latency is task-dependent (a thinking model is slower on long output
  but cheaper on prefill — see media-resolution-tokens.md).

## Default + candidates

- **Default `gemini-2.5-flash`** (DEFAULT_MODELS in server.py): stable, STOP at 2048,
  meaningful `audio_tokens`, clean output without a thinking preamble.
- **Speed candidate `gemini-2.5-flash-lite`** — 2× faster (6.6s), stable, hears. Good for
  bulk/cheap passes.
- **Quality/scene candidate `gemini-3.5-flash`** — stable, ~3× cheaper video tokens
  (media-resolution-tokens.md), strong scene reasoning — BUT can overthink (the dogfood
  "360° panorama" hallucination on an ffmpeg-hstack clip; native multi-video `paths=[...]`
  avoids the seam that confused it). Needs a real bake-off before becoming default.
- **Override per call:** `analyze_media(..., model="gemini-3.5-flash")`; or env
  `JAINE_MEDIA_MODEL` / `JAINE_MEDIA_LOCATE_MODEL`.

## Reproduce / re-sweep

```bash
cd server && GEMINI_API_KEY=... uv run --no-sync python ../reference/model_capability_sweep.py
```
Enumerates the live catalog (no hardcoded model names → survives ships/retires) and
re-checks hears/finish/latency on real speech. Run on any model change.
