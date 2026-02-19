---
description: Ask {EXPERT_NAME} ({EXPERT_ROLE}) — news or specific question
argument-hint: "[question]"
allowed-tools: ["Task", "Read", "Write", "Glob", "WebSearch", "WebFetch", "Bash", "AskUserQuestion"]
---

# Ask {EXPERT_NAME}

Query {EXPERT_NAME} ({EXPERT_ROLE}) for recent activity or specific questions. Uses public sources: GitHub, blog posts, social media.

## Step 1: Determine Mode

- If `$ARGUMENTS` contains a question or topic: **Q&A Mode**
- If `$ARGUMENTS` is empty: **News Mode** (what's new since last check)

## Step 2: Read Expert Profile

Read the expert profile for search sequences and known sources:

@${CLAUDE_PLUGIN_ROOT}/skills/expert-config/references/{EXPERT_ID}-profile.md

## Step 3: Read State

Read `~/.claude/ask-expert.local.md` to get `last_checked` date for {EXPERT_ID}.

- If file exists: use the `last_checked` timestamp under `experts.{EXPERT_ID}`
- If file does not exist: use "last 7 days" as default timeframe

## Step 4: Launch Expert Researcher Agent

**IMMEDIATELY use the Task tool** to launch the `ask:expert-researcher` agent.

Provide the agent with:

**For News Mode:**
```
Research recent activity of {EXPERT_NAME} ({EXPERT_ROLE}).

MODE: news
LAST CHECKED: {last_checked date or "last 7 days"}
CURRENT DATE: {today's date}

EXPERT PROFILE:
{paste the full profile content from Step 2}

Execute all search sequences from the profile in order. Report findings grouped by source.
```

**For Q&A Mode:**
```
Answer a question about {EXPERT_NAME}'s practices/setup.

MODE: qa
QUESTION: {user's question from $ARGUMENTS}
CURRENT DATE: {today's date}

EXPERT PROFILE:
{paste the full profile content from Step 2}

Execute search sequences focusing on the question. Provide answer with source citations.
```

## Step 5: Update State (News Mode ONLY)

**ONLY update state if Mode = News.** Q&A mode does NOT change last_checked.

If Mode = News, update `~/.claude/ask-expert.local.md`:
Update the `last_checked` date for {EXPERT_ID} to current ISO timestamp.

If Mode = Q&A, **skip this step entirely.**

## Step 6: Present Results

Show the agent's findings to the user. Ensure all sources are linked.
