# vendor-analyzer

Exhaustive vendor codebase analysis with 5-agent pipeline generating Obsidian-compatible documentation vault.

## Overview

vendor-analyzer solves the problem of understanding third-party code by creating comprehensive, navigable documentation through automated analysis.

**Use cases:**
- Onboarding new developers to unfamiliar codebases
- Finding where to make patches and modifications
- Understanding architecture for forking or replacing
- Auditing security and code quality

## Pipeline

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ va-inventory │───▶│ va-structure │───▶│ va-deps      │───▶│ va-algorithms│───▶│ va-report    │
│              │    │              │    │              │    │              │    │              │
│ Files, sizes │    │ Modules,     │    │ Dependency   │    │ Core logic,  │    │ Obsidian     │
│ languages    │    │ entry points │    │ graph        │    │ flowcharts   │    │ vault        │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

## Quick Start

```bash
# Analyze a vendor codebase
/analyze vendors/serena

# With specific depth
/analyze vendors/serena --depth exhaustive
```

## Agents

| Agent | Phase | Purpose |
|-------|-------|---------|
| `va-inventory` | 1 | File inventory, languages, git history, licenses |
| `va-structure` | 2 | Module boundaries, entry points, exports/imports |
| `va-dependencies` | 3 | Dependency graph, CVE check, circular detection |
| `va-algorithms` | 4 | Core logic, pseudocode, flowcharts, complexity |
| `va-report` | 5 | Executive summary, navigation, link validation |

## Depth Levels

| Level | Time | Files | Description |
|-------|------|-------|-------------|
| `surface` | ~30min | 10-15 | Entry points, public API |
| `standard` | ~2-3h | 50-80 | All modules, main algorithms |
| `deep` | ~8h | 200+ | Every function, edge cases |
| `exhaustive` | ~24h+ | 500+ | Line-by-line, git history |

## Output

Analysis creates an Obsidian-compatible vault in `<target>/.analysis/`:

```
.analysis/
├── 00-index.md           # Main entry point
├── EXECUTIVE-SUMMARY.md  # For stakeholders
├── GETTING-STARTED.md    # For new developers
├── _metadata/
│   └── inventory.md      # File manifest
├── modules/              # Module documentation
├── deps/                 # Dependency analysis
└── algorithms/           # Algorithm documentation
```

## Features

- **[[Wiki-links]]** for navigation between docs
- **#tags** for categorization
- **YAML frontmatter** for metadata
- **Mermaid diagrams** for visualization
- **Obsidian Graph View** compatible

## Cost Estimate

For 50k LOC project on Opus:

| Depth | Estimated Cost |
|-------|----------------|
| Surface | $5-10 |
| Standard | $20-30 |
| Deep | $50-70 |
| Exhaustive | $100-150 |

## Supported Languages

- Python
- TypeScript/JavaScript
- Go
- Rust
- Java

## Requirements

- Claude Code with Opus model access
- Target codebase accessible locally
- (Optional) Obsidian for viewing results

## Development

This plugin is part of jaine-plugins, developed on the `jaine-plugins/main` branch.

```bash
# Location
/0/ANTHROPICS_DEV/jaine-plugins/plugins/vendor-analyzer/

# Test locally
cc --plugin-dir /0/ANTHROPICS_DEV/jaine-plugins
```

## License

MIT
