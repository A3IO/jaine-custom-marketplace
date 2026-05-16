# Skill Feedback Protocol — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Feedback section to both bulldozer SKILL.md files and a Feedback Protocol section to CLAUDE.md, so JAINE-consumer can create structured GitHub Issues when encountering friction, and JAINE-developer knows to check them.

**Architecture:** Three doc-only edits. Consumer instructions go at the end of each SKILL.md (look, check). Developer instructions go in CLAUDE.md before "Known Issues". No new files, no code, no hooks.

**Tech Stack:** Markdown, `gh` CLI

**Spec:** `docs/superpowers/specs/2026-05-13-skill-feedback-protocol-design.md`

---

### Task 1: Add Feedback section to SKILL.md (look)

**Files:**
- Modify: `skills/look/SKILL.md` — append at end of file (after the Fallback Matrix section)

- [ ] **Step 1: Add the Feedback section**

Append the following at the end of `skills/look/SKILL.md`:

```markdown

## Feedback

If you encounter friction while using this skill — documentation mismatch, missing capability, unclear error, or need a workaround — create a GitHub issue so JAINE-developer can fix it in real-time.

**Create issue when:**
1. SKILL.md describes behavior X, reality is Y
2. Had to use a workaround instead of the standard path
3. Need a feature that doesn't exist
4. Script failed with an unhelpful error message
5. No existing bulldozer skill covers the use case (use `[feedback/new-skill]` prefix)

**Do NOT create issue when:** own mistake in arguments, external problem (browser not running), or behavior documented as a known limitation.

**Command:**

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,look" \
  --title "[feedback/look] short description" \
  --body "$(cat <<ISSUE
## What I was doing
{task description}

## What I expected
{expected behavior}

## What happened
{actual behavior, errors}

## Workaround used
{what was done instead, or "none — blocked"}

## Environment
- Plugin version: $(jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json")
- Skill: look
- Project: $(pwd)
ISSUE
)"
```

**For new-skill requests (trigger #5):** use title prefix `[feedback/new-skill]`, labels `feedback,bulldozer` (omit `look`).

After creating the issue, tell the user:
> "I created a feedback issue about the look skill: {URL}. Want me to continue with a workaround, or would you like to get this fixed first?"
```

- [ ] **Step 2: Verify the section renders correctly**

Read the file back and confirm: section appears after Fallback Matrix, no broken markdown nesting (the nested code block uses triple-backtick with `bash` inside a section that itself is not in a code block).

- [ ] **Step 3: Commit**

```bash
git add skills/look/SKILL.md
git commit -m "docs(look): add Feedback section for consumer issue creation"
```

---

### Task 2: Add Feedback section to SKILL.md (check)

**Files:**
- Modify: `skills/check/SKILL.md` — append at end of file (after "Integration with Other Skills" section)

- [ ] **Step 1: Add the Feedback section**

Append the following at the end of `skills/check/SKILL.md`:

```markdown

## Feedback

If you encounter friction while using this skill — documentation mismatch, missing capability, unclear error, or need a workaround — create a GitHub issue so JAINE-developer can fix it in real-time.

**Create issue when:**
1. SKILL.md describes behavior X, reality is Y
2. Had to use a workaround instead of the standard path
3. Need a feature that doesn't exist
4. Script failed with an unhelpful error message
5. No existing bulldozer skill covers the use case (use `[feedback/new-skill]` prefix)

**Do NOT create issue when:** own mistake in arguments, external problem (Codex CLI not installed, network down), or behavior documented as a known limitation.

**Command:**

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,check" \
  --title "[feedback/check] short description" \
  --body "$(cat <<ISSUE
## What I was doing
{task description}

## What I expected
{expected behavior}

## What happened
{actual behavior, errors}

## Workaround used
{what was done instead, or "none — blocked"}

## Environment
- Plugin version: $(jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json")
- Skill: check
- Project: $(pwd)
ISSUE
)"
```

After creating the issue, tell the user:
> "I created a feedback issue about the check skill: {URL}. Want me to continue with a workaround, or would you like to get this fixed first?"
```

- [ ] **Step 2: Verify the section renders correctly**

Read the file back and confirm section appears after "Integration with Other Skills".

- [ ] **Step 3: Commit**

```bash
git add skills/check/SKILL.md
git commit -m "docs(check): add Feedback section for consumer issue creation"
```

---

### Task 3: Add Feedback Protocol to CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` — insert new section before the `## Known Issues` heading
- Modify: `.claude-plugin/plugin.json` — bump CalVer version

- [ ] **Step 1: Add the Feedback Protocol section**

Insert the following before the "## Known Issues" section in `CLAUDE.md`:

```markdown
## Feedback Protocol

Before working on skill improvements, check for feedback issues from other JAINE sessions:

```bash
gh issue list --repo A3IO/jaine-plugins --label feedback,bulldozer --state open
```

Feedback issues are created by JAINE-consumer sessions that encountered friction while using bulldozer skills. Each issue follows a structured template: what was attempted, what went wrong, workaround used, and plugin version.

When fixing a feedback issue:
1. Read the issue body for reproduction context
2. Fix the skill/code/documentation
3. Bump plugin version in `.claude-plugin/plugin.json`
4. Refresh consumer's plugin cache (`jaine-sync plugins update` or `rm -rf ~/.claude/plugins/cache/jaine-custom/bulldozer/`)
5. Close the issue with a reference to the fix commit

**CRITICAL:** Steps 3-4 prevent stale cache — the root cause of false positives in issue #46 where 3/6 feedback items were invalid because JAINE-consumer was running an old plugin version.
```

- [ ] **Step 2: Bump versions**

Edit the version footer at the end of `CLAUDE.md`:

```
*Version: 1.7.0 | Last Updated: 2026-05-13*
```

Bump `.claude-plugin/plugin.json` version to `2026.05.13` (or `2026.05.13.1` if same-day merge with other changes).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .claude-plugin/plugin.json
git commit -m "docs: add Feedback Protocol to CLAUDE.md for developer workflow

JAINE-developer checks gh issue list --label feedback,bulldozer
before working on skills. Workflow includes version bump and
cache refresh to prevent stale cache (issue #46 root cause)."
```

---

### Task 4: Verify end-to-end consistency

- [ ] **Step 1: Check all three files have matching content**

Verify that:
- Both SKILL.md files have identical trigger lists (5 items)
- Both SKILL.md files have matching body template fields (5 sections)
- Both SKILL.md files reference the same repo (`A3IO/jaine-plugins`)
- CLAUDE.md developer workflow step count matches spec (5 steps)
- Labels in SKILL.md `--label` args match what CLAUDE.md `--label` filters

```bash
# Extract Feedback sections and diff (should differ only in skill name look/check)
sed -n '/^## Feedback/,$ p' skills/look/SKILL.md > /tmp/fb-look.md
sed -n '/^## Feedback/,$ p' skills/check/SKILL.md > /tmp/fb-check.md
diff /tmp/fb-look.md /tmp/fb-check.md

# Verify labels match between SKILL.md commands and CLAUDE.md filter
grep -o 'label "[^"]*"' skills/look/SKILL.md skills/check/SKILL.md
grep "label" CLAUDE.md | grep feedback

# Verify trigger count
grep -c "^\d\." /tmp/fb-look.md /tmp/fb-check.md
```

- [ ] **Step 2: Verify no spec requirements missed**

Cross-check against spec sections:
- [x] Consumer instructions in SKILL.md (look) — Task 1
- [x] Consumer instructions in SKILL.md (check) — Task 2
- [x] Developer instructions in CLAUDE.md — Task 3
- [x] Issue title format with `[feedback/{skill}]` prefix — in both SKILL.md
- [x] Labels: `feedback` + `bulldozer` + `{skill}` — in both SKILL.md
- [x] Body template with 5 sections — in both SKILL.md
- [x] 5 triggers + "do NOT" list — in both SKILL.md
- [x] Consumer message to Chris — in both SKILL.md
- [x] Developer workflow with 5 steps including version bump + cache refresh — in CLAUDE.md
- [x] CRITICAL note about issue #46 stale cache — in CLAUDE.md
- [x] `new-skill` title prefix for trigger #5 — in both SKILL.md
- [x] `$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json` path — in both SKILL.md
- [x] Neutral phrasing to Chris — in both SKILL.md
