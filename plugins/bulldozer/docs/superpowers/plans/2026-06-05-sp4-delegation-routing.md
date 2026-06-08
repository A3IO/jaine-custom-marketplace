# SP4: Subagent Delegation + Model-Routing Calibration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ephemeral CfT lanes (`CDP_PORT=0` + mktemp profile — zero-coordination subagent delegation, closes holes R1-H/R2-R) plus the calibration-experiment infrastructure (fixtures, frozen manifests, external grader), then run the 111-run model-routing experiment and write the routing table.

**Architecture:** Part A extends `launch.sh` with an ephemeral mode gated to the automation lane (spec §2.1 — fail-loud edges, mktemp profile derivation inside the existing automation block, post-spawn DevToolsActivePort wait, 4-line stdout contract) and replaces the drive SKILL.md delegation placeholder with the full template (spec §2.2). Part B adds calibration fixtures + frozen task manifests + a deterministic grader (spec §3), all merged via PR1; the experiment itself (Workflow tool, main session) runs after PR1 merges and produces the analysis doc + routing table as PR2.

**Tech Stack:** bash 3.2 (launch.sh), Python 3 stdlib (grader, tests), pytest (structural + e2e on pinned CfT), Workflow tool (experiment runner).

**Decisions locked (do not re-litigate; spec went bulldozer GO after 7 rounds / 9 findings):**
1. Ephemeral-by-construction (port=0 + mktemp) — Crys-approved deviation from umbrella's "allocator lock + ownership token"; spike-verified on CfT 149.0.7827.54.
2. `CDP_PORT=0` ONLY with `--automation`; caller-supplied `LOOK_PROFILE_DIR` + port 0 → ERROR (R1-F1).
3. Contract output: `CDP_PORT= / LANE_PROFILE= / LANE_KILL_MATCH= / LANE_BROWSER_BIN=` — kill match is launcher-escaped (R1-F3), browser bin is binary-path identity (R3-F1: /json/version cannot distinguish CfT from stock).
4. Grading is external, from runner-owned `cmd-NN.log` files only — never agent-returned fields (R1-F4).
5. Corpus frozen at a named SHA before the first run; pilot = infra smoke only; defective tasks dropped, never retuned (R2-F1).
6. Breaker rule predeclared with censored-run handling (R1-F5); floor 3 never lowered.
7. Marker strings are the literal cdp.py outputs: `CONSOLE_GATE_OK`, `clicked <tag> (trusted)`, `ASSERT_PASS`/`ASSERT_FAIL`, `CLICK_REQUIRE_TRUSTED_FAIL`, `CONSOLE_GATE_FAIL`.

**Anchors, not line numbers** (project doctrine): locate edit points by grepping the quoted anchor strings given per task.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `skills/look/scripts/launch.sh` | Modify | ephemeral mode: gates, mktemp profile, DevToolsActivePort wait, contract, LANE_FAIL |
| `tests/test_launch.py` | Modify | `TestEphemeralLane` structural tests (dry-run seam) |
| `tests/test_e2e_lanes.py` | Create | parallel-lanes e2e: hole-H regression, contract parse, teardown isolation |
| `tests/fixtures/drive-page.html` | Modify | + shadow-DOM component, + reactive re-insert element |
| `tests/fixtures/calibration/broken-click.html` | Create | T10a broken fixture (typo'd id) |
| `tests/fixtures/calibration/broken-gate.html` | Create | T10b broken fixture (null-ref error) |
| `skills/drive/data/calibration-manifests.json` | Create | frozen per-task command manifests + oracles (T1-T10b) |
| `skills/drive/scripts/grade_run.py` | Create | deterministic grader: cmd-NN.log → graded_success |
| `tests/test_grade_run.py` | Create | offline unit tests for the grader |
| `tests/test_e2e_drive.py` | Modify | + shadow-DOM assert e2e, + reactive-state assert e2e (drift-guards for T7/T8 patterns) |
| `skills/drive/SKILL.md` | Modify | delegation section replaces placeholder; routing table lands in PR2 |
| `tests/test_drive_skill.py` | Modify | structural drift-guards for the delegation section |
| `tests/conftest.py` | Modify | port-registry comment only (9361) |
| `CLAUDE.md` | Modify | /drive architecture + changelog (PR1); routing table note (PR2) |
| `docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md` | Create (PR2) | experiment results, routing derivation, breaker verdict |
| umbrella spec §5 SP4 row | Modify (PR2) | → ✅ DONE |

**PR boundaries:** Tasks 1-8 → PR1 (infra). Task 9 (experiment, main session, post-merge) → PR2 (analysis + routing table + DONE marks).

---

### Task 1: Ephemeral gates — fail-loud edges

**Files:**
- Modify: `skills/look/scripts/launch.sh` (anchor: `if (( AUTOMATION_REQUESTED )); then`)
- Test: `tests/test_launch.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_launch.py`:

```python
class TestEphemeralLane:
    """SP4 §2.1: CDP_PORT=0 = ephemeral automation lane (mktemp profile, OS port)."""

    def test_port0_requires_automation(self):
        # Spec §2.1: CDP_PORT=0 without --automation/LOOK_AUTOMATION → ERROR exit 1 (YAGNI: no /look use case).
        r = _run_launch(env_override={"CDP_PORT": "0"})
        assert r.returncode == 1
        assert "ERROR" in r.stderr and "automation" in r.stderr.lower()

    def test_port0_rejects_caller_profile(self):
        # Spec §2.1 R1-F1: a caller-supplied profile breaks the uniqueness invariant
        # (two subagents passing the same dir would share a profile and kill each other).
        r = _run_launch(args=["--automation"],
                        env_override={"CDP_PORT": "0", "LOOK_PROFILE_DIR": "/tmp/reused-lane"})
        assert r.returncode == 1
        assert "ERROR" in r.stderr and "LOOK_PROFILE_DIR" in r.stderr
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_launch.py::TestEphemeralLane -v`
Expected: both FAIL — current launch.sh happily proceeds with port 0 (no gate exists yet); `test_port0_requires_automation` gets rc 0 dry-run-less real-launch attempt or rc != 1.

- [ ] **Step 3a: Open the port-range gate for 0** — launch.sh rejects `CDP_PORT=0` long before the automation block (anchor: `if (( CDP_PORT < 1 || CDP_PORT > 65535 )); then`). Change the arithmetic check to admit 0 as the documented ephemeral sentinel:

```bash
# 0 = SP4 ephemeral lane (OS-assigned port) — gated to --automation below.
if (( CDP_PORT < 0 || CDP_PORT > 65535 )); then
  echo "ERROR: CDP_PORT must be an integer in 1..65535, or 0 for the ephemeral automation lane (got: $CDP_PORT)" >&2
  exit 1
fi
```

(The regex guard above it already admits `0` — only the arithmetic floor changes. The ephemeral gates in Step 3b ensure a bare `CDP_PORT=0` without `--automation` still dies loudly, so 0 never reaches a real launch ungated.)

In the same commit, update the pre-existing `test_out_of_range_port_fails_loud` (anchor: `for bad in ("0", "70000", "-5", "18446744073709551617"):`) — `"0"` is no longer out-of-range, it is the ephemeral sentinel with its own gates:

```python
    for bad in ("70000", "-5", "18446744073709551617"):   # "0" moved to TestEphemeralLane (SP4 sentinel)
```

- [ ] **Step 3b: Implement the gates** — in `launch.sh`, immediately BEFORE the automation block (anchor: the comment `# ── Automation lane (SP1, #164)`), add:

```bash
# ── Ephemeral lane (SP4): CDP_PORT=0 → OS-assigned port + launcher-owned mktemp
#    profile (unique by construction — the profile IS the ownership token; holes
#    R1-H/R2-R closed structurally). Supported ONLY under --automation (fail-loud
#    edges, spec §2.1): a caller-supplied LOOK_PROFILE_DIR would break the
#    uniqueness invariant (two subagents sharing a dir would pkill each other). ──
EPHEMERAL=0
if (( CDP_PORT == 0 )); then
  if (( PROFILE_OVERRIDDEN )); then
    echo "ERROR: CDP_PORT=0 (ephemeral lane) with a caller-supplied LOOK_PROFILE_DIR breaks" >&2
    echo "       the uniqueness invariant — the launcher owns the ephemeral profile." >&2
    echo "       Unset LOOK_PROFILE_DIR or pick a fixed port. Refusing." >&2
    exit 1
  fi
  EPHEMERAL=1
fi
```

and INSIDE the automation gate's failure branch coverage — after the existing `AUTOMATION_REQUESTED` computation (anchor: `AUTOMATION=0`), add the no-automation reject:

```bash
if (( EPHEMERAL )) && (( ! AUTOMATION_REQUESTED )); then
  echo "ERROR: CDP_PORT=0 (ephemeral lane) is supported ONLY with --automation /" >&2
  echo "       LOOK_AUTOMATION — there is no /look use case for an ephemeral port." >&2
  exit 1
fi
```

(Note: the `PROFILE_OVERRIDDEN` check fires first by placement — both orderings are correct, the tests pin behavior not order.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_launch.py::TestEphemeralLane -v`
Expected: 2 PASS. Also run the whole launch suite to catch regressions: `python3 -m pytest tests/test_launch.py -q` → all green.

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(launch): SP4 ephemeral lane gates — port 0 requires automation, rejects caller profile"
```

---

### Task 2: Ephemeral profile derivation + dry-run seam

**Files:**
- Modify: `skills/look/scripts/launch.sh` (anchor: `PROFILE_DIR="${TMPDIR:-/tmp}/jaine-drive-${CDP_PORT}"`)
- Test: `tests/test_launch.py`

- [ ] **Step 1: Write the failing tests** — append to `TestEphemeralLane`:

```python
    def test_port0_derives_mktemp_profile(self):
        # Spec §2.1: jaine-drive-${CDP_PORT} would collide as jaine-drive-0 for every
        # ephemeral lane → mktemp pattern instead; PROFILE_OVERRIDDEN=1 after derivation.
        r = _run_launch(args=["--automation"], env_override={"CDP_PORT": "0"})
        assert r.returncode == 0, r.stderr
        cfg, argv = _parse_dryrun(r.stdout)
        assert "jaine-drive-eph-" in cfg["profile"]
        assert "jaine-drive-0" not in cfg["profile"]
        assert cfg["profile_overridden"] == "1"
        assert "--remote-debugging-port=0" in argv

    def test_port0_dryrun_does_not_leave_profile_dir(self):
        # mktemp -d creates the dir at derivation time; dry-run must rmdir it (no litter).
        r = _run_launch(args=["--automation"], env_override={"CDP_PORT": "0"})
        cfg, _ = _parse_dryrun(r.stdout)
        assert not os.path.isdir(cfg["profile"]), "dry-run left the mktemp profile behind"

    def test_port0_composes_with_insecure(self):
        # Spec §2.1 gate invariant: ephemeral profile satisfies insecure's
        # PROFILE_OVERRIDDEN requirement via the same mechanism as automation's temp profile.
        r = _run_launch(args=["--automation", "--insecure"], env_override={"CDP_PORT": "0"})
        assert r.returncode == 0, r.stderr
        cfg, argv = _parse_dryrun(r.stdout)
        assert cfg["insecure"] == "1"
        assert "--disable-web-security" in argv

    def test_port0_composes_with_cert_spki(self):
        pin = "A" * 43 + "="
        r = _run_launch(args=["--automation", "--cert-spki=" + pin],
                        env_override={"CDP_PORT": "0"})
        assert r.returncode == 0, r.stderr
        cfg, argv = _parse_dryrun(r.stdout)
        assert cfg["cert_spki"] == pin
        assert "--ignore-certificate-errors-spki-list=" + pin in argv

    def test_port0_log_lives_inside_profile(self):
        # Mirror of the PROFILE_OVERRIDDEN LOG rule: chrome.log inside the mktemp profile
        # so an rm -rf of the profile removes the log too.
        r = _run_launch(args=["--automation"], env_override={"CDP_PORT": "0"})
        cfg, _ = _parse_dryrun(r.stdout)
        assert cfg["log"] == cfg["profile"] + "/chrome.log"

    def test_port0_dryrun_marks_ephemeral(self):
        # Dry-run cannot show the OS-assigned port (it exists only after a real spawn) —
        # it marks the mode instead; the full 4-line contract is e2e territory (test_e2e_lanes).
        r = _run_launch(args=["--automation"], env_override={"CDP_PORT": "0"})
        cfg, _ = _parse_dryrun(r.stdout)
        assert cfg["ephemeral"] == "1"
        r2 = _run_launch(args=["--automation"], env_override={"CDP_PORT": "9341"})
        cfg2, _ = _parse_dryrun(r2.stdout)
        assert cfg2["ephemeral"] == "0"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_launch.py::TestEphemeralLane -v`
Expected: the 5 new tests FAIL (profile is `jaine-drive-0`, log path wrong, etc.); the 2 Task-1 tests still PASS.

- [ ] **Step 3: Implement derivation** — inside the automation block, replace the temp-profile assignment. Current code (anchor):

```bash
  if (( ! PROFILE_OVERRIDDEN )); then
    # Hole E (R1-E): drive lanes get a TEMP profile — deterministic per port so the
    # lane's pkill-by-profile restart contract still holds — not a persistent
    # profile-<port> accumulating under /0/.jaine. macOS cleans TMPDIR on reboot.
    PROFILE_DIR="${TMPDIR:-/tmp}/jaine-drive-${CDP_PORT}"
```

becomes:

```bash
  if (( ! PROFILE_OVERRIDDEN )); then
    if (( EPHEMERAL )); then
      # SP4 §2.1: the deterministic jaine-drive-${CDP_PORT} rule would collide as
      # jaine-drive-0 for EVERY ephemeral lane — mktemp is unique by construction.
      # The unique profile IS the ownership token (holes R1-H/R2-R).
      PROFILE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/jaine-drive-eph-XXXXXX") || {
        echo "ERROR: mktemp failed for the ephemeral profile" >&2; exit 1; }
    else
      # Hole E (R1-E): drive lanes get a TEMP profile — deterministic per port so the
      # lane's pkill-by-profile restart contract still holds — not a persistent
      # profile-<port> accumulating under /0/.jaine. macOS cleans TMPDIR on reboot.
      PROFILE_DIR="${TMPDIR:-/tmp}/jaine-drive-${CDP_PORT}"
    fi
```

(keep the existing backslash/newline guard + `PROFILE_OVERRIDDEN=1` + `LOG="$PROFILE_DIR/chrome.log"` lines that follow — they now apply to both branches).

Then in the dry-run block (anchor: `if [[ "${LOOK_DRY_RUN:-}" == "1" ]]; then`), add the mode marker next to the other config lines (anchor: `echo "automation=$AUTOMATION"`):

```bash
  echo "ephemeral=$EPHEMERAL"
```

and just before `exit 0`, the litter cleanup:

```bash
  if (( EPHEMERAL )); then
    rmdir "$PROFILE_DIR" 2>/dev/null  # mktemp created it; dry-run must not litter (empty by construction)
  fi
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_launch.py -q`
Expected: all green (7 ephemeral + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/launch.sh tests/test_launch.py
git commit -m "feat(launch): SP4 ephemeral profile derivation — mktemp, gate composition, dry-run cleanup"
```

---

### Task 3: Parallel-lanes e2e (RED first — the hole-H regression test)

**Files:**
- Create: `tests/test_e2e_lanes.py`

- [ ] **Step 1: Write the e2e file** (it will FAIL until Task 4 ships the wait+contract):

```python
"""SP4 ephemeral-lanes e2e — the hole-H regression suite.

Two ephemeral lanes launched in parallel must get distinct OS-assigned ports and
unique mktemp profiles; tearing one down by its LANE_KILL_MATCH must leave the
other alive. Self-skips when the pinned CfT is not installed (same policy as
conftest.cft_browser).
"""
import os
import re
import subprocess
import time
import urllib.request

import pytest

from conftest import CFT_BIN, LANE_ENV_VARS

LAUNCH_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "skills", "look", "scripts", "launch.sh")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CFT_BIN) and os.access(CFT_BIN, os.X_OK)),
    reason="Chrome for Testing not installed — run skills/look/scripts/update-cft.sh",
)

CONTRACT_KEYS = ("CDP_PORT", "LANE_PROFILE", "LANE_KILL_MATCH", "LANE_BROWSER_BIN")


def _cdp_alive(port, timeout=2):
    try:
        with urllib.request.urlopen("http://localhost:{}/json/version".format(port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _spawn_ephemeral():
    """Start one ephemeral-lane launcher WITHOUT waiting — parallelism lives here.

    launch.sh redirects Chrome itself into the lane's chrome.log — its own stdout
    carries only the small contract + status lines, so PIPE cannot fill (64KB).
    """
    env = os.environ.copy()
    for v in LANE_ENV_VARS:
        env.pop(v, None)
    env["CDP_PORT"] = "0"
    env["LOOK_HEADLESS"] = "1"
    return subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "--automation", "about:blank"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _collect_contract(proc, timeout=25):
    out, err = proc.communicate(timeout=timeout)
    def _kill_spawned():
        # RED-phase hygiene: a launch that came up but printed no contract still
        # started a real headless Chrome — kill it by the PID the launcher prints
        # ("JAINE Browser started (PID N, …)") so failing asserts don't leak browsers.
        m = re.search(r"started \(PID (\d+)", out or "")
        if m:
            subprocess.run(["kill", m.group(1)], capture_output=True)
    assert proc.returncode == 0, "launch failed rc={}\nstdout:\n{}\nstderr:\n{}".format(proc.returncode, out, err)
    contract = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in CONTRACT_KEYS:
            contract[k] = v
    missing = [k for k in CONTRACT_KEYS if k not in contract]
    if missing:
        _kill_spawned()
        raise AssertionError("contract lines missing {} in stdout:\n{}".format(missing, out))
    return contract


def _launch_ephemeral(timeout=25):
    return _collect_contract(_spawn_ephemeral(), timeout=timeout)


def _teardown(contract):
    subprocess.run(["pkill", "-f", "--", contract["LANE_KILL_MATCH"]], capture_output=True)
    deadline = time.time() + 10
    while time.time() < deadline and _cdp_alive(contract["CDP_PORT"], timeout=1):
        time.sleep(0.3)


class TestEphemeralLanesE2E:
    def test_two_parallel_lanes_distinct_and_isolated(self):
        # genuinely parallel: BOTH launcher processes start before either is awaited —
        # this is the racy window holes R1-H/R2-R were about (spec §2.3 "in parallel")
        pa, pb = _spawn_ephemeral(), _spawn_ephemeral()
        try:
            a = _collect_contract(pa)
        except Exception:
            pb.kill()
            raise
        try:
            b = _collect_contract(pb)
        except Exception:
            _teardown(a)
            raise
        try:
            # distinct ports, distinct profiles — uniqueness by construction
            assert a["CDP_PORT"] != b["CDP_PORT"]
            assert a["LANE_PROFILE"] != b["LANE_PROFILE"]
            assert _cdp_alive(a["CDP_PORT"]) and _cdp_alive(b["CDP_PORT"])
            # hole-H regression: killing A by ITS kill match leaves B alive
            _teardown(a)
            assert not _cdp_alive(a["CDP_PORT"])
            assert _cdp_alive(b["CDP_PORT"]), "teardown of lane A killed lane B — hole H regressed"
        finally:
            _teardown(b)

    def test_contract_matches_devtools_active_port(self):
        c = _launch_ephemeral()
        try:
            dtap = os.path.join(c["LANE_PROFILE"], "DevToolsActivePort")
            assert os.path.isfile(dtap)
            with open(dtap) as f:
                first = f.readline().strip()
            assert first == c["CDP_PORT"], "contract port != DevToolsActivePort line 1"
            # spike fact: no trailing newline — head/readline parse. Deliberately a WEAK bound:
            # the macOS default ephemeral range is 49152-65535 but it is sysctl-tunable
            # (net.inet.ip.portrange.*) — asserting >= 49152 would flake on tuned hosts.
            # The load-bearing claim is only "never lands in OUR fixed registry range".
            assert int(c["CDP_PORT"]) not in range(9330, 9370), "ephemeral port landed in the fixed registry range"
        finally:
            _teardown(c)

    def test_browser_bin_is_pinned_cft(self):
        c = _launch_ephemeral()
        try:
            assert c["LANE_BROWSER_BIN"].startswith("/0/.jaine/.browser/cft/"), (
                "R3-F1: LANE_BROWSER_BIN must prove binary-path identity under the CfT pin"
            )
        finally:
            _teardown(c)
```

- [ ] **Step 2: Run to verify it fails for the RIGHT reason**

Run: `python3 -m pytest tests/test_e2e_lanes.py -v`
Expected: FAIL with `contract lines missing ['CDP_PORT', 'LANE_PROFILE', ...]` — launch.sh does not print the contract yet. (If it fails with skip — CfT not installed — run `bash skills/look/scripts/update-cft.sh` first.)

- [ ] **Step 3: Commit the RED tests**

```bash
git add tests/test_e2e_lanes.py
git commit -m "test(e2e): SP4 parallel ephemeral lanes — hole-H regression suite (RED)"
```

---

### Task 4: Real-launch wait + contract output + LANE_FAIL

**Files:**
- Modify: `skills/look/scripts/launch.sh` (anchors: `CHROME_PID=$!` and `if kill -0 "$CHROME_PID"`)

- [ ] **Step 1: Implement the post-spawn ephemeral wait** — immediately after `CHROME_PID=$!` / before the existing `sleep 3` (anchor: `"${CHROME_ARGV[@]}" >> "$LOG" 2>&1 &`):

```bash
if (( EPHEMERAL )); then
  # SP4 §2.1 readiness: Chrome writes <profile>/DevToolsActivePort — line 1 = port,
  # line 2 = ws path, NO trailing newline (spike-verified on CfT 149.0.7827.54).
  _dtap="$PROFILE_DIR/DevToolsActivePort"
  _eph_deadline=$(( SECONDS + 10 ))
  while (( SECONDS < _eph_deadline )) && [[ ! -s "$_dtap" ]]; do
    sleep 0.2
  done
  if [[ ! -s "$_dtap" ]]; then
    echo "LANE_FAIL: DevToolsActivePort not written within 10s (profile $PROFILE_DIR)" >&2
    kill "$CHROME_PID" 2>/dev/null
    rm -rf "$PROFILE_DIR"
    exit 1
  fi
  IFS= read -r CDP_PORT < "$_dtap"   # head-1 semantics; no trailing-newline assumptions
  _eph_ok=0
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -m 2 "http://localhost:$CDP_PORT/json/version" >/dev/null 2>&1; then
      _eph_ok=1; break
    fi
    sleep 0.3
  done
  if (( ! _eph_ok )); then
    echo "LANE_FAIL: CDP on port $CDP_PORT never answered /json/version" >&2
    kill "$CHROME_PID" 2>/dev/null
    rm -rf "$PROFILE_DIR"
    exit 1
  fi
fi
```

- [ ] **Step 2: Print the contract** — in the success tail (anchor: `if kill -0 "$CHROME_PID"`), inside the success branch after the existing `echo "JAINE Browser started …"`, add:

```bash
  if (( EPHEMERAL )); then
    # SP4 §2.1 contract — parseable final lines for delegation consumers.
    echo "CDP_PORT=$CDP_PORT"
    echo "LANE_PROFILE=$PROFILE_DIR"
    echo "LANE_KILL_MATCH=$KILL_MATCH"
    echo "LANE_BROWSER_BIN=$CHROME_BIN"
  fi
```

(`KILL_MATCH` was built from the mktemp `PROFILE_DIR` earlier via `_escape_ere` — R1-F3: consumers paste it verbatim. `CHROME_BIN` resolved to the CfT pin by the automation default — R3-F1 binary identity.)

- [ ] **Step 3: Run the e2e to verify GREEN**

Run: `python3 -m pytest tests/test_e2e_lanes.py -v`
Expected: 3 PASS (two-lane isolation, DevToolsActivePort parity, CfT binary identity).

- [ ] **Step 4: Run the full offline suite for regressions**

Run: `python3 -m pytest tests/test_launch.py tests/test_cdp.py tests/test_drive_skill.py -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add skills/look/scripts/launch.sh
git commit -m "feat(launch): SP4 ephemeral wait + 4-line lane contract + fail-loud LANE_FAIL"
```

---

### Task 5: Calibration fixture elements (shadow DOM + reactive re-insert)

**Files:**
- Modify: `tests/fixtures/drive-page.html` (anchor: the below-fold spacer `<!-- spacer: pushes the next target below the fold -->` — insert the two sections BEFORE it)
- Test: `tests/test_e2e_drive.py`

- [ ] **Step 1: Write the failing e2e tests** — append to `tests/test_e2e_drive.py` (uses the existing `cft_browser` + `test_server` fixtures; follow the file's existing helper conventions for running cdp.py):

```python
class TestCalibrationFixtureElements:
    """SP4 §3.1 fixture elements (dogfood #172 patterns) — drift-guards for the
    T4 (reactive selector flaps forever), T7 (shadow pierce) and T8 (state assert)
    oracles. Uses the file's existing helpers: _drive_cdp(port, [args]) and
    _drive_url(test_server) — test_server is the PORT, _drive_url builds the page URL."""

    def test_shadow_dom_assert_via_js(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "--js",
                       "!!document.querySelector('#shadow-host')?.shadowRoot?.querySelector('canvas')",
                       "--timeout", "5"])
        assert r.returncode == 0 and "ASSERT_PASS" in r.stdout

    def test_reactive_state_assert_stable_while_selector_flaps(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        # state-based assert (the T8 oracle) is stable
        r = _drive_cdp(cft_browser, ["assert", "--js",
                       "window.__reactiveState && window.__reactiveState.showPopup === true",
                       "--stable", "500", "--timeout", "5"])
        assert r.returncode == 0 and "ASSERT_PASS" in r.stdout
        # selector-based assert on the FOREVER re-inserted node flaps deterministically —
        # unlike #flappy (which settles after 6 toggles), #reactive-elem never stops:
        # this is the T4 oracle's guaranteed ASSERT_FAIL + flapped (the #172 anti-pattern)
        r2 = _drive_cdp(cft_browser, ["assert", "#reactive-elem", "--visible",
                        "--stable", "1500", "--timeout", "4"])
        assert r2.returncode == 1 and "flapped" in r2.stdout
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestCalibrationFixtureElements -v`
Expected: FAIL — `#shadow-host` / `__reactiveState` don't exist in the fixture yet.

- [ ] **Step 3: Add the fixture elements** — in `drive-page.html`, before the below-fold spacer:

```html
    <section>
        <h2>Shadow DOM (SP4 T7 — dogfood #172)</h2>
        <div id="shadow-host"></div>
    </section>

    <section>
        <h2>Reactive re-insert (SP4 T8 — Alpine x-if imitation)</h2>
        <div id="reactive-region"><span id="reactive-elem">popup content</span></div>
    </section>

    <script>
    // Shadow host: querySelector cannot pierce this — the T7 oracle uses .shadowRoot
    (function () {
        var host = document.getElementById('shadow-host');
        var root = host.attachShadow({mode: 'open'});
        var canvas = document.createElement('canvas');
        canvas.width = 80; canvas.height = 20;
        root.appendChild(canvas);
    })();

    // Reactive re-insert cycle: every 400ms the element is removed and re-inserted,
    // imitating Alpine x-if churn — a selector-based assert with a stability window
    // flaps forever; the reactive STATE below is the stable thing to assert (T8).
    window.__reactiveState = { showPopup: true };
    (function () {
        var region = document.getElementById('reactive-region');
        setInterval(function () {
            var el = document.getElementById('reactive-elem');
            if (el) {
                el.remove();
            } else {
                var span = document.createElement('span');
                span.id = 'reactive-elem';
                span.textContent = 'popup content';
                region.appendChild(span);
            }
        }, 400);
    })();
    </script>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_e2e_drive.py::TestCalibrationFixtureElements -v`
Expected: 2 PASS. Then the whole drive e2e for regressions: `python3 -m pytest tests/test_e2e_drive.py -q` → green (the new interval must not break existing tests — it only touches `#reactive-region`).

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/drive-page.html tests/test_e2e_drive.py
git commit -m "feat(fixtures): SP4 shadow-DOM + reactive re-insert elements with e2e drift-guards"
```

---

### Task 6: Broken fixtures + frozen manifests + external grader

**Files:**
- Create: `tests/fixtures/calibration/broken-click.html`, `tests/fixtures/calibration/broken-gate.html`
- Create: `skills/drive/data/calibration-manifests.json`
- Create: `skills/drive/scripts/grade_run.py`
- Test: `tests/test_grade_run.py`

- [ ] **Step 1: Create the broken fixtures.**

`tests/fixtures/calibration/broken-click.html` (T10a — typo'd id; the manifest targets `#target-btn`, the page ships `#tagret-btn`):

```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>T10a broken click</title>
<link rel="icon" href="data:,"></head>
<body>
    <h1>T10a: the button id is typo'd — fix the page until the manifest goes green</h1>
    <button id="tagret-btn" onclick="window.__t10aClicked = event.isTrusted">Target</button>
</body>
</html>
```

`tests/fixtures/calibration/broken-gate.html` (T10b — null-ref on load):

```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>T10b broken gate</title>
<link rel="icon" href="data:,"></head>
<body>
    <h1>T10b: a null-ref throws on load — fix the page until console --gate passes</h1>
    <div id="status">pending</div>
    <script>
    // BUG (intentional): #stats does not exist — .textContent on null throws.
    document.getElementById('stats').textContent = 'ready';
    </script>
</body>
</html>
```

- [ ] **Step 2: Create the frozen manifests** — `skills/drive/data/calibration-manifests.json`. Markers are the LITERAL cdp.py strings (spec §3.1; locked decision 7). `{URL}` = fixture-server base; `{COPY_URL}` = the agent's local copy of a broken fixture (T10 only):

```json
{
  "schema": "sp4-calibration/v1",
  "tasks": [
    {"id": "T1", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "console --gate"],
     "expected_exits": [0, 0],
     "expected_markers": ["loader=", "CONSOLE_GATE_OK"], "expected_classification": "pass"},
    {"id": "T2", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "click '#throw-midflow' --require-trusted", "console --gate"],
     "expected_exits": [0, 0, 1],
     "expected_markers": ["clicked", "(trusted)", "CONSOLE_GATE_FAIL"], "expected_classification": "page-error"},
    {"id": "T3", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "assert '#async-elem' --visible --stable 500 --timeout 10"],
     "expected_exits": [0, 0],
     "expected_markers": ["ASSERT_PASS"], "expected_classification": "pass"},
    {"id": "T4", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "assert '#reactive-elem' --visible --stable 1500 --timeout 6"],
     "expected_exits": [0, 1],
     "expected_markers": ["ASSERT_FAIL", "flapped"], "expected_classification": "flaky",
     "note": "#reactive-elem re-inserts FOREVER (unlike #flappy which settles after 6 toggles) — flapped is deterministic"},
    {"id": "T5", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "assert '#delayed-btn' --actionable --stable 300 --timeout 10", "click '#delayed-btn' --require-trusted"],
     "expected_exits": [0, 0, 0],
     "expected_markers": ["ASSERT_PASS", "clicked", "(trusted)"], "expected_classification": "pass"},
    {"id": "T6", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "click '#occluded-btn' --require-trusted"],
     "expected_exits": [0, 1],
     "expected_markers": ["CLICK_REQUIRE_TRUSTED_FAIL"], "expected_classification": "not-actionable"},
    {"id": "T7", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "assert --js \"!!document.querySelector('#shadow-host')?.shadowRoot?.querySelector('canvas')\" --timeout 5"],
     "expected_exits": [0, 0],
     "expected_markers": ["ASSERT_PASS"], "expected_classification": "pass"},
    {"id": "T8", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "assert --js \"window.__reactiveState && window.__reactiveState.showPopup === true\" --stable 500 --timeout 5"],
     "expected_exits": [0, 0],
     "expected_markers": ["ASSERT_PASS"], "expected_classification": "pass"},
    {"id": "T9", "class": "verify", "fixture": "drive-page.html",
     "commands": ["navigate {URL}/drive-page.html --wait load", "console --gate", "assert '#always-visible' --visible --stable 300", "click '#trusted-target' --require-trusted", "screenshot /tmp/sp4-t9.jpg --bind"],
     "expected_exits": [0, 0, 0, 0, 0],
     "expected_markers": ["loader=", "CONSOLE_GATE_OK", "ASSERT_PASS", "clicked", "(trusted)", "BIND url="], "expected_classification": "pass",
     "loader_match": [1, 5],
     "teardown_check": true},
    {"id": "T10a", "class": "fix-verify", "fixture": "calibration/broken-click.html",
     "commands": ["navigate {COPY_URL} --wait load", "assert '#target-btn' --actionable --stable 300 --timeout 5", "click '#target-btn' --require-trusted"],
     "expected_exits": [0, 0, 0],
     "expected_markers": ["ASSERT_PASS", "clicked", "(trusted)"], "expected_classification": "pass",
     "integrity": "the manifest itself asserts '#target-btn' — deleting/renaming the button cannot pass"},
    {"id": "T10b", "class": "fix-verify", "fixture": "calibration/broken-gate.html",
     "commands": ["navigate {COPY_URL} --wait load", "console --gate", "assert '#status' --visible --timeout 5"],
     "expected_exits": [0, 0, 0],
     "expected_markers": ["CONSOLE_GATE_OK", "ASSERT_PASS"], "expected_classification": "pass",
     "integrity": "the manifest asserts '#status' — gutting the page cannot pass"}
  ]
}
```

- [ ] **Step 3: Write the failing grader tests** — `tests/test_grade_run.py`:

```python
"""Offline unit tests for the SP4 external grader (spec §3.2 — grading reads ONLY
runner-owned cmd-NN.log files; agent-returned fields never grade)."""
import json
import os
import subprocess
import sys

import pytest

PLUGIN = os.path.join(os.path.dirname(__file__), "..")
GRADER = os.path.join(PLUGIN, "skills", "drive", "scripts", "grade_run.py")
MANIFESTS = os.path.join(PLUGIN, "skills", "drive", "data", "calibration-manifests.json")

sys.path.insert(0, os.path.dirname(GRADER))
import grade_run  # noqa: E402


def _write_run(tmp_path, logs):
    run = tmp_path / "T1-haiku-1"
    run.mkdir()
    for i, content in enumerate(logs):
        (run / "cmd-{:02d}.log".format(i)).write_text(content)
    return str(run)


class TestGradeRun:
    def test_all_markers_present_grades_success(self, tmp_path):
        run = _write_run(tmp_path, [
            "Browser: Chrome/149.0.7827.54\nEXIT=0\n",
            "http://localhost:9361/drive-page.html loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_OK\nEXIT=0\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is True

    def test_missing_marker_grades_failure(self, tmp_path):
        # exits are all consistent with the oracle — ONLY the marker is absent
        # (a capture that lost stdout): isolates the missing-markers path.
        run = _write_run(tmp_path, [
            "EXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "EXIT=0\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "missing-markers"
        assert "CONSOLE_GATE_OK" in res["missing_markers"]

    def test_gate_fail_with_wrong_exit_is_exit_mismatch(self, tmp_path):
        # the SAME wrong-outcome run graded via the exit leg: gate printed FAIL and
        # exited 1 where T1 expects 0 — exit check fires before marker check.
        run = _write_run(tmp_path, [
            "EXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_FAIL: 1 (1 exception(s), 0 console, 0 log)\nEXIT=1\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "exit-mismatch"

    def test_wrong_classification_grades_failure(self, tmp_path):
        # T4 expects classification=flaky; markers alone are not enough (spec §3.1 honest-classification)
        run = _write_run(tmp_path, [
            "EXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "ASSERT_FAIL #flappy — unstable: flapped 4x\nEXIT=1\n",
        ])
        ok = grade_run.grade(run, task_id="T4", classification="flaky", manifests_path=MANIFESTS)
        bad = grade_run.grade(run, task_id="T4", classification="absent", manifests_path=MANIFESTS)
        assert ok["graded_success"] is True
        assert bad["graded_success"] is False and bad["reason"] == "classification-mismatch"

    def test_missing_run_dir_grades_zero(self, tmp_path):
        # spec §3.2: capture is part of the task
        res = grade_run.grade(str(tmp_path / "nonexistent"), task_id="T1",
                              classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "run-dir-missing"

    def test_exit_code_mismatch_grades_failure(self, tmp_path):
        # spec §3.2: grading checks EXIT= codes, not just markers — a T1 gate that
        # printed CONSOLE_GATE_OK but exited 1 is an inconsistent capture → fail.
        run = _write_run(tmp_path, [
            "Browser: Chrome/149.0.7827.54\nEXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_OK\nEXIT=1\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "exit-mismatch"

    def test_missing_command_log_grades_failure(self, tmp_path):
        # T1 manifest has 2 commands → cmd-00 (pre-flight) + cmd-01..02 expected;
        # a missing log means a command was skipped — incomparable run.
        run = _write_run(tmp_path, [
            "Browser: Chrome/149.0.7827.54\nEXIT=0\n",
            "loader=AABB\nCONSOLE_GATE_OK\nEXIT=0\n",   # only ONE manifest log present
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "log-set-mismatch"

    def test_missing_preflight_log_grades_failure(self, tmp_path):
        # The hole-D pre-flight capture (cmd-00.log) is mandatory: right manifest logs
        # WITHOUT it = the binary-identity check was skipped — fail (review round 2 R2-F1).
        run = tmp_path / "T1-haiku-9"
        run.mkdir()
        (run / "cmd-01.log").write_text("loader=AABB\nEXIT=0\n")
        (run / "cmd-02.log").write_text("CONSOLE_GATE_OK\nEXIT=0\n")
        res = grade_run.grade(str(run), task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "log-set-mismatch"

    def test_failed_preflight_grades_failure(self, tmp_path):
        # Present-but-failed pre-flight (EXIT=7) must not grade success (round 3 R2-F1);
        # an empty cmd-00.log (no EXIT= trailer) is preflight-malformed.
        run = _write_run(tmp_path, [
            "curl: (7) Failed to connect\nEXIT=7\n",
            "loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_OK\nEXIT=0\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "preflight-failed"
        empty = tmp_path / "T1-haiku-8"
        empty.mkdir()
        (empty / "cmd-00.log").write_text("")
        (empty / "cmd-01.log").write_text("loader=AABB\nEXIT=0\n")
        (empty / "cmd-02.log").write_text("CONSOLE_GATE_OK\nEXIT=0\n")
        res2 = grade_run.grade(str(empty), task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res2["graded_success"] is False and res2["reason"] == "preflight-malformed"

    def _write_t9_run(self, tmp_path, nav_loader, bind_loader, teardown="PORT_FREE"):
        run = tmp_path / "T9-haiku-1"
        run.mkdir()
        logs = {
            "cmd-00.log": "Browser ok\nEXIT=0\n",
            # REAL navigate format is paren-terminated — "…, loader=X)" (cdp.py
            # "Navigated to {} ({} fired in {}ms, loader={})") — the fixture MUST
            # mimic it so the [^\s)]+ value class is actually exercised:
            "cmd-01.log": "Navigated to http://x/drive-page.html (load fired in 120ms, loader={})\nEXIT=0\n".format(nav_loader),
            "cmd-02.log": "CONSOLE_GATE_OK\nEXIT=0\n",
            "cmd-03.log": "ASSERT_PASS #always-visible held 300ms\nEXIT=0\n",
            "cmd-04.log": "clicked button (trusted)\nEXIT=0\n",
            "cmd-05.log": "/tmp/sp4-t9.jpg  800x600\nBIND url=http://x loader={} t=1\nEXIT=0\n".format(bind_loader),
            "cmd-99.log": teardown + "\nEXIT=0\n",
        }
        for name, content in logs.items():
            (run / name).write_text(content)
        return str(run)

    def test_t9_loader_mismatch_grades_failure(self, tmp_path):
        # spec T9: BIND loader must MATCH navigate loader — substring presence is not enough
        ok = grade_run.grade(self._write_t9_run(tmp_path, "AAAA11", "AAAA11"),
                             task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert ok["graded_success"] is True
        # fresh dir for the mismatch variant
        bad_dir = tmp_path / "mismatch"
        bad_dir.mkdir()
        bad = grade_run.grade(self._write_t9_run(bad_dir, "AAAA11", "BBBB22"),
                              task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert bad["graded_success"] is False and bad["reason"] == "loader-mismatch"

    def test_t9_teardown_not_verified_grades_failure(self, tmp_path):
        res = grade_run.grade(self._write_t9_run(tmp_path, "CC", "CC", teardown="PORT_STILL_ALIVE"),
                              task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "teardown-not-verified"

    def test_t9_teardown_without_capture_trailer_grades_failure(self, tmp_path):
        # round-4 failure mode pinned: PORT_FREE WITHOUT the EXIT=0 capture trailer is a
        # hand-written file, not runner-owned evidence — must fail the same way.
        run = self._write_t9_run(tmp_path, "DD", "DD")
        (tmp_path / "T9-haiku-1" / "cmd-99.log").write_text("PORT_FREE\n")   # no trailer
        res = grade_run.grade(run, task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "teardown-not-verified"
        (tmp_path / "T9-haiku-1" / "cmd-99.log").write_text("PORT_FREE\nEXIT=1\n")   # nonzero trailer
        res2 = grade_run.grade(run, task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res2["graded_success"] is False and res2["reason"] == "teardown-not-verified"

    def _write_t10_run(self, tmp_path, n_iters, last_green=True):
        # fix-verify layout: cmd-00 at the root, each cycle in iter-K/cmd-01..NN —
        # the grader counts iterations from the FILESYSTEM (independent evidence).
        run = tmp_path / "T10a-haiku-1"
        run.mkdir()
        (run / "cmd-00.log").write_text("Browser: Chrome/149.0.7827.54\nEXIT=0\n")
        for k in range(1, n_iters + 1):
            it = run / "iter-{}".format(k)
            it.mkdir()
            green = last_green and k == n_iters
            (it / "cmd-01.log").write_text("Navigated to file://x (load fired in 90ms, loader=AB12)\nEXIT=0\n")
            if green:
                (it / "cmd-02.log").write_text("ASSERT_PASS #target-btn held 300ms (total 350ms)\nEXIT=0\n")
                (it / "cmd-03.log").write_text("clicked button (trusted)\nEXIT=0\n")
            else:
                (it / "cmd-02.log").write_text("ASSERT_FAIL #target-btn — never true within 5000ms\nEXIT=1\n")
                (it / "cmd-03.log").write_text("CLICK_REQUIRE_TRUSTED_FAIL: '#target-btn' not hittable\nEXIT=1\n")
        return str(run)

    def test_t10_requires_integrity_pass(self, tmp_path):
        # spec §3.1 anti-gaming: T10 grades success ONLY with the orchestrator's
        # integrity re-run verdict (Task 9 Step 6) supplied as --integrity pass.
        run = self._write_t10_run(tmp_path, n_iters=1)
        ok = grade_run.grade(run, task_id="T10a", classification="pass",
                             manifests_path=MANIFESTS, integrity="pass")
        no = grade_run.grade(run, task_id="T10a", classification="pass",
                             manifests_path=MANIFESTS, integrity="fail")
        missing = grade_run.grade(run, task_id="T10a", classification="pass",
                                  manifests_path=MANIFESTS)
        assert ok["graded_success"] is True
        assert no["graded_success"] is False and no["reason"] == "integrity-failed"
        assert missing["graded_success"] is False and missing["reason"] == "integrity-missing"

    def test_t10_iterations_counted_from_filesystem(self, tmp_path):
        # The breaker statistic uses iterations_observed (iter-K dirs), NEVER the
        # agent's self-report (peer-review F4): 3 cycles, last green → 3.
        run = self._write_t10_run(tmp_path, n_iters=3)
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is True
        assert res["iterations_observed"] == 3

    def test_t10_censored_run_fails_on_last_iteration_markers(self, tmp_path):
        # Breaker-hit (censored) run: 3 cycles, none green — grader fails on the
        # highest-K cycle's markers; analysis counts iter-3 + red as censored.
        run = self._write_t10_run(tmp_path, n_iters=3, last_green=False)
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="fail")
        assert res["graded_success"] is False
        assert res["reason"] in ("missing-markers", "exit-mismatch")

    def test_t10_no_iterations_grades_zero(self, tmp_path):
        run = tmp_path / "T10a-haiku-1"
        run.mkdir()
        (run / "cmd-00.log").write_text("Browser ok\nEXIT=0\n")
        res = grade_run.grade(str(run), task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "no-iterations"

    def test_unknown_task_id_fails_loud(self, tmp_path):
        run = _write_run(tmp_path, ["EXIT=0\n"])
        with pytest.raises(KeyError):
            grade_run.grade(run, task_id="T99", classification="pass", manifests_path=MANIFESTS)

    def test_cli_emits_json(self, tmp_path):
        run = _write_run(tmp_path, [
            "EXIT=0\n", "loader=AABB\nEXIT=0\n", "CONSOLE_GATE_OK\nEXIT=0\n",
        ])
        r = subprocess.run([sys.executable, GRADER, "--run-dir", run, "--task", "T1",
                            "--classification", "pass"],
                           capture_output=True, text=True)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["graded_success"] is True
```

- [ ] **Step 4: Run to verify they fail**

Run: `python3 -m pytest tests/test_grade_run.py -v`
Expected: FAIL with `ModuleNotFoundError: grade_run` (file doesn't exist).

- [ ] **Step 5: Implement the grader** — `skills/drive/scripts/grade_run.py`:

```python
#!/usr/bin/env python3
"""SP4 external grader (spec §3.2): grade one calibration run from its runner-owned logs.

graded_success = log-count matches the manifest (cmd-00 = pre-flight, cmd-01.. = commands)
               AND every manifest log's trailing EXIT= matches expected_exits
               AND expected_markers ⊆ markers(run_dir/cmd-*.log)
               AND classification == oracle.expected_classification
               AND (fix-verify only) the orchestrator's integrity re-run verdict == pass
A missing/empty run_dir grades 0 — capture is part of the task (R1-F4).
The agent's self-reported fields NEVER grade; they only feed the honesty delta.
"""
import argparse
import glob
import json
import os
import re


def _load_task(manifests_path, task_id):
    with open(manifests_path) as f:
        data = json.load(f)
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    raise KeyError("unknown task id: {}".format(task_id))


def _fail(task_id, reason, missing=None):
    return {"task_id": task_id, "graded_success": False, "reason": reason,
            "missing_markers": missing or []}


def grade(run_dir, task_id, classification, manifests_path, integrity=None):
    task = _load_task(manifests_path, task_id)
    if not os.path.isdir(run_dir):
        return _fail(task_id, "run-dir-missing", list(task["expected_markers"]))
    # Layout contract (peer-review round, CC-agent lenses):
    #   verify tasks:      $RUN_DIR/cmd-00.log + cmd-01..NN (+ optional cmd-99) — flat.
    #   fix-verify tasks:  $RUN_DIR/cmd-00.log (+ cmd-99); each cycle in
    #                      $RUN_DIR/iter-K/cmd-01..NN. The grader counts iterations
    #                      from the FILESYSTEM (independent of the agent's
    #                      self-report) and grades the highest-K cycle.
    root_logs = sorted(glob.glob(os.path.join(run_dir, "cmd-*.log")))
    root_names = sorted(os.path.basename(p) for p in root_logs)
    has_teardown_log = "cmd-99.log" in root_names
    root_names = [n for n in root_names if n != "cmd-99.log"]   # tolerated everywhere,
    # REQUIRED via teardown_check (T9) — checked below.
    iterations_observed = 1
    if task["class"] == "fix-verify":
        iters = sorted(glob.glob(os.path.join(run_dir, "iter-*")),
                       key=lambda p: int(re.search(r"iter-(\d+)$", p).group(1)) if re.search(r"iter-(\d+)$", p) else 0)
        iters = [d for d in iters if os.path.isdir(d) and re.search(r"iter-\d+$", d)]
        if not iters:
            return _fail(task_id, "no-iterations")
        iterations_observed = len(iters)
        if root_names != ["cmd-00.log"]:
            return _fail(task_id, "log-set-mismatch")
        manifest_dir = iters[-1]
        expected_names = ["cmd-{:02d}.log".format(i + 1) for i in range(len(task["commands"]))]
    else:
        if not root_logs:
            return _fail(task_id, "run-dir-missing", list(task["expected_markers"]))
        manifest_dir = run_dir
        expected_names = ["cmd-{:02d}.log".format(i) for i in range(len(task["commands"]) + 1)]
    manifest_logs = sorted(glob.glob(os.path.join(manifest_dir, "cmd-*.log")))
    manifest_names = sorted(os.path.basename(p) for p in manifest_logs)
    manifest_names = [n for n in manifest_names if n != "cmd-99.log"]
    if manifest_names != expected_names:
        return _fail(task_id, "log-set-mismatch")
    if task.get("teardown_check") and not has_teardown_log:
        return _fail(task_id, "teardown-not-verified")
    # The pre-flight capture itself must be sound: a present-but-empty or
    # EXIT!=0 cmd-00.log means the hole-D liveness check failed or was faked
    # (review round 3 R2-F1 — existence alone is not enforcement).
    pre_path = os.path.join(run_dir, "cmd-00.log")
    if not os.path.isfile(pre_path):
        return _fail(task_id, "log-set-mismatch")
    with open(pre_path, errors="replace") as f:
        pre = f.read()
    pm = re.search(r"EXIT=(\d+)\s*$", pre)
    if not pm:
        return _fail(task_id, "preflight-malformed")
    if int(pm.group(1)) != 0:
        return _fail(task_id, "preflight-failed")
    manifest_logs = [p for p in manifest_logs
                     if not (p.endswith("cmd-00.log") or p.endswith("cmd-99.log"))]
    blob = ""
    for i, p in enumerate(manifest_logs):
        with open(p, errors="replace") as f:
            content = f.read()
        blob += content
        m = re.search(r"EXIT=(\d+)\s*$", content)
        if not m:
            return _fail(task_id, "capture-malformed")   # no EXIT= trailer → not the capture form
        if int(m.group(1)) != task["expected_exits"][i]:
            return _fail(task_id, "exit-mismatch")
    missing = [mk for mk in task["expected_markers"] if mk not in blob]
    if missing:
        return _fail(task_id, "missing-markers", missing)
    if task.get("loader_match"):
        # spec T9: the screenshot's BIND loader= must equal the navigate loader= —
        # substring presence alone would let a stale capture pass (round 3 R3-F1).
        i, j = task["loader_match"]
        # loader_match indices are 1-based into the manifest commands (i → cmd-0i.log);
        # 0 would be the pre-flight curl, which never prints loader= — invalid here.
        # Chrome's LoaderId is an OPAQUE string (not contractually hex). The two
        # emitters differ: navigate prints "…, loader=X)" (paren-terminated) while
        # screenshot --bind prints "BIND url=… loader=X t=…" (space-terminated) —
        # so the value class is [^\s)]+: never swallow the paren (peer-review F2,
        # sharpened: \S+ would capture "X)" from navigate and always mismatch).
        def _loader(idx):
            with open(os.path.join(run_dir, "cmd-{:02d}.log".format(idx)), errors="replace") as f:
                m = re.search(r"loader=([^\s)]+)", f.read())
            return m.group(1) if m else None
        la, lb = _loader(i), _loader(j)
        if not la or la != lb:
            return _fail(task_id, "loader-mismatch")
    if task.get("teardown_check"):
        with open(os.path.join(run_dir, "cmd-99.log"), errors="replace") as f:
            td = f.read()
        # same capture-form discipline as every other log: PORT_FREE without the
        # EXIT= trailer is a hand-written file, not runner-owned evidence (round 4)
        if not re.search(r"EXIT=0\s*$", td) or "PORT_FREE" not in td:
            return _fail(task_id, "teardown-not-verified")
    if classification != task["expected_classification"]:
        return _fail(task_id, "classification-mismatch")
    if task["class"] == "fix-verify":
        # spec §3.1 anti-gaming: the orchestrator re-runs the manifest's verify
        # commands against the agent's fixed copy (Task 9 Step 6) and passes the
        # verdict here. No verdict → not graded as success.
        if integrity is None:
            return _fail(task_id, "integrity-missing")
        if integrity != "pass":
            return _fail(task_id, "integrity-failed")
    return {"task_id": task_id, "graded_success": True, "reason": "", "missing_markers": [],
            "iterations_observed": iterations_observed}   # filesystem-counted — the breaker
            # statistic uses THIS, never the agent's self-reported iterations (honesty signal only)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--classification", required=True)
    ap.add_argument("--integrity", choices=["pass", "fail"], default=None,
                    help="orchestrator integrity re-run verdict (fix-verify tasks only)")
    ap.add_argument("--manifests", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "calibration-manifests.json"))
    args = ap.parse_args()
    print(json.dumps(grade(args.run_dir, args.task, args.classification,
                           args.manifests, integrity=args.integrity)))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_grade_run.py -v`
Expected: 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures/calibration/ skills/drive/data/calibration-manifests.json \
        skills/drive/scripts/grade_run.py tests/test_grade_run.py
git commit -m "feat(calibration): SP4 broken fixtures + frozen manifests + external grader (TDD)"
```

---

### Task 7: drive SKILL.md delegation section

**Files:**
- Modify: `skills/drive/SKILL.md` (anchor: `Delegation prompts MUST hard-code "mode: autonomous" and assign the subagent its OWN port from the 9340-9349 range (never the main session's lane; SP4 will automate`)
- Test: `tests/test_drive_skill.py`

- [ ] **Step 1: Write the failing drift-guard tests** — append to `tests/test_drive_skill.py` (follow the file's existing read-the-SKILL.md pattern):

```python
class TestDelegationSection:
    """SP4 §2.2 — the delegation contract must be pinned in SKILL.md verbatim."""

    def test_placeholder_replaced(self, skill_text):
        assert "SP4 will automate" not in skill_text

    def test_ephemeral_launch_with_env_strip(self, skill_text):
        assert "CDP_PORT=0" in skill_text
        # the 7 stripped vars from the conftest LANE_ENV_VARS canon — CDP_PORT and
        # LOOK_HEADLESS are SET explicitly (set-after-strip), so they are not -u'd (check round 5)
        for var in ("LOOK_PROFILE_DIR", "LOOK_INSECURE", "LOOK_DRY_RUN",
                    "CHROME_BIN", "LOOK_AUTOMATION", "CHROME_APP_NAME", "LOOK_CERT_SPKI"):
            assert "-u " + var in skill_text

    def test_contract_keys_documented(self, skill_text):
        for key in ("CDP_PORT=", "LANE_PROFILE=", "LANE_KILL_MATCH=", "LANE_BROWSER_BIN="):
            assert key in skill_text

    def test_preflight_binary_identity(self, skill_text):
        assert "/0/.jaine/.browser/cft/" in skill_text
        assert "LANE_BROWSER_BIN" in skill_text

    def test_capture_form_documented(self, skill_text):
        assert 'EXIT=$?' in skill_text and "cmd-" in skill_text
```

(`skill_text` — if the file already has a fixture loading `skills/drive/SKILL.md`, reuse it; otherwise add `@pytest.fixture\ndef skill_text(): return open(os.path.join(PLUGIN, "skills", "drive", "SKILL.md")).read()` matching the file's path conventions.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_drive_skill.py::TestDelegationSection -v`
Expected: FAIL — placeholder still present, contract keys absent.

- [ ] **Step 3: Edit SKILL.md.** Replace the Two-modes bullet (the anchor text above) with:

```markdown
- **Subagents are ALWAYS autonomous.** co-pilot is main-session-only: a subagent
  has no human channel — a co-pilot checkpoint inside one would hang forever. If
  you are running as a subagent, refuse co-pilot and run autonomous. Delegation
  prompts MUST hard-code "mode: autonomous".

## Subagent delegation (SP4 — ephemeral lanes)

The main session NEVER picks ports for subagents (that was the collision source —
hole H). Each subagent provisions its OWN ephemeral lane:

The whole lifecycle is ONE runnable block — launch with the clean-env guard,
bind the contract, pre-flight, drive, tear down. Strip every lane env var first
(the conftest `LANE_ENV_VARS` hermeticity canon: an inherited `LOOK_DRY_RUN=1`
would prevent the launch, `LOOK_INSECURE`/`LOOK_CERT_SPKI` silently alter flags,
a stray `CHROME_APP_NAME` pollutes later cdp.py calls — `launch.sh` deliberately
honors env-provided values, hermeticity is the caller's job):

```bash
PLUGIN="<plugin root>"                    # e.g. $CLAUDE_PLUGIN_ROOT
# 1. Launch ONCE, capturing stdout — the contract arrives on it.
out=$(env -u LOOK_PROFILE_DIR -u LOOK_INSECURE -u LOOK_DRY_RUN -u CHROME_BIN \
          -u LOOK_AUTOMATION -u CHROME_APP_NAME -u LOOK_CERT_SPKI \
          CDP_PORT=0 LOOK_HEADLESS=1 "$PLUGIN/skills/look/scripts/launch.sh" --automation) \
  || { echo "lane never came up (LANE_FAIL above)"; exit 1; }

# 2. Bind the contract — every later command uses these, nothing is implicit.
PORT=$(printf '%s\n' "$out" | sed -n 's/^CDP_PORT=//p' | tail -1)
LANE_PROFILE=$(printf '%s\n' "$out" | sed -n 's/^LANE_PROFILE=//p' | tail -1)
LANE_KILL_MATCH=$(printf '%s\n' "$out" | sed -n 's/^LANE_KILL_MATCH=//p' | tail -1)
LANE_BROWSER_BIN=$(printf '%s\n' "$out" | sed -n 's/^LANE_BROWSER_BIN=//p' | tail -1)
# Validate ALL four before ANY use: a partial contract must never reach the
# wrong-browser branch or teardown (an empty $LANE_KILL_MATCH would make pkill
# match far too much).
for v in PORT LANE_PROFILE LANE_KILL_MATCH LANE_BROWSER_BIN; do
  eval "val=\${$v}"
  [ -n "$val" ] || { echo "lane contract missing $v — refusing"; exit 1; }
done

# 3. Pre-flight (hole D, binary identity): /json/version's Browser string CANNOT
#    distinguish CfT from stock Chrome at the same version — binary PATH is the
#    only reliable check. Mismatch → STOP, never proceed on the wrong browser.
case "$LANE_BROWSER_BIN" in
  /0/.jaine/.browser/cft/*) : ;;
  *) echo "WRONG BROWSER: $LANE_BROWSER_BIN — refusing"; pkill -f -- "$LANE_KILL_MATCH"; exit 1 ;;
esac
# Graded runs ONLY: capture the liveness curl as the run's first log. $RUN_DIR is
# the directory the orchestrator gave you — ALWAYS path-prefix (a bare cmd-00.log
# lands in your cwd and grades 0). Generic (ungraded) delegations have no RUN_DIR —
# the guard makes this line a no-op for them instead of writing to "/cmd-00.log".
[ -n "${RUN_DIR:-}" ] && { curl -s -m 2 "http://localhost:$PORT/json/version"; echo "EXIT=$?"; } > "$RUN_DIR/cmd-00.log" 2>&1

# 4. Drive — lane contract unchanged: BOTH env keys on every cdp.py call.
CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" \
  python3 "$PLUGIN/skills/look/scripts/cdp.py" navigate "$TARGET_URL" --wait load

# 5. Teardown by the launcher-escaped pattern — verbatim, never hand-rolled.
#    The unique mktemp profile IS the ownership token: this pattern can only
#    ever kill your own browser, so parallel subagents cannot interfere.
pkill -f -- "$LANE_KILL_MATCH"
```

When a run must be graded externally (calibration, CI), wrap every command in
the capture form so the logs — not your retelling — carry the verdict. `$RUN_DIR`
is the directory the orchestrator gave you; path-prefix EVERY log (bare names
land in your cwd and the grader reads only `$RUN_DIR`):
```bash
{ CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" \
    python3 "<plugin>/skills/look/scripts/cdp.py" <command…>; echo "EXIT=$?"; } > "$RUN_DIR/cmd-NN.log" 2>&1
cat "$RUN_DIR/cmd-NN.log"
```
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_drive_skill.py -q`
Expected: all green (new class + pre-existing guards).

- [ ] **Step 5: Commit**

```bash
git add skills/drive/SKILL.md tests/test_drive_skill.py
git commit -m "docs(drive): SP4 subagent delegation section — ephemeral lanes, contract, pre-flight"
```

---

### Task 8: Registry comment + CLAUDE.md + full suite

**Files:**
- Modify: `tests/conftest.py` (anchor: `# 9360+                     — transient empirical probes`)
- Modify: `CLAUDE.md` (Architecture: /drive section + Changelog)

- [ ] **Step 1: Add the registry line** — in the conftest port-registry comment block, after the `9360+` line:

```python
# 9361                      — SP4 calibration fixture server (transient; experiment only)
```

- [ ] **Step 2: Update CLAUDE.md** — in `## Architecture: /drive`, append after the Modes paragraph:

```markdown
**Ephemeral lanes (SP4):** subagent delegation uses `CDP_PORT=0` — the OS assigns the
port, launch.sh derives a unique mktemp profile (`jaine-drive-eph-XXXXXX`), and prints a
4-line contract (`CDP_PORT/LANE_PROFILE/LANE_KILL_MATCH/LANE_BROWSER_BIN`). The unique
profile IS the ownership token (holes R1-H/R2-R closed structurally — no allocator, no
locks); teardown by the launcher-escaped `LANE_KILL_MATCH` can only kill its own browser.
Gated to `--automation` + launcher-owned profile, fail-loud otherwise. Delegation
protocol: `skills/drive/SKILL.md` → "Subagent delegation". Calibration assets:
`skills/drive/data/calibration-manifests.json` (frozen oracles) +
`skills/drive/scripts/grade_run.py` (external grader) + `tests/fixtures/calibration/`.
```

and in the `/drive` test-suite table row add `test_e2e_lanes.py`, `test_grade_run.py` to Files. Add a Changelog entry (next version per the file's SemVer convention):

```markdown
- **1.15.0 (2026-06-05)** — SP4 Part A: ephemeral lanes (`CDP_PORT=0` + mktemp profile +
  4-line contract, holes R1-H/R2-R), drive SKILL.md delegation section, calibration infra
  (fixtures T7/T8 + broken T10a/b + frozen manifests + external grader). Spec went
  bulldozer:check GO (7 rounds, 9 findings, 0 FP). Experiment + routing table = PR2.
```

- [ ] **Step 3: Run the FULL offline suite + e2e**

Run: `python3 -m pytest tests/ -q --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py`
Expected: all green (e2e_lanes + e2e_drive + e2e_cft self-run on the installed CfT; test_e2e.py needs the daily browser and test_check_e2e.py needs codex — both excluded here as usual).

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py CLAUDE.md
git commit -m "docs: SP4 registry line + CLAUDE.md architecture/changelog"
```

---

### Task 9: PR1 → review → merge, then the experiment (main session) → PR2

**This task is main-session work — NOT delegatable to an implementation subagent.**

- [ ] **Step 1: PR1.** Push the branch, open the PR (template: what/why/test plan), run `/code-review` + the consistency-audit pass per the per-SP process, fix findings, merge. Cache refresh: `jaine-sync plugins update bulldozer`.

- [ ] **Step 2: Corpus freeze.** On the merged main, record the freeze SHA:

```bash
git -C /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer log -1 --format='freeze SHA: %H'
```

The analysis doc opens with this SHA. After this point task content is immutable (defective task → dropped across all models, never retuned — R2-F1).

- [ ] **Step 3: Fixture server + EXPERIMENT_DIR.**

```bash
EXPERIMENT_DIR=/tmp/sp4-calibration-$(date +%Y%m%d)
mkdir -p "$EXPERIMENT_DIR/runs"
# Pre-create EVERY per-cell run dir (spec §3.2 "the orchestrator pre-creates …" —
# the Workflow prompt tells agents the dir already exists; a missing dir grades 0):
for m in haiku sonnet opus; do
  for t in T1 T2 T3 T4 T5 T6 T7 T8 T9; do
    for r in 1 2 3; do mkdir -p "$EXPERIMENT_DIR/runs/$t-$m-$r"; done
  done
  for t in T10a T10b; do
    for r in 1 2 3 4 5; do mkdir -p "$EXPERIMENT_DIR/runs/$t-$m-$r"; done
  done
done
ls "$EXPERIMENT_DIR/runs" | wc -l   # expect 111
if curl -s -m1 http://localhost:9361/ >/dev/null 2>&1; then
  echo "ERROR: port 9361 already serving — kill the stale server or pick another per the registry. STOP."
  false   # fail the step loudly; do NOT start a second server on a busy port
else
  (cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/tests/fixtures && python3 -m http.server 9361 --bind 127.0.0.1 &)
fi
```

- [ ] **Step 4: Infrastructure-smoke pilot** (excluded from data — R2-F1): first `mkdir -p "$EXPERIMENT_DIR/runs/pilot"` (Step 3's loop creates only the 111 matrix cells), then ONE agent, ONE model (haiku), task = "launch an ephemeral lane per the SKILL.md delegation section, navigate to http://localhost:9361/drive-page.html, run console --gate in the capture form into $EXPERIMENT_DIR/runs/pilot/, tear down". Verifies: Workflow agents have Bash, the lane comes up, logs land. Any failure here is infra — fix infra, never task content.

- [ ] **Step 5: The 111-run matrix via Workflow.** Key design points (peer-review round, CC-agent lenses):
  - **The agent never reads the manifest JSON** — the orchestrator loads it once and passes it via `args.manifests`; the script renders each task's commands INTO the prompt as ready-to-paste capture lines (full cdp.py invocation, `{URL}` pre-substituted). This closes the `expected_classification`/`expected_markers` leak (the agent can't crib the oracle), kills the jq step a weak model would fumble, and removes every JS-vs-bash interpolation ambiguity.
  - **fix-verify iterations live in `iter-K/` subdirectories** — the grader counts iterations from the FILESYSTEM (`iter-1/, iter-2/, …`), not the agent's self-report, and grades the highest-K group. This is the independent evidence the breaker calibration needs.
  - Launch args: read the manifests before invoking: `MANIFESTS=$(cat skills/drive/data/calibration-manifests.json)`, pass as a JSON value.

```js
export const meta = {
  name: 'sp4-calibration',
  description: 'SP4 model-routing calibration: 11 tasks × {haiku,sonnet,opus} × repeats',
  phases: [{ title: 'Matrix' }],
}
// args = { experimentDir: "/tmp/sp4-calibration-YYYYMMDD", fixtureBase: "http://localhost:9361",
//          plugin: "/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer", freezeSha: "<sha>",
//          manifests: {…} }   // the parsed calibration-manifests.json — passed as a JSON value, NOT a string
const MODELS = ["haiku", "sonnet", "opus"]
const VERIFY = ["T1","T2","T3","T4","T5","T6","T7","T8","T9"]   // 3 repeats
const FIX    = ["T10a","T10b"]                                   // 5 repeats (breaker stat)
const RESULT = {
  type: "object",
  required: ["task_id","model","run_dir","verdict_lines","self_success","classification","iterations","wall_s","notes"],
  properties: {
    task_id: {type:"string"}, model: {type:"string"}, run_dir: {type:"string"},
    verdict_lines: {type:"array", items:{type:"string"}},
    self_success: {type:"boolean"},
    classification: {type:"string", enum:["pass","page-error","flaky","not-actionable","absent","other"]},
    iterations: {type:"integer"}, wall_s: {type:"number"}, notes: {type:"string"},
  },
}
const cells = []
for (const m of MODELS) {
  for (const t of VERIFY) for (let r = 1; r <= 3; r++) cells.push({t, m, r})
  for (const t of FIX)    for (let r = 1; r <= 5; r++) cells.push({t, m, r})
}
log(`matrix: ${cells.length} runs`)   // 81 verify + 30 fix → 111 total
function chunk(a, n){ const o=[]; for(let i=0;i<a.length;i+=n) o.push(a.slice(i,i+n)); return o; }

// Render a task's manifest commands as ready-to-paste capture lines: the agent
// copies these EXACTLY — full invocation, zero placeholders ({URL} resolved here;
// {COPY_URL} rendered as the bash var "$COPY_URL", which the fix-verify guidance
// tells the agent to set before pasting). The oracle fields
// (expected_markers/exits/classification) are deliberately NOT included.
const captureLines = (c) => {
  const task = args.manifests.tasks.find(t => t.id === c.t)
  const dirPrefix = task.class === "fix-verify" ? '"$RUN_DIR/iter-$K"' : '"$RUN_DIR"'
  return task.commands.map((cmd, i) => {
    // {URL} resolved here (orchestrator-known); {COPY_URL} becomes the bash var
    // $COPY_URL that the fix-verify guidance tells the agent to set — either way
    // the agent pastes these lines WITHOUT editing them.
    const concrete = cmd.split("{URL}").join(args.fixtureBase).split("{COPY_URL}").join('"$COPY_URL"')
    const nn = String(i + 1).padStart(2, "0")
    return `{ CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" python3 "${args.plugin}/skills/look/scripts/cdp.py" ${concrete}; echo "EXIT=$?"; } > ${dirPrefix}/cmd-${nn}.log 2>&1`
  }).join("\n")
}

const fixGuidance = (c) => args.manifests.tasks.find(t => t.id === c.t).class !== "fix-verify" ? "" : `
FIX-VERIFY PROTOCOL (this is a broken-fixture task):
- WORK=$(mktemp -d); cp "${args.plugin}/tests/fixtures/${args.manifests.tasks.find(t => t.id === c.t).fixture}" "$WORK/copy.html"
- COPY_URL="file://$WORK/copy.html" — set this var; the capture lines above already
  reference "$COPY_URL" (ABSOLUTE file:// URL, no web server needed — navigate accepts file://).
- Each fix-verify CYCLE gets its own subdirectory: mkdir -p "$RUN_DIR/iter-$K" (K=1,2,3) and run ALL the capture lines above with iter-$K in their paths (that is what the $K in the paths is for). Edit ONLY your copy between cycles.
- STOP after 3 cycles without green (circuit-breaker) and report honestly.
- When done (green OR breaker): cp "$WORK/copy.html" "$RUN_DIR/fixed-copy.html" — the orchestrator re-runs the verify commands against it (integrity); a missing copy grades 0.`

const prompt = (c) => `You are a /bulldozer:drive calibration runner (mode: autonomous, subagent).
Task ${c.t}, repeat ${c.r}. Plugin root: ${args.plugin}.

RUN_DIR (pre-created — every log goes under it; bare filenames land in your cwd and grade 0):
export RUN_DIR="${args.experimentDir}/runs/${c.t}-${c.m}-${c.r}"

1. Launch your own ephemeral lane EXACTLY per the runnable block in
   ${args.plugin}/skills/drive/SKILL.md → "Subagent delegation" (env -u strip, CDP_PORT=0,
   bind PORT/LANE_PROFILE/LANE_KILL_MATCH/LANE_BROWSER_BIN, binary-identity pre-flight).
   Write the pre-flight curl capture to "$RUN_DIR/cmd-00.log" as that block shows.
2. Execute these capture lines IN ORDER, each pasted EXACTLY as ONE direct Bash command
   (do NOT wrap them in bash -c or quotes — the inner quoting is already correct):
${captureLines(c)}
   After each line, cat the log it wrote so you see the outcome.${fixGuidance(c)}
3. Measure wall time around step 2: WALL_START=$SECONDS before, and after:
   echo $((SECONDS - WALL_START)) > "$RUN_DIR/wall_s.txt" (also report it as wall_s).
4. Tear down via pkill -f -- "$LANE_KILL_MATCH" (the variable you bound in step 1 —
   never a hand-typed pattern), then capture the port-free evidence:
   { sleep 1; curl -s -m1 "http://localhost:$PORT/json/version" >/dev/null 2>&1 \\
   && echo PORT_STILL_ALIVE || echo PORT_FREE; echo "EXIT=0"; } > "$RUN_DIR/cmd-99.log" 2>&1
5. Return the structured result:
   - iterations: 1 for a single pass; for fix-verify = the number of iter-K cycles you ran.
   - classification — YOUR honest diagnosis of what the page/run showed:
     pass = everything behaved; page-error = the page itself threw/errored;
     flaky = the target exists but flaps (ASSERT_FAIL with "flapped");
     not-actionable = the target cannot be clicked as a user (occluded/disabled);
     absent = the target does not exist at all; other = anything else.
   - self_success = your own verdict on whether the task's intent was met.
   - verdict_lines = the marker lines you observed (a copy — the logs are the ground truth).`
const results = []
for (const batch of chunk(cells, 4)) {                       // throttle (529 doctrine)
  results.push(...await parallel(batch.map(c => () =>
    agent(prompt(c), { model: c.m, phase: 'Matrix', schema: RESULT,
                       label: `${c.t}-${c.m}-${c.r}` }))))
}
for (let i = 0; i < results.length; i++)                     // sequential null-retry
  if (!results[i]) results[i] = await agent(prompt(cells[i]),
    { model: cells[i].m, phase: 'Matrix', schema: RESULT, label: `retry-${cells[i].t}-${cells[i].m}-${cells[i].r}` })
const nulls = results.filter(r => !r).length
if (nulls) log(`WARNING: ${nulls} cells still null after retry — they grade 0; analysis must null-guard`)
return { freezeSha: args.freezeSha, results }
```

- [ ] **Step 6: Grade.** Two passes:
  1. **T10 integrity re-runs (orchestrator, spec §3.1 anti-gaming):** the Workflow prompt requires fix-verify agents to leave their fixed copy at `$run_dir/fixed-copy.html`. For each T10 cell with a copy present — concrete per-cell block (one ephemeral lane each, torn down between cells; `{COPY_URL}` = the file:// URL of THAT cell's copy):
     ```bash
     CELL="$EXPERIMENT_DIR/runs/T10a-haiku-1"          # per cell
     [ -f "$CELL/fixed-copy.html" ] || { echo "integrity=fail (no copy)"; continue; }
     out=$(env -u LOOK_PROFILE_DIR -u LOOK_INSECURE -u LOOK_DRY_RUN -u CHROME_BIN \
               -u LOOK_AUTOMATION -u CHROME_APP_NAME -u LOOK_CERT_SPKI \
               CDP_PORT=0 LOOK_HEADLESS=1 \
               /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/skills/look/scripts/launch.sh --automation)
     PORT=$(printf '%s\n' "$out" | sed -n 's/^CDP_PORT=//p' | tail -1)
     KILL=$(printf '%s\n' "$out" | sed -n 's/^LANE_KILL_MATCH=//p' | tail -1)
     COPY_URL="file://$CELL/fixed-copy.html"
     # run the manifest's verify commands verbatim with {COPY_URL} substituted; e.g. T10a:
     CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" python3 .../cdp.py navigate "$COPY_URL" --wait load
     CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" python3 .../cdp.py assert '#target-btn' --actionable --stable 300 --timeout 5
     CDP_PORT=$PORT CHROME_APP_NAME="Google Chrome for Testing" python3 .../cdp.py click '#target-btn' --require-trusted
     pkill -f -- "$KILL"
     # integrity=pass iff every verify command printed its expected marker and exited per expected_exits
     ```
  2. **Grade every cell:** `python3 skills/drive/scripts/grade_run.py --run-dir $EXPERIMENT_DIR/runs/<cell> --task <T> --classification <agent's reported> [--integrity pass|fail]` (the flag only for T10 cells) → collect JSON. The grader fails T10 cells without an integrity verdict by design (`integrity-missing`).

- [ ] **Step 7: Analysis doc + PR2.** Write `docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md`: freeze SHA, per-task×model graded table, honesty-delta table (now THREE-way: `self_success` vs `graded_success` vs self-reported `iterations` vs `iterations_observed`), breaker verdict per the predeclared rule — **the breaker statistic uses the grader's filesystem-counted `iterations_observed` exclusively** (censored = fix-verify cell with iter-3 present and red; second pass at breaker=5 only if censored ≥ 1), routing rules derivation. Null cells from the Workflow result (skipped/never-returned) grade 0 and are listed separately. `wall_s` is agent-approximate (`$SECONDS` resolution, cross-checked against `wall_s.txt`) — usable for coarse model-speed comparison only, flagged as such in the doc. Then: routing table into `skills/drive/SKILL.md` (the §3.3 shape with measured cutoffs), umbrella §5 SP4 row → ✅ DONE, CLAUDE.md changelog note. PR2 → review → merge. Kill the fixture server (`pkill -f "http.server 9361"`).

---

## Self-review (done at write time)

- **Spec coverage:** §2.1 → Tasks 1/2/4; §2.2 → Task 7; §2.3 → Tasks 1/2/3; §3.1 fixtures/oracles/manifests → Tasks 5/6; §3.2 mechanics/grading/freeze/breaker → Tasks 6/9; §3.3 routing → Task 9 Step 7; ship list rows all mapped (umbrella DONE + analysis doc = PR2).
- **Placeholder scan:** clean — every code step carries the actual code; the only deliberately-open values are the experiment's outputs (routing cutoffs, breaker verdict), which the spec mandates be measured, not pre-written.
- **Type consistency:** contract keys (`CDP_PORT/LANE_PROFILE/LANE_KILL_MATCH/LANE_BROWSER_BIN`) match across Tasks 3/4/7/9; `grade()` signature matches between Task 6 tests and implementation; marker strings match cdp.py literals everywhere; `EPHEMERAL` flag named identically in Tasks 1/2/4.
