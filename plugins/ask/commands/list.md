---
description: List all configured experts and available commands
allowed-tools: ["Read", "Glob"]
---

# List Experts

Show all configured experts in the ask plugin.

## Step 1: Discover Expert Profiles

Use Glob to find all expert profile files:

```
${CLAUDE_PLUGIN_ROOT}/skills/expert-config/references/*-profile.md
```

## Step 2: Read State

Read `~/.claude/ask-expert.local.md` to get last_checked dates for each expert.
If the file does not exist, note that no experts have been queried yet.

## Step 3: Present Expert List

For each discovered profile, display:

| Expert | Role | Command | Last Checked |
|--------|------|---------|-------------|
| Name | Role from profile | `/ask:{id}` | Date or "never" |

## Step 4: Show Available Actions

After the table, show:

- `/ask:{id}` — query specific expert (news or Q&A)
- `/ask:{id} "your question"` — ask specific question
- `/ask:add` — add new expert
- `/ask:help` — plugin documentation
