---
description: Ask Boris Cherny (Claude Code creator) — news or specific question
argument-hint: "[question about Claude Code setup, hooks, workflows]"
allowed-tools: ["Task", "Read", "Write", "Glob", "WebSearch", "WebFetch", "Bash", "AskUserQuestion"]
---

# Ask Boris Cherny

Query Boris Cherny (creator of Claude Code at Anthropic) for recent activity or specific questions. Uses public sources: GitHub, blog posts, social media.

## Step 1: Determine Mode

- If `$ARGUMENTS` contains a question or topic: **Q&A Mode**
- If `$ARGUMENTS` is empty: **News Mode** (what's new since last check)

## Step 2: Read Expert Profile

Read the Boris Cherny expert profile for search sequences and known sources:

@${CLAUDE_PLUGIN_ROOT}/skills/expert-config/references/boris-profile.md

## Step 3: Read State

Read `~/.claude/ask-expert.local.md` to get `last_checked` date for boris.

- If file exists: use the `last_checked` timestamp under `experts.boris`
- If file does not exist: use "last 7 days" as default timeframe

## Step 4: Launch Expert Researcher Agent

**IMMEDIATELY use the Task tool** to launch the `ask:expert-researcher` agent.

Provide the agent with:

**For News Mode:**
```
Research recent activity of Boris Cherny (Claude Code creator).

MODE: news
LAST CHECKED: {last_checked date or "last 7 days"}
CURRENT DATE: {today's date}

EXPERT PROFILE:
{paste the full profile content from Step 2}

Execute all search sequences from the profile in order. Report findings grouped by source.
```

**For Q&A Mode:**
```
Answer a question about Boris Cherny's practices/setup.

MODE: qa
QUESTION: {user's question from $ARGUMENTS}
CURRENT DATE: {today's date}

EXPERT PROFILE:
{paste the full profile content from Step 2}

Execute search sequences focusing on the question. Provide answer with source citations.
```

## Step 5: Update State (News Mode ONLY)

**ONLY update state if Mode = News.** Q&A mode does NOT change last_checked — it answers a question, not checks for news.

If Mode = News, after the agent returns results, update `~/.claude/ask-expert.local.md`:

If file exists, update the `last_checked` date for boris to current ISO timestamp.

If file does not exist, create it:

```yaml
---
experts:
  boris:
    last_checked: "{current ISO timestamp}"
---
```

If Mode = Q&A, **skip this step entirely.**

## Step 6: Present Results

Show the agent's findings to the user. Ensure all sources are linked.
