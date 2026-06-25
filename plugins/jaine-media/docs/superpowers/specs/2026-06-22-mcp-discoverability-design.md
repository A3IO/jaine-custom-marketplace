# jaine-media #228 — discoverability: MCP `instructions=` + a workflow-glue skill

**Date:** 2026-06-22 · **Issue:** A3IO/jaine-plugins#228 (rescoped) · **Status:** design approved, pre-TDD

## Problem

jaine-media is MCP-only. When its MCP server is **not connected** (plugin disabled,
process crashed, MCP approval pending on first install, deps failed to install — our
own #217), the tools are absent from the model's registry. The model is **blind to the
capability**: asked about a video it answers "I can't see video" or — worse — writes its
own `ffmpeg`/`python` instead of routing to the tools. Editing the user's
`~/.claude/CLAUDE.md` would patch this locally but is useless to a community install —
**discoverability must ship inside the plugin.**

## Research (what was refuted, what was confirmed)

The issue's original premise — "ship a companion *discoverability/advertiser* skill" —
was **cargo-cult**:

- **No first-party precedent for an advertiser skill.** Anthropic's MCP plugins either
  ship no skill (Asana, Linear; like community context7, playwright) or ship
  **config/setup** skills (Discord `/configure`, `/access`), never a skill whose only job
  is to announce the MCP's tools. Anthropic **closed the nearest auto-discovery request
  (#38253) as not-planned**.
- **But a *workflow-glue* skill IS precedented** — verified in our own installed plugins:
  `postman-knowledge` ("*Postman concepts and **MCP tool guidance**… make better
  decisions about tool selection*"), `postman-context`, jaine-matrix `access`/`configure`.
  The legitimate skill encodes **how to use the tools well** (and, as a side effect, its
  always-on `description` makes the capability discoverable + degrades gracefully when the
  server is down). That is a different artifact from an "advertiser."

**Consult panel (codex+grok+agy, find-holes) — actionable holes:**

1. *Going fully abstract weakens routing.* Dropping the "an agent cannot natively inspect
   video/audio" motivation lets the model default to filename/metadata guessing or writing
   its own ffmpeg. Fix: keep the motivation **client/backend-agnostic but concrete** —
   a negative constraint + concrete triggers + delegation boundary.
2. *The server-down gap is real* (model writes ffmpeg / says "can't" instead of "enable the
   plugin"). `instructions=` can't fix it (server down ⇒ not loaded); only a skill's
   always-on `description` is present when the server is down.
3. *Keep `plugin.json description` concrete-outcome*, not bare "media understanding".

**Verified mechanics (ground-truth, not agent hearsay):**

- **MCP `instructions` IS injected by Claude Code.** `instructions` is a standard MCP
  `InitializeResult` field ("MAY be added to the system prompt"); issue **#30135**
  ("Disabled MCP servers still inject instructions into context window", CLOSED) proves CC
  injects it. (One research agent wrongly claimed it isn't standard — refuted by #30135.)
  → setting `instructions=` is worthwhile; it reaches the model and aids routing **when
  connected**.
- **Tool-name format:** `mcp__plugin_<plugin>_<server>__<tool>` →
  `mcp__plugin_jaine-media_jaine-media__analyze_media` (self-verified in the live deferred-tool list).
- **No naming collision** between plugin name, server name, and skill dir; `/jaine-media:media` is clean.
- **Graceful degradation = prose only.** No native "if tool unavailable, do Y" mechanism;
  the skill body instructs the model to advise enabling the plugin.
- **`allowed-tools` only GRANTS, never restricts**, and our own MCP+skill plugins do NOT
  list `mcp__` tools there → we won't either (no value: tools are already available when up,
  absent when down).

## Design — three artifacts, all client/backend-agnostic in what ships

### 1. MCP `instructions=` (routing, when server is UP)

Set on `FastMCP("jaine-media", instructions=INSTRUCTIONS)`. Client/backend-agnostic
(no "Claude"/"Gemini") but **concrete**: negative constraint + triggers + tool-chain. ≤2KB.

```
Tools for understanding the CONTENTS of video and audio — a language model cannot
natively inspect media payloads. Do NOT answer media-content questions from a filename,
metadata, or the user's description, and do NOT write your own ffmpeg/python to process
media — delegate to these tools.

Route by intent:
- A video/audio file (.mp4/.mov/.webm/.mkv/.mp3/.wav/.m4a…) or a YouTube/web URL, and a
  question about what's in it ("what happens", "transcribe", "describe", "at what point…")
  → analyze_media(path=<file-or-URL>, question=...). A URL is passed directly and
  auto-routes (native YouTube vs download). Compare clips → paths=[...]. Continue the
  discussion or add clips mid-conversation → history=[...]. Tune with detail
  (brief|normal|full), model, language, fps.
- SEE exact frames at a moment → extract_frame(path, timecode, window) — returns image
  frames the agent reads directly.
- A URL/YouTube is large, or you want a local copy first → fetch_media(url) (SSRF-guarded,
  quality-capped) → workspace file.
- A file is too large/long to process → prepare_media(path) — compress or trim to fit.
- Picking a model, or a model errored → list_models() — live catalog of usable models.

Typical chain: fetch_media (if remote) → prepare_media (if oversized) → analyze_media →
extract_frame (exact moments). For most "what's in this video/audio" tasks call
analyze_media directly — it accepts files and URLs and fetches internally.
```

### 2. `skills/media/SKILL.md` (discoverability + glue + graceful degradation, when server is DOWN or UP)

- **Command:** `/jaine-media:media` (= directory name). `name: media` is display-only.
- **`description`** (always-on; the only always-loaded cost; ≤1536 chars, key use first,
  third-person, trigger phrases, client/backend-agnostic):

  ```
  Understand the CONTENTS of video and audio — transcribe, describe, answer what happens
  in a clip, locate moments, extract frames, compare clips, or analyze a YouTube/web URL.
  Use whenever a request is about media a text model cannot natively inspect (.mp4/.mov/.mp3,
  "what's in this video", "transcribe this audio", "extract a frame at 0:42"). Routes to the
  jaine-media MCP tools; if they are unavailable, explains how to enable the plugin instead
  of guessing or writing ffmpeg.
  ```

- **`allowed-tools`:** minimal — `[Bash, Read]` (Bash only for the diagnose step
  `claude mcp list`; no `mcp__` names listed).
- **Body** (loads only on invocation; progressive disclosure):
  - Tool-chain guidance: `fetch_media` (remote) → `prepare_media` (oversized) →
    `analyze_media` → `extract_frame` (exact moments); when to use `paths=`/`history=`/`detail`.
  - References the tools by their `mcp__plugin_jaine-media_jaine-media__<tool>` names.
  - **Graceful degradation:** "If the `mcp__plugin_jaine-media_jaine-media__*` tools are not
    available, the server isn't connected. Tell the user to enable it — `claude mcp list` to
    check, `/reload-plugins` or approve the plugin, ensure `GEMINI_API_KEY` is set — and do
    NOT fall back to writing ffmpeg/python."

### 3. `plugin.json description` (human-facing, marketplace; concrete outcomes, agnostic)

```
Media understanding — analyze video & audio (local files or YouTube/web URLs), extract
frames, fetch & prepare media.
```

## Rejected

- Advertiser-only skill (no first-party precedent; #38253 not-planned).
- Naming "Claude"/"Gemini" in any **shipped** artifact (instructions/description) — keep
  dev-specifics in CLAUDE.md/AGENTS.md only; MCP is client-agnostic and the backend may change.
- Listing `mcp__` tools in skill `allowed-tools` (no value; not what our plugins do).
- A separate `when_to_use` frontmatter key (not a verified field; fold into `description`).

## Test plan (TDD, visible RED first)

- **`instructions=`:** import `server.mcp`; assert `mcp.instructions` is non-empty, ≤2048
  chars, contains routing anchors (`analyze_media`, `extract_frame`, "cannot natively"),
  and contains neither "Claude" nor "Gemini".
- **`skills/media/SKILL.md`:** file exists; frontmatter parses as YAML; `description`
  present, ≤1536 chars, free of "Claude"/"Gemini"; body references at least
  `analyze_media`/`extract_frame` and the graceful-degradation hint (`reload-plugins`/`mcp list`).
- **`plugin.json description`:** concrete-outcome words present (`video`, `audio`,
  `frames`); free of "Gemini".

## Acceptance

207 existing tests stay green; new tests RED→GREEN; `codex_review` on the committed diff
iterated to CLEAN; PR `--base jaine-media/main`; #228 closed manually on merge.
