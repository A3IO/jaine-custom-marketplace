# git-workflow

Git workflow automation plugin for Claude Code.

## Features

| Skill | Description |
|-------|-------------|
| `/changelog-before-merge` | Generate comprehensive changelog before merge |

## What's New in v1.8.0

### v1.8.0 (2026-02-07)
- **Caller-computed filename** — command computes exact filename BEFORE launching agent
- **Output Location moved up** — agent sees filename rules immediately after mission
- **Consistent format** — all docs use `{type}_{branch}[_PR{N}]` pattern
- **Edge cases documented** — worktree branches, no-type, no-PR, API errors

### v1.7.1 (2026-02-07)
- **check-issue-type.sh** — deterministic script for issue/PR distinction

### v1.7.0 (2026-02-07)
- **numstat validation** — per-file stats from `git diff --numstat`
- **Issue/PR distinction** — API verification for `#N` references

### v1.4.0 (2026-01-25)
- **Migrated to Skills pattern** — proper Claude Code skill structure
- **Code Verification (MANDATORY)** — verify method names, endpoints, SSE events against source
- **Progressive disclosure** — lean SKILL.md with references/ for details
- **Better description** — third-person format for skill matching

### v1.3.0 (2026-01-25)
- **Code Verification added** — verify all technical claims against code

### v1.2.1 (2026-01-23)
- **Worktree pattern support** — auto-detect `{product}/main` for `/0/SETUP/` repos

### v1.2.0 (2026-01-23)
- **ISO 8601 filenames** — `2026-01-23_BRANCH_CHANGELOG_fix_foo_PR51.md`
- **Clickable links** — `[#45](url) (url)` format for Issues/PRs
- **Auto-detect repo URL** — from git remote + $FORGEJO_API_URL
- **Auto-detect PR number** — via Forgejo/GitHub API
- **Mandatory fact-checking** — verify stats before saving

### v1.1.0 (2026-01-23)
- **Russian by default** — changelog generated in Russian
- **Enhanced Executive Summary** — bullet points with emoji
- **Diagram Requirements** — 5+ Mermaid for PR 30+ files
- **Quality Gates** — automatic verification before saving

## Agents

| Agent | Purpose |
|-------|---------|
| `changelog-analyzer` | Specialized agent for git diff analysis with ultrathink (model: opus) |

## Installation

The plugin is auto-loaded from JAINE plugins directory.

## Usage

### Changelog Before Merge

```bash
# Auto-detect target branch (worktree pattern support)
# jaine-speech/fix/foo → compares with jaine-speech/main
/changelog-before-merge

# Explicit target branch
/changelog-before-merge jaine-speech/main

# Deep analysis (5+ diagrams)
/changelog-before-merge --depth thorough

# English output
/changelog-before-merge --lang en
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target-branch` | **auto-detect** | `{product}/main` for worktree, `main` otherwise |
| `--depth` | `standard` | `standard` or `thorough` |
| `--lang` | `ru` | `ru` (Russian) or `en` (English) |

### Output

**Filename format (ISO 8601):**
```
docs/{YYYY-MM-DD}_BRANCH_CHANGELOG_{type}_{branch}[_PR{N}].md
```

Examples:
- `2026-01-23_BRANCH_CHANGELOG_fix_voice-key-naming_PR51.md`
- `2026-01-23_BRANCH_CHANGELOG_feat_provider-wizard.md` (no PR yet)
- `2026-02-07_BRANCH_CHANGELOG_hotfix-urgent.md` (no type prefix, no PR)

### Link Format

All issue/PR references become clickable:
```markdown
[#45](http://repo-url/issues/45) (http://repo-url/issues/45)
```

### All Sections

1. Executive Summary (with emoji bullets)
2. Statistics table + Commit Type Distribution
3. Commit History table
4. Architecture Changes (Mermaid diagrams)
5. Bug Fixes (Root Cause Analysis)
6. New Features
7. Breaking Changes (with migration)
8. Related Issues
9. File-by-File Analysis
10. Migration Notes
11. **Code Verification** (method/endpoint verification)
12. **Verification** (fact-checking results)

### Diagram Requirements

| PR Size | Minimum Diagrams |
|---------|------------------|
| 1-10 files | 1 |
| 11-30 files | 3 |
| 31+ files | 5 |

## Examples

See `examples/` directory:

| File | Purpose |
|------|---------|
| `TEMPLATE.md` | Clean template with placeholders |
| `EXAMPLE_ANNOTATED.md` | Educational example with guidance |
| `REAL_WORLD_jaine-speech.md` | Production example |

## Architecture

```
git-workflow/
├── .claude-plugin/
│   └── plugin.json
├── commands/                          # Slash-commands (/ menu)
│   └── changelog-before-merge.md
├── skills/
│   └── changelog-before-merge/
│       ├── SKILL.md                   # Main skill file
│       └── references/
│           ├── code-verification.md   # Verification process
│           ├── mermaid-diagrams.md    # Diagram requirements
│           └── quality-checklist.md   # Quality gates
├── agents/
│   └── changelog-analyzer.md          # Opus + ultrathink
├── scripts/
│   └── check-issue-type.sh           # Issue/PR type checker
├── examples/
│   ├── TEMPLATE.md
│   ├── EXAMPLE_ANNOTATED.md
│   └── REAL_WORLD_jaine-speech.md
└── README.md
```

## Quality Gates

Agent verifies before saving:
- [ ] Executive Summary includes emoji bullet points
- [ ] All bugs have Root Cause analysis
- [ ] Mermaid diagrams match PR size
- [ ] Commit Type Distribution present
- [ ] Language matches --lang parameter
- [ ] **All #N are clickable links**
- [ ] **Filename in ISO 8601 format**
- [ ] **Fact-checking passed**
- [ ] **Code Verification passed**
- [ ] **Verification Report added**

## Future Skills

Planned additions:

- `/pr-description` — Generate PR description from commits
- `/release-notes` — Generate release notes for tag

---

*JAINE Plugin v1.8.0 (2026-02-07)*
