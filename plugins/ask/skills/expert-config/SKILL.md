---
name: expert-config
description: >
  This skill should be used when the user asks to "add an expert", "configure expert profile",
  "set up search sequences", "expert tracking", "expert sources", or needs guidance on
  expert profile format, search sequence configuration, or the ask plugin architecture.
---

# Expert Configuration Guide

## Expert Profile Format

Each expert is defined by a profile markdown file in `references/{id}-profile.md`.

### Required Sections

1. **Identity** — name, role, bio
2. **Search Sequences** — ordered pipeline of sources
3. **Known URLs** — bookmarked links for the expert

### Search Sequence Types

| Type | Tool | Reliability | Description |
|------|------|------------|-------------|
| `github_prs` | Bash (gh CLI) | RELIABLE | PRs by the expert to specific repos |
| `github_config` | Bash (gh CLI) | RELIABLE | Commits to config/setup repos |
| `web_search` | WebSearch | RELIABLE | Blog posts, articles, interviews |
| `social_threads` | WebFetch | BEST EFFORT | Meta Threads posts |
| `social_x` | WebSearch | BEST EFFORT | X/Twitter via search + mirrors |

### Reliability Levels

- **RELIABLE** — expected to work consistently, core of the research
- **BEST EFFORT** — may fail due to platform restrictions, agent should skip and continue

## State Management

Plugin state is stored in `~/.claude/ask-expert.local.md`:

```yaml
---
experts:
  boris:
    last_checked: "2026-01-30T15:00:00Z"
  swyx:
    last_checked: null
---
```

- `last_checked: null` means the expert has never been queried
- Fallback: if no date, agent uses "last 7 days" window

## Adding a New Expert

1. Use `/ask:add` wizard for guided setup
2. Or manually create:
   - Profile: `skills/expert-config/references/{id}-profile.md`
   - Command: `commands/{id}.md`
   - State entry in `~/.claude/ask-expert.local.md`
3. Restart Claude Code to activate new command

## Templates

- Profile template: `examples/expert-profile-template.md`
- Command template: `examples/expert-command-template.md`
