# Native YouTube URL ingestion in analyze_media (#229)

**Date:** 2026-06-22 · **Issue:** A3IO/jaine-plugins#229 · **Status:** approved (Chris)

## Problem

To analyze a YouTube video today, the caller must first run `fetch_media`, which
ALWAYS downloads (yt-dlp) then uploads to the Files API. But Gemini accepts a public
YouTube URL **natively** (`fileData.fileUri = <youtube-url>`) — no download, no upload.

Measured A/B (`gemini-2.5-flash`, same clip/prompt): native and uploaded give
**identical** VIDEO tokens (162008) and equal/richer quality. Tradeoff:
- **native** — no download/upload on our side (faster end-to-end on a cold one-shot), but
  Gemini re-pulls the clip server-side **every request** (~+18s/call) and there is **no
  fileUri reuse**.
- **download+upload** — slower upfront, but the fileUri is cached ~48h, so repeated/
  multi-turn questions are cheap and fast.

So: **one-shot → native wins; many questions about the same video → download wins.**

## Decision (Chris: "make it so I don't have to think about it")

`analyze_media` accepts a URL (in the existing `path`/`paths`) and **auto-routes**. No new
required params, no cognition for the caller — it picks the cheap/fast path itself.

### Routing rules

| Input | Route | Why |
|---|---|---|
| YouTube URL, **single**, no `history` (one-shot) | **native passthrough** (fileData.fileUri = URL, no download) | one-shot — native is faster end-to-end, no upload |
| YouTube URL **with `history`** (multi-turn) | **download + upload** (reuse) | multi-turn re-pulls every turn natively → download+reuse is cheaper/faster |
| non-YouTube http(s) URL | **download + upload** | native is YouTube-only; download path Just Works (+ SSRF guard) |
| multiple inputs (`paths`) where any is a URL | **download + upload** all | native supports max **1** YouTube link per request |
| local path | unchanged (upload as today) | status quo |

A URL is detected by scheme `http`/`https` in a `path`/`paths` entry. YouTube is detected
by host (`youtube.com`, `youtu.be`, `m.youtube.com`, `youtube.com/shorts`).

### Surface

- No new public params. `analyze_media(path="https://youtu.be/...", question=...)` works.
- `fetch_media` stays a separate explicit tool (for when you WANT the file on disk:
  prepare/compress/extract_frame). analyze's internal download reuses `fetch.download` +
  `gemini_files.get_or_upload` — same code, no duplication.

### Safety

- **native** hands the URL to Gemini; Google fetches it server-side — no SSRF surface on
  our side, and native is YouTube-host-only anyway.
- **download** route keeps the existing `fetch.validate_url` SSRF guard (http(s) only,
  host not private/loopback/link-local). Unchanged.

## Scope / non-goals

- `fps` already rides `videoMetadata.fps` and works natively — no change.
- **No trim/start-end in analyze** (that's `prepare_media`; YAGNI here).
- No private-YouTube detection: a private/blocked URL → Gemini returns an error → our
  existing structured-error path surfaces it (with `available`-style guidance). Acceptable.
- No content-hash cache for native (there's no local file to hash) — by design.

## Testing

Strict TDD, network mocked:
- URL detection helpers (`is_url`, `is_youtube_url`) — hosts in/out.
- Routing: one-shot YouTube → native part built (fileData.fileUri == URL, no upload call);
  YouTube+history → download+upload path taken; non-YouTube URL → download; multi-URL →
  download; local path → unchanged.
- `_media_part` builds a valid native YouTube part (fileUri = URL).
- Structured-error contract preserved on a failed native call (no crash).
- Live smoke (one real public YouTube one-shot) only with Chris's ok (outward call).
