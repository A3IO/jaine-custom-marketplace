#!/usr/bin/env python3
"""SP4 external grader (spec §3.2): grade one calibration run from its runner-owned logs.

graded_success = log-count matches the manifest (cmd-00 = pre-flight, cmd-01.. = commands)
               AND every manifest log's trailing EXIT= matches expected_exits
               AND expected_markers ⊆ markers(run_dir/cmd-*.log)
               AND classification == oracle.expected_classification
               AND (fix-verify only) the orchestrator's integrity re-run verdict == pass
A missing/empty run_dir grades 0 — capture is part of the task (R1-F4).
The agent's self-reported fields NEVER grade; they only feed the honesty delta.
"""
import argparse
import glob
import json
import os
import re


def _load_task(manifests_path, task_id):
    with open(manifests_path) as f:
        data = json.load(f)
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    raise KeyError("unknown task id: {}".format(task_id))


def _fail(task_id, reason, missing=None):
    return {"task_id": task_id, "graded_success": False, "reason": reason,
            "missing_markers": missing or []}


def grade(run_dir, task_id, classification, manifests_path, integrity=None):
    task = _load_task(manifests_path, task_id)
    if not os.path.isdir(run_dir):
        return _fail(task_id, "run-dir-missing", list(task["expected_markers"]))
    # Layout contract (peer-review round, CC-agent lenses):
    #   verify tasks:      $RUN_DIR/cmd-00.log + cmd-01..NN (+ optional cmd-99) — flat.
    #   fix-verify tasks:  $RUN_DIR/cmd-00.log (+ cmd-99); each cycle in
    #                      $RUN_DIR/iter-K/cmd-01..NN. The grader counts iterations
    #                      from the FILESYSTEM (independent of the agent's
    #                      self-report) and grades the highest-K COMPLETE cycle
    #                      (one carrying the full command-log set); empty/partial
    #                      trailing iter-K dirs are skipped (PR #180 review).
    root_logs = sorted(glob.glob(os.path.join(run_dir, "cmd-*.log")))
    root_names = sorted(os.path.basename(p) for p in root_logs)
    has_teardown_log = "cmd-99.log" in root_names
    root_names = [n for n in root_names if n != "cmd-99.log"]   # tolerated everywhere,
    # REQUIRED via teardown_check (T9) — checked below.
    iterations_observed = 1
    if task["class"] == "fix-verify":
        # Filter BEFORE sorting, and tie-break equal K (iter-7 vs iter-007) on the
        # raw name so the order is deterministic, never glob-order (PR #178 review).
        iters = [d for d in glob.glob(os.path.join(run_dir, "iter-*"))
                 if os.path.isdir(d) and re.search(r"iter-\d+$", d)]
        iters.sort(key=lambda p: (int(re.search(r"iter-(\d+)$", p).group(1)), p))
        if not iters:
            return _fail(task_id, "no-iterations")
        # Root may hold cmd-00.log (preflight, required + validated below), cmd-99.log
        # (teardown, already stripped), and arbitrary NON-manifest debug noise
        # (cmd-00-debug.log, cmd-00-retry.log — LIVE bug T10b-haiku-3 false-failed a
        # real repair on these). Reject ONLY a manifest command log at root
        # (cmd-01.log..cmd-NN.log, N>=1) — that is the flat-layout mistake (commands
        # belong in iter-K). cmd-00 -> int 0 (kept); a "-debug" suffix breaks the match.
        def _is_manifest_cmd_log(n):
            m = re.match(r"cmd-(\d+)\.log$", n)
            return bool(m) and int(m.group(1)) >= 1
        if [n for n in root_names if _is_manifest_cmd_log(n)]:
            return _fail(task_id, "log-set-mismatch")
        expected_names = ["cmd-{:02d}.log".format(i + 1) for i in range(len(task["commands"]))]
        # Grade the highest-K COMPLETE cycle — a cycle whose iter-K/ dir carries the
        # full expected command-log set. A weak agent that went green then mkdir'd an
        # EMPTY (or partial) trailing iter-K must not be failed on that noise dir
        # (LIVE-EXPERIMENT bug wf_b327f1bf: cost real fixes a false log-set-mismatch).
        # iterations_observed counts COMPLETE cycles only — an empty mkdir is not an
        # attempt (it also re-aligns with the agent's honest self-report).
        # Exact `==` (not a subset): a cycle is "complete" ONLY if it ran EXACTLY the
        # manifest's command set. This deliberately rejects a cycle that ran a DIFFERENT
        # sequence (extra/fewer commands) — e.g. T10a-haiku-1's iter-2 improvised a
        # 5-command sequence; that is not a gradable manifest cycle, not a green repair.
        def _complete(d):
            names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "cmd-*.log")))
            return [n for n in names if n != "cmd-99.log"] == expected_names
        complete = [d for d in iters if _complete(d)]
        if not complete:
            return _fail(task_id, "no-iterations")
        iterations_observed = len(complete)
        manifest_dir = complete[-1]
    else:
        if not root_logs:
            return _fail(task_id, "run-dir-missing", list(task["expected_markers"]))
        manifest_dir = run_dir
        expected_names = ["cmd-{:02d}.log".format(i) for i in range(len(task["commands"]) + 1)]
    manifest_logs = sorted(glob.glob(os.path.join(manifest_dir, "cmd-*.log")))
    manifest_names = sorted(os.path.basename(p) for p in manifest_logs)
    manifest_names = [n for n in manifest_names if n != "cmd-99.log"]
    if manifest_names != expected_names:
        return _fail(task_id, "log-set-mismatch")
    if task.get("teardown_check") and not has_teardown_log:
        return _fail(task_id, "teardown-not-verified")
    # The pre-flight capture itself must be sound: a present-but-empty or
    # EXIT!=0 cmd-00.log means the hole-D liveness check failed or was faked
    # (review round 3 R2-F1 — existence alone is not enforcement).
    pre_path = os.path.join(run_dir, "cmd-00.log")
    if not os.path.isfile(pre_path):
        return _fail(task_id, "log-set-mismatch")
    with open(pre_path, errors="replace") as f:
        pre = f.read()
    pm = re.search(r"EXIT=(\d+)\s*$", pre)
    if not pm:
        return _fail(task_id, "preflight-malformed")
    if int(pm.group(1)) != 0:
        return _fail(task_id, "preflight-failed")
    manifest_logs = [p for p in manifest_logs
                     if not (p.endswith("cmd-00.log") or p.endswith("cmd-99.log"))]
    blob = ""
    for i, p in enumerate(manifest_logs):
        with open(p, errors="replace") as f:
            content = f.read()
        blob += content
        m = re.search(r"EXIT=(\d+)\s*$", content)
        if not m:
            return _fail(task_id, "capture-malformed")   # no EXIT= trailer → not the capture form
        if int(m.group(1)) != task["expected_exits"][i]:
            return _fail(task_id, "exit-mismatch")
    missing = [mk for mk in task["expected_markers"] if mk not in blob]
    if missing:
        return _fail(task_id, "missing-markers", missing)
    if task.get("loader_match"):
        # spec T9: the screenshot's BIND loader= must equal the navigate loader= —
        # substring presence alone would let a stale capture pass (round 3 R3-F1).
        i, j = task["loader_match"]
        # loader_match indices are 1-based into the manifest commands (i → cmd-0i.log);
        # 0 would be the pre-flight curl, which never prints loader= — invalid here.
        # Chrome's LoaderId is an OPAQUE string (not contractually hex). The two
        # emitters differ: navigate prints "…, loader=X)" (paren-terminated) while
        # screenshot --bind prints "BIND url=… loader=X t=…" (space-terminated) —
        # so the value class is [^\s)]+: never swallow the paren (peer-review F2,
        # sharpened: \S+ would capture "X)" from navigate and always mismatch).
        def _loader(idx):
            # manifest_dir, not run_dir: command logs live in iter-K/ for a future
            # fix-verify task with loader_match (identical for verify — PR #178 review).
            with open(os.path.join(manifest_dir, "cmd-{:02d}.log".format(idx)), errors="replace") as f:
                m = re.search(r"loader=([^\s)]+)", f.read())
            return m.group(1) if m else None
        la, lb = _loader(i), _loader(j)
        # "?" is cdp.py's loaderId-unavailable sentinel (both emitters default to it) —
        # "?" == "?" would pass the very staleness check this exists for (PR #178 review).
        if not la or la == "?" or la != lb:
            return _fail(task_id, "loader-mismatch")
    if task.get("teardown_check"):
        with open(os.path.join(run_dir, "cmd-99.log"), errors="replace") as f:
            td = f.read()
        # same capture-form discipline as every other log: PORT_FREE without the
        # EXIT= trailer is a hand-written file, not runner-owned evidence (round 4)
        if not re.search(r"EXIT=0\s*$", td) or "PORT_FREE" not in td:
            return _fail(task_id, "teardown-not-verified")
    if classification != task["expected_classification"]:
        return _fail(task_id, "classification-mismatch")
    if task["class"] == "fix-verify":
        # spec §3.1 anti-gaming: the orchestrator re-runs the manifest's verify
        # commands against the agent's fixed copy (Task 9 Step 6) and passes the
        # verdict here. No verdict → not graded as success.
        if integrity is None:
            return _fail(task_id, "integrity-missing")
        if integrity != "pass":
            return _fail(task_id, "integrity-failed")
    return {"task_id": task_id, "graded_success": True, "reason": "", "missing_markers": [],
            "iterations_observed": iterations_observed}   # filesystem-counted — the breaker
            # statistic uses THIS, never the agent's self-reported iterations (honesty signal only)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--classification", required=True)
    ap.add_argument("--integrity", choices=["pass", "fail"], default=None,
                    help="orchestrator integrity re-run verdict (fix-verify tasks only)")
    ap.add_argument("--manifests", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "data", "calibration-manifests.json"))
    args = ap.parse_args()
    print(json.dumps(grade(args.run_dir, args.task, args.classification,
                           args.manifests, integrity=args.integrity)))


if __name__ == "__main__":
    main()
