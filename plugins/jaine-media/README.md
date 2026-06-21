# jaine-media

Media understanding for Claude Code via Gemini. Claude can't watch video or hear
audio — this plugin routes media to Gemini (which can do both) and brings the
answer back. Asking again about the same file is cheap (upload cached by content
hash, ~48h).

> **Working on this plugin? Read [`AGENTS.md`](AGENTS.md) FIRST** — it carries every
> hard-won lesson from the spikes so you don't re-step the rakes.

## Status

Five-tool media supertool, all built and tested. `analyze_media` is proven end-to-end
live in Claude Code; `fetch_media`'s real network download is gated behind explicit
approval (logic unit-tested with yt-dlp mocked). Test count:
`cd server && uv run pytest --co -q | tail -1`.

## Tools

| Tool | Does | Status |
|------|------|--------|
| `analyze_media` | Gemini see/hear video/audio file(s), answer a question | ✅ |
| `extract_frame` | timecode → a ±window of PNGs (ffmpeg) for native close-up Read | ✅ |
| `prepare_media` | compress (size) / trim (explicit range) to fit Gemini (ffmpeg) | ✅ |
| `fetch_media` | URL / YouTube → workspace file (yt-dlp, SSRF-guarded, quality-capped) | ✅ |
| `list_models` | live Gemini flash/pro catalog — pick a `model` without guessing names | ✅ |

## analyze_media — beyond a single Q&A

- **Compare clips:** `paths=[a, b, …]` sends several videos in ONE request at full
  resolution each — the answer can point at "in the first clip … but the second …".
- **Continue a conversation:** `history=[{role, text, paths?}]` replays prior turns
  (Gemini multi-turn is stateless/client-side — the caller owns the history). Add a
  different video mid-conversation and compare against earlier ones.
- **Answer frame:** the model thinks freely, but the VISIBLE answer is capped by `detail`
  (brief/normal/full → 2000/8000/32000 chars; override `answer_chars`) so it can't blow
  Claude Code's context. An over-long reply is cut client-side and the full text dropped
  to `workspace/<sha>/answer-<n>.md` (`truncated` + `full_answer_file` in the result).
- **Honest finish:** a truncated / blocked / empty reply surfaces `finish_reason`,
  `complete:false`, `thinking_tokens` / `answer_tokens`, and a `note` — never a silent
  partial answer (a thinking model that starved its own answer is named as such).

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
