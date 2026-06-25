# Codex interruptible turns + concurrent wait-loop (#218 + #252) — design

**Status:** design (brainstorming output) — 2026-06-22
**Issues:** #218 (interruptible codex turns), #252 (approval-wait pipe-fill deadlock)
**File:** `mcp/codex_server.py` (codex MCP bridge)

## Goal

When a codex turn is cut off mid-flight — by the user pressing **Esc**, by a Claude Code
tool-call **timeout**, or by our own **opt-in `timeout`** arg — stop the codex turn
**cleanly** (`turn/interrupt`, session stays warm) and return a **partial result + the
`thread_id`** so the caller can resume. Simultaneously fix the #252 approval-wait
deadlock by draining the codex child's stdout during the approval wait. Ship **default-on**
with a coarse **kill-switch** env var.

## Background — empirically verified primitives

All three load-bearing facts were verified live (2026-06-22), not assumed:

1. **CC sends `notifications/cancelled` mid-turn** with `requestId` = the cancelled
   tools/call id. Verified via the #263 stdin probe: a mid-turn Esc on a 58 s codex_run
   produced `notifications/cancelled requestId=4`, buffered during the turn and read at
   `TURN_END` (the serial dispatcher couldn't see it until the turn ended).
2. **codex `turn/interrupt {threadId, turnId}` → `{}`** stops the turn instantly:
   `turn/completed` with `status="interrupted"` arrives immediately, 0 further deltas, and
   a new thread starts in ~1.3 s afterward — **the app-server session stays warm** (no
   cold-start, no process kill). Verified via `/tmp/turn_interrupt_probe.py`.
3. **The "180 s death" is the caller's own opt-in `timeout`**, not a system limit. The
   reporting session passed `timeout: 180` to `codex_run` on a heavy task; our turn loop
   honored it (`"turn timed out after 180 s"`). The schema already says *"Omit for no cap"*;
   there is **no 600 s cap** (omit = unlimited). So this is not a bug — but the cutoff is
   currently a bare error, which #218 makes graceful (partial + resume).

`turnId` is obtained from the `turn/start` response (`TurnStartResponse = {turn: Turn}` →
`turn.id`), confirmed in the app-server schema and the interrupt probe. The capture point
in code is the Phase-1 ACK branch (`kind == "response" and frame.get("id") == mid`); see
§"Interrupt routine" for the `review/start` variant and the pre-ACK window.

## Architecture — one unified concurrent wait-loop

Today the bridge is a **strictly-serial** reader: during a turn it `select`s only on the
codex child's stdout (`Reactor.pump`), and it reads CC stdin only **between** turns (the
main `readline` loop) or **during an approval** (`cc_read_fn`). That serial property is
documented in-code as "the primary concurrency protection."

The change keeps it single-threaded but teaches the two in-turn wait points (the turn pump
AND the approval wait) to `select` on **both** fds and dispatch by frame type. There is
still exactly **one reader of `sys.stdin` at any instant** (the main `readline` loop is
idle while a turn runs), so the serial-concurrency guarantee is preserved.

### stdin read discipline (F4)

The concurrent loop reads CC stdin with the **same mechanism `cc_read_fn` already uses**:
`select([sys.stdin], timeout)` then a **single `sys.stdin.readline()`**. We do NOT switch
to `os.read()` on the raw fd: the in-code contract (codex_server.py header, "dispatcher
holds stdin exclusively for cc_read_fn; MUST use sys.stdin.readline()") exists because
`readline()` keeps its own buffer — mixing `os.read()` with `readline()` would split bytes
across two buffers and silently lose frames. Keeping `readline()` everywhere is the only
buffer-consistent choice.

Consequence and its bound: `readline()` blocks until a newline. We rely on CC writing each
JSON-RPC frame as one line, flushed atomically (the MCP stdio transport does; our own
`cc_write_fn` does the mirror — `write(json.dumps(frame)+"\n"); flush()`). The existing
dispatcher and `cc_read_fn` **already** depend on this exact assumption, so the concurrent
loop inherits it rather than introducing new risk. A partial or pipelined line is the same
rare, unobserved edge the current code already lives with; if it ever bites, the fix is a
shared buffered frame-reader used by `main()`, `cc_read_fn`, AND this loop together — out of
scope here, recorded as the known limitation.

### Mid-turn CC frame dispatch (F3)

A CC frame surfaced mid-turn is routed by **shape**, never silently dropped:

| CC frame | Action |
|---|---|
| `notifications/cancelled` with `params.requestId == active cc_id` | trigger the interrupt routine (unless kill-switch) |
| `notifications/cancelled` for a different `requestId` | ignore (not our turn) |
| any other **notification** (no `id`) | ignore (defensive; nothing else is expected) |
| an **id-bearing request** (`tools/call`, `tools/list`, `ping`, …) | reply immediately: `ping`/`tools/list` answered normally; a second `tools/call` gets `TurnStateMachine.busy_error()` ("codex turn already in flight"); any other gets a minimal JSON-RPC error. **Never leave an id-bearing request unanswered** (CC would block). |
| a frame that is the **pending approval reply** (`id == eid`) | only meaningful inside the approval wait — see §"Approval wait (#252)" |

Classification reuses the existing `classify(frame)` helper (response/request/notification).

**CC-facing envelopes (R3-F2).** The mid-turn router writes the SAME envelopes the main
dispatcher writes, via the existing `reply(id, result=…, error=…)` helper — it does NOT emit
an inner error dict raw:
- `tools/list` → `reply(id, {"tools": TOOLS})`
- `ping` → `reply(id, {})`
- a second `tools/call` → wrap `TurnStateMachine.busy_error()` in the MCP `CallToolResult`
  exactly as `main()` does at the dispatcher (`{"content":[{"type":"text","text": json.dumps(busy)}], "isError": true}`),
  then `reply(id, that)` — **never** the bare `{"error":…}` dict (that helper is only the
  inner `codex_run_v2` shape; CC expects a `CallToolResult`)
- any unsupported method → `reply(id, error=<JSON-RPC error object>)`

### Child frame routing — one shared handler (F1)

Child (app-server) frames must be processed **identically** whether they arrive in the main
turn pump or while we are draining during an approval wait. Today the turn loop's Phase-2
body (delta accumulation into `final_message_parts`, `thread/tokenUsage/updated` →
`usage_snapshot`, the `turn/completed` terminal arm, the `error` arm) is inline in
`codex_run_v2`. The design factors the per-frame body into a **shared handler** that both
wait points call, so a delta or a `turn/completed` that arrives during an approval drain is
**accumulated/acted-on, not discarded**. The handler returns a sentinel telling the caller
whether the turn terminated (so the approval wait can unwind correctly if codex completes
mid-approval — unusual but possible). Accumulator state (`final_message_parts`,
`usage_snapshot`, `narrative_shown`, `interrupting`) lives in the turn frame so both wait
points share one source of truth.

### Three interrupt triggers — one mechanism

| Trigger | Source | Detection |
|---|---|---|
| User Esc | CC → `notifications/cancelled` mid-turn | concurrent stdin read in the turn pump / approval wait |
| CC tool-call timeout | CC → `notifications/cancelled` (IF — see Open Questions) OR stdin EOF | same path; EOF → teardown branch |
| Our opt-in `timeout` | our turn-loop deadline (`args["timeout"]`) | existing deadline check |

All three converge on the same **interrupt routine** (next section).

## Components (in `mcp/codex_server.py`)

1. **`Reactor.pump` extension.** A `watch_cc: bool` param (**default `False` — child-only,
   byte-identical to today**) adds `sys.stdin` to the `select` set and returns, alongside
   child frames, any CC frame read (tagged so the caller can tell them apart). **Only the
   active-turn pump and the approval wait pass `watch_cc=True`** — and only they own a
   CC-frame router (§"Mid-turn CC frame dispatch"). Every OTHER caller of the reactor —
   `_pump_until`, `connection_request`, and the `ensure`/`start_thread`/`resume_thread`
   setup waits — keeps the default and so NEVER reads CC stdin (R2-F1: a setup-phase reader
   with no router would silently swallow a `notifications/cancelled` or an id-bearing CC
   request). When the kill-switch is set, even the turn pump passes `watch_cc=False`.

2. **Turn pump (`codex_run_v2` turn loop).** Calls the extended pump; child frames go to the
   shared handler (§F1), CC frames go to the mid-turn dispatch (§F3). A
   `notifications/cancelled` whose `requestId == cc_id` (the active tools/call id, already
   threaded in as `_cc_id`) triggers interrupt. The existing opt-in `timeout` deadline
   triggers the **same** interrupt routine instead of returning a bare error.

3. **Interrupt routine.** Inputs: the captured `turnId`, `thread_id`, the accumulated
   `partial_text`/`usage`, and `interrupted_by` ∈ {`cancel`, `timeout`}.

   - **Normal path.** Set the in-flight `interrupting` flag, send
     `turn/interrupt {threadId, turnId}`, then pump (child-only is fine here) until the
     terminating `turn/completed`. Build the graceful per-mode result (§"Return contract").
   - **Terminal-arm interaction (must not regress).** The turn pump already owns a
     terminal-failure arm — `if t.get("status") != "completed" or t.get("error")` (codex
     0.141 `TurnStatus` ∈ {completed, interrupted, failed, inProgress}) — which maps any
     non-`completed` `turn/completed` to `{"error": "turn failed: status=…"}`. An interrupt
     we initiate produces exactly `status="interrupted"`; a truthy `interrupting` flag
     routes that `turn/completed` to the graceful result, bypassing the terminal arm. A
     `status="interrupted"` arriving WITHOUT our flag (codex self-interrupted — not expected
     on 0.141) still falls through to the existing terminal arm unchanged, so non-interrupted
     turns are byte-identical to today.
   - **Pre-turn windows where no `turnId` exists (F5).** Two sub-windows, both handled
     without fabricating a `turnId`:
     - **Setup window — `ensure`/`start_thread`/`resume_thread` (the cold start, ≤180 s).**
       These run BEFORE the turn loop and use child-only `_pump_until` (`watch_cc=False`, per
       R2-F1 — a setup reader with no router must not consume CC frames). A cancel arriving
       here is therefore not read mid-setup; CC's `notifications/cancelled` stays buffered on
       stdin and is consumed by the turn loop's **first** `watch_cc=True` pump immediately
       after setup completes, which then runs the interrupt routine. So the Esc IS honored,
       with latency bounded by the remaining cold-start — and a hung cold-start is itself
       bounded by `start_thread`'s existing 180 s `_pump_until` (→ a normal setup-timeout
       error result). No CC frame is dropped: it is deferred, not lost.
     - **Post-`turn/start`, pre-ACK window.** Once the turn loop is running (`watch_cc=True`)
       but the ACK has not arrived, a cancel/timeout sets a `cancel_pending` flag; the loop
       keeps pumping for the ACK, captures `turnId`, and immediately runs the interrupt path
       (returning `partial_text=""`). If the ACK never arrives (`ack_deadline` expires, or
       stdin EOF) we tear down — kill the child so it respawns clean next call (the #227b
       transactional respawn handles the restart) — and return the graceful interrupted
       result with an empty partial. (The opt-in `timeout` is not armed until the turn loop,
       so pre-ACK timeout is the rare case, routed here too.)
   - **`review/start` variant (F5 / Open-Q2).** `codex_review` starts the turn via
     `review/start`, not `turn/start`. The `turnId` is captured from whichever start
     response is the ACK for our `mid` (the capture branch is shared). The impl MUST verify
     the `review/start` response carries a resolvable turn id; if a particular codex build
     does not surface one, the review path falls back to the teardown branch (kill +
     graceful partial) rather than sending a malformed `turn/interrupt`.
   - **Completion-timeout fallback (F6).** The post-interrupt wait for `turn/completed` is
     **bounded** (a small constant, e.g. `_INTERRUPT_COMPLETE_TIMEOUT = 10 s`; the probe
     showed it is effectively immediate). If it does NOT arrive: tear down the child (kill →
     respawn next call) and STILL return the graceful interrupted result built from the
     partial already accumulated (per the teardown invariant below).
   - **Teardown invariant — applies to EVERY kill-the-child branch (R3-F1).** Three branches
     drop the child before a normal `turn/completed`: the F6 completion timeout, the pre-ACK
     no-ACK/EOF teardown, and the `review/start` missing-turn-id fallback. Each MUST both
     (a) call `state_machine.turn_completed()` to clear the in-flight busy state — the turn is
     marked busy at `turn_started` BEFORE the ACK, so skipping this wedges the NEXT call into
     `busy_error` (the existing ack-timeout path already clears it; the new branches must
     match) — and (b) return `thread_warm: false` (the child is gone → resume needs a fresh
     cold-start). The normal warm interrupt path (child survives) keeps `thread_warm: true`.
     A hung interrupt therefore can never wedge the bridge.

4. **Approval wait (#252).** `read_correlated` / `bridge_approval` currently read CC stdin
   **only** (confirmed: `cc_read_fn` in the wait, no child drain) → a child flooding stdout
   while we block fills its pipe and deadlocks. The fix makes the approval wait use the same
   unified `select`:
   - **Drain ownership (F1).** Child frames read during the wait go through the **shared
     child-frame handler**, not `/dev/null` — deltas accumulate, `usage` updates, and a
     `turn/completed` arriving mid-approval is acted on (returns up through the turn loop).
     Nothing is dropped.
   - **Cancel during approval (F2).** A `notifications/cancelled` (our `cc_id`) arriving
     while an app-server request is outstanding must FIRST write a **method-valid** response
     to that exact request, THEN run the interrupt routine (order matters — interrupt-
     without-reply can strand the app-server on the unanswered request). The response shape
     differs per method, so we do NOT invent a generic `"decline"`; instead we **reuse the
     decline payload `bridge_approval` already emits** on a CC decline/timeout (its
     `read_correlated`-returns-`None` branch) for that method — verified per-method against
     the current bridge:
     - `item/commandExecution/requestApproval` → `"decline"`
     - `item/fileChange/requestApproval` → `"decline"`
     - `item/permissions/requestApproval` → `PERM_DECLINE` (`{"permissions":{}, "scope":"turn"}`)
     - `item/tool/requestUserInput` → `{"answers": {}}`
     - `mcpServer/elicitation/request` → `{"action":"cancel", "content":null, "_meta":null}`
     - legacy `execCommandApproval` / `applyPatchApproval` → the ReviewDecision deny value

     Mechanically: the unified approval wait, on seeing our cancel, unwinds through
     `bridge_approval`'s existing per-method `None`-branch (so the caller writes the correct
     `{id, result: <payload>}` via `manager._write`, exactly as the happy path does at the
     `kind=="request"` branch) and sets `cancel_pending`; the turn loop then runs the
     interrupt routine. Zero new per-method shapes — the per-method correctness is inherited
     from the already-tested decline paths.
   - The approval reply itself (`id == eid`, a `response`) resolves the waiter exactly as
     today.
   - **#252 is independent of #218.** The approval-wait child-drain is a deadlock fix and is
     **always on**, even when the interrupt kill-switch is set (see matrix).

5. **Kill-switch — `BULLDOZER_CODEX_NO_INTERRUPT` (F8).** Scopes to **interrupt behavior
   only**, never to the #252 drain:

   | Concern | kill-switch UNSET (default) | kill-switch SET |
   |---|---|---|
   | #252 approval-wait child-drain | on | **on** (deadlock fix, always) |
   | approval reply / cancel-during-approval **interrupt** | interrupt on cancel | drain only, cancel ignored (legacy: skip frame) |
   | main turn-pump watches CC stdin for cancel | yes → interrupt | no (legacy serial; cancel surfaces at TURN_END) |
   | opt-in `timeout` | graceful interrupt + partial | legacy bare `{"error": "turn timed out…"}` |

   Logged once when the switch is set, to the stable `~/.claude/hooks/bulldozer-codex.log`.
   Coarse and boring, per the consult verdict (gpt-5.5, 2026-06-22: "default-on, but require
   a kill-switch").

## Return contract (on any interrupt) (F7)

The interrupted result is the **same mode-shaped result a completed turn returns**, built
from the partial via the existing `_shape_result(mode, thread_id, partial_text, meta)`, plus
interrupt metadata — and crucially with **no `"error"` key**, so the dispatcher's
`if "error" in res: isError=True` does NOT mark it an error (an interrupt is a graceful
partial, not a failure):

```jsonc
// mode=review  → existing {verdict, findings, schema_ok, …} on partial_text
// mode=implement → existing {result: partial_text, …}
// codex_review (review/start) → existing {review: partial_text, …}
// PLUS, on every interrupted result:
{
  "thread_id": "<id>",
  "status": "interrupted",
  "interrupted_by": "cancel" | "timeout",
  "partial_text": "<agentMessage deltas collected before the interrupt, if any>",
  "thread_warm": true,           // false on ANY teardown branch (F6 timeout / pre-ACK no-ACK / review-start missing turn-id) — resume needs cold-start
  "usage": { ... },              // via the existing _build_result_meta
  "timing": { "duration_ms": ... }
}
```

- `partial_text` — agentMessage deltas accumulated during the turn (Choice A: include
  partial). Empty string if nothing was produced yet.
- `status` is the additive-meta `status` field too (a third value alongside
  `completed`/`failed`), so `_build_result_meta(..., "interrupted")` is the single place it
  is set.
- `thread_id` — caller resumes with `codex_run(thread_id=…)` (resume already exists; the
  interrupt keeps the thread warm — verified — unless `thread_warm:false`).
- For the opt-in-timeout case the result also hints: omit `timeout` for no cap, or resume.

## Testing (TDD, per house discipline)

Offline (FakeChild simulating the app-server wire), each RED→GREEN:

- mid-turn `notifications/cancelled(requestId==turn)` → `turn/interrupt` sent with the
  captured `turnId`; result `status=interrupted`, `partial_text` carries collected deltas,
  **`isError` is NOT set**.
- opt-in `timeout` elapses → same interrupt routine → graceful result (not a bare error).
- a cancel for a non-active `requestId` → ignored (no interrupt).
- **mid-turn id-bearing CC request (F3):** a `tools/call` mid-turn gets the busy error; a
  `ping` gets a response; neither is dropped.
- **pre-turn cancel (F5):** cancel during the setup `_pump_until` (cold start) stays buffered
  and the turn loop's first `watch_cc` pump runs the interrupt right after setup; cancel after
  `turn/start` but before the ACK → `cancel_pending` → interrupt once `turnId` is captured;
  no-ACK-ever (EOF) → teardown + graceful partial.
- **Reactor default child-only (R2-F1):** `Reactor.pump()` with no `watch_cc` arg never reads
  CC stdin; a `_pump_until` / `connection_request` setup wait does NOT consume a buffered
  `notifications/cancelled` or id-bearing CC request (it stays for the turn loop).
- **interrupt-completion timeout (F6):** `turn/interrupt` sent but no `turn/completed` within
  the bound → child torn down, result still graceful with `thread_warm:false`, bridge state
  cleared (next call works).
- **#252 drain ownership (F1):** child emits deltas AND a large stdout burst during an
  approval wait → no deadlock, approval reply still delivered, the drained deltas appear in
  the final/partial text (not lost).
- **cancel during approval (F2):** for EACH bridged method (command / fileChange /
  permissions / tool-input / mcpServer elicitation / legacy), a cancel mid-approval writes
  that method's existing decline payload to the app-server BEFORE `turn/interrupt`.
- **kill-switch (F8):** set → no cancel-interrupt and opt-in timeout returns the legacy bare
  error, **but the #252 approval drain still runs** (no deadlock).
- **per-mode interrupted shape (F7):** review / implement / codex_review interrupted results
  each carry their mode's keys plus the interrupt metadata.
- **teardown invariant (R3-F1):** the pre-ACK no-ACK/EOF teardown AND the `review/start`
  missing-turn-id fallback each clear `TurnStateMachine` (a follow-up call is NOT busy) and
  return `thread_warm:false`; the warm interrupt path keeps `thread_warm:true`.
- **mid-turn envelopes (R3-F2):** a mid-turn second `tools/call` gets a `CallToolResult`
  (`content` + `isError:true`), `tools/list` gets `{tools}`, `ping` gets `{}`, an unsupported
  method gets a JSON-RPC error — none is answered with a bare `{"error":…}` dict.
- happy path unchanged: a normal turn with no interruption is byte-identical to today
  (`watch_cc=False` path + no `interrupting` flag).

Slow real-codex e2e (`-m slow`, self-skip without codex): start a real turn, send
`turn/interrupt`, assert `status=interrupted` + session warm afterward (mirrors the probe);
and a real opt-in-timeout that returns partial + a resumable `thread_id`.

Mandatory: full `pytest tests/test_codex_mcp_v2.py` (incl slow) after any change.

## Rollout

Default-on. Kill-switch `BULLDOZER_CODEX_NO_INTERRUPT` + the new return-contract fields
documented in CLAUDE.md ("Architecture: codex MCP server"). Triangulated dogfood
(codex_review + 2 read-only reviewers) before merge, as usual.

## Open questions (to resolve during implementation, not blockers)

1. **Does CC's tool-call *timeout* path also send `notifications/cancelled`?** Esc does
   (verified); the timeout wire-behavior is undocumented (claude-code-guide, 2026-06-22).
   If it sends cancel, trigger #2 is free (same path). If it instead closes stdin (EOF), the
   turn pump sees EOF → the teardown branch (kill + graceful partial). The design handles
   both; a short follow-up probe (lower CC's `MCP_TOOL_TIMEOUT`, run a long turn, watch the
   probe log) can confirm which, but is not a blocker.
2. **`turn/interrupt` while an approval is outstanding** — F2 writes the decline first, then
   interrupts. Confirm at impl that codex accepts `turn/interrupt` cleanly after a declined
   approval (the probe interrupted a plain streaming turn; the approval-pending variant
   should be exercised by the F2 slow-e2e). If codex needs the turn to advance past the
   decline before it honors the interrupt, the shared handler's drain covers the gap.
3. **Optional usage-routing note** in `SERVER_INSTRUCTIONS` (#256 manifest): low-priority,
   debatable scope — not part of this change.

## Out of scope

- Raising/clamping the opt-in `timeout` value (no cap exists; omit = unlimited — already
  correct).
- `turn/steer` (mid-turn input injection) — separate capability, not needed here.
- Changing the default MCP tool-call timeout (a CC/consumer config concern, not ours).
- A shared buffered (`os.read`) stdin frame-reader (F4 robustness) — recorded as the known
  limitation; the current `readline` discipline is retained to stay buffer-consistent.
