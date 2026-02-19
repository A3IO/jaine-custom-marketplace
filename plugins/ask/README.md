# ask — Expert Tracking Plugin

> **Status: MVP / Proof of Concept.** Плагин в зачаточном состоянии. Базовая функциональность работает (`/ask:boris`), но архитектура требует полного анализа и рефакторинга: хранение пользовательских данных в кеше плагина, отсутствие тестов, hardcoded пути, потеря динамических экспертов при update. После мерджа в main необходимо провести аудит и спланировать v0.2.0 с исправлением архитектурных проблем.

Track and query industry experts from their public sources. Aggregate GitHub activity, blog posts, and social media into structured research reports.

## Quick Start

```
/ask:boris                          # What's new from Boris Cherny?
/ask:boris how does he use hooks?   # Ask a specific question
/ask:list                           # List all configured experts
/ask:add                            # Add a new expert (wizard)
/ask:help                           # Full documentation
```

## Pre-installed Experts

| Expert | Command | Role |
|--------|---------|------|
| Boris Cherny | `/ask:boris` | Creator of Claude Code (Anthropic) |

## How It Works

Each expert has a **profile** with **search sequences** — an ordered pipeline of public sources:

1. **GitHub PRs** — what they're building (via `gh` CLI)
2. **GitHub config** — their setup changes (mirrors, config repos)
3. **Web Search** — blog posts, articles, interviews
4. **Social media** — Threads, X/Twitter (best effort)

### Modes

- **News Mode** (`/ask:boris`): Aggregates activity since last check. Tracks `last_checked` date per expert.
- **Q&A Mode** (`/ask:boris "question"`): Searches sources filtered by your question, returns answer with citations.

### Architecture

```
/ask:boris "question"
    │
    ├─→ Read expert profile (boris-profile.md)
    ├─→ Read state (~/.claude/ask-expert.local.md)
    ├─→ Launch expert-researcher agent (opus)
    │       │
    │       ├─→ GitHub PRs (gh cli)
    │       ├─→ GitHub config mirror
    │       ├─→ Web Search (articles)
    │       ├─→ Threads (best effort)
    │       └─→ X/Twitter (best effort)
    │
    ├─→ Update state (last_checked)
    └─→ Present results with sources
```

## Adding Experts

### Interactive wizard

```
/ask:add
```

Guides you through: name, GitHub, social handles, search keywords. Creates profile and command files. Requires Claude Code restart to activate new `/ask:{name}` command.

### Manual

1. Create profile: `skills/expert-config/references/{id}-profile.md` (see templates in `examples/`)
2. Create command: `commands/{id}.md` (see template in `examples/`)
3. Add to `~/.claude/ask-expert.local.md` state
4. Restart Claude Code

## State

```yaml
# ~/.claude/ask-expert.local.md
---
experts:
  boris:
    last_checked: "2026-01-30T15:00:00Z"
  swyx:
    last_checked: null
---
```

## Plugin Structure

```
ask/
├── .claude-plugin/plugin.json
├── commands/
│   ├── boris.md          # /ask:boris
│   ├── add.md            # /ask:add (wizard)
│   ├── list.md           # /ask:list
│   └── help.md           # /ask:help
├── agents/
│   └── expert-researcher.md  # opus research agent
├── skills/
│   └── expert-config/
│       ├── SKILL.md
│       ├── references/boris-profile.md
│       └── examples/ (templates)
└── README.md
```

## Known Limitations (v0.1.0)

1. **Plugin update loses custom experts.** Dynamically added experts (via `/ask:add`) are stored in the plugin cache. Running `./sync plugins update` will overwrite the cache and delete custom expert commands and profiles. **Workaround:** Re-run `/ask:add` after update, or back up `~/.claude/plugins/cache/jaine-custom/ask/` before updating.

2. **`gh` CLI required** for GitHub search sequences. If not installed, the agent will attempt fallback via WebFetch but results may be limited. Install: `brew install gh && gh auth login`.

3. **Social sources are unreliable.** Threads and X/Twitter may be unavailable. The agent skips them gracefully.

4. **New expert commands require restart.** After `/ask:add`, Claude Code must be restarted for the new `/ask:{name}` command to appear.

---

*Version: 0.1.0*
