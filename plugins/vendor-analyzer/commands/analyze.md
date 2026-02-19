---
description: Run exhaustive vendor codebase analysis pipeline with 5 sequential agents
argument-hint: <path> [--depth surface|standard|deep|exhaustive]
allowed-tools: ["Task", "TodoWrite", "Read", "Write", "Glob", "Grep", "LS", "Bash"]
---

# Vendor Analysis Pipeline

Run comprehensive codebase analysis using 5 specialized agents in sequence.

## Arguments

- `<path>` - Target directory to analyze (required)
- `--depth` - Analysis depth level (default: standard)
  - `surface` - Entry points only (~30 min, 10-15 files)
  - `standard` - All modules, main algorithms (~2-3h, 50-80 files)
  - `deep` - Every function, edge cases (~8h, 200+ files)
  - `exhaustive` - Line-by-line, git history (~24h+, 500+ files)

## Pipeline Overview

```
Phase 1: va-inventory → Phase 2: va-structure → Phase 3: va-dependencies → Phase 4: va-algorithms → Phase 5: va-report
```

Each phase MUST complete before the next begins. Results are stored in `<target>/.analysis/`.

---

## Execution Instructions

### Step 0: Parse Arguments

Extract from $ARGUMENTS:
- `target_path` - The directory to analyze
- `depth` - The depth level (default: standard)

If no path provided, ask user for target directory.

### Step 1: Create Todo List

Use TodoWrite to create pipeline tracking:

```
1. [pending] Phase 1: Inventory (va-inventory)
2. [pending] Phase 2: Structure (va-structure)
3. [pending] Phase 3: Dependencies (va-dependencies)
4. [pending] Phase 4: Algorithms (va-algorithms)
5. [pending] Phase 5: Report (va-report)
```

### Step 2: Validate Target

Check that target directory exists:
```bash
ls -la <target_path>
```

If not found, report error and stop.

### Step 3: Phase 1 - Inventory

Mark Phase 1 as `in_progress` in todos.

Launch va-inventory agent:
```
Analyze codebase at <target_path> with depth <depth>.
Create inventory at <target_path>/.analysis/_metadata/inventory.md
```

Wait for completion. Verify output exists:
```bash
ls <target_path>/.analysis/_metadata/inventory.md
```

Mark Phase 1 as `completed`.

### Step 4: Phase 2 - Structure

Mark Phase 2 as `in_progress`.

Launch va-structure agent:
```
Analyze module structure at <target_path>.
Read inventory from .analysis/_metadata/inventory.md.
Create module docs in .analysis/modules/.
```

Wait for completion. Verify outputs:
```bash
ls <target_path>/.analysis/modules/
ls <target_path>/.analysis/00-structure.md
```

Mark Phase 2 as `completed`.

### Step 5: Phase 3 - Dependencies

Mark Phase 3 as `in_progress`.

Launch va-dependencies agent:
```
Analyze dependencies at <target_path>.
Build internal and external dependency graphs.
Create dependency docs in .analysis/deps/.
```

Wait for completion. Verify outputs:
```bash
ls <target_path>/.analysis/deps/
ls <target_path>/.analysis/00-dependencies.md
```

Mark Phase 3 as `completed`.

### Step 6: Phase 4 - Algorithms

Mark Phase 4 as `in_progress`.

Launch va-algorithms agent:
```
Document core algorithms at <target_path>.
Create algorithm docs with pseudocode and flowcharts.
Output to .analysis/algorithms/.
```

Wait for completion. Verify outputs:
```bash
ls <target_path>/.analysis/algorithms/
ls <target_path>/.analysis/00-algorithms.md
```

Mark Phase 4 as `completed`.

### Step 7: Phase 5 - Report

Mark Phase 5 as `in_progress`.

Launch va-report agent:
```
Synthesize final report at <target_path>.
Create executive summary and validate all links.
Output 00-index.md, EXECUTIVE-SUMMARY.md, GETTING-STARTED.md.
```

Wait for completion. Verify outputs:
```bash
ls <target_path>/.analysis/00-index.md
ls <target_path>/.analysis/EXECUTIVE-SUMMARY.md
ls <target_path>/.analysis/GETTING-STARTED.md
```

Mark Phase 5 as `completed`.

### Step 8: Final Summary

Output completion message:

```
🎉 Vendor Analysis Complete!

📁 Output: <target_path>/.analysis/

📊 Statistics:
   - Modules: N
   - Algorithms: N
   - Dependencies: N

📖 Key Files:
   - 00-index.md - Start here
   - EXECUTIVE-SUMMARY.md - Quick overview
   - GETTING-STARTED.md - For new developers

💡 Tip: Open .analysis/ folder in Obsidian for best experience.
```

---

## Error Handling

If any phase fails:
1. Log error with context
2. Mark phase as failed in todos
3. Ask user how to proceed:
   - Retry the failed phase
   - Skip to next phase
   - Abort pipeline

Do NOT continue automatically if a phase fails.

---

## Example Usage

```
/analyze vendors/serena
/analyze vendors/serena --depth exhaustive
/analyze /path/to/project --depth surface
```

---

## Notes

- Each agent runs with `model: opus` for deep analysis
- Full `exhaustive` run on 50k LOC takes ~24 hours and costs ~$100-150
- Output is Obsidian-compatible with [[wiki-links]] and #tags
- All phases must complete in order; no skipping
