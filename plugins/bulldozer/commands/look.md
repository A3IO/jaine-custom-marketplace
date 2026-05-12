---
description: Open URL in JAINE Browser, take screenshot, and show it
argument-hint: "[URL]"
allowed-tools: ["Bash", "Read"]
---

Visual verification via JAINE Browser (CDP :9333).

1. Check browser status:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" status
```

2. If OFFLINE, launch (pass URL if provided):
```bash
"${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh" "$ARGUMENTS" &
sleep 5
```

3. If browser was already ONLINE and URL argument provided, navigate:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" navigate "$ARGUMENTS"
sleep 2
```
Skip step 3 if you just launched the browser in step 2 (it already opened the URL).

4. Screenshot + show:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/cdp.py" screenshot /tmp/jaine-look.jpg
```
Then Read the screenshot file to see it.

5. Report what you see to the user.
