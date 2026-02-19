# Claude Light

Lightweight Claude Code launcher with task list picker.

## Overview

Claude Light provides two shell commands for launching Claude Code with minimal configuration:

| Command | Description |
|---------|-------------|
| `cl` | Claude Light — no plugins, no global CLAUDE.md |
| `clp` | Claude Light + Plugins — plugins enabled, no global CLAUDE.md |

Both commands include an interactive task list picker (via gum).

## Installation

```bash
/claude-light:install
```

This will:
1. Copy `cl.sh` to `~/.claude/`
2. Create CLP profile at `/0/.staff/CLP/.claude/`
3. Add `cl` and `clp` aliases to `~/.zshrc`

## Usage

```bash
cl              # Task picker → minimal Claude
clp             # Task picker → Claude with plugins
cl -n           # Clean start (no picker)
clp -n          # Clean start with plugins
cl -r           # Resume session
clp -c          # Continue session
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    cl.sh (unified)                      │
├─────────────────────────────────────────────────────────┤
│  CL_MODE=light              │  CL_MODE=plugins          │
│  --setting-sources          │  CLAUDE_CONFIG_DIR=       │
│    project,local            │    /0/.staff/CLP/.claude  │
├─────────────────────────────────────────────────────────┤
│                    Task List Picker                     │
│                      (via gum)                          │
└─────────────────────────────────────────────────────────┘
```

## Configuration

### CLP Profile

Location: `/0/.staff/CLP/.claude/`

```
/0/.staff/CLP/.claude/
├── settings.json    # enabledPlugins config
└── plugins/         # → ~/.claude/plugins (symlink)
```

Plugins are symlinked, so updates to `~/.claude/plugins/` apply automatically.

## Commands

| Command | Description |
|---------|-------------|
| `/claude-light:install` | Install aliases and CLP profile |
| `/claude-light:uninstall` | Remove aliases |
| `/claude-light:help` | Show documentation |

## Version History

- **1.0.0** — Initial release with unified cl.sh, task picker, CL_MODE support
