# Codex Facade Multiplexer — internally parallel bridge as the plugin's default server

**Issue:** #344 · **Status:** DESIGN (implementation is a separate session/PR)
**Decision owner:** Chris (2026-07-13: "заводи issue, делай спеку, полосы снимаем, и codex-stock вопрос закрываем")

## 1. Problem

The bulldozer codex bridge (`mcp/codex_server.py`) is single-turn by construction: a serial
dispatcher plus module singletons (`_v2_manager`, `_v2_state_machine`). Everything hardened over
the last year — approval bridging (#18268 fix), park/resume (#277), interrupts (#218/#252),
native dialogs (#340) — assumes exclusive ownership of stdin/stdout and ONE app-server child.
Consequences, all live-verified 2026-07-13:

- Subagents share a session's single connection **per registration**; a second concurrent call
  into the same registration is rejected by the busy guard (`codex turn already in flight`).
- The stock `codex mcp-server` IS internally concurrent (one process; two turns overlapped
  fully: 148 s + 124 s, wall = max) — but it carries the #18268 approval bug (Accept parsed as
  Denied), no MCP isolation, no audit log, and pulls the user's codex plugins into verdicts.
- The **lane pool** (N named local registrations of our own bridge) parallelizes WITH all our
  features — probes on 2 lanes: turns 99.6 s and 128.3 s, overlap 102 s, wall 147 s ≠ sum 249 s,
  both `cold_spawn=true`, `+2` app-server children. But it costs N idle processes per session,
  demands explicit per-lane routing in every fan-out prompt, only exists where registered, and
  local copies of the server **suppress the plugin's own registration**
  (`excludeStalePluginClients`, observed 3/3 sessions).

Chris's verdict: one server, parallel inside — lanes and stock removed (2026-07-13) pending this
feature.

## 2. Goal / non-goals

**Goal:** ONE MCP server (the plugin's default `.mcp.json` entry) that runs multiple codex turns
concurrently with the full existing feature set, near-zero idle footprint, and zero per-project
registrations. Consumers get parallel codex out of the box.

**Non-goals:** rewriting the single-turn engine (it is the asset being preserved — the ONLY
engine change is the additive env-gated `worker=` log field, §3.1/Env); cross-machine
pooling; parallelism inside ONE assistant turn beyond what CC dispatches; removing the busy
guard from the worker (it stays as worker-level defense-in-depth).

## 3. Architecture

```
CC session ──stdio── FACADE (mcp/codex_facade.py — the registered server)
                      │  serial stdin reader; stdout writes behind a lock
                      │  id-remap tables; park→worker map; call→worker map
                      ├─pipe── worker 1: python3 codex_server.py   (UNCHANGED engine)
                      ├─pipe── worker 2: python3 codex_server.py
                      └─ …    (lazy: spawned on demand, reaped when idle)
```

The facade is a thin MCP **multiplexer**. Each worker is the existing `codex_server.py` run as a
subprocess speaking MCP over pipes — byte-identical protocol to what CC speaks to it today. The
year of approval/interrupt/park/dialog hardening ships inside every worker, untouched.

### 3.1 Facade responsibilities

| Concern | Behavior |
|---|---|
| `initialize` | Answered by the facade itself: same `serverInfo`, `SERVER_INSTRUCTIONS` imported from `codex_server` + one appended line ("turns run in parallel; just issue concurrent calls"). Workers are initialized by the facade on spawn (facade replays its own `initialize` params). |
| `tools/list` | Served from `codex_server`'s own schema constants (same module import — zero drift by construction). |
| `tools/call` | Dispatch through the §3.2 scheduler (writable-root serialization, per-thread affinity, approval-capable serialization) to an idle worker; none idle → spawn (cap `BULLDOZER_MAX_WORKERS`, default 4); at cap → FIFO queue. A QUEUED call is cancellable: `notifications/cancelled` for its id removes it from the queue and answers CC with the worker-shaped interrupted result — it must never execute later (review P1). Record `cc_call_id → worker`. Forward the frame verbatim (same id — see §3.4) — with ONE deliberate exception (r6 P2): a `codex_review` dispatch gets `approval_policy: "never"` injected into its arguments before forwarding (§3.2(3) — the injection IS the dispatch contract for review calls, not a violation of it). |
| Server→client requests (elicitation, approvals) | Proxy worker→CC with **id remapping**: worker request id `x` → globally unique facade id `x'`; CC's reply routed back by the `x' → (worker, x)` table. Payloads pass through untouched — every arm's decision mapping stays worker-side. |
| `notifications/cancelled` (Esc) | `params.requestId` names the CC-side `tools/call` id → (1) if the call is still QUEUED: dequeue + answer as interrupted (review P1); (2) if dispatched: forward to the owning worker (its own #218 machinery). Only a genuinely unknown id is dropped. |
| Park affinity (#277) | A worker result with `status: awaiting_approval` registers `park_token → worker` before forwarding to CC. A later `codex_approve` call is routed to that worker, NOT the idle-dispatch path. Token unknown/expired → the worker-shaped `parked turn expired` error, facade-synthesized. A parked worker is pinned: not reaped, not dispatched other calls (its own `_parked_busy_block` would reject anyway). **The pin has a deadline** (review P1): a worker whose park cap (`BULLDOZER_PARK_CAP_S`, default 1800 s) expires auto-declines INTERNALLY without emitting any MCP frame — the facade mirrors the cap (+60 s margin) and unpins on expiry; a later `codex_approve` still routes to that worker and receives its natural `parked turn expired`. |
| `thread_id` resume | No affinity required: cross-session resume already works from `~/.codex/sessions/` rollout files, so ANY worker can resume any thread. Optional warm-affinity optimization (route to the worker already holding that thread) is a nice-to-have, NOT correctness. |
| `codex_info` | Routed to the DESIGNATED worker, always (review r2 P2: `query='servers'`/`config` reflect the answering worker's live connection state, which varies with its last `mcp` selection — idle-worker routing would make answers nondeterministic). Cold spawn for an info call is acceptable and rare. |
| Worker lifecycle | Lazy spawn; reap after `BULLDOZER_WORKER_IDLE_S` (default 900 s) idle; **keep-one-warm** policy (the most-recently-used worker survives reaping) to amortize the 28–80 s cold start for the common single-call pattern. Parked or in-flight workers are never reaped. **A worker OWNING a temp-cwd thread is not reaped either** (review r4 P2): a thread started with omitted `cwd` runs under that worker's `$TMPDIR`, deleted at the worker's process exit — reaping it would break a later omitted-arg resume that inherits that cwd. Such workers are pinned until the thread's temp-cwd is provably unreferenced (or the session ends); alternatively the facade owns the temp-cwd (impl choice, deferred to code). |
| Worker crash / EOF mid-call | Error result for THAT call only (worker-shaped `{"error": …}`); facade logs and survives; the worker is removed from the pool. |
| CC stdin EOF | Teardown by CLOSING each worker's stdin first (review P1: a kill would BYPASS the worker's own EOF handling — the #218 in-flight interrupt, the parked-turn teardown, the atexit tmpdir cleanup, `_osascript_stage` dialog cleanup), then a bounded drain/wait (5 s), then terminate → kill as the escalating fallback. **Limitation (review r3 P2):** a worker blocked in COLD `thread/start`/`resume` setup, or run with `BULLDOZER_CODEX_NO_INTERRUPT`, is not watching its MCP stdin, so the graceful close may not be honored within the bound and the SIGTERM fallback then skips `atexit` tmpdir cleanup — the SAME exposure the single bridge has today (CC kills it identically), NOT a regression; a future engine shutdown hook (out of scope) would close it. EOF-priority for an ACTIVE turn is preserved because the worker runs its own #218 path. |
| Env | Facade passes its environment through to workers. All existing knobs (approval sentinels #340, unattended #277, translation #247, logs) work unchanged — they are read by worker code. **Audit attribution** (review r1+r2 P2): the facade writes `FACADE_DISPATCH` / `FACADE_DONE` lines (call id ↔ worker N ↔ tool) via `lib/bulldozer_log.py`, AND the engine gains ONE additive, env-gated field — `_drift_warn` appends `worker=N` when `BULLDOZER_WORKER` is set (engine sets nothing else; second-resolution timestamps + identical models make time-based correlation unreliable, so the worker id must be ON the TURN_OK/TURN_ERROR/APPROVAL lines themselves). This is the single deliberate engine touch of the feature — additive, default-off, one line. |

### 3.2 Scheduler constraints (review P1 ×3 — correctness over raw parallelism)

Dispatch is NOT plain round-robin. Three serialization rules, checked in order:

1. **Writable-root serialization.** A `workspace-write` call registers its writable root
   (`cwd`, realpath-canonicalized; omitted `cwd` = the worker's own isolated tmpdir → no root,
   freely parallel). Two writable calls whose roots overlap (equal, or ancestor/descendant)
   NEVER run concurrently — the second queues behind the first. **Shared temp is isolated per
   worker** (review r3 P2): `workspace-write` also permits writes to `$TMPDIR` and `/tmp`
   (`_sandbox_policy` leaves both un-excluded), so two "disjoint-cwd" writers could still collide
   on a common temp path — the facade gives each worker its OWN `$TMPDIR` (private mktemp dir), so
   `$TMPDIR` writes never overlap; literal-`/tmp` writes remain a shared surface, but that is
   PRE-EXISTING codex behavior (today's single bridge already lets sequential turns share `/tmp`)
   — documented, not regressed. **`danger-full-access` is a
   GLOBAL writer** (review r2 P1): it can write anywhere regardless of `cwd`, so it conflicts
   with EVERY write-capable call (and they with it) — effectively a global write lock. This is
   the lane doctrine ("two workspace-write lanes into one checkout race it") enforced
   structurally instead of by prose. Read-only calls hold no root; the `never`-policy ones
   parallelize without constraint (a read-only `on-request` call is approval-capable — rule 3
   makes it a global writer; r5).
   **The root reservation OUTLIVES the MCP call when the turn parks** (review r2 P1): an
   `awaiting_approval` return leaves a suspended generator that can resume writing via
   `codex_approve` — the reservation is held until the turn finally completes, the mirrored
   park cap expires, **the inner app-server child dies / emits a terminal frame** (review r4 P2:
   `_parked_wait` clears the park LOCALLY on inner-child teardown without any MCP frame, so
   mirroring only the elapsed cap would pin the worker + its root for up to ~31 min after the turn
   is already dead — the facade needs a park-ended SIGNAL or a bounded liveness probe of the inner
   child, not just the cap timer), or the worker dies.
   **Resume posture** (review r2 P1): `codex_run(thread_id=…)` with omitted `sandbox`/`cwd`/
   `approval_policy` INHERITS the thread's previous posture inside the engine, invisibly to the
   facade. The facade therefore persists each thread's effective posture (root, sandbox class,
   approval-capability) in its thread map and REFRESHES it on every dispatch whose args carry an
   explicit `sandbox`/`cwd`/`approval_policy` (review r3 P1: an explicit resume can UPGRADE a
   thread — read-only → workspace-write — and later omitted resumes inherit the NEW value, so
   recording only at first dispatch would leave the facade stale), applying the stored posture to
   any omitted-arg resume — with ONE asymmetry (r6 P1): approval-capability refreshes UPWARD
   only; a thread marked sticky-approval-capable ignores an explicit `never` (rule 3's
   grant-persistence note); a resume of a thread the facade has NEVER seen (cross-session) is
   scheduled CONSERVATIVELY: treated as approval-capable AND a global writer (funneled +
   exclusive) — correctness over throughput for the rare cold-resume.

2. **Per-thread affinity.** The v2 contract allows ONE in-flight turn per thread. The facade keeps
   an active/parked `thread_id` ownership map; a call carrying a `thread_id` that is active on
   worker W queues FOR worker W (FIFO per thread). Cross-session resume proves a COMPLETED thread
   loads elsewhere — not that two simultaneous turns on one rollout are safe.

   **Constraint interaction** (review r2 P1): a call is assigned a worker only at DEQUEUE
   time, when ALL its constraints are simultaneously satisfiable — constraints are re-evaluated
   then, never sticky. Concretely: a call for thread T (active on non-designated worker W) that
   is also approval-capable WAITS until T is inactive, then routes by rule 3 to the designated
   worker (the thread map hands T over to it). No deadlock: waiting calls hold no reservations.

3. **Approval-capable serialization + global-writer exclusion (phase 1 = serialize; review r4
   collapsed r3's "conditional" idea; r5 widened the exclusion and fixed the class).** An
   approval-CAPABLE call is one whose EFFECTIVE `approval_policy` ≠ `never` — the sandbox does
   NOT narrow the class (r5): even a read-only `on-request` turn can emit
   `item/permissions/requestApproval` and be GRANTED a `fileSystem` write profile (that is what
   the #272 grant-echo does), and the engine defaults to `on-request` for BOTH tools
   (`approval_policy_for_start = … or "on-request"`; `codex_review_v2` sets `sandbox` but never
   the policy). Such a call is funneled to a single DESIGNATED worker (FIFO), at most one at a
   time, AND is treated as a GLOBAL WRITER in rule 1 (the `danger-full-access` class): it
   excludes — and is excluded by — every write-capable call for its whole lifetime, park
   included. **Grant persistence beyond the turn (r6 P1):** an accepted permissions request can
   carry `scope: "session"` (the `Grant for this session` label), and that widened `fileSystem`
   scope OUTLIVES the turn that acquired it — releasing the exclusion at turn completion would
   let a later explicit `approval_policy: "never"` resume of the same thread overlap a writer
   inside the granted root. The facade CANNOT reliably observe grants to narrow this (in #340
   dialog mode the approval reply never transits the facade — the worker answers locally via
   osascript), so the fix is observation-free: the posture map marks a thread
   STICKY-approval-capable once ANY approval-capable turn has run on it — every later turn on
   that thread, including an explicit `never` resume, schedules as approval-capable/global-writer
   for the rest of the facade session (rule 1's explicit-refresh is thereby UPWARD-only for
   approval-capability: an explicit `never` never un-widens a thread; cross-session cold-resume
   is already conservative). Observation-based narrowing (classify the proxied reply's `LBL_*`
   label / the parked `codex_approve` decision_id) is a phase-2 refinement, valid only when
   dialog mode is provably off. The global-writer treatment itself is r5's correction of r4: "one approval-capable turn
   at a time" alone still let that turn overlap a PLAIN (`never`-policy) writer at a disjoint
   root, and a mid-turn grant can cover ANY root including that writer's — the grant-holder
   must not overlap any writer, period. r3's conditional funnel (parallelize when dialogs off +
   Q1 passes) stays dead for the second, independent r4 reason: either dialog sentinel is
   resolved FRESH inside `_elicit`, so a `touch` after two approval-capable calls are in flight
   makes BOTH open osascript windows — a dispatch-time check cannot prevent it. **The dominant
   fan-out is UNAFFECTED and still parallel:** `approval_policy: never` calls fan out SUBJECT to
   rules 1–2 (r6 P2: a `never` + `workspace-write`/`danger-full-access` call still serializes on
   its root / globally — policy never bypasses the write locks; the truly unconstrained class is
   `never` + read-only, which IS the swarm pattern) — and the `never` class is established
   STRUCTURALLY, not by observation (r5): `codex_review` exposes
   no approval arg and would default to `on-request` inside the engine, so the FACADE INJECTS
   `approval_policy: "never"` into every `codex_review` dispatch before forwarding
   (`codex_review_v2` does `run_args = dict(args)` — extra keys pass through to `thread/start`
   untouched; zero engine change). Behavioral delta ≈ none — a native `review/start` turn has
   never been observed asking for approval; now it provably cannot. Default `on-request`
   `codex_run` calls serialize AND exclude writers — acceptable for v1 (interactive
   `implement`-style turns are not the swarm pattern; a caller wanting a parallel read-only
   `codex_run` passes `approval_policy: "never"` explicitly). **Phase 2 (post-Q1, touches the
   engine):** a worker-side `flock` around `_elicit` serializes DIALOGS only — it does NOT
   bound the GRANT lifetime (the widened scope outlives the elicitation and the lock; r5), so
   parallelizing approval-capable turns additionally requires grant→reservation propagation:
   the facade already proxies every approval reply (§3.1), so it can parse the granted
   `fileSystem` profile (#272 echo) and WIDEN the turn's reservation at grant time, queueing
   newly-conflicting dispatches from that moment. Both pieces together — flock for dialogs,
   grant-propagation for scopes — are the phase-2 unlock; deferred deliberately.
   **Non-approval elicitation sources** (review r2 P1): `item/tool/requestUserInput` and
   `mcpServer/elicitation/request` are forwarded to CC regardless of `approval_policy`, so even a
   read-only `approval_policy: never` turn can put a CC request in flight (the latter only with
   `mcp` ≠ `isolated`). These are exactly what the Q1 probe measures; if CC mishandles two pending
   requests the facade holds back a second worker→CC request until the first resolves. **The hold
   is bounded** (review r3 P1): the held worker keeps counting its own ~300 s wait while queued, so
   the facade tombstones a request that has waited past a safety bound (well under 300 s) — dropping
   it rather than forwarding a dialog whose reply can no longer satisfy the already-timed-out
   worker wait. **A held request is ALSO associated with its owning `tools/call` and tombstoned on
   EVERY terminal call outcome** (review r4 P2: if the owning call is cancelled mid-hold the worker
   consumes the cancel and returns an interrupted result WITHOUT dying, so a death-only tombstone
   would leave the mapping live and later forward a stale dialog). Rare by construction
   (requestUserInput self-answers `{}`; mcpServer elicitation needs non-isolated `mcp`).

### 3.3 What CC must support (already proven)

- **Concurrent `tools/call` on one connection:** proven — the busy guard fired when two subagent
  calls arrived at one process mid-turn (2026-07-13). CC does NOT serialize per connection.
- **Interleaved server→client requests:** MCP is id-correlated JSON-RPC; CC already answers our
  elicitations by id. **Open question Q1 (§6): two elicitations PENDING at once** — does CC
  render both dialogs (queued or stacked)? Needs a live probe before implementation.

### 3.4 Id spaces

- CC→facade ids (`tools/call`): forwarded to workers **verbatly unchanged** — each worker sees
  only its own calls, and worker responses are matched back via the `cc_call_id → worker` table,
  so no remap is needed in this direction (collisions impossible: CC ids are unique per
  connection and each id goes to exactly one worker).
- Worker→CC ids (elicitation/approvals): REMAPPED through a facade counter (workers all start
  their `_next_bridge_id` at the same values — collisions guaranteed without remap). Each entry
  is ASSOCIATED with its owning `tools/call`; it lives until the reply is routed, and is
  TOMBSTONED on worker death OR on any terminal outcome of the owning call (r5 — this restates
  §3.2's held-request rule so the two lifecycle sections agree; an interrupted owner leaves the
  worker ALIVE, so "reply or worker death" alone would keep a stale mapping forwardable). On
  tombstoning the facade answers/cancels the pending CC request (decline-shaped, so CC never
  leaks a dialog), a held-not-yet-forwarded request is dropped instead of forwarded, and a late
  CC reply hitting a tombstone is swallowed.

### 3.5 Serialization points

- ONE reader on CC stdin (the facade loop; workers never see the real stdin).
- CC-facing stdout writes behind a mutex (frames from N workers + facade interleave whole-frame).
- Worker pipes each get their own reader (thread per worker; the existing `JsonRpcStream`
  reused). The facade is thread-based, not asyncio — matching the codebase's style (threads +
  select already used in the engine).

## 4. Rollout

1. `mcp/codex_facade.py` + tests land behind the EXISTING `.mcp.json` (unchanged — facade not
   yet wired). Dev verification via a temporary local registration in the dev project only.
2. Flip `.mcp.json` `command` to the facade in a follow-up PR after a bake-in dogfood (a week of
   real check/consult/review traffic through a dev registration).
3. Kill switch: `BULLDOZER_FACADE_OFF=1` → the facade `exec`s a single `codex_server.py` in
   place (zero multiplexing, byte-identical legacy path).
4. Rollback = revert the `.mcp.json` line (→ legacy single bridge). The engine carries exactly
   ONE additive change from this feature — the env-gated `worker=N` log field (§3.1/Env), inert
   when `BULLDOZER_WORKER` is unset — so the `.mcp.json` revert alone fully disables the facade;
   the dormant field can be reverted separately but has zero runtime effect while off.

## 5. Test plan

- **Offline unit:** fake workers (scripted subprocess stubs) — dispatch to idle worker; spawn at
  cap; FIFO queue order; QUEUED-call cancel (dequeued, answered interrupted, never executed);
  writable-root overlap queues while disjoint roots parallelize (incl. ancestor/descendant
  paths); same-thread_id calls serialize onto one worker; id remap round-trip; park affinity (approve →
  same worker; wrong token → expired; parked worker not reaped/dispatched) + PIN EXPIRY unpins
  after the mirrored cap; dead-worker pending-elicitation → requester-side cancel + tombstone
  swallows the late reply; crash containment; EOF teardown closes stdin first (graceful path
  observed) with kill as fallback; keep-one-warm reap; facade audit lines written;
  `BULLDOZER_FACADE_OFF` exec path; danger-full-access excludes ALL writers regardless of cwd;
  per-worker `$TMPDIR` isolation; approval-CAPABLE turns (effective policy ≠ `never`) funnel to
  the designated worker AND take the global writer lock (overlap with a disjoint-root
  `never`-policy writer is REFUSED — r5) while `never`-policy READ-ONLY calls fan out (a
  `never` writer still queues by rule 1 — r6); a `codex_review` dispatch gets
  `approval_policy: "never"` injected (asserted on the forwarded frame — r5); a thread that ran
  an approval-capable turn stays global-writer on a later explicit `never` resume (sticky
  widening — r6); a held non-approval elicitation is tombstoned before it
  outlives the worker wait AND on any terminal outcome of its owning call; temp-cwd-owning worker
  not reaped (resume-after-reap covered); park unpins on inner-child death (not just cap);
  parked writable turn HOLDS its root (second writer queues until
  resume completes / cap expires); resume of a facade-known thread applies persisted posture and
  REFRESHES on explicit override, cold resume schedules conservatively (funneled + exclusive);
  thread+approval constraint interaction (wait, then re-evaluate — no deadlock); codex_info always
  answered by the designated worker; engine lines carry worker=N when BULLDOZER_WORKER is set.
- **Regression:** the entire existing `test_codex_mcp_v2.py` suite runs against `codex_server.py`
  with only the additive default-off `worker=N` log field changed (existing assertions hold), plus
  +1 test that the field appears when `BULLDOZER_WORKER` is set. The engine's BEHAVIOR is the
  invariant, not its byte-count.
- **Live e2e (slow, self-skip):** two concurrent `approval_policy: "never"` `codex_run` turns
  through ONE facade → overlap measured, wall ≈ max (the lane-experiment discriminator, now
  inside one server; default `on-request` turns would correctly QUEUE under §3.2(3) — that
  queueing is asserted in the unit tier, not here); a parked approval resumed through the
  facade; Esc mid-turn.
- **Q1 probe** (pre-implementation): two workers elicit simultaneously against a REAL CC session;
  record whether CC queues, stacks, or drops the second dialog — the answer decides the
  bounded-hold behavior for NON-approval elicitations (`requestUserInput` /
  `mcpServer/elicitation`); approval-capable turns serialize in v1 REGARDLESS of the outcome
  (§3.2(3); r5 killed this bullet's last conditional-funnel residue).

## 6. Open questions (resolve during implementation, in this order)

- **Q1 — concurrent elicitations in CC** (§3.3): with phase 1 serializing approval-capable turns,
  the only concurrent-elicitation risk left is the two non-approval sources below on parallel
  `never`-policy calls. Probe still worthwhile before implementation (review r2 — requestUserInput /
  mcpServer elicitation escape the approval funnel, §3.2(3)). Probe: two workers put pending CC
  requests in flight simultaneously; record queue/stack/drop behavior. Also verifies the
  dead-worker path: does CC dismiss a pending dialog on requester-side `notifications/cancelled`
  (§3.4)?
- **Q2 — native dialogs (#340) under concurrency:** RESOLVED — §3.2(3) serializes ALL
  approval-capable turns in phase 1, so at most one worker can ever reach `_elicit`, dialog or not;
  the live-sentinel race (r4 P1) cannot arise. Phase 2 re-enables parallel approval turns only
  with BOTH pieces — the `_elicit` flock (dialogs) AND grant→reservation propagation (scopes);
  the flock alone is insufficient (r5, §3.2(3)).
- **Q3 — `codex_info` cold-cost:** acceptable as specced (rare path); revisit if dogfood shows
  info-calls spawning workers annoyingly often (then: facade-side 60 s cache of info results).

## 7. Sizing

Facade ≈ 600–800 lines + ~700 lines of tests + ONE additive env-gated engine log line (§3.1/Env).
The §3.2 scheduler (posture map, writable/temp roots, approval funnel + global-writer exclusion,
review-dispatch `never` injection) and the park/EOF lifecycle
protocols are the review-pressure zones. Comparable to #340 in effort; LESS risky (new file, kill
switch, engine change is one dormant log field) but the multiplexing tables (id remap, park map,
thread/posture map, queue) are exactly where review pressure should go.

## 7.1 Design-review status

Seven rounds of `codex_review` (xhigh, gpt-5.6-sol) hardened this spec — 34 findings folded in
across scheduler correctness (write isolation, per-thread turns, resume posture, approval
serialization), request lifecycle (queued-call cancel, id remap, held-request tombstones), park
affinity + unpin signals, graceful EOF, and rollback honesty. **Rounds 5–7 were explicit
finding-by-finding VERIFICATION passes on the prior round's fixes:** r5 (on r4: 2 RESOLVED,
3 PARTIAL) forced the global-writer exclusion, the structural `never`-class +
`codex_review` policy injection, and the §3.4/§3.2 tombstone reconciliation; r6 (on r5:
2 RESOLVED, 2 PARTIAL) forced sticky thread widening (session-scope grants outlive the turn;
dialog mode makes grants unobservable), the rules-1–2 qualifier on `never` fan-out, and the
verbatim-forwarding exception in §3.1; **r7 (on r6) returned CLEAN — 3/3 RESOLVED, no new
in-scope contradictions. The loop is closed on a clean verification round**, with the
implementation-time items (exact park-ended signal wiring, temp-cwd ownership mechanism, the Q1
probe result, phase-2 observation details) flagged inline for the implementation session.

## 8. Post-ship cleanup

- CLAUDE.md § Module singleton: lane-pool paragraph → replaced by facade description (lanes
  remain documented as the manual fallback pattern that PROVED per-registration processes).
- workflow-swarms skill § "Codex fan-out": route via plain concurrent `codex_run` calls (one per
  subagent, same tool names everywhere); drop lane routing.
- memory `reference_codex_mcp_vs_cli`: fan-out block updated.
- The `BULLDOZER_LANE` preamble machinery stays (harmless, env-gated) — useful if anyone ever
  runs manual lanes again.
