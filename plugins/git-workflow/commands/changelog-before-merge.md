---
description: Analyze git diff between branches and generate comprehensive BRANCH_CHANGELOG.md. Use before creating pull requests to document all changes with commit history, file analysis, bug fixes, architecture diagrams, and related issues.
argument-hint: "[target-branch] [--depth thorough|standard] [--lang ru|en]"
allowed-tools: ["Task", "Bash", "AskUserQuestion"]
---

# Changelog Before Merge

**IMMEDIATELY use the Task tool to launch the git-workflow:changelog-analyzer agent.**

Do NOT attempt to generate the changelog yourself. The changelog-analyzer agent has:
- Model: **opus** (required for deep analysis)
- Sequential thinking capabilities
- Code verification expertise

## Step 1: Get Current Branch

```bash
git rev-parse --abbrev-ref HEAD
```

## Step 2: Determine Target Branch

If $ARGUMENTS contains target branch, use it.
Otherwise auto-detect:
- `product/fix/foo` → target = `product/main`
- `feat/bar` → target = `main`

## Step 2.5: Compute Output Filename

**Before launching the agent, compute the EXACT output filename.**

### 2.5.1: Get today's date
```bash
DATE=$(date +%Y-%m-%d)
```

### 2.5.2: Extract type and sanitized name from CURRENT_BRANCH

**Worktree branch** (2+ slashes AND first segment is NOT a conventional type like feat/fix/chore/docs/refactor/test/style/perf/ci/build):
- Remove worktree prefix (first segment): `jaine-speech/fix/voice-key` → `fix/voice-key`
- Type = second segment: `fix`
- Name = remaining segments joined with `-`: `voice-key`

**Standard branch** (1 slash, OR 2+ slashes where first segment IS a conventional type):
- Type = first segment: `feat`
- Name = remaining segments joined with `-`: `my-feature`
- Example with nested path: `feat/ui/header` → Type=`feat`, Name=`ui-header`

**No slash** (e.g. `hotfix-urgent`):
- Type = "" (omitted from filename)
- Name = branch as-is: `hotfix-urgent`

Strip any characters that are unsafe in filenames from Name (keep alphanumerics, `-`, `_`).

### 2.5.3: Detect PR number

```bash
# GitHub:
PR_NUM=$(gh pr list --head "CURRENT_BRANCH" --state open --json number --jq '.[0].number // empty')

# Forgejo (if $FORGEJO_API_URL set):
# PR_NUM=$(curl -s -H "Authorization: token $FORGEJO_API_TOKEN" \
#   "$FORGEJO_API_URL/repos/OWNER/REPO/pulls?state=open" \
#   | jq -r '[.[] | select(.head.ref=="CURRENT_BRANCH")] | .[0].number // empty')
```

If no PR found or API error → omit `_PR{N}` suffix. Do NOT fail.

### 2.5.4: Build filename

| Type | PR | Filename |
|------|-----|----------|
| ✅ | ✅ | `docs/DATE_BRANCH_CHANGELOG_TYPE_NAME_PRN.md` |
| ✅ | ❌ | `docs/DATE_BRANCH_CHANGELOG_TYPE_NAME.md` |
| ❌ | ✅ | `docs/DATE_BRANCH_CHANGELOG_NAME_PRN.md` |
| ❌ | ❌ | `docs/DATE_BRANCH_CHANGELOG_NAME.md` |

Example: branch `git-workflow/fix/changelog-filename`, date `2026-02-07`, PR #18:
→ `docs/2026-02-07_BRANCH_CHANGELOG_fix_changelog-filename_PR18.md`

### 2.5.5: Ensure docs/ exists

```bash
mkdir -p docs/
```

## Step 3: Launch Agent NOW

**Call the Task tool with subagent_type="git-workflow:changelog-analyzer":**

Prompt for the agent:
```
Analyze git diff for changelog generation.

Current branch: {CURRENT_BRANCH}
Target branch: {TARGET_BRANCH}
Depth: {depth from args, default: standard}
Language: {lang from args, default: ru}

Generate comprehensive BRANCH_CHANGELOG.md with all required sections.
Output to: {COMPUTED_FILENAME from Step 2.5}

CRITICAL: Use the exact filename above — it was computed by the caller.
CRITICAL: Verify all claims against actual code before finalizing!
```

## Step 4: Report Result

After agent completes, show the user:
- Path to generated changelog
- Brief summary of what was documented

## Step 4.5: Cross-Verify Against Reality (MANDATORY)

**After the agent generates the changelog, YOU must verify key facts yourself.**

The agent may have relied on PR description which can be stale. Run these checks:

```bash
# 1. Actual commit count
git log TARGET..HEAD --oneline | wc -l

# 2. Actual file/line stats
git diff TARGET..HEAD --stat | tail -1

# 3. Test count (if mentioned in changelog)
# Find the test file referenced in changelog and count
grep -c "def test_" path/to/test_file.py

# 4. Total test suite count (if mentioned)
# Run test suite to get actual count

# 5. Per-file line counts (CRITICAL — catches LLM hallucination!)
git diff TARGET..HEAD --numstat
# Cross-check EACH file's +/- in changelog Section 9 with numstat output
# LLMs often ESTIMATE line counts from diff content — numstat is the truth

# 6. Issue/PR distinction for #N references
# For each #N in changelog, verify it's actually an issue (not a PR)
# Forgejo: curl -s "$FORGEJO_API_URL/repos/OWNER/REPO/issues/N" | jq '.pull_request != null'
# GitHub: gh api repos/OWNER/REPO/issues/N --jq '.pull_request != null'
# If true → #N is a PR, should be "PR #N" with /pulls/N URL
```

**Then read the generated changelog and cross-check:**
- Does commit count in Statistics table match `git log` output?
- Do file/line stats match `git diff --stat`?
- Do **per-file** stats in Section 9 match `git diff --numstat`? (most common LLM error!)
- Are all `#N` references correctly typed (issue vs PR)?
- Do test counts match `grep -c`?
- Does PR description match changelog claims? (read PR body with `gh pr view N --json body`)

**If discrepancies found**, use AskUserQuestion:

```json
{
  "questions": [{
    "question": "Changelog содержит неточности относительно реальных данных git. Как поступить?",
    "header": "Discrepancy",
    "options": [
      {"label": "Исправить автоматически", "description": "Обновить changelog фактическими данными из git"},
      {"label": "Показать diff", "description": "Показать расхождения для ручного решения"},
      {"label": "Оставить как есть", "description": "Коммитить changelog без изменений"}
    ],
    "multiSelect": false
  }]
}
```

If user chooses "Исправить автоматически" — fix the changelog file and report what was changed.

**Also check PR description.** If PR body has stale data (wrong test count, wrong stats), ask:

```json
{
  "questions": [{
    "question": "PR description тоже содержит устаревшие данные. Обновить?",
    "header": "PR body",
    "options": [
      {"label": "Да, обновить PR", "description": "gh pr edit N --body с актуальными данными"},
      {"label": "Нет, только changelog", "description": "PR description останется без изменений"}
    ],
    "multiSelect": false
  }]
}
```

## Step 5: Ask About Commit

**Use AskUserQuestion to ask if user wants to commit the changelog:**

```json
{
  "questions": [{
    "question": "Закоммитить сгенерированный changelog в текущую ветку?",
    "header": "Commit",
    "options": [
      {"label": "Да, закоммитить", "description": "git add + git commit + git push"},
      {"label": "Нет, оставить untracked", "description": "Файл останется для ручного review"}
    ],
    "multiSelect": false
  }]
}
```

**If user chooses "Да, закоммитить":**
```bash
git add docs/*BRANCH_CHANGELOG*.md
git commit -m "docs: Add branch changelog for $(git rev-parse --abbrev-ref HEAD)"
git push
```

**If user chooses "Нет":**
Just inform the user the file is ready for manual review.
