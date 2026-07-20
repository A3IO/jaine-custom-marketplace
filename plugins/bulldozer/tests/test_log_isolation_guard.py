"""Guard tests: the test suite must never write into the production stable logs.

Spec: docs/superpowers/specs/2026-07-20-test-log-isolation-design.md (issue #357).
Layers under test: D1 (conftest module-level env redirect + session sentinel),
D2 (scan_for_leaks + sessionfinish scan), D3 (test_env builder, Popen chokepoint,
static scans). Built TDD — each test names its spec row (T1..T7).
"""
import json
import os
import re
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import ast

import pytest

from conftest import (
    CENTRAL_ALLOWLIST,
    _import_frozen_problems,
    _start_assert_problems,
    scan_for_leaks,
    test_env,
)

PLUGIN_ROOT = Path(__file__).parent.parent

# The full env-override surface producers honor (spec §3 F1): 6 file knobs + 1 dir.
LOG_KNOBS = (
    "BULLDOZER_LOG",
    "BULLDOZER_CODEX_LOG",
    "BULLDOZER_LOOK_LOG",
    "BULLDOZER_CONSULT_LOG",
    "BULLDOZER_DRIVE_LOG",
    "WORKFLOW_HOOK_LOG",
    "BULLDOZER_INVOKE_LOG_DIR",
)

# A test known to exercise the wrapper's missing-args writer: _bdz_log falls back
# to $HOME/.claude/hooks/bulldozer.log and emits a wrapper-fail line whose fields
# are byte-identical to a genuine production line (spec §1, R1-F1).
LEAKY_TEST = ("tests/test_check_round_wrapper.py::TestSkeleton::"
              "test_missing_required_args_fails_with_nonzero_exit")


def test_t4_no_leak_into_scratch_home(tmp_path):
    """T4 (spec §5): an inner pytest run of the known-leaking test under a scratch
    $HOME with NO log knobs must leave the scratch stable-log dir empty.

    RED pre-D1: the wrapper-fail line lands in scratch .claude/hooks/bulldozer.log.
    GREEN post-D1: the inner run's own conftest redirect rescues the fallback path.
    Mutation: dropping one knob from D1's redirect list must turn this RED again.
    """
    scratch = tmp_path / "home"
    hooks = scratch / ".claude" / "hooks"
    hooks.mkdir(parents=True)

    # Strip every knob and the sentinel so ONLY the inner run's own conftest can
    # rescue the fallback path — an inherited outer redirect must not mask a
    # broken D1. (This file is pinned in CENTRAL_ALLOWLIST for these drops.)
    env = test_env(
        drop=list(LOG_KNOBS) + ["BULLDOZER_TEST_SENTINEL",
                                "BULLDOZER_TEST_SENTINEL_SPAWNER"],
        unsafe_allow=list(LOG_KNOBS),
        set_vars={"HOME": str(scratch)})

    result = subprocess.run(
        [sys.executable, "-m", "pytest", LEAKY_TEST, "-p", "no:cacheprovider", "-q"],
        cwd=str(PLUGIN_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "inner pytest run failed — T4 needs the leaking test itself green:\n"
        + result.stdout + result.stderr
    )

    leaked = sorted(p.name for p in hooks.iterdir())
    assert leaked == [], (
        "test run leaked into the scratch production log dir: {}\nevidence:\n{}".format(
            leaked,
            "\n".join(
                (hooks / n).read_text().splitlines()[0]
                for n in leaked
                if (hooks / n).is_file() and (hooks / n).read_text()
            ),
        )
    )


# ── T1/T3: scan_for_leaks unit tests (spec §5, D2 mechanics) ─────────────────

SENTINEL = "PTaaaaaa"  # exact wire form: "PT" + 6 hex, 8 chars total


def _iso(offset_s=0):
    """Local-tz ISO timestamp `offset_s` from now — the writers' leading field."""
    return (datetime.now().astimezone() + timedelta(seconds=offset_s)).isoformat(
        timespec="seconds")


def _write(log, *lines):
    with open(log, "a") as fh:
        for ln in lines:
            fh.write(ln + "\n")


def _fresh_line(session=SENTINEL, event="audit", tail="proposed=1", offset_s=0):
    return "{} | event={} | session={} | {}".format(_iso(offset_s), event, session, tail)


def test_t1_reports_fresh_sentinel_line(tmp_path):
    line = _fresh_line()
    _write(tmp_path / "bulldozer.log", line)
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5,
                          {"bulldozer.log": None}, [])
    assert len(hits) == 1 and line in hits[0]


def test_t1_ignores_historical_sentinel_line(tmp_path):
    """Pins the timestamp filter: a marked line from a PAST run must not fire."""
    _write(tmp_path / "bulldozer.log",
           "2020-01-01T00:00:00+00:00 | event=audit | session={} | proposed=1".format(
               SENTINEL))
    assert scan_for_leaks(tmp_path, SENTINEL, time.time() - 5,
                          {"bulldozer.log": None}, []) == []


def test_t1_ignores_fresh_foreign_line(tmp_path):
    """A concurrent production writer's line (foreign session, no markers) passes."""
    _write(tmp_path / "bulldozer.log", _fresh_line(session="deadbeef"))
    assert scan_for_leaks(tmp_path, SENTINEL, time.time() - 5,
                          {"bulldozer.log": None}, []) == []


def test_t1_empty_dir_passes(tmp_path):
    baselines = {n: None for n in (
        "bulldozer.log", "bulldozer-codex.log", "require-workflow-skill.log")}
    assert scan_for_leaks(tmp_path, SENTINEL, time.time(), baselines, []) == []


def test_t1_byte_identical_wrapper_fail_caught(tmp_path):
    """Pins detector 1 (sentinel): a wrapper-fail line whose FIELDS are
    byte-identical to production (all empty) is attributable only by session."""
    line = _fresh_line(
        event="wrapper-fail",
        tail="round= | artifact= | reviewer= | depth= | exit=64 | "
             "reason=missing required flag(s)")
    _write(tmp_path / "bulldozer.log", line)
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5,
                          {"bulldozer.log": None}, [])
    assert len(hits) == 1 and line in hits[0]


def test_t1_cafebabe_reserved_session_caught(tmp_path):
    """Pins detector 2: the reserved explicit-session id (F9/F10 closure)."""
    line = _fresh_line(session="cafebabe", event="reconciled",
                       tail="round=1 | artifact=x | findings=0 | verdict=GO")
    _write(tmp_path / "bulldozer.log", line)
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5,
                          {"bulldozer.log": None}, [])
    assert len(hits) == 1 and line in hits[0]


def test_t1_content_marker_caught(tmp_path):
    """Detector 3: content marker (pytest tmp path) with a foreign session."""
    line = _fresh_line(session="deadbeef",
                       tail="project=/private/var/folders/x/pytest-of-it/pytest-1/p")
    _write(tmp_path / "bulldozer.log", line)
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5,
                          {"bulldozer.log": None}, ["pytest-of-it"])
    assert len(hits) == 1 and line in hits[0]


def test_t1_offset_fast_path_skips_baseline_bytes(tmp_path):
    """Lines inside the recorded baseline (pre-session, e.g. a concurrent OLD
    run) are not rescanned; only post-baseline growth is."""
    log = tmp_path / "bulldozer.log"
    _write(log, _fresh_line(session="cafebabe"))  # inside baseline → skipped
    st = os.stat(log)
    baselines = {"bulldozer.log": (st.st_ino, st.st_size)}
    _write(log, _fresh_line(session="deadbeef"))  # fresh foreign → clean
    assert scan_for_leaks(tmp_path, SENTINEL, time.time() - 5, baselines, []) == []
    leak = _fresh_line()
    _write(log, leak)
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5, baselines, [])
    assert len(hits) == 1 and leak in hits[0]


def test_t3_rotation_scans_both_generations(tmp_path):
    """T3: st_ino change → both the current file and the .1 sibling are scanned;
    a marked line stranded in the rotated generation is still reported."""
    log = tmp_path / "bulldozer.log"
    old_leak = _fresh_line(tail="gen=old")
    _write(log, old_leak)
    st = os.stat(log)
    baselines = {"bulldozer.log": (st.st_ino, st.st_size)}
    # NB: old_leak is INSIDE the baseline offset — but rotation invalidates the
    # offset, so the full-scan of .1 must still surface it (ts window applies).
    os.replace(log, str(log) + ".1")
    new_leak = _fresh_line(tail="gen=new")
    _write(log, new_leak)
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5, baselines, [])
    assert len(hits) == 2
    assert any(old_leak in h for h in hits) and any(new_leak in h for h in hits)


# ── T2: writer-boundary provenance — one repro per producer family ───────────


def test_t2_writer_boundary_all_six_families_scanned(tmp_path):
    """T2 (spec §5): each of the six stable-log producer families, run against a
    fabricated $HOME with no knobs, lands its line in scratch — and
    scan_for_leaks reports ALL of them (sentinel or reserved-session detector).
    This pins scan coverage over the REAL wire shapes, including the
    byte-identical wrapper-fail and FACADE_SPAWN_FAIL forms."""
    scratch = tmp_path / "home"
    hooks = scratch / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    # CLAUDE_CODE_SESSION_ID stays = the suite sentinel (D1 set it in os.environ).
    t0 = time.time()

    def run(cmd, *, extra_env=None, stdin=None):
        e = test_env(drop=list(LOG_KNOBS), unsafe_allow=list(LOG_KNOBS),
                     set_vars={"HOME": str(scratch), **(extra_env or {})})
        r = subprocess.run(cmd, env=e, cwd=str(PLUGIN_ROOT), input=stdin,
                           capture_output=True, text=True, timeout=60)
        return r

    # (a) wrapper missing-args → wrapper-fail, byte-identical fields (exit 64 expected)
    run(["bash", str(PLUGIN_ROOT / "skills/check/scripts/bulldozer-round.sh")])
    # (b) update-state reconcile over fabricated state CARRYING a session key
    #     (F9: session comes from state.json, not env — reserved id required)
    review = tmp_path / "review"
    review.mkdir()
    (review / "state.json").write_text(json.dumps({
        "round": 1, "artifact": "test-artifact", "depth": "standard",
        "started_at": "2026-05-28T00:00:00+00:00", "reviewer": "codex/test",
        "findings_total": 0, "fixed_total": 0, "false_positives": 0,
        "history": [{"round": 1, "verdict": "UNKNOWN", "findings": 0, "fixed": 0,
                     "fp": 0, "timestamp": "2026-05-28T00:00:00+00:00",
                     "session": "cafebabe99", "manual_extraction_pending": True}],
    }))
    run([sys.executable, str(PLUGIN_ROOT / "skills/check/scripts/update-state.py"),
         "--review-dir", str(review), "--mode=replace-extraction", "1", "0", "GO"])
    # (c) invoke hook with the reserved explicit session id
    run([sys.executable, str(PLUGIN_ROOT / "hooks/log_skill_invoke.py")],
        extra_env={"CLAUDE_CODE_SESSION_ID": "cafebabe99"},
        stdin=json.dumps({"prompt": "/bulldozer:check probe", "cwd": str(tmp_path)}))
    # (d) facade FACADE_SPAWN_FAIL shape through the facade's own path resolution
    run([sys.executable, "-c",
         "import sys; sys.path.insert(0, {mcp!r}); sys.path.insert(0, {lib!r}); "
         "from codex_facade import _facade_log_path; "
         "from bulldozer_log import append_line; "
         "append_line(_facade_log_path(), 'FACADE_SPAWN_FAIL', call='42', err='probe')"
         .format(mcp=str(PLUGIN_ROOT / "mcp"), lib=str(PLUGIN_ROOT / "lib"))])
    # (e) look cdp.py log() — import-frozen LOG_FILE resolution
    run([sys.executable, "-c",
         "import sys; sys.path.insert(0, {look!r}); import cdp; cdp.log('probe')"
         .format(look=str(PLUGIN_ROOT / "skills/look/scripts"))])
    # (f) drive CLI shim through launch.sh's exact resolution shape
    run(["bash", "-c",
         'python3 lib/bulldozer_log.py '
         '"${BULLDOZER_DRIVE_LOG:-${HOME:-}/.claude/hooks/bulldozer-drive.log}" '
         'lane-launch port=0'])

    sentinel = os.environ["BULLDOZER_TEST_SENTINEL"]
    baselines = {n: None for n in ("bulldozer.log", "bulldozer-codex.log",
                                   "bulldozer-look.log", "bulldozer-drive.log")}
    hits = scan_for_leaks(hooks, sentinel, t0, baselines, [])
    joined = "\n".join(hits)
    for token in ("event=wrapper-fail", "event=reconciled", "event=invoke",
                  "event=FACADE_SPAWN_FAIL", "event=probe", "event=lane-launch"):
        assert token in joined, (
            "producer family line missing from scan report: {}\nscan saw:\n{}"
            .format(token, joined or "(nothing)"))
    assert len(hits) >= 6


# ── T2b / T5: start-assert unit surface ──────────────────────────────────────


def test_t2b_start_assert_clean_in_suite():
    """Positive: in the live suite the start-assert helper reports nothing."""
    assert _start_assert_problems() == []


def test_t2b_import_frozen_fallback_detects_unset_knob(monkeypatch):
    """Fresh-probe branch: with a knob unset, a future import of the producer
    would freeze the real default — the helper must name the module."""
    cdp_path = (PLUGIN_ROOT / "skills" / "look" / "scripts" / "cdp.py").resolve()
    saved = {name: sys.modules[name] for name in list(sys.modules)
             if getattr(sys.modules[name], "__file__", None)
             and Path(sys.modules[name].__file__).resolve() == cdp_path}
    for name in saved:
        del sys.modules[name]
    monkeypatch.delenv("BULLDOZER_LOOK_LOG", raising=False)
    try:
        problems = _import_frozen_problems()
    finally:
        sys.modules.update(saved)
    assert any("cdp.py" in p and "fresh-import" in p for p in problems), problems


def test_t2b_sys_modules_first_checks_loaded_instance():
    """R1-F2 r3 pin: an ALREADY-LOADED producer instance whose frozen constant
    escaped the redirect must fail the assert — a fresh importlib probe alone
    would pass and prove nothing about the instance tests actually use."""
    cdp_path = str((PLUGIN_ROOT / "skills" / "look" / "scripts" / "cdp.py").resolve())
    fake = types.ModuleType("_bdz_fake_cdp_instance")
    fake.__file__ = cdp_path
    fake.LOG_FILE = "/Users/nobody/.claude/hooks/bulldozer-look.log"
    sys.modules["_bdz_fake_cdp_instance"] = fake
    try:
        problems = _import_frozen_problems()
    finally:
        del sys.modules["_bdz_fake_cdp_instance"]
    assert any("cdp.py" in p and "(loaded)" in p and "escaped" in p
               for p in problems), problems


def test_t5_start_assert_names_repointed_knob(monkeypatch):
    """T5: a knob re-pointed at the real production path is named verbatim."""
    prod = str(Path.home() / ".claude" / "hooks" / "bulldozer.log")
    monkeypatch.setenv("BULLDOZER_LOG", prod)
    problems = _start_assert_problems()
    assert any(p.startswith("BULLDOZER_LOG=") for p in problems), problems


# ── R11-F1: codex descendants must not be env-scrubbed past the chokepoint ───


def test_r11f1_engine_child_env_forwards_log_knobs():
    """R11-F1 (spec §2 amendment): the engine's fail-closed child-env allowlist
    forwards the 7 BULLDOZER_* knobs WHEN PRESENT — without this, a codex
    descendant loses the redirect and a stable-log producer it runs falls back
    to the real $HOME. The fail-closed property must stay intact (secrets still
    dropped), and absent knobs must not be invented."""
    sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))
    import codex_server
    parent = {"PATH": "/bin", "HOME": "/tmp/h", "ANTHROPIC_API_KEY": "secret"}
    for k in LOG_KNOBS:
        parent[k] = "/tmp/redirect/" + k
    child = codex_server._build_child_env(parent)
    for k in LOG_KNOBS:
        assert child.get(k) == parent[k], "knob {} not forwarded".format(k)
    assert "ANTHROPIC_API_KEY" not in child  # fail-closed property unchanged
    bare = codex_server._build_child_env({"PATH": "/bin"})
    assert not any(k in bare for k in LOG_KNOBS)  # forwarded-WHEN-PRESENT only


def test_r11f1_facade_worker_env_carries_log_knobs(tmp_path):
    """The facade-worker path (spec R11-F1 T6c): Worker spawns with FULL-COPY
    env semantics today — this test PINS that the knobs survive into the worker
    spawn env, so a future Worker allowlist refactor cannot silently scrub them."""
    sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))
    from codex_facade import Worker
    out = tmp_path / "worker-env.json"
    code = ("import json, os; "
            "json.dump(dict(os.environ), open({p!r}, 'w'))".format(p=str(out)))
    w = Worker(1, [sys.executable, "-c", code], None,
               lambda *a, **k: None, lambda *a, **k: None)
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if out.exists() and out.read_text().strip():
                break
            time.sleep(0.05)
        seen = json.loads(out.read_text())
        for k in LOG_KNOBS:
            assert seen.get(k) == os.environ[k], "knob {} scrubbed".format(k)
        assert seen["BULLDOZER_WORKER"] == "1"
    finally:
        try:
            w.proc.wait(timeout=10)
        except Exception:
            w.proc.kill()
        import shutil
        shutil.rmtree(w.tmpdir, ignore_errors=True)


# ── T7 / T2b(xdist): inner-pytest runs via a conftest shim ───────────────────
# The shim exec's the REAL tests/conftest.py by path under a private module name
# and re-exports its hooks/fixture — so an inner run over a scratch dir gets the
# full D1+D2 machinery (redirect, sentinel, baselines, sessionfinish scan)
# without touching the repo tree.

_SHIM_CONFTEST = """\
import importlib.util as _ilu
import os as _os

_spec = _ilu.spec_from_file_location("bulldozer_real_conftest", {real_conftest!r})
_m = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_m)

pytest_sessionfinish = _m.pytest_sessionfinish
_log_isolation_start_assert = _m._log_isolation_start_assert

_rec = _os.environ.get("BULLDOZER_SHIM_RECORD")
if _rec:
    with open(_rec, "a") as _fh:
        _fh.write(_os.environ.get("BULLDOZER_TEST_SENTINEL", "?") + " "
                  + _os.environ.get("CLAUDE_CODE_SESSION_ID", "?") + "\\n")
"""

# Module body runs at COLLECTION import — i.e. AFTER the shim conftest recorded
# baselines — faithfully simulating a collection-time leak (spec T7).
_PLANTED_LEAK = """\
import sys
sys.path.insert(0, {lib!r})
from pathlib import Path
from bulldozer_log import append_line
append_line(Path.home() / ".claude" / "hooks" / "bulldozer.log", "reconciled",
            session="cafebabe99", round=1, artifact="planted-leak", findings=0,
            verdict="GO")
{epilogue}
"""


def _run_shim(tmp_path, planted, args=(), env_updates=None, record=False):
    root = tmp_path / "shim"
    root.mkdir()
    home = tmp_path / "shim-home"
    (home / ".claude" / "hooks").mkdir(parents=True)
    (root / "conftest.py").write_text(_SHIM_CONFTEST.format(
        real_conftest=str(PLUGIN_ROOT / "tests" / "conftest.py")))
    (root / "test_planted.py").write_text(planted)
    record_file = tmp_path / "shim-record.txt"
    set_vars = {"HOME": str(home)}
    set_vars.update(env_updates or {})
    if record:
        set_vars["BULLDOZER_SHIM_RECORD"] = str(record_file)
    env = test_env(
        drop=list(LOG_KNOBS) + ["BULLDOZER_TEST_SENTINEL",
                                "BULLDOZER_TEST_SENTINEL_SPAWNER"],
        unsafe_allow=list(LOG_KNOBS),
        set_vars=set_vars)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "test_planted.py", "-q",
         "-p", "no:cacheprovider", *args],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=180)
    return r, home, record_file


def _leak_source(epilogue="def test_noop():\n    pass\n"):
    return _PLANTED_LEAK.format(lib=str(PLUGIN_ROOT / "lib"), epilogue=epilogue)


def test_t7_collection_leak_caught_in_co_only_run(tmp_path):
    """T7: --co runs execute NO fixtures — the sessionfinish scan alone must
    catch a collection-time leak and force a non-zero exit."""
    r, _, _ = _run_shim(tmp_path, _leak_source(), args=("--co",))
    assert r.returncode != 0, r.stdout + r.stderr
    assert "TEST-LOG LEAK GUARD" in r.stderr, r.stderr
    assert "planted-leak" in r.stderr, r.stderr


def test_t7_collection_error_still_scanned(tmp_path):
    """T7: a collection ERROR (module raises after leaking) must not skip the
    scan — the offender is still reported."""
    r, _, _ = _run_shim(
        tmp_path, _leak_source(epilogue="raise RuntimeError('planted collection error')"))
    assert r.returncode != 0
    assert "TEST-LOG LEAK GUARD" in r.stderr, r.stderr
    assert "planted-leak" in r.stderr, r.stderr


def test_t7_clean_co_run_exits_zero(tmp_path):
    """Control: without a leak, --co exits 0 — the guard does not false-fire."""
    r, _, _ = _run_shim(tmp_path, "def test_noop():\n    pass\n", args=("--co",))
    assert r.returncode == 0, r.stdout + r.stderr


def test_t2b_stale_sentinel_regenerated_and_leak_still_caught(tmp_path):
    """T2b (spec §5, GREEN col): a stale valid-form BULLDOZER_TEST_SENTINEL +
    mismatched production CLAUDE_CODE_SESSION_ID in the OUTER env: the inner
    controller must REGENERATE and re-point BOTH vars, and the planted line is
    still detected. SPAWNER is pinned to a value that can never equal the inner
    process's ppid — under an -n auto OUTER run PYTEST_XDIST_WORKER is
    inherited by this subprocess, so WITHOUT the spawner-pid conjunct the inner
    run would masquerade as an xdist worker and ADOPT the stale sentinel (the
    hole this test caught live)."""
    check = (
        "import os\n"
        "_s = os.environ['BULLDOZER_TEST_SENTINEL']\n"
        "assert _s != 'PTdddddd', 'stale sentinel was adopted by a non-worker'\n"
        "assert os.environ['CLAUDE_CODE_SESSION_ID'] == _s, 'vars diverged'\n"
        "def test_noop():\n    pass\n")
    r, _, _ = _run_shim(
        tmp_path, _leak_source(epilogue=check), args=("--co",),
        env_updates={"BULLDOZER_TEST_SENTINEL": "PTdddddd",
                     "CLAUDE_CODE_SESSION_ID": "aaaaaaaaaaaa",
                     "BULLDOZER_TEST_SENTINEL_SPAWNER": "1"})
    assert "stale sentinel was adopted" not in r.stdout + r.stderr
    assert "vars diverged" not in r.stdout + r.stderr
    assert r.returncode != 0, r.stdout + r.stderr
    assert "planted-leak" in r.stderr, r.stderr


def test_t2b_xdist_workers_share_one_sentinel(tmp_path):
    """T2b: under -n 2 every process (controller + workers, via the shim
    record) and every test sees ONE identical wire-form sentinel — the
    adopt-don't-regenerate protocol of D1."""
    planted = (
        "import os\n"
        "import pytest\n"
        "@pytest.mark.parametrize('i', range(4))\n"
        "def test_record(i):\n"
        "    with open(os.environ['BULLDOZER_SHIM_RECORD'], 'a') as fh:\n"
        "        fh.write(os.environ['BULLDOZER_TEST_SENTINEL'] + ' '\n"
        "                 + os.environ['CLAUDE_CODE_SESSION_ID'] + '\\n')\n")
    r, _, record_file = _run_shim(tmp_path, planted, args=("-n", "2"), record=True)
    assert r.returncode == 0, r.stdout + r.stderr
    tokens = record_file.read_text().split()
    # ≥3 shim records (controller + 2 workers) ×2 tokens + 4 test records ×2
    assert len(tokens) >= 10, tokens
    assert len(set(tokens)) == 1, "sentinel diverged across processes: {}".format(
        sorted(set(tokens)))
    assert re.match(r"^PT[0-9a-f]{6}$", tokens[0]), tokens[0]


# ── T6a: test_env builder runtime checks (D3a) ───────────────────────────────


def test_t6a_drop_protected_without_allow_raises():
    with pytest.raises(RuntimeError, match="BULLDOZER_LOG"):
        test_env(drop=["BULLDOZER_LOG"])


def test_t6a_empty_value_for_protected_raises():
    """F11: an empty string falls through `os.environ.get(K) or default` exactly
    like a removal — emptying a protected var is a drop."""
    with pytest.raises(RuntimeError, match="BULLDOZER_CODEX_LOG"):
        test_env(set_vars={"BULLDOZER_CODEX_LOG": ""})


def test_t6a_loop_variable_drop_raises():
    """Aliases/loops are irrelevant — every path reaches the same runtime check."""
    doomed = ["CDP_PORT", "BULLDOZER_LOG"]
    with pytest.raises(RuntimeError, match="BULLDOZER_LOG"):
        test_env(drop=doomed)


def test_t6a_lane_var_drops_pass():
    env = test_env(drop=["CDP_PORT", "LOOK_HEADLESS"])
    assert "CDP_PORT" not in env
    assert env["BULLDOZER_LOG"] == os.environ["BULLDOZER_LOG"]


def test_t6a_pinned_unsafe_allow_works_here():
    """This file carries a central-allowlist entry — an authorized drop passes."""
    env = test_env(drop=["BULLDOZER_LOG"], unsafe_allow=["BULLDOZER_LOG"])
    assert "BULLDOZER_LOG" not in env


def test_t6a_unsafe_allow_from_unpinned_callsite_raises(tmp_path):
    """R1-F5 r4: caller-supplied justification alone must not self-authorize —
    a callsite file absent from the central allowlist raises regardless."""
    mod_path = tmp_path / "rogue_helper.py"
    mod_path.write_text(
        "import sys\n"
        "sys.path.insert(0, {tests!r})\n"
        "from conftest import test_env\n"
        "def build():\n"
        "    return test_env(drop=['BULLDOZER_LOG'], unsafe_allow=['BULLDOZER_LOG'])\n"
        .format(tests=str(PLUGIN_ROOT / "tests")))
    import importlib.util
    spec = importlib.util.spec_from_file_location("rogue_helper", str(mod_path))
    rogue = importlib.util.module_from_spec(spec)
    sys.modules["rogue_helper"] = rogue
    try:
        spec.loader.exec_module(rogue)
        with pytest.raises(RuntimeError, match="not pinned"):
            rogue.build()
    finally:
        del sys.modules["rogue_helper"]


def test_t6a_set_vars_none_unsets_unprotected():
    env = test_env(set_vars={"CDP_PORT": None, "LOOK_HEADLESS": "1"})
    assert "CDP_PORT" not in env
    assert env["LOOK_HEADLESS"] == "1"


def test_t6a_scrub_env_still_carries_redirect():
    """A deliberately minimal env still lands its writers in tmp (R9-F1)."""
    env = test_env(scrub=True)
    assert env["BULLDOZER_LOG"] == os.environ["BULLDOZER_LOG"]
    assert "PATH" in env
    assert len(env) < 20  # genuinely minimal, not a full copy


# ── T6c: runtime Popen chokepoint, repair semantics (D3a-rt) ─────────────────


def _child_env_dump(env):
    """Run a child that prints the knobs it actually sees; returns dict."""
    code = ("import json, os; print(json.dumps({k: os.environ.get(k) "
            "for k in " + repr(list(LOG_KNOBS)) + "}))")
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_t6c_env_i_argv_raises():
    """Command-level env clearing cannot be repaired → refused outright."""
    with pytest.raises(RuntimeError, match="env -i"):
        subprocess.run(["env", "-i", "true"], capture_output=True)


def test_t6c_from_scratch_env_gets_knobs_injected():
    """The repair rule: a from-scratch env (production-builder shape) has the
    absent knobs injected — its writers land in the redirect, not $HOME."""
    seen = _child_env_dump({"PATH": os.environ["PATH"]})
    for k in LOG_KNOBS:
        assert seen[k] == os.environ[k], (k, seen[k])


def test_t6c_per_test_tmp_repoint_preserved(tmp_path):
    """A non-production re-point is a legitimate sandbox — never overwritten."""
    mine = str(tmp_path / "my.log")
    env = os.environ.copy()
    env["BULLDOZER_LOG"] = mine
    assert _child_env_dump(env)["BULLDOZER_LOG"] == mine


def test_t6c_foreign_home_absent_knobs_stay_absent(tmp_path):
    """The scratch-$HOME repro shape (T2/T4): child's Path.home() fallback is
    sandboxed, so absent knobs are deliberately left absent."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "scratch-home")
    for k in LOG_KNOBS:
        env.pop(k, None)
    seen = _child_env_dump(env)
    assert all(v is None for v in seen.values()), seen


def test_t6c_foreign_home_explicit_production_value_repaired(tmp_path):
    """R10-F2: an EXPLICIT knob resolving under the real production dir outranks
    the sandboxed fallback in every producer — repaired even under foreign HOME."""
    prod = str(Path.home() / ".claude" / "hooks" / "bulldozer.log")
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "scratch-home")
    env["BULLDOZER_LOG"] = prod
    seen = _child_env_dump(env)
    assert seen["BULLDOZER_LOG"] != prod
    assert seen["BULLDOZER_LOG"] == os.environ["BULLDOZER_LOG"]


def test_t6c_env_none_parent_mutation_detected(monkeypatch):
    """R10-F2: an in-process mutation of os.environ pointing a knob back at
    production must not ride the env=None inherit branch silently."""
    monkeypatch.setenv("BULLDOZER_CONSULT_LOG",
                       str(Path.home() / ".claude" / "hooks" / "bulldozer-consult.log"))
    with pytest.raises(RuntimeError, match="BULLDOZER_CONSULT_LOG"):
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True)


def test_t6c_relative_knob_value_always_repaired():
    """Post-impl codex review P2: a RELATIVE log override resolves inside the
    CHILD against ITS cwd — with cwd at the real HOME, '.claude/hooks/…' IS the
    production log, while the parent-side resolve() (against pytest's cwd)
    misclassifies it as non-production. Relative values are unclassifiable →
    always repaired."""
    env = test_env(set_vars={"BULLDOZER_LOG": ".claude/hooks/bulldozer.log"})
    r = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['BULLDOZER_LOG'])"],
        env=env, cwd=str(Path.home()), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == os.environ["BULLDOZER_LOG"], (
        "relative knob value must be repaired to the absolute redirect, got: "
        + r.stdout.strip())


def test_t6c_relative_home_not_treated_as_foreign():
    """Post-impl codex review P2 (companion): a RELATIVE HOME is not a
    sanctioned foreign-HOME sandbox — the child expands it against ITS cwd,
    which can be anywhere (including the real home). Not foreign → the normal
    branch injects the absent knobs, which outrank the fallback in every
    producer."""
    env = test_env(drop=list(LOG_KNOBS), unsafe_allow=list(LOG_KNOBS),
                   set_vars={"HOME": "."})
    seen = _child_env_dump(env)
    for k in LOG_KNOBS:
        assert seen.get(k) == os.environ[k], (k, seen.get(k))


def test_t6c_caller_dict_never_mutated():
    """Repair works on a COPY — the caller's env dict stays untouched."""
    env = {"PATH": os.environ["PATH"]}
    before = dict(env)
    _child_env_dump(env)
    assert env == before


# ── T6b: two narrow static scans (D3b) ───────────────────────────────────────


def _is_test_env_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "test_env")


def _scan_env_kwarg_provenance(paths=None):
    """Scan 1 (R1-F5 r4/r5/r6, r12 refinement): anchored on the `env=` KEYWORD
    ARGUMENT itself, not the callee — aliasing, rebinding and future wrappers
    are all caught identically. Accepted value forms:
      1. env=None
      2. env=test_env(...) — a direct call
      3. env=<name> where the enclosing function binds <name> EXACTLY ONCE and
         that binding is `<name> = test_env(...)` (the §7-rollout shape; any
         other or additional binding of the name → violation)
      4. any shape INSIDE a helper pinned in
         CENTRAL_ALLOWLIST['env_forward_helpers'] (scope pin), or a direct
         call / once-bound name of a pinned BUILDER helper defined in the same
         file (whose internal build must itself go through test_env)
    Post-build dict tweaks of an accepted name are permitted — protected-var
    damage after test_env() is the RUNTIME chokepoint's job (division of
    labor, spec §4)."""
    helper_pins = {(name, f) for name, f, _why in
                   CENTRAL_ALLOWLIST["env_forward_helpers"]}
    files = ([Path(p) for p in paths] if paths is not None
             else sorted((PLUGIN_ROOT / "tests").glob("*.py")))
    violations = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        pinned_builders = {name for name, f, _why in
                           CENTRAL_ALLOWLIST["env_forward_helpers"]
                           if f == path.name}
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def _is_sanctioned_build(node):
            return _is_test_env_call(node) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in pinned_builders)

        def enclosing_function(node):
            n = node
            while n in parents:
                n = parents[n]
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return n
            return None

        def bindings_of(name, scope):
            """All value-nodes bound to `name` by plain assignment in `scope`
            (the whole module when scope is None), plus a None marker for
            non-assignment bindings (params, loops, with/aug targets)."""
            root = scope if scope is not None else tree
            found = []
            for n in ast.walk(root):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not root:
                    continue  # do not descend into nested scopes
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id == name:
                            found.append(n.value)
                elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                    if isinstance(n.target, ast.Name) and n.target.id == name:
                        found.append(None)
                elif isinstance(n, (ast.For, ast.comprehension)):
                    tgt = n.target
                    for t in ast.walk(tgt):
                        if isinstance(t, ast.Name) and t.id == name:
                            found.append(None)
            if scope is not None:
                for a in (scope.args.args + scope.args.posonlyargs
                          + scope.args.kwonlyargs):
                    if a.arg == name:
                        found.append(None)
            return found

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "env":
                    continue
                v = kw.value
                if isinstance(v, ast.Constant) and v.value is None:
                    continue
                if _is_sanctioned_build(v):
                    continue
                scope = enclosing_function(node)
                if scope is not None and (scope.name, path.name) in helper_pins:
                    continue
                if isinstance(v, ast.Name):
                    bound = bindings_of(v.id, scope)
                    if len(bound) == 1 and bound[0] is not None \
                            and _is_sanctioned_build(bound[0]):
                        continue
                violations.append(
                    "{}:{}: env={} — must be None, a test_env(...) call, or a "
                    "name bound once from test_env(...)".format(
                        path.name, node.lineno, ast.unparse(v)[:60]))
    return violations


# Best-effort tripwire shapes (the chokepoint is the enforcement point; these
# are defense-in-depth). Values must LOOK like session ids (lowercase run) so a
# tuple of ENV VAR NAMES ("CLAUDE_CODE_SESSION_ID", "BULLDOZER_…") or a string
# that merely ENDS in `session=` ("session=" + x) never false-fires.
_SESSION_LITERAL_PATTERNS = (
    # subscript-assign / setenv-arg / dict-literal forms: after the var name's
    # own closing quote comes `] =`, `,` or `:`, then the id literal
    re.compile(r'CLAUDE_CODE_SESSION_ID["\']\s*(?:\]\s*=|[,:])\s*'
               r'["\'](?!cafebabe99["\'])[a-z0-9 _|\-]{1,40}["\']'),
    # fabricated state.json "session" keys
    re.compile(r'["\']session["\']\s*:\s*["\'](?!cafebabe)[^"\']+["\']'),
    # explicit session= kwargs (append_line / CLI shim)
    re.compile(r'\bsession\s*=\s*["\'](?!cafebabe)[^"\']+["\']'),
)


def _scan_session_literals(paths=None):
    """Scan 2 (feeds D2 detector 2, closes R1-F1 r3): every explicit session id
    a test fabricates must be the reserved `cafebabe99` — detector 2 then
    catches any leak from it BY CONSTRUCTION. Novel literals fail unless the
    file is pinned in CENTRAL_ALLOWLIST['session_literals'] (hermetic unit
    tests of the logging path itself, spec F10)."""
    allowed = {f for f, _why in CENTRAL_ALLOWLIST["session_literals"]}
    files = ([Path(p) for p in paths] if paths is not None
             else sorted((PLUGIN_ROOT / "tests").glob("*.py")))
    violations = []
    for path in files:
        rel = "tests/" + path.name
        if paths is None and rel in allowed:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for pat in _SESSION_LITERAL_PATTERNS:
                if pat.search(line):
                    violations.append(
                        "{}:{}: non-reserved explicit session id — use "
                        "'cafebabe99' or pin the file in CENTRAL_ALLOWLIST"
                        "['session_literals']: {}".format(
                            path.name, lineno, line.strip()[:80]))
                    break
    return violations


def test_t6b_scan1_env_kwarg_provenance_clean():
    violations = _scan_env_kwarg_provenance()
    assert violations == [], (
        "{} env= callsite(s) bypass test_env:\n  ".format(len(violations))
        + "\n  ".join(violations))


def test_t6b_scan1_catches_planted_shapes(tmp_path):
    """RED battery from the spec: from-scratch dict, copy-then-pop recurrence,
    and BOTH rebinding shapes — the kwarg anchor sees them regardless of callee."""
    planted = tmp_path / "test_planted_scan1.py"
    planted.write_text(
        "import os, subprocess\n"
        "subprocess.run(['x'], env={'PATH': '/bin'})\n"
        "env = os.environ.copy()\n"
        "env.pop('BULLDOZER_LOG', None)\n"
        "subprocess.run(['x'], env=env)\n"
        "sp = subprocess\n"
        "sp.run(['x'], env=env)\n"
        "run = subprocess.run\n"
        "run(['x'], env=env)\n"
        "subprocess.run(['x'], env=os.environ.copy())\n"
        "def rebound():\n"
        "    env = test_env()\n"
        "    env = os.environ.copy()\n"
        "    subprocess.run(['x'], env=env)\n")
    violations = _scan_env_kwarg_provenance(paths=[planted])
    assert len(violations) == 6, violations


def test_t6b_scan1_accepts_none_and_test_env(tmp_path):
    planted = tmp_path / "test_planted_ok.py"
    planted.write_text(
        "import subprocess\n"
        "from conftest import test_env\n"
        "subprocess.run(['x'], env=None)\n"
        "subprocess.run(['x'], env=test_env(set_vars={'CDP_PORT': '1'}))\n"
        "def single_binding():\n"
        "    env = test_env(set_vars={'CDP_PORT': '1'})\n"
        "    env['LOOK_HEADLESS'] = '1'\n"
        "    subprocess.run(['x'], env=env)\n")
    assert _scan_env_kwarg_provenance(paths=[planted]) == []


def test_t6b_scan2_session_literals_clean():
    violations = _scan_session_literals()
    assert violations == [], (
        "{} non-reserved session literal(s):\n  ".format(len(violations))
        + "\n  ".join(violations))


def test_t6b_scan2_catches_planted_literals(tmp_path):
    planted = tmp_path / "test_planted_scan2.py"
    planted.write_text(
        'import os\n'
        'os.environ["CLAUDE_CODE_SESSION_ID"] = "deadbeef99"\n'
        'STATE = {"session": "deadbeef"}\n'
        'def f(append_line, p):\n'
        '    append_line(p, "x", session="deadbeef")\n')
    violations = _scan_session_literals(paths=[planted])
    assert len(violations) == 3, violations


def test_t6b_scan2_reserved_id_passes(tmp_path):
    planted = tmp_path / "test_planted_scan2_ok.py"
    planted.write_text(
        'import os\n'
        'os.environ["CLAUDE_CODE_SESSION_ID"] = "cafebabe99"\n'
        'STATE = {"session": "cafebabe99"}\n'
        'def f(append_line, p):\n'
        '    append_line(p, "x", session="cafebabe99")\n')
    assert _scan_session_literals(paths=[planted]) == []


def test_t3_rotate_and_regrow_pins_inode_check(tmp_path):
    """Mutation pin (spec T3): a size-based generation check would fast-path
    past a marked line sitting at an offset BELOW the saved size in a rotated-
    and-regrown file; the inode check must catch it."""
    log = tmp_path / "bulldozer.log"
    _write(log, *[_fresh_line(session="deadbeef", tail="pad={}".format(i))
                  for i in range(5)])
    st = os.stat(log)
    baselines = {"bulldozer.log": (st.st_ino, st.st_size)}
    os.replace(log, str(log) + ".1")
    leak = _fresh_line(tail="gen=regrown")  # lands at offset < saved size
    filler = [_fresh_line(session="deadbeef", tail="fill={}".format(i))
              for i in range(10)]  # regrow PAST the saved size
    _write(log, leak, *filler)
    assert os.stat(log).st_size > st.st_size
    hits = scan_for_leaks(tmp_path, SENTINEL, time.time() - 5, baselines, [])
    assert len(hits) == 1 and leak in hits[0]
