"""Shared fixtures for bulldozer tests.

E2E tests need a running JAINE Browser. The `jaine_browser` fixture
reuses an already-running browser or launches one via launch.sh.

The `slow` marker (used by `tests/test_check_e2e.py`) is registered here so
running `pytest` without `-m slow` doesn't print PytestUnknownMarkWarning.
Slow tests are not deselected by default — register a default filter via
`-m "not slow"` if you want fast runs only.
"""
import atexit
import fcntl
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

# ── Test-log isolation, D1 (#357) ────────────────────────────────────────────
# MODULE level, not a fixture: conftest imports BEFORE collection imports test
# modules, so producers that freeze their log path at import time (spec F2 —
# cdp.py, require-workflow-skill.py, consult_panel.py) capture the redirect,
# and every subprocess inherits it via os.environ. Restore runs via atexit,
# which also covers the xdist controller / zero-selected / collection-error
# paths where no fixture ever executes (spec F8).
# Spec: docs/superpowers/specs/2026-07-20-test-log-isolation-design.md

_KNOB_TO_LOG_NAME = {
    "BULLDOZER_LOG": "bulldozer.log",
    "BULLDOZER_CODEX_LOG": "bulldozer-codex.log",
    "BULLDOZER_LOOK_LOG": "bulldozer-look.log",
    "BULLDOZER_CONSULT_LOG": "bulldozer-consult.log",
    "BULLDOZER_DRIVE_LOG": "bulldozer-drive.log",
    "WORKFLOW_HOOK_LOG": "require-workflow-skill.log",
}
# Full override surface is 7 knobs (spec F1): 6 files + one DIRECTORY.
LOG_ISOLATION_KNOBS = tuple(_KNOB_TO_LOG_NAME) + ("BULLDOZER_INVOKE_LOG_DIR",)
PRODUCTION_LOG_DIR = Path.home() / ".claude" / "hooks"
PRODUCTION_LOG_NAMES = tuple(_KNOB_TO_LOG_NAME.values())

_SAVED_LOG_ENV = {
    k: os.environ.get(k)
    for k in LOG_ISOLATION_KNOBS + ("CLAUDE_CODE_SESSION_ID",
                                    "BULLDOZER_TEST_SENTINEL",
                                    "BULLDOZER_TEST_SENTINEL_SPAWNER")
}

TEST_LOG_DIR = Path(tempfile.mkdtemp(prefix="bulldozer-test-logs-"))
for _knob, _log_name in _KNOB_TO_LOG_NAME.items():
    os.environ[_knob] = str(TEST_LOG_DIR / _log_name)
_invoke_dir = TEST_LOG_DIR / "invoke"
_invoke_dir.mkdir()
os.environ["BULLDOZER_INVOKE_LOG_DIR"] = str(_invoke_dir)

# Session sentinel, adopt-protocol (spec D1.3): adopt the inherited value ONLY
# in a positively identified xdist worker AND only in exact wire form (writers
# truncate session to 8 chars via _session_token — a longer value would never
# appear on the wire). "Positively identified" needs THREE conjuncts:
# PYTEST_XDIST_WORKER is inherited by every descendant of a worker, so a nested
# pytest spawned FROM a worker (the shim runs in the guard tests) would
# masquerade as one — the SPAWNER pid check disambiguates: execnet spawns
# workers as DIRECT children of the controller, a nested run's parent is the
# worker. Controller / non-xdist / nested / malformed → regenerate. Both vars
# are re-pointed unconditionally so they can never diverge.
_SENTINEL_WIRE_FORM = re.compile(r"^PT[0-9a-f]{6}$")
_inherited_sentinel = os.environ.get("BULLDOZER_TEST_SENTINEL", "")
if ("PYTEST_XDIST_WORKER" in os.environ
        and _SENTINEL_WIRE_FORM.match(_inherited_sentinel)
        and os.environ.get("BULLDOZER_TEST_SENTINEL_SPAWNER") == str(os.getppid())):
    TEST_SENTINEL = _inherited_sentinel
else:
    TEST_SENTINEL = "PT" + secrets.token_hex(3)
    os.environ["BULLDOZER_TEST_SENTINEL_SPAWNER"] = str(os.getpid())
os.environ["BULLDOZER_TEST_SENTINEL"] = TEST_SENTINEL
os.environ["CLAUDE_CODE_SESSION_ID"] = TEST_SENTINEL

# Baselines for the D2 scan — recorded HERE, not at fixture setup, so a
# collection-time leak falls inside both the timestamp window and the offset
# fast-path window (spec D1.4). st_ino identifies a rotation (rename); size
# alone cannot (a rotated file can regrow past the saved offset).
SESSION_START = time.time()
LOG_BASELINES = {}
for _log_name in PRODUCTION_LOG_NAMES:
    try:
        _st = os.stat(PRODUCTION_LOG_DIR / _log_name)
        LOG_BASELINES[_log_name] = (_st.st_ino, _st.st_size)
    except FileNotFoundError:
        LOG_BASELINES[_log_name] = None


def _restore_log_isolation():
    subprocess.Popen = _REAL_POPEN  # uninstall the D3a-rt chokepoint
    for _k, _v in _SAVED_LOG_ENV.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    shutil.rmtree(TEST_LOG_DIR, ignore_errors=True)


atexit.register(_restore_log_isolation)


def _line_ts_epoch(line):
    """Epoch seconds of a stable-log line's leading ISO field, or None.

    Parsed, never compared lexicographically (spec D2): canonical lines carry a
    colon-offset ISO ts; a naive (legacy-form) ts is assumed local —
    datetime.timestamp() does exactly that. Unparseable → None (a mid-line
    read fragment or pre-canonical garbage; every current producer writes a
    valid leading ts via the canonical helper)."""
    ts = line.split(" | ", 1)[0].strip()
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _read_generations(path, baseline):
    """Text of `path`'s NEW content since `baseline` (spec D2 scan mechanics).

    baseline None (file absent at session start) → full current + full `.1`
    sibling (the ts window suppresses genuinely old lines). Inode changed →
    rotation happened → full current + full `.1`; size alone cannot identify a
    generation (a rotated file can regrow past the saved offset). Same inode,
    size grew → tail from the saved offset (fast path). Read under a brief
    SHARED flock on the writer's `<log>.lock` sibling so a concurrent
    os.replace rotation cannot be raced mid-read.

    Best-effort boundary (spec D2): TWO rotations of one log within a single
    pytest run would overwrite `.1` and lose the middle generation — with
    tests redirected to tmp, real-log growth comes only from foreign writers
    (~hundreds of lines/day vs the 5 MB cap); not chased."""
    lock_fh = None
    try:
        # Lock ONLY when the writer's lock file already exists — the scan must
        # never CREATE files in the production dir (it is a reader). No lock
        # file ⇒ no canonical writer has touched this log; scan unlocked.
        # O_APPEND WITHOUT O_CREAT makes that atomic — no exists→open TOCTOU
        # window that could recreate a just-deleted lock (Copilot, PR #358).
        lock_path = str(path) + ".lock"
        try:
            lock_fh = os.fdopen(os.open(lock_path, os.O_WRONLY | os.O_APPEND), "a")
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
        except OSError:
            lock_fh = None  # absent lock / lock unavailable → scan unlocked
        texts = []

        def _full(p):
            try:
                with open(p, "r", errors="replace") as fh:
                    texts.append(fh.read())
            except OSError:
                pass

        try:
            st = os.stat(path)
        except FileNotFoundError:
            st = None
        rotated = Path(str(path) + ".1")
        if baseline is None:
            if st:
                _full(path)
            if rotated.exists():
                _full(rotated)
        else:
            ino, size = baseline
            if st is None:
                if rotated.exists():
                    _full(rotated)
            elif st.st_ino != ino:
                if rotated.exists():
                    _full(rotated)
                _full(path)
            elif st.st_size > size:
                try:
                    with open(path, "r", errors="replace") as fh:
                        fh.seek(size)
                        texts.append(fh.read())
                except OSError:
                    pass
            elif st.st_size < size:
                _full(path)  # same-inode shrink: abnormal — rescan, ts window filters
        return "".join(texts)
    finally:
        if lock_fh:
            lock_fh.close()  # releases the flock


def scan_for_leaks(log_dir, sentinel, since, baselines, markers):
    """D2 guard core (#357): pure leak scanner over stable-log files.

    Returns offending lines (prefixed with the file name), [] when clean. A
    line is a leak iff its ts >= since - 1s AND any detector fires:
      1. session=<sentinel>   — primary (every env-derived writer, spec F7);
      2. session=cafebabe     — the reserved explicit-test-session id (F9/F10);
      3. any caller marker substring (private tmp path, pytest-of-it,
         artifact=test forms) — secondary content attribution.
    All inputs explicit (target dir, baselines) — no env seam by design
    (R1-F6: an env seam is an unauthenticated kill switch)."""
    threshold = since - 1.0
    sentinel_token = "session=" + sentinel
    offenders = []
    for name, baseline in baselines.items():
        path = Path(log_dir) / name
        for line in _read_generations(path, baseline).splitlines():
            ts = _line_ts_epoch(line)
            if ts is None or ts < threshold:
                continue
            if (sentinel_token in line
                    or "session=cafebabe" in line
                    or any(m in line for m in markers)):
                offenders.append("{}: {}".format(name, line))
    return offenders


# Secondary content markers for the authoritative scan (D2 detectors 3-4).
LEAK_MARKERS = [
    "/pytest-of-",             # tmp_path factory paths (user-agnostic prefix)
    "artifact=test |",         # the two historical polluted classes — exact-field
    "artifact=test-artifact",  # forms so a real artifact=tests/foo.py never matches
    str(TEST_LOG_DIR),         # this process's private redirect dir
]

# The three producers that freeze their log path at IMPORT time (spec F2).
_IMPORT_FROZEN_PRODUCERS = (
    ("skills/look/scripts/cdp.py", "LOG_FILE"),
    ("hooks/require-workflow-skill.py", "LOG"),
    ("skills/consult/scripts/consult_panel.py", "CONSULT_LOG"),
)


def _import_frozen_problems():
    """Start-assert (b): the import-frozen producer constants captured the redirect.

    sys.modules-first (R1-F2 r3): every ALREADY-LOADED instance of a producer is
    checked — a fresh importlib load proves nothing about the instance tests
    actually use. The fresh probe (never registered in sys.modules) runs only
    when no instance is loaded, proving what a future import WOULD capture."""
    import importlib.util
    problems = []
    tld = str(TEST_LOG_DIR)
    for rel, attr in _IMPORT_FROZEN_PRODUCERS:
        path = (Path(__file__).parent.parent / rel).resolve()
        loaded = [m for m in list(sys.modules.values())
                  if getattr(m, "__file__", None)
                  and Path(m.__file__).resolve() == path]
        probes = [("loaded", m) for m in loaded]
        if not probes:
            probe_name = "_bdz_frozen_probe_" + Path(rel).stem.replace("-", "_")
            spec = importlib.util.spec_from_file_location(probe_name, str(path))
            mod = importlib.util.module_from_spec(spec)
            # Registered under the throwaway probe name DURING exec (py3.14
            # dataclasses resolve annotations via sys.modules[cls.__module__])
            # and removed right after — zero lasting sys.modules pollution.
            sys.modules[probe_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as e:  # noqa: BLE001 — report, don't crash the assert
                problems.append("{}: fresh-import probe failed: {!r}".format(rel, e))
                continue
            finally:
                sys.modules.pop(probe_name, None)
            probes = [("fresh-import", mod)]
        for source, mod in probes:
            val = getattr(mod, attr, None)
            if val is None or not str(val).startswith(tld):
                problems.append(
                    "{} ({}): {}={!r} escaped the redirect (expected under {})"
                    .format(rel, source, attr, val, tld))
    return problems


def _start_assert_problems():
    """Start-assert (a)+(b): redirect in effect, sentinel coherent, frozen
    constants captured. Returns human-readable problems, [] when healthy."""
    problems = []
    tld = str(TEST_LOG_DIR)
    for knob in LOG_ISOLATION_KNOBS:
        val = os.environ.get(knob)
        if not val or not val.startswith(tld):
            problems.append(
                "{}={!r} — not inside the private redirect dir {}".format(
                    knob, val, tld))
    for var in ("CLAUDE_CODE_SESSION_ID", "BULLDOZER_TEST_SENTINEL"):
        if os.environ.get(var) != TEST_SENTINEL:
            problems.append("{}={!r} != session sentinel {!r}".format(
                var, os.environ.get(var), TEST_SENTINEL))
    return problems + _import_frozen_problems()


def pytest_sessionfinish(session, exitstatus):
    """D2 authoritative leak scan (#357).

    Runs in EVERY process that imported this conftest — xdist workers AND the
    controller (which finishes last), zero-selected runs, collection errors —
    exactly the cases where no fixture ever executes (spec F8). Offenders are
    printed verbatim and the run is forced non-zero: a leak is an emergency,
    not a routine assert."""
    offenders = scan_for_leaks(PRODUCTION_LOG_DIR, TEST_SENTINEL, SESSION_START,
                               LOG_BASELINES, LEAK_MARKERS)
    if offenders:
        sys.stderr.write(
            "\n=== TEST-LOG LEAK GUARD (#357): {} test-origin line(s) leaked "
            "into {} ===\n  {}\nA test wrote into the production stable logs — "
            "fix the leak site (spec: docs/superpowers/specs/"
            "2026-07-20-test-log-isolation-design.md).\n".format(
                len(offenders), PRODUCTION_LOG_DIR, "\n  ".join(offenders)))
        session.exitstatus = 1


# ── Test-log isolation, D3 (#357) ────────────────────────────────────────────
# The redirect cannot protect a child whose env is CONSTRUCTED without the
# knobs (fallback → real $HOME). Three layers close the accidental-
# misconstruction class (deliberate evasion is out of scope by declaration —
# spec §4 D3 threat model): the test_env builder (ergonomics + protected-drop
# checks), the runtime Popen chokepoint (repair, not rejection), and two
# static scans in tests/test_log_isolation_guard.py. All consult the ONE
# central allowlist below.

PROTECTED_ENV_VARS = LOG_ISOLATION_KNOBS + ("CLAUDE_CODE_SESSION_ID",)

# THE central allowlist (D3a/D3b). Every entry carries a justification; a
# callsite file absent from the relevant section cannot self-authorize
# (R1-F5 r4). Sections:
#   unsafe_env           — (var|"*", callsite file, why) test_env may drop/empty
#                          that protected var when called from that file
#   env_forward_helpers  — (helper name, defining file, why) scan-1 permits the
#                          helper's internal `env=<local>` forward; its build
#                          MUST go through test_env
#   session_literals     — (relative file, why) scan-2 permits non-cafebabe99
#                          session literals in that file (hermetic tmp-only
#                          unit tests of the logging path itself, spec F10)
CENTRAL_ALLOWLIST = {
    "unsafe_env": (
        ("*", "test_log_isolation_guard.py",
         "scratch-HOME leak repros (T2/T4/T7) + sentinel-protocol probes"),
        ("BULLDOZER_DRIVE_LOG", "test_drive_logging_pr5.py",
         "intentional DRIVE_LOG-absent fallback routing through "
         "BULLDOZER_INVOKE_LOG_DIR under a scratch HOME (R10-F1)"),
        ("CLAUDE_CODE_SESSION_ID", "test_launch.py",
         "auto-lane PPID-fallback derivation (#187 §8.1.3) — dry-run only, "
         "no log writer runs in the child"),
    ),
    "env_forward_helpers": (
        ("_env", "test_check_logging_pr4.py",
         "pinned env-builder — single internal test_env(set_vars=…) call"),
        ("_child_env_dump", "test_log_isolation_guard.py",
         "chokepoint probe — forwards RAW env shapes BY DESIGN (the shapes "
         "ARE the test subject; internal test_env would defeat the premise)"),
    ),
    "session_literals": (
        ("tests/test_bulldozer_log.py",
         "unit tests of _session_token/append_line themselves — adversarial "
         "ids, explicit tmp log paths only"),
        ("tests/test_consult_panel.py",
         "one adversarial-session sanitization test, tmp-only"),
        ("tests/test_log_isolation_guard.py",
         "sentinel-protocol probes (stale PT-form values, divergence checks)"),
    ),
}


def _unsafe_authorized(var, caller_file):
    for allowed_var, allowed_file, _why in CENTRAL_ALLOWLIST["unsafe_env"]:
        if allowed_file == caller_file and allowed_var in ("*", var):
            return True
    return False


def test_env(drop=(), set_vars=None, unsafe_allow=(), scrub=False):
    """D3a (#357): the ONLY sanctioned way to build a modified subprocess env.

    Starts from os.environ.copy() (which carries the D1 redirect), applies
    set_vars, drops `drop` — and raises if any operation would remove or empty
    a PROTECTED var (directly, via loop variable, or set_vars={K: ""} — F11)
    unless that var is named in unsafe_allow AND this callsite file is pinned
    in CENTRAL_ALLOWLIST["unsafe_env"]. scrub=True starts from a minimal env
    instead — which still carries the redirect knobs, so even a deliberately
    minimal child that unexpectedly launches a stable-log writer lands in tmp.
    set_vars value None unsets the var."""
    import inspect
    caller_file = Path(inspect.currentframe().f_back.f_code.co_filename).name
    for var in unsafe_allow:
        if not _unsafe_authorized(var, caller_file):
            raise RuntimeError(
                "test_env: unsafe_allow of {!r} from {!r} is not pinned in "
                "CENTRAL_ALLOWLIST['unsafe_env'] (tests/conftest.py) — add an "
                "entry with a justification".format(var, caller_file))
    if scrub:
        env = {k: os.environ[k]
               for k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
               if k in os.environ}
        for k in PROTECTED_ENV_VARS:
            if k in os.environ:
                env[k] = os.environ[k]
    else:
        env = os.environ.copy()
    for var in drop:
        if var in PROTECTED_ENV_VARS and var not in unsafe_allow:
            raise RuntimeError(
                "test_env: dropping protected {!r} — the producer would fall "
                "back to the real production log; name it in unsafe_allow and "
                "pin this file in CENTRAL_ALLOWLIST if genuinely intended"
                .format(var))
        env.pop(var, None)
    for var, val in (set_vars or {}).items():
        if (var in PROTECTED_ENV_VARS and (val is None or val == "")
                and var not in unsafe_allow):
            raise RuntimeError(
                "test_env: emptying protected {!r} — an empty string falls "
                "through `os.environ.get(K) or default` exactly like a "
                "removal (F11)".format(var))
        if val is None:
            env.pop(var, None)
        else:
            env[var] = str(val)
    return env


test_env.__test__ = False  # helper named per spec — never collect as a test


# D3a-rt: runtime process-creation chokepoint, REPAIR-based (spec §4). Installed
# at MODULE level (same import-ordering argument as D1) so collection-time
# creations are covered; restored by the same atexit handler. Enforces the real
# invariant — no child of the test process may resolve a stable log into the
# real Path.home()/.claude/hooks — at the only place it can be guaranteed.
_REDIRECT_VALUES = {k: os.environ[k] for k in LOG_ISOLATION_KNOBS}
_REAL_HOME = Path.home().resolve()
_REAL_POPEN = subprocess.Popen


def _resolves_production(val):
    """True when `val` names a path at or under the real production log dir."""
    if not val:
        return False
    try:
        resolved = Path(val).resolve()
    except OSError:
        return False
    prod = PRODUCTION_LOG_DIR.resolve()
    return resolved == prod or str(resolved).startswith(str(prod) + os.sep)


def _repair_env_for_child(argv, env):
    """Repair (never reject) a child env per D3a-rt; returns the env to use.

    env=None → inherit, but ASSERT the parent os.environ knobs still resolve
    non-production (R10-F2 — an unsanctioned in-process mutation must not ride
    the inherit branch). Foreign HOME → the child's Path.home() fallback is
    sandboxed: absent/empty knobs stay absent (the T2/T4 scratch-HOME repros
    keep their RED premise), but an EXPLICIT production-resolving knob outranks
    the fallback in every producer (`os.environ.get(K) or default`) and is
    repaired anyway. Otherwise → inject the session redirect for each knob
    that is absent, empty, or production-resolving, into a COPY (the caller's
    dict is never mutated); a per-test tmp re-point is non-production and is
    preserved. A literal `env -i` argv prefix raises — command-level clearing
    cannot be repaired."""
    head = list(argv[:2]) if isinstance(argv, (list, tuple)) else []
    if (len(head) == 2 and os.path.basename(str(head[0])) == "env"
            and str(head[1]) == "-i"):
        raise RuntimeError(
            "subprocess launches an `env -i` child — command-level env "
            "clearing cannot be repaired; build the env with test_env() "
            "instead (#357 D3a-rt)")
    if env is None:
        for k in LOG_ISOLATION_KNOBS:
            val = os.environ.get(k)
            if not val or _resolves_production(val):
                raise RuntimeError(
                    "env=None inherit with mutated parent os.environ: {}={!r} "
                    "would reach the production stable logs (#357 R10-F2)"
                    .format(k, val))
        return None
    # RELATIVE paths are unclassifiable from the parent (post-impl codex review
    # P2): the child resolves them against ITS cwd — a relative HOME or knob
    # with cwd at the real home lands IN the production dir while our resolve()
    # (against pytest's cwd) reads it as non-production. So: a relative HOME is
    # never a sanctioned foreign sandbox, and a relative knob value is always
    # repaired.
    home = env.get("HOME")
    try:
        foreign_home = (bool(home) and os.path.isabs(home)
                        and Path(home).resolve() != _REAL_HOME)
    except OSError:
        foreign_home = True
    repaired = None
    for k in LOG_ISOLATION_KNOBS:
        val = env.get(k)
        relative = bool(val) and not os.path.isabs(val)
        if not relative:
            if foreign_home:
                if not _resolves_production(val):
                    continue
            elif val and not _resolves_production(val):
                continue
        if repaired is None:
            repaired = dict(env)
        repaired[k] = _REDIRECT_VALUES[k]
    return repaired if repaired is not None else env


class _IsolationGuardedPopen(subprocess.Popen):
    def __init__(self, args, *pargs, **kwargs):
        if len(pargs) >= 10:  # env passed positionally (10th after args)
            pargs = list(pargs)
            pargs[9] = _repair_env_for_child(args, pargs[9])
            pargs = tuple(pargs)
        else:
            kwargs["env"] = _repair_env_for_child(args, kwargs.get("env"))
        super().__init__(args, *pargs, **kwargs)


subprocess.Popen = _IsolationGuardedPopen
# ── end test-log isolation D1+D2+D3 ──────────────────────────────────────────


def pytest_configure(config):
    """Register custom markers used across the bulldozer test suite."""
    config.addinivalue_line(
        "markers",
        "slow: tests that take >10s — typically because they invoke a real "
        "external service (codex, JAINE Browser, network). Run with `-m slow` "
        "to include explicitly.",
    )

import pytest


@pytest.fixture(scope="session", autouse=True)
def _log_isolation_start_assert():
    """D2 early start-assert (#357): fail loudly BEFORE any test runs when the
    redirect is not in effect. The AUTHORITATIVE scan is pytest_sessionfinish —
    fixtures never execute on the xdist controller (spec F8)."""
    problems = _start_assert_problems()
    if problems:
        pytest.fail("test-log isolation start-assert (#357):\n  "
                    + "\n  ".join(problems))
    yield


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
    set_vars = {"CDP_PORT": str(CDP_PORT)}
    set_vars.update(env_override or {})
    return subprocess.run(
        [sys.executable, CDP_SCRIPT] + args,
        capture_output=True, text=True, timeout=timeout,
        env=test_env(set_vars=set_vars),
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
    profile = tempfile.mkdtemp(prefix="jaine-lane-{}-".format(port))
    kill_match = _kill_pattern(profile)
    subprocess.Popen(["bash", LAUNCH_SCRIPT, "about:blank"],
                     env=test_env(drop=LANE_ENV_VARS,
                                  set_vars={"CDP_PORT": str(port),
                                            "LOOK_PROFILE_DIR": profile,
                                            "LOOK_HEADLESS": "1",
                                            "LOOK_AUTOMATION": "1",
                                            "CHROME_APP_NAME": CFT_APP_NAME}),
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
    lane_vars = {"CDP_PORT": str(CDP_PORT)}
    temp_profile = None
    if CDP_PORT == 9333:
        kill_match = _kill_pattern(BROWSER_PROFILE)
    else:
        temp_profile = tempfile.mkdtemp(prefix="jaine-test-{}-".format(CDP_PORT))
        lane_vars["LOOK_PROFILE_DIR"] = temp_profile
        lane_vars["LOOK_HEADLESS"] = "1"
        kill_match = _kill_pattern(temp_profile)
    # DEVNULL, not PIPE: launch.sh redirects Chrome itself into the lane's
    # chrome.log; an unread PIPE could fill (64KB) and block the child.
    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"],
        env=test_env(drop=LANE_ENV_VARS, set_vars=lane_vars),
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
    temp_profile = tempfile.mkdtemp(prefix="jaine-cft-{}-".format(DRIVE_TEST_PORT))
    kill_match = _kill_pattern(temp_profile)
    # DEVNULL, not PIPE: launch.sh redirects Chrome itself into the lane's
    # chrome.log; an unread PIPE could fill (64KB) and block the child.
    subprocess.Popen(
        ["bash", LAUNCH_SCRIPT, "about:blank"],
        env=test_env(drop=LANE_ENV_VARS,
                     set_vars={"CDP_PORT": str(DRIVE_TEST_PORT),
                               "LOOK_PROFILE_DIR": temp_profile,
                               "LOOK_HEADLESS": "1",
                               "LOOK_AUTOMATION": "1",
                               # lane contract (R1-F3) — explicit > implicit
                               "CHROME_APP_NAME": CFT_APP_NAME}),
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
