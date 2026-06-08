# SP1 — Chrome for Testing Foundation (#164) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the engine (`cdp.py` + `launch.sh`) a Chrome-for-Testing automation lane — parameterized app-name, pinned CfT install, fail-closed `--automation` gate, temp drive-profiles, CfT-aware e2e fixture — without changing the `/look` daily-lane behavior by a single byte.

**Architecture:** Shared engine, two fronts (spec §4.1). SP1 adds *mechanism only*: a new `--automation` / `LOOK_AUTOMATION` lane in `launch.sh` (CfT binary default + `--enable-automation` + `--use-mock-keychain`, gated to non-9333 + non-daily-profile), `CHROME_APP_NAME` threading for every AppleScript/Quartz touchpoint, and `update-cft.sh` as the single pin-mover for `/0/.jaine/.browser/cft/current`. The `/drive` *policy* (SKILL.md, verify-core) is SP2.

**Tech Stack:** bash (launch.sh, update-cft.sh), Python 3 stdlib (cdp.py, tests), pytest, Chrome for Testing mac-arm64 (googlechromelabs last-known-good JSON).

**Spec:** `docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` §4.2 + §5 SP1 row. Holes addressed: **B** (owner collision), **C/R1-C** (gate not port-only), **E** (temp profiles), **G** (locale menus), **I** (infobar empirical), **J** (AppleScript hang), **K** (keychain).

**Branch:** `bulldozer/drive-sp1`, worktree `/0/.aitemp/bulldozer-drive-sp1`.

---

## Verified ground truth (2026-06-05, this worktree @ 7d5b277)

- `cdp.py:52` — `CHROME_APP = "Google Chrome"`; **9 callsites**, all through the symbol (lines 245, 263, 273, 286, 915, 948, 957, 961, 965). Only line 286 has *logic*: `owner=CHROME_APP.split()[0]` → matches any window owner containing `"Google"` (hole B).
- `cdp.py:214` — `osascript()` already has `subprocess.run(..., timeout=10)` → hole J is already half-closed on the Python side; `launch.sh` osascript calls have **no** timeout.
- `launch.sh` — 3 app-name literals at lines 268/271/282 (single-quoted osascript blocks); `CHROME_BIN` default at line 69 (env-parameterized *path*, stays stock); insecure gate at lines 136-179 is the exact fail-closed pattern to reuse; `LOOK_DRY_RUN` block at 216-234 is the TDD seam; dry-run prints `key=value` lines then `ARGV`.
- `tests/conftest.py` — `LANE_ENV_VARS` (line 53) strips 6 lane vars; `CHROME` baseline const line 48 (stays stock, A.7); `jaine_browser` fixture launches non-9333 lanes headless with temp profile; `_cdp_is_online()` is hardwired to module `CDP_PORT`.
- `tests/test_launch.py` — `_run_launch` + `_parse_dryrun` + `EXPECTED_DEFAULT_ARGV` (byte-identical default-lane guard — must keep passing untouched).
- `tests/test_cdp.py` — subprocess-CLI convention (`run_cdp`); no in-process import today.
- CfT mac-arm builds exist in `~/.cache/puppeteer/chrome/` (newest `mac_arm-134.0.6998.35`) — confirms platform/layout `chrome-mac-arm64/Google Chrome for Testing.app`.
- No pytest config file → e2e files are self-contained by convention (fixture auto-launches; `pytest.skip` when external dep missing).

## Non-goals (SP2+, do NOT build here)

navigate-wait, console streaming, assertion primitive, cookie-seed, `skills/drive/SKILL.md`, co-pilot/autonomous modes, Playwright (`--engine`), lane allocator/ownership (SP4), dropping `--enable-automation` in co-pilot-headful (SP2 decision — SP1 only *measures* R1-I).

---

### Task 1: cdp.py — `CHROME_APP_NAME` env parameterization

**Files:**
- Modify: `skills/look/scripts/cdp.py` (line 52)
- Test: `tests/test_cdp.py`

- [ ] **Step 1.1: Write the failing tests**

Append to `tests/test_cdp.py` (module level, after existing helpers). The in-process import helper is needed because `CHROME_APP` is a module constant with no CLI surface; `exec_module` is safe — cdp.py guards `main()` behind `__name__ == "__main__"`:

```python
# ── SP1: CHROME_APP_NAME parameterization (spec §4.2) ──

def _import_cdp(env_override=None):
    """Import cdp.py as a fresh module in a subprocess-free way.

    Constants like CHROME_APP are bound at import; passing env via os.environ
    around exec_module would leak across tests — so run the import in a child
    interpreter (keeps the file's subprocess-CLI convention).
    """
    code = (
        "import importlib.util; "
        "spec = importlib.util.spec_from_file_location('cdp_mod', {!r}); "
        "m = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(m); "
        "print(m.CHROME_APP)"
    ).format(CDP_SCRIPT)
    env = os.environ.copy()
    env.pop("CHROME_APP_NAME", None)
    if env_override:
        env.update(env_override)
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=10, env=env)


def test_chrome_app_name_env_overrides():
    r = _import_cdp({"CHROME_APP_NAME": "Google Chrome for Testing"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Google Chrome for Testing"


def test_chrome_app_default_is_stock():
    r = _import_cdp()
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "Google Chrome"
```

- [ ] **Step 1.2: Run to verify RED**

Run: `python3 -m pytest tests/test_cdp.py -k chrome_app -v`
Expected: `test_chrome_app_name_env_overrides` FAILS (`'Google Chrome' == 'Google Chrome for Testing'` mismatch); `test_chrome_app_default_is_stock` passes.

- [ ] **Step 1.3: Implement**

In `skills/look/scripts/cdp.py` replace line 52:

```python
CHROME_APP = "Google Chrome"
```

with:

```python
# SP1 (#164): AppleScript/Quartz app name. Drive lanes set "Google Chrome for
# Testing"; default stays the stock daily browser. All 9 callsites read this symbol.
CHROME_APP = os.environ.get("CHROME_APP_NAME", "Google Chrome")
```

- [ ] **Step 1.4: Run to verify GREEN**

Run: `python3 -m pytest tests/test_cdp.py -v`
Expected: all pass (new 2 + existing).

- [ ] **Step 1.5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "feat(sp1): parameterize cdp.py app name via CHROME_APP_NAME (#164)"
```

---

### Task 2: cdp.py — hole B: lane-pid window match (no `split()[0]`)

**Files:**
- Modify: `skills/look/scripts/cdp.py` (`native_screenshot`, lines 277-303)
- Test: `tests/test_cdp.py`

Current defect: the Quartz window lookup matches `'Google' in kCGWindowOwnerName` — collides with "Google Chrome for Testing", "Google Drive", any Google app (R1-B + R2). Fix: match the *lane's* browser-process pid (resolved from `--remote-debugging-port=<port>` — only the browser process carries it; helpers carry `--type=…`), falling back to **exact** owner-name == `CHROME_APP`.

- [ ] **Step 2.1: Write the failing tests**

Append to `tests/test_cdp.py`:

```python
# ── SP1: hole B — native_screenshot owner match ──

class _FakePgrep:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _load_cdp_module():
    """In-process import for unit-testing pure helpers (no CLI surface)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cdp_unit", CDP_SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_chrome_pid_for_port_returns_first_pid():
    m = _load_cdp_module()
    calls = {}

    def fake_runner(cmd, **kw):
        calls["cmd"] = cmd
        return _FakePgrep(returncode=0, stdout="4242\n4300\n")

    assert m._chrome_pid_for_port(9355, _runner=fake_runner) == 4242
    # Anchored ERE: port 9355 must not match 93550 (mirrors launch.sh KILL_MATCH).
    assert calls["cmd"][0] == "pgrep"
    assert calls["cmd"][1] == "-f"
    assert calls["cmd"][2] == r"--remote-debugging-port=9355($|[[:space:]])"


def test_chrome_pid_for_port_none_when_no_match():
    m = _load_cdp_module()
    assert m._chrome_pid_for_port(
        9355, _runner=lambda *a, **k: _FakePgrep(returncode=1)) is None


def test_chrome_pid_for_port_none_on_oserror():
    m = _load_cdp_module()

    def boom(*a, **k):
        raise OSError("pgrep missing")

    assert m._chrome_pid_for_port(9355, _runner=boom) is None


def test_native_screenshot_owner_match_is_not_prefix_substring():
    """Structural: the 'Google' substring collision (hole B) is gone."""
    text = Path(CDP_SCRIPT).read_text()
    assert "split()[0]" not in text
    assert "kCGWindowOwnerPID" in text
```

- [ ] **Step 2.2: Run to verify RED**

Run: `python3 -m pytest tests/test_cdp.py -k "chrome_pid or owner_match" -v`
Expected: 4 failures (`_chrome_pid_for_port` AttributeError ×3; structural finds `split()[0]`).

- [ ] **Step 2.3: Implement**

In `skills/look/scripts/cdp.py`, insert ABOVE `def native_screenshot(path):` (line 277):

```python
def _chrome_pid_for_port(port, _runner=subprocess.run):
    """Browser-process pid owning this lane's CDP port, or None (hole B, R1).

    Lane-precise: two Chrome instances (stock + CfT, or two CfT lanes) both match
    any name heuristic; only ONE owns --remote-debugging-port=<port>. Helper
    processes carry --type=… and no debugging port, so the anchored pgrep -f
    pattern matches exactly the browser process of THIS lane.
    """
    pat = r"--remote-debugging-port={}($|[[:space:]])".format(port)
    try:
        r = _runner(["pgrep", "-f", pat], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    pids = r.stdout.split()
    return int(pids[0]) if pids else None
```

Then inside `native_screenshot`, replace the owner-match construction:

```python
    _quartz_code = (
        "import Quartz as Q; ws=Q.CGWindowListCopyWindowInfo("
        "Q.kCGWindowListOptionOnScreenOnly|Q.kCGWindowListExcludeDesktopElements,"
        "Q.kCGNullWindowID);\n"
        "wid=next((w['kCGWindowNumber'] for w in ws "
        "if '{owner}' in str(w.get('kCGWindowOwnerName','')) "
        "and w.get('kCGWindowName','')),None)\n"
        "print(wid if wid is not None else '')"
    ).format(owner=CHROME_APP.split()[0])
```

with:

```python
    # Hole B: match THIS lane's browser pid (precise across stock/CfT/parallel
    # lanes); fall back to exact owner-name == CHROME_APP — never a substring
    # ('Google' used to match Google Drive and CfT alike).
    pid = _chrome_pid_for_port(CDP_PORT)
    if pid is not None:
        _match = "w.get('kCGWindowOwnerPID')=={}".format(pid)
    else:
        _match = "str(w.get('kCGWindowOwnerName',''))=={!r}".format(CHROME_APP)
    _quartz_code = (
        "import Quartz as Q; ws=Q.CGWindowListCopyWindowInfo("
        "Q.kCGWindowListOptionOnScreenOnly|Q.kCGWindowListExcludeDesktopElements,"
        "Q.kCGNullWindowID);\n"
        "wid=next((w['kCGWindowNumber'] for w in ws "
        "if " + _match + " "
        "and w.get('kCGWindowName','')),None)\n"
        "print(wid if wid is not None else '')"
    )
```

- [ ] **Step 2.4: Run to verify GREEN**

Run: `python3 -m pytest tests/test_cdp.py -v`
Expected: all pass.

- [ ] **Step 2.5: Commit**

```bash
git add skills/look/scripts/cdp.py tests/test_cdp.py
git commit -m "fix(sp1): native_screenshot matches lane pid / exact app name, not 'Google' substring (hole B)"
```

---

### Task 3: launch.sh — `CHROME_APP_NAME` + pid-targeted AppleScript + RU/EN menus + timeout guard (holes G, J)

**Files:**
- Modify: `skills/look/scripts/launch.sh` (config section ~line 69; enablement block lines 264-291)
- Test: `tests/test_launch.py`

Three changes in one region: (1) the 3 name literals disappear from launch.sh AppleScript entirely — GUI targeting goes **by the lane's pid** (`first application process whose unix id is $CHROME_PID`), which is spec §6 J's "path-based not name-based *where possible*" satisfied literally (no LaunchServices resolution → no "Where is …?" hang) AND lane-precise when two same-named browsers run headful (unifies with hole B's pid-first); `CHROME_APP_NAME` remains the lane's app-name *contract* (validated env + dry-run key) for cdp.py's AppleScript JS channel, where `tell application "<name>"` is unavoidable (Chrome's dictionary requires it; guarded by the shipped `timeout=10`) — that channel is the "where possible" boundary; (2) every osascript call goes through a python3 timeout wrapper; (3) the menu click tries the Russian tree then the English tree (hole G: CfT ships English menus).

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/test_launch.py`:

```python
# ── SP1: CHROME_APP_NAME threading + AppleScript hardening (holes G, J) ──


def test_app_name_default_in_dry_run():
    r = _run_launch()
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["app_name"] == "Google Chrome"


def test_app_name_env_override():
    r = _run_launch(env_override={"CHROME_APP_NAME": "Google Chrome for Testing"})
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["app_name"] == "Google Chrome for Testing"


def test_app_name_with_quote_fails_loud():
    r = _run_launch(env_override={"CHROME_APP_NAME": 'Evil " App'})
    assert r.returncode == 1
    assert "CHROME_APP_NAME" in r.stderr


def test_app_name_with_backslash_fails_loud():
    r = _run_launch(env_override={"CHROME_APP_NAME": "Evil \\ App"})
    assert r.returncode == 1
    assert "CHROME_APP_NAME" in r.stderr


def test_no_hardcoded_app_name_in_applescript():
    """Structural (hole G/J prep): the osascript blocks must reference
    $CHROME_APP_NAME, not the literal. The only allowed 'Google Chrome' literals
    are the CHROME_BIN default path components."""
    for line in LAUNCH_TEXT.splitlines():
        if "tell application \"Google Chrome\"" in line or "tell process \"Google Chrome\"" in line:
            raise AssertionError("hardcoded app name in AppleScript: {!r}".format(line))


def test_applescript_menu_click_is_locale_robust():
    """Structural (hole G): both the Russian and the English menu trees present."""
    assert "Разрешить JavaScript из событий Apple" in LAUNCH_TEXT
    assert "Allow JavaScript from Apple Events" in LAUNCH_TEXT


def test_osascript_goes_through_timeout_guard():
    """Structural (hole J): no bare `osascript` COMMAND in the launch path — every
    call goes through the _osascript_to timeout helper (whose python3 body holds
    the single allowed osascript invocation). R1-F1: match only command
    invocations — `^\\s*osascript\\s` — NOT comments, NOT the dry-run `osascript=`
    config key, NOT `osascript_steps=` (underscore fails the \\s)."""
    assert "_osascript_to" in LAUNCH_TEXT
    bare = [l for l in LAUNCH_TEXT.splitlines()
            if re.match(r"^\s*osascript\s", l)]
    assert bare == [], "bare osascript invocations: {!r}".format(bare)


def test_no_app_resolving_activate():
    """Structural (hole J): `tell application "<browser>"` resolves the app via
    LaunchServices and can hang on the "Where is" picker; launch.sh must drive the
    GUI exclusively through System Events. R1-F1: bash comments are prose — skip
    them; only code lines count."""
    for line in LAUNCH_TEXT.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "tell application" in line and "System Events" not in line:
            raise AssertionError("app-resolving tell: {!r}".format(line))


def test_enablement_targets_lane_by_pid():
    """Structural (holes J+B unified, spec §6 J "not name-based where possible"):
    the GUI-enablement AppleScript targets the lane's browser by unix id
    (CHROME_PID) — lane-precise and resolution-free — never by process name.
    Comments skipped (R1-F1)."""
    assert "unix id is " in LAUNCH_TEXT
    for line in LAUNCH_TEXT.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if 'tell process "' in line:
            raise AssertionError("name-based process targeting: {!r}".format(line))
```

NOTES:
- `LAUNCH_TEXT` is read once at module import (line 20) — these structural tests run against the post-edit file automatically.
- `import re` must be added to test_launch.py's imports (it is not imported today).
- **R1-F1 second half — update the EXISTING A.7 drift guard in the same edit.** `test_conftest_chrome_const_matches_launch_default` (test_launch.py, currently asserts the one-line `'CHROME_BIN="${CHROME_BIN:-' + CHROME + '}"'` form) breaks when Task 3.3 introduces the `CHROME_BIN_DEFAULTED` block. Rewrite it to pin the SAME invariant against the new form:

```python
def test_conftest_chrome_const_matches_launch_default():
    """A.7: the CHROME_BIN default in launch.sh must equal conftest.CHROME
    (single change-point — change both or neither). SP1 form: the default is the
    assignment inside the CHROME_BIN_DEFAULTED block."""
    from conftest import CHROME
    assert ('CHROME_BIN="' + CHROME + '"') in LAUNCH_TEXT
```

(Pre-impl the old line is `CHROME_BIN="${CHROME_BIN:-…}"` — the rewritten assertion fails (RED ✓); post-impl the block contains the exact `CHROME_BIN="/Applications/…"` assignment (GREEN ✓). `CFT_BIN="…"` cannot false-match — different variable name.)

- [ ] **Step 3.2: Run to verify RED**

Run: `python3 -m pytest tests/test_launch.py -k "app_name or applescript or osascript or activate or pid or chrome_const" -v`
Expected: ~9 failures (`app_name` key missing from dry-run; CHROME_APP_NAME validation absent; literals present; no `_osascript_to`; no `unix id is`; EN menu literal absent; rewritten A.7 guard fails against the old one-line form).

- [ ] **Step 3.3: Implement — config + validation**

In `skills/look/scripts/launch.sh`, AFTER the `CHROME_BIN=` line (line 69) — and replace that line itself to capture defaulted-ness (needed by Task 5's CfT default):

Replace:

```bash
# Chrome binary: single change-point shared with tests/conftest.py (CHROME const).
CHROME_BIN="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
```

with:

```bash
# Chrome binary: single change-point shared with tests/conftest.py (CHROME const).
# DEFAULTED flag lets the automation lane (SP1) swap in the CfT default without
# overriding an explicit env CHROME_BIN.
CHROME_BIN_DEFAULTED=0
if [[ -z "${CHROME_BIN:-}" ]]; then
  CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  CHROME_BIN_DEFAULTED=1
fi

# AppleScript app/process name (SP1, #164): stock Chrome by default; the automation
# lane defaults it to "Google Chrome for Testing". Threads into the headful
# JS-enablement osascript blocks below. A double quote or backslash would break the
# AppleScript string / bash quoting — fail loud (same principle as the PROFILE_DIR
# backslash guard above).
CHROME_APP_NAME_DEFAULTED=0
if [[ -z "${CHROME_APP_NAME:-}" ]]; then
  CHROME_APP_NAME="Google Chrome"
  CHROME_APP_NAME_DEFAULTED=1
fi
if [[ "$CHROME_APP_NAME" == *'"'* || "$CHROME_APP_NAME" == *\\* || "$CHROME_APP_NAME" == *$'\n'* ]]; then
  echo "ERROR: CHROME_APP_NAME must not contain a double quote, backslash or newline (got: $CHROME_APP_NAME)" >&2
  exit 1
fi
```

- [ ] **Step 3.4: Implement — timeout helper**

Insert AFTER the `_escape_ere()` function (line 13):

```bash
# Timeout-guarded osascript (hole J): a `tell application "<name>"` with an unknown
# name raises the LaunchServices "Where is <name>?" picker and hangs osascript
# forever. launch.sh AppleScript is process-based (System Events — no app
# resolution) AND every call is killed after a hard cap. python3 is already a
# launch.sh dependency (profile canonicalization, Local State patch).
_osascript_to() {  # _osascript_to <seconds> <applescript>
  python3 - "$1" "$2" <<'PY'
import subprocess, sys
try:
    subprocess.run(["osascript", "-e", sys.argv[2]],
                   capture_output=True, timeout=float(sys.argv[1]))
except (subprocess.TimeoutExpired, OSError):
    pass
PY
}
```

- [ ] **Step 3.5: Implement — dry-run key**

In the LOOK_DRY_RUN block, after the `echo "chrome_bin=$CHROME_BIN"` line add:

```bash
  echo "app_name=$CHROME_APP_NAME"
```

- [ ] **Step 3.6: Implement — rewrite the enablement block**

Replace the whole headful AppleScript section (lines 264-291, from `# AppleScript JS enablement (headful only — no GUI/menus when headless)` through the second `end tell' 2>/dev/null`) with:

```bash
# AppleScript JS enablement (headful only — no GUI/menus when headless).
# Targets OUR lane's browser by unix id (CHROME_PID) via System Events — no
# LaunchServices name resolution (hole J: no "Where is" hang), lane-precise even
# when two same-named browsers run headful (unifies with hole B's pid-first).
# RU/EN menu trees (hole G: CfT ships English menus even on a Russian system)
# + hard timeout on every call. The name-based `tell application` remains ONLY in
# cdp.py's AppleScript JS channel where Chrome's dictionary requires it (guarded
# by its shipped timeout=10) — spec §6 J's "where possible" boundary.
if [[ "$HEADLESS" != "1" ]]; then
  _osascript_to 15 '
tell application "System Events"
    try
        set frontmost of (first application process whose unix id is '"$CHROME_PID"') to true
    end try
end tell'
  sleep 0.5
  # Auto-enable AppleScript JS via menu click (one-time, persists in profile)
  _osascript_to 15 '
tell application "System Events"
    tell (first application process whose unix id is '"$CHROME_PID"')
        try
            click menu item "Разрешить JavaScript из событий Apple" of menu 1 of menu item "Разработчикам" of menu "Вид" of menu bar 1
        end try
        try
            click menu item "Allow JavaScript from Apple Events" of menu 1 of menu item "Developer" of menu "View" of menu bar 1
        end try
    end tell
end tell'

  # Handle confirmation dialog if it appears
  sleep 1
  _osascript_to 15 '
tell application "System Events"
    tell (first application process whose unix id is '"$CHROME_PID"')
        try
            click button "Разрешить" of sheet 1 of window 1
        end try
        try
            click button "Allow" of sheet 1 of window 1
        end try
    end tell
end tell'
fi
```

(Quoting: `'…'"$CHROME_PID"'…'` — single-quoted AppleScript with the bash variable spliced in double quotes; `CHROME_PID=$!` is set by launch.sh itself right above this block, always numeric. The dead-pid case errors inside the `try` blocks — best-effort, no hang. `CHROME_APP_NAME` is no longer spliced into launch.sh AppleScript at all — it stays as the validated env + dry-run contract consumed by cdp.py.)

- [ ] **Step 3.7: Run to verify GREEN + no regression**

Run: `python3 -m pytest tests/test_launch.py -v`
Expected: all pass, including `test_default_invocation_argv_is_byte_identical` (argv untouched — only post-launch AppleScript and config changed).

- [ ] **Step 3.8: Manual smoke — daily lane unaffected**

Run: `LOOK_DRY_RUN=1 bash skills/look/scripts/launch.sh | head -20`
Expected: `port=9333`, `profile=/0/.jaine/.browser/profile`, `app_name=Google Chrome`, argv identical to before.

- [ ] **Step 3.9: Commit**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(sp1): CHROME_APP_NAME contract, pid-targeted RU/EN AppleScript, osascript timeout (holes G, J)"
```

---

### Task 4: `update-cft.sh` — install/pin Chrome for Testing

**Files:**
- Create: `skills/look/scripts/update-cft.sh` (executable)
- Test: `tests/test_launch.py` (structural + dry-run unit)

Layout: `/0/.jaine/.browser/cft/<version>/chrome-mac-arm64/Google Chrome for Testing.app`; `cft/current` symlink = the pin. The script is the ONLY mover of `current` (pin discipline: launching never auto-updates).

- [ ] **Step 4.1: Write the failing tests**

Append to `tests/test_launch.py`:

```python
# ── SP1: update-cft.sh (install/pin Chrome for Testing) ──

UPDATE_CFT = str(PLUGIN_ROOT / "skills" / "look" / "scripts" / "update-cft.sh")


def test_update_cft_exists_and_executable():
    assert os.path.exists(UPDATE_CFT)
    assert os.access(UPDATE_CFT, os.X_OK)


def test_update_cft_dry_run_resolves_stable():
    """CFT_DRY_RUN=1 resolves the Stable version + mac-arm64 url and exits 0
    WITHOUT downloading. Needs network (googlechromelabs JSON) — skip offline."""
    r = subprocess.run(["bash", UPDATE_CFT], capture_output=True, text=True,
                       timeout=30, env={**os.environ, "CFT_DRY_RUN": "1"})
    if r.returncode != 0 and "could not resolve" in r.stderr:
        pytest.skip("offline — CfT version endpoint unreachable")
    assert r.returncode == 0, r.stderr
    assert "CfT Stable:" in r.stdout
    assert "mac-arm64" in r.stdout
    assert "CFT_DRY_RUN" in r.stdout


def test_update_cft_is_strict_bash():
    text = Path(UPDATE_CFT).read_text()
    assert "set -euo pipefail" in text
    assert "ln -sfn" in text          # atomic-enough pin move
    assert "last-known-good-versions-with-downloads.json" in text
```

- [ ] **Step 4.2: Run to verify RED**

Run: `python3 -m pytest tests/test_launch.py -k update_cft -v`
Expected: 3 failures (file missing).

- [ ] **Step 4.3: Implement**

Create `skills/look/scripts/update-cft.sh`:

```bash
#!/usr/bin/env bash
# update-cft.sh — install/refresh the pinned Chrome for Testing for drive lanes (SP1, #164).
#
# Layout:  $CFT_ROOT/<version>/chrome-mac-arm64/Google Chrome for Testing.app
#          $CFT_ROOT/current -> <version>            (the pin; ln -sfn)
# This script is the ONLY mover of `current` — launching a lane never auto-updates
# (that auto-update drift is exactly what #164 removes).
#
# CFT_DRY_RUN=1 → resolve + print version/url, no download, no pin move.
set -euo pipefail

CFT_ROOT="${CFT_ROOT:-/0/.jaine/.browser/cft}"
JSON_URL="https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
PLATFORM="mac-arm64"

resolved=$(curl -fsSL --max-time 20 "$JSON_URL" | python3 -c '
import json, sys
d = json.load(sys.stdin)
ch = d["channels"]["Stable"]
url = next(x["url"] for x in ch["downloads"]["chrome"] if x["platform"] == "mac-arm64")
print(ch["version"], url)
') || { echo "ERROR: could not resolve CfT Stable from $JSON_URL" >&2; exit 1; }
VERSION="${resolved%% *}"
URL="${resolved#* }"
[[ -n "$VERSION" && -n "$URL" ]] || { echo "ERROR: could not resolve CfT Stable" >&2; exit 1; }

echo "CfT Stable: $VERSION"
echo "url: $URL  (platform: $PLATFORM)"

if [[ "${CFT_DRY_RUN:-}" == "1" ]]; then
  echo "CFT_DRY_RUN — not downloading, pin untouched"
  exit 0
fi

BIN="$CFT_ROOT/$VERSION/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
if [[ -x "$BIN" ]]; then
  echo "already installed: $CFT_ROOT/$VERSION"
else
  mkdir -p "$CFT_ROOT/$VERSION"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -fSL --progress-bar -o "$tmp/cft.zip" "$URL"
  unzip -q "$tmp/cft.zip" -d "$CFT_ROOT/$VERSION"
  [[ -x "$BIN" ]] || { echo "ERROR: unzip did not produce expected layout: $BIN" >&2; exit 1; }
fi

ln -sfn "$CFT_ROOT/$VERSION" "$CFT_ROOT/current"
echo "pinned: current -> $VERSION"
"$CFT_ROOT/current/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" --version
```

Then: `chmod +x skills/look/scripts/update-cft.sh`

- [ ] **Step 4.4: Run to verify GREEN**

Run: `python3 -m pytest tests/test_launch.py -k update_cft -v`
Expected: 3 pass (dry-run hits the network; skips if offline).

- [ ] **Step 4.5: Live install (needed by Task 7 e2e + Task 8 empirical)**

Run: `bash skills/look/scripts/update-cft.sh`
Expected output (versions will differ):

```
CfT Stable: 1XX.0.XXXX.XX
url: https://storage.googleapis.com/chrome-for-testing-public/…/mac-arm64/chrome-mac-arm64.zip  (platform: mac-arm64)
pinned: current -> 1XX.0.XXXX.XX
Google Chrome for Testing 1XX.0.XXXX.XX
```

Verify: `ls -l /0/.jaine/.browser/cft/` shows `<version>/` + `current -> <version>`. The final `--version` line proves the binary launches (curl downloads carry no quarantine xattr; if Gatekeeper still blocks, record it and resolve before continuing — this is an SP1 empirical fact for Task 8's doc).

- [ ] **Step 4.6: Commit**

```bash
git add skills/look/scripts/update-cft.sh tests/test_launch.py
git commit -m "feat(sp1): update-cft.sh — install/pin Chrome for Testing to /0/.jaine/.browser/cft (#164)"
```

---

### Task 5: launch.sh — `--automation` lane (gate, temp profile, CfT defaults, argv)

**Files:**
- Modify: `skills/look/scripts/launch.sh`
- Test: `tests/test_launch.py`

Semantics (spec §4.2 + R1-C + R1-E):
- `--automation` flag or `LOOK_AUTOMATION` truthy env (same resolution pattern as insecure).
- **Gate, fail-closed:** port ≠ 9333 AND profile is not / does not resolve to the daily profile. Port alone is NOT sufficient (#160).
- **Temp profile default (hole E):** no explicit `LOOK_PROFILE_DIR` → `${TMPDIR:-/tmp}/jaine-drive-<port>` (deterministic per port so the lane's pkill-by-profile restart contract still holds; macOS cleans TMPDIR periodically/reboot — not persistent under `/0/.jaine`). Sets `PROFILE_OVERRIDDEN=1` + recomputes `LOG` inside the profile.
- **CfT defaults:** `CHROME_BIN` not set by env → CfT pin; `CHROME_APP_NAME` not set → `"Google Chrome for Testing"`. Explicit env always wins.
- **Argv:** `--enable-automation` (infobar suppression — verified: CfT alone does NOT hide it) + `--use-mock-keychain` (hole K).
- **Ordering:** the automation block runs AFTER headless resolution and BEFORE the insecure gate — so `automation+insecure` composes (auto-temp profile satisfies insecure's explicit-isolated-profile requirement).
- **Binary preflight:** real-launch path fails loud when `CHROME_BIN` does not exist (clear `update-cft.sh` hint for the automation lane). Dry-run does NOT check existence (hermetic tests).

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/test_launch.py`:

```python
# ── SP1: --automation lane (gate R1-C, temp profile R1-E, CfT defaults, argv) ──

CFT_BIN_EXPECTED = ("/0/.jaine/.browser/cft/current/chrome-mac-arm64/"
                    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")


def test_automation_forbidden_on_9333():
    r = _run_launch(["--automation"])
    assert r.returncode == 1
    assert "9333" in r.stderr


def test_automation_env_forbidden_on_9333():
    r = _run_launch(env_override={"LOOK_AUTOMATION": "1"})
    assert r.returncode == 1


def test_automation_rejects_daily_profile_explicit():
    r = _run_launch(["--automation"], env_override={
        "CDP_PORT": "9444", "LOOK_PROFILE_DIR": "/0/.jaine/.browser/profile"})
    assert r.returncode == 1
    assert "daily" in r.stderr


def test_automation_rejects_daily_profile_via_realpath_alias():
    r = _run_launch(["--automation"], env_override={
        "CDP_PORT": "9444",
        "LOOK_PROFILE_DIR": "/0/.jaine/.browser/profile/../profile"})
    assert r.returncode == 1


def test_automation_default_gets_temp_profile_and_flags():
    r = _run_launch(["--automation"], env_override={"CDP_PORT": "9444"})
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["automation"] == "1"
    assert cfg["profile"].endswith("/jaine-drive-9444") or cfg["profile"].endswith("jaine-drive-9444")
    assert cfg["profile"] != "/0/.jaine/.browser/profile-9444"  # hole E: not persistent
    assert cfg["log"] == cfg["profile"] + "/chrome.log"
    assert "--enable-automation" in argv
    assert "--use-mock-keychain" in argv
    assert cfg["chrome_bin"] == CFT_BIN_EXPECTED       # CHROME_BIN stripped by _run_launch
    assert cfg["app_name"] == "Google Chrome for Testing"


def test_automation_env_equivalent_to_flag():
    r = _run_launch(env_override={"CDP_PORT": "9444", "LOOK_AUTOMATION": "1"})
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["automation"] == "1"
    assert "--enable-automation" in argv


def test_automation_explicit_overrides_win(tmp_path):
    r = _run_launch(["--automation"], env_override={
        "CDP_PORT": "9444",
        "LOOK_PROFILE_DIR": str(tmp_path / "lane"),
        "CHROME_BIN": "/custom/bin/chrome",
        "CHROME_APP_NAME": "Custom Chrome"})
    assert r.returncode == 0, r.stderr
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["profile"] == str(tmp_path / "lane")
    assert cfg["chrome_bin"] == "/custom/bin/chrome"
    assert cfg["app_name"] == "Custom Chrome"


def test_automation_composes_with_insecure():
    """drive lane + LAN testing (roadmap #8): auto-temp profile satisfies the
    insecure gate's explicit-isolated-profile requirement."""
    r = _run_launch(["--automation", "--insecure"], env_override={"CDP_PORT": "9444"})
    assert r.returncode == 0, r.stderr
    _, argv = _parse_dryrun(r.stdout)
    assert "--enable-automation" in argv
    assert "--disable-web-security" in argv


def test_automation_composes_with_headless():
    r = _run_launch(["--automation", "--headless"], env_override={"CDP_PORT": "9444"})
    assert r.returncode == 0, r.stderr
    _, argv = _parse_dryrun(r.stdout)
    assert "--headless=new" in argv
    assert "--enable-automation" in argv


def test_default_lane_has_no_automation():
    r = _run_launch()
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["automation"] == "0"
    assert "--enable-automation" not in argv
    assert "--use-mock-keychain" not in argv
```

- [ ] **Step 5.2: Run to verify RED**

Run: `python3 -m pytest tests/test_launch.py -k automation -v`
Expected: ~10 failures (`--automation` unknown flag → rc=1 *everywhere*, key `automation` missing).

- [ ] **Step 5.3: Implement — shared daily-profile resolver**

Insert AFTER `_osascript_to()` (Task 3.4):

```bash
# Canonical "does this profile resolve to the daily profile?" check — shared by the
# insecure gate (R1-F1) and the automation gate (R1-C). Echoes 1/0; fail-CLOSED:
# canonicalization error → 1 (treated AS the daily profile).
_resolves_to_daily_profile() {
  python3 - "$1" <<'PY' 2>/dev/null || echo 1
import os, sys
print(1 if os.path.realpath(sys.argv[1]) == os.path.realpath("/0/.jaine/.browser/profile") else 0)
PY
}
```

And refactor the insecure block to use it — replace the `_profile_is_daily=$(python3 - "$PROFILE_DIR" <<'PY' …)` heredoc (lines 161-165) with:

```bash
  _profile_is_daily=$(_resolves_to_daily_profile "$PROFILE_DIR")
```

(keep the existing string-compare `[[ "$_profile_is_daily" != "0" ]]` semantics untouched).

- [ ] **Step 5.4: Implement — CFT_BIN const**

In the config section, after the `CHROME_APP_NAME` block (Task 3.3):

```bash
# Pinned Chrome for Testing (the automation-lane default binary; update-cft.sh is
# the only mover of `current`).
CFT_BIN="/0/.jaine/.browser/cft/current/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
```

- [ ] **Step 5.5: Implement — arg parsing**

In the `for a in "$@"` loop: add `AUTOMATION_ARG=0` to the init block above the loop (next to `INSECURE_ARG=0`), add the case arm:

```bash
    --automation) AUTOMATION_ARG=1 ;;
```

and update the unknown-flag message:

```bash
      echo "ERROR: unknown flag '$a' (look launcher accepts --headless/--headful/--insecure/--automation)" >&2
```

- [ ] **Step 5.6: Implement — the automation block**

Insert AFTER the headless resolution (after line 134 `fi`) and BEFORE the `# ── Web-security relax` comment:

```bash
# ── Automation lane (SP1, #164): opt-in --automation / LOOK_AUTOMATION. CfT default
#    binary/app-name + --enable-automation + keychain isolation. Gate is fail-closed:
#    non-9333 port AND non-daily profile (R1-C: port alone is NOT sufficient, #160).
#    --enable-automation must NEVER reach the daily browser: it sets
#    navigator.webdriver, suppresses password-save UI, disables auto-reload. ──
shopt -s nocasematch
if [[ "${LOOK_AUTOMATION:-}" =~ ^(1|true|yes)$ ]]; then
  _env_automation=1
else
  _env_automation=0
fi
shopt -u nocasematch
if (( AUTOMATION_ARG )) || (( _env_automation )); then
  AUTOMATION_REQUESTED=1
else
  AUTOMATION_REQUESTED=0
fi

AUTOMATION=0
if (( AUTOMATION_REQUESTED )); then
  if (( CDP_PORT == 9333 )); then
    echo "ERROR: --automation / LOOK_AUTOMATION is forbidden on the daily 9333 lane" >&2
    echo "       (automation flags must never reach the user's daily browser). Use a" >&2
    echo "       non-9333 CDP_PORT." >&2
    exit 1
  fi
  if (( ! PROFILE_OVERRIDDEN )); then
    # Hole E (R1-E): drive lanes get a TEMP profile — deterministic per port so the
    # lane's pkill-by-profile restart contract still holds — not a persistent
    # profile-<port> accumulating under /0/.jaine. macOS cleans TMPDIR on reboot.
    PROFILE_DIR="${TMPDIR:-/tmp}/jaine-drive-${CDP_PORT}"
    PROFILE_OVERRIDDEN=1
    LOG="$PROFILE_DIR/chrome.log"   # mirror the PROFILE_OVERRIDDEN LOG rule above
  elif [[ "$(_resolves_to_daily_profile "$PROFILE_DIR")" != "0" ]]; then
    echo "ERROR: --automation profile resolves to the daily profile ($PROFILE_DIR). Refusing." >&2
    exit 1
  fi
  AUTOMATION=1
  if (( CHROME_BIN_DEFAULTED )); then
    CHROME_BIN="$CFT_BIN"
  fi
  if (( CHROME_APP_NAME_DEFAULTED )); then
    CHROME_APP_NAME="Google Chrome for Testing"
  fi
fi
```

- [ ] **Step 5.7: Implement — argv + dry-run + preflight**

After the `if (( INSECURE )); then CHROME_ARGV+=(--disable-web-security); fi` block:

```bash
if (( AUTOMATION )); then
  # --enable-automation: suppresses the bad-flags infobar (CfT alone does NOT —
  # research-verified); --use-mock-keychain: no macOS keychain prompts/leak (hole K).
  CHROME_ARGV+=(--enable-automation --use-mock-keychain)
fi
```

In the dry-run block, after `echo "insecure=$INSECURE"`:

```bash
  echo "automation=$AUTOMATION"
```

In the real-launch path, right after the dry-run block's `fi` (before `mkdir -p`):

```bash
# Binary preflight: fail loud with an actionable hint instead of a silent
# chrome.log-only death (the automation default points at the CfT pin, which may
# simply not be installed yet).
if [[ ! -x "$CHROME_BIN" ]]; then
  echo "ERROR: Chrome binary not found or not executable: $CHROME_BIN" >&2
  if (( AUTOMATION )); then
    echo "       Install the pinned Chrome for Testing: bash skills/look/scripts/update-cft.sh" >&2
  fi
  exit 1
fi
```

- [ ] **Step 5.8: Run to verify GREEN + full regression**

Run: `python3 -m pytest tests/test_launch.py -v`
Expected: ALL pass — including `test_default_invocation_argv_is_byte_identical` (default lane byte-identical) and every pre-SP1 insecure/headless test.

- [ ] **Step 5.9: Commit**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(sp1): --automation lane — fail-closed gate, temp drive-profile, CfT defaults, --enable-automation + --use-mock-keychain (#164, R1-C/E/K)"
```

---

### Task 6: conftest — CfT fixture + lane-var hygiene

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_launch.py` (drift guard)

- [ ] **Step 6.1: Write the failing test (drift guard)**

Append to `tests/test_launch.py`:

```python
def test_lane_env_vars_cover_sp1_vars():
    """Hermeticity: a shell LOOK_AUTOMATION=1 or CHROME_APP_NAME bleeding into
    fixtures/dry-run tests would silently flip lanes — the strip-list must cover
    the SP1 vars."""
    assert "LOOK_AUTOMATION" in _LANE_VARS
    assert "CHROME_APP_NAME" in _LANE_VARS
```

Run: `python3 -m pytest tests/test_launch.py -k lane_env_vars -v` → FAILS.

- [ ] **Step 6.2: Implement conftest changes**

In `tests/conftest.py`:

(a) Extend the strip-list (line 53):

```python
LANE_ENV_VARS = ("CDP_PORT", "LOOK_PROFILE_DIR", "LOOK_HEADLESS", "LOOK_INSECURE",
                 "LOOK_DRY_RUN", "CHROME_BIN", "LOOK_AUTOMATION", "CHROME_APP_NAME")
```

(b) Add CfT constants after the `CHROME` const (line 48):

```python
# SP1 (#164): pinned Chrome for Testing — the automation-lane binary. The /drive
# e2e fixture gets its OWN CfT lane (R1-A), never piggybacking the stock baseline.
CFT_BIN = ("/0/.jaine/.browser/cft/current/chrome-mac-arm64/"
           "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
CFT_APP_NAME = "Google Chrome for Testing"
# ── e2e port registry (R1-F2: keep lanes distinct; grep this block before adding one) ──
# 9355  TEST_CDP_PORT       — stock-Chrome baseline lane (this file)
# 9356  INSECURE_TEST_PORT  — insecure lane (tests/test_e2e.py; falls back to 9358)
# 9359  DRIVE_TEST_PORT     — CfT automation lane (tests/test_e2e_cft.py)
# 9360+                     — SP1 empirical probes (plan Task 8: 9360 CfT headful, 9361 stock)
DRIVE_TEST_PORT = 9359
```

(9356 is TAKEN: `tests/test_e2e.py` line 516 — `INSECURE_TEST_PORT = 9356 if CDP_PORT != 9356 else 9358`; a session-scoped CfT browser on 9356 would make the module-scoped insecure fixture fail loud, order-dependently.)

(c) Parameterize the online probe — replace `_cdp_is_online()`:

```python
def _cdp_is_online(port=CDP_PORT):
    try:
        r = urlopen("http://localhost:{}/json/version".format(port), timeout=3)
        return r.status == 200
    except (URLError, OSError):
        return False
```

(existing callers pass nothing — behavior identical).

(d) Add the fixture after `jaine_browser`:

```python
@pytest.fixture(scope="session")
def cft_browser():
    """Isolated Chrome-for-Testing automation lane on DRIVE_TEST_PORT (R1-A).

    Skips (not fails) when CfT is not installed, so the suite stays green on a
    machine that never ran update-cft.sh. A pre-existing listener on the CfT test
    port is a fail-loud setup error (isolation), mirroring jaine_browser.

    LANE CONTRACT (R1-F3): every process driving a CfT lane carries BOTH env keys
    — CDP_PORT=<port> AND CHROME_APP_NAME="Google Chrome for Testing". launch.sh
    defaults the app name itself under LOOK_AUTOMATION, but that default does NOT
    propagate to later, separate cdp.py processes; cdp.py's AppleScript/native
    paths would silently target stock "Google Chrome". The fixture models the
    full contract explicitly.
    """
    if not os.path.exists(CFT_BIN):
        pytest.skip("Chrome for Testing not installed — run skills/look/scripts/update-cft.sh")
    if _cdp_is_online(DRIVE_TEST_PORT):
        pytest.fail(
            "Unexpected CDP listener already on CfT test port {0} — refusing to reuse "
            "a browser the fixture does not own (isolation). Kill it "
            "(pkill -f remote-debugging-port={0}) and re-run.".format(DRIVE_TEST_PORT)
        )
    env = os.environ.copy()
    for _v in LANE_ENV_VARS:
        env.pop(_v, None)
    temp_profile = tempfile.mkdtemp(prefix="jaine-cft-{}-".format(DRIVE_TEST_PORT))
    env["CDP_PORT"] = str(DRIVE_TEST_PORT)
    env["LOOK_PROFILE_DIR"] = temp_profile
    env["LOOK_HEADLESS"] = "1"
    env["LOOK_AUTOMATION"] = "1"
    env["CHROME_APP_NAME"] = CFT_APP_NAME  # lane contract (R1-F3) — explicit > implicit
    kill_match = _kill_pattern(temp_profile)
    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_is_online(DRIVE_TEST_PORT):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
        shutil.rmtree(temp_profile, ignore_errors=True)
        pytest.fail("CfT browser did not start on port {} within 20s".format(DRIVE_TEST_PORT))

    yield DRIVE_TEST_PORT

    subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
    shutil.rmtree(temp_profile, ignore_errors=True)
```

(The fixture passes an *explicit* temp profile — exercising the explicit-profile gate path; the auto-temp default path is covered by Task 5 dry-run units.)

- [ ] **Step 6.3: Run to verify**

Run: `python3 -m pytest tests/test_launch.py tests/test_cdp.py -v`
Expected: all pass (fixture itself is exercised in Task 7).

- [ ] **Step 6.4: Commit**

```bash
git add tests/conftest.py tests/test_launch.py
git commit -m "test(sp1): CfT e2e fixture (cft_browser, DRIVE_TEST_PORT) + SP1 lane-var hygiene (R1-A)"
```

---

### Task 7: CfT e2e — `tests/test_e2e_cft.py`

**Files:**
- Create: `tests/test_e2e_cft.py`

Requires Task 4.5 (live CfT install). Self-contained per repo convention: the fixture auto-launches; the whole file skips when CfT is absent.

- [ ] **Step 7.1: Write the e2e tests**

Create `tests/test_e2e_cft.py`:

```python
#!/usr/bin/env python3
"""E2E tests for the Chrome-for-Testing automation lane (SP1, #164).

Self-contained: cft_browser launches an isolated headless CfT lane on
DRIVE_TEST_PORT (temp profile, --enable-automation, --use-mock-keychain) and
tears it down. The whole file skips when CfT is not installed
(run skills/look/scripts/update-cft.sh).
"""
import inspect
import json
import os
import subprocess
import sys
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402
from conftest import CDP_SCRIPT, CFT_APP_NAME, DRIVE_TEST_PORT  # noqa: E402


def _run_cdp_on(port, args, timeout=15):
    """CfT-lane contract (R1-F3): every cdp.py call against a CfT lane carries
    BOTH CDP_PORT and CHROME_APP_NAME — cdp.py's AppleScript/native paths would
    otherwise default to stock "Google Chrome" and target the wrong app."""
    env = os.environ.copy()
    env["CDP_PORT"] = str(port)
    env["CHROME_APP_NAME"] = CFT_APP_NAME
    return subprocess.run([sys.executable, CDP_SCRIPT] + args,
                          capture_output=True, text=True, timeout=timeout, env=env)


def test_cft_lane_serves_pinned_version(cft_browser):
    """The lane runs the PINNED CfT build — /json/version matches cft/current."""
    r = urlopen("http://localhost:{}/json/version".format(cft_browser), timeout=5)
    data = json.loads(r.read().decode())
    pinned = os.path.basename(os.path.realpath("/0/.jaine/.browser/cft/current"))
    # headless=new reports "HeadlessChrome/<ver>", headful "Chrome/<ver>"
    assert data["Browser"].endswith("/" + pinned), data["Browser"]


def test_cdp_py_drives_cft_lane(cft_browser):
    """cdp.py works unmodified against the CfT lane (CDP parity)."""
    r = _run_cdp_on(cft_browser, ["js", "2+2"])
    assert r.returncode == 0, r.stderr
    assert "4" in r.stdout


def test_cft_process_carries_automation_flags(cft_browser):
    """The browser process (not a helper) carries the SP1 automation flags."""
    ps = subprocess.run(["ps", "-axo", "command"], capture_output=True,
                        text=True, timeout=10)
    lane = [l for l in ps.stdout.splitlines()
            if "remote-debugging-port={}".format(cft_browser) in l
            and "--type=" not in l]
    assert lane, "CfT browser process not found in ps output"
    assert "--enable-automation" in lane[0]
    assert "--use-mock-keychain" in lane[0]
    assert "Google Chrome for Testing" in lane[0]


def test_cft_lane_contract_threads_app_name(cft_browser):
    """R1-F3 drift guard: the lane-contract helper must set BOTH env keys — a
    future 'simplification' dropping CHROME_APP_NAME would silently re-point
    cdp.py's AppleScript/native paths at stock Chrome. (A behavioral app-name
    e2e needs a headful window — that is plan Task 8 Probe A; this pins the
    contract carrier structurally.)"""
    r = _run_cdp_on(cft_browser, ["js", "1"])
    assert r.returncode == 0, r.stderr
    src = inspect.getsource(_run_cdp_on)
    assert "CHROME_APP_NAME" in src
    assert "CDP_PORT" in src
```

- [ ] **Step 7.2: Run**

Run: `python3 -m pytest tests/test_e2e_cft.py -v`
Expected: 4 pass (≈10s incl. lane launch). If CfT not installed → 4 skips (re-run Task 4.5).

- [ ] **Step 7.3: Confirm lane teardown**

Run (probe the ACTUAL CfT lane port — `DRIVE_TEST_PORT = 9359`, R2-F1):
`pgrep -fl "remote-debugging-port=9359" || echo "lane clean"`
Expected: `lane clean`.

- [ ] **Step 7.4: Commit**

```bash
git add tests/test_e2e_cft.py
git commit -m "test(sp1): CfT-lane e2e — pinned version, cdp.py parity, automation argv (R1-A)"
```

---

### Task 8: Empirical block (R1-I infobar, R1-K keychain, process name, hole-J probe) → analysis doc

**Files:**
- Create: `docs/superpowers/analysis/2026-06-05-sp1-cft-empirical-findings.md`

These are the spec-mandated *measurements* (§5 SP1 row: "Empirical: …"). Run each probe, record raw output in the doc. No production-code changes expected; if a probe falsifies an SP1 assumption (e.g. CfT process name ≠ "Google Chrome for Testing"), STOP and fix the constant before the doc.

- [ ] **Step 8.1: Probe A — CfT process name in System Events (grounds hole B fallback + Task 3 pid-targeting)**

Ports per the conftest registry (R1-F2): probes use **9360** (CfT headful) and **9361** (stock comparison) — never the 9355/9356/9359 fixture lanes.

```bash
LOOK_PROFILE_DIR=$(mktemp -d) CDP_PORT=9360 LOOK_AUTOMATION=1 \
  bash skills/look/scripts/launch.sh --headful about:blank
sleep 2
osascript -e 'tell application "System Events" to get name of every process whose name contains "Chrome"'
ps -axo pid,command | grep -E "remote-debugging-port=9360" | grep -v grep
```

Record: the exact System-Events process name for CfT (expected `Google Chrome for Testing`) and the browser pid. Leave the lane running for Probe B.

- [ ] **Step 8.2: Probe B — R1-I: does `--enable-automation` show an infobar / shift the viewport in headful?**

With the 9360 CfT headful lane still up, and the stock headful comparison lane (note: CfT-lane cdp.py calls carry `CHROME_APP_NAME` per the R1-F3 contract):

```bash
# CfT + automation lane viewport (numeric primary signal):
CDP_PORT=9360 CHROME_APP_NAME="Google Chrome for Testing" \
  python3 skills/look/scripts/cdp.py js "({w: window.innerWidth, h: window.innerHeight})"
# Visual check — NATIVE window capture (cdp.py screenshot is the CDP channel =
# viewport-only, an infobar in browser chrome would NOT appear there):
screencapture -x -t png /tmp/sp1-desktop-cft.png   # whole desktop; crop visually

# Stock headful lane WITHOUT automation on 9361 for the same window-size:
LOOK_PROFILE_DIR=$(mktemp -d) CDP_PORT=9361 bash skills/look/scripts/launch.sh --headful about:blank
sleep 2
CDP_PORT=9361 python3 skills/look/scripts/cdp.py js "({w: window.innerWidth, h: window.innerHeight})"
```

Record: innerHeight delta between the automation and non-automation headful lanes at identical `--window-size` (a shifted viewport ⇒ an infobar is occupying chrome), plus visual confirmation from the native desktop capture. This is the SP2 input for "drop `--enable-automation` in co-pilot-headful?" — record, do NOT decide here.

- [ ] **Step 8.3: Probe C — R1-K: keychain silence**

While the 9360 lane is up: navigate to an https page (`CDP_PORT=9360 CHROME_APP_NAME="Google Chrome for Testing" python3 skills/look/scripts/cdp.py navigate https://example.com`), observe: no macOS keychain prompt appears (`--use-mock-keychain`). Record qualitative result + the flag presence from Probe A's ps line.

- [ ] **Step 8.4: Probe D — hole J: "Where is" eliminated**

```bash
# Pid-targeted AppleScript against a dead/bogus unix id must return fast (error
# inside try), not hang, and never raise the LaunchServices "Where is" picker:
time osascript -e 'tell application "System Events"
    try
        set frontmost of (first application process whose unix id is 999999) to true
    end try
end tell'
```

Record: returns in <1s with no GUI picker (constructive elimination — launch.sh no longer resolves apps by name at all; cdp.py's `tell application` channel keeps its shipped `timeout=10`).

- [ ] **Step 8.5: Teardown probes**

```bash
pkill -f "remote-debugging-port=9360"; pkill -f "remote-debugging-port=9361"
pgrep -fl "remote-debugging-port=936[01]" || echo clean
```

- [ ] **Step 8.6: Write the findings doc**

Create `docs/superpowers/analysis/2026-06-05-sp1-cft-empirical-findings.md` with: a probe→result table (A: process name verbatim; B: innerHeight numbers + screenshot verdict; C: keychain observation; D: timing), raw command outputs, and a "Consequences" section (which SP1 constants the data confirms; what SP2 must decide — the R1-I co-pilot-headful question). Include the CfT version installed by Task 4.5 and the Gatekeeper/quarantine observation.

- [ ] **Step 8.7: Commit**

```bash
git add docs/superpowers/analysis/2026-06-05-sp1-cft-empirical-findings.md
git commit -m "docs(sp1): CfT empirical findings — process name, R1-I infobar/viewport, R1-K keychain, hole-J probe"
```

---

### Task 9: Docs sync + full suite + PR

**Files:**
- Modify: `CLAUDE.md` (Lanes paragraph), `skills/look/SKILL.md` (lane docs), `docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md` (§5 SP1 row)

- [ ] **Step 9.1: CLAUDE.md — extend the Lanes paragraph**

In the `**Lanes (/look-v2, 2026-06):**` paragraph of `CLAUDE.md`, append after the `--insecure` sentence:

```markdown
**Automation lane (SP1, #164):** opt-in `--automation` / `LOOK_AUTOMATION=1` — pinned Chrome for Testing binary by default (`/0/.jaine/.browser/cft/current`, installed/pinned ONLY by `skills/look/scripts/update-cft.sh`), `--enable-automation` + `--use-mock-keychain`, temp per-port drive-profile (`$TMPDIR/jaine-drive-<port>`), gated **fail-closed**: non-9333 port + profile that does not resolve to the daily profile. `CHROME_APP_NAME` threads the AppleScript/Quartz app name (defaults: stock `Google Chrome`; automation `Google Chrome for Testing`).
```

- [ ] **Step 9.2: SKILL.md — Automation lane section**

In `skills/look/SKILL.md`, after the "Web-security lane" section, add a sibling section (mirror its structure/tone):

```markdown
### Automation lane (Chrome for Testing — SP1, #164)

`launch.sh --automation` (or `LOOK_AUTOMATION=1`) starts the lane on the **pinned
Chrome for Testing** (`/0/.jaine/.browser/cft/current` — install/refresh via
`skills/look/scripts/update-cft.sh`; launching never auto-updates) with
`--enable-automation` (suppresses the bad-flags infobar; CfT alone does not) and
`--use-mock-keychain` (no macOS keychain prompts). Fail-closed gate: requires a
non-9333 `CDP_PORT` AND a profile that does not resolve to the daily profile;
without an explicit `LOOK_PROFILE_DIR` the lane gets a temp per-port profile
(`$TMPDIR/jaine-drive-<port>`). **Lane contract for cdp.py:** every `cdp.py` call
against a CfT lane carries BOTH `CDP_PORT=<port>` and
`CHROME_APP_NAME="Google Chrome for Testing"` — launch.sh's automation default
does not propagate to separate cdp.py processes, and without it the
AppleScript/native paths target stock Chrome. This is the engine lane `/drive`
(SP2) builds on; `/look` on 9333 is structurally unaffected.
```

- [ ] **Step 9.3: Spec — mark SP1 done**

In the spec's §5 table, prefix the SP1 scope cell with `✅ **DONE (2026-06-05)** — ` and append `Empirical findings: docs/superpowers/analysis/2026-06-05-sp1-cft-empirical-findings.md.` Keep the rest of the cell intact.

- [ ] **Step 9.4: Full suite**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -25`
Expected: all green except the **2 known worktree artifacts** (`test_claude_md_cache_path_uses_correct_marketplace`, `test_spec_cache_path_uses_correct_marketplace` — `get_project_root()` resolves to `.aitemp`; verified passing from the canonical plugin location). If they are the ONLY failures, re-verify them from `/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer` after merge, per established SP0 practice.

- [ ] **Step 9.5: Commit + PR**

```bash
git add CLAUDE.md skills/look/SKILL.md docs/superpowers/specs/2026-06-04-look-drive-test-command-design.md
git commit -m "docs(sp1): lanes documentation + spec SP1 row closed"
git push -u origin bulldozer/drive-sp1
gh pr create --repo A3IO/jaine-plugins --base bulldozer/main --title "SP1: Chrome for Testing foundation (#164)" --body "…summary per template…"
```

PR body covers: scope (§4.2), holes closed (B/C/E/G/J/K + I measured), test delta, empirical doc pointer, `/look` unchanged proof (byte-identical default argv test).

---

## Self-Review (per writing-plans)

- **Spec coverage:** §4.2 bullet 1 (app-name param) → Tasks 1+3; bullet 2 (CfT switch + automation path) → Task 5; bullet 3 (install/pin + update-cft.sh) → Task 4; bullet 4 (gate) → Task 5; bullet 5 (conftest) → Tasks 6+7. §5 SP1 extras: holes G/J/B *how* → Tasks 3+2; temp profiles R1-E → Task 5; empirical R1-I/K + process-name → Task 8. ✓
- **Placeholders:** none — every step carries the actual code/commands. The one intentionally-open item is Task 8's *findings* (they are measurements, unknowable pre-run; the doc structure is specified).
- **Type/name consistency:** `CHROME_APP_NAME` (env) / `CHROME_APP` (cdp.py symbol) / `CFT_BIN` (launch.sh + conftest, same path string) / `DRIVE_TEST_PORT=9359` / probes use 9360-9361 — registry in conftest keeps 9355 (stock), 9356→9358 (insecure, test_e2e.py), 9359 (CfT), 9360+ (probes) disjoint (R1-F2). ✓
- **R1 codex round reconciliation:** F1 — structural matchers narrowed to command invocations (`^\s*osascript\s`, comment-skip) + the existing A.7 CHROME_BIN guard rewritten for the `CHROME_BIN_DEFAULTED` block form, same invariant; F2 — DRIVE_TEST_PORT moved off the insecure lane's 9356 to 9359 + port registry; F3 — the CfT lane contract (CDP_PORT + CHROME_APP_NAME on every cdp.py call) is now explicit in the fixture, the e2e helper, a drift-guard test, and SKILL.md.
- **Known risks (explicit):** (a) BSD `pgrep -f` ERE `($|[[:space:]])` — mirrors the shipped launch.sh KILL_MATCH idiom, verified working there; (b) `ln -sfn` is unlink+symlink (not atomic) — acceptable, single-user pin; (c) CfT zip layout assumed `chrome-mac-arm64/…` — asserted post-unzip with fail-loud (Task 4.3); (d) dry-run does not existence-check CHROME_BIN (hermetic tests) — real launch preflights instead.
- **E1 pre-review reconciliation (spec §6 J):** the spec's "path-based not name-based where possible" is satisfied literally — launch.sh GUI AppleScript targets the lane by `unix id` (CHROME_PID), name-free; the single remaining name-based `tell application` lives in cdp.py's AppleScript JS channel where Chrome's dictionary requires a name (guarded by `timeout=10`) — that is the "where possible" boundary, stated in Task 3.
