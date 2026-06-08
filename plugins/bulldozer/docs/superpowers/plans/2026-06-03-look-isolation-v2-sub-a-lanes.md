# /look-v2 Sub-Project A — Lane Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parameterize `launch.sh` so `/look` can run multiple isolated, optionally-headless browser lanes in parallel (each a set of env vars), with the default invocation byte-identical to today; collapse `conftest.py` onto that one launch path so the e2e suite runs against an isolated headless lane and never touches the user's daily 9333 browser.

**Architecture:** A *lane* is fully described by env vars (`CDP_PORT`, `LOOK_PROFILE_DIR`, `LOOK_HEADLESS`, `CHROME_BIN`) + two flags (`--headless`/`--headful`). `launch.sh` builds the Chrome invocation as a single `CHROME_ARGV` bash array — one source of truth shared by a new `LOOK_DRY_RUN` printer (prints resolved config + argv, exits 0 without launching) and the real background launch. Every knob is TDD'd against dry-run output, so unit tests never spawn Chrome. `conftest.py` stops launching Chrome directly and always goes through `launch.sh`.

**Tech Stack:** bash (3.2-compatible — macOS `/bin/bash`), Python 3 + pytest, Chrome DevTools Protocol, Chrome `--headless=new`.

**Spec:** `docs/superpowers/specs/2026-06-03-look-isolation-v2-design.md` §4 (sub-project A, A.0–A.12) + §8 testing. This plan implements **only sub-project A**; B/C/D are separate plans.

---

## Context for the implementer

- **Worktree + branch:** work in `/0/.aitemp/bulldozer-look-isolation-v2` on branch `bulldozer/feat/look-isolation-v2`. Run every command from that directory.
- **Do NOT edit `plugin.json`** — the `auto-calver` post-merge hook bumps the version on merge. A manual bump causes a double-bump.
- **Do NOT add `set -euo pipefail`** to `launch.sh`. It currently has none; today's tolerant behavior (e.g. `osascript … 2>/dev/null` + `try`) depends on that. New fail-loud paths use explicit `echo >&2; exit 1` — no reliance on `set -e`.
- **Bash 3.2 compatibility is required** (there is a dedicated `tests/test_log_round_bash32_compat.py` for a sibling script). Arrays, `[[ =~ ]]`, `(( ))`, `shopt -s nocasematch`, `printf '%s\n' "${arr[@]}"` are all 3.2-safe. **Do NOT** use bash-4 lowercase expansion `${var,,}`.
- **Preserve these substrings in `launch.sh`** (existing structural tests assert them — see `tests/test_cdp.py::test_launch_sh_delegates_to_cdp_normalize_url`, `::test_issue_54_launch_sh_drops_chrome_dark_mode_flags`, `tests/test_plugin_structure.sh`):
  - `#!/usr/bin/env bash` shebang
  - the normalize-url block: `"$URL" == /*`, a call to `cdp.py … normalize-url`, `-n "$normalized"`; and **never** `as_uri()`
  - a `chrome.log` reference
  - **never** `--force-dark-mode` or `WebContentsForceDark`
- **The Chrome argv must stay byte-identical for the default lane.** Task 1 lands a characterization test (`EXPECTED_DEFAULT_ARGV`) that locks it; every later task must keep it green.
- **TDD discipline (RED before GREEN):** each behavioral task writes the failing test FIRST, runs it to SEE it fail for the right reason, then implements. The only non-TDD step is Task 1 Step 1 (a one-line dry-run safety guard) — it is scaffolding that makes all later tests safe (no Chrome spawned), called out explicitly.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `skills/look/scripts/launch.sh` | Resolve a lane from env/flags; build one `CHROME_ARGV`; dry-run printer; background launch; headful-only AppleScript/Local-State steps | **Rewrite** (parameterize, keep default byte-identical) |
| `tests/test_launch.py` | All `LOOK_DRY_RUN` unit tests for every knob (A.0–A.10) + bash-3.2 smoke + structural guards + conftest-helper tests | **Create** |
| `tests/conftest.py` | `jaine_browser` fixture: always launch via `launch.sh`; default lane = isolated non-9333 headless; `_reuse_decision` guard; `TEST_CDP_PORT`/`LANE_IS_HEADLESS` constants | **Modify** (A.11) |
| `tests/test_e2e.py` | Skip the AppleScript-only `window bounds` e2e when the lane is headless (until sub-project B) | **Modify** (A.11 monitor) |
| `skills/look/SKILL.md` | Document the lane model, headless channel implications, `LOOK_DRY_RUN` | **Modify** (A.12) |
| `CLAUDE.md` | Add `test_launch.py` to the `/look` test-suite row | **Modify** (doc hygiene) |

---

## Task 1: `CHROME_ARGV` array + `LOOK_DRY_RUN` seam, byte-identical default (A.0, A.10)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Create: `tests/test_launch.py`

- [ ] **Step 1 (safety scaffold — not TDD): add a minimal dry-run exit guard so later tests never spawn Chrome.**

Edit `skills/look/scripts/launch.sh`. Immediately after the `SCRIPT_DIR=…` line (currently around the top, after `URL="${1:-about:blank}"`), the script must short-circuit when `LOOK_DRY_RUN` is set. For this scaffolding step add the bare guard right after the shebang/comment header and the `SCRIPT_DIR` assignment:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# LOOK_DRY_RUN short-circuit (expanded in Task 1 Step 3 to print config + argv).
if [[ -n "${LOOK_DRY_RUN:-}" ]]; then
  echo "LOOK_DRY_RUN"
  exit 0
fi
```

Verify manually it does not launch Chrome:

Run: `LOOK_DRY_RUN=1 bash skills/look/scripts/launch.sh`
Expected: prints exactly `LOOK_DRY_RUN`, exits 0, no Chrome process spawned (`pgrep -f remote-debugging-port=9333` prints nothing new).

- [ ] **Step 2 (RED): write the characterization test for the default argv.**

Create `tests/test_launch.py`:

```python
"""Unit tests for skills/look/scripts/launch.sh lane parameterization (sub-project A).

launch.sh is exercised in LOOK_DRY_RUN mode: it resolves config + builds the
Chrome argv array, prints them, and exits 0 WITHOUT launching Chrome. Every knob
is asserted from that output, so these tests never spawn a browser. Pattern
mirrors tests/test_log_round_bash32_compat.py (bash script via subprocess).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))  # make `from conftest import …` reliable

PLUGIN_ROOT = Path(__file__).parent.parent
LAUNCH_SCRIPT = str(PLUGIN_ROOT / "skills" / "look" / "scripts" / "launch.sh")
LAUNCH_TEXT = Path(LAUNCH_SCRIPT).read_text()

# A lane env var leaking from the pytest process would make these tests
# non-hermetic; _run_launch strips them and re-adds only what a case sets.
_LANE_VARS = ("CDP_PORT", "LOOK_PROFILE_DIR", "LOOK_HEADLESS", "CHROME_BIN", "LOOK_INSECURE")


def _run_launch(args=None, env_override=None, dry_run=True, timeout=10, bash="bash"):
    env = os.environ.copy()
    for k in _LANE_VARS:
        env.pop(k, None)
    if dry_run:
        env["LOOK_DRY_RUN"] = "1"
    if env_override:
        for k, v in env_override.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [bash, LAUNCH_SCRIPT] + (args or []),
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _parse_dryrun(stdout):
    """Parse dry-run output: 'key=value' config lines, then 'ARGV' + one token/line."""
    lines = stdout.splitlines()
    assert lines and lines[0] == "LOOK_DRY_RUN", "missing dry-run marker; got: {!r}".format(stdout)
    cfg, argv, in_argv = {}, [], False
    for ln in lines[1:]:
        if ln == "ARGV":
            in_argv = True
            continue
        if in_argv:
            argv.append(ln)
        else:
            k, _, v = ln.partition("=")
            cfg[k] = v
    return cfg, argv


EXPECTED_DEFAULT_ARGV = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "--user-data-dir=/0/.jaine/.browser/profile",
    "--remote-debugging-port=9333",
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-sync",
    "--disable-translate",
    "--disable-background-networking",
    "--disable-component-update",
    "--window-size=1440,900",
    "--window-position=100,100",
    "about:blank",
]


def test_default_invocation_argv_is_byte_identical():
    """A.0: default lane (no env, no flag) → exactly today's Chrome argv."""
    r = _run_launch()
    assert r.returncode == 0, "dry-run failed: {}".format(r.stderr)
    _, argv = _parse_dryrun(r.stdout)
    assert argv == EXPECTED_DEFAULT_ARGV, "argv drift:\n{!r}".format(argv)


def test_default_url_token_overridable():
    """The first positional becomes the trailing URL token."""
    r = _run_launch(args=["https://example.com/x"])
    _, argv = _parse_dryrun(r.stdout)
    assert argv[-1] == "https://example.com/x", argv


def test_dry_run_does_not_launch_and_exits_zero():
    """LOOK_DRY_RUN prints + exits 0; no real launch side effects."""
    r = _run_launch()
    assert r.returncode == 0
    assert r.stdout.startswith("LOOK_DRY_RUN\n")
```

Run: `pytest tests/test_launch.py -v`
Expected: FAIL — `_parse_dryrun` raises / `test_default_invocation_argv_is_byte_identical` fails because Step 1's guard prints only `LOOK_DRY_RUN` with no `ARGV` block (no token lines). This is the RED, and it is safe — the guard exits before any launch.

- [ ] **Step 3 (GREEN): refactor the Chrome invocation into one `CHROME_ARGV` array + expand the dry-run printer.**

Rewrite `skills/look/scripts/launch.sh` to this exact structure (config still hardcoded to today's values — parameterization lands in later tasks):

```bash
#!/usr/bin/env bash
# JAINE Browser — dedicated Chrome instance with CDP + AppleScript
# Separate profile, no extensions, dark theme, remote debugging
# Usage: jaine-browser [URL]   (LOOK_DRY_RUN=1 prints resolved config + argv, no launch)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Config (hardcoded today's defaults; parameterized in later tasks) ──
CDP_PORT=9333
PROFILE_DIR="/0/.jaine/.browser/profile"
PROFILE_OVERRIDDEN=0
WINDOW_WIDTH=1440
WINDOW_HEIGHT=900
WINDOW_POSITION="100,100"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOG="/0/.jaine/.browser/chrome.log"

# ── URL resolution (#60: normalize a bare absolute path to a file:// URL) ──
# Delegate to `cdp.py normalize-url` — the SINGLE source of truth (guard +
# pathlib.as_uri live in cdp.py normalize_url). Cheap-skip non-slash URLs.
URL="${1:-about:blank}"
if [[ "$URL" == /* ]]; then
  normalized=$(python3 "$SCRIPT_DIR/cdp.py" normalize-url "$URL" 2>/dev/null)
  if [[ -n "$normalized" ]]; then
    URL="$normalized"
  fi
fi

# ── Headless placeholder — resolved in Task 6 from --headless/--headful + LOOK_HEADLESS.
#    MUST sit AFTER argument parsing so HEADLESS_ARG (set by the Task 5 parser) exists. ──
HEADLESS=0

# ── pkill match (today's unanchored form; lane-scoped in Task 7) ──
KILL_MATCH="user-data-dir=$PROFILE_DIR"

# ── Single Chrome argv array: one source for dry-run AND the real launch ──
CHROME_ARGV=(
  "$CHROME_BIN"
  "--user-data-dir=$PROFILE_DIR"
  "--remote-debugging-port=$CDP_PORT"
  "--remote-allow-origins=*"
  --no-first-run
  --no-default-browser-check
  --disable-extensions
  --disable-sync
  --disable-translate
  --disable-background-networking
  --disable-component-update
  "--window-size=${WINDOW_WIDTH},${WINDOW_HEIGHT}"
  "--window-position=${WINDOW_POSITION}"
)
CHROME_ARGV+=("$URL")

# ── LOOK_DRY_RUN: print resolved config + argv, do NOT launch ──
if [[ -n "${LOOK_DRY_RUN:-}" ]]; then
  local_state_patch=$(( HEADLESS == 1 ? 0 : 1 ))
  osascript_steps=$(( HEADLESS == 1 ? 0 : 1 ))
  echo "LOOK_DRY_RUN"
  echo "port=$CDP_PORT"
  echo "profile=$PROFILE_DIR"
  echo "profile_overridden=$PROFILE_OVERRIDDEN"
  echo "headless=$HEADLESS"
  echo "local_state_patch=$local_state_patch"
  echo "osascript=$osascript_steps"
  echo "window_position=$WINDOW_POSITION"
  echo "chrome_bin=$CHROME_BIN"
  echo "log=$LOG"
  echo "kill_match=$KILL_MATCH"
  echo "ARGV"
  printf '%s\n' "${CHROME_ARGV[@]}"
  exit 0
fi

# ── Real launch ──
# Kill existing JAINE browser on this profile
pkill -f "$KILL_MATCH" 2>/dev/null
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

"${CHROME_ARGV[@]}" >> "$LOG" 2>&1 &

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
  echo "ERROR: Chrome failed to start — check $LOG" >&2
  exit 1
fi
```

Run: `pytest tests/test_launch.py -v`
Expected: PASS (all three tests).

- [ ] **Step 4 (structural guard): assert one argv array, used by the real launch via the array (A.10).**

Add to `tests/test_launch.py`:

```python
def test_single_chrome_argv_array_used_by_real_launch():
    """A.10: exactly one CHROME_ARGV definition, and the real launch uses it as
    a backgrounded array expansion (NOT exec, NOT a second arg list)."""
    assert LAUNCH_TEXT.count("CHROME_ARGV=(") == 1, "expected exactly one CHROME_ARGV array"
    assert '"${CHROME_ARGV[@]}" >> "$LOG" 2>&1 &' in LAUNCH_TEXT, \
        "real launch must background the shared CHROME_ARGV array"
    assert "\nexec " not in LAUNCH_TEXT, "must background (&), not exec — post-launch steps still run"


def test_normalize_url_delegation_preserved():
    """Existing #60 contract (tests/test_cdp.py::test_launch_sh_delegates_to_cdp_normalize_url)."""
    assert "normalize-url" in LAUNCH_TEXT
    assert '"$URL" == /*' in LAUNCH_TEXT
    assert "-n \"$normalized\"" in LAUNCH_TEXT
    assert "as_uri()" not in LAUNCH_TEXT


def test_no_dark_mode_flags():
    """Existing #54 contract (no Chrome Auto-Dark flags)."""
    assert "--force-dark-mode" not in LAUNCH_TEXT
    assert "WebContentsForceDark" not in LAUNCH_TEXT
```

Run: `pytest tests/test_launch.py -v`
Expected: PASS.

- [ ] **Step 5: confirm the existing launch.sh structural tests still pass, then commit.**

Run: `pytest tests/test_cdp.py -k "launch or normalize or issue_54" -v && bash tests/test_plugin_structure.sh`
Expected: PASS (normalize-url delegation, dark-mode, shebang, chrome.log all intact).

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): CHROME_ARGV array + LOOK_DRY_RUN seam (sub-A, byte-identical default)"
```

---

## Task 2: Port + profile parameterization + validation (A.1, A.4, A.6 port)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (RED): tests for port/profile resolution + validation.**

Add to `tests/test_launch.py`:

```python
def test_cdp_port_threads_into_argv():
    r = _run_launch(env_override={"CDP_PORT": "9334"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["port"] == "9334"
    assert "--remote-debugging-port=9334" in argv


def test_profile_derived_from_non_default_port():
    """A.4: non-9333 port without override → /0/.jaine/.browser/profile-<port>."""
    r = _run_launch(env_override={"CDP_PORT": "9334"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["profile"] == "/0/.jaine/.browser/profile-9334"
    assert "--user-data-dir=/0/.jaine/.browser/profile-9334" in argv
    assert cfg["profile_overridden"] == "0"


def test_profile_9333_unchanged():
    r = _run_launch(env_override={"CDP_PORT": "9333"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["profile"] == "/0/.jaine/.browser/profile"


def test_look_profile_dir_used_verbatim():
    """A.4: LOOK_PROFILE_DIR overrides derivation and marks profile_overridden=1."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["profile"] == "/tmp/lane-x"
    assert cfg["profile_overridden"] == "1"
    assert "--user-data-dir=/tmp/lane-x" in argv


def test_non_numeric_port_fails_loud():
    """A.6: mirror cdp.py's int-guard — non-numeric port → non-zero exit + stderr."""
    r = _run_launch(env_override={"CDP_PORT": "abc"})
    assert r.returncode != 0, "non-numeric CDP_PORT must fail loud"
    assert "CDP_PORT" in r.stderr


def test_out_of_range_port_fails_loud():
    """A.6: launch.sh adds a 1..65535 range check cdp.py lacks. The huge value (R2-F1)
    must be rejected by the {1,5} digit bound, NOT wrap through 10# into a valid port."""
    for bad in ("0", "70000", "-5", "18446744073709551617"):
        r = _run_launch(env_override={"CDP_PORT": bad})
        assert r.returncode != 0, "port {} must fail loud".format(bad)
        assert "CDP_PORT" in r.stderr


def test_leading_zero_port_canonicalized_not_octal_trapped():
    """R1-F2: a leading-zero port must NOT hit bash's octal trap; canonicalize to
    decimal (matching cdp.py's int('08')=8), never silently pass through."""
    r = _run_launch(env_override={"CDP_PORT": "08"})
    assert r.returncode == 0, "CDP_PORT=08 should canonicalize cleanly: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["port"] == "8"
    assert "--remote-debugging-port=8" in argv
```

Run: `pytest tests/test_launch.py -k "port or profile" -v`
Expected: FAIL (port/profile hardcoded; no validation).

- [ ] **Step 2 (GREEN): parameterize port + profile + add validation.**

In `launch.sh`, replace the hardcoded `CDP_PORT=9333` / `PROFILE_DIR=…` / `PROFILE_OVERRIDDEN=0` config lines with resolution. Replace:

```bash
# ── Config (hardcoded today's defaults; parameterized in later tasks) ──
CDP_PORT=9333
PROFILE_DIR="/0/.jaine/.browser/profile"
PROFILE_OVERRIDDEN=0
WINDOW_WIDTH=1440
```

with:

```bash
# ── Config ──
CDP_PORT="${CDP_PORT:-9333}"

# Port must be an integer in 1..65535. The {1,5} digit bound rejects non-numeric AND
# over-long input BEFORE arithmetic: a huge numeric string would otherwise overflow
# bash's 64-bit (( )) via 10# and wrap into 1..65535 (R2-F1). 10# then canonicalizes
# (a leading-zero "08" would hit bash's octal trap AND diverge from cdp.py int("08")=8).
if ! [[ "$CDP_PORT" =~ ^[0-9]{1,5}$ ]]; then
  echo "ERROR: CDP_PORT must be an integer in 1..65535 (got: $CDP_PORT)" >&2
  exit 1
fi
CDP_PORT=$((10#$CDP_PORT))
if (( CDP_PORT < 1 || CDP_PORT > 65535 )); then
  echo "ERROR: CDP_PORT must be an integer in 1..65535 (got: $CDP_PORT)" >&2
  exit 1
fi

# Profile: LOOK_PROFILE_DIR verbatim, else derive from port
if [[ -n "${LOOK_PROFILE_DIR:-}" ]]; then
  PROFILE_DIR="$LOOK_PROFILE_DIR"
  PROFILE_OVERRIDDEN=1
elif [[ "$CDP_PORT" == "9333" ]]; then
  PROFILE_DIR="/0/.jaine/.browser/profile"
  PROFILE_OVERRIDDEN=0
else
  PROFILE_DIR="/0/.jaine/.browser/profile-${CDP_PORT}"
  PROFILE_OVERRIDDEN=0
fi

WINDOW_WIDTH=1440
```

Run: `pytest tests/test_launch.py -k "port or profile" -v`
Expected: PASS. Also confirm no byte-identical regression:

Run: `pytest tests/test_launch.py::test_default_invocation_argv_is_byte_identical -v`
Expected: PASS.

- [ ] **Step 3: commit.**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): CDP_PORT + LOOK_PROFILE_DIR resolution + port validation (sub-A)"
```

---

## Task 3: Window-position derivation with non-negative cap (A.6 window)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (RED): tests for the capped, non-negative offset.**

Add to `tests/test_launch.py`:

```python
def test_window_position_9333_unchanged():
    r = _run_launch(env_override={"CDP_PORT": "9333"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["window_position"] == "100,100"
    assert "--window-position=100,100" in argv


def test_window_position_derived_above_9333():
    """A.6: 9334 → 100 + ((40 % 1200)+1200)%1200 = 140 on both axes."""
    r = _run_launch(env_override={"CDP_PORT": "9334"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["window_position"] == "140,140"


def test_window_position_non_negative_below_9333():
    """A.6: a port below 9333 must NOT yield a negative coordinate.
    9304 → off=-1160 → ((-1160 % 1200)+1200)%1200 = 40 → 140,140."""
    r = _run_launch(env_override={"CDP_PORT": "9304"})
    cfg, _ = _parse_dryrun(r.stdout)
    x, y = cfg["window_position"].split(",")
    assert int(x) >= 0 and int(y) >= 0, "coordinate went negative: {}".format(cfg["window_position"])
    assert cfg["window_position"] == "140,140"
```

Run: `pytest tests/test_launch.py -k window_position -v`
Expected: FAIL (window position hardcoded `100,100`).

- [ ] **Step 2 (GREEN): derive the window position.**

In `launch.sh`, replace:

```bash
WINDOW_WIDTH=1440
WINDOW_HEIGHT=900
WINDOW_POSITION="100,100"
```

with:

```bash
WINDOW_WIDTH=1440
WINDOW_HEIGHT=900

# Window position (headful only): derived from port, normalized non-negative + capped.
# bash % keeps the dividend's sign, so a port below 9333 would go negative without
# the ((x % CAP)+CAP)%CAP normalization.
if [[ "$CDP_PORT" == "9333" ]]; then
  WINDOW_X=100
  WINDOW_Y=100
else
  _off=$(( (CDP_PORT - 9333) * 40 ))
  _norm=$(( ((_off % 1200) + 1200) % 1200 ))
  WINDOW_X=$(( 100 + _norm ))
  WINDOW_Y=$(( 100 + _norm ))
fi
WINDOW_POSITION="${WINDOW_X},${WINDOW_Y}"
```

Run: `pytest tests/test_launch.py -k window_position -v`
Expected: PASS.

- [ ] **Step 3: commit.**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): per-port window-position offset, non-negative + capped (sub-A)"
```

---

## Task 4: `CHROME_BIN` + `mkdir -p` + chrome.log path (A.7, A.8, A.9)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (RED): tests for binary override (quoted), and the three log-path cases.**

Add to `tests/test_launch.py`:

```python
def test_chrome_bin_default_unescaped():
    r = _run_launch()
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["chrome_bin"] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert argv[0] == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def test_chrome_bin_override_with_space_preserved_as_one_token():
    """A.7: CHROME_BIN is honored and stays one argv token even with a space."""
    r = _run_launch(env_override={"CHROME_BIN": "/opt/My Browser/chrome"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["chrome_bin"] == "/opt/My Browser/chrome"
    assert argv[0] == "/opt/My Browser/chrome"


def test_log_path_9333_unchanged():
    r = _run_launch(env_override={"CDP_PORT": "9333"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["log"] == "/0/.jaine/.browser/chrome.log"


def test_log_path_inside_overridden_profile():
    """A.9: LOOK_PROFILE_DIR override → log INSIDE it (so rmtree(temp) cleans it)."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["log"] == "/tmp/lane-x/chrome.log"


def test_log_path_derived_next_to_per_port_profile():
    """A.9: derived per-port profile → chrome-<port>.log next to it."""
    r = _run_launch(env_override={"CDP_PORT": "9334"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["log"] == "/0/.jaine/.browser/chrome-9334.log"


def test_mkdir_p_present():
    """A.8: launch.sh must mkdir -p the profile + log dir before reads/redirection."""
    assert 'mkdir -p "$PROFILE_DIR" "$(dirname "$LOG")"' in LAUNCH_TEXT
```

Run: `pytest tests/test_launch.py -k "chrome_bin or log_path or mkdir" -v`
Expected: FAIL (binary + log hardcoded; no mkdir).

- [ ] **Step 2 (GREEN): parameterize CHROME_BIN, derive LOG, add mkdir.**

In `launch.sh`, replace (HEADLESS=0 is no longer adjacent — Task 1 moved it after the URL block):

```bash
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LOG="/0/.jaine/.browser/chrome.log"
```

with:

```bash
# Chrome binary: single change-point shared with tests/conftest.py (CHROME const).
CHROME_BIN="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

# chrome.log: 9333 → global path (unchanged); LOOK_PROFILE_DIR override → inside the
# (temp) profile so the fixture's rmtree removes it; else chrome-<port>.log next to
# the derived per-port profile.
if (( PROFILE_OVERRIDDEN )); then
  LOG="$PROFILE_DIR/chrome.log"
elif [[ "$CDP_PORT" == "9333" ]]; then
  LOG="/0/.jaine/.browser/chrome.log"
else
  LOG="$(dirname "$PROFILE_DIR")/chrome-${CDP_PORT}.log"
fi
```

Then add `mkdir -p` in the real-launch section, immediately after the `# ── Real launch ──` comment and before `pkill`:

```bash
# ── Real launch ──
mkdir -p "$PROFILE_DIR" "$(dirname "$LOG")"

# Kill existing JAINE browser on this profile
pkill -f "$KILL_MATCH" 2>/dev/null
```

Run: `pytest tests/test_launch.py -k "chrome_bin or log_path or mkdir" -v`
Expected: PASS. Re-run the byte-identical guard:

Run: `pytest tests/test_launch.py::test_default_invocation_argv_is_byte_identical -v`
Expected: PASS.

- [ ] **Step 3: commit.**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): CHROME_BIN override + per-lane chrome.log + mkdir -p (sub-A)"
```

---

## Task 5: Argument parser — flags, `--` terminator, unknown-flag fail-loud (A.2)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (RED): tests for the real parser.**

Add to `tests/test_launch.py`:

```python
def test_unknown_flag_fails_loud():
    """A.2: an unknown --flag is a fail-loud error (no silent fallback)."""
    r = _run_launch(args=["--bogus"])
    assert r.returncode != 0, "unknown flag must fail loud"
    assert "bogus" in r.stderr or "unknown" in r.stderr.lower()


def test_insecure_arg_rejected_as_unknown_flag():
    """A.2 + D.2: --insecure is D-owned and unsupported until D ships → unknown flag."""
    r = _run_launch(args=["--insecure"])
    assert r.returncode != 0, "--insecure must fail loud until sub-project D"
    assert "insecure" in r.stderr.lower() or "unknown" in r.stderr.lower()


def test_double_dash_terminator_forces_url():
    """A.2: a URL that starts with -- is accepted after the -- terminator."""
    r = _run_launch(args=["--", "--weird-url"])
    _, argv = _parse_dryrun(r.stdout)
    assert argv[-1] == "--weird-url", argv


def test_url_default_when_absent():
    r = _run_launch(args=["--headful"])
    _, argv = _parse_dryrun(r.stdout)
    assert argv[-1] == "about:blank", argv


def test_flag_after_url_recognized():
    """A.2: flags may appear before or after the URL (URL still parsed correctly).
    Headless WIRING is Task 6 — here we only assert the parser keeps the URL."""
    r = _run_launch(args=["https://x.test", "--headful"])
    _, argv = _parse_dryrun(r.stdout)
    assert argv[-1] == "https://x.test"
```

Run: `pytest tests/test_launch.py -k "unknown_flag or insecure_arg or terminator or url_default or flag_after" -v`
Expected: FAIL (no parser yet — `--bogus`/`--headful` currently land as the URL via `${1:-…}`).

- [ ] **Step 2 (GREEN): replace the single-positional URL read with a real parser.**

In `launch.sh`, replace the entire URL-resolution block:

```bash
# ── URL resolution (#60: normalize a bare absolute path to a file:// URL) ──
# Delegate to `cdp.py normalize-url` — the SINGLE source of truth (guard +
# pathlib.as_uri live in cdp.py normalize_url). Cheap-skip non-slash URLs.
URL="${1:-about:blank}"
if [[ "$URL" == /* ]]; then
  normalized=$(python3 "$SCRIPT_DIR/cdp.py" normalize-url "$URL" 2>/dev/null)
  if [[ -n "$normalized" ]]; then
    URL="$normalized"
  fi
fi
```

with:

```bash
# ── Argument parsing ──
# Recognize --headless/--headful (NOT --insecure — that flag is D's; until
# sub-project D it is an unknown flag → fail-loud). A `--` terminator forces the
# next token to be the URL (lets a URL legitimately starting with -- through).
# First non-flag token is the URL; unknown --flag → fail-loud (no silent fallback).
HEADLESS_ARG=""    # "", "0" (headful) or "1" (headless)
URL=""
URL_SET=0
SAW_TERMINATOR=0
for a in "$@"; do
  if (( SAW_TERMINATOR )); then
    if (( ! URL_SET )); then URL="$a"; URL_SET=1; fi
    continue
  fi
  case "$a" in
    --)        SAW_TERMINATOR=1 ;;
    --headless) HEADLESS_ARG=1 ;;
    --headful)  HEADLESS_ARG=0 ;;
    --*)
      echo "ERROR: unknown flag '$a' (look launcher accepts --headless/--headful)" >&2
      exit 1
      ;;
    *) if (( ! URL_SET )); then URL="$a"; URL_SET=1; fi ;;
  esac
done
if (( ! URL_SET )); then URL="about:blank"; fi

# #60: normalize a bare absolute path to a file:// URL. Delegate to
# `cdp.py normalize-url` — the SINGLE source of truth. Cheap-skip non-slash URLs.
if [[ "$URL" == /* ]]; then
  normalized=$(python3 "$SCRIPT_DIR/cdp.py" normalize-url "$URL" 2>/dev/null)
  if [[ -n "$normalized" ]]; then
    URL="$normalized"
  fi
fi
```

Run: `pytest tests/test_launch.py -k "unknown_flag or insecure_arg or terminator or url_default or flag_after" -v`
Expected: PASS. Re-run the byte-identical guard:

Run: `pytest tests/test_launch.py::test_default_invocation_argv_is_byte_identical -v`
Expected: PASS.

- [ ] **Step 3: commit.**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): real arg parser — flags, -- terminator, unknown-flag fail-loud (sub-A)"
```

---

## Task 6: Headless precedence + skip osascript/Local-State (A.3)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (RED): tests for headless resolution + headful-only gating.**

Add to `tests/test_launch.py`:

```python
def test_headless_env_truthy_adds_headless_new():
    for val in ("1", "true", "TRUE", "yes", "Yes"):
        r = _run_launch(env_override={"LOOK_HEADLESS": val})
        cfg, argv = _parse_dryrun(r.stdout)
        assert cfg["headless"] == "1", "LOOK_HEADLESS={} should be headless".format(val)
        assert "--headless=new" in argv


def test_headless_env_falsy_stays_headful():
    for val in ("0", "false", "no", ""):
        r = _run_launch(env_override={"LOOK_HEADLESS": val})
        cfg, argv = _parse_dryrun(r.stdout)
        assert cfg["headless"] == "0"
        assert "--headless=new" not in argv


def test_headless_flag_overrides_env_both_directions():
    """A.3: --headful beats LOOK_HEADLESS=1; --headless beats LOOK_HEADLESS=0."""
    r = _run_launch(args=["--headful"], env_override={"LOOK_HEADLESS": "1"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["headless"] == "0"
    r2 = _run_launch(args=["--headless"], env_override={"LOOK_HEADLESS": "0"})
    cfg2, argv2 = _parse_dryrun(r2.stdout)
    assert cfg2["headless"] == "1"
    assert "--headless=new" in argv2


def test_headless_flag_after_url_sets_headless():
    """A.2+A.3: a flag after the URL still wires headless (combined parse+resolve)."""
    r = _run_launch(args=["https://x.test", "--headless"])
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["headless"] == "1" and argv[-1] == "https://x.test"


def test_headless_skips_osascript_and_local_state():
    """A.3: headless lane skips both osascript blocks + the Local-State pre-patch."""
    r = _run_launch(env_override={"LOOK_HEADLESS": "1"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["osascript"] == "0"
    assert cfg["local_state_patch"] == "0"


def test_headful_default_runs_osascript_and_local_state():
    r = _run_launch()
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["osascript"] == "1"
    assert cfg["local_state_patch"] == "1"


def test_headless_new_appears_before_url():
    r = _run_launch(env_override={"LOOK_HEADLESS": "1"})
    _, argv = _parse_dryrun(r.stdout)
    assert argv.index("--headless=new") == len(argv) - 2, "expected --headless=new just before the URL"
    assert argv[-1] == "about:blank"


def test_local_state_and_osascript_are_headful_gated_structurally():
    """A.3: the Local-State patch + osascript blocks live behind a headful guard."""
    assert LAUNCH_TEXT.count('if [[ "$HEADLESS" != "1" ]]; then') >= 2


def test_double_dash_url_gets_chrome_end_of_options_separator():
    """R1-F3: a URL beginning with -- gets a Chrome `--` end-of-options separator in
    the argv (dry-run prints the real CHROME_ARGV), else Chrome parses it as a flag."""
    r = _run_launch(args=["--", "--weird-url"])
    _, argv = _parse_dryrun(r.stdout)
    assert argv[-2:] == ["--", "--weird-url"], argv


def test_normal_url_has_no_end_of_options_separator():
    """R1-F3: the -- separator appears ONLY for --prefixed URLs — default argv intact."""
    r = _run_launch(args=["https://x.test"])
    _, argv = _parse_dryrun(r.stdout)
    assert argv[-1] == "https://x.test" and argv[-2] != "--", argv
```

Run: `pytest tests/test_launch.py -k headless -v`
Expected: FAIL (HEADLESS hardcoded to 0; `--headless=new` never added; osascript/Local-State unguarded).

- [ ] **Step 2 (GREEN): resolve HEADLESS, append `--headless=new`, gate the headful-only steps.**

In `launch.sh`, replace the `HEADLESS=0` line (Task 1 placed it AFTER the argument-parsing block, so `HEADLESS_ARG` is already set when this resolves — this ordering is load-bearing) with the precedence resolution. Replace:

```bash
HEADLESS=0
```

with:

```bash
# Headless: arg wins over env; else LOOK_HEADLESS truthy (1/true/yes, case-insensitive).
shopt -s nocasematch
if [[ "${LOOK_HEADLESS:-}" =~ ^(1|true|yes)$ ]]; then
  _env_headless=1
else
  _env_headless=0
fi
shopt -u nocasematch
if [[ -n "$HEADLESS_ARG" ]]; then
  HEADLESS="$HEADLESS_ARG"
else
  HEADLESS="$_env_headless"
fi
```

Append `--headless=new` to the array when headless. In the `CHROME_ARGV+=("$URL")` region, replace:

```bash
CHROME_ARGV+=("$URL")
```

with:

```bash
if (( HEADLESS )); then
  CHROME_ARGV+=(--headless=new)
fi
# Chrome end-of-options: a URL beginning with -- must not be parsed as a Chrome flag
# (R1-F3). The --headless=new flag above MUST precede this -- separator, or Chrome
# would treat --headless=new itself as a positional (post---) argument.
if [[ "$URL" == --* ]]; then
  CHROME_ARGV+=(--)
fi
CHROME_ARGV+=("$URL")
```

Gate the Local-State pre-patch. Replace:

```bash
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
```

with:

```bash
# Pre-patch Local State to enable AppleScript JS (headful only — no GUI when headless)
if [[ "$HEADLESS" != "1" ]]; then
  LOCAL_STATE="$PROFILE_DIR/Local State"
  if [ -f "$LOCAL_STATE" ]; then
    python3 -c "
import json
with open('$LOCAL_STATE') as f: s = json.load(f)
s.setdefault('browser', {})['allow_javascript_apple_events'] = True
with open('$LOCAL_STATE', 'w') as f: json.dump(s, f)
"
  fi
fi
```

Gate the two osascript blocks. Replace the block from the first `# Auto-enable AppleScript JS via menu click` comment through the second osascript invocation's closing `end tell' 2>/dev/null` with the same content wrapped in a single headful guard:

```bash
# AppleScript JS enablement (headful only — no GUI/menus when headless)
if [[ "$HEADLESS" != "1" ]]; then
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
fi
```

Run: `pytest tests/test_launch.py -k headless -v`
Expected: PASS. Re-run byte-identical + full file:

Run: `pytest tests/test_launch.py -v`
Expected: PASS (all).

- [ ] **Step 3: commit.**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): --headless=new precedence + headful-only osascript/Local-State (sub-A)"
```

---

## Task 7: `pkill` lane-scoping — spike + anchored/escaped match (A.5)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (SPIKE — validate macOS `pkill` ERE anchoring BEFORE implementing).**

The risk (spec §9): does macOS `pgrep -f`/`pkill -f` honor an ERE anchor `($|[[:space:]])` so `…/profile` does NOT match `…/profile-9334`? Run this experiment and record the result in the commit message:

```bash
# Two fake processes whose argv contains each profile path (exec -a sets argv[0]).
( exec -a "chrome --user-data-dir=/tmp/pk/profile --remote-debugging-port=9333" sleep 60 ) &
( exec -a "chrome --user-data-dir=/tmp/pk/profile-9334 --remote-debugging-port=9334" sleep 60 ) &
sleep 0.5
echo "--- anchored pattern should match ONLY the default profile, not -9334 ---"
pgrep -f -- 'user-data-dir=/tmp/pk/profile($|[[:space:]])'   # expect: exactly ONE pid
echo "--- cleanup ---"
pkill -f -- 'user-data-dir=/tmp/pk/profile($|[[:space:]])'
pkill -f 'user-data-dir=/tmp/pk/profile-9334'
```

Expected: the `pgrep` prints exactly one PID (the default-profile process); the `-9334` process is untouched. If the ERE anchor is NOT honored on this macOS, fall back to matching `--user-data-dir=<escaped> ` (trailing space) plus a separate end-of-arg case, and update the implementation + tests below to the validated form. **Do not proceed to Step 2 until the spike confirms the exact form.**

- [ ] **Step 2 (RED): tests for the anchored + escaped kill_match.**

Add to `tests/test_launch.py` (assuming the spike confirmed the `($|[[:space:]])` form — adjust the literals if the spike chose a different anchor):

```python
def test_kill_match_default_is_anchored_and_escaped():
    """A.5: pkill match anchors to an arg boundary + regex-escapes the path so
    `…/profile` cannot kill `…/profile-9334`."""
    r = _run_launch()
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["kill_match"] == r"--user-data-dir=/0/\.jaine/\.browser/profile($|[[:space:]])", \
        "kill_match not anchored/escaped: {!r}".format(cfg["kill_match"])


def test_kill_match_escapes_regex_metachars():
    r = _run_launch(env_override={"LOOK_PROFILE_DIR": "/tmp/a.b+c(d)"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["kill_match"] == r"--user-data-dir=/tmp/a\.b\+c\(d\)($|[[:space:]])", \
        "metachars not escaped: {!r}".format(cfg["kill_match"])


def test_real_pkill_uses_kill_match_anchored():
    """A.5: the real pkill uses the anchored match (-- guards a path starting with -)."""
    assert 'pkill -f -- "$KILL_MATCH"' in LAUNCH_TEXT
```

Run: `pytest tests/test_launch.py -k kill_match -v`
Expected: FAIL (kill_match is today's unanchored `user-data-dir=$PROFILE_DIR`).

- [ ] **Step 3 (GREEN): add the escaper + anchored match; use `pkill -f --`.**

In `launch.sh`, add an escaper function near the top (after `SCRIPT_DIR=…`):

```bash
# Backslash-escape ERE metacharacters so an arbitrary profile path matches
# literally in pkill -f (A.5). Realistic path metachars: . [ ] ( ) { } ^ $ * + ? |
# (a literal backslash in a path is pathological and out of scope).
_escape_ere() {
  printf '%s' "$1" | sed -E 's/([].[(){}^$*+?|])/\\\1/g'
}
```

Replace:

```bash
# ── pkill match (today's unanchored form; lane-scoped in Task 7) ──
KILL_MATCH="user-data-dir=$PROFILE_DIR"
```

with:

```bash
# ── pkill match: anchored to an arg boundary + regex-escaped so the default
#    `…/profile` never kills `…/profile-9334` (A.5) ──
KILL_MATCH="--user-data-dir=$(_escape_ere "$PROFILE_DIR")(\$|[[:space:]])"
```

Replace the real-launch `pkill` line:

```bash
pkill -f "$KILL_MATCH" 2>/dev/null
```

with:

```bash
pkill -f -- "$KILL_MATCH" 2>/dev/null
```

Run: `pytest tests/test_launch.py -k kill_match -v`
Expected: PASS.

- [ ] **Step 4: commit (include the spike result in the message).**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): lane-scoped pkill — anchored + regex-escaped match (sub-A, A.5)

Spike: macOS pgrep -f honors ERE ($|[[:space:]]); profile vs profile-9334 verified."
```

---

## Task 8: `LOOK_INSECURE` reserved-env guard — two-mechanism refusal (A.1 / D.2 / R3-F1)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Modify: `tests/test_launch.py`

> Why here, not in sub-project D: D *owns* the flag, but A must already fail-loud for BOTH the `--insecure` **arg** (Task 5's unknown-flag handler) and the `LOOK_INSECURE` **env** (a reserved-env guard), so that no pre-D state silently ignores it. This is the R3-F1 ownership consistency the spec spent 3 review rounds on. The env path cannot be caught by the arg parser — it needs its own guard.

- [ ] **Step 1 (RED): tests pinning both refusal mechanisms.**

Add to `tests/test_launch.py`:

```python
def test_look_insecure_env_reserved_until_d():
    """A.1/D.2: a set LOOK_INSECURE env fails loud (arg parser only sees argv)."""
    r = _run_launch(env_override={"LOOK_INSECURE": "1"})
    assert r.returncode != 0, "LOOK_INSECURE env must fail loud until sub-project D"
    assert "LOOK_INSECURE" in r.stderr


def test_insecure_refused_by_two_distinct_mechanisms():
    """R3-F1: the --insecure ARG and the LOOK_INSECURE ENV are each fail-loud,
    by distinct mechanisms (arg via unknown-flag; env via reserved-env guard)."""
    arg = _run_launch(args=["--insecure"])
    env = _run_launch(env_override={"LOOK_INSECURE": "1"})
    assert arg.returncode != 0 and env.returncode != 0
    # the env guard exists as an explicit reserved-env check (not just arg parsing)
    assert '[ -n "${LOOK_INSECURE:-}" ]' in LAUNCH_TEXT or '-n "${LOOK_INSECURE:-}"' in LAUNCH_TEXT
```

Run: `pytest tests/test_launch.py -k insecure -v`
Expected: `test_look_insecure_env_reserved_until_d` FAILS (env is currently ignored — only the `--insecure` arg is rejected, by Task 5).

- [ ] **Step 2 (GREEN): add the reserved-env guard early.**

In `launch.sh`, immediately after the port validation block (before profile resolution), add:

```bash
# LOOK_INSECURE is reserved for sub-project D (web-security lane). Until D ships,
# refuse it loudly — the --insecure ARG is rejected by the arg parser (unknown
# flag); the ENV needs its own guard because the parser only sees argv (R3-F1).
if [ -n "${LOOK_INSECURE:-}" ]; then
  echo "ERROR: LOOK_INSECURE is reserved and not yet supported (sub-project D); refusing to launch. Unset it." >&2
  exit 1
fi
```

Run: `pytest tests/test_launch.py -k insecure -v`
Expected: PASS.

- [ ] **Step 3: run the whole launch unit suite + bash 3.2 smoke; commit.**

Add a bash-3.2 compatibility smoke test (the project ships a sibling regression for `log-round.sh`):

```python
@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash 3.2 path missing")
def test_dry_run_works_under_bash_32():
    """launch.sh must dry-run cleanly under macOS /bin/bash (3.2): arrays,
    printf, shopt nocasematch, (( )) — no bash-4-only constructs."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_HEADLESS": "1"}, bash="/bin/bash")
    assert r.returncode == 0, "bash 3.2 dry-run failed: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["headless"] == "1" and "--headless=new" in argv
```

Run: `pytest tests/test_launch.py -v`
Expected: PASS (all, including the bash-3.2 smoke).

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(look): LOOK_INSECURE reserved-env guard — two-mechanism refusal (sub-A, R3-F1)"
```

---

## Task 9: conftest collapse onto `launch.sh` (A.11)

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_launch.py`

- [ ] **Step 1 (RED): test the `_reuse_decision` helper + the CHROME_BIN drift guard.**

Add to `tests/test_launch.py`:

```python
def test_reuse_decision_matrix():
    """A.11: 9333 reuses-or-launches; a non-9333 test lane fails loud on an
    unexpected pre-existing listener (never silent reuse)."""
    from conftest import _reuse_decision
    assert _reuse_decision(9333, True) == "reuse"
    assert _reuse_decision(9333, False) == "launch"
    assert _reuse_decision(9355, True) == "fail"
    assert _reuse_decision(9355, False) == "launch"


def test_conftest_chrome_const_matches_launch_default():
    """A.7: the CHROME_BIN default in launch.sh must equal conftest.CHROME
    (single change-point — change both or neither)."""
    from conftest import CHROME
    assert ('CHROME_BIN="${CHROME_BIN:-' + CHROME + '}"') in LAUNCH_TEXT


def test_conftest_default_lane_is_isolated_headless():
    """A.11: a bare pytest defaults to a non-9333 headless lane (no env reaches 9333)."""
    from conftest import CDP_PORT, LANE_IS_HEADLESS, TEST_CDP_PORT
    # default (no CDP_PORT in this process env) resolves to the dedicated test port
    if "CDP_PORT" not in os.environ:
        assert CDP_PORT == TEST_CDP_PORT
        assert CDP_PORT != 9333
        assert LANE_IS_HEADLESS is True


def test_conftest_kill_pattern_anchored_and_escaped():
    """R2-F2: conftest's cleanup pattern anchors + escapes (mirrors launch.sh A.5),
    so an explicit 9333 run can't cross-kill a /profile-9334 lane."""
    from conftest import _kill_pattern
    assert _kill_pattern("/0/.jaine/.browser/profile") == \
        r"--user-data-dir=/0/\.jaine/\.browser/profile($|[[:space:]])"


def test_conftest_cleanup_uses_pkill_dashdash():
    """R2-F2: BOTH fixture pkill calls pass -- (the pattern starts with --); the old
    unanchored form must be gone."""
    conftest_text = (PLUGIN_ROOT / "tests" / "conftest.py").read_text()
    assert 'subprocess.run(["pkill", "-f", "--", kill_match]' in conftest_text
    assert 'subprocess.run(["pkill", "-f", kill_match]' not in conftest_text
```

Run: `pytest tests/test_launch.py -k "reuse_decision or chrome_const or isolated_headless" -v`
Expected: FAIL (`_reuse_decision`, `_kill_pattern`, `TEST_CDP_PORT`, `LANE_IS_HEADLESS` don't exist yet; conftest still has the unanchored `pkill`; launch.sh CHROME_BIN default form differs from the asserted literal only if mismatched — verify it matches).

- [ ] **Step 2 (GREEN): rewrite conftest constants + the fixture.**

First add `import re` to the conftest imports (used by `_kill_pattern` below).

In `tests/conftest.py`, replace:

```python
CDP_PORT = int(os.environ.get("CDP_PORT", "9333"))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

with:

```python
# The e2e default is itself an isolated lane: a bare `pytest` (no CDP_PORT) drives
# a dedicated NON-9333 headless test browser, never the user's daily 9333 browser.
# Driving the daily browser is explicit opt-in: CDP_PORT=9333 pytest …
TEST_CDP_PORT = 9355
CDP_PORT = int(os.environ.get("CDP_PORT", str(TEST_CDP_PORT)))
LANE_IS_HEADLESS = CDP_PORT != 9333
# Shared Chrome-binary reference (A.7): launch.sh's CHROME_BIN default must match.
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _reuse_decision(port, is_online):
    """Return 'reuse' | 'launch' | 'fail' for the jaine_browser fixture (A.11).

    port == 9333 (explicit opt-in to drive the user's daily browser): reuse an
    already-running browser, else launch one. A non-9333 test lane: a pre-existing
    CDP listener is UNEXPECTED — refuse to silently reuse a browser the fixture
    does not own (isolation guarantee) → fail loud; otherwise launch fresh.
    """
    if port == 9333:
        return "reuse" if is_online else "launch"
    return "fail" if is_online else "launch"


def _kill_pattern(profile):
    """Anchored + escaped pkill pattern (mirrors launch.sh's A.5 form) so fixture
    cleanup never cross-kills a sibling lane — e.g. /profile must not match
    /profile-9334 (R2-F2). Use with `pkill -f -- <pattern>`."""
    return "--user-data-dir=" + re.escape(profile) + r"($|[[:space:]])"
```

Then replace the whole `jaine_browser` fixture body with the unified launch path:

```python
@pytest.fixture(scope="session")
def jaine_browser():
    """Ensure a JAINE Browser on CDP_PORT via launch.sh (unified path, A.11).

    Default (no env): an isolated headless lane on TEST_CDP_PORT with a temp
    profile — never touches the user's daily 9333 browser. CDP_PORT=9333 drives
    the daily browser (reuse-if-online). A pre-existing listener on a non-9333
    test port is a fail-loud setup error (isolation), never silent reuse.
    """
    decision = _reuse_decision(CDP_PORT, _cdp_is_online())
    if decision == "fail":
        pytest.fail(
            "Unexpected CDP listener already on test port {0} — refusing to reuse "
            "a browser the fixture does not own (isolation). Kill it "
            "(pkill -f remote-debugging-port={0}) and re-run.".format(CDP_PORT)
        )
    if decision == "reuse":
        yield "reused"
        return

    env = os.environ.copy()
    env["CDP_PORT"] = str(CDP_PORT)
    temp_profile = None
    if CDP_PORT == 9333:
        kill_match = _kill_pattern(BROWSER_PROFILE)
    else:
        temp_profile = tempfile.mkdtemp(prefix="jaine-test-{}-".format(CDP_PORT))
        env["LOOK_PROFILE_DIR"] = temp_profile
        env["LOOK_HEADLESS"] = "1"
        kill_match = _kill_pattern(temp_profile)
    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_is_online():
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
        if temp_profile:
            shutil.rmtree(temp_profile, ignore_errors=True)
        pytest.fail("JAINE Browser did not start on port {} within 20s".format(CDP_PORT))

    yield "launched"

    subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
    if temp_profile:
        shutil.rmtree(temp_profile, ignore_errors=True)
```

The direct `subprocess.Popen([CHROME, "--user-data-dir=…", …])` block is removed (its job is now `launch.sh` with `LOOK_PROFILE_DIR` + `LOOK_HEADLESS`). `CHROME` stays only as the shared-reference const.

Run: `pytest tests/test_launch.py -k "reuse_decision or chrome_const or isolated_headless" -v`
Expected: PASS.

- [ ] **Step 3: commit.**

```bash
git add tests/conftest.py tests/test_launch.py
git commit -m "test(look): collapse conftest onto launch.sh — isolated headless default lane (sub-A, A.11)"
```

---

## Task 10: e2e headful-only window skip + full headless e2e green (A.11 monitor)

**Files:**
- Modify: `tests/test_e2e.py`

- [ ] **Step 1: mark the AppleScript-only window test headful-only.**

In `tests/test_e2e.py`, add `import pytest` (the file currently does not import it) below the existing imports, and update the import-from-conftest line to also pull `LANE_IS_HEADLESS`:

```python
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_cdp, LANE_IS_HEADLESS  # noqa: E402
```

Then decorate the window test:

```python
@pytest.mark.skipif(
    LANE_IS_HEADLESS,
    reason="window bounds is AppleScript-only until sub-project B ports it to CDP "
           "(headless has no GUI)",
)
def test_window_bounds_returns_coords(jaine_browser):
    r = run_cdp(["window", "bounds"])
    assert r.returncode == 0, "window bounds failed: {}".format(r.stderr)
    assert "," in r.stdout, "Expected comma-separated bounds, got: {}".format(r.stdout)
```

- [ ] **Step 2: run the full e2e suite against the default isolated headless lane.**

> Requires a working Chrome on this machine. This is the A-acceptance behavioral gate (spec §4 A-acceptance, §8). The window test is skipped (headless), everything else must pass via the CDP/websocket path.

Run: `pytest tests/test_e2e.py -v`
Expected: PASS for every content command (status, tabs, navigate, screenshot, js, click, fill, wait, console, network, pdf, viewport, clip/scale), `test_window_bounds_returns_coords` SKIPPED. If the 20s startup deadline is flaky on a cold profile (spec §9), bump the fixture deadline and note it.

- [ ] **Step 3: commit.**

```bash
git add tests/test_e2e.py
git commit -m "test(look): window-bounds e2e headful-only until sub-project B (sub-A)"
```

---

## Task 11: Docs — SKILL.md lane model + CLAUDE.md test table (A.12)

**Files:**
- Modify: `skills/look/SKILL.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: document the lane model + headless implications + LOOK_DRY_RUN in SKILL.md.**

In `skills/look/SKILL.md`, in the **## Browser Setup** section (after the existing `- **Launcher:** …` line), add a lane subsection. Keep the existing default-invocation docs unchanged:

```markdown
### Lanes (parallel + headless)

A *lane* is an isolated browser = a set of env vars; the default invocation (no
env, no flag) is unchanged.

```bash
# Isolated headless lane on port 9334 (own temp profile):
CDP_PORT=9334 LOOK_PROFILE_DIR=/tmp/lane-a LOOK_HEADLESS=1 \
  "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/launch.sh" "http://localhost:9401"
CDP_PORT=9334 python3 "$CDP" screenshot /tmp/a.jpg
```

| Knob | Source | Default |
|------|--------|---------|
| Port | `CDP_PORT` env | `9333` |
| Profile | `LOOK_PROFILE_DIR` env, else derived from port | `9333 → /0/.jaine/.browser/profile`; else `…/profile-<port>` |
| Headless | `--headless`/`--headful` arg (wins), else `LOOK_HEADLESS` truthy | headful |
| Window pos (headful) | derived from port | `9333 → 100,100` |
| Chrome binary | `CHROME_BIN` env | macOS Chrome path |

**Headless ⇒ websocket-only.** The AppleScript DOM channel and the macOS-native
screenshot fallback both need a GUI and are unavailable headless; with bundled
`websocket-client` present, every content command (navigate/screenshot/js/click/
fill/wait/console/network) works over CDP. `window upper/lower/activate` are
headful-only ergonomics. Audio: a trusted click satisfies user-activation but a
headless browser has no output device → functional verification yes, audible no.

**Dry run:** `LOOK_DRY_RUN=1 …/launch.sh url` prints the resolved config + full
Chrome argv and exits without launching — useful to confirm a lane's flags.
```

- [ ] **Step 2: add `test_launch.py` to the CLAUDE.md `/look` test row.**

In `CLAUDE.md`, in the **Testing → Test suites** table, the `/look` row currently lists `test_cdp.py`, `test_e2e.py`. Update it to include the new file:

```markdown
| `/look` | `test_cdp.py`, `test_launch.py`, `test_e2e.py` | structural + behavioral | `test_e2e.py` → JAINE Browser |
```

- [ ] **Step 3: verify SKILL.md structural tests still pass; commit.**

Run: `bash tests/test_plugin_structure.sh && pytest tests/test_cdp.py -k "issue_56 or launch" -v`
Expected: PASS (look SKILL.md keeps its `launch.sh` reference; the `test_issue_56_*` guards confirm no `launch.sh" "$ARGUMENTS"` pattern was introduced).

```bash
git add skills/look/SKILL.md CLAUDE.md
git commit -m "docs(look): lane model + LOOK_DRY_RUN in SKILL.md; CLAUDE.md test row (sub-A)"
```

---

## Task 12: Final acceptance — full offline suite green (A-acceptance)

**Files:** none (verification only)

- [ ] **Step 1: run the entire offline test suite.**

Run: `pytest tests/ -v --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py`
Expected: PASS (all structural + unit tests, including the new `test_launch.py`). No regressions in `test_cdp.py`, `test_plugin_structure.sh` (run separately: `bash tests/test_plugin_structure.sh`).

- [ ] **Step 2: confirm the A-acceptance checklist (spec §4 "A — acceptance") against the suite.**

Tick each against a passing test:
- Default lane byte-identical → `test_default_invocation_argv_is_byte_identical`
- `CDP_PORT=9334` → `profile-9334` + capped offset + port in argv → `test_cdp_port_threads_into_argv`, `test_profile_derived_from_non_default_port`, `test_window_position_derived_above_9333`
- `LOOK_PROFILE_DIR` verbatim + log inside it → `test_look_profile_dir_used_verbatim`, `test_log_path_inside_overridden_profile`
- headless (env and `--headless`) → `--headless=new`, osascript/Local-State skipped → `test_headless_env_truthy_adds_headless_new`, `test_headless_skips_osascript_and_local_state`
- `--headful` overrides `LOOK_HEADLESS=1` → `test_headless_flag_overrides_env_both_directions`
- `CHROME_BIN` honored + quoted → `test_chrome_bin_override_with_space_preserved_as_one_token`
- fail-loud on unknown `--flag` / non-numeric & out-of-range port → `test_unknown_flag_fails_loud`, `test_non_numeric_port_fails_loud`, `test_out_of_range_port_fails_loud`
- `pkill` does not cross lanes → `test_kill_match_default_is_anchored_and_escaped`, `test_kill_match_escapes_regex_metachars` (+ spike)
- `LOOK_INSECURE`/`--insecure` fail-loud (two mechanisms) → `test_insecure_refused_by_two_distinct_mechanisms`
- existing e2e green via unified launch path (headless), window test skipped-headful → Task 10 Step 2

- [ ] **Step 3: hand off to sub-project B.**

Sub-project A is complete. Next per spec §3: sub-project **B** (window-over-CDP) — its own plan→TDD→PR. After B lands, remove the Task-10 headful-only skip on `test_window_bounds_returns_coords`.

---

## Notes on what is deliberately NOT in sub-project A

- **No `set -euo pipefail`** in launch.sh (preserves today's tolerant behavior; new fail-loud paths exit explicitly).
- **No window-position removal when headless** — `--window-position` is included always (harmless/ignored headless) to keep `CHROME_ARGV` construction uniform and the characterization stable. "Headful only" describes where it has effect, not strict presence.
- **No auto-port allocation / lane registry** — a lane is just env vars; the caller owns them (spec §1 out-of-scope).
- **`LOOK_INSECURE` is refused, not implemented** — the actual web-security relaxation is sub-project D (#93). A only ships the fail-loud guard for ownership consistency (R3-F1).
