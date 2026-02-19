# Changelog Examples

This directory contains templates and examples for the `/changelog-before-merge` skill.

## Files

| File | Purpose | When to Use |
|------|---------|-------------|
| `TEMPLATE.md` | Clean template with placeholders | Copy and fill in manually |
| `EXAMPLE_ANNOTATED.md` | Educational example with `<!-- GUIDANCE -->` comments | Learn the format, understand best practices |
| `REAL_WORLD_jaine-speech.md` | Actual PR changelog from production | See real output, build confidence |

## Why Three Files?

| Type | Audience | Value |
|------|----------|-------|
| **Template** | Developers | Quick start, copy-paste ready |
| **Annotated** | Learners | Understand when/why to include each section |
| **Real-world** | Skeptics | "This actually works in production" |

## Quick Start

1. Run `/changelog-before-merge` — skill generates changelog automatically
2. Or copy `TEMPLATE.md` and fill manually

## Template Placeholders

| Placeholder | Replace With | Source |
|-------------|--------------|--------|
| `{{BRANCH}}` | Current branch name | `git rev-parse --abbrev-ref HEAD` |
| `{{TARGET}}` | Target branch | Usually `main` |
| `{{DATE}}` | Generation date | `date +%Y-%m-%d` |
| `{{FILES_CHANGED}}` | Number of files | `git diff --stat \| tail -1` |
| `{{COMMIT_COUNT}}` | Number of commits | `git log TARGET..HEAD --oneline \| wc -l` |

## Conditional Sections

Not every PR needs every section. Use this guide:

| Section | Include When |
|---------|--------------|
| Architecture Changes | New modules, changed dependencies, structural refactoring |
| Bug Fixes | Any `fix:` commits |
| New Features | Any `feat:` commits |
| Breaking Changes | API changes, removed functionality, changed behavior |
| Migration Notes | Post-merge actions required |

## Mermaid Diagram Types

| Type | Use For | Example |
|------|---------|---------|
| `flowchart` | Component relationships, module structure | Architecture overview |
| `sequenceDiagram` | Request/response flows, API interactions | Auth flow |
| `classDiagram` | Data models, class relationships | Model changes |
| `erDiagram` | Database schema changes | New tables |
| `gitGraph` | Branch/merge visualization | Complex merge strategy |

## Best Practices

1. **Executive Summary** — Write for stakeholders, not developers
2. **Commit History** — Group by date for multi-day PRs
3. **Bug Fixes** — Always include root cause, not just solution
4. **Breaking Changes** — Always provide migration path
5. **Diagrams** — One diagram worth 1000 words for architecture
