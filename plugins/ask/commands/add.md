---
description: Add a new expert to track (interactive wizard)
allowed-tools: ["Task", "Read", "Write", "Glob", "AskUserQuestion", "Bash"]
---

# Add New Expert

Interactive wizard to configure a new expert for tracking.

## Step 1: Gather Basic Information

Use AskUserQuestion to collect:

**Question 1:** "What is the expert's name and role?"
- Options: Free text (use "Other" for custom input)
- Example: "Swyx — AI Engineer, Latent Space"

Parse the response to extract:
- `name` — full name
- `id` — lowercase, no spaces (e.g., "swyx")
- `role` — their role/title

## Step 2: Gather GitHub Information

Use AskUserQuestion:

**Question:** "What is their GitHub username and which repos should we track?"
- Options: Free text
- Example: "swyxio — repos: ai-notes, latent-space"

Parse:
- `github_username`
- `github_repos` (list)

## Step 3: Gather Social & Search Info

Use AskUserQuestion (multiSelect possible):

**Question:** "Social media and search keywords?"
- Collect: X/Twitter handle, Threads handle, web search keywords
- All optional — user can skip

Parse:
- `x_handle` (optional)
- `threads_handle` (optional)
- `search_keywords` (list)

## Step 4: Read Templates

Read the expert profile template:
@${CLAUDE_PLUGIN_ROOT}/skills/expert-config/examples/expert-profile-template.md

Read the expert command template:
@${CLAUDE_PLUGIN_ROOT}/skills/expert-config/examples/expert-command-template.md

## Step 5: Create Expert Profile

Using the profile template, create a new file:
`${CLAUDE_PLUGIN_ROOT}/skills/expert-config/references/{id}-profile.md`

Replace all placeholders with collected information:
- `{EXPERT_NAME}` → full name
- `{EXPERT_ROLE}` → role
- `{GITHUB_USERNAME}` → GitHub username
- `{GITHUB_REPOS}` → tracked repos
- `{X_HANDLE}` → X handle (or remove section if not provided)
- `{THREADS_HANDLE}` → Threads handle (or remove section if not provided)
- `{SEARCH_KEYWORDS}` → web search terms
- `{YEAR}` → current year

## Step 6: Create Expert Command

Using the command template, create a new file:
`${CLAUDE_PLUGIN_ROOT}/commands/{id}.md`

Replace placeholders:
- `{EXPERT_ID}` → id
- `{EXPERT_NAME}` → full name
- `{EXPERT_ROLE}` → role

## Step 7: Update State

Read `~/.claude/ask-expert.local.md` and add the new expert:

```yaml
experts:
  {id}:
    last_checked: null
```

If the file doesn't exist, create it with this content.

## Step 8: Confirm

Tell the user:

1. Expert profile created: `skills/expert-config/references/{id}-profile.md`
2. Command created: `commands/{id}.md`
3. State updated: `~/.claude/ask-expert.local.md`
4. **IMPORTANT: Restart Claude Code to activate `/ask:{id}` command**
