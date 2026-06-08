"""Offline unit tests for the SP4 external grader (spec §3.2 — grading reads ONLY
runner-owned cmd-NN.log files; agent-returned fields never grade)."""
import json
import os
import subprocess
import sys

import pytest

PLUGIN = os.path.join(os.path.dirname(__file__), "..")
GRADER = os.path.join(PLUGIN, "skills", "drive", "scripts", "grade_run.py")
MANIFESTS = os.path.join(PLUGIN, "skills", "drive", "data", "calibration-manifests.json")

sys.path.insert(0, os.path.dirname(GRADER))
import grade_run  # noqa: E402


def _write_run(tmp_path, logs):
    run = tmp_path / "T1-haiku-1"
    run.mkdir()
    for i, content in enumerate(logs):
        (run / "cmd-{:02d}.log".format(i)).write_text(content)
    return str(run)


class TestGradeRun:
    def test_all_markers_present_grades_success(self, tmp_path):
        run = _write_run(tmp_path, [
            "Browser: Chrome/149.0.7827.54\nEXIT=0\n",
            "http://localhost:9361/drive-page.html loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_OK\nEXIT=0\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is True

    def test_missing_marker_grades_failure(self, tmp_path):
        # exits are all consistent with the oracle — ONLY the marker is absent
        # (a capture that lost stdout): isolates the missing-markers path.
        run = _write_run(tmp_path, [
            "EXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "EXIT=0\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "missing-markers"
        assert "CONSOLE_GATE_OK" in res["missing_markers"]

    def test_gate_fail_with_wrong_exit_is_exit_mismatch(self, tmp_path):
        # the SAME wrong-outcome run graded via the exit leg: gate printed FAIL and
        # exited 1 where T1 expects 0 — exit check fires before marker check.
        run = _write_run(tmp_path, [
            "EXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_FAIL: 1 (1 exception(s), 0 console, 0 log)\nEXIT=1\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "exit-mismatch"

    def test_wrong_classification_grades_failure(self, tmp_path):
        # T4 expects classification=flaky; markers alone are not enough (spec §3.1 honest-classification)
        run = _write_run(tmp_path, [
            "EXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "ASSERT_FAIL #flappy — unstable: flapped 4x\nEXIT=1\n",
        ])
        ok = grade_run.grade(run, task_id="T4", classification="flaky", manifests_path=MANIFESTS)
        bad = grade_run.grade(run, task_id="T4", classification="absent", manifests_path=MANIFESTS)
        assert ok["graded_success"] is True
        assert bad["graded_success"] is False and bad["reason"] == "classification-mismatch"

    def test_missing_run_dir_grades_zero(self, tmp_path):
        # spec §3.2: capture is part of the task
        res = grade_run.grade(str(tmp_path / "nonexistent"), task_id="T1",
                              classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "run-dir-missing"

    def test_exit_code_mismatch_grades_failure(self, tmp_path):
        # spec §3.2: grading checks EXIT= codes, not just markers — a T1 gate that
        # printed CONSOLE_GATE_OK but exited 1 is an inconsistent capture → fail.
        run = _write_run(tmp_path, [
            "Browser: Chrome/149.0.7827.54\nEXIT=0\n",
            "loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_OK\nEXIT=1\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "exit-mismatch"

    def test_missing_command_log_grades_failure(self, tmp_path):
        # T1 manifest has 2 commands → cmd-00 (pre-flight) + cmd-01..02 expected;
        # a missing log means a command was skipped — incomparable run.
        run = _write_run(tmp_path, [
            "Browser: Chrome/149.0.7827.54\nEXIT=0\n",
            "loader=AABB\nCONSOLE_GATE_OK\nEXIT=0\n",   # only ONE manifest log present
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "log-set-mismatch"

    def test_missing_preflight_log_grades_failure(self, tmp_path):
        # The hole-D pre-flight capture (cmd-00.log) is mandatory: right manifest logs
        # WITHOUT it = the binary-identity check was skipped — fail (review round 2 R2-F1).
        run = tmp_path / "T1-haiku-9"
        run.mkdir()
        (run / "cmd-01.log").write_text("loader=AABB\nEXIT=0\n")
        (run / "cmd-02.log").write_text("CONSOLE_GATE_OK\nEXIT=0\n")
        res = grade_run.grade(str(run), task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "log-set-mismatch"

    def test_failed_preflight_grades_failure(self, tmp_path):
        # Present-but-failed pre-flight (EXIT=7) must not grade success (round 3 R2-F1);
        # an empty cmd-00.log (no EXIT= trailer) is preflight-malformed.
        run = _write_run(tmp_path, [
            "curl: (7) Failed to connect\nEXIT=7\n",
            "loader=AABB\nEXIT=0\n",
            "CONSOLE_GATE_OK\nEXIT=0\n",
        ])
        res = grade_run.grade(run, task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "preflight-failed"
        empty = tmp_path / "T1-haiku-8"
        empty.mkdir()
        (empty / "cmd-00.log").write_text("")
        (empty / "cmd-01.log").write_text("loader=AABB\nEXIT=0\n")
        (empty / "cmd-02.log").write_text("CONSOLE_GATE_OK\nEXIT=0\n")
        res2 = grade_run.grade(str(empty), task_id="T1", classification="pass", manifests_path=MANIFESTS)
        assert res2["graded_success"] is False and res2["reason"] == "preflight-malformed"

    def _write_t9_run(self, tmp_path, nav_loader, bind_loader, teardown="PORT_FREE"):
        run = tmp_path / "T9-haiku-1"
        run.mkdir()
        logs = {
            "cmd-00.log": "Browser ok\nEXIT=0\n",
            # REAL navigate format is paren-terminated — "…, loader=X)" (cdp.py
            # "Navigated to {} ({} fired in {}ms, loader={})") — the fixture MUST
            # mimic it so the [^\s)]+ value class is actually exercised:
            "cmd-01.log": "Navigated to http://x/drive-page.html (load fired in 120ms, loader={})\nEXIT=0\n".format(nav_loader),
            "cmd-02.log": "CONSOLE_GATE_OK\nEXIT=0\n",
            "cmd-03.log": "ASSERT_PASS #always-visible held 300ms\nEXIT=0\n",
            "cmd-04.log": "clicked BUTTON (trusted)\nEXIT=0\n",   # real cdp.py: el.tagName is UPPERCASE
            "cmd-05.log": "/tmp/sp4-t9.jpg  800\u00d7600\nBIND url=http://x loader={} t=1\nEXIT=0\n".format(bind_loader),  # real cdp.py dims separator is U+00D7
            "cmd-99.log": teardown + "\nEXIT=0\n",
        }
        for name, content in logs.items():
            (run / name).write_text(content)
        return str(run)

    def test_t9_loader_mismatch_grades_failure(self, tmp_path):
        # spec T9: BIND loader must MATCH navigate loader — substring presence is not enough
        ok = grade_run.grade(self._write_t9_run(tmp_path, "AAAA11", "AAAA11"),
                             task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert ok["graded_success"] is True
        # fresh dir for the mismatch variant
        bad_dir = tmp_path / "mismatch"
        bad_dir.mkdir()
        bad = grade_run.grade(self._write_t9_run(bad_dir, "AAAA11", "BBBB22"),
                              task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert bad["graded_success"] is False and bad["reason"] == "loader-mismatch"

    def test_t9_teardown_not_verified_grades_failure(self, tmp_path):
        res = grade_run.grade(self._write_t9_run(tmp_path, "CC", "CC", teardown="PORT_STILL_ALIVE"),
                              task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "teardown-not-verified"

    def test_t9_teardown_without_capture_trailer_grades_failure(self, tmp_path):
        # round-4 failure mode pinned: PORT_FREE WITHOUT the EXIT=0 capture trailer is a
        # hand-written file, not runner-owned evidence — must fail the same way.
        run = self._write_t9_run(tmp_path, "DD", "DD")
        (tmp_path / "T9-haiku-1" / "cmd-99.log").write_text("PORT_FREE\n")   # no trailer
        res = grade_run.grade(run, task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "teardown-not-verified"
        (tmp_path / "T9-haiku-1" / "cmd-99.log").write_text("PORT_FREE\nEXIT=1\n")   # nonzero trailer
        res2 = grade_run.grade(run, task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res2["graded_success"] is False and res2["reason"] == "teardown-not-verified"

    def _write_t10_run(self, tmp_path, n_iters, last_green=True):
        # fix-verify layout: cmd-00 at the root, each cycle in iter-K/cmd-01..NN —
        # the grader counts iterations from the FILESYSTEM (independent evidence).
        run = tmp_path / "T10a-haiku-1"
        run.mkdir()
        (run / "cmd-00.log").write_text("Browser: Chrome/149.0.7827.54\nEXIT=0\n")
        for k in range(1, n_iters + 1):
            it = run / "iter-{}".format(k)
            it.mkdir()
            green = last_green and k == n_iters
            (it / "cmd-01.log").write_text("Navigated to file://x (load fired in 90ms, loader=AB12)\nEXIT=0\n")
            if green:
                (it / "cmd-02.log").write_text("ASSERT_PASS #target-btn held 300ms (total 350ms)\nEXIT=0\n")
                (it / "cmd-03.log").write_text("clicked BUTTON (trusted)\nEXIT=0\n")
            else:
                (it / "cmd-02.log").write_text("ASSERT_FAIL #target-btn — never true within 5000ms\nEXIT=1\n")
                (it / "cmd-03.log").write_text("CLICK_REQUIRE_TRUSTED_FAIL: '#target-btn' not hittable\nEXIT=1\n")
        return str(run)

    def test_t10_requires_integrity_pass(self, tmp_path):
        # spec §3.1 anti-gaming: T10 grades success ONLY with the orchestrator's
        # integrity re-run verdict (Task 9 Step 6) supplied as --integrity pass.
        run = self._write_t10_run(tmp_path, n_iters=1)
        ok = grade_run.grade(run, task_id="T10a", classification="pass",
                             manifests_path=MANIFESTS, integrity="pass")
        no = grade_run.grade(run, task_id="T10a", classification="pass",
                             manifests_path=MANIFESTS, integrity="fail")
        missing = grade_run.grade(run, task_id="T10a", classification="pass",
                                  manifests_path=MANIFESTS)
        assert ok["graded_success"] is True
        assert no["graded_success"] is False and no["reason"] == "integrity-failed"
        assert missing["graded_success"] is False and missing["reason"] == "integrity-missing"

    def test_t10_iterations_counted_from_filesystem(self, tmp_path):
        # The breaker statistic uses iterations_observed (iter-K dirs), NEVER the
        # agent's self-report (peer-review F4): 3 cycles, last green → 3.
        run = self._write_t10_run(tmp_path, n_iters=3)
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is True
        assert res["iterations_observed"] == 3

    def test_t10_censored_run_fails_on_last_iteration_markers(self, tmp_path):
        # Breaker-hit (censored) run: 3 cycles, none green — grader fails on the
        # highest-K cycle's markers; analysis counts iter-3 + red as censored.
        run = self._write_t10_run(tmp_path, n_iters=3, last_green=False)
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="fail")
        assert res["graded_success"] is False
        assert res["reason"] in ("missing-markers", "exit-mismatch")

    def test_t10_empty_trailing_iter_grades_on_last_complete_cycle(self, tmp_path):
        # LIVE-EXPERIMENT bug (wf_b327f1bf): a weak agent went green at iter-2 then
        # mkdir'd an EMPTY iter-3 and stopped. The old "highest-numbered dir" rule
        # graded the empty iter-3 → log-set-mismatch → FALSE fail of a real fix.
        # Grade the highest COMPLETE cycle instead; the empty dir is not an attempt.
        run = self._write_t10_run(tmp_path, n_iters=2, last_green=True)
        os.mkdir(os.path.join(run, "iter-3"))   # empty trailing dir, no command logs
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is True
        assert res["iterations_observed"] == 2, "empty mkdir must not count as an attempt"

    def test_t10_partial_trailing_iter_skipped(self, tmp_path):
        # Same defense for a PARTIAL trailing cycle (agent wrote cmd-01 then crashed):
        # an incomplete log set at the highest K must not mask the green iter-2 below it.
        run = self._write_t10_run(tmp_path, n_iters=2, last_green=True)
        os.mkdir(os.path.join(run, "iter-3"))
        with open(os.path.join(run, "iter-3", "cmd-01.log"), "w") as f:
            f.write("Navigated to file://x (load fired in 90ms, loader=AB12)\nEXIT=0\n")
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is True
        assert res["iterations_observed"] == 2

    def test_t10_green_then_red_grades_the_last_complete_cycle(self, tmp_path):
        # Review F2: complete[-1] (not complete[0]) must be graded. An agent that
        # fixed the page (iter-1 green) then REGRESSED it (iter-2 complete-RED) ends
        # red — the grader must report the FINAL complete cycle, not the earlier green.
        # Guards against a complete[-1]→complete[0] regression the other tests miss.
        run = self._write_t10_run(tmp_path, n_iters=1, last_green=True)  # iter-1 green
        it2 = os.path.join(run, "iter-2")
        os.mkdir(it2)
        with open(os.path.join(it2, "cmd-01.log"), "w") as f:
            f.write("Navigated to file://x (load fired in 90ms, loader=AB12)\nEXIT=0\n")
        with open(os.path.join(it2, "cmd-02.log"), "w") as f:
            f.write("ASSERT_FAIL #target-btn — never true within 5000ms\nEXIT=1\n")
        with open(os.path.join(it2, "cmd-03.log"), "w") as f:
            f.write("CLICK_REQUIRE_TRUSTED_FAIL: '#target-btn' not hittable\nEXIT=1\n")
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is False
        assert res["reason"] in ("missing-markers", "exit-mismatch")

    def test_t10_tolerates_extra_root_debug_files(self, tmp_path):
        # LIVE-EXPERIMENT bug (T10b-haiku-3): the agent fixed the page (green cycle)
        # but left debug noise at root (cmd-00-debug.log / cmd-00-retry.log). The
        # strict `root_names == ["cmd-00.log"]` check false-failed a real repair with
        # log-set-mismatch. Non-manifest-command root files are agent noise — tolerate.
        run = self._write_t10_run(tmp_path, n_iters=2, last_green=True)
        open(os.path.join(run, "cmd-00-debug.log"), "w").close()
        open(os.path.join(run, "cmd-00-retry.log"), "w").close()
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is True, res

    def test_t10_rejects_manifest_command_log_at_root(self, tmp_path):
        # The root check must STILL reject a manifest command log (cmd-01.log) at root —
        # that is the flat-layout mistake (commands belong in iter-K), not debug noise.
        run = self._write_t10_run(tmp_path, n_iters=2, last_green=True)
        with open(os.path.join(run, "cmd-01.log"), "w") as f:
            f.write("ASSERT_PASS\nEXIT=0\n")
        res = grade_run.grade(run, task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is False and res["reason"] == "log-set-mismatch"

    def test_t10_all_incomplete_grades_no_iterations(self, tmp_path):
        # If NO iter-K dir has a complete log set, there is no gradable cycle.
        run = tmp_path / "T10a-haiku-1"
        run.mkdir()
        (run / "cmd-00.log").write_text("Browser ok\nEXIT=0\n")
        (run / "iter-1").mkdir()
        (run / "iter-1" / "cmd-01.log").write_text("Navigated …\nEXIT=0\n")  # partial only
        res = grade_run.grade(str(run), task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS, integrity="pass")
        assert res["graded_success"] is False and res["reason"] == "no-iterations"

    def test_t10_no_iterations_grades_zero(self, tmp_path):
        run = tmp_path / "T10a-haiku-1"
        run.mkdir()
        (run / "cmd-00.log").write_text("Browser ok\nEXIT=0\n")
        res = grade_run.grade(str(run), task_id="T10a", classification="pass",
                              manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "no-iterations"

    def test_unknown_task_id_fails_loud(self, tmp_path):
        run = _write_run(tmp_path, ["EXIT=0\n"])
        with pytest.raises(KeyError):
            grade_run.grade(run, task_id="T99", classification="pass", manifests_path=MANIFESTS)

    def test_cli_emits_json(self, tmp_path):
        run = _write_run(tmp_path, [
            "EXIT=0\n", "loader=AABB\nEXIT=0\n", "CONSOLE_GATE_OK\nEXIT=0\n",
        ])
        r = subprocess.run([sys.executable, GRADER, "--run-dir", run, "--task", "T1",
                            "--classification", "pass"],
                           capture_output=True, text=True)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["graded_success"] is True

    def test_t9_loader_sentinel_question_mark_grades_failure(self, tmp_path):
        # cdp.py emits loader=? when loaderId is unavailable (both emitters default
        # to "?") — "?" == "?" must NOT pass the staleness check (PR #178 review).
        res = grade_run.grade(self._write_t9_run(tmp_path, "?", "?"),
                              task_id="T9", classification="pass", manifests_path=MANIFESTS)
        assert res["graded_success"] is False and res["reason"] == "loader-mismatch"

    def test_t10_iter_sort_is_numeric_with_name_tiebreak(self, tmp_path):
        # iter-10 must sort after iter-2 (numeric, not lexicographic); a stray
        # iter-007 alongside iter-7 must not make iters[-1] glob-order-dependent.
        run = self._write_t10_run(tmp_path, n_iters=2)
        # add iter-10 as the real last (green) cycle; iter-2 from the helper is red
        import shutil
        shutil.copytree(str(tmp_path / "T10a-haiku-1" / "iter-2"),
                        str(tmp_path / "T10a-haiku-1" / "iter-10"))
        (tmp_path / "T10a-haiku-1" / "iter-2" / "cmd-02.log").write_text(
            "ASSERT_FAIL #target-btn — never true within 5000ms\nEXIT=1\n")
        (tmp_path / "T10a-haiku-1" / "iter-2" / "cmd-03.log").write_text(
            "CLICK_REQUIRE_TRUSTED_FAIL: '#target-btn' not hittable\nEXIT=1\n")
        res = grade_run.grade(str(tmp_path / "T10a-haiku-1"), task_id="T10a",
                              classification="pass", manifests_path=MANIFESTS, integrity="pass")
        # iter-10 (green, copied from the helper's green iter-2) is the graded cycle
        assert res["iterations_observed"] == 3
        assert res["graded_success"] is True
