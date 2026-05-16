#!/usr/bin/env python3
"""E2E tests for cdp.py — real browser, real commands.

Requires JAINE Browser (auto-launched by jaine_browser fixture).
Run: pytest tests/test_e2e.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_cdp  # noqa: E402


# ── Status & Tabs ──


def test_status_shows_online(jaine_browser):
    r = run_cdp(["status"])
    assert r.returncode == 0, "status failed: {}".format(r.stderr)
    assert "ONLINE" in r.stdout


def test_tabs_lists_test_page(test_page_url):
    r = run_cdp(["tabs"])
    assert r.returncode == 0 or r.stdout, "tabs returned nothing"
    assert "localhost" in r.stdout


# ── Navigation ──


def test_navigate_to_server_root(jaine_browser, test_server):
    url = "http://localhost:{}/".format(test_server)
    r = run_cdp(["navigate", url])
    assert r.returncode == 0, "navigate failed: {}".format(r.stderr)
    assert "Navigated" in r.stdout


def test_open_creates_new_tab(jaine_browser, test_server):
    r_before = run_cdp(["tabs"])
    before_count = r_before.stdout.strip().count("\n") + 1 if r_before.stdout.strip() else 0

    url = "http://localhost:{}/test-page.html".format(test_server)
    r = run_cdp(["open", url])
    assert r.returncode == 0, "open failed: {}".format(r.stderr)
    assert "Opened" in r.stdout

    r_after = run_cdp(["tabs"])
    after_count = r_after.stdout.strip().count("\n") + 1 if r_after.stdout.strip() else 0
    assert after_count > before_count, "Tab count did not increase"


def test_reload_succeeds(test_page_url):
    r = run_cdp(["reload"])
    assert r.returncode == 0, "reload failed: {}".format(r.stderr)
    assert "Reloaded" in r.stdout


# ── See ──


def test_screenshot_creates_file(test_page_url, tmp_path):
    path = str(tmp_path / "shot.jpg")
    r = run_cdp(["screenshot", path])
    assert r.returncode == 0, "screenshot failed: {}".format(r.stderr)
    assert os.path.exists(path), "Screenshot file not created"
    size = os.path.getsize(path)
    assert size > 5_000, "Screenshot too small ({}B), likely empty".format(size)
    with open(path, "rb") as f:
        header = f.read(3)
    assert header == b'\xff\xd8\xff', "Not a valid JPEG file (header={!r})".format(header)


def test_title_returns_page_title(test_page_url):
    r = run_cdp(["title"])
    assert r.returncode == 0, "title failed: {}".format(r.stderr)
    assert "JAINE Test Page" in r.stdout


def test_html_returns_content(test_page_url):
    r = run_cdp(["html"])
    assert r.returncode == 0, "html failed: {}".format(r.stderr)
    assert "JAINE Test Page" in r.stdout
    assert "<html" in r.stdout


# ── Execute ──


def test_js_returns_value(test_page_url):
    r = run_cdp(["js", "2+2"])
    assert r.returncode == 0, "js failed: {}".format(r.stderr)
    assert "4" in r.stdout


def test_js_reads_dom(test_page_url):
    r = run_cdp(["js", "document.title"])
    assert r.returncode == 0, "js failed: {}".format(r.stderr)
    assert "JAINE Test Page" in r.stdout


def test_click_triggers_handler(test_page_url):
    r = run_cdp(["click", "#test-btn"])
    assert r.returncode == 0, "click failed: {}".format(r.stderr)
    assert "clicked" in r.stdout.lower()

    r2 = run_cdp(["js", "document.getElementById('test-btn').dataset.clicked"])
    assert "true" in r2.stdout


def test_fill_sets_value(test_page_url):
    r = run_cdp(["fill", "#test-input", "hello e2e"])
    assert r.returncode == 0, "fill failed: {}".format(r.stderr)
    assert "filled" in r.stdout.lower()

    r2 = run_cdp(["js", "document.getElementById('test-input').value"])
    assert "hello e2e" in r2.stdout


def test_fill_dispatches_events(test_page_url):
    r = run_cdp(["fill", "#test-input", "event test"])
    assert r.returncode == 0

    r2 = run_cdp(["js", "document.getElementById('test-input').dataset.inputFired"])
    assert "true" in r2.stdout


def test_wait_finds_existing(test_page_url):
    r = run_cdp(["wait", "#test-btn", "5"])
    assert r.returncode == 0, "wait failed: {}".format(r.stderr)
    assert "Found" in r.stdout


def test_wait_timeout_missing(test_page_url):
    r = run_cdp(["wait", "#nonexistent-element", "2"])
    assert r.returncode != 0, "wait should fail for missing element"
    assert "Timeout" in r.stderr or "not found" in r.stderr


# ── Debug ──


def test_console_captures_heartbeat(test_page_url):
    r = run_cdp(["console"])
    assert r.returncode == 0, "console failed: {}".format(r.stderr)
    assert "CONSOLE_HEARTBEAT" in r.stdout, (
        "Console did not capture heartbeat. Output: {}".format(r.stdout)
    )


def test_network_captures_requests(test_page_url):
    r = run_cdp(["network"])
    assert r.returncode == 0, "network failed: {}".format(r.stderr)
    assert r.stdout.strip(), "No network output"
    assert "200" in r.stdout or "localhost" in r.stdout


# ── Generate ──


def test_pdf_creates_file(test_page_url, tmp_path):
    path = str(tmp_path / "page.pdf")
    r = run_cdp(["pdf", path])
    assert r.returncode == 0, "pdf failed: {}".format(r.stderr)
    assert os.path.exists(path), "PDF file not created"
    with open(path, "rb") as f:
        header = f.read(5)
    assert header == b'%PDF-', "Not a valid PDF: header={}".format(header)


def test_viewport_changes_size(test_page_url):
    r = run_cdp(["viewport", "375", "812"])
    assert r.returncode == 0, "viewport failed: {}".format(r.stderr)
    assert "375" in r.stdout and "812" in r.stdout

    r2 = run_cdp(["js", "window.innerWidth"])
    assert r2.returncode == 0
    width = r2.stdout.strip()
    assert width == "375", "innerWidth should be 375, got {}".format(width)

    run_cdp(["viewport", "1440", "900"])


# ── Window ──


def test_window_bounds_returns_coords(jaine_browser):
    r = run_cdp(["window", "bounds"])
    assert r.returncode == 0, "window bounds failed: {}".format(r.stderr)
    assert "," in r.stdout, "Expected comma-separated bounds, got: {}".format(r.stdout)


# ── Issue #55: --clip, --scale, dimensions in stdout ──

# Import the production helper from cdp.py so tests stay in sync with the
# source-of-truth parser (no drift if _image_dimensions gains bugfixes).
def _load_image_dimensions():
    import importlib.util
    from conftest import CDP_SCRIPT  # noqa: E402
    spec = importlib.util.spec_from_file_location("_cdp_under_test", CDP_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._image_dimensions


_read_jpeg_dimensions = _load_image_dimensions()


def test_screenshot_clip_captures_region(test_page_url, tmp_path):
    """--clip X Y W H captures the requested CSS-pixel region at native DPR (issue #55).

    Default --clip produces native-DPR output (Retina 2× preserves UI detail —
    the skill's primary use case is verifying fine UI details). Output
    dimensions = CSS-pixel size × devicePixelRatio.
    """
    run_cdp(["viewport", "1440", "900"])
    dpr_r = run_cdp(["js", "window.devicePixelRatio"])
    try:
        dpr = float(dpr_r.stdout.strip())
    except ValueError:
        dpr = 1.0

    path = str(tmp_path / "clip.jpg")
    r = run_cdp(["screenshot", path, "--clip", "100", "100", "200", "150"])
    assert r.returncode == 0, "screenshot --clip failed: {}".format(r.stderr)
    dims = _read_jpeg_dimensions(path)
    assert dims is not None, "Could not parse JPEG dimensions from {}".format(path)
    w, h = dims
    expected_w = int(round(200 * dpr))
    expected_h = int(round(150 * dpr))
    assert w == expected_w and h == expected_h, (
        "Clip dimensions wrong: got {}×{}, expected {}×{} (200×150 CSS × DPR={})".format(
            w, h, expected_w, expected_h, dpr
        )
    )


def test_screenshot_prints_dimensions_to_stdout(test_page_url, tmp_path):
    """screenshot prints dimensions in the canonical 'PATH  W×H' format (issue #55).

    Tight format guard: the path is followed by two spaces and W×H using the
    U+00D7 multiplication sign. Agents and downstream parsers can rely on it.
    """
    path = str(tmp_path / "dim.jpg")
    r = run_cdp(["screenshot", path])
    assert r.returncode == 0, "screenshot failed: {}".format(r.stderr)
    import re
    pattern = re.escape(path) + r"\s\s+\d+×\d+"
    assert re.search(pattern, r.stdout), (
        "screenshot stdout should match '{}  W×H' (two spaces, U+00D7). Got: {!r}".format(path, r.stdout)
    )


def test_screenshot_scale_one_produces_css_pixel_output(test_page_url, tmp_path):
    """--scale 1 produces 1:1 (CSS-pixel) screenshot via clip.scale = 1 / devicePixelRatio (issue #55).

    Starts from a known 1440×900 viewport. --scale 1 is the opt-in path for
    explicit 1:1 output — width must not exceed ~1500 px regardless of native DPR
    (on Retina the 2× capture is divided by setting clip.scale = 0.5).
    """
    run_cdp(["viewport", "1440", "900"])
    path = str(tmp_path / "scale1.jpg")
    r = run_cdp(["screenshot", path, "--scale", "1"])
    assert r.returncode == 0, "screenshot --scale 1 failed: {}".format(r.stderr)
    dims = _read_jpeg_dimensions(path)
    assert dims is not None, "Could not parse JPEG dimensions"
    w, _ = dims
    assert w <= 1500, (
        "Width {} suggests DPR > 1; --scale 1 should produce ~1440px-wide image".format(w)
    )


def test_screenshot_clip_and_scale_combined(test_page_url, tmp_path):
    """--clip + --scale 1 produces region capture at CSS-pixel resolution (issue #55).

    Regression guard for the combined-path: --scale must override the clip.scale=1
    that --clip pre-fills. Without this, a refactor that loses the override would
    silently produce DPR-scaled clip output (e.g. 400×300 instead of 200×150 on Retina).
    """
    run_cdp(["viewport", "1440", "900"])
    path = str(tmp_path / "combo.jpg")
    r = run_cdp(["screenshot", path, "--clip", "0", "0", "200", "150", "--scale", "1"])
    assert r.returncode == 0, "screenshot --clip + --scale failed: {}".format(r.stderr)
    dims = _read_jpeg_dimensions(path)
    assert dims == (200, 150), (
        "Combined --clip + --scale 1 must produce CSS-pixel output (200×150), got {}".format(dims)
    )
