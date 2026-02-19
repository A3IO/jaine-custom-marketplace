---
description: Show ask plugin documentation and usage guide
allowed-tools: ["Read"]
---

# Ask Plugin — Help

## What is this?

The `ask` plugin tracks industry experts in the AI/dev ecosystem. It aggregates their public activity (GitHub PRs, blog posts, social media) and lets you ask questions answered from their public sources.

## Commands

| Command | Description |
|---------|-------------|
| `/ask:boris` | News from Boris Cherny (Claude Code creator) |
| `/ask:boris "question"` | Ask Boris a question (searches his public sources) |
| `/ask:list` | List all configured experts |
| `/ask:add` | Add a new expert (interactive wizard) |
| `/ask:help` | This help page |

## How It Works

Each expert has a **profile** with configured **search sequences** — an ordered pipeline of sources:

1. **GitHub PRs** (reliable) — what they're building
2. **GitHub config** (reliable) — their setup/config changes
3. **Web Search** (reliable) — blog posts, articles, interviews
4. **Social media** (best effort) — Threads, X/Twitter

When you query an expert:
- **No arguments** = "What's new?" — searches for activity since last check
- **With question** = Q&A mode — searches sources for relevant answers

## State

Plugin state is stored in `~/.claude/ask-expert.local.md` with last_checked timestamps per expert. This survives across sessions.

## Adding Experts

Use `/ask:add` to add a new expert. The wizard will ask for:
- Name and role
- GitHub username and repos to track
- Social media handles (optional)
- Keywords for web search

After adding, restart Claude Code to activate the new `/ask:{name}` command.

## Pre-installed Experts

- **Boris Cherny** (`/ask:boris`) — Creator of Claude Code at Anthropic
