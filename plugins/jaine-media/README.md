# jaine-media

Media understanding for Claude Code via Gemini. Claude can't watch video or hear
audio — this plugin routes media to Gemini (which can do both) and brings the
answer back. Asking again about the same file is cheap (upload cached by content
hash, ~48h).

> **Working on this plugin? Read [`AGENTS.md`](AGENTS.md) FIRST** — it carries every
> hard-won lesson from the spikes so you don't re-step the rakes.

## Status

All four media-supertool tools built and tested (61 tests). `analyze_media` is
proven end-to-end live in Claude Code; `fetch_media`'s real network download is
gated behind explicit approval (its logic is unit-tested with yt-dlp mocked).

## Tools

| Tool | Does | Status |
|------|------|--------|
| `analyze_media` | Gemini see/hear a video/audio file, answer a question | ✅ |
| `extract_frame` | timecode → a ±window of PNGs (ffmpeg) for native close-up Read | ✅ |
| `prepare_media` | compress (size) / trim (explicit range) to fit Gemini (ffmpeg) | ✅ |
| `fetch_media` | URL / YouTube → workspace file (yt-dlp, SSRF-guarded, quality-capped) | ✅ |

## Setup

Needs a Gemini API key in the environment:

```bash
export GEMINI_API_KEY="..."   # free-tier key is enough (verified)
```

Frame/prepare/fetch tools also need `ffmpeg` + `yt-dlp` on PATH.

## Development

This is a worktree on branch `jaine-media/main`. Build iteratively with tests +
live checks. **Do not publish** (`./scripts/publish-plugin.sh jaine-media` pushes to
A3IO + triggers CI) until the plugin is ready and explicitly requested.

See `reference/` for spike artifacts (timecode eval, test video, concept demo).

---

*Part of jaine-plugins marketplace*
