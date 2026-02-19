---
description: Show Claude Light usage and documentation
allowed-tools: []
---

# Claude Light Help

Claude Light provides lightweight shell commands to launch Claude Code with different configurations.

## Commands

| Command | Global CLAUDE.md | Plugins | Task Picker |
|---------|-----------------|---------|-------------|
| `cl` | No | No | Yes |
| `clp` | No | **Yes** | Yes |
| `c` | Yes | Yes | No |

## Usage

```bash
cl              # Task list picker → Claude (minimal)
clp             # Task list picker → Claude (with plugins)
cl -n           # Clean start, no task picker
clp -n          # Clean start with plugins
cl -r           # Resume last session
clp -c          # Continue last session
```

## Options

| Option | Description |
|--------|-------------|
| `-n`, `--clean` | Start fresh without task list picker |
| `-t`, `--tasks` | Show task list picker (default) |
| `-h`, `--help` | Show help |
| Any other | Passed directly to claude |

## How It Works

### cl (Claude Light)
- Uses `--setting-sources project,local` to skip global settings
- No global `~/.claude/CLAUDE.md`
- No plugins loaded
- Task picker via gum

### clp (Claude Light + Plugins)
- Uses `CLAUDE_CONFIG_DIR=/0/.staff/CLP/.claude`
- No global `~/.claude/CLAUDE.md`
- Plugins loaded from symlinked directory
- Task picker via gum

## Configuration

### CLP Profile Location
`/0/.staff/CLP/.claude/`

Contents:
- `settings.json` — enabledPlugins configuration
- `plugins/` → symlink to `~/.claude/plugins/`

### Script Location
`~/.claude/cl.sh`

## Plugin Commands

- `/claude-light:install` — Install cl/clp aliases
- `/claude-light:uninstall` — Remove aliases
- `/claude-light:help` — This help

## Tips

1. **Task Lists**: Press `Ctrl+T` inside Claude to view task list
2. **Resume**: Use `cl -r` or `clp -r` to resume last session
3. **Clean Start**: Use `-n` when you don't want task picker
4. **Plugins Update**: CLP uses symlink, so plugin updates apply automatically
