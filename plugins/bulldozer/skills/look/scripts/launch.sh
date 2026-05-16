#!/usr/bin/env bash
# JAINE Browser — dedicated Chrome instance with CDP + AppleScript
# Separate profile, no extensions, dark theme, remote debugging
# Usage: jaine-browser [URL]

PROFILE_DIR="/0/.jaine/.browser/profile"
CDP_PORT=9333
WINDOW_WIDTH=1440
WINDOW_HEIGHT=900

URL="${1:-about:blank}"

# Kill existing JAINE browser if running
pkill -f "user-data-dir=$PROFILE_DIR" 2>/dev/null
sleep 1

# Pre-patch Local State to enable AppleScript JS (Chrome reads on startup)
LOCAL_STATE="$PROFILE_DIR/Local State"
if [ -f "$LOCAL_STATE" ]; then
  python3 -c "
import json
with open('$LOCAL_STATE') as f: s = json.load(f)
s.setdefault('browser', {})['allow_javascript_apple_events'] = True
with open('$LOCAL_STATE', 'w') as f: json.dump(s, f)
"
fi

/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --user-data-dir="$PROFILE_DIR" \
  --remote-debugging-port=$CDP_PORT \
  --remote-allow-origins=* \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions \
  --disable-sync \
  --disable-translate \
  --disable-background-networking \
  --disable-component-update \
  --window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT} \
  --window-position=100,100 \
  "$URL" \
  >> /0/.jaine/.browser/chrome.log 2>&1 &

CHROME_PID=$!
sleep 3

# Auto-enable AppleScript JS via menu click (one-time, persists in profile)
osascript -e '
tell application "Google Chrome" to activate
delay 0.5
tell application "System Events"
    tell process "Google Chrome"
        try
            click menu item "Разрешить JavaScript из событий Apple" of menu 1 of menu item "Разработчикам" of menu "Вид" of menu bar 1
        end try
    end tell
end tell' 2>/dev/null

# Handle confirmation dialog if it appears
sleep 1
osascript -e '
tell application "System Events"
    tell process "Google Chrome"
        try
            click button "Разрешить" of sheet 1 of window 1
        end try
        try
            click button "Allow" of sheet 1 of window 1
        end try
    end tell
end tell' 2>/dev/null

if kill -0 "$CHROME_PID" 2>/dev/null; then
  echo "JAINE Browser started (PID $CHROME_PID, CDP :$CDP_PORT)"
else
  echo "ERROR: Chrome failed to start — check /0/.jaine/.browser/chrome.log" >&2
  exit 1
fi
