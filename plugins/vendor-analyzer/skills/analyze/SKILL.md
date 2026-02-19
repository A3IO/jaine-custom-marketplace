---
name: analyze
description: >
  Use this skill when the user asks to "analyze a vendor codebase",
  "understand third-party code", "review vendor architecture",
  "document vendor dependencies", or "create codebase documentation".
  Triggers on mentions of vendor analysis, codebase documentation,
  or understanding unfamiliar/external codebases.
---

# Vendor Codebase Analysis

Use this skill when analyzing third-party/vendor codebases to understand their architecture, dependencies, and algorithms. The vendor-analyzer provides exhaustive documentation in Obsidian-compatible format.

## Quick Start

```bash
# Analyze a vendor codebase
/analyze vendors/serena

# With specific depth
/analyze vendors/serena --depth exhaustive
```

## Available Agents

| Agent | Phase | Purpose |
|-------|-------|---------|
| `va-inventory` | 1 | File inventory, languages, git history |
| `va-structure` | 2 | Module boundaries, entry points, exports |
| `va-dependencies` | 3 | Dependency graph, security audit |
| `va-algorithms` | 4 | Core logic, pseudocode, flowcharts |
| `va-report` | 5 | Final synthesis, executive summary |

## Depth Levels

| Level | Time | Files | Use Case |
|-------|------|-------|----------|
| `surface` | ~30min | 10-15 | Quick overview |
| `standard` | ~2-3h | 50-80 | Normal analysis |
| `deep` | ~8h | 200+ | Thorough review |
| `exhaustive` | ~24h+ | 500+ | Complete audit |

## Output Structure

After analysis, find results in `<target>/.analysis/`:

```
.analysis/
├── 00-index.md           # Main entry point
├── EXECUTIVE-SUMMARY.md  # For stakeholders
├── GETTING-STARTED.md    # For new developers
├── 00-structure.md       # Architecture overview
├── 00-dependencies.md    # Dependency summary
├── 00-algorithms.md      # Algorithm index
├── _metadata/
│   └── inventory.md      # File manifest
├── modules/
│   └── *.md              # Module documentation
├── deps/
│   ├── internal.md       # Internal dependencies
│   └── external.md       # External packages
└── algorithms/
    └── *.md              # Algorithm documentation
```

## Obsidian Integration

Output uses Obsidian features:
- `[[wiki-links]]` for navigation
- `#tags` for categorization
- YAML frontmatter for metadata
- Mermaid diagrams for visualization

Open `.analysis/` folder as Obsidian vault for best experience.

## Cost Estimate

For 50k LOC project on Opus:
- Surface: ~$5-10
- Standard: ~$20-30
- Deep: ~$50-70
- Exhaustive: ~$100-150

## Tips

1. **Start with surface** for initial understanding
2. **Use standard** for most projects
3. **Reserve exhaustive** for critical vendor code
4. **Open in Obsidian** for graph view and navigation
