"""Shared fixtures for bulldozer tests.

E2E tests need a running JAINE Browser. The `jaine_browser` fixture
reuses an already-running browser or launches one via launch.sh.

The `slow` marker (used by `tests/test_check_e2e.py`) is registered here so
running `pytest` without `-m slow` doesn't print PytestUnknownMarkWarning.
Slow tests are not deselected by default — register a default filter via
`-m "not slow"` if you want fast runs only.
"""
import os
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
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
CDP_PORT = 9333


def run_cdp(args, env_override=None, timeout=15):
    env = os.environ.copy()
    env["CDP_PORT"] = str(CDP_PORT)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [sys.executable, CDP_SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _cdp_is_online():
    try:
        r = urlopen("http://localhost:{}/json/version".format(CDP_PORT), timeout=3)
        return r.status == 200
    except (URLError, OSError):
        return False


BROWSER_PROFILE = "/0/.jaine/.browser/profile"


@pytest.fixture(scope="session")
def jaine_browser():
    """Ensure JAINE Browser is running. Reuse if already online."""
    if _cdp_is_online():
        yield "reused"
        return

    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_is_online():
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", "user-data-dir=" + BROWSER_PROFILE],
                       capture_output=True)
        pytest.fail("JAINE Browser did not start within 20s")

    yield "launched"

    subprocess.run(["pkill", "-f", "user-data-dir=" + BROWSER_PROFILE],
                   capture_output=True)


@pytest.fixture(scope="session")
def test_server():
    """Serve tests/fixtures/ on a random port."""
    handler = partial(SimpleHTTPRequestHandler, directory=FIXTURES_DIR)
    server = HTTPServer(("127.0.0.1", 0), handler)
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
