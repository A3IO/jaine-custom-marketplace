"""Shared fixtures for bulldozer tests.

E2E tests need a running JAINE Browser. The `jaine_browser` fixture
reuses an already-running browser or launches one via launch.sh.

The `slow` marker (used by `tests/test_check_e2e.py`) is registered here so
running `pytest` without `-m slow` doesn't print PytestUnknownMarkWarning.
Slow tests are not deselected by default — register a default filter via
`-m "not slow"` if you want fast runs only.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def pytest_configure(config):
    """Register custom markers used across the bulldozer test suite."""
    config.addinivalue_line(
        "markers",
        "slow: tests that take >10s — typically because they invoke a real "
        "external service (codex, JAINE Browser, network). Run with `-m slow` "
        "to include explicitly.",
    )

import pytest

PLUGIN_ROOT = Path(__file__).parent.parent
CDP_SCRIPT = str(PLUGIN_ROOT / "skills" / "look" / "scripts" / "cdp.py")
LAUNCH_SCRIPT = str(PLUGIN_ROOT / "skills" / "look" / "scripts" / "launch.sh")
FIXTURES_DIR = str(Path(__file__).parent / "fixtures")
# The e2e default is itself an isolated lane: a bare `pytest` (no CDP_PORT) drives
# a dedicated NON-9333 headless test browser, never the user's daily 9333 browser.
# Driving the daily browser is explicit opt-in: CDP_PORT=9333 pytest …
TEST_CDP_PORT = 9355
CDP_PORT = int(os.environ.get("CDP_PORT", str(TEST_CDP_PORT)))
LANE_IS_HEADLESS = CDP_PORT != 9333
# Shared Chrome-binary reference (A.7): launch.sh's CHROME_BIN default must match.
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# SP1 (#164): pinned Chrome for Testing — the automation-lane binary. The /drive
# e2e fixture gets its OWN CfT lane (R1-A), never piggybacking the stock baseline.
CFT_BIN = ("/0/.jaine/.browser/cft/current/chrome-mac-arm64/"
           "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
CFT_APP_NAME = "Google Chrome for Testing"
# ── e2e port registry (R1-F2: keep lanes distinct; grep this block before adding one) ──
# 9355  TEST_CDP_PORT       — stock-Chrome baseline lane (this file)
# 9356  INSECURE_TEST_PORT  — insecure lane (tests/test_e2e.py; falls back to 9358)
# 9359  DRIVE_TEST_PORT     — CfT automation lane (tests/test_e2e_cft.py, test_e2e_drive.py)
# 9340-9349                 — interactive /drive lanes (skills/drive/SKILL.md)
# 9360+                     — transient empirical probes (SP1/SP2 analysis docs name each lane's config)
# 9361                      — SP4 calibration fixture server (transient; experiment only)
# 9362                      — cookie-seed e2e seed-target (tests/test_e2e_drive.py, transient)
DRIVE_TEST_PORT = 9359

# Lane env vars the harness must NOT inherit from the dev's shell, so fixtures stay
# hermetic — a stray LOOK_DRY_RUN/LOOK_HEADLESS/LOOK_INSECURE/LOOK_PROFILE_DIR would
# otherwise bleed into launch.sh. Shared by jaine_browser + test_launch.py._run_launch.
LANE_ENV_VARS = ("CDP_PORT", "LOOK_PROFILE_DIR", "LOOK_HEADLESS", "LOOK_INSECURE",
                 "LOOK_DRY_RUN", "CHROME_BIN", "LOOK_AUTOMATION", "CHROME_APP_NAME",
                 "LOOK_CERT_SPKI")


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


def run_cdp(args, env_override=None, timeout=15):
    env = os.environ.copy()
    env["CDP_PORT"] = str(CDP_PORT)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, CDP_SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def run_cdp_on_lane(port, args, timeout=15):
    """Lane-contract wrapper (R1-F3): BOTH env keys on every cdp.py call against
    a CfT lane — launch.sh's automation defaults do NOT propagate to separate
    cdp.py processes. Single enforcement point shared by test_e2e_cft.py and
    test_e2e_drive.py (two private byte-identical copies had already drifted
    on their default timeout)."""
    return run_cdp(args,
                   env_override={"CDP_PORT": str(port),
                                 "CHROME_APP_NAME": CFT_APP_NAME},
                   timeout=timeout)


@contextmanager
def transient_cft_lane(port, start_timeout=20):
    """Launch a short-lived CfT automation lane on `port`, yield the port, then
    kill the lane and wait for the port to actually release. Extracted from the
    inline block in TestCookieSeed (same lifecycle as cft_browser, minus the
    session scope/skip logic). Fail-loud on a pre-existing listener."""
    if _cdp_is_online(port):
        raise RuntimeError(
            "port {} unexpectedly occupied — see the e2e port registry".format(port))
    env = os.environ.copy()
    for v in LANE_ENV_VARS:
        env.pop(v, None)
    profile = tempfile.mkdtemp(prefix="jaine-lane-{}-".format(port))
    env.update({"CDP_PORT": str(port), "LOOK_PROFILE_DIR": profile,
                "LOOK_HEADLESS": "1", "LOOK_AUTOMATION": "1",
                "CHROME_APP_NAME": CFT_APP_NAME})
    kill_match = _kill_pattern(profile)
    subprocess.Popen(["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + start_timeout
        while time.time() < deadline and not _cdp_is_online(port):
            time.sleep(0.5)
        if not _cdp_is_online(port):
            raise RuntimeError("transient CfT lane did not start on port {} "
                               "within {}s".format(port, start_timeout))
        yield port
    finally:
        subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
        _wait_port_release(port)
        shutil.rmtree(profile, ignore_errors=True)


def _cdp_is_online(port=CDP_PORT):
    try:
        r = urlopen("http://localhost:{}/json/version".format(port), timeout=3)
        return r.status == 200
    except (URLError, OSError):
        return False


def _wait_port_release(port, timeout=10):
    """Wait until nothing serves CDP on the port (condition-based, no blind sleep).

    headless=new Chrome keeps serving CDP for a few seconds after SIGTERM
    (live-observed in SP1); a teardown that returns before the port is actually
    free makes the NEXT launch/fixture trip its own fail-loud
    pre-existing-listener guard on a back-to-back run. Shared by jaine_browser,
    cft_browser and test_e2e.py's insecure_lane."""
    deadline = time.time() + timeout
    while time.time() < deadline and _cdp_is_online(port):
        time.sleep(0.5)


BROWSER_PROFILE = "/0/.jaine/.browser/profile"


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

    # Strip lane vars bleeding from the dev's shell so the fixture is hermetic: a shell
    # LOOK_DRY_RUN=1 would make launch.sh dry-run + never start (misleading 20s timeout);
    # LOOK_HEADLESS=1 would launch the 9333 daily browser headless; LOOK_INSECURE=1 fails
    # launch.sh loud. Mirrors test_launch.py's _run_launch via the shared LANE_ENV_VARS.
    env = os.environ.copy()
    for _v in LANE_ENV_VARS:
        env.pop(_v, None)
    env["CDP_PORT"] = str(CDP_PORT)
    temp_profile = None
    if CDP_PORT == 9333:
        kill_match = _kill_pattern(BROWSER_PROFILE)
    else:
        temp_profile = tempfile.mkdtemp(prefix="jaine-test-{}-".format(CDP_PORT))
        env["LOOK_PROFILE_DIR"] = temp_profile
        env["LOOK_HEADLESS"] = "1"
        kill_match = _kill_pattern(temp_profile)
    # DEVNULL, not PIPE: launch.sh redirects Chrome itself into the lane's
    # chrome.log; an unread PIPE could fill (64KB) and block the child.
    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
    _wait_port_release(CDP_PORT)
    if temp_profile:
        shutil.rmtree(temp_profile, ignore_errors=True)


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
    if not (os.path.exists(CFT_BIN) and os.access(CFT_BIN, os.X_OK)):
        pytest.skip("Chrome for Testing not installed (or not executable) — run skills/look/scripts/update-cft.sh")
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
    # DEVNULL, not PIPE: launch.sh redirects Chrome itself into the lane's
    # chrome.log; an unread PIPE could fill (64KB) and block the child.
    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_is_online(DRIVE_TEST_PORT):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
        _wait_port_release(DRIVE_TEST_PORT)
        shutil.rmtree(temp_profile, ignore_errors=True)
        pytest.fail("CfT browser did not start on port {} within 20s".format(DRIVE_TEST_PORT))

    yield DRIVE_TEST_PORT

    subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
    _wait_port_release(DRIVE_TEST_PORT)
    shutil.rmtree(temp_profile, ignore_errors=True)


@pytest.fixture(scope="session")
def test_server():
    """Serve tests/fixtures/ on a random port."""
    handler = partial(SimpleHTTPRequestHandler, directory=FIXTURES_DIR)
    # ThreadingHTTPServer, NOT the single-threaded HTTPServer: Chrome opens speculative
    # preconnect sockets (TCP with no request); a single-threaded server blocks reading a
    # request from such a socket and every later connection hangs in the backlog — the D
    # e2e fetch then times out at PENDING after a long session (latent until sub-D).
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield port

    server.shutdown()


@pytest.fixture
def test_page_url(jaine_browser, test_server):
    """Navigate to test page and return its URL."""
    url = "http://localhost:{}/test-page.html".format(test_server)
    r = run_cdp(["navigate", url])
    assert r.returncode == 0, "Failed to navigate to test page: {}".format(r.stderr)
    time.sleep(0.5)
    return url
