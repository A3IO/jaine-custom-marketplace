---
name: changelog-before-merge
description: >
  Use this skill when the user asks to "generate changelog",
  "analyze changes before merge", "document PR changes", "what changed",
  "create branch changelog", or needs comprehensive git diff analysis
  with commit history, bug fixes, architecture diagrams, and related issues.
---

# Changelog Before Merge

Generate comprehensive changelog with date, Issue/PR links, and code verification.

**Output format:** `docs/{YYYY-MM-DD}_BRANCH_CHANGELOG_{type}_{branch}[_PR{N}].md`

## Quick Usage

```bash
# Auto-detect target branch (jaine-speech/fix/foo -> jaine-speech/main)
/changelog-before-merge

# Explicit target branch
/changelog-before-merge jaine-speech/main

# Deep analysis (more diagrams, detailed root cause)
/changelog-before-merge --depth thorough

# English output
/changelog-before-merge --lang en
```

**Worktree pattern support:** If current branch is `jaine-speech/fix/voice-key`, default target is automatically `jaine-speech/main`, not `main`.

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `target-branch` | **auto-detect** | `{product}/main` for worktree pattern, `main` otherwise |
| `--depth` | `standard` | `standard` or `thorough` (more diagrams, deeper analysis) |
| `--lang` | `ru` | Output language: `ru` (Russian) or `en` (English) |

---

## Workflow

### Step 1: Detect Branches

```bash
CURRENT=$(git rev-parse --abbrev-ref HEAD)

# Auto-detect for /0/SETUP worktree pattern
if [[ "$CURRENT" == */* ]]; then
    PRODUCT_PREFIX=$(echo "$CURRENT" | cut -d'/' -f1)
    DEFAULT_TARGET="${PRODUCT_PREFIX}/main"
else
    DEFAULT_TARGET="main"
fi

TARGET=${1:-$DEFAULT_TARGET}
```

**Auto-detection examples:**
| Current Branch | Default Target |
|----------------|----------------|
| `jaine-speech/fix/voice-key` | `jaine-speech/main` |
| `dotfiles/feat/zsh-update` | `dotfiles/main` |
| `feat/simple-feature` | `main` |

### Step 2: Gather Data

```bash
git log $TARGET..HEAD --oneline | wc -l    # Commit count
git diff $TARGET..HEAD --stat              # File stats
git log $TARGET..HEAD --oneline            # Full history
git diff $TARGET..HEAD                     # Detailed diff
```

### Step 3: Deep Analysis

Use `mcp__sequentialthinking__sequentialthinking` for systematic analysis:

1. **Categorize commits** by type (feat, fix, refactor, docs, test)
2. **Identify bug fixes** - extract root cause and solution
3. **Detect architecture changes** - new modules, patterns, dependencies
4. **Find breaking changes** - removed APIs, changed signatures
5. **Extract related issues** - from commit messages (#123 format)

### Step 4: Generate Document

Create changelog file using the caller-provided filename (command computes it before launching agent). Format: `docs/{YYYY-MM-DD}_BRANCH_CHANGELOG_{type}_{branch}[_PR{N}].md`. Example: `docs/2026-02-07_BRANCH_CHANGELOG_fix_voice-key_PR51.md`

### Step 4.5: Code Verification (MANDATORY)

**After generating draft, MUST verify all claims against code.**

See [references/code-verification.md](references/code-verification.md) for detailed verification process.

Quick summary:
- Verify method names: `grep "def method_name" src/`
- Verify endpoints: `grep "@app.post.*endpoint" src/`
- Verify new files: `test -f path`
- Verify SSE events: check BOTH server AND client
- Apply corrections and add Verification Report section

---

## Required Sections

1. **Executive Summary** - Overview + emoji bullet points
2. **Statistics Table** - Files, lines, commits, issues
3. **Commit History + Distribution** - Table + type percentages
4. **Architecture Changes** - Mermaid diagrams (see [references/mermaid-diagrams.md](references/mermaid-diagrams.md))
5. **Bug Fixes** - Root Cause Analysis (Symptom -> Cause -> Solution)
6. **New Features** - Description with usage examples
7. **Breaking Changes** - Migration steps (if any)
8. **Related Issues** - Linked issues table
9. **File-by-File Analysis** - Grouped by directory/layer
10. **Migration Notes** - Post-merge actions (if needed)

---

## Depth Levels

### Standard (default)
- Commit categorization
- 1-3 Mermaid diagrams
- Basic root cause for bugs
- Issue extraction

### Thorough (`--depth thorough`)
- 5+ Mermaid diagrams
- Extended sequential thinking for each major change
- Deep root cause analysis with code examples
- Performance implications
- Security review (if sensitive files changed)
- Test coverage analysis

---

## Quality Gates

See [references/quality-checklist.md](references/quality-checklist.md) for full checklist.

**Critical checks:**
- [ ] Executive Summary includes emoji bullet points
- [ ] All bugs have Root Cause analysis
- [ ] Mermaid diagrams match PR size requirements
- [ ] Code Verification passed
- [ ] Verification Report section added

---

## Output

**Filename Format (ISO 8601):**
```
docs/{YYYY-MM-DD}_BRANCH_CHANGELOG_{type}_{branch}[_PR{N}].md
```

Examples:
- `2026-01-23_BRANCH_CHANGELOG_fix_voice-key-naming_PR51.md`
- `2026-01-23_BRANCH_CHANGELOG_feat_provider-wizard.md` (no PR)
- `2026-02-07_BRANCH_CHANGELOG_hotfix-urgent.md` (no type prefix, no PR)

**Caller (command) computes filename** before launching the agent. Agent uses it as-is.
If not provided, agent computes using branch sanitization + PR auto-detection.

---

## Examples

See `${CLAUDE_PLUGIN_ROOT}/examples/` for templates and real-world examples:
- `TEMPLATE.md` - Clean template with placeholders
- `EXAMPLE_ANNOTATED.md` - Generic annotated example
- `REAL_WORLD_jaine-speech.md` - Real PR #44 example

---

## Tips

- Run **before** creating PR to document your work
- Generated changelog can be copy-pasted into PR description
- Use `--depth thorough` for large PRs (30+ files) or architectural changes
- Review and edit before committing
- Russian language by default for team readability
