---
name: media
description: Understands the contents of video and audio — transcribes, describes, locates moments, extracts frames, compares clips, analyzes YouTube/web URLs. ALWAYS invoke when a request is about media a text model cannot natively inspect; do NOT answer from a filename or metadata and do NOT write your own ffmpeg — route to the jaine-media MCP tools (if they are unavailable, explain how to enable the plugin). Triggers on "what's in this video", "transcribe this audio", "extract a frame at 0:42", .mp4/.mov/.mp3.
allowed-tools: [Read]
---

# Media understanding

A text model cannot natively inspect the contents of video or audio. These tools can —
do NOT answer media-content questions from a filename, metadata, or the user's
description, and do NOT write your own ffmpeg/python. Delegate to the tools below.

## Tools (call them directly when the server is connected)

- `mcp__plugin_jaine-media_jaine-media__analyze_media` — see/hear a clip and answer.
  Accepts a local file OR a URL (`path=`), several clips at once (`paths=[...]`), and a
  running conversation (`history=[...]`). A YouTube/web URL is passed straight in — it
  auto-routes (native YouTube one-shot vs download+upload). Tune `detail`
  (brief|normal|full), `model`, `language`, `fps`.
- `mcp__plugin_jaine-media_jaine-media__extract_frame` — pull the exact image frame(s) at a
  timecode (±`window`) when you need to SEE a moment, not just read about it.
- `mcp__plugin_jaine-media_jaine-media__fetch_media` — download a URL/YouTube to a local
  workspace file (SSRF-guarded, quality-capped) when you want a local copy first.
- `mcp__plugin_jaine-media_jaine-media__prepare_media` — compress or trim a file that is too
  large/long to process.
- `mcp__plugin_jaine-media_jaine-media__list_models` — live catalog of usable models; use it
  to pick a `model` or after a model error.

## Typical chain

`fetch_media` (if remote and you want a local copy) → `prepare_media` (if oversized) →
`analyze_media` → `extract_frame` (for exact moments). For most "what's in this video/audio"
questions, just call `analyze_media` directly — it accepts files and URLs and fetches
internally.

## If the tools are not available

If the `mcp__plugin_jaine-media_jaine-media__*` tools are absent, the jaine-media MCP server
is not connected. Do NOT fall back to writing ffmpeg/python. Tell the user how to enable it:

1. Ask the user to run `claude mcp list` to check whether `jaine-media` is connected.
2. If the plugin is installed but the server is down, run `/reload-plugins` (or restart the
   session); approve the server if its connection is pending.
3. Ensure the backend API key the plugin requires is set in the environment (the plugin's
   `.mcp.json` lists the exact variable).

Once it reconnects, retry the request.
