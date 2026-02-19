# Quality Checklist

## Pre-Save Validation (12 checkpoints)

Agent MUST verify ALL items before saving changelog:

### Content Quality
- [ ] Executive Summary includes bullet points with emoji
- [ ] All bugs have Root Cause analysis (Symptom -> Cause -> Solution + code)
- [ ] Mermaid diagrams match PR size requirements (1/3/5+ based on file count)
- [ ] Commit Type Distribution table present
- [ ] All #issue references extracted from commit messages
- [ ] Files grouped by layers/modules

### Formatting
- [ ] Language matches `--lang` parameter (ru default, en if specified)
- [ ] **All #N are clickable links: `[#N](url) (url)`**
- [ ] **Filename starts with ISO 8601 date**

### Verification
- [ ] **Fact-checking passed (git stats/commits verified)**
- [ ] **Code Verification passed (methods, endpoints, events verified)**
- [ ] **Verification Report section added in footer**

---

## Fact-Checking Commands

**Before saving, VERIFY with actual git commands:**

```bash
# 1. Verify git stats
git diff TARGET..HEAD --stat | tail -1
# Compare with your "X files, +Y/-Z" claims

# 2. Verify commit count
git log TARGET..HEAD --oneline | wc -l
# Must match your commit count

# 3. Verify test count (if mentioned)
grep -c "def test_" tests/FILE.py
# Must match any test count claims

# 4. Verify commit hashes exist
git cat-file -t abc123
# Must return "commit" for each hash mentioned
```

---

## Working Links Format

**All #N references MUST be clickable links with full URL visible:**

```markdown
[#45](http://repo-url/issues/45) (http://repo-url/issues/45)
```

### Auto-detect Repository URL

```bash
# Step 1: Get git remote URL
REMOTE=$(git remote get-url origin)

# Step 2: Check environment for base URL
# $FORGEJO_API_URL -> extract base (remove /api/v1)
# $GITHUB_URL -> use directly

# Step 3: Build link
REPO_URL="http://192.168.31.116:3300/0_INFRA/SETUP"
ISSUE_LINK="[#45](${REPO_URL}/issues/45) (${REPO_URL}/issues/45)"
PR_LINK="[PR #51](${REPO_URL}/pulls/51) (${REPO_URL}/pulls/51)"
```

### Link Conversion Rules

| Reference | URL Pattern | Example |
|-----------|-------------|---------|
| Issue #N | `/issues/{N}` | `[#45](url/issues/45) (url/issues/45)` |
| PR #N | `/pulls/{N}` | `[PR #51](url/pulls/51) (url/pulls/51)` |
| Commit hash | `/commit/{hash}` | `[abc123](url/commit/abc123)` |

**Apply to ALL occurrences of #N in the document!**

---

## Verification Section Template

Include in document footer:

```markdown
---

## Verification

| Check | Command | Result |
|-------|---------|--------|
| Git stats | `git diff --stat \| tail -1` | 3 files, +233/-71 |
| Commits | `git log --oneline \| wc -l` | 3 commits |
| Test count | `grep -c "def test_"` | 10 tests |
```

**If verification fails, FIX the document before saving!**

---

## Quality Standards

- **Accuracy**: Every fact must come from actual diff AND be verified
- **Completeness**: Don't skip significant changes
- **Clarity**: Write for developers who didn't make the changes
- **Actionability**: Migration notes must be specific and executable
- **Scannable**: Use tables, bullets, emoji for quick reading
- **Links**: All #N references must be clickable with visible URL
