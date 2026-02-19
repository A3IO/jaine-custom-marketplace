---
description: Remove cl/clp functions and optionally CLP profile
allowed-tools: ["Bash", "Read", "Edit", "AskUserQuestion"]
---

# Uninstall Claude Light

Remove `cl` and `clp` shell functions.

## Pre-flight Check

Check if installed:

```bash
grep -q 'cl() { source ~/.claude/cl.sh' ~/.zshrc && echo "INSTALLED" || echo "NOT_INSTALLED"
```

If `NOT_INSTALLED`, show message and stop:
```
ℹ️ Claude Light is not installed (functions not found in ~/.zshrc).
```

## Uninstall Steps

### Step 1: Remove Claude Light block from .zshrc

1. Use Read tool to read `~/.zshrc`
2. Use Edit tool to remove the Claude Light block:

**old_string:**
```
# Claude Light - minimal config with task list picker
cl() { source ~/.claude/cl.sh "$@"; }
clp() { CL_MODE=plugins source ~/.claude/cl.sh "$@"; }
# cl/clp completion (sourced after compinit)
[[ -f ~/.claude/cl-completion.zsh ]] && source ~/.claude/cl-completion.zsh

```

**new_string:** (empty string)
```
```

**Note:** Keep the JAINE PLUGINS section markers — other plugins may use them.

### Step 2: Check if JAINE PLUGINS section is now empty

Use Bash tool:
```bash
sed -n '/# === JAINE PLUGINS START ===/,/# === JAINE PLUGINS END ===/p' ~/.zshrc | grep -v '^#' | grep -v '^$' | wc -l
```

If result is `0` (section empty), ask user:

Use AskUserQuestion tool:
```
question: "JAINE PLUGINS section is now empty. Remove it?"
options:
  - label: "Yes, remove empty section"
    description: "Clean up .zshrc"
  - label: "No, keep section"
    description: "Keep markers for future plugins"
```

If "Yes", use Edit tool:

**old_string:**
```
# === JAINE PLUGINS START ===
# Managed by JAINE plugins. Do not edit manually.

# === JAINE PLUGINS END ===
```

**new_string:** (empty)

### Step 3: Ask about cl.sh and completion removal

Use AskUserQuestion tool:
```
question: "Remove cl.sh script and completion?"
options:
  - label: "Yes, remove"
    description: "Delete cl.sh and cl-completion.zsh"
  - label: "No, keep"
    description: "Keep scripts for manual use"
```

If "Yes", use Bash tool:
```bash
rm -f ~/.claude/cl.sh ~/.claude/cl-completion.zsh
```

### Step 4: Ask about CLP profile removal

Use AskUserQuestion tool:
```
question: "Remove CLP profile directory?"
options:
  - label: "Yes, remove"
    description: "Delete /0/.staff/CLP/ entirely"
  - label: "No, keep"
    description: "Keep the CLP profile"
```

If "Yes", use Bash tool:
```bash
rm -rf /0/.staff/CLP
```

### Step 5: Clear completion cache

Use Bash tool:
```bash
rm -f ~/.zcompdump* 2>/dev/null; echo "Completion cache cleared"
```

### Step 6: Verify

Use Bash tool:
```bash
grep "cl()" ~/.zshrc || echo "Functions removed successfully"
```

## Success Message

```
✅ Claude Light uninstalled.

Removed:
- cl/clp functions from ~/.zshrc
- ~/.claude/cl.sh (if confirmed)
- ~/.claude/cl-completion.zsh (if confirmed)
- /0/.staff/CLP/ (if confirmed)

Run: source ~/.zshrc
```
