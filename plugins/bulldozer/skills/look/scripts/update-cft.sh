#!/usr/bin/env bash
# update-cft.sh — install/refresh the pinned Chrome for Testing for drive lanes (SP1, #164).
#
# Layout:  $CFT_ROOT/<version>/chrome-<platform>/Google Chrome for Testing.app
#          $CFT_ROOT/current -> <version>            (the pin; ln -sfn)
# This script is the ONLY mover of `current` — launching a lane never auto-updates
# (that auto-update drift is exactly what #164 removes).
#
# CFT_DRY_RUN=1 → resolve + print version/url, no download, no pin move.
set -euo pipefail

CFT_ROOT="${CFT_ROOT:-/0/.jaine/.browser/cft}"
JSON_URL="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
PLATFORM="${CFT_PLATFORM:-mac-arm64}"

resolved=$(curl -fsSL --max-time 20 "$JSON_URL" | python3 -c '
import json, sys
plat = sys.argv[1]
d = json.load(sys.stdin)
ch = d["channels"]["Stable"]
url = next(x["url"] for x in ch["downloads"]["chrome"] if x["platform"] == plat)
print(ch["version"], url)
' "$PLATFORM") || { echo "ERROR: could not resolve CfT Stable from $JSON_URL" >&2; exit 1; }
VERSION="${resolved%% *}"
URL="${resolved#* }"
[[ -n "$VERSION" && -n "$URL" ]] || { echo "ERROR: could not resolve CfT Stable" >&2; exit 1; }
# VERSION becomes a path component below — accept only a plain dotted number
# (defense-in-depth against a spoofed/compromised endpoint feeding "../..").
if ! [[ "$VERSION" =~ ^[0-9][0-9.]*$ ]]; then
  echo "ERROR: suspicious CfT version string (not a dotted number): $VERSION" >&2
  exit 1
fi

echo "CfT Stable: $VERSION"
echo "url: $URL  (platform: $PLATFORM)"

if [[ "${CFT_DRY_RUN:-}" == "1" ]]; then
  echo "CFT_DRY_RUN — not downloading, pin untouched"
  exit 0
fi

BIN="$CFT_ROOT/$VERSION/chrome-$PLATFORM/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
if [[ -x "$BIN" ]]; then
  echo "already installed: $CFT_ROOT/$VERSION"
else
  # Install ATOMICALLY: download + unzip into a temp dir, validate the layout,
  # then move the completed tree into place. A partially-extracted version dir
  # (interrupted unzip) could otherwise pass [[ -x "$BIN" ]] on a re-run and get
  # pinned while missing framework files.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fSL --progress-bar -o "$tmp/cft.zip" "$URL"
  unzip -q "$tmp/cft.zip" -d "$tmp/unpacked"
  TBIN="$tmp/unpacked/chrome-$PLATFORM/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
  [[ -x "$TBIN" ]] || { echo "ERROR: unzip did not produce expected layout: chrome-$PLATFORM/…" >&2; exit 1; }
  # Clear any partial leftover from an interrupted earlier run (never the pinned
  # tree — a complete install would have taken the already-installed branch).
  rm -rf "${CFT_ROOT:?}/${VERSION:?}"
  mkdir -p "$CFT_ROOT/$VERSION"
  mv "$tmp/unpacked/chrome-$PLATFORM" "$CFT_ROOT/$VERSION/"
fi

# Validate BEFORE moving the pin — a broken binary must never become `current`.
"$BIN" --version
ln -sfn "$CFT_ROOT/$VERSION" "$CFT_ROOT/current"
echo "pinned: current -> $VERSION"
