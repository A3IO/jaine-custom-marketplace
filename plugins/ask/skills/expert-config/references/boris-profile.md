# Boris Cherny — Expert Profile

## Identity

- **Name:** Boris Cherny
- **Role:** Creator of Claude Code (Anthropic)
- **Bio:** Software engineer at Anthropic. Created Claude Code CLI. Known for vanilla-but-effective setup, parallel sessions workflow, verification-first approach.
- **Key quote:** "If you can't verify, you're gambling."

## Search Sequences

Ordered pipeline for gathering information. Execute top-to-bottom, skip unavailable sources.

### 1. GitHub PRs (RELIABLE)

```
tool: Bash
command: gh pr list --author bcherny --repo anthropics/claude-code --state all --limit 15 --json number,title,state,createdAt,url
```

Recent PRs by Boris to Claude Code repo. Primary source of what he's building.

### 2. GitHub Config Mirror (RELIABLE)

```
tool: Bash
command: gh api repos/0xquinto/bcherny-claude/commits --jq '.[0:5] | .[] | {date: .commit.committer.date, message: .commit.message}'
```

Community mirror of his `.claude/` setup: agents, commands, settings. Check for config updates.

### 3. Web Search — Articles & Blog Posts (RELIABLE)

```
tool: WebSearch
query: "Boris Cherny" Claude Code {year}
```

Blog breakdowns, interviews, conference talks. Good for detailed analysis of his workflow.

**Known high-quality sources:**
- [paddo.dev](https://paddo.dev/blog/how-boris-uses-claude-code/) — detailed breakdown
- [InfoQ](https://www.infoq.com/news/2026/01/claude-code-creator-workflow/) — professional analysis
- [Dev Genius](https://blog.devgenius.io/how-the-creator-of-claude-code-actually-uses-it-13-practical-moves-2bf02eec032a) — 13 practical moves
- [Substack](https://karozieminski.substack.com/p/boris-cherny-claude-code-workflow) — workflow analysis

### 4. Threads (BEST EFFORT)

```
tool: WebFetch
url: https://www.threads.com/@boris_cherny/
prompt: "What are the latest posts by Boris Cherny about Claude Code? Summarize each post."
```

Meta Threads account. May or may not be fetchable.

### 5. X/Twitter (BEST EFFORT)

```
tool: WebSearch
query: from:bcherny Claude Code site:x.com OR site:xcancel.com
```

Primary social channel. Direct access unreliable; use search + mirrors.

**Known mirrors:**
- [XCancel](https://xcancel.com/bcherny/) — Twitter/X mirror
- [Thread Reader](https://threadreaderapp.com/thread/2007179832300581177.html) — thread unroller

## Known Configuration (as of Jan 2026)

### Agents (5)
| Agent | Purpose |
|-------|---------|
| code-simplifier | Review and simplify recently written code |
| code-architect | Design reviews, structural evaluation |
| verify-app | Multi-phase application testing (static, automated, manual, edge cases) |
| build-validator | CI/build readiness checks |
| oncall-guide | Production incident troubleshooting |

### Commands (5)
| Command | Purpose |
|---------|---------|
| /commit-push-pr | Full git workflow (used dozens of times daily) |
| /quick-commit | Stage + descriptive commit |
| /test-and-fix | Run tests, fix failures iteratively |
| /review-changes | Analyze uncommitted changes |
| /first-principles | Break down problems to foundations |

### Hooks
- **PostToolUse** (Write/Edit): Auto-format with `npm run format` or `npx prettier`
- No PreToolUse, Stop, or UserPromptSubmit hooks

### Key Practices
- 5 parallel Claude Code sessions (terminal) + 5-10 on claude.ai/code
- Opus 4.5 with thinking for all coding
- Plan mode first, iterate, then auto-accept
- CLAUDE.md shared per team (2.5k tokens)
- Verification loop: typecheck, test, lint before PRs
- 259 PRs in 30 days (497 commits, 40k lines)

## Stats

- **Productivity:** ~100 PRs/week
- **Model preference:** Opus 4.5 with extended thinking
- **Setup philosophy:** "Surprisingly vanilla"
