#!/usr/bin/env python3
"""E2E tests for cdp.py — real browser, real commands.

Requires JAINE Browser (auto-launched by jaine_browser fixture).
Run: pytest tests/test_e2e.py -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402
from conftest import run_cdp, CDP_PORT, FIXTURES_DIR, LAUNCH_SCRIPT, LANE_ENV_VARS, _kill_pattern, _wait_port_release  # noqa: E402


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


def test_navigate_bare_path_normalizes_to_file_uri(jaine_browser, tmp_path):
    """Issue #60 §1: a bare absolute path navigates correctly (normalized to file://).

    Without normalize_url, CDP rejects the bare path with
    'Cannot navigate to invalid URL' and the page stays where it was.
    """
    page = tmp_path / "bd60.html"
    page.write_text("<!DOCTYPE html><html><head><title>BD60 BARE PATH</title></head><body>ok</body></html>")
    bare = str(page)  # no file:// scheme
    assert bare.startswith("/")

    r = run_cdp(["navigate", bare])
    assert r.returncode == 0, "navigate to bare path failed: {}".format(r.stderr)

    # Wait for THIS page's title specifically — a generic `title.length > 0` would
    # fire on the previous page's title (CDP navigate returns before load completes).
    w = run_cdp(["wait", "--js", "document.title === 'BD60 BARE PATH'", "5"])
    assert w.returncode == 0, "page did not finish loading bd60.html in time: {}".format(w.stderr)
    href = run_cdp(["js", "location.href"]).stdout
    title = run_cdp(["js", "document.title"]).stdout
    assert "file://" in href, "bare path should have been normalized to file://, got href={!r}".format(href)
    assert "BD60 BARE PATH" in title, "page did not load (title={!r})".format(title)


def test_navigate_bare_path_with_spaces_and_reserved_chars(jaine_browser, tmp_path):
    """Issue #60 R1-F1 (e2e): a bare path with a space + '#' navigates correctly —
    the percent-encoded file:// URI must round-trip through CDP to the real file."""
    page = tmp_path / "bd60 spaced#page.html"
    page.write_text("<!DOCTYPE html><html><head><title>BD60 SPACED</title></head><body>ok</body></html>")
    bare = str(page)
    assert " " in bare and "#" in bare

    r = run_cdp(["navigate", bare])
    assert r.returncode == 0, "navigate to spaced/# path failed: {}".format(r.stderr)
    w = run_cdp(["wait", "--js", "document.title === 'BD60 SPACED'", "5"])
    assert w.returncode == 0, "spaced/# page did not load: {}".format(w.stderr)
    href = run_cdp(["js", "location.href"]).stdout
    title = run_cdp(["js", "document.title"]).stdout
    assert "%20" in href and "%23" in href, "space + '#' must be percent-encoded in href, got {!r}".format(href)
    assert "BD60 SPACED" in title, "spaced/# page did not load (title={!r})".format(title)


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
    out = r.stdout.strip()
    # B.2 contract: exactly `left,top,width,height` — comma-separated, no spaces.
    assert " " not in out, "contract is left,top,width,height (no spaces), got: {!r}".format(out)
    parts = out.split(",")
    assert len(parts) == 4, "expected 4 fields left,top,width,height, got: {!r}".format(out)
    left, top, width, height = (int(p) for p in parts)
    assert width > 0 and height > 0, "width/height must be positive, got {}x{}".format(width, height)


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


def test_click_trusted_grants_user_activation(test_page_url):
    """Visible button → trusted Input.dispatchMouseEvent → isTrusted + user activation."""
    r = run_cdp(["click", "#trusted-probe"])
    assert r.returncode == 0, "click failed: {}".format(r.stderr)
    assert "trusted" in r.stdout.lower(), "expected trusted marker, got: {}".format(r.stdout)
    assert "fallback" not in r.stdout.lower(), "visible button must not fall back: {}".format(r.stdout)
    t = run_cdp(["js", "String(window.__clickTrusted)"])
    assert "true" in t.stdout, "event.isTrusted should be true, got: {}".format(t.stdout)
    a = run_cdp(["js", "String(window.__userActivation)"])
    assert "true" in a.stdout, "userActivation.isActive should be true, got: {}".format(a.stdout)


def test_click_hidden_falls_back_untrusted(test_page_url):
    """display:none element → not hittable → untrusted el.click() fallback + stderr WARN."""
    r = run_cdp(["click", "#hidden-btn"])
    assert r.returncode == 0, "fallback click must still return 0: {}".format(r.stderr)
    assert "fallback" in r.stdout.lower(), "expected fallback marker, got: {}".format(r.stdout)
    assert "activation" in r.stderr.lower(), "expected user-activation WARN on stderr, got: {}".format(r.stderr)
    h = run_cdp(["js", "String(window.__hiddenClicked)"])
    assert "true" in h.stdout, "hidden handler should fire via el.click fallback: {}".format(h.stdout)


def test_click_occluded_falls_back(test_page_url):
    """Element under a higher-z-index overlay → elementFromPoint miss → fallback."""
    r = run_cdp(["click", "#occluded-btn"])
    assert r.returncode == 0, "occluded click must still return 0: {}".format(r.stderr)
    assert "fallback" in r.stdout.lower(), "occluded element must fall back, got: {}".format(r.stdout)
    o = run_cdp(["js", "String(window.__occludedClicked)"])
    assert "true" in o.stdout, "occluded handler should fire via el.click fallback: {}".format(o.stdout)


def test_click_belowfold_smooth_scroll_stays_trusted(test_page_url):
    """Below-fold button on a scroll-behavior:smooth page must STILL be trusted —
    regression guard that scrollIntoView uses behavior:'instant' (R1-F1)."""
    r = run_cdp(["click", "#belowfold-btn"])
    assert r.returncode == 0, "click failed: {}".format(r.stderr)
    assert "trusted" in r.stdout.lower() and "fallback" not in r.stdout.lower(), \
        "below-fold smooth-scroll button must be trusted (instant scroll), got: {}".format(r.stdout)
    t = run_cdp(["js", "String(window.__belowfoldTrusted)"])
    assert "true" in t.stdout, "below-fold isTrusted should be true, got: {}".format(t.stdout)


# ── sub-C: --target tab pinning ──


def _open_lane(token, test_server):
    """Open test-page.html?<token> in a new tab; return its 12-char id from `tabs`."""
    url = "http://localhost:{}/test-page.html?{}".format(test_server, token)
    r = run_cdp(["open", url])
    assert r.returncode == 0, "open failed: {}".format(r.stderr)
    t = run_cdp(["tabs"])
    for line in t.stdout.splitlines():
        if token in line:
            return line.split()[0]  # id[:12] is the first column
    raise AssertionError("opened tab not in `tabs` for {}: {}".format(token, t.stdout))


def test_target_pins_cdp_js_family(jaine_browser, test_server, tmp_path):
    """--target drives ONLY the selected tab for the cdp_js family, value-distinguished:
    js (location.search), fill (input value), wait (--js predicate), title (per-tab
    document.title), html (per-tab marker), screenshot --full-page (per-tab height).
    Per-tab markers are injected with the js command (proven-pinned first)."""
    base = "http://localhost:{}/test-page.html".format(test_server)
    id_a = _open_lane("look=cjA", test_server)
    id_b = _open_lane("look=cjB", test_server)

    # js (cdp_js) honors the id pin
    assert run_cdp(["--target", id_a, "js", "location.search"]).stdout.strip() == "?look=cjA"
    assert run_cdp(["--target", id_b, "js", "location.search"]).stdout.strip() == "?look=cjB"
    # url-substring selector resolves the same tab (end-to-end)
    assert run_cdp(["--target", "look=cjB", "js", "location.search"]).stdout.strip() == "?look=cjB"

    # fill (cdp_js) drives only the pinned tab
    run_cdp(["--target", id_b, "fill", "#test-input", "valB"])
    assert run_cdp(["--target", id_b, "js",
                    "document.getElementById('test-input').value"]).stdout.strip() == "valB"
    assert run_cdp(["--target", id_a, "js",
                    "document.getElementById('test-input').value"]).stdout.strip() == ""

    # wait (cdp_js) polls the pinned tab
    assert run_cdp(["--target", id_b, "wait", "--js",
                    "location.search === '?look=cjB'", "5"]).returncode == 0

    # Inject per-tab markers via the proven-pinned js (A also gets a 4000px pad).
    run_cdp(["--target", id_a, "js",
             "document.title='TTL_A'; document.body.setAttribute('data-lane','LANE_A');"
             " var d=document.createElement('div'); d.style.height='4000px';"
             " document.body.appendChild(d); 'ok'"])
    run_cdp(["--target", id_b, "js",
             "document.title='TTL_B'; document.body.setAttribute('data-lane','LANE_B'); 'ok'"])

    # title (cdp_js) pins
    assert run_cdp(["--target", id_a, "title"]).stdout.strip() == "TTL_A"
    assert run_cdp(["--target", id_b, "title"]).stdout.strip() == "TTL_B"

    # html (cdp_js) pins — each tab's marker present only in its own DOM
    html_a = run_cdp(["--target", id_a, "html"]).stdout
    html_b = run_cdp(["--target", id_b, "html"]).stdout
    assert "LANE_A" in html_a and "LANE_B" not in html_a
    assert "LANE_B" in html_b and "LANE_A" not in html_b

    # screenshot --full-page (cdp_js metrics) pins — A's 4000px pad ⇒ taller capture
    pa, pb = str(tmp_path / "fpA.jpg"), str(tmp_path / "fpB.jpg")
    run_cdp(["--target", id_a, "screenshot", "--full-page", pa])
    run_cdp(["--target", id_b, "screenshot", "--full-page", pb])
    dim_a, dim_b = _read_jpeg_dimensions(pa), _read_jpeg_dimensions(pb)
    assert dim_a and dim_b, "could not read screenshot dims: {} {}".format(dim_a, dim_b)
    assert dim_a[1] > dim_b[1] + 1000, (
        "A's full-page capture (4000px pad) must be much taller than B's — proves "
        "--full-page metrics read the PINNED tab; got A={} B={}".format(dim_a, dim_b))


def test_target_pins_direct_commands(jaine_browser, test_server):
    """--target drives ONLY the selected tab for representative DIRECT get_tab
    side-effect commands (navigate, click, network); the sibling tab is untouched and
    a bare call (no --target) is unchanged. The remaining direct callers
    (reload/console/pdf/viewport/window) share the identical get_tab(TARGET) pattern,
    guaranteed by test_no_unpinned_tab_resolution. (cdp_js-family + selector-form
    coverage lives in test_target_pins_cdp_js_family.)"""
    base = "http://localhost:{}/test-page.html".format(test_server)
    id_a = _open_lane("look=dirA", test_server)
    id_b = _open_lane("look=dirB", test_server)

    # direct (navigate) drives only the pinned tab; id is stable across navigation
    run_cdp(["--target", id_a, "navigate", base + "?look=dirA2"])
    run_cdp(["--target", id_a, "wait", "--js", "location.search === '?look=dirA2'", "5"])
    assert run_cdp(["--target", id_a, "js", "location.search"]).stdout.strip() == "?look=dirA2"
    assert run_cdp(["--target", id_b, "js", "location.search"]).stdout.strip() == "?look=dirB"

    # direct (click) drives only the pinned tab
    run_cdp(["--target", id_b, "click", "#test-btn"])
    assert "true" in run_cdp(["--target", id_b, "js",
                              "document.getElementById('test-btn').dataset.clicked"]).stdout
    a_clicked = run_cdp(["--target", id_a, "js",
                         "String(document.getElementById('test-btn').dataset.clicked)"]).stdout.strip()
    assert a_clicked in ("undefined", "null", ""), \
        "tab A's button must NOT be clicked, got {!r}".format(a_clicked)

    # direct (network) reloads + captures the PINNED tab → its own URL is in the log
    net_b = run_cdp(["--target", id_b, "network"])
    assert net_b.returncode == 0 and "look=dirB" in net_b.stdout, (
        "network must reload+capture the pinned tab B (its ?look=dirB request), "
        "got rc={} out={!r}".format(net_b.returncode, net_b.stdout))

    # no --target is unchanged (drives some tab, rc 0)
    assert run_cdp(["js", "location.search"]).returncode == 0


def test_target_ambiguous_and_miss_fail_loud(jaine_browser, test_server):
    """An ambiguous url selector and an unknown selector both fail loud (non-zero)."""
    base = "http://localhost:{}/test-page.html".format(test_server)
    run_cdp(["open", base + "?dup=AMB"])
    run_cdp(["open", base + "?dup=AMB"])  # two tabs share the token → ambiguous

    amb = run_cdp(["--target", "dup=AMB", "js", "1"])
    assert amb.returncode != 0, "ambiguous url selector must fail loud, got rc=0"
    assert "ambiguous" in amb.stderr.lower() or "match" in amb.stderr.lower()

    miss = run_cdp(["--target", "zzz-no-such-tab", "js", "1"])
    assert miss.returncode != 0, "unknown selector must fail loud"
    assert "no tab" in miss.stderr.lower() or "matched no" in miss.stderr.lower()


# ── sub-D: --insecure web-security lane (#93) ──

# A dedicated insecure e2e port: fixed + in-range + never 9333, and always distinct from the
# secure lane's CDP_PORT (R1-F4). `CDP_PORT + 2` could land on 9333 (CDP_PORT=9331) or out of
# range (CDP_PORT=65534) (R2-F1) — so use a fixed in-range port and only step away if CDP_PORT
# happens to equal it. Both 9356 and 9358 are in 1..65535 and != 9333.
INSECURE_TEST_PORT = 9356 if CDP_PORT != 9356 else 9358


def _cdp_online(port):
    try:
        return urlopen("http://localhost:{}/json/version".format(port), timeout=3).status == 200
    except (URLError, OSError):
        return False


@pytest.fixture(scope="module")
def insecure_lane():
    """An isolated HEADLESS lane launched via launch.sh with LOOK_INSECURE=1
    (--disable-web-security). Dedicated non-9333 port + temp profile; torn down with
    pkill + rmtree. Fails loud on an unexpected pre-existing listener (isolation)."""
    if _cdp_online(INSECURE_TEST_PORT):
        pytest.fail("Unexpected CDP listener on insecure test port {0} — kill it "
                    "(pkill -f remote-debugging-port={0}) and re-run.".format(INSECURE_TEST_PORT))
    profile = tempfile.mkdtemp(prefix="jaine-insecure-{}-".format(INSECURE_TEST_PORT))
    env = os.environ.copy()
    for _v in LANE_ENV_VARS:
        env.pop(_v, None)
    env.update({
        "CDP_PORT": str(INSECURE_TEST_PORT),
        "LOOK_PROFILE_DIR": profile,
        "LOOK_HEADLESS": "1",
        "LOOK_INSECURE": "1",
    })
    kill_match = _kill_pattern(profile)
    # DEVNULL, not PIPE: launch.sh redirects Chrome into the lane's chrome.log;
    # an unread PIPE could fill and block the child (SP1 review).
    subprocess.Popen(["bash", LAUNCH_SCRIPT, "about:blank"], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        if _cdp_online(INSECURE_TEST_PORT):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
        _wait_port_release(INSECURE_TEST_PORT)
        shutil.rmtree(profile, ignore_errors=True)
        pytest.fail("insecure lane did not start on {} within 20s".format(INSECURE_TEST_PORT))
    yield INSECURE_TEST_PORT
    subprocess.run(["pkill", "-f", "--", kill_match], capture_output=True)
    # headless=new Chrome serves CDP for seconds after SIGTERM — wait for actual
    # port release so a back-to-back run doesn't trip the fail-loud guard above.
    _wait_port_release(INSECURE_TEST_PORT)
    shutil.rmtree(profile, ignore_errors=True)


def _fetch_result(cdp_port, target_url, poll=5.0):
    """Navigate the lane at cdp_port to the file:// repro (target in #hash); poll #r."""
    repro = (Path(FIXTURES_DIR) / "look-insecure-fetch.html").as_uri()  # percent-encoded (space-safe)
    nav = run_cdp(["navigate", repro + "#" + target_url], env_override={"CDP_PORT": str(cdp_port)})
    assert nav.returncode == 0, "navigate failed: {}".format(nav.stderr)
    deadline = time.time() + poll
    last = ""
    while time.time() < deadline:
        r = run_cdp(["js", "document.getElementById('r').textContent"],
                    env_override={"CDP_PORT": str(cdp_port)})
        last = r.stdout.strip()
        if last and last != "PENDING":
            return last
        time.sleep(0.25)
    return last


def test_insecure_lane_unblocks_file_fetch(insecure_lane, test_server):
    """D acceptance (flag-shipped): an --insecure lane lets a file:// page fetch an
    http:// origin (the #93 case) — fetch SUCCEEDS."""
    target = "http://127.0.0.1:{}/lan-probe.txt".format(test_server)
    result = _fetch_result(insecure_lane, target)
    assert result.startswith("OK:"), "insecure lane must unblock the fetch, got {!r}".format(result)
    assert "PONG-LOOK-93" in result


def test_secure_lane_blocks_file_fetch(jaine_browser, test_server):
    """Causation control: the DEFAULT (secure) lane CANNOT fetch http:// from file://
    — proves --disable-web-security, not something else, is what unblocks it."""
    target = "http://127.0.0.1:{}/lan-probe.txt".format(test_server)
    result = _fetch_result(CDP_PORT, target)  # CDP_PORT = the secure jaine_browser lane
    assert result.startswith("FAIL:"), "secure lane must block the cross-origin fetch, got {!r}".format(result)


# ── AX & Ref-Bridge (#185) ──

import re as _re

AX_PAGE = os.path.join(FIXTURES_DIR, "ax-page.html")


def _navigate_ax_page(jaine_browser):
    r = run_cdp(["navigate", AX_PAGE, "--wait", "load"])
    assert r.returncode == 0, "navigate failed: {}".format(r.stderr)


def _find_ref(ax_stdout, name_substr):
    for line in ax_stdout.splitlines():
        m = _re.search(r'\[ref=(\d+)\]', line)
        if m and name_substr in line:
            return m.group(1)
    return None


def test_ax_grammar_first_line(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0, "ax failed: {}".format(r.stderr)
    first = r.stdout.splitlines()[0]
    assert _re.match(r'^AX_OK nodes=\d+ shown=\d+ frames=\d+( truncated=1)?$', first), \
        "First line doesn't match AX_OK grammar: {!r}".format(first)


def test_ax_snapshot_has_roles_and_states(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    out = r.stdout
    assert "button" in out
    assert "[disabled]" in out
    assert "[checked]" in out
    assert "[ref=" in out
    assert "heading" in out


def test_ax_table_15_rows(jaine_browser):
    """§6: fixture has 15-row table, ax must show them."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax", "--max-nodes", "0"])
    assert r.returncode == 0
    row_count = r.stdout.count("- row")
    assert row_count >= 15, "Expected >= 15 rows, got {}".format(row_count)


def test_ax_iframe_frame_section(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    assert "frame:" in r.stdout
    assert "Frame Button" in r.stdout


def test_click_ref_from_live_snapshot(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    ref = _find_ref(r.stdout, "Submit")
    assert ref, "Could not find Submit button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    assert "clicked BUTTON (trusted, ref={})".format(ref) in cr.stdout
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "click:ax-btn" in jr.stdout


def test_fill_ref_sets_value_and_events(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Search")
    assert ref, "No Search textbox ref"
    fr = run_cdp(["fill", "--ref", ref, "test-value"])
    assert fr.returncode == 0
    assert "filled" in fr.stdout.lower()
    vr = run_cdp(["js", "document.getElementById('ax-input').value"])
    assert "test-value" in vr.stdout
    er = run_cdp(["js", "document.getElementById('ax-input').dataset.inputFired"])
    assert "true" in er.stdout


def test_js_ref_accesses_element(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Submit")
    assert ref
    jr = run_cdp(["js", "--ref", ref, "el.tagName"])
    assert jr.returncode == 0
    assert "BUTTON" in jr.stdout


def test_assert_ref_actionable_pass(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Submit")
    assert ref
    ar = run_cdp(["assert", "--ref", ref, "--actionable", "--stable", "200"])
    assert ar.returncode == 0
    assert "ASSERT_PASS" in ar.stdout


def test_assert_ref_occluded_fail(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Occluded AX")
    assert ref
    ar = run_cdp(["assert", "--ref", ref, "--actionable", "--stable", "200", "--timeout", "2"])
    assert ar.returncode != 0
    assert "ASSERT_FAIL" in ar.stdout


def test_click_ref_not_hittable(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Occluded AX")
    assert ref
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode != 0
    assert "CLICK_REF_NOT_HITTABLE" in cr.stdout


def test_key_ref_enter_submits_form(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Form field")
    assert ref, "No form textbox ref (look for 'Form field' label)"
    kr = run_cdp(["key", "--ref", ref, "Enter"])
    assert kr.returncode == 0
    assert "pressed Enter (ref={})".format(ref) in kr.stdout
    sr = run_cdp(["js", "window.__submitted"])
    assert "true" in sr.stdout


def test_hover_selector_shows_tooltip(jaine_browser):
    _navigate_ax_page(jaine_browser)
    hr = run_cdp(["hover", "#hover-target"])
    assert hr.returncode == 0
    assert "hovered DIV" in hr.stdout
    vr = run_cdp(["js", "getComputedStyle(document.getElementById('hover-tooltip')).display"])
    assert vr.stdout.strip() != "none"


def test_hover_ref_path(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Submit")
    assert ref
    hr = run_cdp(["hover", "--ref", ref])
    assert hr.returncode == 0
    assert "hovered BUTTON (ref={})".format(ref) in hr.stdout


def test_hover_not_hittable(jaine_browser):
    _navigate_ax_page(jaine_browser)
    hr = run_cdp(["hover", "#occluded-ax-btn"])
    assert hr.returncode != 0
    assert "HOVER_NOT_HITTABLE" in hr.stdout


def test_ax_scoped_ref(jaine_browser):
    _navigate_ax_page(jaine_browser)
    full = run_cdp(["ax"])
    assert full.returncode == 0
    ref = _find_ref(full.stdout, "Submit")
    assert ref
    scoped = run_cdp(["ax", "--ref", ref])
    assert scoped.returncode == 0
    assert "AX_OK" in scoped.stdout
    assert len(scoped.stdout) < len(full.stdout)


def test_drag_mouse_pointer_zone(jaine_browser):
    _navigate_ax_page(jaine_browser)
    dr = run_cdp(["drag", "#drag-src", "#drag-dst"])
    assert dr.returncode == 0
    assert "dragged DIV -> DIV (mouse)" in dr.stdout
    pr = run_cdp(["js", "window.__pointerDropped"])
    assert "true" in pr.stdout


def test_drag_html5_zone(jaine_browser):
    _navigate_ax_page(jaine_browser)
    run_cdp(["js", "window.__html5Dropped=null"])
    dr = run_cdp(["drag", "--html5", "#html5-src", "#html5-dst"])
    assert dr.returncode == 0
    assert "dragged DIV -> DIV (html5)" in dr.stdout
    pr = run_cdp(["js", "window.__html5Dropped"])
    assert "payload-42" in pr.stdout


def test_drag_cancel_esc(jaine_browser):
    _navigate_ax_page(jaine_browser)
    run_cdp(["js", "window.__actions=[]"])
    dr = run_cdp(["drag", "--cancel", "#esc-src", "#drag-dst"])
    assert dr.returncode == 0
    assert "DRAG_CANCELLED" in dr.stdout and "(esc)" in dr.stdout
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "esc-cancel" in jr.stdout


def test_drag_not_hittable(jaine_browser):
    _navigate_ax_page(jaine_browser)
    dr = run_cdp(["drag", "#occluded-ax-btn", "#drag-dst"])
    assert dr.returncode != 0
    assert "DRAG_NOT_HITTABLE" in dr.stdout


def test_ref_stale_after_reload_all_commands(jaine_browser):
    """§4 REF_STALE: after reload, old refs stale for ALL ref-commands."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Submit")
    assert ref
    run_cdp(["reload"])
    run_cdp(["wait", "h1", "5"])
    for cmd_args in [
        ["click", "--ref", ref],
        ["fill", "--ref", ref, "x"],
        ["js", "--ref", ref, "el.tagName"],
        ["assert", "--ref", ref, "--timeout", "1"],
        ["key", "--ref", ref, "Enter"],
        ["hover", "--ref", ref],
        ["ax", "--ref", ref],
    ]:
        cr = run_cdp(cmd_args)
        assert cr.returncode != 0, "Expected REF_STALE for {}".format(cmd_args)
        assert "REF_STALE" in cr.stdout, "Missing REF_STALE marker for {}".format(cmd_args)


def test_shadow_markers_in_snapshot(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    assert r.returncode == 0
    assert "[shadow=open]" in r.stdout
    assert "[shadow=closed]" in r.stdout


def test_assert_ref_actionable_shadow_button(jaine_browser):
    """Regression: assert --ref --actionable must PASS on shadow buttons (shadow-walk parity with click)."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Shadow Open Btn")
    assert ref
    ar = run_cdp(["assert", "--ref", ref, "--actionable", "--stable", "200"])
    assert ar.returncode == 0, "shadow button should be actionable (shadow-walk hit-test): {}".format(ar.stdout)
    assert "ASSERT_PASS" in ar.stdout


def test_shadow_open_button_clickable_via_ref(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Shadow Open Btn")
    assert ref, "No shadow open button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "click:shadow-open" in jr.stdout


def test_shadow_closed_button_clickable_via_ref(jaine_browser):
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Shadow Closed Btn")
    assert ref, "No shadow closed button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    jr = run_cdp(["js", "JSON.stringify(window.__actions)"])
    assert "click:shadow-closed" in jr.stdout


def test_shadow_canvas_absent_from_ax(jaine_browser):
    """§2.5 honest negative: canvas in shadow has NO AX node."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax", "--raw", "--max-nodes", "0"])
    assert r.returncode == 0
    lines_lower = r.stdout.lower()
    assert "canvas" not in lines_lower or "shadow" not in lines_lower.split("canvas")[0][-50:]


def test_click_ref_child_frame(jaine_browser):
    """§4 R1-F2: ref from child frame clickable from parent session."""
    _navigate_ax_page(jaine_browser)
    r = run_cdp(["ax"])
    ref = _find_ref(r.stdout, "Frame Button")
    assert ref, "No iframe button ref"
    cr = run_cdp(["click", "--ref", ref])
    assert cr.returncode == 0
    assert "clicked" in cr.stdout.lower()


# ── #322 PR6 D2: log redaction (behavioral proof against a live browser) ──


def test_navigate_log_redacts_query_string(test_page_url, tmp_path):
    """navigate must not persist query/fragment values into the long-lived log."""
    log = tmp_path / "look.log"
    r = run_cdp(["navigate", test_page_url + "?bdzsecret=hunter2"],
                env_override={"BULLDOZER_LOOK_LOG": str(log)})
    assert r.returncode == 0, r.stderr
    text = log.read_text()
    assert "hunter2" not in text, "query value leaked into the log"
    assert "?<redacted>" in text, "redaction marker missing"
    assert "event=navigate" in text


def test_js_log_hashes_expression(test_page_url, tmp_path):
    """js must log length+hash of the expression, never its source."""
    log = tmp_path / "look.log"
    expr = "'bdzmarker_' + 'hunter2'"
    r = run_cdp(["js", expr], env_override={"BULLDOZER_LOOK_LOG": str(log)})
    assert r.returncode == 0, r.stderr
    text = log.read_text()
    assert "hunter2" not in text, "JS source leaked into the log"
    assert "expr_len={}".format(len(expr)) in text
    assert "expr_sha=" in text
