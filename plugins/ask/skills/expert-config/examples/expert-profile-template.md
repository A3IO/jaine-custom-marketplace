# {EXPERT_NAME} — Expert Profile

## Identity

- **Name:** {EXPERT_NAME}
- **Role:** {EXPERT_ROLE}
- **Bio:** {Brief description of who they are and why they're tracked}

## Search Sequences

Ordered pipeline for gathering information. Execute top-to-bottom, skip unavailable sources.

### 1. GitHub PRs (RELIABLE)

```
tool: Bash
command: gh pr list --author {GITHUB_USERNAME} --repo {GITHUB_REPO} --state all --limit 15 --json number,title,state,createdAt,url
```

### 2. Web Search — Articles & Blog Posts (RELIABLE)

```
tool: WebSearch
query: "{EXPERT_NAME}" {SEARCH_KEYWORDS} {YEAR}
```

### 3. Threads (BEST EFFORT)

```
tool: WebFetch
url: https://www.threads.com/@{THREADS_HANDLE}/
prompt: "What are the latest posts by {EXPERT_NAME}? Summarize each post."
```

Remove this section if no Threads handle.

### 4. X/Twitter (BEST EFFORT)

```
tool: WebSearch
query: from:{X_HANDLE} {SEARCH_KEYWORDS} site:x.com OR site:xcancel.com
```

Remove this section if no X handle.

## Known URLs

- GitHub: https://github.com/{GITHUB_USERNAME}
- X/Twitter: https://x.com/{X_HANDLE}
- Threads: https://www.threads.com/@{THREADS_HANDLE}/
