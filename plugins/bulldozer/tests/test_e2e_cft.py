#!/usr/bin/env python3
"""E2E tests for the Chrome-for-Testing automation lane (SP1, #164).

Self-contained: cft_browser launches an isolated headless CfT lane on
DRIVE_TEST_PORT (temp profile, --enable-automation, --use-mock-keychain) and
tears it down. The whole file skips when CfT is not installed
(run skills/look/scripts/update-cft.sh).
"""
import json
import os
import subprocess
import sys
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(__file__))
from conftest import CFT_BIN, run_cdp_on_lane as _run_cdp_on  # noqa: E402


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
    """The browser process (not a helper) carries the SP1 automation flags.

    Filter by the CfT binary path prefix, NOT by a substring grep — a substring
    matches the test runner's own shell whose cmdline mentions the port (observed:
    the zsh wrapper carrying this very test command shadowed lane[0])."""
    ps = subprocess.run(["ps", "-axo", "command"], capture_output=True,
                        text=True, timeout=10)
    lane = [l for l in ps.stdout.splitlines()
            if l.startswith(CFT_BIN)
            and "remote-debugging-port={}".format(cft_browser) in l
            and "--type=" not in l]
    assert lane, "CfT browser process not found in ps output"
    assert "--enable-automation" in lane[0]
    assert "--use-mock-keychain" in lane[0]
    assert "Google Chrome for Testing" in lane[0]
