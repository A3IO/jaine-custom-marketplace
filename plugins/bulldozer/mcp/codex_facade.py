"""Codex facade multiplexer (#344) — ONE registered MCP server fronting a lazy
pool of UNCHANGED codex_server.py workers over pipes.

Spec: docs/superpowers/specs/2026-07-13-codex-facade-multiplexer-design.md.

Layering: this module starts with the PURE scheduler core (§3.2 — posture
classification, thread posture map, pairwise admission, FIFO queue) so every
serialization rule is unit-testable without processes; the plumbing (worker
pool, dispatch, id remap, park affinity, EOF teardown) builds on top of it.

Admission and PLACEMENT are one atomic step: the scheduler admits a call only
if it neither conflicts nor fails to get a worker (`place_fn`), so capacity is
the pool's real worker count — never a parallel accounting that can drift.
"""

import dataclasses
import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import bulldozer_log  # noqa: E402

# Mirrors the engine default: approval_policy_for_start = <arg> or "on-request"
# (codex_server.py) — an omitted policy means the turn CAN emit approval
# requests, so the facade must schedule it as approval-capable.
_DEFAULT_APPROVAL_POLICY = "on-request"
_DEFAULT_SANDBOX = "read-only"
_WRITE_SANDBOX = "workspace-write"
_DANGER_SANDBOX = "danger-full-access"


@dataclasses.dataclass(frozen=True)
class Posture:
    """A call's scheduling class (§3.2), derived at dispatch time.

    root            canonical writable root (None = holds no root: read-only,
                    or a writing sandbox with no cwd → per-worker tmpdir)
    global_writer   excludes every write-capable call: danger-full-access OR
                    approval-capable (r5 — a mid-turn grant can cover ANY root)
    approval_capable  effective approval_policy != "never" (r5/r6: the sandbox
                    does not narrow the class; sticky threads stay capable)
    sandbox         the EFFECTIVE sandbox class (persisted so a later
                    cwd-only resume can be re-rooted correctly)
    temp_cwd        the turn runs in the worker's OWN $TMPDIR (writing sandbox,
                    no cwd) — such a thread pins its worker against reaping
    assumed         a cold-resume GUESS, not observed truth — it may schedule
                    conservatively but must never be persisted as the thread's
                    posture (that would widen the thread forever on ignorance)
    """

    tool: str
    root: str | None
    global_writer: bool
    approval_capable: bool
    thread_id: str | None
    sandbox: str | None = None
    temp_cwd: bool = False
    assumed: bool = False


def prepare_dispatch_args(tool: str, args: dict) -> dict:
    """§3.1: forward tools/call args verbatim — with ONE deliberate exception:
    a codex_review dispatch gets approval_policy="never" injected (r5/r6 P2).
    The engine passes extra keys through (codex_review_v2: run_args =
    dict(args)) and approvalPolicy is thread-level, so the injected value
    governs the review turn — review fan-out is approval-free by construction.
    """
    if tool == "codex_review":
        out = dict(args)
        out["approval_policy"] = "never"
        return out
    return args


class ThreadMap:
    """Per-thread persisted posture + sticky approval-capability (§3.2 rule 1
    resume-posture / rule 3 grant-persistence).

    Sticky widening (r6 P1): once ANY approval-capable turn ran on a thread, a
    session-scope grant may have widened its write scope invisibly (in #340
    dialog mode the reply never transits the facade), so the thread schedules
    as approval-capable/global-writer forever after — explicit "never" on a
    later resume does NOT un-widen (upward-only refresh).
    """

    def __init__(self):
        self._postures: dict[str, Posture] = {}
        self._sticky: set[str] = set()

    def known(self, thread_id: str) -> bool:
        return thread_id in self._postures

    def is_sticky(self, thread_id: str) -> bool:
        return thread_id in self._sticky

    def persisted(self, thread_id: str) -> Posture | None:
        return self._postures.get(thread_id)

    def forget(self, thread_id: str) -> None:
        """Drop a thread's persisted posture (its effective state became
        AMBIGUOUS). The sticky bit is deliberately KEPT: an approval-capable
        turn may already have widened the thread with a session grant."""
        self._postures.pop(thread_id, None)

    def record_dispatch(self, posture: Posture) -> None:
        """Persist the EFFECTIVE posture of a dispatched RESUME (refresh-on-
        explicit is already folded in by classify_call)."""
        if posture.thread_id is None:
            return
        self._postures[posture.thread_id] = posture
        if posture.approval_capable:
            self._sticky.add(posture.thread_id)

    def bind(self, thread_id: str | None, posture: Posture) -> None:
        """Bind a NEW-thread call's posture to the thread_id its RESULT
        returned. A new thread is started WITHOUT a thread_id arg (the tool
        contract: pass thread_id only to resume), so the facade learns the id
        post-hoc from the worker's response and binds the posture then."""
        if thread_id is None:
            return
        self._postures[thread_id] = dataclasses.replace(
            posture, thread_id=thread_id)
        if posture.approval_capable:
            self._sticky.add(thread_id)


def _canonical_root(cwd: str) -> str:
    return os.path.realpath(cwd)


def classify_call(tool: str, args: dict, thread_map: ThreadMap) -> Posture:
    """Derive the §3.2 scheduling posture from prepared dispatch args.

    New thread: effective values from explicit args + engine defaults.
    Known-thread resume: omitted args INHERIT the persisted posture (the engine
    does the same inside thread state, invisibly to the facade); explicit args
    refresh it — upward-only for approval-capability (sticky). Each arg is
    inherited INDEPENDENTLY (review P1): a resume may carry only `cwd` while
    inheriting the sandbox, so the root is re-derived from the explicit cwd
    against the EFFECTIVE sandbox, not kept at the persisted root.
    Unknown-thread resume (cross-session): conservative — approval-capable AND
    global writer; correctness over throughput for the rare cold-resume.
    """
    if tool == "codex_info":
        # Connection-level read, not a turn: no approvals, no writes. Its
        # DESIGNATED-worker requirement (§3.1) is a PLACEMENT constraint the
        # pool enforces, not a scheduling conflict.
        return Posture(tool=tool, root=None, global_writer=False,
                       approval_capable=False, thread_id=None)

    thread_id = args.get("thread_id")
    explicit_policy = args.get("approval_policy")
    explicit_sandbox = args.get("sandbox")
    explicit_cwd = args.get("cwd")

    if thread_id is not None and not thread_map.known(thread_id):
        # Cold cross-session resume: the thread's real posture is unknown, so
        # assume the worst. An explicit cwd is honored as a root as well.
        # `assumed` marks this a SCHEDULING guess: it must never be persisted as
        # the thread's truth once the thread becomes known (that would widen the
        # thread forever — sticky — on nothing but our own ignorance).
        return Posture(
            tool=tool,
            root=_canonical_root(explicit_cwd) if explicit_cwd else None,
            global_writer=True, approval_capable=True, thread_id=thread_id,
            sandbox=explicit_sandbox, temp_cwd=False, assumed=True)

    persisted = thread_map.persisted(thread_id) if thread_id is not None else None

    # --- approval capability (effective policy != never; sticky is upward-only)
    if thread_id is not None and thread_map.is_sticky(thread_id):
        approval_capable = True
    elif explicit_policy is not None:
        approval_capable = explicit_policy != "never"
    elif persisted is not None:
        approval_capable = persisted.approval_capable
    else:
        approval_capable = _DEFAULT_APPROVAL_POLICY != "never"

    # --- effective sandbox (inherited INDEPENDENTLY of cwd)
    if explicit_sandbox is not None:
        sandbox = explicit_sandbox
    elif persisted is not None and persisted.sandbox is not None:
        sandbox = persisted.sandbox
    else:
        sandbox = _DEFAULT_SANDBOX

    writes = sandbox == _WRITE_SANDBOX
    danger = sandbox == _DANGER_SANDBOX

    # --- writable root: an explicit cwd wins; else inherit the thread's root
    root = None
    temp_cwd = False
    if writes:
        if explicit_cwd:
            root = _canonical_root(explicit_cwd)
        elif persisted is not None:
            root = persisted.root
            temp_cwd = persisted.temp_cwd
        else:
            temp_cwd = True   # new thread, no cwd → the worker's own $TMPDIR

    global_writer = danger or approval_capable
    return Posture(tool=tool, root=root, global_writer=global_writer,
                   approval_capable=approval_capable, thread_id=thread_id,
                   sandbox=sandbox, temp_cwd=temp_cwd)


def _roots_overlap(a: str, b: str) -> bool:
    """True iff the two canonical paths are equal or one contains the other.

    Compared with a trailing separator so `/w/a` never 'contains' `/w/ab`, and
    the filesystem root `/` correctly contains everything. The comparison is
    CASE-INSENSITIVE (review P1): the default macOS volume is case-insensitive,
    where `/Repo` and `/repo` are ONE directory that `realpath` still spells two
    ways — and over-serializing two genuinely distinct case-variant paths on a
    case-sensitive volume is the safe direction to be wrong in.
    """
    a = (a if a.endswith(os.sep) else a + os.sep).lower()
    b = (b if b.endswith(os.sep) else b + os.sep).lower()
    return a == b or a.startswith(b) or b.startswith(a)


def conflicts(a: Posture, b: Posture) -> bool:
    """Pairwise §3.2 admission check — True = the two calls must not overlap.

    Rule 2: one in-flight turn per thread.
    Rule 1: writable roots must not overlap (equal / ancestor / descendant);
            a global writer excludes every write-capable call.
    Rule 3: at most one approval-capable turn at a time (the funnel) —
            implied by global-writer today, asserted independently so the
            funnel survives any future loosening of the writer rule.
    """
    if a.thread_id is not None and a.thread_id == b.thread_id:
        return True
    a_writer = a.global_writer or a.root is not None or a.temp_cwd
    b_writer = b.global_writer or b.root is not None or b.temp_cwd
    if a.global_writer and b_writer:
        return True
    if b.global_writer and a_writer:
        return True
    if a.root is not None and b.root is not None and _roots_overlap(a.root, b.root):
        return True
    if a.approval_capable and b.approval_capable:
        return True
    return False


@dataclasses.dataclass
class _Entry:
    call_id: str
    posture: Posture
    group: str | None    # calls in the same group share ONE worker (the funnel)


class Scheduler:
    """§3.2 admission + FIFO queue. Process-free: the facade feeds it events
    (submit / release / cancel / park) and supplies two callbacks —

      place_fn(call_id) -> bool   assign a worker NOW (the pool's real capacity);
                                  False = no worker available → queue.
      reclassify(call_id) -> Posture | None   re-derive a QUEUED call's posture
                                  at DEQUEUE time (its thread may have been
                                  widened or re-rooted by the call ahead of it).

    Admission and placement are ONE atomic step, so a drained batch can never
    admit more calls than the pool can actually run.

    Fairness: a call queues if it conflicts with an ACTIVE call, with any
    already-QUEUED call, or shares a QUEUED call's group (FIFO inside the
    designated-worker funnel).
    """

    def __init__(self):
        self._active: dict[str, _Entry] = {}
        self._queue: list[_Entry] = []

    def _conflicts_with_any(self, e: _Entry, others) -> bool:
        return any(conflicts(e.posture, o.posture) for o in others)

    def _blocked_by_queue(self, e: _Entry, ahead: list[_Entry]) -> bool:
        for a in ahead:
            if conflicts(e.posture, a.posture):
                return True
            if e.group is not None and a.group == e.group:
                return True   # FIFO within the shared worker (review P2)
        return False

    def submit(self, call_id: str, posture: Posture, group=None,
               place_fn=None) -> bool:
        """True = dispatched (worker assigned, registered active).
        False = queued (conflict, group-FIFO, or no worker available)."""
        e = _Entry(call_id, posture, group)
        if (self._conflicts_with_any(e, self._active.values())
                or self._blocked_by_queue(e, self._queue)
                or (place_fn is not None and not place_fn(call_id))):
            self._queue.append(e)
            return False
        self._active[call_id] = e
        return True

    def _drain(self, place_fn=None, reclassify=None) -> list[str]:
        placed: list[str] = []
        remaining: list[_Entry] = []
        for e in self._queue:
            if reclassify is not None:
                fresh = reclassify(e.call_id)
                if fresh is not None:
                    e.posture = fresh   # re-evaluated at DEQUEUE time
            if (self._conflicts_with_any(e, self._active.values())
                    or self._blocked_by_queue(e, remaining)
                    or (place_fn is not None and not place_fn(e.call_id))):
                remaining.append(e)
            else:
                self._active[e.call_id] = e
                placed.append(e.call_id)
        self._queue = remaining
        return placed

    def release(self, key: str, place_fn=None, reclassify=None) -> list[str]:
        """A call (or parked reservation) ended: free its slot, then drain."""
        self._active.pop(key, None)
        return self._drain(place_fn, reclassify)

    def cancel_queued(self, call_id: str, place_fn=None, reclassify=None):
        """Remove a still-QUEUED call (§3.1) — it must never execute later.
        Returns (removed, placed): dropping it can unblock calls behind it."""
        for i, e in enumerate(self._queue):
            if e.call_id == call_id:
                del self._queue[i]
                return True, self._drain(place_fn, reclassify)
        return False, []

    def drop_active(self, call_id: str) -> bool:
        """Remove an ACTIVE call without draining (the caller drains once)."""
        return self._active.pop(call_id, None) is not None

    def park_transfer(self, call_id: str, park_token: str) -> None:
        """The turn parked (awaiting_approval): the MCP call returns but the
        reservation OUTLIVES it (§3.2 rule 1) — re-key it under the park token
        until the resume completes, the mirrored cap expires, or the liveness
        probe finds the park already ended inside the worker."""
        e = self._active.pop(call_id, None)
        if e is not None:
            e.call_id = park_token
            self._active[park_token] = e

    def is_active(self, key: str) -> bool:
        return key in self._active

    def is_queued(self, call_id: str) -> bool:
        return any(e.call_id == call_id for e in self._queue)


# ---------------------------------------------------------------------------
# Plumbing: Worker subprocess + Facade multiplexer (§3.1/§3.4/§3.5)
# ---------------------------------------------------------------------------

_DEFAULT_MAX_WORKERS = 4
_PARK_CAP_S_DEFAULT = 1800.0     # mirrors the engine's #277 default
_PARK_CAP_MARGIN_S = 60.0        # the facade unpins only AFTER the engine did
_PARK_PROBE_EVERY_S = 30.0       # liveness probe cadence for a parked worker
_DESIGNATED = "designated"       # the funnel's home worker (approvals + info)
_FACADE_PARALLEL_LINE = (
    "\n\nFACADE: turns run in PARALLEL — just issue concurrent tools/call; "
    "the facade multiplexes an internal worker pool (writable roots and "
    "approval-capable turns are serialized for safety)."
)


def _park_cap_s(env_value=None) -> float:
    """Mirror the engine's `_park_cap_s` EXACTLY (review P2): malformed → the
    default; clamped to [1, 86400]. A facade cap that diverges from the engine's
    would unpin too early (racing a live turn) or too late (a 31-min leak)."""
    raw = os.environ.get("BULLDOZER_PARK_CAP_S") if env_value is None else env_value
    if raw is None or raw == "":
        raw = _PARK_CAP_S_DEFAULT
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = _PARK_CAP_S_DEFAULT
    return max(1.0, min(v, 86400.0))


def _facade_log_path():
    return (os.environ.get("BULLDOZER_CODEX_LOG")
            or os.path.expanduser("~/.claude/hooks/bulldozer-codex.log"))


def _engine():
    """Lazy import of the sibling engine module (codex_server.py). Reused for
    TOOLS / _initialize_result / JsonRpcStream — zero schema drift by
    construction (§3.1)."""
    import codex_server
    return codex_server


class Worker:
    """One pooled worker: the UNCHANGED engine as a subprocess over pipes,
    speaking the same NDJSON JSON-RPC it speaks to CC today. Owns a PRIVATE
    $TMPDIR (§3.2 rule 1 — shared-temp isolation) and carries BULLDOZER_WORKER=N
    for engine-side audit attribution (§3.1/Env).

    At most ONE tools/call is in flight per worker (the engine is single-turn by
    construction), so `call` is a single id, never a set."""

    def __init__(self, index, argv, env, on_frame, on_exit):
        self.index = index
        self.tmpdir = tempfile.mkdtemp(prefix=f"bulldozer-facade-w{index}-")
        self.handshake_id = f"__facade_init__{os.urandom(8).hex()}"
        wenv = dict(os.environ if env is None else env)
        wenv["BULLDOZER_WORKER"] = str(index)
        wenv["TMPDIR"] = self.tmpdir
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, env=wenv)   # stderr inherited → CC debug log
        except OSError:
            # Popen raised after mkdtemp: nothing will ever reach cleanup_tmpdir
            # for this worker (it is never registered) — clean up here.
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            raise
        self.call = None             # cc call id currently dispatched here
        self.park_token = None       # park pinning this worker (if any)
        self.temp_threads = set()    # threads whose cwd lives in OUR tmpdir
        self.retiring = False        # selected for reaping — never dispatch to
        self.alive = True
        self.last_used = time.monotonic()
        self._wlock = threading.Lock()
        self._reader = threading.Thread(
            target=self._read_loop, args=(on_frame, on_exit),
            name=f"facade-worker-{index}-reader", daemon=True)
        try:
            self._reader.start()
        except RuntimeError:
            # e.g. "can't start new thread" under resource exhaustion: the worker
            # is never registered, so nothing else would ever reap it.
            self.proc.kill()
            try:
                self.proc.wait(timeout=2)   # REAP it — kill() alone leaves a zombie
            except subprocess.TimeoutExpired:
                pass
            self.close_pipes()
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            raise

    def busy(self) -> bool:
        return self.call is not None or self.park_token is not None

    def available(self) -> bool:
        return self.alive and not self.retiring and not self.busy()

    def _read_loop(self, on_frame, on_exit):
        stream = _engine().JsonRpcStream()
        out = self.proc.stdout
        try:
            while True:
                chunk = out.read1(65536)
                if not chunk:
                    break
                for frame in stream.feed(chunk):
                    if not isinstance(frame, dict):
                        continue   # valid JSON, not a JSON-RPC object — drop
                    on_frame(self, frame)
        except Exception:
            pass   # a reader must NEVER die silently mid-call (review r7 P2):
                   # falling through to on_exit fails the call and frees its slot
        finally:
            self.alive = False
            on_exit(self)

    def send(self, frame) -> bool:
        try:
            with self._wlock:
                self.proc.stdin.write((json.dumps(frame) + "\n").encode())
                self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def close_stdin(self):
        try:
            with self._wlock:
                self.proc.stdin.close()
        except OSError:
            pass

    def close_pipes(self):
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    def cleanup_tmpdir(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class Facade:
    """The registered MCP server: ONE stdio connection to CC, N workers inside.

    Threading (§3.5): the caller's loop feeds CC frames via handle_cc_frame;
    each worker has its own reader thread calling _on_worker_frame. ALL shared
    state is guarded by one RLock. CC-facing writes take their own lock and are
    never issued while holding the state lock."""

    def __init__(self, cc_write, worker_argv=None, max_workers=None, env=None,
                 park_cap_s=None, log_path=None):
        if max_workers is None:
            try:
                max_workers = int(os.environ.get(
                    "BULLDOZER_MAX_WORKERS", str(_DEFAULT_MAX_WORKERS)))
            except ValueError:
                max_workers = _DEFAULT_MAX_WORKERS
        # A non-positive cap can never place ANYTHING: every call would queue
        # forever with no worker to produce a release event (review r7 P2).
        max_workers = max(1, int(max_workers))
        if worker_argv is None:
            worker_argv = [sys.executable,
                           os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "codex_server.py")]
        self._argv = worker_argv
        self._env = env
        self._park_cap_s = _park_cap_s() if park_cap_s is None else _park_cap_s(
            park_cap_s)
        self._log_path = log_path or _facade_log_path()
        self._cc_write_raw = cc_write
        self._cc_lock = threading.Lock()
        self._lock = threading.RLock()
        self._max_workers = max_workers
        self._sched = Scheduler()
        self._tm = ThreadMap()
        self._workers: list[Worker] = []
        self._calls: dict = {}       # cc call id -> entry dict
        self._remap: dict = {}       # facade srv id -> (worker, worker id, owner call id)
        self._parks: dict = {}       # park token -> {"worker","deadline","origin","next_probe"}
        self._parked_origin: dict = {}   # original cc call id -> park token
        self._probes: dict = {}      # probe id -> park token
        self._designated: Worker | None = None
        self._worker_ids = itertools.count()   # MONOTONIC: a removed worker's
        # index must never be reused (review r6 P2 — BULLDOZER_WORKER=N and the
        # facade audit lines would otherwise be ambiguous across a respawn).
        self._unplaceable: list = []   # spawn failed → answer CC after the lock
        self._srv_ids = itertools.count(1)
        self._init_params = None
        self._shutting_down = False
        self._log_q: queue.Queue = queue.Queue(maxsize=10000)
        self._log_dropped = 0
        self._log_thread = threading.Thread(target=self._log_writer,
                                            name="facade-log", daemon=True)
        try:
            self._log_thread.start()
        except RuntimeError:
            # Thread exhaustion. Audit logging is BEST-EFFORT (review r8 P1) —
            # it must never take the whole MCP server down before `initialize`.
            self._log_thread = None

    # -- introspection (tests / audit) ------------------------------------
    def worker_count(self) -> int:
        return len([w for w in self._workers if w.alive])

    def thread_map(self) -> ThreadMap:
        return self._tm

    def _log(self, event, **kv):
        """Audit lines are QUEUED, never written inline (review r7 P2):
        bulldozer_log does blocking file I/O under a shared lock, and _log is
        called while the facade lock is held — a wedged log file would otherwise
        block every worker reader and freeze the whole multiplexer."""
        if self._log_thread is None:
            return
        try:
            self._log_q.put_nowait((event, kv))
        except queue.Full:
            # Never block the bridge — but never lose the gap SILENTLY either
            # (review r8 P2): the count is emitted once the backlog clears.
            self._log_dropped += 1

    def _log_writer(self):
        while True:
            item = self._log_q.get()
            try:
                if item is None:
                    return
                event, kv = item
                if event == "__flush__":
                    self._report_drops()
                    kv.set()          # a flush barrier: the queue is drained AND
                    continue          # every line before it is already on disk
                bulldozer_log.append_line(self._log_path, event, **kv)
                self._report_drops()
            except Exception:
                pass   # best-effort

    def _report_drops(self):
        """Emit the count of audit lines lost to a full queue, once the backlog
        has cleared (review r8 P2 — a silent gap in the call↔worker audit chain
        must never look like "nothing happened")."""
        if self._log_dropped and self._log_q.empty():
            dropped, self._log_dropped = self._log_dropped, 0
            bulldozer_log.append_line(self._log_path, "FACADE_LOG_DROPPED",
                                      n=dropped)

    def flush_log(self, timeout=2.0):
        """Block until every queued audit line is ON DISK. A sentinel is pushed
        THROUGH the queue (review r8 P2: `empty()` goes true while the writer is
        still inside append_line, so it never proved the line had landed)."""
        if self._log_thread is None:
            return
        deadline = time.monotonic() + timeout
        done = threading.Event()
        while time.monotonic() < deadline:
            try:
                self._log_q.put_nowait(("__flush__", done))
                break
            except queue.Full:
                time.sleep(0.01)   # a full queue means a backlog — wait for room
        else:
            return
        done.wait(max(0.0, deadline - time.monotonic()))

    # -- CC-facing output ---------------------------------------------------
    def _cc_send(self, frame):
        with self._cc_lock:
            self._cc_write_raw(frame)

    def _cc_reply(self, mid, result=None, error=None):
        frame = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            frame["error"] = error
        else:
            frame["result"] = result
        self._cc_send(frame)

    def _cc_tool_reply(self, mid, payload, is_error=False):
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        if is_error:
            result["isError"] = True
        self._cc_reply(mid, result)

    def _cancel_cc_requests(self, stale_ids):
        for k in stale_ids:
            self._cc_send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": k}})

    # -- CC → facade --------------------------------------------------------
    def handle_cc_frame(self, frame):
        """Never raises: ONE malformed frame must not take down a multiplexer
        serving N concurrent calls (review P2 — the legacy engine guards this
        too)."""
        try:
            self._handle_cc_frame(frame)
        except Exception as e:
            mid = frame.get("id") if isinstance(frame, dict) else None
            if mid is not None:
                self._fail_call(mid, f"facade error: {e}")
            self._log("FACADE_ERROR", err=repr(e))

    def _fail_call(self, mid, message):
        """Terminal error for a call: drop its state, free its slot, answer CC."""
        with self._lock:
            entry = self._calls.pop(mid, None)
            if entry is not None:
                worker = entry.get("worker")
                if worker is not None and worker.call == mid:
                    worker.call = None
                key = entry.get("resume_of") or mid
                self._sched.drop_active(key)
                self._sched.release(key, self._place, self._reclassify)
            else:
                self._sched.cancel_queued(mid, self._place, self._reclassify)
        self._cc_tool_reply(mid, {"error": message}, is_error=True)

    def _handle_cc_frame(self, frame):
        if not isinstance(frame, dict):
            return   # array/scalar JSON — not a JSON-RPC object; drop
        method = frame.get("method")
        mid = frame.get("id")
        if method == "initialize":
            params = frame.get("params")
            self._init_params = params if isinstance(params, dict) else {}
            result = _engine()._initialize_result(self._init_params)
            result["instructions"] = (
                result.get("instructions") or "") + _FACADE_PARALLEL_LINE
            self._cc_reply(mid, result)
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            self._cc_reply(mid, {"tools": _engine().TOOLS})
        elif method == "tools/call":
            self._handle_tool_call(frame)
        elif method == "notifications/cancelled":
            self._handle_cancel(frame)
        elif method is None and mid is not None:
            self._route_cc_reply(frame)
        elif method is not None and mid is not None:
            self._cc_reply(mid, error={"code": -32601,
                                       "message": f"method not found: {method}"})
        # else: unknown notification — drop

    def _handle_tool_call(self, frame):
        params = frame.get("params")
        params = params if isinstance(params, dict) else {}
        tool = params.get("name")
        mid = frame.get("id")
        args = params.get("arguments")
        args = args if isinstance(args, dict) else {}
        if tool == "codex_approve":
            self._handle_approve(frame, mid, args)
            return
        prepared = prepare_dispatch_args(tool, args)
        frame = {**frame, "params": {**params, "arguments": prepared}}
        with self._lock:
            posture = classify_call(tool, prepared, self._tm)
            group = _DESIGNATED if self._needs_designated(posture) else None
            self._calls[mid] = {"posture": posture, "frame": frame, "tool": tool,
                                "args": prepared, "worker": None,
                                "resume_of": None, "group": group}
            if not self._sched.submit(mid, posture, group=group,
                                      place_fn=self._place):
                self._log("FACADE_QUEUED", call=str(mid), tool=str(tool))
        self._settle()

    def _handle_approve(self, frame, mid, args):
        """Park affinity (§3.1): route HOME by token, bypassing the scheduler —
        the parked reservation (keyed by the token) is already active."""
        token = args.get("park_token")
        with self._lock:
            park = self._parks.get(token)
            worker = park["worker"] if park else None
            if worker is not None and worker.call is not None:
                # An approve for this park is ALREADY in flight (review r3 P2):
                # a second one would overwrite worker.call and strand the first.
                self._cc_tool_reply(
                    mid, {"error": "approval already in flight for this park"},
                    is_error=True)
                return
            if worker is not None and worker.alive and not worker.retiring:
                self._calls[mid] = {"posture": None, "frame": frame,
                                    "tool": "codex_approve", "args": args,
                                    "worker": worker, "resume_of": token,
                                    "group": _DESIGNATED}
                worker.call = mid
                worker.last_used = time.monotonic()
                worker.send(frame)
                self._log("FACADE_DISPATCH", call=str(mid), tool="codex_approve",
                          worker=worker.index)
                return
        self._cc_tool_reply(
            mid, {"error": "parked turn expired: unknown park_token"},
            is_error=True)

    def _route_cc_reply(self, frame):
        with self._lock:
            entry = self._remap.pop(frame.get("id"), None)
        if entry is None:
            return  # late / unknown / tombstoned reply — swallow (§3.4)
        worker, wid, _owner = entry
        fwd = dict(frame)
        fwd["id"] = wid
        worker.send(fwd)

    def _tombstone_requests_locked(self, worker=None, owner=None):
        """§3.4: drop remap entries for a dead worker OR a terminally-ended
        owning call; return the facade-side ids to cancel toward CC (so a
        pending dialog is dismissed and a late reply hits nothing)."""
        stale = [k for k, (w, _wid, own) in self._remap.items()
                 if (worker is not None and w is worker)
                 or (owner is not None and own == owner)]
        for k in stale:
            del self._remap[k]
        return stale

    def _handle_cancel(self, frame):
        params = frame.get("params")
        rid = params.get("requestId") if isinstance(params, dict) else None
        synth = False
        forward_to = None
        with self._lock:
            if rid in self._parked_origin:
                # The call already returned awaiting_approval; its RESERVATION
                # lives under the park token (review P1). Forward the cancel so
                # the worker tears its park down — but do NOT unpin here (review
                # r3 P1): the engine clears its park ASYNCHRONOUSLY, so placing a
                # queued call on that worker now would hit its parked busy-block.
                # Arm an immediate probe instead; the park is released the moment
                # the worker confirms it is no longer parked. No CC reply — the
                # original tools/call was answered at park time.
                token = self._parked_origin[rid]
                park = self._parks.get(token)
                if park is not None:
                    # Send the cancel BEFORE arming the probe (review r4 P2):
                    # otherwise housekeeping can probe in the gap, hear "still
                    # parked", and then miss the teardown for a whole interval.
                    park["worker"].send(frame)
                    park["next_probe"] = 0.0   # probe ASAP → unpin on confirm
            elif rid in self._calls and self._calls[rid]["worker"] is None:
                # Queued, OR admitted-but-not-yet-placed (review P1: the old
                # code dropped a cancel that landed in this window).
                self._calls.pop(rid)
                removed, _placed = self._sched.cancel_queued(
                    rid, self._place, self._reclassify)
                if not removed:
                    self._sched.drop_active(rid)
                    self._sched.release(rid, self._place, self._reclassify)
                synth = True
            elif rid in self._calls:
                forward_to = self._calls[rid]["worker"]   # worker's #218 path
        if synth:
            # §3.1 P1: a QUEUED call must never execute later — answer CC with
            # the worker-shaped interrupted result ourselves.
            self._cc_tool_reply(rid, {"status": "interrupted",
                                      "interrupted_by": "cancel",
                                      "queued": True, "thread_warm": False})
        if forward_to is not None:
            forward_to.send(frame)
        self._settle()
        # unknown id → dropped (spec: only a genuinely unknown id is dropped)

    # -- placement (capacity == the pool's REAL workers) -----------------------
    @staticmethod
    def _needs_designated(posture) -> bool:
        return posture is not None and (posture.approval_capable
                                        or posture.tool == "codex_info")

    def _reclassify(self, call_id):
        """Re-derive a QUEUED call's posture at dequeue time (review P1): the
        call ahead of it may have widened its thread (sticky approval) or moved
        its root. The fresh posture is written BACK into the facade's own call
        state (review P1) so placement, the funnel group, and the eventual
        ThreadMap update all use it. Runs under the lock, inside the scheduler."""
        entry = self._calls.get(call_id)
        if entry is None or entry["posture"] is None:
            return None
        fresh = classify_call(entry["tool"], entry["args"], self._tm)
        entry["posture"] = fresh
        entry["group"] = _DESIGNATED if self._needs_designated(fresh) else None
        return fresh

    def _temp_owner(self, thread_id):
        """The worker whose private $TMPDIR holds this thread's cwd (if any)."""
        if thread_id is None:
            return None
        for w in self._workers:
            if thread_id in w.temp_threads:
                return w
        return None

    def _live(self):
        return [w for w in self._workers if w.alive and not w.retiring]

    def _free_worker(self, exclude=None):
        for w in self._workers:
            if w.available() and w is not exclude:
                return w
        return None

    def _designated_ok(self) -> bool:
        d = self._designated
        return d is not None and d.alive and not d.retiring

    def _place(self, call_id) -> bool:
        """Assign a worker to an admitted call and SEND it. False = no worker
        available right now (the scheduler then queues the call). Called under
        the facade lock, from inside the scheduler — admission and placement are
        one atomic step, so a drained batch can never over-admit."""
        if self._shutting_down:
            # CC is gone (review r9 P1): a worker spawned now would be absent
            # from shutdown()'s snapshot and never closed, waited on or cleaned.
            return False
        if call_id in self._unplaceable:
            # Its spawn already failed this cascade (review r9 P2): retrying it on
            # every subsequent drain is what made the attempts grow quadratically.
            return False
        entry = self._calls.get(call_id)
        if entry is None:
            return False
        posture = entry.get("posture")
        # Pin ONLY while the thread still runs in its owner's private $TMPDIR
        # (review r6 P1): a resume with an explicit cwd has moved OUT of it, so
        # pinning it to a busy owner would queue it for nothing — possibly
        # forever, if that owner is wedged.
        owner = (self._temp_owner(posture.thread_id)
                 if posture is not None and posture.temp_cwd else None)
        if owner is not None:
            # The thread's cwd lives INSIDE this worker's private $TMPDIR (§3.2
            # rule 1). Running the resume elsewhere would let a fresh temp-cwd
            # turn on the owner write the very tree the resume is using — the two
            # postures do not conflict (different tmpdirs, by assumption) but they
            # would share one (review r5 P1). Pin the resume to its owner.
            if not owner.alive or owner.retiring or owner.busy():
                return False
            if entry["group"] == _DESIGNATED:
                # The funnel must keep ONE home worker (§3.1: codex_info is
                # always answered by it). If the current home is a DIFFERENT live
                # worker, hand the role over — but only while it is idle, else
                # wait (review r6 P1).
                d = self._designated
                if (d is not None and d.alive and not d.retiring
                        and d is not owner and d.busy()):
                    return False
                self._designated = owner
            worker = owner
            entry["worker"] = worker
            worker.call = call_id
            worker.last_used = time.monotonic()
            worker.send(entry["frame"])
            self._log("FACADE_DISPATCH", call=str(call_id),
                      tool=str(entry["tool"]), worker=worker.index)
            return True
        try:
            worker = self._pick_worker(entry["group"])
        except (OSError, RuntimeError) as e:
            # OSError = fork/exec failed; RuntimeError = the reader thread could
            # not start (review r7 P1: it used to escape the drain and leave the
            # dead call unanswered).
            # NEVER raise into the scheduler: its drain loop would abort
            # mid-iteration with the queue half-committed (review r3 P1). Record
            # the call as unplaceable — _settle() answers CC and frees its slot
            # once the lock is released.
            self._log("FACADE_SPAWN_FAIL", call=str(call_id), err=repr(e))
            if call_id not in self._unplaceable:
                # De-duplicate (review r9 P2): a persistent spawn failure re-drains
                # the queue once per settled call, and the markers (and the spawn
                # attempts behind them) would otherwise grow quadratically.
                self._unplaceable.append(call_id)
            return False
        if worker is None:
            return False
        entry["worker"] = worker
        worker.call = call_id
        worker.last_used = time.monotonic()
        worker.send(entry["frame"])
        self._log("FACADE_DISPATCH", call=str(call_id),
                  tool=str(entry["tool"]), worker=worker.index)
        return True

    def _settle(self):
        """Answer + free any call whose worker could not be spawned. Runs after
        every scheduler interaction, with the lock released for the CC write.

        Removing the call re-drains the queue WITH placement enabled (review r4
        P1: a follower blocked behind it must still get a worker, not be moved to
        active with none). Idempotent (review r4 P2): a call already answered by
        a racing cancel is skipped — one CC id is never answered twice."""
        while True:
            with self._lock:
                if not self._unplaceable:
                    return
                call_id = self._unplaceable.pop(0)
                entry = self._calls.get(call_id)
                if entry is None:
                    continue   # already terminal (cancelled) — never reply twice
                if entry.get("worker") is not None:
                    # A concurrent drain PLACED it after the failed spawn (review
                    # r5 P1): the marker is stale — the call is running, and
                    # answering it here would drop its real reply and wedge the
                    # worker (its `call` would never be cleared).
                    continue
                self._calls.pop(call_id, None)
                removed, _ = self._sched.cancel_queued(
                    call_id, self._place, self._reclassify)
                if not removed:
                    self._sched.drop_active(call_id)
                    self._sched.release(call_id, self._place, self._reclassify)
            self._cc_tool_reply(
                call_id, {"error": "codex worker could not be started"},
                is_error=True)

    def _pick_worker(self, group):
        """The designated worker is the funnel's home (approvals + codex_info).
        It is NOT reserved exclusively: an ordinary call may use it when it is
        idle and nothing else is free — otherwise a 1-worker pool would deadlock
        (review P2)."""
        if group == _DESIGNATED:
            if self._designated_ok():
                d = self._designated
                return None if d.busy() else d
            w = self._free_worker()
            if w is None and len(self._live()) < self._max_workers:
                w = self._spawn()
            if w is not None:
                self._designated = w
            return w
        w = self._free_worker(exclude=self._designated)
        if w is not None:
            return w
        if len(self._live()) < self._max_workers:
            return self._spawn()
        d = self._designated
        if d is not None and d.available():
            return d      # last resort: the idle funnel home (no deadlock)
        return None

    def _spawn(self) -> Worker:
        w = Worker(next(self._worker_ids), self._argv, self._env,
                   self._on_worker_frame, self._on_worker_exit)
        init_params = self._init_params or {
            "protocolVersion": "2025-06-18",
            "capabilities": {}, "clientInfo": {"name": "bulldozer-facade"}}
        w.send({"jsonrpc": "2.0", "id": w.handshake_id,
                "method": "initialize", "params": init_params})
        w.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._workers.append(w)
        return w

    # -- worker → facade (runs on the worker's reader thread) -----------------
    def _on_worker_frame(self, worker, frame):
        method = frame.get("method")
        fid = frame.get("id")
        if method is not None:
            if fid is None:
                self._cc_send(frame)   # notification passthrough
                return
            with self._lock:
                new_id = f"srv-{next(self._srv_ids)}"
                # Owner = the call this worker is serving (§3.4: a remap entry
                # is ASSOCIATED with its owning tools/call and dies with it).
                self._remap[new_id] = (worker, fid, worker.call)
            fwd = dict(frame)
            fwd["id"] = new_id
            self._cc_send(fwd)
            return
        if fid == worker.handshake_id:
            return   # our own initialize reply (unguessable id)
        if fid in self._probes:
            self._handle_probe_reply(worker, fid, frame)
            return

        stale = []
        with self._lock:
            entry = self._calls.pop(fid, None)
            if entry is None:
                return  # response for a call we no longer track — drop
            if worker.call == fid:
                worker.call = None
            worker.last_used = time.monotonic()
            res = self._parse_tool_result(frame)
            resume_of = entry.get("resume_of")

            # Thread posture is persisted BEFORE the queue drains (review P1) —
            # a queued resume of this thread must be re-classified against the
            # posture this very turn just established.
            if res and entry.get("posture") is not None:
                tid = res.get("thread_id")
                known_thread = entry["posture"].thread_id
                if not tid and known_thread and res.get("error"):
                    # A resume that ERRORED without a thread_id is AMBIGUOUS
                    # (review r8 P1): the engine may have applied its explicit
                    # sandbox/cwd (mid-turn failure) or rejected the call before
                    # turn setup (bad `mcp`). We cannot tell — so we persist
                    # NEITHER root and FORGET the thread instead: every later
                    # resume is then scheduled conservatively (cold-resume rules),
                    # which is safe against both possibilities. Temp-cwd ownership
                    # is deliberately KEPT (dropping it could unpin a thread that
                    # still lives in the owner's $TMPDIR).
                    self._tm.forget(known_thread)
                if tid:
                    posture = entry["posture"]
                    if posture.assumed:
                        # The cold-resume GUESS must never become the thread's
                        # persisted truth. By now the thread may be KNOWN (a
                        # concurrent call bound it) — then re-derive from the
                        # args. If it is STILL unknown we learned nothing, so we
                        # persist NOTHING (review r3 P2): a later resume is
                        # scheduled conservatively again, rather than the guess
                        # widening the thread — sticky — forever.
                        fresh = classify_call(entry["tool"], entry["args"],
                                              self._tm)
                        posture = fresh if not fresh.assumed else None
                    if posture is not None:
                        if posture.thread_id is not None:
                            self._tm.record_dispatch(posture)
                        else:
                            self._tm.bind(tid, posture)
                        if posture.temp_cwd:
                            # The thread's cwd lives in THIS worker's $TMPDIR —
                            # the worker must outlive it or an omitted-cwd resume
                            # would inherit a deleted directory.
                            worker.temp_threads.add(tid)
                        else:
                            # An explicit cwd moved the thread OUT of the private
                            # tmpdir: drop the ownership so later resumes are not
                            # pinned to a worker they no longer need (r6 P1).
                            for w in self._workers:
                                w.temp_threads.discard(tid)

            stale = self._tombstone_requests_locked(owner=fid)
            parked = bool(res) and res.get("status") == "awaiting_approval" \
                and res.get("park_token")
            err = str((res or {}).get("error") or "")
            # A codex_approve ERROR is RETRYABLE and leaves the engine's park
            # UNCHANGED (stale token / hallucinated decision_id — codex_server
            # validates BEFORE gen.send). Tearing our park down here would make
            # a still-live park unreachable (review r3 P1) — so keep it pinned
            # and let the liveness probe reconcile if the engine really ended it.
            # The one exception: 'parked turn expired' means the engine has NO
            # park, so our own record is the stale one.
            keep_park = (resume_of is not None and err
                         and "expired" not in err and not parked)
            if parked:
                # New park, or a RE-park after codex_approve (multi-approval).
                # `origin` is the call that PARKED — the engine binds its own
                # cancellation id to the current call, so a re-park re-binds it
                # to the approve call (review r3 P1).
                token = res["park_token"]
                src = resume_of if resume_of is not None else fid
                if resume_of is not None and resume_of != token:
                    self._forget_park_locked(resume_of)
                self._sched.park_transfer(src, token)
                self._parks[token] = {
                    "worker": worker, "origin": fid,
                    "deadline": time.monotonic() + self._park_cap_s
                    + _PARK_CAP_MARGIN_S,
                    "next_probe": time.monotonic() + _PARK_PROBE_EVERY_S}
                self._parked_origin[fid] = token
                worker.park_token = token   # pinned: not idle, not reaped
                self._log("FACADE_PARK", call=str(fid), worker=worker.index)
            elif keep_park:
                worker.park_token = resume_of   # still pinned; retry is allowed
                self._log("FACADE_APPROVE_RETRYABLE", call=str(fid),
                          worker=worker.index)
            else:
                key = resume_of if resume_of is not None else fid
                if resume_of is not None:
                    self._forget_park_locked(resume_of)
                worker.park_token = None
                self._sched.release(key, self._place, self._reclassify)
                self._log("FACADE_DONE", call=str(fid),
                          tool=str(entry.get("tool")), worker=worker.index)
        self._cc_send(frame)   # forward verbatim (same id — §3.4)
        self._cancel_cc_requests(stale)
        self._settle()

    @staticmethod
    def _parse_tool_result(frame):
        try:
            return json.loads(frame["result"]["content"][0]["text"])
        except Exception:
            return None

    # -- park lifecycle -------------------------------------------------------
    def _forget_park_locked(self, token):
        park = self._parks.pop(token, None)
        if park is not None:
            self._parked_origin.pop(park.get("origin"), None)
        for pid in [p for p, t in self._probes.items() if t == token]:
            self._probes.pop(pid, None)
        return park

    def _release_park_locked(self, token):
        """Unpin a park: free the worker, drop the maps, release the reservation
        (and drain). The caller holds the lock."""
        park = self._forget_park_locked(token)
        if park is not None and park["worker"].park_token == token:
            park["worker"].park_token = None
        self._sched.drop_active(token)
        return self._sched.release(token, self._place, self._reclassify)

    def _park_busy_with_approve(self, token) -> bool:
        park = self._parks.get(token)
        if park is None:
            return False
        w = park["worker"]
        entry = self._calls.get(w.call) if w.call is not None else None
        return entry is not None and entry.get("resume_of") == token

    def expire_parks(self, now=None):
        """The engine's `_parked_wait` tears a park down on its cap WITHOUT any
        MCP frame — so the facade mirrors the cap (+margin) and unpins: worker,
        slot and writable-root reservation. A park whose codex_approve is
        IN FLIGHT is never expired (review P1: the resumed turn may be writing)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            for token in [t for t, p in self._parks.items()
                          if p["deadline"] <= now]:
                if self._park_busy_with_approve(token):
                    continue
                w = self._parks[token]["worker"]
                self._release_park_locked(token)
                self._log("FACADE_PARK_EXPIRED", token=str(token)[:8],
                          worker=w.index)
        self._settle()

    def probe_parks(self, now=None):
        """Bounded LIVENESS PROBE (§3.2 rule 1 — the park-ended signal): the
        engine also ends a park with no MCP frame when its inner app-server dies
        or emits a terminal frame. While a turn is parked the engine busy-blocks
        every tool except codex_approve with a DISTINCT 'codex turn parked'
        error — so a cheap local `codex_info` answers the question: still parked
        (that error) or already ended (anything else → unpin)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            due = [(t, p) for t, p in self._parks.items()
                   if p.get("next_probe", 0) <= now
                   and not self._park_busy_with_approve(t)]
            for token, park in due:
                park["next_probe"] = now + _PARK_PROBE_EVERY_S
                pid = f"__facade_probe__{os.urandom(6).hex()}"
                self._probes[pid] = token
                park["worker"].send({
                    "jsonrpc": "2.0", "id": pid, "method": "tools/call",
                    "params": {"name": "codex_info",
                               "arguments": {"query": "approval"}}})

    def _handle_probe_reply(self, worker, pid, frame):
        with self._lock:
            token = self._probes.pop(pid, None)
            if token is None or token not in self._parks:
                return
            res = self._parse_tool_result(frame) or {}
            err = str(res.get("error") or "")
            if "parked" in err:
                return   # still parked — the engine busy-blocked our probe
            # Anything else means the engine is NOT parked any more (inner-child
            # death / terminal frame): the park ended silently — unpin.
            self._release_park_locked(token)
            self._log("FACADE_PARK_ENDED", token=str(token)[:8],
                      worker=worker.index)
        self._settle()

    # -- worker death ---------------------------------------------------------
    def _on_worker_exit(self, worker):
        if self._shutting_down:
            worker.cleanup_tmpdir()
            return
        dead_call = None
        with self._lock:
            # The call assigned to this worker fails — including a codex_approve
            # resuming its park, which is keyed by its OWN call id while the
            # worker still holds the token (review P1).
            cid = worker.call
            worker.call = None
            entry = self._calls.pop(cid, None) if cid is not None else None
            if entry is not None:
                dead_call = cid
                key = entry.get("resume_of") or cid
                if entry.get("resume_of") is not None:
                    self._forget_park_locked(entry["resume_of"])
                self._sched.drop_active(key)
            if worker.park_token is not None:
                self._forget_park_locked(worker.park_token)
                self._sched.drop_active(worker.park_token)
                worker.park_token = None
            for token in [t for t, p in self._parks.items()
                          if p["worker"] is worker]:
                self._forget_park_locked(token)
                self._sched.drop_active(token)
            stale = self._tombstone_requests_locked(worker=worker)
            if self._designated is worker:
                self._designated = None   # re-adopted / respawned on demand
            if worker in self._workers:
                # Drop it from the pool (review r5 P2): a long-lived facade would
                # otherwise accumulate dead workers, their pipes and their fds,
                # and every placement scan would walk them.
                self._workers.remove(worker)
            worker.close_pipes()
            self._sched.release("__worker_died__", self._place, self._reclassify)
        if dead_call is not None:
            self._cc_tool_reply(dead_call,
                                {"error": "codex worker died mid-call"},
                                is_error=True)
        # §3.1/§3.4: dismiss any CC dialog still pending on the dead worker —
        # requester-side cancel; a late reply then hits a tombstone.
        self._cancel_cc_requests(stale)
        self._log("FACADE_WORKER_DIED", worker=worker.index,
                  call=str(dead_call))
        worker.cleanup_tmpdir()
        self._settle()

    # -- keep-one-warm reap (§3.1 worker lifecycle) ----------------------------
    def reap_idle(self, idle_s=None):
        """Reap idle workers, keeping the most-recently-used one warm. Busy,
        parked, designated and temp-cwd-owning workers are never victims.
        Victims are marked `retiring` UNDER THE LOCK before their stdin is
        closed, so a concurrent placement can never pick one mid-teardown."""
        if idle_s is None:
            idle_s = float(os.environ.get("BULLDOZER_WORKER_IDLE_S", "900"))
        with self._lock:
            idle = [w for w in self._workers if w.available()]
            if len(idle) <= 1:
                return
            mru = max(idle, key=lambda w: w.last_used)
            now = time.monotonic()
            victims = [w for w in idle
                       if w is not mru
                       and not w.temp_threads
                       and w is not self._designated
                       and now - w.last_used >= idle_s]
            for w in victims:
                w.retiring = True
        for w in victims:
            w.close_stdin()   # graceful: worker exits on EOF, its reader reaps it

    # -- teardown (§3.1 CC stdin EOF) ------------------------------------------
    def shutdown(self, timeout=5):
        self._shutting_down = True
        with self._lock:
            workers = list(self._workers)
        for w in workers:
            w.close_stdin()   # graceful FIRST: run the worker's own EOF paths
        deadline = time.monotonic() + timeout
        for w in workers:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                w.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                w.proc.terminate()
                try:
                    w.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    w.proc.kill()
                    try:
                        w.proc.wait(timeout=2)   # REAP — kill() alone leaves a zombie
                    except subprocess.TimeoutExpired:
                        pass
            w.close_pipes()
            w.cleanup_tmpdir()
        # Audit lines are emitted by the teardown itself — flush AFTER it, then
        # stop the writer thread (review r8 P2: it was neither drained nor
        # joined, so repeated Facades leaked writer threads).
        self.flush_log()
        if self._log_thread is not None:
            try:
                self._log_q.put_nowait(None)   # NEVER block (review r9 P2): a
            except queue.Full:                 # wedged log file would otherwise
                pass                           # keep the facade from exiting
            self._log_thread.join(timeout=2)
            self._log_thread = None


# ---------------------------------------------------------------------------
# Entry point (§4 rollout: kill switch first, then the facade loop)
# ---------------------------------------------------------------------------

def main():
    # Kill switch (§4.3): BULLDOZER_FACADE_OFF=1 → exec the single legacy bridge
    # IN PLACE (zero multiplexing, byte-identical path). Checked BEFORE any
    # engine import so the escape hatch works even if the facade is broken.
    if os.environ.get("BULLDOZER_FACADE_OFF"):
        engine_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "codex_server.py")
        os.execv(sys.executable, [sys.executable, engine_path])

    def cc_write(frame):
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()

    test_worker = os.environ.get("BULLDOZER_FACADE_TEST_WORKER")
    worker_argv = [sys.executable, test_worker] if test_worker else None
    facade = Facade(cc_write, worker_argv=worker_argv)

    def _housekeeping():
        while True:
            time.sleep(5)
            try:
                facade.expire_parks()   # the mirrored park cap (§3.2 rule 1)
                facade.probe_parks()    # the park-ended liveness signal
                facade.reap_idle()
            except Exception:
                pass   # housekeeping is best-effort; never kills the server

    try:
        threading.Thread(target=_housekeeping, name="facade-housekeeping",
                         daemon=True).start()
    except RuntimeError:
        # Thread exhaustion (review r9 P2): housekeeping (park cap / liveness
        # probe / reaping) is best-effort — the server must still serve calls.
        print("bulldozer-facade: housekeeping thread unavailable", file=sys.stderr)

    # ONE reader on CC stdin (§3.5); workers never see the real stdin.
    stream = _engine().JsonRpcStream()
    stdin = sys.stdin.buffer
    while True:
        chunk = stdin.read1(65536)
        if not chunk:
            break
        for frame in stream.feed(chunk):
            facade.handle_cc_frame(frame)
    facade.shutdown()   # CC gone → graceful teardown (close worker stdin first)


if __name__ == "__main__":
    main()
