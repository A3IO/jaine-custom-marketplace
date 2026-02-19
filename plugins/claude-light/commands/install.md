---
description: Install cl/clp functions and CLP profile
allowed-tools: ["Bash", "Read", "Write", "Edit", "Glob"]
---

# Install Claude Light

Install `cl` and `clp` shell functions for lightweight Claude Code launching.

## Pre-flight Check

Check if claude-light is already installed:

```bash
grep -q 'cl() { source ~/.claude/cl.sh' ~/.zshrc && echo "ALREADY_INSTALLED" || echo "NOT_INSTALLED"
```

If `ALREADY_INSTALLED`, show message and stop:
```
ℹ️ Claude Light is already installed.

To reinstall: Run /claude-light:uninstall first
```

## Installation Steps

### Step 1: Copy cl.sh script

Use Bash tool:
```bash
cp "${CLAUDE_PLUGIN_ROOT}/scripts/cl.sh" ~/.claude/cl.sh && chmod +x ~/.claude/cl.sh
```

### Step 2: Install completion

Use Bash tool:
```bash
cp "${CLAUDE_PLUGIN_ROOT}/scripts/completion.zsh" ~/.claude/cl-completion.zsh
```

### Step 3: Create CLP profile directory

Use Bash tool:
```bash
mkdir -p /0/.staff/CLP/.claude
```

### Step 4: Create symlink to plugins

Use Bash tool:
```bash
ln -sf "$HOME/.claude/plugins" /0/.staff/CLP/.claude/plugins
```

### Step 5: Create CLP settings.json

1. Use Read tool to read `~/.claude/settings.json`
2. Extract the `enabledPlugins` object
3. Use Write tool to create `/0/.staff/CLP/.claude/settings.json`:

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "env": {
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "enabledPlugins": {
    <COPY_FROM_USER_SETTINGS>
  },
  "language": "Русский",
  "defaultMode": "bypassPermissions"
}
```

### Step 6: Add functions to .zshrc (with JAINE PLUGINS section)

#### Step 6a: Check if JAINE PLUGINS section exists

Use Bash tool:
```bash
grep -q '# === JAINE PLUGINS START ===' ~/.zshrc && echo "SECTION_EXISTS" || echo "NO_SECTION"
```

#### Step 6b: If NO_SECTION — create section at end of file

Use Read tool to read `~/.zshrc`, then use Edit tool:

**old_string:** (last few lines of file — read file to find them)

**new_string:** (same lines + new section)
```
<LAST_LINES_OF_FILE>

# === JAINE PLUGINS START ===
# Managed by JAINE plugins. Do not edit manually.

# Claude Light - minimal config with task list picker
cl() { source ~/.claude/cl.sh "$@"; }
clp() { CL_MODE=plugins source ~/.claude/cl.sh "$@"; }
# cl/clp completion (sourced after compinit)
[[ -f ~/.claude/cl-completion.zsh ]] && source ~/.claude/cl-completion.zsh

# === JAINE PLUGINS END ===
```

#### Step 6c: If SECTION_EXISTS — add before END marker

Use Edit tool:

**old_string:**
```
# === JAINE PLUGINS END ===
```

**new_string:**
```
# Claude Light - minimal config with task list picker
cl() { source ~/.claude/cl.sh "$@"; }
clp() { CL_MODE=plugins source ~/.claude/cl.sh "$@"; }
# cl/clp completion (sourced after compinit)
[[ -f ~/.claude/cl-completion.zsh ]] && source ~/.claude/cl-completion.zsh

# === JAINE PLUGINS END ===
```

### Step 7: Clear completion cache

Use Bash tool:
```bash
rm -f ~/.zcompdump* 2>/dev/null; echo "Completion cache cleared"
```

### Step 8: Verify

Use Bash tool:
```bash
grep -A3 "# Claude Light" ~/.zshrc
```

Should show `cl` and `clp` functions.

## Success Message

```
✅ Claude Light installed!

Commands:
  cl       Task picker → Claude (minimal)
  clp      Task picker → Claude (with plugins)
  cl -n    Clean start (no picker)
  clp -n   Clean start with plugins

Tab completion:
  cl -<TAB>  Show available options

Run: source ~/.zshrc
```
