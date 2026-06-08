#!/usr/bin/env python3
"""E2E tests for the SP2 verify-core (navigate --wait, console --gate, assert,
click --require-trusted, screenshot --bind) + cookie-seed, on the isolated
Chrome-for-Testing lane (cft_browser, DRIVE_TEST_PORT).

Self-contained: skips when CfT is not installed (run skills/look/scripts/update-cft.sh).
Every cdp.py call carries the full lane contract (CDP_PORT + CHROME_APP_NAME).
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from conftest import run_cdp_on_lane, transient_cft_lane  # noqa: E402

COOKIE_SEED = os.path.join(os.path.dirname(__file__), "..",
                           "skills", "drive", "scripts", "cookie_seed.py")
SEED_TARGET_PORT = 9362  # e2e port registry (conftest)


def _drive_cdp(port, args, timeout=30):
    """Lane-contract wrapper — single enforcement point lives in conftest."""
    return run_cdp_on_lane(port, args, timeout=timeout)


def _drive_url(test_server):
    return "http://localhost:{}/drive-page.html".format(test_server)


def test_drive_page_serves(cft_browser, test_server):
    """Scaffold smoke: the drive fixture page loads on the CfT lane.
    Uses --wait load (deterministic) instead of a flat sleep (review pack D)."""
    r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
    assert r.returncode == 0, r.stderr
    t = _drive_cdp(cft_browser, ["title"])
    assert "JAINE Drive Page" in t.stdout


class TestNavigateWait:
    def test_wait_load_prints_final_url_and_loader(self, cft_browser, test_server):
        url = _drive_url(test_server)
        r = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        assert r.returncode == 0, r.stderr
        assert url in r.stdout                 # final URL printed
        assert "load fired" in r.stdout
        assert "loader=" in r.stdout           # navigation token for --bind cross-check

    def test_wait_bare_defaults_to_load(self, cft_browser, test_server):
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait"])
        assert r.returncode == 0, r.stderr
        assert "load fired" in r.stdout

    def test_wait_domcontentloaded(self, cft_browser, test_server):
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                     "--wait", "domcontentloaded"])
        assert r.returncode == 0, r.stderr
        assert "domcontentloaded fired" in r.stdout

    def test_wait_networkidle(self, cft_browser, test_server):
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                     "--wait", "networkidle"])
        assert r.returncode == 0, r.stderr
        assert "networkidle fired" in r.stdout

    def test_wait_instant_page_not_lost(self, cft_browser):
        """data: URL loads near-instantly — the load event can fire BEFORE the
        Page.navigate response arrives; the event buffer must not lose it."""
        r = _drive_cdp(cft_browser, ["navigate", "data:text/html,<title>fast</title>ok",
                                     "--wait", "load"])
        assert r.returncode == 0, r.stderr
        assert "load fired" in r.stdout

    def test_wait_load_held_by_slow_subresource(self, cft_browser):
        """R1-F2 race guard, behavioral proof: the awaited load is bound to OUR
        loaderId. A page whose subresource responds only after ~1.2s must hold
        --wait load for >=1s — an early or foreign load-class event must not
        satisfy the wait.

        The page itself is served over http (same origin as the slow image):
        a data: document does NOT hold its window load for an http subresource
        (opaque origin — measured 104ms vs 1249ms for the http page), so the
        data:-shaped fixture would false-fail this assertion."""
        import base64 as b64
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class SlowHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/slow.png"):
                    time.sleep(1.2)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.end_headers()
                    self.wfile.write(b64.b64decode(  # 1x1 transparent PNG
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                        "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))
                elif self.path.startswith("/page.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<title>slowsub</title><img src='/slow.png'>")
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        srv.daemon_threads = True  # review sweep: a sleeping non-daemon request
        # thread would outlive shutdown() and stall runner exit on a failure path
        sport = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            page = "http://127.0.0.1:{}/page.html".format(sport)
            t0 = time.time()
            r = _drive_cdp(cft_browser, ["navigate", page, "--wait", "load",
                                         "--timeout", "10"])
            elapsed = time.time() - t0
            assert r.returncode == 0, r.stderr
            assert "load fired" in r.stdout
            assert elapsed >= 1.0, ("load satisfied after only {:.2f}s — the wait "
                                    "is not bound to our navigation".format(elapsed))
        finally:
            srv.shutdown()

    def test_expect_url_pass_and_fail(self, cft_browser, test_server):
        url = _drive_url(test_server)
        ok = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load",
                                      "--expect-url", "drive-page"])
        assert ok.returncode == 0, ok.stderr
        bad = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load",
                                       "--expect-url", "WRONG-SUBSTRING"])
        assert bad.returncode == 1
        assert "NAVIGATE_URL_MISMATCH" in bad.stdout  # verdict grammar: stdout

    def test_navigation_error_fails_loud(self, cft_browser):
        r = _drive_cdp(cft_browser, ["navigate", "http://localhost:1/nope",
                                     "--wait", "load", "--timeout", "8"])
        assert r.returncode == 1
        assert "NAVIGATE_FAIL" in r.stdout  # verdict grammar: stdout

    def test_flags_before_url_order(self, cft_browser, test_server):
        """Flag order is free: flags may precede the URL positional."""
        r = _drive_cdp(cft_browser, ["navigate", "--wait", "load",
                                     _drive_url(test_server)])
        assert r.returncode == 0, r.stderr
        assert "load fired" in r.stdout

    def test_wait_event_typo_fails_loud(self, cft_browser, test_server):
        """Review sweep: '--wait networkIdle' (case typo) must NOT silently
        default to load — waiting for the wrong event defeats the verify-core."""
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                     "--wait", "networkIdle"])
        assert r.returncode == 1
        assert "lowercase" in r.stderr

    def test_expect_url_flag_swallow_guard(self, cft_browser, test_server):
        """Review pack A: '--expect-url --timeout 5' must error, not consume
        '--timeout' as the substring."""
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                     "--wait", "load", "--expect-url", "--timeout", "5"])
        assert r.returncode == 1
        assert "needs a substring" in r.stderr

    def test_legacy_navigate_unchanged(self, cft_browser, test_server):
        """No --wait → exact legacy contract (the /look default is untouched)."""
        r = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server)])
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith("Navigated to ")
        assert "fired" not in r.stdout


class TestAssert:
    def test_assert_present_pass(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#always-visible"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_absent_fails_never_true(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#no-such-element", "--timeout", "2"])
        assert r.returncode == 1
        assert "ASSERT_FAIL" in r.stdout
        assert "never true" in r.stdout

    def test_assert_visible_rejects_hidden(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#always-hidden", "--visible",
                                     "--timeout", "2"])
        assert r.returncode == 1
        assert "ASSERT_FAIL" in r.stdout

    def test_assert_js_delayed_enable(self, cft_browser, test_server):
        """Waits out the 600ms disabled window via a JS condition."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "--js",
                       "!document.querySelector('#delayed-btn').disabled",
                       "--stable", "300", "--timeout", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_stability_window_outwaits_flapping(self, cft_browser, test_server):
        """THE flaky-vs-real discriminator: #flappy toggles 6x/200ms then goes
        stable. A bare presence check would pass on the first flash; the
        stability window must hold through the flapping and pass only once
        the element is continuously visible."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#flappy", "--visible",
                                     "--stable", "600", "--timeout", "8"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_flap_diagnostics_on_short_timeout(self, cft_browser, test_server):
        """Timing out DURING the flapping reports flap diagnostics, not a bare
        fail — this is what distinguishes 'flaky' from 'absent' in reports.
        Review pack D: assert the DIAGNOSTIC, not just the FAIL marker — and
        never the 'never true' (absent) class for an element that does exist."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#flappy", "--visible",
                                     "--stable", "5000", "--timeout", "1"])
        assert r.returncode == 1
        assert "ASSERT_FAIL" in r.stdout
        assert "never true" not in r.stdout
        # depending on when the assert lands in the flap cycle the element is
        # either mid-flap ("flapped Nx") or already steadily visible but short
        # of the 5s window ("held only") — both are the not-absent diagnostic
        assert ("flapped" in r.stdout) or ("held only" in r.stdout), r.stdout

    def test_assert_actionable_rejects_occluded(self, cft_browser, test_server):
        """R1-F3 / spec §4.3: #occluded-btn is VISIBLE but covered by an overlay —
        --visible passes, --actionable must fail (hit-test misses the target)."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        vis = _drive_cdp(cft_browser, ["assert", "#occluded-btn", "--visible",
                                       "--timeout", "3"])
        assert vis.returncode == 0, "occluded button should still be VISIBLE"
        act = _drive_cdp(cft_browser, ["assert", "#occluded-btn", "--actionable",
                                       "--timeout", "2"])
        assert act.returncode == 1
        assert "ASSERT_FAIL" in act.stdout

    def test_assert_actionable_waits_out_disabled(self, cft_browser, test_server):
        """#delayed-btn is disabled for 600ms — actionability includes enabled,
        so the assert must hold until the enable then pass."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#delayed-btn", "--actionable",
                                     "--stable", "300", "--timeout", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_actionable_scrolls_to_below_fold(self, cft_browser, test_server):
        """R2 recheck of R1-F3: actionability scrolls into view first, exactly
        like cmd_click's measure — a below-fold control IS actionable (click
        would scroll+hit it); without the scroll, elementFromPoint returns
        nothing at off-viewport coords and the assert would falsely fail."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["assert", "#belowfold-btn", "--actionable",
                                     "--stable", "300", "--timeout", "5"])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ASSERT_PASS" in r.stdout

    def test_assert_mode_flags_mutex(self, cft_browser):
        r = _drive_cdp(cft_browser, ["assert", "--js", "true", "--visible"])
        assert r.returncode == 1
        assert "mutually exclusive" in r.stderr
        r2 = _drive_cdp(cft_browser, ["assert", "#x", "--visible", "--actionable"])
        assert r2.returncode == 1
        assert "mutually exclusive" in r2.stderr


class TestCookieSeed:
    def test_two_lane_seed_roundtrip(self, cft_browser, test_server):
        """Full chain: cookie set in lane A (source stands in for the daily
        browser via --from-port) → cookie_seed.py → visible in fresh lane B.
        Lane B lifecycle via conftest.transient_cft_lane (review pack D — the
        inline Popen/poll/pkill block duplicated the fixture)."""
        url = _drive_url(test_server)
        _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "document.cookie='sp2seed=ok; path=/'; 'set'"])

        with transient_cft_lane(SEED_TARGET_PORT):
            seed = subprocess.run(
                [sys.executable, COOKIE_SEED, "--domains", "localhost",
                 "--from-port", str(cft_browser), "--to-port", str(SEED_TARGET_PORT)],
                capture_output=True, text=True, timeout=30)
            assert seed.returncode == 0, seed.stdout + seed.stderr
            assert "localhost" in seed.stdout

            r = _drive_cdp(SEED_TARGET_PORT, ["navigate", url, "--wait", "load"])
            assert r.returncode == 0, r.stderr
            chk = _drive_cdp(SEED_TARGET_PORT, ["js", "document.cookie"])
            assert "sp2seed=ok" in chk.stdout, \
                "seeded cookie not visible in target lane: " + chk.stdout

    def test_dry_run_writes_nothing(self, cft_browser, test_server):
        url = _drive_url(test_server)
        _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        _drive_cdp(cft_browser, ["js", "document.cookie='sp2dry=x; path=/'; 'set'"])
        r = subprocess.run(
            [sys.executable, COOKIE_SEED, "--domains", "localhost",
             "--from-port", str(cft_browser), "--to-port", "9399", "--dry-run"],
            capture_output=True, text=True, timeout=30)
        # dry-run must not need the target lane at all (9399 is not running)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "DRY-RUN" in r.stdout


class TestScreenshotBind:
    def test_bind_emits_url_loader_timestamp(self, cft_browser, test_server, tmp_path):
        url = _drive_url(test_server)
        nav = _drive_cdp(cft_browser, ["navigate", url, "--wait", "load"])
        nav_loader = re.search(r"loader=(\S+)\)", nav.stdout).group(1)
        shot = str(tmp_path / "bound.jpg")
        r = _drive_cdp(cft_browser, ["screenshot", shot, "--bind"])
        assert r.returncode == 0, r.stderr
        lines = r.stdout.strip().splitlines()
        assert lines[0].startswith(shot)            # legacy first line intact
        bind = [l for l in lines if l.startswith("BIND ")]
        assert bind, "no BIND line in: " + r.stdout
        assert "url=" + url in bind[0]
        m = re.search(r"loader=(\S+)", bind[0])
        assert m and m.group(1) == nav_loader, \
            "screenshot loaderId does not match the navigation it claims to capture"
        assert re.search(r"t=\d+", bind[0])

    def test_bind_detects_stale_navigation(self, cft_browser, test_server, tmp_path):
        """The whole point of binding: navigating AWAY changes the loaderId, so a
        screenshot taken after an unnoticed navigation is distinguishable."""
        nav1 = _drive_cdp(cft_browser, ["navigate", _drive_url(test_server),
                                        "--wait", "load"])
        loader1 = re.search(r"loader=(\S+)\)", nav1.stdout).group(1)
        _drive_cdp(cft_browser, ["navigate", "data:text/html,<title>away</title>moved",
                                 "--wait", "load"])
        shot = str(tmp_path / "stale.jpg")
        r = _drive_cdp(cft_browser, ["screenshot", shot, "--bind"])
        bind = [l for l in r.stdout.splitlines() if l.startswith("BIND ")][0]
        m = re.search(r"loader=(\S+)", bind)
        assert m.group(1) != loader1

    def test_screenshot_without_bind_unchanged(self, cft_browser, test_server, tmp_path):
        shot = str(tmp_path / "plain.jpg")
        r = _drive_cdp(cft_browser, ["screenshot", shot])
        assert r.returncode == 0
        assert "BIND" not in r.stdout


class TestRequireTrusted:
    def test_trusted_click_succeeds(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["click", "#trusted-target", "--require-trusted"])
        assert r.returncode == 0, r.stderr
        assert "(trusted)" in r.stdout
        chk = _drive_cdp(cft_browser, ["js", "window.__driveTrusted"])
        assert "true" in chk.stdout.lower()

    def test_hidden_element_refused_not_clicked(self, cft_browser, test_server):
        """R2-U: with --require-trusted the untrusted fallback is REFUSED —
        exit 1, no click happens at all (vs default mode's exit-0 fallback)."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["click", "#hidden-btn", "--require-trusted"])
        assert r.returncode == 1
        assert "CLICK_REQUIRE_TRUSTED_FAIL" in r.stdout  # verdict grammar: stdout
        chk = _drive_cdp(cft_browser, ["js", "window.__hiddenClicked === true"])
        assert "true" not in chk.stdout.lower()

    def test_default_mode_fallback_unchanged(self, cft_browser, test_server):
        """No flag → legacy contract: untrusted fallback still exit 0 + WARN."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["click", "#hidden-btn"])
        assert r.returncode == 0
        assert "untrusted" in r.stdout


class TestConsoleGate:
    def test_gate_clean_page_passes(self, cft_browser, test_server):
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 0, r.stderr
        assert "CONSOLE_GATE_OK" in r.stdout

    def test_gate_catches_midflow_exception(self, cft_browser, test_server):
        """PIN of the SP2 console-gate verification (P1): an uncaught exception
        that fires BETWEEN cdp.py calls (nobody listening) is replayed to the
        late one-shot subscriber on the pinned CfT. If a future pin-bump breaks
        replay, this fails loudly instead of silently weakening the gate."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ throw new Error('GATE_MIDFLOW_E2E'); }, 200); 'armed'"])
        time.sleep(0.7)  # 200ms timer + margin — the error must fire pre-gate
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 1
        assert "GATE_MIDFLOW_E2E" in r.stdout
        assert "CONSOLE_GATE_FAIL" in r.stdout  # verdict grammar: stdout
        assert "1 exception(s)" in r.stdout     # breakdown names the leg

    def test_gate_catches_log_domain_cors(self, cft_browser, test_server):
        """Review pack A (empirically confirmed gap): browser-generated errors —
        CORS blocks, net::ERR_* — surface ONLY via Log.entryAdded, which the
        gate now subscribes to. A cross-origin fetch with no CORS headers fires
        inside the gate's live window and must gate."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ fetch('http://example.com/').catch(function(e){}); }, 300); 'armed'"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 1, "Log-domain error did not gate: " + r.stdout
        assert "[log:" in r.stdout
        assert "CONSOLE_GATE_FAIL" in r.stdout

    def test_gate_catches_console_error_live(self, cft_browser, test_server):
        """PIN (P2-revised): console.error fired DURING the gate's 3s listen
        window is caught live. Retroactive console.* replay is NOT guaranteed
        (it depends on a fragile storage-activation quirk — see the SP2
        console-gate verification doc); the gate's retroactive guarantee covers
        exceptions only, so the verify-core calls the gate right after the
        action it checks."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ console.error('GATE_CERR_E2E'); }, 200); 'armed'"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])  # window covers the 200ms shot
        assert r.returncode == 1
        assert "GATE_CERR_E2E" in r.stdout

    def test_gate_log_error_carries_resource_url(self, cft_browser, test_server):
        """Dogfood finding #2 (VRHOT TTS, 2026-06-05): a Log-domain resource error
        printed WITHOUT its URL ('Failed to load resource: 404' — which resource?)
        is undiagnosable. Log.entryAdded carries entry.url — the gate must print it."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "var i=document.createElement('img');"
                   "i.src='missing-resource-404.png';document.body.appendChild(i);'armed'"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 1, "404 resource load must gate: " + r.stdout
        assert "missing-resource-404.png" in r.stdout, \
            "the gated Log error must name the failing resource URL: " + r.stdout

    def test_gate_scoped_by_navigation(self, cft_browser, test_server):
        """PIN (P4): the console buffer clears on navigate — the gate is naturally
        scoped to the CURRENT page; prior-page errors don't leak through."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ throw new Error('PRIOR_PAGE_ERR'); }, 100); 'armed'"])
        time.sleep(0.4)  # 100ms timer + margin
        _drive_cdp(cft_browser, ["navigate",
                   "data:text/html,<title>cleanB</title>ok", "--wait", "load"])
        r = _drive_cdp(cft_browser, ["console", "--gate"])
        assert r.returncode == 0, "prior-page error leaked through the gate: {}".format(r.stdout)
        assert "PRIOR_PAGE_ERR" not in r.stdout

    def test_console_without_gate_unchanged(self, cft_browser, test_server):
        """No --gate → legacy contract: messages printed, ALWAYS exit 0.
        The error fires inside the read's live window (retro console.* replay
        is not guaranteed — see test_gate_catches_console_error_live)."""
        _drive_cdp(cft_browser, ["navigate", _drive_url(test_server), "--wait", "load"])
        _drive_cdp(cft_browser, ["js",
                   "setTimeout(function(){ console.error('LEGACY_NO_GATE'); }, 200); 'armed'"])
        r = _drive_cdp(cft_browser, ["console"])
        assert r.returncode == 0
        assert "LEGACY_NO_GATE" in r.stdout


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
