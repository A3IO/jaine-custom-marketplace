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

Set `JAINECHAT_PORT` to change the port (default: 7777).

## Tools

| Tool | Purpose |
| --- | --- |
| `reply` | Send to the UI. Takes `text`, optionally `reply_to` (message ID) and `files` (absolute path, 50MB). |
| `edit_message` | Edit a previously-sent message in place. |

Inbound files save to `~/.claude/channels/jainechat/inbox/`.
Outbound files are copied to `outbox/` and served over HTTP.
