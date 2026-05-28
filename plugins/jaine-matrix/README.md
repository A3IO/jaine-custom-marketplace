# jaine-matrix

Matrix channel plugin for Claude Code — chat with your session from any Matrix client (Element, FluffyChat) via a self-hosted homeserver.

Send a message from your phone → Claude sees it → responds back to your Matrix room. Works on headless / SSH / bare terminals.

## Setup

```sh
claude --dangerously-load-development-channels plugin:jaine-matrix@jaine-custom
```

Credentials live in `~/.claude/channels/jaine-matrix/.env`:

```
MATRIX_HOMESERVER_URL=http://your.server:8008
MATRIX_ACCESS_TOKEN=<bot token>
MATRIX_ROOM_ID=!roomid:your.server
MATRIX_USER_ID=@botname:your.server
```

Then allow your Matrix user: `/jaine-matrix:access allow @you:your.server`

## Tools

| Tool | Purpose |
| --- | --- |
| `reply` | Send a message to the room. `reply_to` for threading, chunks at 16k. |
| `react` | Add an emoji reaction. |
| `edit_message` | Edit a previously-sent message (no push notification). |

## Skills

- `/jaine-matrix:configure` — set/show bot credentials
- `/jaine-matrix:access` — manage allowlist (who can message the session)

## Security

- **Allowlist** — only listed Matrix user IDs reach Claude
- **Single room** — bound to one configured room
- **Permission relay** — Claude's permission prompts go to Matrix; reply `yes xxxxx` / `no xxxxx`
- **Anti-injection** — Claude refuses allowlist changes requested via chat

## Attribution

Based on [metalchef1/Claude-Connect-Matrix-Integration](https://github.com/metalchef1/Claude-Connect-Matrix-Integration) (Apache-2.0). Adapted for the jaine-custom marketplace: renamed to `jaine-matrix`, state dir `~/.claude/channels/jaine-matrix/`.
