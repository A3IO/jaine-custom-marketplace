"""Unit tests for skills/look/scripts/launch.sh lane parameterization (sub-project A).

launch.sh is exercised in LOOK_DRY_RUN mode: it resolves config + builds the
Chrome argv array, prints them, and exits 0 WITHOUT launching Chrome. Every knob
is asserted from that output, so these tests never spawn a browser. Pattern
mirrors tests/test_log_round_bash32_compat.py (bash script via subprocess).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))  # make `from conftest import …` reliable

PLUGIN_ROOT = Path(__file__).parent.parent
LAUNCH_SCRIPT = str(PLUGIN_ROOT / "skills" / "look" / "scripts" / "launch.sh")
LAUNCH_TEXT = Path(LAUNCH_SCRIPT).read_text()

# A lane env var leaking from the pytest process would make these tests non-hermetic;
# _run_launch strips them and re-adds only what a case sets. Shared with conftest's
# fixture so the strip-list never drifts (and includes LOOK_DRY_RUN — F5b).
from conftest import LANE_ENV_VARS as _LANE_VARS  # noqa: E402
from conftest import test_env  # noqa: E402


def _run_launch(args=None, env_override=None, dry_run=True, timeout=10, bash="bash"):
    env = test_env()
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
    for bad in ("70000", "-5", "18446744073709551617"):   # "0" moved to TestEphemeralLane (SP4 sentinel)
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


def test_unknown_flag_fails_loud():
    """A.2: an unknown --flag is a fail-loud error (no silent fallback)."""
    r = _run_launch(args=["--bogus"])
    assert r.returncode != 0, "unknown flag must fail loud"
    assert "bogus" in r.stderr or "unknown" in r.stderr.lower()


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


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash 3.2 path missing")
def test_dry_run_works_under_bash_32():
    """launch.sh must dry-run cleanly under macOS /bin/bash (3.2): arrays,
    printf, shopt nocasematch, (( )) — no bash-4-only constructs."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_HEADLESS": "1"}, bash="/bin/bash")
    assert r.returncode == 0, "bash 3.2 dry-run failed: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["headless"] == "1" and "--headless=new" in argv


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
    (single change-point — change both or neither). SP1 form: the default is the
    assignment inside the CHROME_BIN_DEFAULTED block."""
    from conftest import CHROME
    assert ('CHROME_BIN="' + CHROME + '"') in LAUNCH_TEXT


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


def test_test_server_uses_threading_http_server():
    """Drift-guard: test_server must stay ThreadingHTTPServer — Chrome opens speculative
    preconnect sockets (TCP with no request); the single-threaded HTTPServer blocks
    reading such a socket and every later request hangs in the backlog (latent until the
    D e2e fetch). A revert can stay green on fast machines and hang real runs."""
    conftest_text = (PLUGIN_ROOT / "tests" / "conftest.py").read_text()
    assert "ThreadingHTTPServer((" in conftest_text
    assert "= HTTPServer((" not in conftest_text


def test_fixture_strips_lane_env_vars():
    """F1 (code-review): jaine_browser must strip lane vars so the fixture is hermetic —
    a shell LOOK_DRY_RUN=1 / LOOK_HEADLESS=1 / LOOK_INSECURE=1 must not bleed into
    launch.sh (which would dry-run-and-never-start / headless-the-daily-browser / exit 1).
    Post-#357 the strip is expressed as test_env(drop=LANE_ENV_VARS, …)."""
    conftest_text = (PLUGIN_ROOT / "tests" / "conftest.py").read_text()
    assert conftest_text.count("drop=LANE_ENV_VARS") >= 3  # jaine_browser, cft_browser, transient_cft_lane


def test_lane_env_vars_includes_dry_run():
    """F5b (code-review): LOOK_DRY_RUN must be in the shared strip-list so it cannot
    silently activate dry-run in the fixture or a future _run_launch(dry_run=False)."""
    assert "LOOK_DRY_RUN" in _LANE_VARS


def test_dry_run_guard_only_activates_on_one():
    """F3 (code-review): LOOK_DRY_RUN=0 must NOT activate dry-run (the `-n` presence
    check treated '0' as set); only the literal '1' activates it."""
    assert '"${LOOK_DRY_RUN:-}" == "1"' in LAUNCH_TEXT


def test_profile_path_with_backslash_fails_loud():
    """F4 (code-review): a profile path with a backslash can't be safely ERE-escaped
    for pkill → fail loud (no silent garble of the kill pattern), consistent with the
    script's fail-loud ethos for unknown flags / bad ports / LOOK_INSECURE."""
    r = _run_launch(env_override={"LOOK_PROFILE_DIR": "/tmp/a\\b"})
    assert r.returncode != 0, "backslash profile path must fail loud"
    assert "backslash" in r.stderr or "LOOK_PROFILE_DIR" in r.stderr


def test_insecure_repro_fixtures_present():
    """D §8: the #93 reproduction fixtures exist — a file:// page that fetches the URL
    in its hash, and a served probe payload with a known token."""
    fx = PLUGIN_ROOT / "tests" / "fixtures"
    html = (fx / "look-insecure-fetch.html").read_text()
    probe = (fx / "lan-probe.txt").read_text()
    assert "location.hash" in html and 'id="r"' in html, "repro html must fetch location.hash into #r"
    assert "PONG-LOOK-93" in probe, "probe payload must carry the known token"


# ── D: --insecure / LOOK_INSECURE isolation gate (replaces the pre-D reservation) ──


def test_insecure_arg_rejected_on_default_lane():
    """D.2: --insecure is now RECOGNIZED (not an unknown flag), but the gate rejects it
    on the default lane (9333, no explicit profile) → fail-loud."""
    r = _run_launch(args=["--insecure"])
    assert r.returncode != 0, "--insecure on the default lane must fail loud (gate)"
    assert "insecure" in r.stderr.lower()
    assert "unknown" not in r.stderr.lower(), "--insecure must be recognized, not an unknown flag"


def test_insecure_env_rejected_on_default_lane():
    """D.2: LOOK_INSECURE=1 on the default lane is rejected BY THE GATE → fail-loud.
    Asserts the gate's 'isolated'-lane rationale — which is ABSENT from the pre-D
    reserved-guard message ('LOOK_INSECURE is reserved ...') — so this is a genuine RED
    against current launch.sh, not a test that passes both before and after the change."""
    r = _run_launch(env_override={"LOOK_INSECURE": "1"})
    assert r.returncode != 0, "LOOK_INSECURE=1 on the default lane must fail loud (gate)"
    assert "isolated" in r.stderr.lower(), \
        "must be rejected by the isolation gate (names 'isolated'), not the pre-D reserved guard"


def test_insecure_both_routes_gated_on_default_lane():
    """D.2: BOTH the --insecure arg and the LOOK_INSECURE env are gated by the SAME
    isolation check — the pre-D reserved-env guard string is gone."""
    arg = _run_launch(args=["--insecure"])
    env = _run_launch(env_override={"LOOK_INSECURE": "1"})
    assert arg.returncode != 0 and env.returncode != 0
    assert '[ -n "${LOOK_INSECURE:-}" ]' not in LAUNCH_TEXT, \
        "the pre-D reserved guard must be replaced by the isolation gate"


def test_insecure_permitted_on_isolated_lane_adds_flag():
    """D.2 (flag-shipped): non-9333 port + explicit non-default LOOK_PROFILE_DIR →
    --insecure permitted: --disable-web-security in argv, insecure=1, loud warning."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-ins"})
    assert r.returncode == 0, "isolated --insecure must be permitted: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "1"
    assert "--disable-web-security" in argv
    assert "RELAXED" in r.stderr or "web security" in r.stderr.lower(), "must warn loudly on the permitted path"


def test_insecure_env_permitted_on_isolated_lane():
    """D.2: the LOOK_INSECURE env route is permitted on the same isolated lane."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-ins",
                                  "LOOK_INSECURE": "1"})
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "1"
    assert "--disable-web-security" in argv


def test_insecure_rejected_on_default_port_even_with_profile():
    """D.2: port 9333 can NEVER go insecure — even with an explicit non-default profile."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9333", "LOOK_PROFILE_DIR": "/tmp/lane-ins"})
    assert r.returncode != 0, "--insecure on 9333 must fail loud regardless of profile"
    assert "9333" in r.stderr or "isolated" in r.stderr.lower()


def test_insecure_rejected_without_explicit_profile():
    """D.2: a non-9333 lane with a DERIVED profile (LOOK_PROFILE_DIR unset) is not
    'provably isolated' → reject."""
    r = _run_launch(args=["--insecure"], env_override={"CDP_PORT": "9334"})
    assert r.returncode != 0, "--insecure without explicit LOOK_PROFILE_DIR must fail loud"
    assert "LOOK_PROFILE_DIR" in r.stderr or "isolated" in r.stderr.lower()


def test_insecure_rejected_when_profile_is_default_daily_path():
    """D.2: explicitly setting LOOK_PROFILE_DIR to the daily profile must NOT permit
    insecure (never relax the daily browser's profile)."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334",
                                  "LOOK_PROFILE_DIR": "/0/.jaine/.browser/profile"})
    assert r.returncode != 0, "explicit daily-profile path must not permit insecure"
    assert "isolated" in r.stderr.lower() or "profile" in r.stderr.lower()


def test_insecure_rejected_when_profile_aliases_daily_path():
    """R1-F1: a profile that RESOLVES to the daily profile (trailing slash, //, /./)
    must be rejected too — the gate canonicalizes (realpath), not exact-string-matches."""
    for alias in ("/0/.jaine/.browser/profile/", "/0/.jaine/.browser/./profile",
                  "/0/.jaine/.browser//profile"):
        r = _run_launch(args=["--insecure"],
                        env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": alias})
        assert r.returncode != 0, "daily-profile alias {!r} must fail loud".format(alias)
        assert "isolated" in r.stderr.lower() or "daily" in r.stderr.lower()


def test_no_insecure_request_has_no_flag():
    """A non-insecure isolated lane launches normally — no --disable-web-security."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "0"
    assert "--disable-web-security" not in argv


def test_default_lane_insecure_zero():
    """Regression: the default lane is insecure=0 with no --disable-web-security
    (complements test_default_invocation_argv_is_byte_identical)."""
    r = _run_launch()
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["insecure"] == "0"
    assert "--disable-web-security" not in argv


def test_insecure_env_falsy_is_off_not_failloud():
    """D.2: any LOOK_INSECURE value that is NOT truthy (1/true/yes, case-insensitive) is
    OFF (fail-closed) — a normal launch, NOT a fail-loud, even on 9333. Covers falsy
    values AND typos/garbage ('ture'): security stays ON, never a silent relax."""
    for val in ("0", "false", "no", "", "ture", "garbage"):
        r = _run_launch(env_override={"CDP_PORT": "9333", "LOOK_INSECURE": val})
        assert r.returncode == 0, "LOOK_INSECURE={!r} must be off, not fail-loud: {}".format(val, r.stderr)
        cfg, argv = _parse_dryrun(r.stdout)
        assert cfg["insecure"] == "0"
        assert "--disable-web-security" not in argv


def test_disable_web_security_precedes_url():
    """The flag is a Chrome flag — it must come before the trailing URL token."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    _, argv = _parse_dryrun(r.stdout)
    assert "--disable-web-security" in argv
    assert argv.index("--disable-web-security") < len(argv) - 1, "flag must precede the URL"
    assert argv[-1] == "about:blank"


def test_insecure_gate_is_single_code_path():
    """D.2: the --disable-web-security append lives in the ONE shared CHROME_ARGV build
    (A.10), and the gate runs BEFORE the dry-run/real fork — so dry-run == real."""
    assert LAUNCH_TEXT.count("CHROME_ARGV+=(--disable-web-security)") == 1, \
        "exactly one argv-append site (the comment + warning also name the flag — R1-F2)"
    gate_pos = LAUNCH_TEXT.index("INSECURE_REQUESTED")
    dryrun_pos = LAUNCH_TEXT.index('"${LOOK_DRY_RUN:-}" == "1"')
    assert gate_pos < dryrun_pos, "the insecure gate must run before the dry-run/real fork"


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash 3.2 path missing")
def test_insecure_gate_works_under_bash_32():
    """The gate uses only bash-3.2-safe constructs (shopt nocasematch, [[ =~ ]], (( )))."""
    r = _run_launch(args=["--insecure"],
                    env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"},
                    bash="/bin/bash")
    assert r.returncode == 0, "bash 3.2 insecure dry-run failed: {}".format(r.stderr)
    _, argv = _parse_dryrun(r.stdout)
    assert "--disable-web-security" in argv


def test_skill_documents_insecure_lane():
    """A.12/D: SKILL.md documents --insecure / LOOK_INSECURE + the isolated-lane boundary."""
    skill = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    assert "--insecure" in skill and "LOOK_INSECURE" in skill
    assert "isolated" in skill.lower(), "must state the isolated-lane-only boundary"
    assert "--disable-web-security" in skill, "name the actual Chrome flag for transparency"


# ── Cert-pin lane (drive dogfood #1): --cert-spki / LOOK_CERT_SPKI →
#    --ignore-certificate-errors-spki-list, gated like --insecure (non-9333 +
#    provably-isolated profile; --automation's temp profile satisfies the gate). ──

# A syntactically-valid SPKI pin (base64 SHA-256 = 43 chars + '='); NOT a real cert.
FAKE_PIN = "A" * 43 + "="
CERT_FLAG_PREFIX = "--ignore-certificate-errors-spki-list="


def test_cert_spki_arg_rejected_on_default_lane():
    """--cert-spki=PIN is RECOGNIZED (not unknown) but gated off the default lane."""
    r = _run_launch(args=["--cert-spki=" + FAKE_PIN])
    assert r.returncode != 0, "--cert-spki on the default lane must fail loud (gate)"
    assert "cert" in r.stderr.lower()
    assert "unknown" not in r.stderr.lower(), "--cert-spki must be recognized, not an unknown flag"


def test_cert_spki_env_rejected_on_default_lane():
    """LOOK_CERT_SPKI on the default lane is rejected BY THE GATE (names 'isolated')."""
    r = _run_launch(env_override={"LOOK_CERT_SPKI": FAKE_PIN})
    assert r.returncode != 0, "LOOK_CERT_SPKI on the default lane must fail loud (gate)"
    assert "isolated" in r.stderr.lower()


def test_cert_spki_permitted_on_isolated_lane_adds_flag():
    """non-9333 + explicit non-default profile → pin permitted: flag in argv, warning."""
    r = _run_launch(args=["--cert-spki=" + FAKE_PIN],
                    env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": "/tmp/lane-pin"})
    assert r.returncode == 0, "isolated --cert-spki must be permitted: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["cert_spki"] == FAKE_PIN
    assert CERT_FLAG_PREFIX + FAKE_PIN in argv
    assert "cert" in r.stderr.lower(), "must warn loudly on the permitted path"


def test_cert_spki_env_permitted_on_isolated_lane():
    """The LOOK_CERT_SPKI env route is permitted on the same isolated lane."""
    r = _run_launch(env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": "/tmp/lane-pin",
                                  "LOOK_CERT_SPKI": FAKE_PIN})
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["cert_spki"] == FAKE_PIN
    assert CERT_FLAG_PREFIX + FAKE_PIN in argv


def test_cert_spki_permitted_via_automation_temp_profile():
    """KEY drive scenario: --automation's auto temp profile satisfies the cert gate —
    no explicit LOOK_PROFILE_DIR needed (mirrors automation+insecure composition)."""
    r = _run_launch(args=["--automation", "--cert-spki=" + FAKE_PIN],
                    env_override={"CDP_PORT": "9341"})
    assert r.returncode == 0, "automation temp profile must satisfy the cert gate: {}".format(r.stderr)
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["automation"] == "1"
    assert cfg["cert_spki"] == FAKE_PIN
    assert CERT_FLAG_PREFIX + FAKE_PIN in argv


def test_cert_spki_rejected_on_default_port_even_with_profile():
    """Port 9333 can NEVER get cert-bypass — even with an explicit non-default profile."""
    r = _run_launch(args=["--cert-spki=" + FAKE_PIN],
                    env_override={"CDP_PORT": "9333", "LOOK_PROFILE_DIR": "/tmp/lane-pin"})
    assert r.returncode != 0, "--cert-spki on 9333 must fail loud regardless of profile"
    assert "9333" in r.stderr or "isolated" in r.stderr.lower()


def test_cert_spki_rejected_without_explicit_profile():
    """non-9333 with a DERIVED profile (no LOOK_PROFILE_DIR, no --automation) is not
    provably isolated → reject (same boundary as --insecure)."""
    r = _run_launch(args=["--cert-spki=" + FAKE_PIN], env_override={"CDP_PORT": "9341"})
    assert r.returncode != 0, "--cert-spki without explicit profile must fail loud"
    assert "LOOK_PROFILE_DIR" in r.stderr or "isolated" in r.stderr.lower()


def test_cert_spki_rejected_when_profile_aliases_daily_path():
    """A profile that RESOLVES to the daily profile must be rejected (realpath, not
    string-match) — same canonicalization as the insecure gate."""
    for alias in ("/0/.jaine/.browser/profile/", "/0/.jaine/.browser/./profile"):
        r = _run_launch(args=["--cert-spki=" + FAKE_PIN],
                        env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": alias})
        assert r.returncode != 0, "daily-profile alias {!r} must fail loud".format(alias)
        assert "isolated" in r.stderr.lower() or "daily" in r.stderr.lower()


def test_cert_spki_invalid_format_fails_loud():
    """A malformed pin = silent interstitial later — reject up front. Each comma-separated
    element must be base64 SHA-256 (43 chars + '=')."""
    for bad in ("garbage", "A" * 43, "A" * 44, FAKE_PIN + ",short", "x y z", 'a"b', FAKE_PIN + ","):
        r = _run_launch(args=["--cert-spki=" + bad],
                        env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": "/tmp/lane-pin"})
        assert r.returncode != 0, "malformed pin {!r} must fail loud".format(bad)
        assert "spki" in r.stderr.lower() or "cert" in r.stderr.lower()
        assert "unknown" not in r.stderr.lower(), \
            "must be rejected by VALIDATION, not as an unknown flag: {}".format(r.stderr)


def test_cert_spki_multiple_pins_comma_separated():
    """Chrome accepts a comma-separated SPKI list — two valid pins pass validation."""
    two = FAKE_PIN + "," + "B" * 43 + "="
    r = _run_launch(args=["--cert-spki=" + two],
                    env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": "/tmp/lane-pin"})
    assert r.returncode == 0, r.stderr
    _, argv = _parse_dryrun(r.stdout)
    assert CERT_FLAG_PREFIX + two in argv


def test_no_cert_spki_request_has_no_flag():
    """A pin-free isolated lane launches normally — no spki-list flag, cert_spki empty."""
    r = _run_launch(env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["cert_spki"] == ""
    assert not any(a.startswith(CERT_FLAG_PREFIX) for a in argv)


def test_cert_spki_empty_env_is_off_not_failloud():
    """LOOK_CERT_SPKI='' (empty) is OFF — a normal launch even on 9333, not a fail-loud."""
    r = _run_launch(env_override={"CDP_PORT": "9333", "LOOK_CERT_SPKI": ""})
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["cert_spki"] == ""
    assert not any(a.startswith(CERT_FLAG_PREFIX) for a in argv)


def test_cert_spki_flag_precedes_url():
    """The spki-list flag is a Chrome flag — it must come before the trailing URL token."""
    r = _run_launch(args=["--cert-spki=" + FAKE_PIN],
                    env_override={"CDP_PORT": "9341", "LOOK_PROFILE_DIR": "/tmp/lane-pin"})
    _, argv = _parse_dryrun(r.stdout)
    flag = CERT_FLAG_PREFIX + FAKE_PIN
    assert flag in argv
    assert argv.index(flag) < len(argv) - 1, "flag must precede the URL"


def test_cert_spki_gate_is_single_code_path():
    """One argv-append site; the gate runs BEFORE the dry-run/real fork (dry-run == real)."""
    assert LAUNCH_TEXT.count('CHROME_ARGV+=("--ignore-certificate-errors-spki-list=') == 1, \
        "exactly one spki argv-append site"
    gate_pos = LAUNCH_TEXT.index("CERT_SPKI_REQUESTED")
    dryrun_pos = LAUNCH_TEXT.index('"${LOOK_DRY_RUN:-}" == "1"')
    assert gate_pos < dryrun_pos, "the cert gate must run before the dry-run/real fork"


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="bash 3.2 path missing")
def test_cert_spki_gate_works_under_bash_32():
    """The gate uses only bash-3.2-safe constructs."""
    r = _run_launch(args=["--automation", "--cert-spki=" + FAKE_PIN],
                    env_override={"CDP_PORT": "9341"}, bash="/bin/bash")
    assert r.returncode == 0, "bash 3.2 cert-spki dry-run failed: {}".format(r.stderr)
    _, argv = _parse_dryrun(r.stdout)
    assert CERT_FLAG_PREFIX + FAKE_PIN in argv


def test_skill_documents_cert_spki_lane():
    """look SKILL.md documents --cert-spki / LOOK_CERT_SPKI + names the Chrome flag."""
    skill = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    assert "--cert-spki" in skill and "LOOK_CERT_SPKI" in skill
    assert "--ignore-certificate-errors-spki-list" in skill, \
        "name the actual Chrome flag for transparency"


def test_drive_skill_mentions_cert_spki():
    """drive SKILL.md points at the cert-pin option for self-signed LAN targets."""
    skill = (PLUGIN_ROOT / "skills" / "drive" / "SKILL.md").read_text()
    assert "--cert-spki" in skill, "drive SKILL.md must surface the self-signed-HTTPS lane option"


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
    """Structural (hole G/J prep): the osascript blocks must not name the app. The
    only allowed 'Google Chrome' literals are the CHROME_BIN default path
    components."""
    for line in LAUNCH_TEXT.splitlines():
        if 'tell application "Google Chrome"' in line or 'tell process "Google Chrome"' in line:
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


# ── SP1: update-cft.sh (install/pin Chrome for Testing) ──

UPDATE_CFT = str(PLUGIN_ROOT / "skills" / "look" / "scripts" / "update-cft.sh")


def test_update_cft_exists_and_executable():
    assert os.path.exists(UPDATE_CFT)
    assert os.access(UPDATE_CFT, os.X_OK)


def test_update_cft_dry_run_resolves_stable():
    """CFT_DRY_RUN=1 resolves the Stable version + mac-arm64 url and exits 0
    WITHOUT downloading. Needs network (googlechromelabs JSON) — skip offline."""
    r = subprocess.run(["bash", UPDATE_CFT], capture_output=True, text=True,
                       timeout=30, env=test_env(set_vars={"CFT_DRY_RUN": "1"}))
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


# ── SP1: --automation lane (gate R1-C, temp profile R1-E, CfT defaults, argv) ──

# Single source (SP1 review D1): conftest.CFT_BIN is the canonical CfT path; the
# guard test below pins launch.sh's literal to it (A.7-style cross-check).
from conftest import CFT_BIN as CFT_BIN_EXPECTED  # noqa: E402


def test_conftest_cft_const_matches_launch_literal():
    """A.7-style: launch.sh's CFT_BIN literal must equal conftest.CFT_BIN
    (single change-point — change both or neither)."""
    assert ('CFT_BIN="' + CFT_BIN_EXPECTED + '"') in LAUNCH_TEXT


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
    assert cfg["profile"].rstrip("/").endswith("jaine-drive-9444")
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


def test_lane_env_vars_cover_sp1_vars():
    """Hermeticity: a shell LOOK_AUTOMATION=1 or CHROME_APP_NAME bleeding into
    fixtures/dry-run tests would silently flip lanes — the strip-list must cover
    the SP1 vars."""
    assert "LOOK_AUTOMATION" in _LANE_VARS
    assert "CHROME_APP_NAME" in _LANE_VARS


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

    def test_port0_dryrun_works_under_bash_32(self):
        # The ephemeral path adds mktemp/rmdir/(( EPHEMERAL )) constructs — pin
        # macOS /bin/bash (3.2) compatibility like the other *_bash_32 tests.
        r = _run_launch(args=["--automation"], env_override={"CDP_PORT": "0"}, bash="/bin/bash")
        assert r.returncode == 0, "bash 3.2 ephemeral dry-run failed: {}".format(r.stderr)
        cfg, _ = _parse_dryrun(r.stdout)
        assert cfg["ephemeral"] == "1"
        assert "jaine-drive-eph-" in cfg["profile"]


def test_look_skill_md_resolves_scripts_without_requiring_plugin_root_env():
    """#236 (sibling of #221): $CLAUDE_PLUGIN_ROOT is NOT exported to the Bash tool
    (empirically empty), so the documented cdp.py / launch.sh invocations AND the feedback
    `jq` version line must SELF-RESOLVE the plugin dir from the cache — honoring the var if
    set, never hard-requiring it. Mirrors the consult fix (PR #235)."""
    skill = (PLUGIN_ROOT / "skills" / "look" / "SKILL.md").read_text()
    assert "ls -dt ~/.claude/plugins/cache/*/bulldozer/*/" in skill
    assert '[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]' in skill                # guarded honor-if-set
    assert "ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json" in skill
    # no raw ${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/... invocation may remain:
    assert "${CLAUDE_PLUGIN_ROOT}/skills/look/scripts/" not in skill
    assert 'jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json"' not in skill


# ── #160: the daily profile belongs to port 9333 and to nothing else ──────────
# The three opt-in gates (automation / insecure / cert-spki) already refuse a profile
# that resolves to the daily one — but a PLAIN lane (no flag) had no gate at all, so
# `CDP_PORT=9334 LOOK_PROFILE_DIR=/0/.jaine/.browser/profile` built its KILL_MATCH from
# the DAILY profile and pkill'ed the user's live browser (the kill is scoped by
# --user-data-dir, not by port). Gate it unconditionally, at profile-resolution time.

DAILY_PROFILE = "/0/.jaine/.browser/profile"


def test_plain_lane_rejects_daily_profile():
    """#160: a non-9333 lane pointed at the daily profile must fail loud — no flag needed
    to trip the gate. Left ungated, its pkill match is the daily browser's own argv."""
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": DAILY_PROFILE})
    assert r.returncode != 0, "plain lane on the daily profile must be refused"
    assert "daily" in r.stderr.lower()


def test_plain_lane_rejects_daily_profile_alias():
    """#160: canonicalized, like the other gates — a trailing slash / '/./' / '//' or a
    symlink must not sneak the daily profile past the check."""
    for alias in (DAILY_PROFILE + "/", "/0/.jaine/.browser/./profile",
                  "/0/.jaine/.browser//profile"):
        r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": alias})
        assert r.returncode != 0, "daily-profile alias {!r} must fail loud".format(alias)
        assert "daily" in r.stderr.lower()


CASE_ALIASES = ("/0/.JAINE/.browser/profile", "/0/.jaine/.BROWSER/profile",
                "/0/.jaine/.browser/PROFILE")


def _case_alias_is_the_daily_dir():
    """Is this host actually case-insensitive AND carrying the daily profile? Only there
    is a case alias the SAME directory — elsewhere it is a distinct (or absent) path and
    the gate is right to let it through (codex #160 r2, P2: don't assert host state)."""
    try:
        return all(os.path.samefile(a, DAILY_PROFILE) for a in CASE_ALIASES)
    except OSError:
        return False


@pytest.mark.skipif(not _case_alias_is_the_daily_dir(),
                    reason="needs the daily profile on a case-insensitive volume")
def test_plain_lane_rejects_daily_profile_case_alias():
    """#160 (codex P1, verified live): /0 is case-insensitive APFS — `/0/.JAINE/.browser/
    profile` IS the daily profile dir on disk, but os.path.realpath does NOT normalize case,
    so a realpath-only gate compares them unequal and lets the alias through. The gate must
    compare filesystem IDENTITY (samefile), not canonical strings."""
    for alias in CASE_ALIASES:
        r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": alias})
        assert r.returncode != 0, "case alias {!r} must fail loud".format(alias)
        assert "daily" in r.stderr.lower()


def test_plain_lane_rejects_daily_profile_via_symlink(tmp_path):
    """#160: realpath-based, not string-based — a symlink to the daily profile is the
    alias the string gates would miss."""
    link = tmp_path / "sneaky-profile"
    link.symlink_to(DAILY_PROFILE)
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": str(link)})
    assert r.returncode != 0, "symlink to the daily profile must fail loud"
    assert "daily" in r.stderr.lower()


def test_daily_profile_still_allowed_on_9333():
    """#160 must not break the daily browser itself: port 9333 + the daily profile is the
    ONE legitimate pairing, whether derived or passed explicitly."""
    r = _run_launch()                                            # derived
    assert r.returncode == 0, "default daily lane broke: {}".format(r.stderr)
    cfg, _ = _parse_dryrun(r.stdout)
    assert cfg["profile"] == DAILY_PROFILE

    r = _run_launch(env_override={"CDP_PORT": "9333", "LOOK_PROFILE_DIR": DAILY_PROFILE})
    assert r.returncode == 0, "explicit daily profile on 9333 must stay allowed: {}".format(r.stderr)


def test_isolated_lanes_unaffected_by_the_daily_gate():
    """#160 regression floor: an ordinary isolated lane (derived or explicit) still launches."""
    r = _run_launch(env_override={"CDP_PORT": "9334"})           # derived profile-9334
    assert r.returncode == 0, "derived isolated lane broke: {}".format(r.stderr)
    r = _run_launch(env_override={"CDP_PORT": "9334", "LOOK_PROFILE_DIR": "/tmp/lane-x"})
    assert r.returncode == 0, "explicit isolated lane broke: {}".format(r.stderr)


# ── #187 Proposal A: --auto-lane ──────────────────────────────────────────────
# Spec: docs/superpowers/specs/2026-07-21-look-auto-lane-design.md (§8.1, §8.5)
# Offline (dry-run + spawn-free real-launch) coverage. Live lifecycle: test_e2e.py.

from conftest import TEST_SENTINEL as _SENTINEL  # noqa: E402

EXPECTED_DEFAULT_CONFIG_LINES = [
    "LOOK_DRY_RUN",
    "port=9333",
    "profile=/0/.jaine/.browser/profile",
    "profile_overridden=0",
    "headless=0",
    "insecure=0",
    "automation=0",
    "ephemeral=0",
    "cert_spki=",
    "local_state_patch=1",
    "osascript=1",
    "window_position=100,100",
    "chrome_bin=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "app_name=Google Chrome",
    "log=/0/.jaine/.browser/chrome.log",
    "kill_match=--user-data-dir=/0/\\.jaine/\\.browser/profile($|[[:space:]])",
    "ARGV",
]


def _cksum_of(value):
    """Mirror of the launch.sh key derivation: cksum CRC of the raw key string."""
    out = subprocess.run(["cksum"], input=value.encode(), capture_output=True)
    return int(out.stdout.split()[0])


def _key8(value):
    return format(_cksum_of(value), "08x")


def _auto(args=None, env_override=None, tmpdir=None, dry_run=True, bash="bash"):
    env = {"TMPDIR": tmpdir} if tmpdir else {}
    env.update(env_override or {})
    return _run_launch(args=["--auto-lane"] + (args or []), env_override=env,
                       dry_run=dry_run, bash=bash)


def test_default_dryrun_full_stdout_is_byte_identical(tmp_path):
    """§8.1.1 (R1-F2): the no-flag dry-run stdout is pinned IN FULL — config block
    AND argv — so ANY additive drift (e.g. an accidental auto_lane= key) fails."""
    r = _run_launch(env_override={"TMPDIR": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines == EXPECTED_DEFAULT_CONFIG_LINES + EXPECTED_DEFAULT_ARGV, \
        "no-flag dry-run stdout drifted:\n{}".format(r.stdout)


def test_auto_lane_dryrun_shape(tmp_path):
    """§8.1.2: flag on → auto_lane=1, port=0, derived key8 profile, headless
    default 1, profile_overridden=1, reuse reported with a reason."""
    r = _auto(tmpdir=str(tmp_path))
    assert r.returncode == 0, r.stderr
    cfg, argv = _parse_dryrun(r.stdout)
    assert cfg["auto_lane"] == "1"
    assert cfg["port"] == "0"
    assert cfg["profile"] == str(tmp_path) + "/look-lane-" + _key8(_SENTINEL)
    assert cfg["profile_overridden"] == "1"
    assert cfg["headless"] == "1"
    assert cfg["auto_lane_reuse"] == "0"
    assert cfg["auto_lane_reuse_reason"] == "no-process"
    assert "--remote-debugging-port=0" in argv
    assert "--headless=new" in argv


def test_auto_lane_deterministic_and_ppid_fallback(tmp_path):
    """§8.1.3: sentinel → same profile twice + golden key8; empty session id via
    the centrally allowlisted unsafe route → PPID fallback, well-formed, different."""
    r1 = _auto(tmpdir=str(tmp_path))
    r2 = _auto(tmpdir=str(tmp_path))
    p1 = _parse_dryrun(r1.stdout)[0]["profile"]
    assert p1 == _parse_dryrun(r2.stdout)[0]["profile"]
    assert p1.endswith("look-lane-" + _key8(_SENTINEL))

    env = test_env(set_vars={"CLAUDE_CODE_SESSION_ID": ""},
                   unsafe_allow=("CLAUDE_CODE_SESSION_ID",))
    for k in _LANE_VARS:
        env.pop(k, None)
    env["LOOK_DRY_RUN"] = "1"
    env["TMPDIR"] = str(tmp_path)
    r3 = subprocess.run(["bash", LAUNCH_SCRIPT, "--auto-lane"],
                        capture_output=True, text=True, timeout=10, env=env)
    assert r3.returncode == 0, r3.stderr
    p3 = _parse_dryrun(r3.stdout)[0]["profile"]
    assert re.search(r"/look-lane-[0-9a-f]{8}$", p3), p3
    assert p3 != p1, "PPID fallback produced the sentinel-derived profile"


def test_auto_lane_distinct_tmpdirs_distinct_profiles(tmp_path):
    """§8 preamble (R6-F1): the per-test TMPDIR isolation mechanism itself."""
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    pa = _parse_dryrun(_auto(tmpdir=str(a)).stdout)[0]["profile"]
    pb = _parse_dryrun(_auto(tmpdir=str(b)).stdout)[0]["profile"]
    assert pa != pb


def test_auto_lane_headless_precedence(tmp_path):
    """§8.1.4 (R1-F8): arg > LOOK_HEADLESS presence > auto-lane default 1."""
    assert _parse_dryrun(_auto(["--headful"], tmpdir=str(tmp_path)).stdout)[0]["headless"] == "0"
    assert _parse_dryrun(_auto(env_override={"LOOK_HEADLESS": "0"}, tmpdir=str(tmp_path)).stdout)[0]["headless"] == "0"
    assert _parse_dryrun(_auto(env_override={"LOOK_HEADLESS": "1"}, tmpdir=str(tmp_path)).stdout)[0]["headless"] == "1"
    assert _parse_dryrun(_auto(tmpdir=str(tmp_path)).stdout)[0]["headless"] == "1"


@pytest.mark.parametrize("port", ["9350", "9333", "0", ""])
def test_auto_lane_rejects_env_cdp_port_with_attribution(port, tmp_path):
    """§8.1.5 (R2-F2): presence semantics — every legacy-surviving value errors
    with the AUTO-LANE text, not the SP4 ephemeral-gate text."""
    r = _auto(env_override={"CDP_PORT": port}, tmpdir=str(tmp_path))
    assert r.returncode != 0
    assert "--auto-lane" in r.stderr, r.stderr
    assert "ephemeral lane" not in r.stderr.split("--auto-lane")[0], \
        "SP4 gate fired before the auto-lane exclusion:\n" + r.stderr


def test_auto_lane_garbage_port_keeps_legacy_error(tmp_path):
    """§8.1.5 carve-out: legacy-invalid CDP_PORT dies in legacy validation
    (pinned accepted outcome — the invariant is no-launch)."""
    r = _auto(env_override={"CDP_PORT": "abc"}, tmpdir=str(tmp_path))
    assert r.returncode != 0
    assert "CDP_PORT must be an integer" in r.stderr


def test_auto_lane_combined_daily_override_keeps_legacy_gate(tmp_path):
    """§8.1.5 carve-out (R2-F2 v3): combined valid port + daily profile trips the
    unconditional #160 SAFETY gate pre-parser; legacy diagnostic pinned."""
    r = _auto(env_override={"CDP_PORT": "9350", "LOOK_PROFILE_DIR": DAILY_PROFILE},
              tmpdir=str(tmp_path))
    assert r.returncode != 0
    assert "DAILY" in r.stderr, r.stderr


@pytest.mark.parametrize("profile", ["/tmp/x", ""])
def test_auto_lane_rejects_env_profile_dir(profile, tmp_path):
    r = _auto(env_override={"LOOK_PROFILE_DIR": profile}, tmpdir=str(tmp_path))
    assert r.returncode != 0
    assert "--auto-lane" in r.stderr, r.stderr


def test_auto_lane_rejects_automation(tmp_path):
    """§8.1.5: both spellings; error points at the CfT ephemeral path."""
    r = _auto(["--automation"], tmpdir=str(tmp_path))
    assert r.returncode != 0 and "CDP_PORT=0 --automation" in r.stderr
    r = _auto(env_override={"LOOK_AUTOMATION": "1"}, tmpdir=str(tmp_path))
    assert r.returncode != 0 and "CDP_PORT=0 --automation" in r.stderr


def test_auto_lane_composes_with_insecure_and_cert(tmp_path):
    """§8.1.6: the auto-lane profile satisfies the isolated-lane gates."""
    r = _auto(["--insecure"], tmpdir=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _parse_dryrun(r.stdout)[0]["insecure"] == "1"
    pin = "A" * 43 + "="
    r = _auto(["--cert-spki=" + pin], tmpdir=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert _parse_dryrun(r.stdout)[0]["cert_spki"] == pin


def test_auto_lane_backslash_tmpdir_rejected(tmp_path):
    bad = str(tmp_path) + "/bad\\dir"
    os.makedirs(bad, exist_ok=True)
    r = _auto(tmpdir=bad)
    assert r.returncode != 0
    assert "backslash" in r.stderr


def test_auto_lane_daily_alias_fail_closed(tmp_path):
    """§8.1.7 (R1-F3): a TMPDIR whose derived profile resolves into the daily
    profile must die in the arm's re-check, not launch."""
    (tmp_path / ("look-lane-" + _key8(_SENTINEL))).symlink_to(DAILY_PROFILE)
    r = _auto(tmpdir=str(tmp_path))
    assert r.returncode != 0
    assert "daily" in r.stderr.lower()


@pytest.mark.parametrize("case", ["shape", "exclusion", "golden"])
def test_auto_lane_bash32(case, tmp_path):
    """§8.1.8: macOS system bash 3.2 runs the same arms."""
    if case == "shape":
        r = _auto(tmpdir=str(tmp_path), bash="/bin/bash")
        assert r.returncode == 0 and _parse_dryrun(r.stdout)[0]["auto_lane"] == "1"
    elif case == "exclusion":
        r = _auto(env_override={"CDP_PORT": "9350"}, tmpdir=str(tmp_path), bash="/bin/bash")
        assert r.returncode != 0 and "--auto-lane" in r.stderr
    else:
        r = _auto(tmpdir=str(tmp_path), bash="/bin/bash")
        assert _parse_dryrun(r.stdout)[0]["profile"].endswith(_key8(_SENTINEL))


def test_auto_lane_lane_fail_log_carries_marker(tmp_path):
    """§8.1.9: auto-lane failure line carries auto_lane=1; no-flag line does not."""
    log = tmp_path / "drive.log"
    r = _auto(dry_run=False, tmpdir=str(tmp_path),
              env_override={"CHROME_BIN": "/nonexistent-chrome",
                            "BULLDOZER_DRIVE_LOG": str(log)})
    assert r.returncode != 0
    text = log.read_text()
    assert "lane-fail" in text and "auto_lane=1" in text

    log2 = tmp_path / "drive2.log"
    r = _run_launch(dry_run=False,
                    env_override={"CDP_PORT": "9350", "TMPDIR": str(tmp_path),
                                  "CHROME_BIN": "/nonexistent-chrome",
                                  "BULLDOZER_DRIVE_LOG": str(log2)})
    assert r.returncode != 0
    assert "auto_lane" not in log2.read_text()


def test_auto_lane_headful_window_position_from_key8(tmp_path):
    """§8.1.10 (R1-F3): headful placement derives from key8 (pre-launch input),
    pinned at dry-run AND argv level."""
    r = _auto(["--headful"], tmpdir=str(tmp_path))
    cfg, argv = _parse_dryrun(r.stdout)
    off = _cksum_of(_SENTINEL) % 1200
    want = "{},{}".format(100 + off, 100 + off)
    assert cfg["window_position"] == want
    assert "--window-position=" + want in argv


def test_auto_lane_stale_devtools_file_fail_closed(tmp_path):
    """§8.1.11 (R1-F1): un-removable stale DevToolsActivePort → exit 1 BEFORE any
    spawn, stderr names the stale file."""
    profile = tmp_path / ("look-lane-" + _key8(_SENTINEL))
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("9999\n/devtools/browser/x")
    profile.chmod(0o555)
    try:
        r = _auto(dry_run=False, tmpdir=str(tmp_path),
                  env_override={"CHROME_BIN": "/usr/bin/true",
                                "BULLDOZER_DRIVE_LOG": str(tmp_path / "d.log")})
        assert r.returncode != 0
        assert "DevToolsActivePort" in r.stderr
    finally:
        profile.chmod(0o755)


def _decoy(profile, extra_args):
    """A main-process decoy: argv carries the profile + signature flags, no --type=."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)",
         "--user-data-dir=" + str(profile)] + extra_args)


@pytest.mark.parametrize("decoy_flags,reason", [
    ([], "config-mismatch"),                                   # headless-only diff (request defaults headless=1)
    (["--headless=new", "--disable-web-security"], "config-mismatch"),  # insecure-only diff
    (["--headless=new", "--ignore-certificate-errors-spki-list=" + "A" * 43 + "="], "config-mismatch"),  # cert-only diff
    (["--headless=new", "--type=renderer"], "no-process"),     # child process, not main
    (["--headless=new"], "unhealthy"),                         # full match, no DevToolsActivePort
])
def test_auto_lane_pass0_signature_arms(decoy_flags, reason, tmp_path):
    """§8.1.12 (R2-F1): per-field cmdline-signature classification via
    auto_lane_reuse_reason; argv[0] is NOT compared (§4.4)."""
    profile = tmp_path / ("look-lane-" + _key8(_SENTINEL))
    profile.mkdir()
    proc = _decoy(profile, decoy_flags)
    try:
        import time as _t
        _t.sleep(0.3)  # let the decoy appear in the process table
        r = _auto(tmpdir=str(tmp_path))
        assert r.returncode == 0, r.stderr
        cfg = _parse_dryrun(r.stdout)[0]
        assert cfg["auto_lane_reuse"] == "0"
        assert cfg["auto_lane_reuse_reason"] == reason, r.stdout
    finally:
        proc.kill()


def _fake_json_version(uuid, port_holder):
    """Test-owned /json/version endpoint (§8.1.13)."""
    import http.server, threading, json as _json

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = _json.dumps({"webSocketDebuggerUrl":
                                "ws://localhost:{}/devtools/browser/{}".format(
                                    self.server.server_address[1], uuid)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port_holder.append(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("uuid_matches,reuse,reason", [
    (False, "0", "identity-mismatch"),
    (True, "1", "ok"),
])
def test_auto_lane_pass0_identity_binding(uuid_matches, reuse, reason, tmp_path):
    """§8.1.13 (R1-F1): the answering endpoint's browser uuid must equal the
    file's line 2 — a recycled-port impostor is never reused, a match is."""
    profile = tmp_path / ("look-lane-" + _key8(_SENTINEL))
    profile.mkdir()
    ports = []
    srv = _fake_json_version("uuid-real" if uuid_matches else "uuid-impostor", ports)
    (profile / "DevToolsActivePort").write_text(
        "{}\n/devtools/browser/uuid-real".format(ports[0]))
    proc = _decoy(profile, ["--headless=new"])
    try:
        import time as _t
        _t.sleep(0.3)
        r = _auto(tmpdir=str(tmp_path))
        assert r.returncode == 0, r.stderr
        cfg = _parse_dryrun(r.stdout)[0]
        assert cfg["auto_lane_reuse"] == reuse, r.stdout
        assert cfg["auto_lane_reuse_reason"] == reason, r.stdout
    finally:
        proc.kill()
        srv.shutdown()


def test_port_registry_untouched_by_auto_lane():
    """§8.5: the port-0 model claims ZERO registry edits — pin it."""
    conftest_src = (Path(__file__).parent / "conftest.py").read_text()
    assert "9340-9349" in conftest_src and "DRIVE_TEST_PORT = 9359" in conftest_src
    lanes_src = (Path(__file__).parent / "test_e2e_lanes.py").read_text()
    assert "range(9330, 9370)" in lanes_src


def test_auto_lane_signature_ignores_url_substring(tmp_path):
    """codex-review F1: switch detection must be token-boundary — a URL argv
    token CONTAINING '--disable-web-security' is not a Chrome switch."""
    profile = tmp_path / ("look-lane-" + _key8(_SENTINEL))
    profile.mkdir()
    proc = _decoy(profile, ["--headless=new", "http://x.test/?q=--disable-web-security"])
    try:
        import time as _t
        _t.sleep(0.3)
        r = _auto(tmpdir=str(tmp_path))
        assert r.returncode == 0, r.stderr
        cfg = _parse_dryrun(r.stdout)[0]
        assert cfg["auto_lane_reuse_reason"] != "config-mismatch", \
            "URL substring misread as a real switch:\n" + r.stdout
        assert cfg["auto_lane_reuse_reason"] == "unhealthy"  # full sig match, no DevToolsActivePort
    finally:
        proc.kill()


def test_auto_lane_reuse_revalidates_before_contract(tmp_path):
    """codex-review F2: between pass 0 and the contract print another call can
    kill/relaunch the lane — the reuse exit must revalidate identity and fail
    loud instead of printing a dead port. Modeled with a one-shot endpoint:
    pass 0 consumes the only answer, revalidation gets nothing."""
    import http.server, threading, json as _json

    class OneShot(http.server.BaseHTTPRequestHandler):
        served = [0]
        def do_GET(self):
            if self.served[0]:
                self.send_response(404); self.end_headers(); return
            self.served[0] = 1
            body = _json.dumps({"webSocketDebuggerUrl":
                                "ws://localhost:{}/devtools/browser/uuid-real".format(
                                    self.server.server_address[1])}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), OneShot)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    profile = tmp_path / ("look-lane-" + _key8(_SENTINEL))
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(
        "{}\n/devtools/browser/uuid-real".format(port))
    proc = _decoy(profile, ["--headless=new"])
    try:
        import time as _t
        _t.sleep(0.3)
        r = _auto(dry_run=False, tmpdir=str(tmp_path),
                  env_override={"BULLDOZER_DRIVE_LOG": str(tmp_path / "d.log")})
        assert r.returncode != 0, \
            "reuse printed a contract without revalidation:\n" + r.stdout
        assert "LANE_REUSED=1" not in r.stdout
    finally:
        proc.kill()
        srv.shutdown()


def test_auto_lane_whitespace_tmpdir_rejected(tmp_path):
    """codex-review r2 F3: ps flattens argv, so a PROFILE_DIR containing
    whitespace could embed switch-shaped text inside one argv element and
    confuse the cmdline signature. Refuse fail-loud (backslash-guard mirror)."""
    bad = str(tmp_path) + "/has space"
    os.makedirs(bad, exist_ok=True)
    r = _auto(tmpdir=bad)
    assert r.returncode != 0
    assert "whitespace" in r.stderr, r.stderr
