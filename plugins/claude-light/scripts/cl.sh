#!/bin/zsh
# Claude Light - unified script for cl and clp
# cl  = minimal (no plugins, no global CLAUDE.md)
# clp = with plugins (no global CLAUDE.md)
#
# Usage: cl [options] [claude-flags...]
#        clp [options] [claude-flags...]

setopt nullglob  # Don't error on empty globs

# Determine mode: CL_MODE env var or script name
if [[ -n "$CL_MODE" ]]; then
    MODE="$CL_MODE"
else
    SCRIPT_NAME="$(basename "$0")"
    case "$SCRIPT_NAME" in
        clp*) MODE="plugins" ;;
        *)    MODE="light" ;;
    esac
fi

# Mode-specific config
if [[ "$MODE" == "plugins" ]]; then
    CLP_CONFIG_DIR="/0/.staff/CLP/.claude"
else
    STATUSLINE_CONFIG="$HOME/.claude/statusline-only.json"
fi

TASKS_DIR="$HOME/.claude/tasks"

# Colors
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
MAGENTA='\033[0;35m'
DIM='\033[2m'
NC='\033[0m'

# Mode-specific color
if [[ "$MODE" == "plugins" ]]; then
    MODE_COLOR="$MAGENTA"
    MODE_LABEL="clp: with plugins"
else
    MODE_COLOR="$CYAN"
    MODE_LABEL="cl: light"
fi

# Base claude command
base_cmd() {
    if [[ "$MODE" == "plugins" ]]; then
        echo "CLAUDE_CONFIG_DIR=$CLP_CONFIG_DIR claude --dangerously-skip-permissions --debug"
    else
        local cmd="claude --dangerously-skip-permissions --debug --setting-sources project,local"
        if [[ -f "$STATUSLINE_CONFIG" ]]; then
            cmd="$cmd --settings $STATUSLINE_CONFIG"
        fi
        echo "$cmd"
    fi
}

show_task_lists() {
    local items=()

    if [[ ! -d "$TASKS_DIR" ]] || [[ -z "$(ls -A "$TASKS_DIR" 2>/dev/null)" ]]; then
        echo -e "${YELLOW}No task lists found.${NC}" >&2
        return 1
    fi

    for dir in "$TASKS_DIR"/*/; do
        [[ -d "$dir" ]] || continue
        local id=$(basename "$dir")
        local mod_date=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$dir")
        local task_count=$(ls "$dir"/*.json 2>/dev/null | wc -l | tr -d ' ')

        # Get first task subject for context
        local first_task=""
        local first_json=$(ls "$dir"/*.json 2>/dev/null | head -1)
        if [[ -f "$first_json" ]]; then
            first_task=$(jq -r '.subject // empty' "$first_json" 2>/dev/null | head -c 50)
        fi

        # Count statuses
        local pending=0 in_progress=0 completed=0
        for f in "$dir"/*.json; do
            [[ -f "$f" ]] || continue
            local task_status=$(jq -r '.status // "pending"' "$f" 2>/dev/null)
            case "$task_status" in
                pending) ((pending++)) ;;
                in_progress) ((in_progress++)) ;;
                completed) ((completed++)) ;;
            esac
        done

        items+=("$id|$mod_date|$task_count|$pending|$in_progress|$completed|$first_task")
    done

    if [[ ${#items[@]} -eq 0 ]]; then
        echo -e "${YELLOW}No task lists found.${NC}" >&2
        return 1
    fi

    # Sort by date (newest first)
    IFS=$'\n' sorted=($(printf '%s\n' "${items[@]}" | sort -t'|' -k2 -r))
    unset IFS

    # Build display lines for gum
    local display_lines=()
    for item in "${sorted[@]}"; do
        IFS='|' read -r id date count pending progress done subject <<< "$item"
        local status_str="[${pending}P/${progress}W/${done}D]"
        local line=$(printf "%-36s  %s  %s  %s" "$id" "$date" "$status_str" "$subject")
        display_lines+=("$line")
    done

    # Header (to stderr so it doesn't pollute return value)
    echo -e "${MODE_COLOR}Task Lists${NC} ${DIM}(${#sorted[@]} found) [$MODE_LABEL]${NC}" >&2
    echo -e "${DIM}ID                                    Date              Status       First Task${NC}" >&2
    echo "" >&2

    # Use gum to choose
    local selected=$(printf '%s\n' "${display_lines[@]}" | gum choose --height=15)

    if [[ -n "$selected" ]]; then
        # Extract ID (first 36 chars) - only this goes to stdout
        echo "$selected" | awk '{print $1}'
    fi
}

# Main logic
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
    echo "Usage: $SCRIPT_NAME [OPTIONS] [CLAUDE-FLAGS...]"
    echo ""
    if [[ "$MODE" == "plugins" ]]; then
        echo "Claude Light with Plugins - task picker, plugins enabled, no global CLAUDE.md"
    else
        echo "Claude Light - minimal config with task list picker"
    fi
    echo ""
    echo "Options:"
    echo "  --tasks, -t    Show task list picker (default if no args)"
    echo "  --clean, -n    Start fresh without task list picker"
    echo "  --help, -h     Show this help"
    echo ""
    echo "Examples:"
    echo "  $SCRIPT_NAME             Interactive task list picker"
    echo "  $SCRIPT_NAME -n          Clean start (no tasks)"
    echo "  $SCRIPT_NAME -r          Resume last session"
    echo "  $SCRIPT_NAME -c          Continue last session"
    echo ""
    if [[ "$MODE" == "plugins" ]]; then
        echo "Config: $CLP_CONFIG_DIR"
    fi
    echo "All other flags passed directly to claude."

elif [[ "$1" == "--clean" || "$1" == "-n" ]]; then
    # Clean start - no task list
    echo -e "${DIM}Starting clean [$MODE_LABEL]...${NC}" >&2
    shift
    eval "$(base_cmd) $@"

elif [[ "$1" == "--tasks" || "$1" == "-t" || -z "$1" ]]; then
    # Interactive picker mode
    selected_id=$(show_task_lists)
    if [[ -n "$selected_id" ]]; then
        export CLAUDE_CODE_TASK_LIST_ID="$selected_id"
        echo -e "${GREEN}Attached to task list:${NC} $selected_id" >&2
    else
        echo -e "${DIM}Starting without task list...${NC}" >&2
    fi

    # Remove --tasks/-t if present, pass rest to claude
    shift 2>/dev/null
    eval "$(base_cmd) $@"

else
    # Pass all args directly to claude
    eval "$(base_cmd) $@"
fi
