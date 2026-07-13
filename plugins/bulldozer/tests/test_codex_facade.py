"""Tests for mcp/codex_facade.py — the facade multiplexer (#344).

Spec: docs/superpowers/specs/2026-07-13-codex-facade-multiplexer-design.md.
This file covers the PURE scheduler core first (§3.2 posture classification +
admission rules), then the worker/pool plumbing with fake workers (scripted
subprocess stubs) per the spec's §5 offline-unit plan.
"""

import os
import queue
import subprocess
import sys
import threading

import pytest

MCP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mcp")
sys.path.insert(0, MCP_DIR)

import codex_facade  # noqa: E402


# ---------------------------------------------------------------------------
# §3.2(3) — dispatch-args preparation: codex_review policy injection
# ---------------------------------------------------------------------------

class TestPrepareDispatchArgs:
    def test_codex_review_injects_never(self):
        args = {"target": "uncommitted", "mcp": "isolated"}
        out = codex_facade.prepare_dispatch_args("codex_review", args)
        assert out["approval_policy"] == "never"

    def test_codex_review_injection_does_not_mutate_caller_args(self):
        args = {"target": "uncommitted", "mcp": "isolated"}
        codex_facade.prepare_dispatch_args("codex_review", args)
        assert "approval_policy" not in args

    def test_codex_review_explicit_policy_still_forced_never(self):
        # The injection is unconditional: review turns are the structural
        # parallel class; a caller-provided policy on codex_review is not a
        # supported surface (the tool schema exposes no approval arg).
        args = {"target": "uncommitted", "approval_policy": "on-request"}
        out = codex_facade.prepare_dispatch_args("codex_review", args)
        assert out["approval_policy"] == "never"

    def test_codex_run_args_forwarded_verbatim(self):
        args = {"prompt": "p", "mcp": "isolated", "approval_policy": "on-request"}
        out = codex_facade.prepare_dispatch_args("codex_run", args)
        assert out == args

    def test_codex_run_no_injection_when_policy_omitted(self):
        # §3.1: forward verbatim — the review injection is the ONLY exception.
        args = {"prompt": "p", "mcp": "isolated"}
        out = codex_facade.prepare_dispatch_args("codex_run", args)
        assert "approval_policy" not in out


# ---------------------------------------------------------------------------
# §3.2 — posture classification
# ---------------------------------------------------------------------------

def _classify(tool="codex_run", args=None, thread_map=None):
    tm = thread_map if thread_map is not None else codex_facade.ThreadMap()
    prepared = codex_facade.prepare_dispatch_args(tool, args or {})
    return codex_facade.classify_call(tool, prepared, tm)


class TestClassifyApprovalCapability:
    def test_read_only_never_is_parallel_class(self):
        p = _classify(args={"sandbox": "read-only", "approval_policy": "never"})
        assert p.approval_capable is False
        assert p.global_writer is False
        assert p.root is None

    def test_default_codex_run_is_approval_capable(self):
        # Engine default is on-request (approval_policy_for_start or "on-request").
        p = _classify(args={"sandbox": "read-only"})
        assert p.approval_capable is True

    @pytest.mark.parametrize("policy", ["on-request", "untrusted", "on-failure"])
    def test_any_non_never_policy_is_approval_capable(self, policy):
        p = _classify(args={"approval_policy": policy})
        assert p.approval_capable is True

    def test_approval_capable_is_global_writer(self):
        # r5: a mid-turn fileSystem grant can cover ANY root — the grant-holder
        # must not overlap any writer, so approval-capable ⇒ global writer.
        p = _classify(args={"sandbox": "read-only"})
        assert p.global_writer is True

    def test_prepared_codex_review_is_parallel_class(self):
        p = _classify(tool="codex_review", args={"target": "uncommitted"})
        assert p.approval_capable is False
        assert p.global_writer is False


class TestClassifyWritableRoot:
    def test_workspace_write_registers_canonical_root(self, tmp_path):
        real = tmp_path / "repo"
        real.mkdir()
        link = tmp_path / "alias"
        link.symlink_to(real)
        p = _classify(args={
            "sandbox": "workspace-write", "approval_policy": "never",
            "cwd": str(link),
        })
        assert p.root == os.path.realpath(str(real))

    def test_workspace_write_omitted_cwd_has_no_root(self):
        # Omitted cwd = the worker's own isolated tmpdir → freely parallel.
        p = _classify(args={"sandbox": "workspace-write", "approval_policy": "never"})
        assert p.root is None
        assert p.global_writer is False

    def test_danger_full_access_is_global_writer(self):
        p = _classify(args={
            "sandbox": "danger-full-access", "approval_policy": "never",
        })
        assert p.global_writer is True

    def test_read_only_holds_no_root_even_with_cwd(self, tmp_path):
        p = _classify(args={
            "sandbox": "read-only", "approval_policy": "never",
            "cwd": str(tmp_path),
        })
        assert p.root is None


class TestThreadMapStickyWidening:
    """r6 P1: session grants outlive the turn and are unobservable in dialog
    mode → a thread that EVER ran an approval-capable turn schedules as
    approval-capable/global-writer forever after, even on explicit never.

    Real flow modeled here: a NEW thread starts WITHOUT thread_id (the tool
    contract), the facade learns the id from the worker's RESULT and binds the
    posture via ThreadMap.bind(); explicit resumes go through record_dispatch.
    """

    def test_sticky_thread_ignores_explicit_never(self):
        tm = codex_facade.ThreadMap()
        first = _classify(args={}, thread_map=tm)  # new thread, on-request
        assert first.approval_capable is True
        tm.bind("t1", first)
        resumed = _classify(
            args={"thread_id": "t1", "approval_policy": "never"}, thread_map=tm)
        assert resumed.approval_capable is True
        assert resumed.global_writer is True

    def test_never_thread_stays_parallel_on_omitted_resume(self):
        tm = codex_facade.ThreadMap()
        first = _classify(args={"approval_policy": "never"}, thread_map=tm)
        tm.bind("t2", first)
        resumed = _classify(args={"thread_id": "t2"}, thread_map=tm)
        # Omitted args inherit the thread's persisted posture — never-thread
        # resume stays in the parallel class (engine inherits thread posture).
        assert resumed.approval_capable is False

    def test_explicit_upgrade_marks_sticky(self):
        tm = codex_facade.ThreadMap()
        first = _classify(args={"approval_policy": "never"}, thread_map=tm)
        tm.bind("t3", first)
        upgraded = _classify(
            args={"thread_id": "t3", "approval_policy": "on-request"},
            thread_map=tm)
        assert upgraded.approval_capable is True
        tm.record_dispatch(upgraded)
        back = _classify(
            args={"thread_id": "t3", "approval_policy": "never"}, thread_map=tm)
        assert back.approval_capable is True  # upward-only, never un-widens

    def test_unknown_thread_resume_is_conservative(self):
        # Cross-session resume of a thread the facade never saw: approval-capable
        # AND global writer — correctness over throughput.
        tm = codex_facade.ThreadMap()
        p = _classify(
            args={"thread_id": "cold", "approval_policy": "never"},
            thread_map=tm)
        assert p.approval_capable is True
        assert p.global_writer is True

    def test_omitted_resume_inherits_persisted_root(self, tmp_path):
        tm = codex_facade.ThreadMap()
        first = _classify(args={
            "approval_policy": "never",
            "sandbox": "workspace-write", "cwd": str(tmp_path),
        }, thread_map=tm)
        tm.bind("t4", first)
        resumed = _classify(args={"thread_id": "t4"}, thread_map=tm)
        assert resumed.root == os.path.realpath(str(tmp_path))

    def test_explicit_refresh_updates_root(self, tmp_path):
        # r3 P1: an explicit resume can UPGRADE a thread (read-only →
        # workspace-write); later omitted resumes inherit the NEW value.
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(); b.mkdir()
        tm = codex_facade.ThreadMap()
        first = _classify(args={
            "approval_policy": "never",
            "sandbox": "workspace-write", "cwd": str(a),
        }, thread_map=tm)
        tm.bind("t5", first)
        second = _classify(args={
            "thread_id": "t5", "approval_policy": "never",
            "sandbox": "workspace-write", "cwd": str(b),
        }, thread_map=tm)
        tm.record_dispatch(second)
        resumed = _classify(args={"thread_id": "t5"}, thread_map=tm)
        assert resumed.root == os.path.realpath(str(b))

    def test_bind_stamps_thread_id_into_stored_posture(self):
        tm = codex_facade.ThreadMap()
        first = _classify(args={"approval_policy": "never"}, thread_map=tm)
        assert first.thread_id is None
        tm.bind("t6", first)
        assert tm.persisted("t6").thread_id == "t6"

    def test_bind_of_capable_new_thread_marks_sticky(self):
        tm = codex_facade.ThreadMap()
        first = _classify(args={}, thread_map=tm)  # on-request default
        tm.bind("t7", first)
        assert tm.is_sticky("t7") is True


# ---------------------------------------------------------------------------
# §3.2 — admission rules (pure pairwise conflict check)
# ---------------------------------------------------------------------------

def _posture(root=None, global_writer=False, approval_capable=False,
             thread_id=None):
    return codex_facade.Posture(
        tool="codex_run", root=root, global_writer=global_writer,
        approval_capable=approval_capable, thread_id=thread_id)


class TestAdmissionRules:
    def test_two_parallel_class_calls_admit(self):
        assert codex_facade.conflicts(_posture(), _posture()) is False

    def test_disjoint_roots_admit(self):
        a = _posture(root="/w/a")
        b = _posture(root="/w/b")
        assert codex_facade.conflicts(a, b) is False

    def test_equal_roots_conflict(self):
        a = _posture(root="/w/a")
        b = _posture(root="/w/a")
        assert codex_facade.conflicts(a, b) is True

    def test_ancestor_descendant_roots_conflict_both_ways(self):
        outer = _posture(root="/w/a")
        inner = _posture(root="/w/a/sub")
        assert codex_facade.conflicts(outer, inner) is True
        assert codex_facade.conflicts(inner, outer) is True

    def test_prefix_sibling_roots_do_not_conflict(self):
        # /w/ab is NOT inside /w/a — naive startswith would say it is.
        a = _posture(root="/w/a")
        b = _posture(root="/w/ab")
        assert codex_facade.conflicts(a, b) is False

    def test_global_writer_conflicts_with_any_writer(self):
        g = _posture(global_writer=True)
        w = _posture(root="/w/a")
        assert codex_facade.conflicts(g, w) is True
        assert codex_facade.conflicts(w, g) is True

    def test_global_writer_admits_with_parallel_class(self):
        # r5/r6: exclusion is against WRITERS; never+read-only fans out beside it.
        g = _posture(global_writer=True, approval_capable=True)
        r = _posture()
        assert codex_facade.conflicts(g, r) is False

    def test_two_global_writers_conflict(self):
        assert codex_facade.conflicts(
            _posture(global_writer=True), _posture(global_writer=True)) is True

    def test_two_approval_capable_conflict(self):
        # The funnel: at most one approval-capable turn at a time. Asserted
        # with global_writer=False (a combination classify_call never emits)
        # so this test guards the funnel CLAUSE itself — with global_writer
        # set, the writer rule would mask a deleted funnel check.
        a = _posture(approval_capable=True)
        b = _posture(approval_capable=True)
        assert codex_facade.conflicts(a, b) is True

    def test_same_thread_conflicts(self):
        # Rule 2: ONE in-flight turn per thread.
        a = _posture(thread_id="t")
        b = _posture(thread_id="t")
        assert codex_facade.conflicts(a, b) is True

    def test_different_threads_admit(self):
        a = _posture(thread_id="t1")
        b = _posture(thread_id="t2")
        assert codex_facade.conflicts(a, b) is False


# ---------------------------------------------------------------------------
# §3.2 — Scheduler: FIFO queue + dequeue-time re-evaluation + park reservations
# ---------------------------------------------------------------------------

class TestSchedulerQueue:
    def _sched(self):
        return codex_facade.Scheduler()

    def test_non_conflicting_dispatches_immediately(self):
        s = self._sched()
        assert s.submit("c1", _posture()) is True
        assert s.submit("c2", _posture()) is True

    def test_conflicting_call_queues(self):
        s = self._sched()
        assert s.submit("w1", _posture(root="/w")) is True
        assert s.submit("w2", _posture(root="/w")) is False

    def test_release_dispatches_queued(self):
        s = self._sched()
        s.submit("w1", _posture(root="/w"))
        s.submit("w2", _posture(root="/w"))
        freed = s.release("w1")
        assert freed == ["w2"]

    def test_fifo_order_preserved_among_conflicting(self):
        s = self._sched()
        s.submit("w1", _posture(root="/w"))
        s.submit("w2", _posture(root="/w"))
        s.submit("w3", _posture(root="/w"))
        freed = s.release("w1")
        # Only w2 dispatches — w3 still conflicts with the now-active w2.
        assert freed == ["w2"]
        freed = s.release("w2")
        assert freed == ["w3"]

    def test_release_dispatches_multiple_independent(self):
        s = self._sched()
        s.submit("g1", _posture(global_writer=True))
        s.submit("a", _posture(root="/a"))
        s.submit("b", _posture(root="/b"))
        freed = s.release("g1")
        # Both disjoint writers were blocked only by the global writer.
        assert freed == ["a", "b"]

    def test_new_call_must_not_jump_conflicting_queue(self):
        # Anti-starvation: a call that conflicts with a QUEUED-ahead call
        # queues behind it even if no ACTIVE call conflicts. (Two same-root
        # writers queued behind a global writer must drain in order — if w2
        # checked only active it would jump w1 on release.)
        s = self._sched()
        s.submit("g1", _posture(global_writer=True))
        assert s.submit("w1", _posture(root="/w")) is False
        assert s.submit("w2", _posture(root="/w")) is False
        freed = s.release("g1")
        assert freed == ["w1"]

    def test_non_conflicting_bypass_is_allowed(self):
        # A reader is NOT held hostage by a queued writer class it does not
        # conflict with.
        s = self._sched()
        s.submit("w1", _posture(root="/w"))
        s.submit("w2", _posture(root="/w"))          # queued
        assert s.submit("r1", _posture()) is True    # bypasses freely

    def test_approval_funnel_fifo(self):
        s = self._sched()
        a = _posture(global_writer=True, approval_capable=True)
        b = _posture(global_writer=True, approval_capable=True)
        assert s.submit("a1", a) is True
        assert s.submit("a2", b) is False
        freed = s.release("a1")
        assert freed == ["a2"]

    def test_cancel_queued_removes_call(self):
        s = self._sched()
        s.submit("w1", _posture(root="/w"))
        s.submit("w2", _posture(root="/w"))
        removed, freed = s.cancel_queued("w2")
        assert removed is True and freed == []
        assert s.release("w1") == []

    def test_cancel_queued_unknown_or_active_returns_false(self):
        s = self._sched()
        s.submit("w1", _posture(root="/w"))
        assert s.cancel_queued("w1")[0] is False   # active, not queued
        assert s.cancel_queued("nope")[0] is False

    def test_cancel_queued_drains_calls_behind_it(self):
        # review P2: removing a queued blocker must re-evaluate the calls it
        # was blocking — C conflicts only with B, not with the active A.
        s = self._sched()
        s.submit("A", _posture(root="/w"))
        s.submit("B", _posture(global_writer=True))   # queues behind A
        s.submit("C", _posture(root="/x"))            # queues behind B
        removed, freed = s.cancel_queued("B")
        assert removed is True
        assert freed == ["C"]     # C dispatches immediately

    def test_release_unknown_call_is_noop(self):
        s = self._sched()
        assert s.release("ghost") == []


class TestSchedulerParkReservation:
    """§3.2 rule 1: the root reservation OUTLIVES the MCP call when the turn
    parks — held until resume completes, the mirrored cap expires, or the
    park-ended signal fires (all surfaced to the scheduler as release())."""

    def test_park_transfer_keeps_reservation(self):
        s = codex_facade.Scheduler()
        s.submit("c1", _posture(root="/w", global_writer=True,
                                approval_capable=True))
        s.park_transfer("c1", "tok-1")
        # Reservation still held under the park token: same-root writer queues.
        assert s.submit("w2", _posture(root="/w")) is False

    def test_release_by_park_token_frees_queue(self):
        s = codex_facade.Scheduler()
        s.submit("c1", _posture(root="/w", global_writer=True,
                                approval_capable=True))
        s.park_transfer("c1", "tok-1")
        s.submit("w2", _posture(root="/w"))
        freed = s.release("tok-1")
        assert freed == ["w2"]

    def test_original_call_id_gone_after_transfer(self):
        s = codex_facade.Scheduler()
        s.submit("c1", _posture(root="/w"))
        s.park_transfer("c1", "tok-1")
        assert s.release("c1") == []
        assert s.submit("w2", _posture(root="/w")) is False

    def test_release_respects_queued_ahead_order(self):
        # Fairness inside the queue at RELEASE time: C(/x) does not conflict
        # any ACTIVE call after the release, but conflicts the earlier-queued
        # global writer B — it must NOT jump B (starvation of B otherwise).
        s = codex_facade.Scheduler()
        s.submit("w", _posture(root="/w"))
        s.submit("r", _posture())                        # unrelated reader
        s.submit("B", _posture(global_writer=True))      # queued (vs w)
        s.submit("C", _posture(root="/x"))               # queued (vs B)
        freed = s.release("r")
        assert freed == []                               # B still blocked by w; C waits behind B
        freed = s.release("w")
        assert freed == ["B"]


class TestSchedulerCapacity:
    """§3.1: capacity is the pool's REAL worker count — the scheduler asks the
    facade to PLACE a call (place_fn) and queues it when no worker is free.
    Admission and placement are one atomic step (review r2 P1)."""

    def _pool(self, size):
        """A fake worker pool: place_fn succeeds while a slot is free."""
        state = {"busy": set()}

        def place(call_id):
            if len(state["busy"]) >= size:
                return False
            state["busy"].add(call_id)
            return True

        def done(call_id):
            state["busy"].discard(call_id)
        return state, place, done

    def test_admits_up_to_cap(self):
        s = codex_facade.Scheduler()
        _st, place, _done = self._pool(2)
        assert s.submit("r1", _posture(), place_fn=place) is True
        assert s.submit("r2", _posture(), place_fn=place) is True
        assert s.submit("r3", _posture(), place_fn=place) is False   # no worker

    def test_release_admits_next_fifo_when_slot_frees(self):
        s = codex_facade.Scheduler()
        _st, place, done = self._pool(2)
        for cid in ("r1", "r2", "r3", "r4"):
            s.submit(cid, _posture(), place_fn=place)
        done("r1")
        assert s.release("r1", place_fn=place) == ["r3"]
        done("r2")
        assert s.release("r2", place_fn=place) == ["r4"]

    def test_parked_reservation_still_occupies_slot(self):
        s = codex_facade.Scheduler()
        _st, place, done = self._pool(1)
        s.submit("c1", _posture(), place_fn=place)
        s.park_transfer("c1", "tok")      # worker stays pinned by the park
        assert s.submit("c2", _posture(), place_fn=place) is False
        done("c1")                        # (the pool frees only on release)
        assert s.release("tok", place_fn=place) == ["c2"]

    def test_no_place_fn_is_uncapped(self):
        s = codex_facade.Scheduler()
        for i in range(20):
            assert s.submit(f"r{i}", _posture()) is True


# ---------------------------------------------------------------------------
# §3.1/§3.4/§3.5 — Facade plumbing with fake workers (scripted subprocess stubs)
# ---------------------------------------------------------------------------

import json      # noqa: E402
import queue     # noqa: E402
import time      # noqa: E402

FAKE_WORKER = os.path.join(os.path.dirname(__file__), "fixtures", "fake_worker.py")


class CCSide:
    """Captures facade→CC frames; lets tests await specific frames like CC
    would. Non-matching frames are STASHED, never dropped — two results can
    arrive in any order and both remain awaitable."""

    def __init__(self):
        self.frames = queue.Queue()
        self.seen = []
        self._stash = []

    def write(self, frame):
        self.seen.append(frame)
        self.frames.put(frame)

    def wait_for(self, pred, what, timeout=5):
        for i, f in enumerate(self._stash):
            if pred(f):
                return self._stash.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                f = self.frames.get(timeout=remaining)
            except queue.Empty:
                break
            if pred(f):
                return f
            self._stash.append(f)
        raise AssertionError(f"no frame matching {what}; saw: "
                             f"{[(f.get('id'), f.get('method')) for f in self.seen]}")

    def wait_for_id(self, mid, timeout=5):
        return self.wait_for(
            lambda f: f.get("id") == mid and "method" not in f,
            f"response id={mid!r}", timeout)

    def wait_for_request(self, method, timeout=5):
        return self.wait_for(
            lambda f: f.get("method") == method,
            f"request {method}", timeout)


def _payload(frame):
    return json.loads(frame["result"]["content"][0]["text"])


def _call_frame(mid, args=None, tool="codex_run"):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}}}


@pytest.fixture
def facade():
    cc = CCSide()
    f = codex_facade.Facade(
        cc_write=cc.write,
        worker_argv=[sys.executable, FAKE_WORKER],
        max_workers=2,
    )
    yield f, cc
    f.shutdown(timeout=3)


class TestFacadePlumbing:
    def test_initialize_answered_by_facade_itself(self, facade):
        f, cc = facade
        f.handle_cc_frame({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                           "params": {"protocolVersion": "1",
                                      "capabilities": {}, "clientInfo": {}}})
        resp = cc.wait_for_id(0)
        assert resp["result"]["serverInfo"]["name"]
        assert "parallel" in resp["result"]["instructions"].lower()
        assert f.worker_count() == 0    # lazy: no worker spawned for initialize

    def test_tools_list_served_from_engine_constants(self, facade):
        f, cc = facade
        f.handle_cc_frame({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        resp = cc.wait_for_id(1)
        names = {t["name"] for t in resp["result"]["tools"]}
        assert {"codex_run", "codex_info", "codex_review",
                "codex_approve"} <= names
        assert f.worker_count() == 0

    def test_tool_call_roundtrip(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(10, {"approval_policy": "never",
                                           "_fake": {"result": {"tag": "A"}}}))
        resp = cc.wait_for_id(10)
        body = _payload(resp)
        assert body["tag"] == "A"
        assert f.worker_count() == 1

    def test_two_parallel_calls_overlap_on_two_workers(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(20, {"approval_policy": "never",
                                           "_fake": {"sleep": 0.6, "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(21, {"approval_policy": "never",
                                           "_fake": {"sleep": 0.6, "result": {"tag": "B"}}}))
        pids = set()
        for mid in (20, 21):
            body = _payload(cc.wait_for_id(mid, timeout=5))
            pids.add(body["pid"])
        assert len(pids) == 2           # two distinct worker processes
        # Overlap is asserted from the WORKERS' own intervals, not host
        # wall-clock (review P2 — see test_overlap_proven_by_worker_intervals).

    def test_third_call_queues_at_cap_then_runs(self, facade):
        f, cc = facade
        for mid, tag in ((30, "A"), (31, "B"), (32, "C")):
            f.handle_cc_frame(_call_frame(mid, {"approval_policy": "never",
                                                "_fake": {"sleep": 0.3, "result": {"tag": tag}}}))
        assert f.worker_count() == 2    # cap respected — no third spawn
        for mid in (30, 31, 32):
            _payload(cc.wait_for_id(mid, timeout=5))

    def test_colliding_elicitation_ids_are_remapped(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(40, {"approval_policy": "never",
                                           "_fake": {"sleep": 0.05, "elicit": {"who": "A"},
                                                     "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(41, {"approval_policy": "never",
                                           "_fake": {"sleep": 0.05, "elicit": {"who": "B"},
                                                     "result": {"tag": "B"}}}))
        r1 = cc.wait_for_request("elicitation/create")
        r2 = cc.wait_for_request("elicitation/create")
        # Both fakes used LOCAL id 1000 — the facade must present distinct ids.
        assert r1["id"] != r2["id"]
        by_who = {r["params"]["who"]: r for r in (r1, r2)}
        # Reply to each facade id; each worker must get ITS OWN answer back.
        f.handle_cc_frame({"jsonrpc": "2.0", "id": by_who["A"]["id"],
                           "result": {"action": "accept", "content": {"for": "A"}}})
        f.handle_cc_frame({"jsonrpc": "2.0", "id": by_who["B"]["id"],
                           "result": {"action": "accept", "content": {"for": "B"}}})
        bodies = {}
        for mid in (40, 41):
            body = _payload(cc.wait_for_id(mid, timeout=5))
            bodies[body["tag"]] = body
        assert bodies["A"]["elicit_reply"]["content"]["for"] == "A"
        assert bodies["B"]["elicit_reply"]["content"]["for"] == "B"

    def test_result_thread_id_binds_posture(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(50, {"approval_policy": "never",
                                           "_fake": {"result": {"thread_id": "th-1"}}}))
        cc.wait_for_id(50)
        assert f.thread_map().known("th-1")
        assert f.thread_map().is_sticky("th-1") is False

    def test_conflicting_calls_serialize_via_scheduler(self, facade, tmp_path):
        f, cc = facade
        root = str(tmp_path)
        t0 = time.monotonic()
        f.handle_cc_frame(_call_frame(60, {"approval_policy": "never",
                                           "sandbox": "workspace-write", "cwd": root,
                                           "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(61, {"approval_policy": "never",
                                           "sandbox": "workspace-write", "cwd": root,
                                           "_fake": {"sleep": 0.3, "result": {"tag": "B"}}}))
        _payload(cc.wait_for_id(60, timeout=5))
        _payload(cc.wait_for_id(61, timeout=5))
        assert time.monotonic() - t0 >= 0.55   # serialized: wall ≈ sum


class TestFacadeParkAffinity:
    """§3.1 park affinity (#277): awaiting_approval registers park_token →
    worker; codex_approve routes HOME; the reservation outlives the call."""

    def test_park_resume_routes_to_same_worker(self, facade, tmp_path):
        f, cc = facade
        root = str(tmp_path)
        f.handle_cc_frame(_call_frame(70, {
            "sandbox": "workspace-write", "cwd": root,     # on-request default
            "_fake": {"park": {"token": "tok-70"}}}))
        parked = _payload(cc.wait_for_id(70))
        assert parked["status"] == "awaiting_approval"
        park_pid = parked["pid"]
        # Reservation held: a same-root writer queues while parked...
        f.handle_cc_frame(_call_frame(71, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": root, "_fake": {"result": {"tag": "W"}}}))
        # ...and resumes only after codex_approve completes the parked turn.
        f.handle_cc_frame(_call_frame(72, {"park_token": "tok-70",
                                           "decision_id": "accept"},
                                      tool="codex_approve"))
        resumed = _payload(cc.wait_for_id(72))
        assert resumed["resumed"] is True          # SAME process resumed it
        assert resumed["pid"] == park_pid
        writer = _payload(cc.wait_for_id(71))
        assert writer["tag"] == "W"

    def test_unknown_park_token_synthesized_expired(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(75, {"park_token": "ghost",
                                           "decision_id": "accept"},
                                      tool="codex_approve"))
        resp = cc.wait_for_id(75)
        body = _payload(resp)
        assert "expired" in body["error"]
        assert resp["result"].get("isError") is True
        assert f.worker_count() == 0    # no worker spawned for a dead token

    def test_parked_worker_not_dispatched_other_calls(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(80, {
            "_fake": {"park": {"token": "tok-80"}}}))
        park_pid = _payload(cc.wait_for_id(80))["pid"]
        f.handle_cc_frame(_call_frame(81, {"approval_policy": "never",
                                           "_fake": {"result": {"tag": "B"}}}))
        other = _payload(cc.wait_for_id(81))
        assert other["pid"] != park_pid    # parked worker is pinned


class TestFacadeCancel:
    def test_cancel_queued_call_never_executes(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(90, {"approval_policy": "never",
                                           "_fake": {"sleep": 0.4, "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(91, {"approval_policy": "never",
                                           "_fake": {"sleep": 0.4, "result": {"tag": "B"}}}))
        f.handle_cc_frame(_call_frame(92, {"approval_policy": "never",
                                           "_fake": {"result": {"tag": "C"}}}))   # queued at cap
        f.handle_cc_frame({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": 92}})
        body = _payload(cc.wait_for_id(92))
        assert body["status"] == "interrupted"
        assert body["queued"] is True
        _payload(cc.wait_for_id(90, timeout=5))
        _payload(cc.wait_for_id(91, timeout=5))
        time.sleep(0.15)   # grace: a wrongly-dispatched 92 would reply now
        replies_92 = [fr for fr in cc.seen
                      if fr.get("id") == 92 and "method" not in fr]
        assert len(replies_92) == 1    # ONLY the synthesized interrupt

    def test_cancel_dispatched_call_forwards_to_worker(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(95, {"approval_policy": "never",
                                           "_fake": {"sleep": 30, "result": {"tag": "A"}}}))
        t0 = time.monotonic()
        # give the worker a beat to enter the sleep, then cancel
        time.sleep(0.1)
        f.handle_cc_frame({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": 95}})
        body = _payload(cc.wait_for_id(95, timeout=5))
        assert body["status"] == "interrupted"
        assert body["interrupted_by"] == "cancel"
        assert time.monotonic() - t0 < 5    # not the 30s sleep


class TestFacadeDesignatedWorker:
    def test_codex_info_waits_for_designated_worker(self, facade):
        f, cc = facade
        # Approval-capable (default on-request) occupies the DESIGNATED worker.
        f.handle_cc_frame(_call_frame(100, {
            "_fake": {"sleep": 0.5, "result": {"tag": "APPROVAL"}}}))
        time.sleep(0.1)   # let it dispatch and start sleeping
        f.handle_cc_frame(_call_frame(101, {}, tool="codex_info"))
        # A parallel-class call is NOT held hostage meanwhile.
        f.handle_cc_frame(_call_frame(102, {"approval_policy": "never",
                                            "_fake": {"result": {"tag": "FREE"}}}))
        free = _payload(cc.wait_for_id(102, timeout=3))
        approval = _payload(cc.wait_for_id(100, timeout=3))
        info = _payload(cc.wait_for_id(101, timeout=3))
        assert info["pid"] == approval["pid"]      # same (designated) worker
        assert free["pid"] != approval["pid"]      # parallel call went elsewhere
        order = [fr.get("id") for fr in cc.seen
                 if fr.get("id") in (100, 101) and "method" not in fr]
        assert order == [100, 101]                 # info WAITED, no third spawn

    def test_multi_approval_repark_rekeys_reservation(self, facade, tmp_path):
        # #277 multi-approval: resume → parks AGAIN under a NEW token; the
        # writable-root reservation must follow the CURRENT token.
        f, cc = facade
        root = str(tmp_path)
        f.handle_cc_frame(_call_frame(110, {
            "sandbox": "workspace-write", "cwd": root,
            "_fake": {"park": {"token": "tok-A"}}}))
        pid = _payload(cc.wait_for_id(110))["pid"]
        f.handle_cc_frame(_call_frame(111, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": root, "_fake": {"result": {"tag": "W"}}}))   # queues on root
        f.handle_cc_frame(_call_frame(112, {"park_token": "tok-A",
                                            "_fake": {"park": {"token": "tok-B"}}},
                                      tool="codex_approve"))
        reparked = _payload(cc.wait_for_id(112))
        assert reparked["status"] == "awaiting_approval"
        assert reparked["park_token"] == "tok-B"
        assert reparked["resumed"] is True and reparked["pid"] == pid
        # Old token now dead; new token resumes on the same worker.
        f.handle_cc_frame(_call_frame(113, {"park_token": "tok-A"},
                                      tool="codex_approve"))
        assert "expired" in _payload(cc.wait_for_id(113))["error"]
        f.handle_cc_frame(_call_frame(114, {"park_token": "tok-B"},
                                      tool="codex_approve"))
        done = _payload(cc.wait_for_id(114))
        assert done["resumed"] is True and done["pid"] == pid
        writer = _payload(cc.wait_for_id(111, timeout=5))
        assert writer["tag"] == "W"    # released only after the FINAL resume


class TestFacadeCrashAndTeardown:
    def test_worker_crash_fails_only_its_call(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(120, {"approval_policy": "never",
                                            "_fake": {"sleep": 0.5, "result": {"tag": "OK"}}}))
        f.handle_cc_frame(_call_frame(121, {"approval_policy": "never",
                                            "_fake": {"die": True}}))
        dead = cc.wait_for_id(121, timeout=5)
        assert dead["result"]["isError"] is True
        assert "died" in _payload(dead)["error"]
        healthy = _payload(cc.wait_for_id(120, timeout=5))
        assert healthy["tag"] == "OK"          # crash contained
        f.handle_cc_frame(_call_frame(122, {"approval_policy": "never",
                                            "_fake": {"result": {"tag": "AFTER"}}}))
        assert _payload(cc.wait_for_id(122, timeout=5))["tag"] == "AFTER"

    def test_dead_worker_pending_elicitation_cancelled_and_tombstoned(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(130, {"approval_policy": "never",
                                            "_fake": {"elicit": {"who": "X"},
                                                      "die": True, "sleep": 0.05}}))
        req = cc.wait_for_request("elicitation/create")
        fid = req["id"]
        # Kill the worker while its elicitation is pending: reply never comes.
        f.handle_cc_frame({"jsonrpc": "2.0", "id": fid,
                           "result": {"action": "accept", "content": {}}})
        # The fake with die+elicit: after the reply it sleeps then dies before
        # replying — so 130 must come back as worker-death error, and the
        # facade must CANCEL any still-pending request on that worker's death.
        dead = cc.wait_for_id(130, timeout=5)
        assert dead["result"]["isError"] is True

    def test_dead_worker_uncollected_elicitation_gets_cancelled(self, facade):
        f, cc = facade
        # elicit with NO reply from CC; worker dies via facade-side kill.
        f.handle_cc_frame(_call_frame(135, {"approval_policy": "never",
                                            "_fake": {"elicit": {"who": "Y"},
                                                      "result": {"tag": "never"}}}))
        req = cc.wait_for_request("elicitation/create")
        with f._lock:
            worker = f._calls[135]["worker"]
        worker.proc.kill()
        dead = cc.wait_for_id(135, timeout=5)
        assert dead["result"]["isError"] is True
        cancel = cc.wait_for_request("notifications/cancelled", timeout=5)
        assert cancel["params"]["requestId"] == req["id"]
        # A LATE reply to the tombstoned id is swallowed without error.
        f.handle_cc_frame({"jsonrpc": "2.0", "id": req["id"],
                           "result": {"action": "accept", "content": {}}})

    def test_shutdown_closes_stdin_first_graceful(self, tmp_path):
        marker = str(tmp_path / "eof")
        cc = CCSide()
        env = dict(os.environ)
        env["FAKE_WORKER_EOF_MARKER"] = marker
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, env=env)
        f.handle_cc_frame(_call_frame(140, {"approval_policy": "never",
                                            "_fake": {"result": {"tag": "A"}}}))
        cc.wait_for_id(140)
        f.shutdown(timeout=5)
        with open(marker) as fh:
            assert fh.read() == "eof-clean"    # worker saw EOF, exited itself


class TestFacadeReap:
    def test_keep_one_warm_reap(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(150, {"approval_policy": "never",
                                            "_fake": {"sleep": 0.2, "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(151, {"approval_policy": "never",
                                            "_fake": {"sleep": 0.2, "result": {"tag": "B"}}}))
        cc.wait_for_id(150); cc.wait_for_id(151)
        assert f.worker_count() == 2
        f.reap_idle(idle_s=0)          # everything is "idle long enough"
        deadline = time.monotonic() + 3
        while f.worker_count() > 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert f.worker_count() == 1   # keep-one-warm: MRU survives
        f.handle_cc_frame(_call_frame(152, {"approval_policy": "never",
                                            "_fake": {"result": {"tag": "C"}}}))
        assert _payload(cc.wait_for_id(152, timeout=5))["tag"] == "C"

    def test_reap_skips_parked_and_busy(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(160, {"_fake": {"park": {"token": "tok-160"}}}))
        cc.wait_for_id(160)
        f.handle_cc_frame(_call_frame(161, {"approval_policy": "never",
                                            "_fake": {"sleep": 1.0, "result": {"tag": "B"}}}))
        time.sleep(0.1)
        f.reap_idle(idle_s=0)
        time.sleep(0.2)
        assert f.worker_count() == 2   # parked + busy both survive
        f.handle_cc_frame(_call_frame(162, {"park_token": "tok-160"},
                                      tool="codex_approve"))
        assert _payload(cc.wait_for_id(162, timeout=5))["resumed"] is True


class TestFacadeMainLoop:
    """Subprocess-level: the real main() over real stdio."""

    def _run(self, env_extra, frames, expect_ids, timeout=15):
        """Feed frames, hold stdin OPEN until every expected id answered
        (EOF = teardown per spec §3.1 — closing early would race replies),
        then close stdin and let the server exit."""
        import subprocess
        import threading as _threading
        env = dict(os.environ)
        env.update(env_extra)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(MCP_DIR, "codex_facade.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        lines = queue.Queue()

        def _reader():
            for raw in proc.stdout:
                lines.put(raw)
        _threading.Thread(target=_reader, daemon=True).start()
        out = []
        try:
            for fr in frames:
                proc.stdin.write((json.dumps(fr) + "\n").encode())
            proc.stdin.flush()
            waiting = set(expect_ids)
            deadline = time.monotonic() + timeout
            while waiting and time.monotonic() < deadline:
                try:
                    raw = lines.get(timeout=max(0.05, deadline - time.monotonic()))
                except queue.Empty:
                    break
                fr = json.loads(raw)
                out.append(fr)
                if "method" not in fr:
                    waiting.discard(fr.get("id"))
            assert not waiting, f"no reply for ids {waiting}; got {out}"
            proc.stdin.close()
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
        return out

    def test_kill_switch_execs_legacy_engine(self):
        frames = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "1", "capabilities": {},
                        "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ]
        out = self._run({"BULLDOZER_FACADE_OFF": "1"}, frames, {0, 1})
        init = next(fr for fr in out if fr.get("id") == 0)
        # Legacy single bridge: NO facade parallel line in instructions.
        assert "FACADE" not in (init["result"].get("instructions") or "")
        tools = next(fr for fr in out if fr.get("id") == 1)
        assert {t["name"] for t in tools["result"]["tools"]} >= {"codex_run"}

    def test_facade_main_serves_initialize_and_tools(self):
        frames = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "1", "capabilities": {},
                        "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ]
        out = self._run({"BULLDOZER_FACADE_TEST_WORKER": FAKE_WORKER}, frames, {0, 1})
        init = next(fr for fr in out if fr.get("id") == 0)
        assert "FACADE" in init["result"]["instructions"]
        tools = next(fr for fr in out if fr.get("id") == 1)
        assert {t["name"] for t in tools["result"]["tools"]} >= {
            "codex_run", "codex_review", "codex_info", "codex_approve"}

    def test_facade_main_full_roundtrip_through_fake_worker(self):
        frames = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "1", "capabilities": {},
                        "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _call_frame(5, {"approval_policy": "never",
                            "_fake": {"result": {"tag": "MAIN"}}}),
        ]
        out = self._run({"BULLDOZER_FACADE_TEST_WORKER": FAKE_WORKER}, frames, {0, 5})
        resp = next(fr for fr in out if fr.get("id") == 5)
        assert json.loads(resp["result"]["content"][0]["text"])["tag"] == "MAIN"


class TestEngineWorkerField:
    """The ONE engine change of the feature (§3.1/Env): _drift_warn appends
    worker=N when BULLDOZER_WORKER is set — additive, default-off."""

    def test_worker_field_appended_when_env_set(self, tmp_path, monkeypatch):
        import codex_server
        log = tmp_path / "log"
        monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(log))
        monkeypatch.setenv("BULLDOZER_WORKER", "7")
        codex_server._drift_warn(None, "TEST_MARK", "detail=x")
        assert log.read_text().strip().endswith("| worker=7")

    def test_no_worker_field_by_default(self, tmp_path, monkeypatch):
        import codex_server
        log = tmp_path / "log"
        monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(log))
        monkeypatch.delenv("BULLDOZER_WORKER", raising=False)
        codex_server._drift_warn(None, "TEST_MARK", "detail=x")
        line = log.read_text().strip()
        assert "worker=" not in line
        assert line.endswith("| TEST_MARK | detail=x")   # byte-identical legacy


# ---------------------------------------------------------------------------
# Round-1 code-review regressions (15 findings) — each test pins ONE fix
# ---------------------------------------------------------------------------

class TestReviewR1Scheduler:
    def test_root_slash_contains_every_path(self):
        # P2: '/' is every path's ancestor — the old rstrip made it '//'-prefix.
        assert codex_facade._roots_overlap("/", "/0/project") is True
        assert codex_facade._roots_overlap("/0/project", "/") is True

    def test_prefix_sibling_still_disjoint(self):
        assert codex_facade._roots_overlap("/w/a", "/w/ab") is False

    def test_cwd_only_resume_rerootss_thread(self, tmp_path):
        # P1: a resume may carry ONLY cwd (sandbox inherited) — the root must
        # follow the NEW cwd, else it can overlap another writer at that root.
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(); b.mkdir()
        tm = codex_facade.ThreadMap()
        first = _classify(args={"approval_policy": "never",
                                "sandbox": "workspace-write", "cwd": str(a)},
                          thread_map=tm)
        tm.bind("t", first)
        resumed = _classify(args={"thread_id": "t", "cwd": str(b)},
                            thread_map=tm)
        assert resumed.root == os.path.realpath(str(b))
        assert resumed.sandbox == "workspace-write"   # inherited independently

    def test_queued_call_reclassified_at_dequeue(self):
        # P1: B queues behind A on the SAME thread and is classified from the
        # thread's posture AT SUBMIT TIME. A then RE-ROOTS the thread (/a → /b).
        # Dequeued with the stale /a posture, B would be admitted alongside an
        # active writer at /b and race it — so B must be re-classified at
        # DEQUEUE, and then correctly queue behind C.
        s = codex_facade.Scheduler()
        s.submit("A", _posture(thread_id="th", root="/a"))
        s.submit("B", _posture(thread_id="th", root="/a"))   # queues: same thread
        s.submit("C", _posture(root="/b"))                   # active: disjoint
        fresh_b = _posture(thread_id="th", root="/b")        # A re-rooted the thread
        freed = s.release("A",
                          reclassify=lambda cid: fresh_b if cid == "B" else None)
        assert freed == []            # B now conflicts with C — correctly held
        assert not s.is_active("B")
        freed = s.release("C")
        assert freed == ["B"]
        # Control: WITHOUT reclassification the stale /a posture would dispatch
        # B straight into C's /b root.
        s2 = codex_facade.Scheduler()
        s2.submit("A", _posture(thread_id="th", root="/a"))
        s2.submit("B", _posture(thread_id="th", root="/a"))
        s2.submit("C", _posture(root="/b"))
        assert s2.release("A") == ["B"]   # the bug, pinned

    def test_designated_group_is_fifo_and_shares_one_worker(self):
        # r1 P1 + r2 P2: an approval turn and a codex_info both live on the
        # designated worker — they share ONE worker (never two slots) and drain
        # in FIFO order (an info admitted later must NOT jump a queued approval).
        placed_on = []

        def place(call_id):
            # ONE designated worker: it can serve a single call at a time.
            if placed_on:
                return False
            placed_on.append(call_id)
            return True

        s = codex_facade.Scheduler()
        appr_a = _posture(global_writer=True, approval_capable=True)
        appr_b = _posture(global_writer=True, approval_capable=True)
        info = _posture()
        assert s.submit("a1", appr_a, group="designated", place_fn=place) is True
        assert s.submit("a2", appr_b, group="designated", place_fn=place) is False
        assert s.submit("i1", info, group="designated", place_fn=place) is False
        placed_on.clear()
        # a2 was queued FIRST → it dequeues first, and i1 stays behind it.
        assert s.release("a1", place_fn=place) == ["a2"]
        placed_on.clear()
        assert s.release("a2", place_fn=place) == ["i1"]


class TestReviewR1Facade:
    def test_park_expiry_unpins_worker_and_root(self, tmp_path):
        # P1: the engine tears a park down on its cap WITHOUT an MCP frame —
        # the facade mirrors the cap and unpins, else the reservation leaks.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, park_cap_s=0.0)
        try:
            root = str(tmp_path)
            f.handle_cc_frame(_call_frame(200, {
                "sandbox": "workspace-write", "cwd": root,
                "_fake": {"park": {"token": "tok-200"}}}))
            cc.wait_for_id(200)
            f.handle_cc_frame(_call_frame(201, {
                "approval_policy": "never", "sandbox": "workspace-write",
                "cwd": root, "_fake": {"result": {"tag": "W"}}}))
            time.sleep(0.05)
            assert not any(fr.get("id") == 201 for fr in cc.seen)  # still held
            f.expire_parks(now=time.monotonic() + 1000)
            assert _payload(cc.wait_for_id(201, timeout=5))["tag"] == "W"
            # The expired token now answers exactly as the engine would.
            f.handle_cc_frame(_call_frame(202, {"park_token": "tok-200"},
                                          tool="codex_approve"))
            assert "expired" in _payload(cc.wait_for_id(202))["error"]
        finally:
            f.shutdown(timeout=3)

    def test_worker_death_during_approve_answers_cc(self, facade):
        # P1: _calls is keyed by the APPROVE id while worker.current is the
        # token — a death must fail the approve call, not leak it.
        f, cc = facade
        f.handle_cc_frame(_call_frame(210, {
            "_fake": {"park": {"token": "tok-210"}}}))
        cc.wait_for_id(210)
        with f._lock:
            worker = f._parks["tok-210"]["worker"]
        f.handle_cc_frame(_call_frame(211, {"park_token": "tok-210",
                                            "_fake": {"sleep": 5}},
                                      tool="codex_approve"))
        time.sleep(0.15)
        worker.proc.kill()
        dead = cc.wait_for_id(211, timeout=5)
        assert dead["result"]["isError"] is True
        assert "died" in _payload(dead)["error"]

    def test_designated_death_replaces_worker_for_queued_info(self, facade):
        # P1: a codex_info queued behind the busy designated worker must still
        # be answered if that worker dies — a fresh designated worker is
        # adopted/spawned and the queue drains onto it.
        f, cc = facade
        f.handle_cc_frame(_call_frame(220, {
            "_fake": {"sleep": 0.4, "result": {"tag": "APPROVAL"}}}))
        time.sleep(0.1)
        f.handle_cc_frame(_call_frame(221, {}, tool="codex_info"))
        with f._lock:
            assert f._sched.is_queued(221)   # waiting for the designated worker
            worker = f._calls[220]["worker"]
        worker.proc.kill()
        dead = cc.wait_for_id(220, timeout=5)
        assert dead["result"]["isError"] is True
        info = _payload(cc.wait_for_id(221, timeout=5))   # re-placed, answered
        assert info["ok"] is True

    def test_designated_reuses_idle_worker_at_cap(self):
        # P2: with max_workers=1 a codex_info must REUSE the idle worker, not
        # blind-spawn a second one.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=1)
        try:
            f.handle_cc_frame(_call_frame(230, {"approval_policy": "never",
                                                "_fake": {"result": {"tag": "A"}}}))
            cc.wait_for_id(230)
            assert f.worker_count() == 1
            f.handle_cc_frame(_call_frame(231, {}, tool="codex_info"))
            cc.wait_for_id(231, timeout=5)
            assert f.worker_count() == 1     # cap honored
        finally:
            f.shutdown(timeout=3)

    def test_temp_cwd_owner_not_reaped(self, facade):
        # P2: a thread started with omitted cwd lives in the worker's $TMPDIR —
        # reaping that worker would break an omitted-arg resume.
        f, cc = facade
        # Both run CONCURRENTLY (two workers); the temp-cwd owner finishes
        # FIRST, so it is the reap victim by LRU — and must be spared anyway.
        f.handle_cc_frame(_call_frame(240, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "_fake": {"sleep": 0.05, "result": {"thread_id": "temp-th"}}}))  # no cwd
        f.handle_cc_frame(_call_frame(241, {
            "approval_policy": "never",
            "_fake": {"sleep": 0.4, "result": {"tag": "B"}}}))
        cc.wait_for_id(240, timeout=5)
        cc.wait_for_id(241, timeout=5)
        assert f.worker_count() == 2
        f.reap_idle(idle_s=0)
        time.sleep(0.3)
        with f._lock:
            owners = [w for w in f._workers if w.alive and w.temp_threads]
        assert f.worker_count() == 2     # nothing reaped: MRU + temp-cwd owner
        assert len(owners) == 1          # the temp-cwd owner survived the reap
        assert "temp-th" in owners[0].temp_threads

    def test_malformed_frames_do_not_kill_the_facade(self, facade):
        f, cc = facade
        f.handle_cc_frame(["not", "an", "object"])                   # array
        f.handle_cc_frame({"jsonrpc": "2.0", "id": 250,
                           "method": "tools/call", "params": "junk"})  # str params
        f.handle_cc_frame({"jsonrpc": "2.0", "id": 251,
                           "method": "tools/call",
                           "params": {"name": "codex_run", "arguments": 5}})
        # The facade still serves real traffic afterwards.
        f.handle_cc_frame(_call_frame(252, {"approval_policy": "never",
                                            "_fake": {"result": {"tag": "OK"}}}))
        assert _payload(cc.wait_for_id(252, timeout=5))["tag"] == "OK"

    def test_cc_call_id_named_like_a_handshake_is_not_swallowed(self, facade):
        # P2: the old code dropped any worker reply whose id started with
        # "__init__" — a CC call id is a caller-controlled string.
        f, cc = facade
        f.handle_cc_frame(_call_frame("__init__0", {
            "approval_policy": "never", "_fake": {"result": {"tag": "ID"}}}))
        assert _payload(cc.wait_for_id("__init__0", timeout=5))["tag"] == "ID"

    def test_facade_audit_lines_written(self, tmp_path):
        cc = CCSide()
        log = tmp_path / "facade.log"
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(log))
        try:
            f.handle_cc_frame(_call_frame(260, {"approval_policy": "never",
                                                "_fake": {"result": {"tag": "A"}}}))
            cc.wait_for_id(260)
        finally:
            f.shutdown(timeout=3)
        text = log.read_text()
        assert "event=FACADE_DISPATCH" in text and "call=260" in text
        assert "event=FACADE_DONE" in text
        assert "worker=0" in text

    def test_overlap_proven_by_worker_intervals(self, facade):
        # P2: replaces the wall-clock assertion — the workers report their own
        # start/end, so the overlap is proven, not inferred from host timing.
        f, cc = facade
        f.handle_cc_frame(_call_frame(270, {"approval_policy": "never",
                                            "_fake": {"sleep": 0.5, "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(271, {"approval_policy": "never",
                                            "_fake": {"sleep": 0.5, "result": {"tag": "B"}}}))
        a = _payload(cc.wait_for_id(270, timeout=10))
        b = _payload(cc.wait_for_id(271, timeout=10))
        assert a["pid"] != b["pid"]
        assert min(a["t_end"], b["t_end"]) > max(a["t_start"], b["t_start"])


# ---------------------------------------------------------------------------
# Round-2 code-review regressions (13 findings) — each test pins ONE fix
# ---------------------------------------------------------------------------

class TestReviewR2:
    def test_case_insensitive_roots_conflict(self):
        # P1: the default macOS volume is case-insensitive — /Repo and /repo are
        # ONE directory that realpath still spells two ways.
        assert codex_facade._roots_overlap("/0/Repo", "/0/repo") is True
        assert codex_facade._roots_overlap("/0/REPO/sub", "/0/repo") is True

    def test_park_cap_mirrors_engine_clamp(self, monkeypatch):
        # P2: malformed → default; clamped to [1, 86400] exactly like the engine.
        import codex_server
        for raw in ("abc", "", None, "-5", "999999", "300"):
            if raw is None:
                monkeypatch.delenv("BULLDOZER_PARK_CAP_S", raising=False)
            else:
                monkeypatch.setenv("BULLDOZER_PARK_CAP_S", raw)
            assert codex_facade._park_cap_s() == codex_server._park_cap_s()

    def test_thread_map_updated_before_queue_drains(self, facade, tmp_path):
        # P1: B re-roots thread TH from /x to /y. C (omitted-arg resume of TH)
        # and D (a plain writer at /y) both queue behind B. When B finishes, the
        # ThreadMap must be updated BEFORE the queue drains — otherwise C is
        # re-classified with the STALE /x root, and C+D (both really writing /y)
        # run CONCURRENTLY and race the same tree.
        f, cc = facade
        x, y = tmp_path / "x", tmp_path / "y"
        x.mkdir(); y.mkdir()
        f.handle_cc_frame(_call_frame(300, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": str(x), "_fake": {"result": {"thread_id": "TH"}}}))
        cc.wait_for_id(300, timeout=5)
        assert f.thread_map().persisted("TH").root == os.path.realpath(str(x))

        f.handle_cc_frame(_call_frame(301, {            # B: re-root TH → /y
            "thread_id": "TH", "approval_policy": "never",
            "sandbox": "workspace-write", "cwd": str(y),
            "_fake": {"sleep": 0.3, "result": {"thread_id": "TH"}}}))
        f.handle_cc_frame(_call_frame(302, {            # C: omitted resume of TH
            "thread_id": "TH",
            "approval_policy": "never",
            "_fake": {"sleep": 0.3, "result": {"thread_id": "TH"}}}))
        f.handle_cc_frame(_call_frame(303, {            # D: plain writer at /y
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": str(y), "_fake": {"sleep": 0.3, "result": {"tag": "D"}}}))
        cc.wait_for_id(301, timeout=10)
        c = _payload(cc.wait_for_id(302, timeout=10))
        d = _payload(cc.wait_for_id(303, timeout=10))
        assert f.thread_map().persisted("TH").root == os.path.realpath(str(y))
        # C and D both target /y → they must NOT overlap in time.
        assert c["t_end"] <= d["t_start"] or d["t_end"] <= c["t_start"]

    def test_reclassified_posture_written_back_to_call_state(self, facade):
        # P1: reclassification must update the facade's own call entry (posture
        # + funnel group), not only the scheduler's private copy.
        f, cc = facade
        f.handle_cc_frame(_call_frame(310, {
            "_fake": {"sleep": 0.3, "result": {"thread_id": "th-310"}}}))  # capable
        f.handle_cc_frame(_call_frame(311, {
            "thread_id": "th-310", "approval_policy": "never",
            "_fake": {"result": {"tag": "R"}}}))
        cc.wait_for_id(310, timeout=5)
        cc.wait_for_id(311, timeout=5)
        # th-310 ran an approval-capable turn → sticky; the queued `never`
        # resume must have been widened at dequeue.
        assert f.thread_map().is_sticky("th-310") is True

    def test_cancel_in_admitted_but_unplaced_window(self):
        # P1: a call that the scheduler admitted but whose worker assignment has
        # not landed yet must still honor a cancel (the old code dropped it).
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2)
        try:
            with f._lock:
                # Simulate the window: an entry exists, no worker yet.
                f._calls[320] = {"posture": _posture(), "frame": _call_frame(320),
                                 "tool": "codex_run", "args": {}, "worker": None,
                                 "resume_of": None, "group": None}
                f._sched.submit(320, _posture())     # active, unplaced
            f.handle_cc_frame({"jsonrpc": "2.0",
                               "method": "notifications/cancelled",
                               "params": {"requestId": 320}})
            body = _payload(cc.wait_for_id(320, timeout=3))
            assert body["status"] == "interrupted"
            with f._lock:
                assert 320 not in f._calls
                assert f._sched.is_active(320) is False   # slot released
        finally:
            f.shutdown(timeout=3)

    def test_park_not_expired_while_approve_in_flight(self, facade, tmp_path):
        # P1: expiring a park whose codex_approve is RUNNING would free the
        # writable-root reservation under a turn that is still writing.
        f, cc = facade
        root = str(tmp_path)
        f.handle_cc_frame(_call_frame(340, {
            "sandbox": "workspace-write", "cwd": root,
            "_fake": {"park": {"token": "tok-340"}}}))
        cc.wait_for_id(340)
        f.handle_cc_frame(_call_frame(341, {"park_token": "tok-340",
                                            "_fake": {"sleep": 0.5}},
                                      tool="codex_approve"))
        time.sleep(0.1)                       # the resumed turn is in flight
        f.expire_parks(now=time.monotonic() + 100000)
        with f._lock:
            assert "tok-340" in f._parks      # NOT expired under a live approve
        assert _payload(cc.wait_for_id(341, timeout=5))["resumed"] is True

    def test_park_liveness_probe_unpins_ended_park(self, facade, tmp_path):
        # P1: the engine also ends a park with NO MCP frame (inner-child death /
        # its own cap). The probe asks the worker: a PARKED engine busy-blocks
        # with a distinct "parked" error; anything else means the park is gone.
        f, cc = facade
        root = str(tmp_path)
        f.handle_cc_frame(_call_frame(350, {
            "sandbox": "workspace-write", "cwd": root,
            "_fake": {"park": {"token": "tok-350"}}}))
        cc.wait_for_id(350)
        f.handle_cc_frame(_call_frame(351, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": root, "_fake": {"result": {"tag": "W"}}}))   # queued on root
        time.sleep(0.05)
        assert not any(fr.get("id") == 351 for fr in cc.seen)
        with f._lock:
            worker = f._parks["tok-350"]["worker"]
        # While the worker IS parked the probe must NOT unpin (it busy-blocks).
        # AWAIT the probe reply (review r5 P2: a sleep could false-green on a
        # loaded host — the park would still look present merely because the
        # reply had not arrived yet).
        f.probe_parks(now=time.monotonic() + 100000)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with f._lock:
                if not f._probes:
                    break        # the probe was answered
            time.sleep(0.01)
        else:
            raise AssertionError("the probe was never answered")
        with f._lock:
            assert "tok-350" in f._parks     # answered "still parked" → no unpin
        assert not any(fr.get("id") == 351 for fr in cc.seen)
        # Now the engine ends the park silently (inner-child death simulation).
        worker.send({"jsonrpc": "2.0", "method": "__fake_drop_park__"})
        time.sleep(0.05)
        f.probe_parks(now=time.monotonic() + 200000)
        assert _payload(cc.wait_for_id(351, timeout=5))["tag"] == "W"
        with f._lock:
            assert "tok-350" not in f._parks

    def test_probe_reply_never_leaks_to_cc(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(360, {
            "_fake": {"park": {"token": "tok-360"}}}))
        cc.wait_for_id(360)
        f.probe_parks(now=time.monotonic() + 100000)
        time.sleep(0.3)
        probe_replies = [fr for fr in cc.seen
                         if str(fr.get("id", "")).startswith("__facade_probe__")]
        assert probe_replies == []      # the probe is internal, never sent to CC

    def test_approve_dispatch_is_audited(self, tmp_path):
        # P2: codex_approve bypasses _place — it must still emit FACADE_DISPATCH.
        cc = CCSide()
        log = tmp_path / "facade.log"
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(log))
        try:
            f.handle_cc_frame(_call_frame(370, {
                "_fake": {"park": {"token": "tok-370"}}}))
            cc.wait_for_id(370)
            f.handle_cc_frame(_call_frame(371, {"park_token": "tok-370"},
                                          tool="codex_approve"))
            cc.wait_for_id(371, timeout=5)
        finally:
            f.shutdown(timeout=3)
        text = log.read_text()
        assert "event=FACADE_DISPATCH" in text and "call=371" in text
        assert "tool=codex_approve" in text

    def test_spawn_failure_frees_the_slot(self):
        # P2/r3-P1: a REAL fork/exec failure (Popen raises FileNotFoundError) —
        # the old test used python3 + a bad script, where Popen SUCCEEDS.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=["/nonexistent/codex-worker-binary"],
                                max_workers=2)
        try:
            f.handle_cc_frame(_call_frame(380, {"approval_policy": "never"}))
            dead = cc.wait_for_id(380, timeout=5)
            assert dead["result"].get("isError") is True
            assert "could not be started" in _payload(dead)["error"]
            with f._lock:
                assert 380 not in f._calls
                assert f._sched.is_active(380) is False
                assert f._sched.is_queued(380) is False
        finally:
            f.shutdown(timeout=3)

    def test_spawn_failure_during_drain_answers_every_call(self):
        # r3 P1: a spawn failure raised INSIDE the scheduler's drain loop used to
        # escape through _on_worker_exit, aborting the drain with the queue
        # half-committed — leaving queued calls unanswered forever.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=1)
        try:
            f.handle_cc_frame(_call_frame(384, {
                "approval_policy": "never",
                "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
            f.handle_cc_frame(_call_frame(385, {   # queued behind it (cap 1)
                "approval_policy": "never", "_fake": {"result": {"tag": "B"}}}))
            with f._lock:
                worker = f._calls[384]["worker"]
                f._argv = ["/nonexistent/codex-worker-binary"]   # next spawn dies
            worker.proc.kill()          # → _on_worker_exit drains the queue
            dead = cc.wait_for_id(384, timeout=5)
            assert dead["result"]["isError"] is True
            # 385 must ALSO be answered — not stranded by the failed respawn.
            other = cc.wait_for_id(385, timeout=5)
            assert other["result"].get("isError") is True
        finally:
            f.shutdown(timeout=3)

    def test_designated_fifo_info_does_not_jump_queued_approval(self, facade):
        # P2: with an approval active, a queued approval must run BEFORE a
        # codex_info admitted later (FIFO inside the designated worker).
        f, cc = facade
        f.handle_cc_frame(_call_frame(390, {
            "_fake": {"sleep": 0.3, "result": {"tag": "A1"}}}))     # capable
        time.sleep(0.05)
        f.handle_cc_frame(_call_frame(391, {
            "_fake": {"result": {"tag": "A2"}}}))                    # capable → queued
        f.handle_cc_frame(_call_frame(392, {}, tool="codex_info"))   # queued behind
        for mid in (390, 391, 392):
            cc.wait_for_id(mid, timeout=10)
        order = [fr.get("id") for fr in cc.seen
                 if fr.get("id") in (390, 391, 392) and "method" not in fr]
        assert order == [390, 391, 392]     # FIFO — info did not jump A2

    def test_cold_resume_guess_is_not_persisted_as_truth(self, facade):
        # r2 self-found: a resume whose thread is not yet BOUND is scheduled
        # conservatively (approval-capable + global writer). That guess must NOT
        # become the thread's persisted posture once the thread is known —
        # otherwise an explicit `never` thread is widened (sticky) FOREVER on
        # nothing but the facade's own ignorance.
        f, cc = facade
        f.handle_cc_frame(_call_frame(400, {                 # binds TH-X fast
            "approval_policy": "never",
            "_fake": {"result": {"thread_id": "TH-X"}}}))
        f.handle_cc_frame(_call_frame(401, {                 # cold resume: assumed
            "thread_id": "TH-X", "approval_policy": "never",
            "_fake": {"sleep": 0.3, "result": {"thread_id": "TH-X"}}}))
        cc.wait_for_id(400, timeout=5)
        cc.wait_for_id(401, timeout=5)
        assert f.thread_map().known("TH-X")
        # Both turns were explicitly `never` → the thread must stay in the
        # parallel class; only the SCHEDULING of 401 was conservative.
        assert f.thread_map().is_sticky("TH-X") is False
        assert f.thread_map().persisted("TH-X").approval_capable is False

    def test_info_storm_cannot_starve_a_queued_approval(self, facade, tmp_path):
        # r2 P2 (FIFO in the funnel): an approval blocked by a ROOT conflict
        # leaves the designated worker FREE. Without group-FIFO, every later
        # codex_info would jump it — an info storm starves the approval.
        f, cc = facade
        root = str(tmp_path)
        f.handle_cc_frame(_call_frame(410, {                 # plain writer at root
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": root, "_fake": {"sleep": 0.4, "result": {"tag": "W"}}}))
        time.sleep(0.05)
        f.handle_cc_frame(_call_frame(411, {                 # approval → global writer
            "_fake": {"result": {"tag": "A"}}}))             # queued behind 410
        f.handle_cc_frame(_call_frame(412, {}, tool="codex_info"))   # must NOT jump
        for mid in (410, 411, 412):
            cc.wait_for_id(mid, timeout=10)
        order = [fr.get("id") for fr in cc.seen
                 if fr.get("id") in (411, 412) and "method" not in fr]
        assert order == [411, 412]     # the approval ran first — no starvation


class TestReviewR3:
    def test_temp_cwd_turn_is_a_writer(self):
        # P1: a workspace-write turn with NO cwd writes into the worker's own
        # $TMPDIR — root is None, but a global writer (danger / approval-capable)
        # can still reach it, so it must count as a writer.
        temp = codex_facade.Posture(
            tool="codex_run", root=None, global_writer=False,
            approval_capable=False, thread_id=None,
            sandbox="workspace-write", temp_cwd=True)
        glob = _posture(global_writer=True)
        assert codex_facade.conflicts(temp, glob) is True
        assert codex_facade.conflicts(glob, temp) is True
        # …but two temp-cwd turns are still parallel (separate private tmpdirs).
        assert codex_facade.conflicts(temp, temp) is False

    def test_retryable_approve_error_preserves_the_park(self, facade):
        # P1: codex_approve validates decision_id BEFORE gen.send — a bad id is
        # a RETRYABLE error with the engine's park INTACT. Tearing our park down
        # would make the live park unreachable ("expired") on the retry.
        f, cc = facade
        f.handle_cc_frame(_call_frame(420, {
            "_fake": {"park": {"token": "tok-420"}}}))
        cc.wait_for_id(420)
        f.handle_cc_frame(_call_frame(421, {
            "park_token": "tok-420", "decision_id": "hallucinated",
            "_fake": {"result": {"error": "unknown decision_id: 'hallucinated'"}}},
            tool="codex_approve"))
        cc.wait_for_id(421, timeout=5)
        with f._lock:
            assert "tok-420" in f._parks          # park PRESERVED
            assert f._parks["tok-420"]["worker"].park_token == "tok-420"
        # The retry with a valid id resumes the SAME park.
        f.handle_cc_frame(_call_frame(422, {"park_token": "tok-420"},
                                      tool="codex_approve"))
        assert _payload(cc.wait_for_id(422, timeout=5))["resumed"] is True

    def test_expired_approve_error_drops_the_stale_park(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(425, {
            "_fake": {"park": {"token": "tok-425"}}}))
        cc.wait_for_id(425)
        f.handle_cc_frame(_call_frame(426, {
            "park_token": "tok-425",
            "_fake": {"result": {"error": "parked turn expired"}}},
            tool="codex_approve"))
        cc.wait_for_id(426, timeout=5)
        with f._lock:
            assert "tok-425" not in f._parks      # our record was the stale one

    def test_cancel_of_parked_call_keeps_pin_until_worker_confirms(self, facade,
                                                                   tmp_path):
        # P1: the engine clears its park ASYNCHRONOUSLY — unpinning at cancel
        # time would place a queued call on a still-parked worker (which would
        # busy-block it). The pin is released only once the worker confirms.
        f, cc = facade
        root = str(tmp_path)
        f.handle_cc_frame(_call_frame(430, {
            "sandbox": "workspace-write", "cwd": root,
            "_fake": {"park": {"token": "tok-430"}}}))
        cc.wait_for_id(430)
        f.handle_cc_frame(_call_frame(431, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": root, "_fake": {"result": {"tag": "W"}}}))   # queues on root
        f.handle_cc_frame({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": 430}})
        with f._lock:
            assert "tok-430" in f._parks         # still pinned — not yet confirmed
            assert f._parks["tok-430"]["next_probe"] == 0.0   # probe armed
        assert not any(fr.get("id") == 431 for fr in cc.seen)
        f.probe_parks()                          # the worker confirms: not parked
        assert _payload(cc.wait_for_id(431, timeout=5))["tag"] == "W"
        with f._lock:
            assert "tok-430" not in f._parks

    def test_repark_rebinds_cancellation_origin(self, facade):
        # P1: the engine binds its park's cancellation id to the CURRENT call —
        # after a multi-approval re-park that is the codex_approve call, not the
        # original one.
        f, cc = facade
        f.handle_cc_frame(_call_frame(440, {
            "_fake": {"park": {"token": "tok-A"}}}))
        cc.wait_for_id(440)
        f.handle_cc_frame(_call_frame(441, {
            "park_token": "tok-A", "_fake": {"park": {"token": "tok-B"}}},
            tool="codex_approve"))
        cc.wait_for_id(441, timeout=5)
        with f._lock:
            assert f._parked_origin.get(441) == "tok-B"   # rebound to the approve
            assert 440 not in f._parked_origin            # the old id is gone

    def test_second_concurrent_approve_is_rejected(self, facade):
        # P2: two approvals for one token would overwrite worker.call and strand
        # the first call forever.
        f, cc = facade
        f.handle_cc_frame(_call_frame(450, {
            "_fake": {"park": {"token": "tok-450"}}}))
        cc.wait_for_id(450)
        f.handle_cc_frame(_call_frame(451, {"park_token": "tok-450",
                                            "_fake": {"sleep": 2.0}},
                                      tool="codex_approve"))
        # Wait for the STATE the facade sets synchronously — not a fixed delay
        # (review r4 P2: a loaded host could finish the approve first).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with f._lock:
                w = f._parks.get("tok-450", {}).get("worker")
                if w is not None and w.call == 451:
                    break
            time.sleep(0.01)
        else:
            raise AssertionError("approve 451 never became in-flight")
        f.handle_cc_frame(_call_frame(452, {"park_token": "tok-450"},
                                      tool="codex_approve"))
        rejected = cc.wait_for_id(452, timeout=3)
        assert rejected["result"]["isError"] is True
        assert "already in flight" in _payload(rejected)["error"]
        assert _payload(cc.wait_for_id(451, timeout=10))["resumed"] is True

    def test_unresolved_cold_resume_persists_nothing(self, facade):
        # P2: a lone cross-session resume never learns the thread's real posture
        # — persisting the conservative GUESS would widen it (sticky) forever.
        f, cc = facade
        f.handle_cc_frame(_call_frame(460, {
            "thread_id": "TH-COLD", "approval_policy": "never",
            "_fake": {"result": {"thread_id": "TH-COLD"}}}))
        cc.wait_for_id(460, timeout=5)
        assert f.thread_map().known("TH-COLD") is False   # nothing persisted
        assert f.thread_map().is_sticky("TH-COLD") is False


class TestReviewR4:
    def test_spawn_failure_settles_and_places_the_follower(self):
        # P1: settling an unplaceable call must re-drain WITH placement — a
        # follower blocked BEHIND it (by conflict, not by the spawn failure)
        # must get a real worker, not be moved to active with worker=None and
        # left unanswered forever.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=1)
        real_spawn = f._spawn
        state = {"fail_next": False}

        def flaky_spawn():
            if state["fail_next"]:
                state["fail_next"] = False     # fails EXACTLY once
                raise OSError("simulated fork/exec failure")
            return real_spawn()
        f._spawn = flaky_spawn
        try:
            root = "/tmp/facade-follower-root"
            f.handle_cc_frame(_call_frame(500, {
                "approval_policy": "never", "sandbox": "workspace-write",
                "cwd": root, "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
            f.handle_cc_frame(_call_frame(501, {        # queued: cap 1
                "approval_policy": "never", "sandbox": "workspace-write",
                "cwd": root, "_fake": {"result": {"tag": "B"}}}))
            f.handle_cc_frame(_call_frame(502, {        # queued behind 501 (root)
                "approval_policy": "never", "sandbox": "workspace-write",
                "cwd": root, "_fake": {"result": {"tag": "C"}}}))
            with f._lock:
                worker = f._calls[500]["worker"]
                state["fail_next"] = True    # the respawn for 501 will fail
            worker.proc.kill()
            assert cc.wait_for_id(500, timeout=5)["result"]["isError"] is True
            assert cc.wait_for_id(501, timeout=5)["result"]["isError"] is True
            # 502 was blocked by 501 — after 501 is settled it must be PLACED
            # (the spawn works again) and complete normally.
            assert _payload(cc.wait_for_id(502, timeout=5))["tag"] == "C"
            with f._lock:
                assert f._unplaceable == []
        finally:
            f.shutdown(timeout=3)

    def test_settle_never_answers_a_cancelled_call_twice(self):
        # P2: a cancel can win the race after _place marked the call unplaceable
        # but before _settle runs — the call must be answered exactly ONCE.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=["/nonexistent/codex-worker-binary"],
                                max_workers=2)
        try:
            with f._lock:
                f._calls[510] = {"posture": _posture(), "frame": _call_frame(510),
                                 "tool": "codex_run", "args": {}, "worker": None,
                                 "resume_of": None, "group": None}
                f._unplaceable.append(510)      # _place already failed
            f.handle_cc_frame({"jsonrpc": "2.0",
                               "method": "notifications/cancelled",
                               "params": {"requestId": 510}})
            time.sleep(0.2)
            f._settle()
            replies = [fr for fr in cc.seen
                       if fr.get("id") == 510 and "method" not in fr]
            assert len(replies) == 1        # exactly one terminal reply
        finally:
            f.shutdown(timeout=3)

    def test_failed_spawn_leaves_no_tempdir(self, monkeypatch, tmp_path):
        # P2: Popen raising AFTER mkdtemp leaked a private tmpdir per attempt.
        # (tempfile CACHES gettempdir(), so the env var alone would not steer
        # mkdtemp here — patch the module attribute.)
        import tempfile as _tf
        monkeypatch.setattr(_tf, "tempdir", str(tmp_path))
        with pytest.raises(OSError):
            codex_facade.Worker(0, ["/nonexistent/codex-worker-binary"], None,
                                lambda *_: None, lambda *_: None)
        leaked = [d for d in os.listdir(tmp_path)
                  if d.startswith("bulldozer-facade-")]
        assert leaked == []

    def test_parked_cancel_is_sent_before_the_probe_is_armed(self, facade):
        # P2: arming next_probe=0 BEFORE forwarding the cancel let housekeeping
        # probe in the gap — the worker answers "still parked", and the facade
        # then waits a whole interval before releasing the reservation.
        # Determinism: the cancel send happens UNDER the facade lock, so a
        # concurrent probe_parks() must block behind it. We hold the send open
        # and race a probe against it.
        f, cc = facade
        f.handle_cc_frame(_call_frame(520, {
            "_fake": {"park": {"token": "tok-520"}}}))
        cc.wait_for_id(520)
        with f._lock:
            worker = f._parks["tok-520"]["worker"]
        gate = threading.Event()
        real_send = worker.send

        def slow_send(frame):
            if frame.get("method") == "notifications/cancelled":
                gate.set()
                time.sleep(0.3)      # hold the cancel in flight
            return real_send(frame)
        worker.send = slow_send

        t = threading.Thread(target=f.handle_cc_frame, args=(
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": {"requestId": 520}},))
        t.start()
        assert gate.wait(3)
        # Correct code: this blocks on the facade lock until the cancel is sent.
        # Mutated code: it slips in first and the worker answers "still parked".
        f.probe_parks(now=time.monotonic() + 100000)
        t.join(5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with f._lock:
                if "tok-520" not in f._parks:
                    break
            time.sleep(0.02)
        with f._lock:
            assert "tok-520" not in f._parks     # unpinned on the FIRST probe
            assert all(not w.busy() for w in f._workers if w.alive)


class TestReviewR5:
    def test_stale_spawn_marker_does_not_kill_a_placed_call(self):
        # P1: _place failed the spawn and marked the call unplaceable; a
        # concurrent drain then PLACED it. _settle must notice and leave it
        # alone — answering it would drop the real reply and wedge the worker.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2)
        try:
            f.handle_cc_frame(_call_frame(600, {
                "approval_policy": "never", "_fake": {"result": {"tag": "A"}}}))
            cc.wait_for_id(600, timeout=5)
            with f._lock:
                worker = f._workers[0]
                # Simulate the race: the call IS placed and running, but a stale
                # spawn-failure marker for it is still queued for settlement.
                f._calls[601] = {"posture": _posture(), "frame": _call_frame(601),
                                 "tool": "codex_run", "args": {}, "worker": worker,
                                 "resume_of": None, "group": None}
                worker.call = 601
                f._unplaceable.append(601)
            f._settle()
            with f._lock:
                assert 601 in f._calls          # NOT killed by the stale marker
                assert worker.call == 601       # worker not wedged
            replies = [fr for fr in cc.seen if fr.get("id") == 601]
            assert replies == []                # and CC was not answered early
        finally:
            f.shutdown(timeout=3)

    def test_temp_cwd_resume_pinned_to_owning_worker(self, facade):
        # P1: a thread created with omitted cwd lives in ITS worker's private
        # $TMPDIR. An omitted-cwd resume must run on that SAME worker — running
        # it elsewhere lets a fresh temp-cwd turn on the owner write the very
        # tree the resume is using.
        f, cc = facade
        f.handle_cc_frame(_call_frame(610, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "_fake": {"result": {"thread_id": "TMP-TH"}}}))          # no cwd
        first = _payload(cc.wait_for_id(610, timeout=5))
        with f._lock:
            owner = f._temp_owner("TMP-TH")
            assert owner is not None and owner.proc.pid == first["pid"]
        # Occupy the owner, then resume the thread: the resume must WAIT for it,
        # never land on the other worker.
        f.handle_cc_frame(_call_frame(611, {
            "approval_policy": "never",
            "_fake": {"sleep": 0.3, "result": {"tag": "OTHER"}}}))
        time.sleep(0.05)
        f.handle_cc_frame(_call_frame(612, {
            "thread_id": "TMP-TH", "approval_policy": "never",
            "_fake": {"result": {"tag": "RESUME"}}}))
        resumed = _payload(cc.wait_for_id(612, timeout=10))
        assert resumed["pid"] == first["pid"]     # same worker → same $TMPDIR

    def test_dead_workers_leave_the_pool(self, facade):
        f, cc = facade
        f.handle_cc_frame(_call_frame(620, {
            "approval_policy": "never", "_fake": {"result": {"tag": "A"}}}))
        cc.wait_for_id(620, timeout=5)
        with f._lock:
            worker = f._workers[0]
        worker.proc.kill()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with f._lock:
                if worker not in f._workers:
                    break
            time.sleep(0.02)
        with f._lock:
            assert worker not in f._workers      # pool does not grow forever
            assert worker.proc.stdout.closed     # its fds are released
        f.handle_cc_frame(_call_frame(621, {
            "approval_policy": "never", "_fake": {"result": {"tag": "B"}}}))
        assert _payload(cc.wait_for_id(621, timeout=5))["tag"] == "B"

    def test_reader_thread_start_failure_cleans_up(self, monkeypatch, tmp_path):
        # P2: Popen succeeded but Thread.start() raised → the subprocess and the
        # private tmpdir were never registered for cleanup.
        import tempfile as _tf
        monkeypatch.setattr(_tf, "tempdir", str(tmp_path))

        def boom(self):
            raise RuntimeError("can't start new thread")
        monkeypatch.setattr(threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            codex_facade.Worker(0, [sys.executable, FAKE_WORKER], None,
                                lambda *_: None, lambda *_: None)
        leaked = [d for d in os.listdir(tmp_path)
                  if d.startswith("bulldozer-facade-")]
        assert leaked == []


class TestReviewR6:
    def test_explicit_cwd_resume_is_not_pinned_to_the_old_owner(self, facade,
                                                                tmp_path):
        # P1: a resume with an explicit cwd has moved OUT of the owner's private
        # $TMPDIR — pinning it to a busy owner would queue it for nothing (and
        # forever, if that owner wedges).
        f, cc = facade
        f.handle_cc_frame(_call_frame(700, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "_fake": {"result": {"thread_id": "MOV-TH"}}}))          # no cwd
        first = _payload(cc.wait_for_id(700, timeout=5))
        with f._lock:
            assert f._temp_owner("MOV-TH") is not None
        # Occupy the owner…
        f.handle_cc_frame(_call_frame(701, {
            "approval_policy": "never",
            "_fake": {"sleep": 0.6, "result": {"tag": "BUSY"}}}))
        time.sleep(0.05)
        # …and resume the thread WITH an explicit cwd: it must run NOW, on the
        # other worker — not wait for the owner.
        t0 = time.monotonic()
        f.handle_cc_frame(_call_frame(702, {
            "thread_id": "MOV-TH", "approval_policy": "never",
            "sandbox": "workspace-write", "cwd": str(tmp_path),
            "_fake": {"result": {"thread_id": "MOV-TH", "tag": "MOVED"}}}))
        moved = _payload(cc.wait_for_id(702, timeout=5))
        assert time.monotonic() - t0 < 0.5      # did NOT wait for the owner
        assert moved["pid"] != first["pid"]
        with f._lock:
            assert f._temp_owner("MOV-TH") is None   # ownership dropped

    def test_pinned_resume_takes_over_the_designated_role(self, facade):
        # P1: a temp-cwd thread upgraded to approval-capable is pinned to its
        # owner — the funnel's home must MOVE to that worker, or codex_info would
        # be answered by a DIFFERENT worker's live connection state.
        f, cc = facade
        # w0 creates the temp-cwd thread and stays BUSY, so the codex_info below
        # is forced to spawn w1 and make IT the designated worker.
        f.handle_cc_frame(_call_frame(710, {                 # temp-cwd, `never`
            "approval_policy": "never", "sandbox": "workspace-write",
            "_fake": {"sleep": 0.4, "result": {"thread_id": "UPG-TH"}}}))
        time.sleep(0.05)
        f.handle_cc_frame(_call_frame(711, {}, tool="codex_info"))
        info1 = _payload(cc.wait_for_id(711, timeout=5))
        owner_pid = _payload(cc.wait_for_id(710, timeout=5))["pid"]
        assert info1["pid"] != owner_pid        # the funnel home is the OTHER worker
        with f._lock:
            assert f._designated.proc.pid == info1["pid"]
            assert f._temp_owner("UPG-TH").proc.pid == owner_pid
        f.handle_cc_frame(_call_frame(712, {                 # upgrade → capable
            "thread_id": "UPG-TH", "approval_policy": "on-request",
            "_fake": {"result": {"thread_id": "UPG-TH", "tag": "UP"}}}))
        up = _payload(cc.wait_for_id(712, timeout=5))
        assert up["pid"] == owner_pid           # pinned to its temp-cwd owner
        with f._lock:
            assert f._designated.proc.pid == owner_pid   # funnel home MOVED
        f.handle_cc_frame(_call_frame(713, {}, tool="codex_info"))
        info2 = _payload(cc.wait_for_id(713, timeout=5))
        assert info2["pid"] == owner_pid        # info follows the funnel home

    def test_reader_start_failure_reaps_the_child(self, monkeypatch, tmp_path):
        # P2: kill() alone leaves a zombie — the child must be waited on.
        import tempfile as _tf
        monkeypatch.setattr(_tf, "tempdir", str(tmp_path))
        seen = {}
        real_popen = subprocess.Popen

        def spy_popen(*a, **kw):
            p = real_popen(*a, **kw)
            seen["proc"] = p
            return p
        monkeypatch.setattr(subprocess, "Popen", spy_popen)
        monkeypatch.setattr(threading.Thread, "start",
                            lambda self: (_ for _ in ()).throw(
                                RuntimeError("can't start new thread")))
        with pytest.raises(RuntimeError):
            codex_facade.Worker(0, [sys.executable, FAKE_WORKER], None,
                                lambda *_: None, lambda *_: None)
        # NB: poll() would REAP it itself — assert the Worker already did.
        assert seen["proc"].returncode is not None   # waited on, not a zombie

    def test_worker_ids_are_monotonic_across_respawn(self, facade):
        # P2: removing a dead worker made len(_workers) reuse its index →
        # BULLDOZER_WORKER=N and the audit lines became ambiguous.
        f, cc = facade
        f.handle_cc_frame(_call_frame(720, {
            "approval_policy": "never",
            "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
        f.handle_cc_frame(_call_frame(721, {
            "approval_policy": "never",
            "_fake": {"sleep": 1.5, "result": {"tag": "B"}}}))   # stays BUSY
        time.sleep(0.1)
        with f._lock:
            w0, w1 = f._workers[0], f._workers[1]
            assert {w0.index, w1.index} == {0, 1}
        w0.proc.kill()                       # worker 0 dies and leaves the pool
        cc.wait_for_id(720, timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with f._lock:
                if w0 not in f._workers:
                    break
            time.sleep(0.02)
        # w1 is STILL busy → this call must SPAWN a replacement worker.
        f.handle_cc_frame(_call_frame(722, {
            "approval_policy": "never", "_fake": {"result": {"tag": "C"}}}))
        cc.wait_for_id(722, timeout=5)
        with f._lock:
            live = [w.index for w in f._workers if w.alive]
            assert len(live) == len(set(live))   # no duplicate ids after respawn
            assert w1.index in live
        cc.wait_for_id(721, timeout=5)


class TestReviewR7:
    def test_reader_start_failure_during_drain_answers_every_call(self):
        # P1: Worker.__init__ re-raises RuntimeError (reader thread could not
        # start). _place caught only OSError → the exception escaped the drain
        # inside _on_worker_exit and the DEAD call was never answered.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=1)
        real_spawn = f._spawn
        state = {"fail_next": False}

        def flaky_spawn():
            if state["fail_next"]:
                state["fail_next"] = False
                raise RuntimeError("can't start new thread")
            return real_spawn()
        f._spawn = flaky_spawn
        try:
            f.handle_cc_frame(_call_frame(800, {
                "approval_policy": "never",
                "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
            f.handle_cc_frame(_call_frame(801, {        # queued (cap 1)
                "approval_policy": "never", "_fake": {"result": {"tag": "B"}}}))
            with f._lock:
                worker = f._calls[800]["worker"]
                state["fail_next"] = True
            worker.proc.kill()
            # BOTH must be answered — the dead call and the queued one.
            assert cc.wait_for_id(800, timeout=5)["result"]["isError"] is True
            assert cc.wait_for_id(801, timeout=5) is not None
        finally:
            f.shutdown(timeout=3)

    def test_erroring_resume_makes_the_thread_ambiguous(self, facade, tmp_path):
        # r7 P1 → r8 P1: an ERRORING resume with an explicit cwd is AMBIGUOUS —
        # the engine may have applied the new cwd (mid-turn failure) or rejected
        # the call before turn setup. Persisting EITHER root can race a writer,
        # so the thread is FORGOTTEN: later resumes schedule conservatively.
        f, cc = facade
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(); b.mkdir()
        f.handle_cc_frame(_call_frame(810, {
            "approval_policy": "never", "sandbox": "workspace-write",
            "cwd": str(a), "_fake": {"result": {"thread_id": "ERR-TH"}}}))
        cc.wait_for_id(810, timeout=5)
        assert f.thread_map().persisted("ERR-TH").root == os.path.realpath(str(a))
        f.handle_cc_frame(_call_frame(811, {
            "thread_id": "ERR-TH", "approval_policy": "never",
            "sandbox": "workspace-write", "cwd": str(b),
            "_fake": {"result": {"error": "app-server died"}}}))
        cc.wait_for_id(811, timeout=5)
        assert f.thread_map().known("ERR-TH") is False   # neither root persisted
        # A later omitted-arg resume is therefore scheduled CONSERVATIVELY.
        posture = codex_facade.classify_call(
            "codex_run", {"thread_id": "ERR-TH", "approval_policy": "never"},
            f.thread_map())
        assert posture.global_writer is True and posture.approval_capable is True

    def test_nonpositive_worker_cap_still_serves_calls(self, monkeypatch):
        # P2: max_workers=0 could never place anything → every call hung forever.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=0)
        try:
            f.handle_cc_frame(_call_frame(820, {
                "approval_policy": "never", "_fake": {"result": {"tag": "OK"}}}))
            assert _payload(cc.wait_for_id(820, timeout=5))["tag"] == "OK"
        finally:
            f.shutdown(timeout=3)

    def test_worker_emitting_a_non_object_frame_does_not_wedge(self, facade):
        # P2: a syntactically valid NON-OBJECT JSON line from a worker raised in
        # the reader thread → the reader died, the call was never answered and
        # the slot leaked. The worker really emits `[]` here.
        f, cc = facade
        f.handle_cc_frame(_call_frame(830, {
            "approval_policy": "never",
            "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
        time.sleep(0.05)
        with f._lock:
            worker = f._calls[830]["worker"]
        worker.send({"jsonrpc": "2.0", "method": "__fake_emit_raw__",
                     "params": {"value": []}})
        # The call still completes normally on that same worker.
        assert _payload(cc.wait_for_id(830, timeout=5))["tag"] == "A"
        f.handle_cc_frame(_call_frame(831, {
            "approval_policy": "never", "_fake": {"result": {"tag": "B"}}}))
        assert _payload(cc.wait_for_id(831, timeout=5))["tag"] == "B"

    def test_audit_log_is_written_off_the_lock(self, tmp_path):
        # P2: _log ran bulldozer_log's blocking file I/O UNDER the facade lock —
        # a wedged log file would freeze every worker reader. It is queued now.
        cc = CCSide()
        log = tmp_path / "facade.log"
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(log))
        try:
            f.handle_cc_frame(_call_frame(840, {
                "approval_policy": "never", "_fake": {"result": {"tag": "A"}}}))
            cc.wait_for_id(840, timeout=5)
            f.flush_log()
        finally:
            f.shutdown(timeout=3)
        text = log.read_text()
        assert "event=FACADE_DISPATCH" in text and "call=840" in text


class TestReviewR8:
    def test_audit_writer_start_failure_does_not_kill_the_facade(self, monkeypatch):
        # P1: audit logging is BEST-EFFORT — a thread-exhaustion RuntimeError
        # while starting its writer must not take the MCP server down.
        cc = CCSide()
        real_start = threading.Thread.start

        def boom(self):
            if self.name == "facade-log":
                raise RuntimeError("can't start new thread")
            return real_start(self)
        monkeypatch.setattr(threading.Thread, "start", boom)
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2)
        try:
            assert f._log_thread is None
            f.handle_cc_frame(_call_frame(900, {
                "approval_policy": "never", "_fake": {"result": {"tag": "OK"}}}))
            assert _payload(cc.wait_for_id(900, timeout=5))["tag"] == "OK"
        finally:
            f.shutdown(timeout=3)

    def test_flush_log_waits_for_the_line_to_land(self, tmp_path, monkeypatch):
        # P2: empty() went true while the writer was still INSIDE append_line, so
        # flush_log could return before the line was on disk. It now pushes a
        # sentinel THROUGH the queue — proven by stalling the writer: flush_log
        # must return only once the stalled write completes, not on its timeout.
        cc = CCSide()
        log = tmp_path / "facade.log"
        gate = threading.Event()
        real_append = codex_facade.bulldozer_log.append_line

        def gated_append(path, event, **kv):
            gate.wait(10)
            return real_append(path, event, **kv)
        monkeypatch.setattr(codex_facade.bulldozer_log, "append_line",
                            gated_append)
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(log))
        try:
            f.handle_cc_frame(_call_frame(910, {
                "approval_policy": "never", "_fake": {"result": {"tag": "A"}}}))
            cc.wait_for_id(910, timeout=5)
            threading.Timer(0.4, gate.set).start()   # the write lands at ~0.4s
            t0 = time.monotonic()
            f.flush_log(timeout=8)                   # generous — must NOT time out
            elapsed = time.monotonic() - t0
            assert 0.3 < elapsed < 4.0    # returned WITH the write, not on timeout
            assert "call=910" in log.read_text()     # …and the line is on disk
        finally:
            gate.set()
            f.shutdown(timeout=3)

    def test_shutdown_flushes_teardown_lines_and_joins_the_writer(self, tmp_path):
        cc = CCSide()
        log = tmp_path / "facade.log"
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(log))
        f.handle_cc_frame(_call_frame(920, {
            "approval_policy": "never", "_fake": {"result": {"tag": "A"}}}))
        cc.wait_for_id(920, timeout=5)
        writer = f._log_thread
        f.shutdown(timeout=3)
        assert f._log_thread is None
        assert not writer.is_alive()            # joined, not leaked
        assert "event=FACADE_DONE" in log.read_text()

    def test_dropped_audit_lines_are_counted_and_reported(self, tmp_path,
                                                          monkeypatch):
        # P2: a full queue silently discarded the call↔worker correlation lines.
        cc = CCSide()
        log = tmp_path / "facade.log"
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(log))
        try:
            # Stall the writer, then fill the queue so the next lines are lost.
            gate = threading.Event()
            real_append = codex_facade.bulldozer_log.append_line

            def blocking_append(path, event, **kv):
                gate.wait(10)
                return real_append(path, event, **kv)
            monkeypatch.setattr(codex_facade.bulldozer_log, "append_line",
                                blocking_append)
            f._log("WARMUP", n=0)                     # the writer blocks on this
            time.sleep(0.05)
            while True:
                try:
                    f._log_q.put_nowait(("FILLER", {"n": 1}))
                except queue.Full:
                    break
            f._log("FACADE_DISPATCH", call="999")     # DROPPED (queue full)
            f._log("FACADE_DONE", call="999")         # DROPPED
            assert f._log_dropped >= 2
            gate.set()                                # let the backlog drain
            f.flush_log(timeout=20)
            f._log("FACADE_MARK", call="1")           # backlog clear → report
            f.flush_log(timeout=10)
            assert "event=FACADE_LOG_DROPPED" in log.read_text()
        finally:
            f.shutdown(timeout=3)


class TestReviewR9:
    def test_no_worker_is_spawned_after_cc_eof(self):
        # P1: a placement racing shutdown could spawn a worker absent from the
        # teardown snapshot — it would never be closed, waited on, or cleaned.
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2)
        f.handle_cc_frame(_call_frame(1000, {
            "approval_policy": "never", "_fake": {"result": {"tag": "A"}}}))
        cc.wait_for_id(1000, timeout=5)
        f._shutting_down = True                  # CC EOF
        before = len(f._workers)
        with f._lock:
            f._calls[1001] = {"posture": _posture(), "frame": _call_frame(1001),
                              "tool": "codex_run", "args": {}, "worker": None,
                              "resume_of": None, "group": None}
            assert f._place(1001) is False       # refused — no post-EOF spawn
        assert len(f._workers) == before
        f.shutdown(timeout=3)

    def test_shutdown_does_not_hang_on_a_wedged_log(self, tmp_path, monkeypatch):
        # P2: a blocking put(None) on a FULL queue waited forever when the log
        # file itself was wedged — the facade could not exit on CC EOF.
        cc = CCSide()
        gate = threading.Event()
        real_append = codex_facade.bulldozer_log.append_line
        monkeypatch.setattr(codex_facade.bulldozer_log, "append_line",
                            lambda p, e, **kv: (gate.wait(30),
                                                real_append(p, e, **kv))[1])
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=2, log_path=str(tmp_path / "l.log"))
        try:
            f._log("WARMUP", n=0)                # the writer blocks here
            time.sleep(0.05)
            while True:
                try:
                    f._log_q.put_nowait(("FILLER", {"n": 1}))
                except queue.Full:
                    break
            t0 = time.monotonic()
            f.shutdown(timeout=2)                # must NOT hang on the sentinel
            assert time.monotonic() - t0 < 15
        finally:
            gate.set()

    def test_unplaceable_markers_do_not_accumulate(self):
        # P2: a persistent spawn failure re-drained the queue per settled call,
        # appending duplicate markers (quadratic growth in memory and spawns).
        cc = CCSide()
        f = codex_facade.Facade(cc_write=cc.write,
                                worker_argv=[sys.executable, FAKE_WORKER],
                                max_workers=1)
        spawns = {"n": 0}
        real_spawn = f._spawn

        def counting_spawn():
            spawns["n"] += 1
            if spawns["n"] > 1:                  # every RESPAWN fails
                raise OSError("simulated spawn failure")
            return real_spawn()
        f._spawn = counting_spawn
        try:
            f.handle_cc_frame(_call_frame(1010, {
                "approval_policy": "never",
                "_fake": {"sleep": 0.3, "result": {"tag": "A"}}}))
            for mid in (1011, 1012, 1013, 1014):
                f.handle_cc_frame(_call_frame(mid, {
                    "approval_policy": "never", "_fake": {"result": {"tag": "X"}}}))
            with f._lock:
                worker = f._calls[1010]["worker"]
            worker.proc.kill()
            for mid in (1010, 1011, 1012, 1013, 1014):
                assert cc.wait_for_id(mid, timeout=6)["result"]["isError"] is True
            with f._lock:
                assert f._unplaceable == []
                assert f._calls == {}
            assert spawns["n"] <= 10      # linear-ish, not quadratic (was ~15+)
        finally:
            f.shutdown(timeout=3)
