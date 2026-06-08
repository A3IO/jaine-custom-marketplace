"""SP4 ephemeral-lanes e2e — the hole-H regression suite.

Two ephemeral lanes launched in parallel must get distinct OS-assigned ports and
unique mktemp profiles; tearing one down by its LANE_KILL_MATCH must leave the
other alive. Self-skips when the pinned CfT is not installed (same policy as
conftest.cft_browser).
"""
import os
import re
import subprocess

import pytest

# Reuse the canonical conftest probes (PR #178 review: a private copy of the
# /json/version probe or the post-kill wait would silently drift from the shared
# rationale — headless=new keeps serving CDP seconds after SIGTERM).
from conftest import CFT_BIN, LANE_ENV_VARS, _cdp_is_online, _wait_port_release

LAUNCH_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "skills", "look", "scripts", "launch.sh")

pytestmark = pytest.mark.skipif(
    not (os.path.exists(CFT_BIN) and os.access(CFT_BIN, os.X_OK)),
    reason="Chrome for Testing not installed — run skills/look/scripts/update-cft.sh",
)

CONTRACT_KEYS = ("CDP_PORT", "LANE_PROFILE", "LANE_KILL_MATCH", "LANE_BROWSER_BIN")


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


def _kill_spawned(out):
    """Failure-path hygiene: a launch that came up but produced no usable contract
    still started a real headless Chrome (detached from the launcher by `&`) —
    kill it by the PID the launcher prints ("JAINE Browser started (PID N, …)")
    so failing asserts/timeouts don't leak browsers (PR #178 review)."""
    m = re.search(r"started \(PID (\d+)", out or "")
    if m:
        subprocess.run(["kill", m.group(1)], capture_output=True)


def _abort_proc(proc):
    """Kill a still-pending launcher AND the Chrome it may have detached: drain
    whatever stdout exists after the kill and PID-kill from it."""
    proc.kill()
    try:
        out, _ = proc.communicate(timeout=5)
    except Exception:
        out = ""
    _kill_spawned(out)


def _collect_contract(proc, timeout=25):
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # communicate() does NOT kill the child on timeout — and the detached
        # Chrome outlives the launcher anyway (PR #178 review).
        _abort_proc(proc)
        raise
    if proc.returncode != 0:
        _kill_spawned(out)
        raise AssertionError("launch failed rc={}\nstdout:\n{}\nstderr:\n{}".format(proc.returncode, out, err))
    contract = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k in CONTRACT_KEYS:
            contract[k] = v
    missing = [k for k in CONTRACT_KEYS if k not in contract]
    if missing:
        _kill_spawned(out)
        raise AssertionError("contract lines missing {} in stdout:\n{}".format(missing, out))
    return contract


def _launch_ephemeral(timeout=25):
    return _collect_contract(_spawn_ephemeral(), timeout=timeout)


def _teardown(contract):
    subprocess.run(["pkill", "-f", "--", contract["LANE_KILL_MATCH"]], capture_output=True)
    _wait_port_release(contract["CDP_PORT"])


class TestEphemeralLanesE2E:
    def test_two_parallel_lanes_distinct_and_isolated(self):
        # genuinely parallel: BOTH launcher processes start before either is awaited —
        # this is the racy window holes R1-H/R2-R were about (spec §2.3 "in parallel")
        pa, pb = _spawn_ephemeral(), _spawn_ephemeral()
        try:
            a = _collect_contract(pa)
        except Exception:
            _abort_proc(pb)   # not pb.kill(): the detached Chrome outlives the launcher
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
            assert _cdp_is_online(a["CDP_PORT"]) and _cdp_is_online(b["CDP_PORT"])
            # hole-H regression: killing A by ITS kill match leaves B alive
            _teardown(a)
            assert not _cdp_is_online(a["CDP_PORT"])
            assert _cdp_is_online(b["CDP_PORT"]), "teardown of lane A killed lane B — hole H regressed"
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
