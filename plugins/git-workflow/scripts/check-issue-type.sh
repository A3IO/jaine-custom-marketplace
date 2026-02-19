#!/usr/bin/env bash
# check-issue-type.sh — Deterministic issue/PR type checker for changelog-analyzer
#
# Usage:
#   bash check-issue-type.sh --numbers 45,48,49 [--owner OWNER] [--repo REPO]
#
# If --owner/--repo not provided, auto-detects from git remote.
# Platform (Forgejo/GitHub) auto-detected from remote URL + environment.
#
# Output: One JSON line per reference number:
#   {"number":45,"type":"issue","url":"http://192.168.31.116:3300/0_INFRA/STATUSLINE/issues/45"}
#   {"number":48,"type":"pr","url":"http://192.168.31.116:3300/0_INFRA/STATUSLINE/pulls/48"}
#
# Exit codes:
#   0 — success (individual errors reported per-line as {"error":"..."})
#   1 — missing --numbers argument
#   2 — cannot detect git remote
#   3 — cannot parse owner/repo from remote
#   4 — cannot detect platform (not Forgejo, not GitHub)

set -euo pipefail

# === Parse arguments ===
NUMBERS=""
OWNER=""
REPO=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --numbers) NUMBERS="$2"; shift 2 ;;
        --owner)   OWNER="$2"; shift 2 ;;
        --repo)    REPO="$2"; shift 2 ;;
        *)         echo "Error: unknown argument: $1" >&2; exit 1 ;;
    esac
done

[[ -z "$NUMBERS" ]] && { echo "Error: --numbers required (e.g. --numbers 45,48,49)" >&2; exit 1; }

# === Auto-detect remote ===
REMOTE=$(git remote get-url origin 2>/dev/null) || { echo "Error: no git remote 'origin'" >&2; exit 2; }

# === Parse owner/repo from remote URL ===
if [[ -z "$OWNER" || -z "$REPO" ]]; then
    # Handles:
    #   ssh://git@host:port/OWNER/REPO.git
    #   http://host:port/OWNER/REPO.git
    #   https://github.com/OWNER/REPO.git
    #   git@github.com:OWNER/REPO.git
    OWNER_REPO=$(echo "$REMOTE" | sed -E '
        s#^ssh://[^/]+/##;
        s#^https?://[^/]+/##;
        s#^[^@]+@[^:]+:##;
        s#\.git$##
    ')
    OWNER=$(echo "$OWNER_REPO" | cut -d'/' -f1)
    REPO=$(echo "$OWNER_REPO" | cut -d'/' -f2)
fi

[[ -z "$OWNER" || -z "$REPO" ]] && { echo "Error: cannot parse owner/repo from remote: $REMOTE" >&2; exit 3; }

# === Detect platform ===
PLATFORM=""
BASE_URL=""

if echo "$REMOTE" | grep -qi "github.com"; then
    PLATFORM="github"
    BASE_URL="https://github.com/${OWNER}/${REPO}"
elif [[ -n "${FORGEJO_API_URL:-}" ]]; then
    PLATFORM="forgejo"
    BASE_URL=$(echo "$FORGEJO_API_URL" | sed 's|/api/v1||')
else
    # Try extracting host from SSH remote: ssh://git@HOST:PORT/...
    HOST=$(echo "$REMOTE" | sed -E 's#^ssh://[^@]+@([^:]+):.*#\1#')
    if [[ -n "$HOST" && "$HOST" != "$REMOTE" ]]; then
        PLATFORM="forgejo"
        PORT="${FORGEJO_WEB_PORT:-3300}"
        BASE_URL="http://${HOST}:${PORT}"
    fi
fi

[[ -z "$PLATFORM" ]] && { echo "Error: cannot detect platform from remote: $REMOTE" >&2; exit 4; }

# === Check each reference number ===
IFS=',' read -ra NUMS <<< "$NUMBERS"
for N in "${NUMS[@]}"; do
    N=$(echo "$N" | tr -d ' ')
    [[ -z "$N" ]] && continue

    if [[ "$PLATFORM" == "forgejo" ]]; then
        TOKEN="${FORGEJO_API_TOKEN:-}"
        if [[ -z "$TOKEN" ]]; then
            echo "{\"number\":$N,\"type\":\"unknown\",\"error\":\"no FORGEJO_API_TOKEN\"}"
            continue
        fi

        API_URL="${FORGEJO_API_URL:-${BASE_URL}/api/v1}"
        RESPONSE=$(curl -s --max-time 5 \
            -H "Authorization: token $TOKEN" \
            "${API_URL}/repos/${OWNER}/${REPO}/issues/${N}" 2>/dev/null) || {
            echo "{\"number\":$N,\"type\":\"unknown\",\"error\":\"api_request_failed\"}"
            continue
        }

        IS_PR=$(echo "$RESPONSE" | jq -r '.pull_request != null' 2>/dev/null)
        if [[ "$IS_PR" == "true" ]]; then
            echo "{\"number\":$N,\"type\":\"pr\",\"url\":\"${BASE_URL}/${OWNER}/${REPO}/pulls/${N}\"}"
        elif [[ "$IS_PR" == "false" ]]; then
            echo "{\"number\":$N,\"type\":\"issue\",\"url\":\"${BASE_URL}/${OWNER}/${REPO}/issues/${N}\"}"
        else
            echo "{\"number\":$N,\"type\":\"unknown\",\"error\":\"cannot_parse_response\"}"
        fi

    elif [[ "$PLATFORM" == "github" ]]; then
        if ! command -v gh >/dev/null 2>&1; then
            echo "{\"number\":$N,\"type\":\"unknown\",\"error\":\"gh_cli_not_found\"}"
            continue
        fi

        IS_PR=$(gh api "repos/${OWNER}/${REPO}/issues/${N}" --jq '.pull_request != null' 2>/dev/null) || {
            echo "{\"number\":$N,\"type\":\"unknown\",\"error\":\"api_request_failed\"}"
            continue
        }

        if [[ "$IS_PR" == "true" ]]; then
            echo "{\"number\":$N,\"type\":\"pr\",\"url\":\"https://github.com/${OWNER}/${REPO}/pull/${N}\"}"
        else
            echo "{\"number\":$N,\"type\":\"issue\",\"url\":\"https://github.com/${OWNER}/${REPO}/issues/${N}\"}"
        fi
    fi
done
