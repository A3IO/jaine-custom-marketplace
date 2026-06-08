# jainechat

JAINE's localhost web chat for Claude Code channel messaging.

## Setup

```sh
claude --dangerously-load-development-channels plugin:jainechat@jaine-custom
```

The server prints the URL to stderr on startup:

```
jainechat: http://localhost:7777
```

Open it. Type. The assistant replies in-thread.

Set `JAINECHAT_PORT` to change the port (default: 7777; if busy, the next free port
up to +20 is taken — the printed URL is canonical).

The server exits when the Claude Code session ends: stdin EOF (the MCP shutdown
signal the SDK itself never watches) plus a backstop watchdog — orphan check + MCP
ping every 15s, detection ≤40s — for a hung client or an undelivered EOF
(fifo-style stdin + `Bun.serve` suppresses it, oven-sh/bun#3255). Ping failure is
detected by timeout, not by broken-pipe errors: Bun ≤1.2.x reported dead-pipe
writes as success (fixed in 1.3.x, `'error'` event still missing — bun#7251), and
the SDK ignores write errors anyway. Closed sessions free their ports. Manual
`bun server.ts` runs are not affected — the watchdog arms only after a real MCP
client initializes.

## Tools

| Tool | Purpose |
| --- | --- |
| `reply` | Send to the UI. Takes `text`, optionally `reply_to` (message ID) and `files` (absolute path, 50MB). |
| `edit_message` | Edit a previously-sent message in place. |

Inbound files save to `~/.claude/channels/jainechat/inbox/`.
Outbound files are copied to `outbox/` and served over HTTP.

## Voice (x.ai)

Full-duplex voice in the chat:

- **🎤 talk** — record from the mic; audio is transcribed by x.ai STT and arrives as a normal message.
- **🔊** — when checked, JAINE's replies are spoken via x.ai TTS (voice `eve`, language auto-detected).

Auth reuses the **grok CLI** OAuth session at `~/.grok/auth.json` — the JWT's `api:access` scope is accepted by `api.x.ai`, and the token auto-refreshes near expiry. Set `XAI_API_KEY` to override. Requires a logged-in `grok` CLI (run `grok` and sign in) or an API key.

| Endpoint | Purpose |
| --- | --- |
| `POST /stt` | multipart `file` (+ optional `language`, `id`) → transcribes, delivers text to the session, returns `{text}`. |
| `POST /tts` | JSON `{text, voice?, language?}` → returns `audio/mpeg`. |

## Local LLM mode (instant replies)

Set `JAINECHAT_LLM` to an [ollama](https://ollama.com) model and the server answers **locally** — streaming the reply phrase by phrase into the TTS pipeline — instead of routing to the Claude Code session. Built for near-instant voice latency (granite4.1:3b warm ≈ 0.15s; first spoken phrase ≈ 0.5s).

```sh
JAINECHAT_LLM=granite4.1:3b bun server.ts
```

| Env | Default | Purpose |
| --- | --- | --- |
| `JAINECHAT_LLM` | _(unset → Claude channel)_ | ollama model name; setting it switches to local mode |
| `JAINECHAT_SYSTEM_PROMPT` | `…/VRHOT/jaine/JAINE_SYSTEM_PROMPT.md` | system-prompt file (JAINE persona) |
| `JAINECHAT_OLLAMA` | `http://localhost:11434` | ollama endpoint |

Replies stream sentence by sentence (markdown/emoji stripped for TTS), length-capped via `num_predict`, with the last 20 turns kept in memory. Unset `JAINECHAT_LLM` to answer through Claude again.
