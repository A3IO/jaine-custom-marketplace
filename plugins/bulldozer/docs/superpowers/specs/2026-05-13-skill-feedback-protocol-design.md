# Skill Feedback Protocol — Design Spec

## Problem

JAINE uses bulldozer skills (look, check) across many projects. When she encounters friction — missing capabilities, documentation gaps, unexpected behavior — that feedback is lost when the conversation compacts. The only way to surface problems today is for Chris to manually read session transcripts and create issues (as happened with issue #46).

## Solution

A lightweight feedback protocol where JAINE-consumer creates structured GitHub Issues when she encounters skill friction. JAINE-developer (working in the bulldozer repo) reads those issues and fixes the skills in real-time: Chris immediately opens a dev session, JAINE-developer applies the fix, bumps the version, and refreshes the cache so the consumer can continue with the updated skill.

No new infrastructure. No files, YAML, hooks, or automation. Just instructions in SKILL.md and CLAUDE.md, and `gh issue create`.

## Architecture

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| Consumer instructions | SKILL.md (look) + SKILL.md (check) | "Feedback" section with issue template, triggers, `gh issue create` command |
| Developer instructions | CLAUDE.md (bulldozer) | "Feedback Protocol" section: check issues before working on skills |
| Storage | GitHub Issues in A3IO/jaine-plugins | Labels: `feedback` + `bulldozer` + `look`/`check` |

### Why SKILL.md

SKILL.md content loads when JAINE invokes a skill. This is exactly when she needs the feedback instruction — while using the skill and potentially encountering friction. No overhead for sessions that don't use bulldozer.

CLAUDE.md of the plugin only loads when pwd is inside the bulldozer directory (developer context), not when using the skill from another project (consumer context).

Alternative considered: SessionStart hook (superpowers pattern) would inject a reminder into every session, surviving compaction. Rejected because: (1) overhead in all sessions even when bulldozer is unused, (2) feedback instruction is only relevant during active skill use, (3) if compaction proves to be a real problem, we can evolve to SessionStart later.

### Data flow

```
JAINE-consumer (in GOATsEXPLORER, JTerm2, ...)
  ↓ uses /bulldozer:look
  ↓ SKILL.md loads (includes Feedback section)
  ↓ encounters friction
  ↓ creates GitHub Issue (gh issue create --label feedback,bulldozer,look)
  ↓ tells Chris: "created issue, fix now or continue?"

Chris opens JAINE-developer session (in bulldozer repo)
  ↓ CLAUDE.md loads (includes Feedback Protocol)
  ↓ checks gh issue list --label feedback,bulldozer
  ↓ reads issue, fixes skill/code/docs
  ↓ closes issue with fix commit reference
  ↓ plugin version bumped, cache refreshed

JAINE-consumer continues with updated skill
```

## Issue format

### Title

```
[feedback/look] cmd_screenshot обрезает нижнюю часть длинной страницы
[feedback/check] Codex timeout не описан в SKILL.md
[feedback/look] feature request: поддержка --full-page в screenshot
[feedback/new-skill] нужен скилл для профилирования JS performance
```

Prefix `[feedback/{skill}]` distinguishes from manually-created issues.

### Labels

- `feedback` — issue type
- `bulldozer` — plugin
- `look` or `check` — specific skill (omit for `new-skill` requests)

### Body template

```markdown
## What I was doing
{task description and context}

## What I expected
{expected behavior based on SKILL.md or reasonable assumption}

## What happened
{actual behavior, error messages, unexpected output}

## Workaround used
{what was done instead, or "none — blocked"}

## Environment
- Plugin version: {from $CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json}
- Skill: {look|check}
- Project: {current working directory}
```

### Command

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,{skill}" \
  --title "[feedback/{skill}] short description" \
  --body "$(cat <<'EOF'
## What I was doing
...
## What I expected
...
## What happened
...
## Workaround used
...
## Environment
- Plugin version: ...
- Skill: look
- Project: /0/SANDBOX/ASSISTS/GOATsEXPLORER
EOF
)"
```

## Triggers

### Create issue when

1. **Documentation mismatch** — SKILL.md describes behavior X, reality is Y
2. **Workaround required** — had to use a non-standard path to accomplish the task
3. **Missing capability** — need feature that doesn't exist (feature request)
4. **Unclear error** — script failed with unhelpful error message
5. **New skill needed** — no existing bulldozer skill covers this use case

### Do NOT create issue when

- Own mistake in arguments (wrong selector, typo in URL)
- External problem (browser not running, network down)
- Behavior already documented as a known limitation in SKILL.md

Rule: issues are created only when **the skill or its documentation** is at fault, not the environment or user.

## Consumer behavior after creating issue

After creating the issue, JAINE tells Chris:

> "I created a feedback issue about the {skill} skill: {URL}.
> Want me to continue with a workaround, or would you like to get this fixed first?"

Chris decides: fix now (opens developer session) or continue (JAINE uses workaround).

## Developer workflow

In CLAUDE.md bulldozer:

```markdown
## Feedback Protocol

Before working on skill improvements, check for feedback issues:

    gh issue list --repo A3IO/jaine-plugins --label feedback,bulldozer --state open

When fixing a feedback issue:
1. Read the issue body for reproduction context
2. Fix the skill/code/documentation
3. Bump plugin version in .claude-plugin/plugin.json
4. Refresh consumer's plugin cache (jaine-sync plugins update or rm -rf ~/.claude/plugins/cache/jaine-custom/bulldozer/)
5. Close the issue with a reference to the fix commit

CRITICAL: Steps 3-4 prevent stale cache — the root cause of false positives
in issue #46 where 3/6 feedback items were invalid because JAINE-consumer
was running an old plugin version.
```

## Scope

MVP: bulldozer only (look + check skills). Can be extended to other plugins later by adding the same Feedback section to their SKILL.md files.

## What this is NOT

- Not a logging system (bulldozer-look.log already handles operational telemetry)
- Not automatic (JAINE decides when friction warrants an issue)
- Not a replacement for manual postmortems (complements them)
- Not a SessionStart hook or persistent reminder (loads only when skill is used)
