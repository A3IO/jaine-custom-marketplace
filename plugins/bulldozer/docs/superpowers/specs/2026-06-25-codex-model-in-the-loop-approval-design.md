# codex MCP — model-in-the-loop approval (return-and-resume) — design spec

**Status:** design spec, pre-implementation, **CONVERGED**. This is the consolidated single source of
truth: it folds the original design + all review corrections (panel `/consult` ×2, `/bulldozer:check`
4 rounds — C1-C17, then a config + holistic pass) into coherent prose. Supersedes the layered draft in
git history and the Tier-1-allow-list-only `2026-06-24-codex-unattended-approval-policy-design.md`.
Feasibility PROVEN by `docs/superpowers/probes/2026-06-24-return-and-resume-feasibility-probe.py`.
Implementation plan: `docs/superpowers/plans/2026-06-25-codex-model-in-the-loop-approval.md`.
(Code references are by SYMBOL, not line number, on purpose — line numbers drift.)

**Decision:** when codex needs an approval and unattended mode is armed, a tiny fast-path auto-accepts
trivially-safe commands and **everything else is RETURNED to the orchestrating session model**, which
decides accept/decline with full task context, then resumes the parked codex turn. Not a regex, not a
separate judge model — the session model that already owns the task.

---

## 1. Why this shape

**The deterministic regex allow-list does not converge.** A Tier-1 shell allow-list was hardened over
four adversarial rounds; each round surfaced another side-channel the accept-path mis-accepted
(`env`-wrapper bypass, runner quote-loss, `rm -rf .`/`/proj/*`, `xargs … -delete`, `awk system()`,
`sed e`/`w`, nested `$(…)`, `git --work-tree=/tmp`, `~user`, process-sub `<(…)`). Parsing arbitrary
shell to *safely-accept* complex commands has an unbounded tail. The sound altitude: **stop trying to
safely-accept complexity — route it to a judge that has context.**

**The session model is the only judge that can be reached.** Verified (claude-code-guide, 2026-06-25):
Claude Code does **not** implement MCP `sampling/createMessage` (issues #1785/#31893 open, not a flag).
The ONLY server→client transport that reaches the *session* model is **returning a value from the
tool call** — elicitation routes to a human dialog, notifications don't trigger inference. So
model-in-the-loop ⟺ return-and-resume. (An inline separate-model judge was considered and rejected:
it lacks the session's task context and adds an auth/cost surface for no gain.)

## 2. Threat model — LOCAL single-user tool

This MCP server is a **local, single-user developer tool**. codex runs the user's own tasks on the
user's machine. There is **no adversary** who controls codex's output. Consequently:

- **Prompt-injection of the judging model is OUT OF SCOPE.** The approval payload carries codex's
  narrative/diff as evidence; in a multi-tenant product that would be an untrusted-input attack
  surface, but here there is no attacker to inject it. We do NOT add untrusted-input framing.
- The remaining concerns are **correctness and operational**, not security: don't auto-run something
  destructive by accident, don't strand a parked turn, don't flood the model with round-trips.

## 3. Architecture — two parts, three routes

| Mode | Who decides | Mechanism |
|---|---|---|
| Attended (default, unarmed) | Human | `elicitation/create` dialog — **today's behaviour, byte-identical** |
| Unattended + trivial command | Fast-path (Part A) | inline accept, no model round-trip |
| Unattended + anything else | **The session model** | return `{awaiting_approval}` → `codex_approve` resume (Part B) |

The whole feature is gated by `_unattended_active()` (env `BULLDOZER_APPROVAL_UNATTENDED` truthy OR a
sentinel file, resolved FRESH per approval). **Default OFF → the attended path is unchanged.**

## 4. Part A — the routing fast-path (replaces the verdict judge)

The unattended judge stops returning an accept/decline **verdict** and returns a **3-way ROUTING
decision** — `route_approval(method, params, project_root) -> "fast_accept" | "park_for_model" |
"fail_closed_decline"`. (The old `classify_approval` verdict is WRONG for this feature: it
auto-DECLINES permissions/escalation and auto-ACCEPTS *every* fileChange — both should PARK now.)
**`route_approval` must normalize `params` to a dict FIRST** (`params = params if isinstance(params,
dict) else {}`) before any `.get()`/helper call — `handle_server_request` preserves a truthy non-dict
`params`, so an unnormalized helper call (`_has_escalation_amendment`) would raise instead of returning
`fail_closed_decline` for an unrepresentable request.

- **`fast_accept`** (inline accept built IN the loop body via `build_decision_response` — no model
  round-trip, no `bridge_approval`; §5.1) — ONLY a single trivial command (or a pipe of them)
  whose verb is a plain read (`cat ls pwd grep rg nl head tail wc stat …`) or an in-project simple
  write (`mkdir`, `touch`, `rm <in-project-subpath>`), via `_is_trivially_safe(command, project_root)`.
  That predicate declines as "not trivial" on ANY shell-complexity marker: command substitution
  `$(…)`/backticks, **variable expansion `$VAR`/`${VAR}`** (a `$FOO` could expand to anything — treat
  as complexity, park it — this is correctness, not a vuln), process substitution `<(…)`/`>(…)`, a
  redirect outside cwd, `~`anything, or a root-level glob.
- **`park_for_model`** (Part B) — EVERYTHING else: any complexity, any non-trivial verb, AND every
  `permissions`, structured-escalation, `fileChange`, `applyPatch`, and the legacy
  `execCommandApproval`/`applyPatchApproval`. These neither auto-decline nor auto-accept.
- **`fail_closed_decline`** — ONLY a malformed/unrepresentable request (can't even build the payload).

**Delete the whack-a-mole gates** — `_command_substitutions`, `_substitution_is_dangerous`,
`_SED_EXEC_WRITE`, the `awk system()` gate, `git --work-tree`/subcommand parsing, `_rm_target_too_broad`,
`_xargs_command_index` recursion, `_drop_leading_words`. Those cases are no longer analyzed; they park.
Keep only the simple tokenizing (`_safe_tokens`, `_strip_cmd_prefixes`, `_split_segments`) the
fast-path predicate needs, plus the escalation hard-floor.

**`route_approval` is consulted ONLY for the five APPROVAL methods** (`commandExecution`/`fileChange`/
`permissions` requestApproval + legacy `execCommandApproval`/`applyPatchApproval`). The other bridged
server-requests — `item/tool/requestUserInput`, `mcpServer/elicitation/request` — **bypass routing
entirely** and keep their exact current human-dialog path. The model judges *approvals*, not arbitrary
input prompts. (C4)

**Aggressiveness is a tunable knob.** Common workflows (`npm install`, `cargo build`, test suites, git)
are non-trivial, so a strict fast-path parks them → a model round-trip each. That is "the feature," but
the round-trip volume is tunable via `_fast_path_scope()` (§8): `"reads"` (pure reads + trivial writes
only) vs `"local-work"` (adds a SMALL set of PLAIN bare-verb forms — `pytest`, `make`, `npm test`,
`cargo build` — with no options-bearing escapes). **`local-work` must NOT re-introduce the deleted git
subcommand / `--work-tree`/`-C` / wrapper parsing (R2-F2):** anything with global git options, a
runner wrapper (`sh -c`, `env`, `xargs`, `timeout`), or any §4 complexity marker still PARKS. If a
form can't be accepted by a trivial bare-verb match, it parks — `local-work` never parses to
safely-accept (that was the non-convergent trap). Git writes are NOT in `local-work` v1.

## 5. Part B — return-and-resume

### 5.1 The mechanic: an INNER generator (C1)

`codex_run_v2` MUST stay a dict-returning function — `codex_review_v2` calls it and `json.dumps`-es the
result, so a bare `yield` in `codex_run_v2` would hand callers a generator object and crash them.
Instead the turn-pump loop body becomes an **inner generator** `_drive_turn(ctx)` that `codex_run_v2`
creates and drives (`next`/`send`):

- At a `park_for_model` approval point the generator does `decision_id = yield build_awaiting_payload(...)`.
  **The escape boundary from the synchronous write path is concrete (R3-F1):** the decision is taken in
  the turn-pump loop body's `if kind == "request"` block — the only place `yield` is syntactically legal,
  and today the sole call site of `manager._write(handle_server_request(frame, …))`. That block consults
  `route_approval` BEFORE writing; on `park_for_model` it BYPASSES `handle_server_request`/`bridge_approval`
  entirely (no synchronous child reply) and yields. A **`fast_accept`** is likewise handled IN the loop
  body — `manager._write(build_decision_response(frame, <the method's plain accept>))` — WITHOUT entering
  `bridge_approval` (whose unattended inline-decision branch is deleted together with
  `classify_approval`/`_unattended_decision`, §9). ONLY an attended (unarmed) approval or a non-approval
  request keeps today's `manager._write(handle_server_request(...))` path. So the synchronous write helpers
  are NEVER entered for the park OR the unattended `fast_accept` case — no nested function ever needs to
  (impossibly) `yield`, and no deleted judge is referenced. The review path never yields — read-only, no
  approvals, so `codex_review_v2` is unaffected.
- On a yielded payload → `codex_run_v2` stores `manager._parked = {park_token, thread_id, inner_gen,
  isolation_sig, started_at, request_frame, decision_ids}`, calls `tsm.park(...)`, and RETURNS the
  payload (a dict). `decision_ids` = the set of `approval.decisions[].id` (+ `"decline"`) — the SOURCE
  `codex_approve_v2` step-2 pre-validates against, so the unknown-id check happens BEFORE `gen.send`
  (R1-F3). The decision→response mapping is built inside the generator from `request_frame`;
  `build_decision_response` therefore receives only an ALREADY-VALID id (no unknown-id branch except
  defensive-unreachable).
- On `StopIteration(result)` → `tsm.turn_completed()` and RETURN the final dict.

**The extraction is an explicit ctx-attribute REWRITE, not a verbatim move (C-extract).** The loop
mutates locals (`deadline += _elapsed`, `cancel_pending = True`, `turn_id`, `turn_acked`,
`narrative_shown`); a move-only generator raises `UnboundLocalError`. Every mutated local becomes
`ctx.<attr>`. `ctx` carries the full set the loop reads (`manager, reactor, ts, mid, start_method,
turn_params, deadline, ack_deadline, watch, mode, review_target, thread_id, meta, cc_write_fn,
cc_read_fn, args/_cc_id, acc, state_machine, turn_timeout, narrative_shown, turn_acked, turn_id,
cancel_pending`) and re-reads `manager._child`/`manager._reactor` LIVE on resume (never a stale local).
The #218/#252 interrupt/EOF/cancel/drain logic stays inside the loop body, unchanged.

The dispatcher `main()` is serial and idles between calls, so the suspended generator + warm child
survive the gap with no new plumbing — the probe proved the child tolerates ≥93 s idle, accepts a late
decision, and completes (codex 0.141/0.142).

### 5.2 The `codex_approve` tool (C9, C15)

Resume is a **dedicated first-class MCP tool**, NOT an overload of `codex_run` (which requires
`prompt`+`mcp` and treats `thread_id` as a real app-server `thread/resume`). Add to the static `TOOLS`
list so `tools/list` advertises it: `codex_approve` with inputSchema `{park_token: str (required),
decision_id: str (required)}`, NO `prompt`/`mcp`. `main()` dispatches `codex_approve` by name BEFORE
the `codex_run` fallback and BEFORE any validation/`ensure()`.

`codex_approve_v2` is THIN:
1. Validate `park_token == manager._parked["park_token"]`; mismatch/absent/already-used →
   `{"error": "parked turn expired"}` fail-closed, park UNCHANGED (this is the double-resume guard).
2. Validate `decision_id` is one of the parked payload's `decisions[].id` (or `"decline"`) **BEFORE
   advancing the generator** — an unknown id → a RETRYABLE MCP error (`isError`), park UNCHANGED, NO
   `gen.send`, NO child write, so a hallucinated id never consumes the park and the SAME token retries.
3. `gen.send(decision_id)` to resume.
4. If the generator yields AGAIN (multi-approval turn) → **RE-PARK** (re-store `manager._parked` +
   `tsm.park`) and return the new payload. If `StopIteration` → `tsm.turn_completed()` (clears
   `_in_flight` AND `_parked` — NOT `unpark()` alone, which would leave `_in_flight` True and deadlock
   the next tool) + return the final dict.

All the heavy resume work lives INSIDE the generator (it owns `ts`, the deadlines, the request frame):
- **DRAIN buffered child frames first (C2)** — `ts["drained_frames"]` + a fresh `reactor.pump` — and
  check a drained TERMINAL/EOF BEFORE writing the decision (else a child that died during the park
  loses its terminal frame). The park is feasible *because* the blocked child shouldn't emit (probe),
  but drain-on-resume is the safety belt that preserves the #252 contract.
- **Credit the park duration (C5)** back to `ctx.deadline`/`ctx.ack_deadline` (the inline path already
  credits an approval's `_elapsed`; the park is the same idea over a longer gap). Use `time.monotonic`
  for these deadlines so a wall-clock shift can't trip them.
- **Rebind the active `_cc_id` (C6)** to the resume call's id, so an Esc during the resume leg is
  matched (`notifications/cancelled.requestId`).
- **Build the codex reply via `build_decision_response(parked_request_frame, decision_id)` (C10)** —
  see 5.4 — and write it, **guarded for `BrokenPipeError`** (a child that died mid-park) → a graceful
  declined/teardown result, never a crash (C8b).

### 5.3 The awaiting payload — per-kind evidence (C13, C16)

`build_awaiting_payload(method, params, ts, narrative, park_token)` returns
`{status:"awaiting_approval", park_token, approval:{kind, …evidence…, decisions:[{id,label}]}, thread_id}`.
The `decisions[]` are BOUNDED options with **opaque ids** derived from `build_command_approval_labels`
(so the model picks an id; the response builder maps it back). Evidence per kind:

- `commandExecution` / `execCommandApproval` → `command`, `cwd`, `reason`, `narrative`.
- `permissions` → the bounded raw `params["permissions"]` profile + `_summarize_permissions(...)` +
  `cwd` + `environmentId` + turn/session scope `decisions[]` (these fields are on the request).
- legacy `applyPatchApproval` → the bounded `params["fileChanges"]` (on the request).
- modern `item/fileChange/requestApproval` → the diff is NOT on the request. It streams as
  **`item/fileChange/patchUpdated`** (+ incremental `item/fileChange/outputDelta`) — NOT `item/completed`
  (which `_handle_child_frame` captures only in its `review_target` branch). **Fix:** extend
  `_handle_child_frame` to accumulate those two events into `ts["file_changes"][itemId]` (a patch
  buffer keyed by `itemId`/`turnId`); the payload builder attaches the assembled diff for the parking
  `itemId`. (`item/fileChange/*` are already in `_KNOWN_NOTIFICATIONS`.)
  - **No-patch case (creations/deletions/binary):** a file create/delete or binary replace may emit no
    text patch. Do NOT blindly fail-closed and permanently auto-decline a legitimate op. **R2-F4 —
    PROVE the field names first:** the modern `item/fileChange/requestApproval` request models only
    `threadId`/`turnId`/`itemId`/`startedAtMs` and today's bridge uses only `reason` — there is NO
    confirmed path/op field on the request. So the implementer MUST verify the real field set against
    the protocol schema (`codex app-server generate-json-schema`) or a live probe BEFORE the payload
    builder relies on it. Order of evidence: (1) the accumulated `ts["file_changes"][itemId]` patch
    buffer (captured via the patch events above — present even for many creates/deletes); (2) only the
    fields the probe CONFIRMS exist; (3) fail-closed decline ONLY when neither yields anything. Add a
    test using the REAL no-patch request shape, not an assumed one.

### 5.4 The decision-response builder (C10)

`build_decision_response(parked_request_frame, decision_id)` emits the EXACT jsonrpc-lite reply per
method, reusing the ORIGINAL request id (`msg.get("id")`):
- command → map the chosen label back via the `build_command_approval_labels` reverse-map (preserve
  `availableDecisions`).
- permissions accept → echo the requested `RequestPermissionProfile` + scope (the #272 grant-echo).
- legacy `execCommandApproval`/`applyPatchApproval` → the review-decision shape.
- `decision_id == "decline"` → the method's safe decline.
- **An UNKNOWN `decision_id`** (model hallucinated/malformed it) is caught by the §5.2 step-2
  pre-validation BEFORE `gen.send` (so the park is never consumed) → a RETRYABLE MCP error
  (`isError: true`, "unknown decision_id …"), park UNCHANGED, so the model corrects and retries the
  SAME token — do NOT advance the generator, do NOT permanent-decline/abort the codex action.

## 6. Parked-turn state, single policy & teardown (C3, C7, C8, C12, C14)

**`TurnStateMachine` gets a distinct PARKED state** (today it is a bare boolean `_in_flight`). Holds
the park token; `is_busy()` stays True while parked; `busy_error` distinguishes parked from ordinary
in-flight.

**ONE policy (C14) while a turn is parked.** Dispatch order matters: `main()` routes a `codex_approve`
call **by TOOL NAME** to `codex_approve_v2` FIRST — the token is validated THERE (§5.2 step 1), so a
WRONG/stale token returns `parked turn expired` (park preserved), NOT the busy path. The parked guard
("matching" = by name, not by token) busy-blocks only NON-approve tools. So:
- `codex_approve`, valid token → resume; wrong/stale/double token → `parked turn expired`, park UNCHANGED;
- **any OTHER AppServerManager-touching tool** (fresh `codex_run` / `codex_info` / `codex_review`) →
  **BUSY, park PRESERVED** — an accidental call NEVER discards a live park. This needs a **global
  parked guard in `main()` BEFORE dispatching any NON-approve tool** (C12): `codex_info` is otherwise routed
  straight to `codex_info_v2` around the state machine, and its `connection_request` would write to +
  drain the parked child, stealing frames the generator needs.
- **TEARDOWN** (= `_teardown_park`) fires ONLY on the genuine end-states: the wall-clock **cap**, CC
  **EOF**, an our-turn **cancel** (matched against the parked turn's `_pending_cc_id` — an unrelated
  cancel must NOT tear it down), or a deliberate **isolation-signature respawn**.

**`_teardown_park` ordering matters (C7, C8):** the parked child is BLOCKED awaiting the approval reply,
so `turn/interrupt` alone is useless — first **auto-decline the pending approval** (unblocks the
child), THEN `turn/interrupt`/kill if needed, THEN clear state. It must call
`TurnStateMachine.turn_completed()` (not merely `unpark()`), or `_in_flight` stays True forever and
deadlocks all future tools. A respawn (`ensure()` signature change, e.g. a `codex_info` with a
different `mcp`) while parked must refuse-or-teardown, never silently strand the captured child/reactor.

## 7. The wall-clock cap (C17)

`main()` blocks on `next_frame(None)` between calls, so a timer cannot fire while idle. **Fix:** while a
turn is parked, `main()` runs a parked wait with a FINITE `cap_remaining` budget (recomputed from
`monotonic` each wake, so a stray unrelated MCP message can't reset the cap). **The parked wait must
watch the CHILD too, not just CC stdin (R2-F1):** `CCStream.next_frame` selects ONLY on `sys.stdin`
(the CC fd) — it is BLIND to child stdout/terminal/death. So each parked-wait iteration must ALSO
`reactor.pump(timeout=0.0)` (route a terminal child frame → teardown) and check `manager._child.poll()`
— otherwise a codex that dies/`turn/completed`s while parked is invisible until the cap or a later
resume (mislabelled as a timeout). Iterate on a short slice (e.g. ≤0.2 s) until `cap_remaining`
exhausts. On cap with no resume → `_teardown_park("cap")` (auto-decline first, per §6). **Priority
order each iteration:** CC EOF / child-death / a terminal child frame WIN over the cap. A `codex_approve`
after the cap → `{"error":"parked turn expired"}`. The cap-teardown auto-decline write is
`BrokenPipeError`-guarded — `main()`'s parked wait has no tool-call try/except, so an unguarded write to
a dead child crashes the server.

## 8. Configuration — env accessors, fresh-per-call, NO config file

Knobs follow the existing scattered-env + tiny-accessor idiom (the elegant choice for a local tool with
scalar knobs — a TOML/JSON config file adds parsing/failure/test surface for no gain; consult panel
2026-06-25, Grok's call, GPT/Gemini's TOML rejected as over-engineering here):

- **Master toggle** — `_unattended_active()` UNCHANGED (env `BULLDOZER_APPROVAL_UNATTENDED` OR sentinel
  file, read FRESH per approval so the user can arm/disarm mid-run).
- **Cap** — `_park_cap_s()` → `float(os.environ.get("BULLDOZER_PARK_CAP_S") or _PARK_CAP_S_DEFAULT)`
  with a sane clamp (`_PARK_CAP_S_DEFAULT = 1800.0`). Read by §7's parked wait at park time.
- **Fast-path scope** — `_fast_path_scope()` → `os.environ.get("BULLDOZER_FAST_PATH_SCOPE") or "reads"`
  (`"reads"` | `"local-work"`). Read by `route_approval`/`_is_trivially_safe` on each approval.

All are pure `os.environ.get` wrappers → existing env-monkeypatch tests keep working; NO module-level
config singleton. Add a block comment near the unattended accessors listing the env names + defaults +
"resolved fresh." **Discoverability:** add a `codex_info` query `"approval"` (or `"bulldozer"`) that
returns each knob's effective value + source (env / default / sentinel) + whether unattended is armed
now — so the model/user can read the current posture without grepping (this is a read-out, the
accessors stay the source of truth).

## 9. What survives / changes / is deleted

- **Survives:** `AppServerManager`, `Reactor`, child lifecycle, the #218/#252 machinery (moves INSIDE
  the inner generator), the attended `elicitation/create` path (byte-identical when unarmed),
  `codex_review`/`codex_info` (plus a parked guard).
- **Changes:** the turn-pump → an inner generator `_drive_turn`; the inline park-route approval → a
  gated `yield`; `main()` gains the `codex_approve` route + the parked guard + the finite parked wait;
  `TurnStateMachine` gains the parked state; the unattended judge → `route_approval`/`_is_trivially_safe`;
  `bridge_approval` loses its unattended inline-decision branch → ATTENDED-ONLY (the loop body builds the
  unattended `fast_accept`/decline reply via `build_decision_response`); `_handle_child_frame` accumulates
  fileChange patches; new accessors `_park_cap_s`/`_fast_path_scope`.
- **Deleted:** the whack-a-mole shell gates (§4) and their tests; `classify_approval` and
  `_unattended_decision` AND `bridge_approval`'s `_unattended_active()` branch that called them (superseded
  by loop-body routing; the attended `bridge_approval`/`_bridge_approval_dispatch` elicitation path stays).

## 10. Testing

- **unit (subprocess `fake_appserver.py` with `FAKE_SCRIPT=with_approval`** — the in-process synchronous
  fake CANNOT model pending-approval-then-resume; extend `with_approval` for multi-approval): the
  generator yields the payload at a park-route approval; `codex_approve(accept)` resumes to completion
  with the exact decision frame (original request id); a WRONG token → expired error, park unchanged
  (double-resume guard); a multi-approval turn re-parks; a `fast_accept` trivial command does NOT yield
  and is answered inline via `build_decision_response` WITHOUT entering `bridge_approval`.
- **routing:** `route_approval` → `fast_accept` for trivial reads; `park_for_model` for complexity /
  `$VAR` / permissions / every fileChange / legacy; `fail_closed_decline` only for malformed;
  `requestUserInput`/`elicitation` bypass routing untouched.
- **teardown:** stray `codex_info` while parked → busy, park preserved; EOF/our-turn-cancel while
  parked → teardown (auto-decline first, `turn_completed` cleared); abandoned park → cap fires teardown
  with NO fresh call; late `codex_approve` → expired error; EOF wins over a same-iteration cap.
- **payload/response:** per-kind evidence present (permissions profile+cwd+environmentId; fileChange
  diff from the patch buffer; no-patch create/delete still gets request-level evidence); unknown
  `decision_id` → retryable MCP error (not permanent decline).
- **config:** `_park_cap_s`/`_fast_path_scope` honor env; `codex_info` `"approval"` reports effective
  values; `_unattended_active` unchanged.
- **attended unchanged:** unarmed → `elicitation/create` path byte-identical (a fake-CC driver answers).
- **slow e2e (real codex):** arm unattended; a real non-trivial approval → `awaiting_approval` → 
  `codex_approve(accept)` → side effect happens → `turn/completed`; plus a `decline` path.

## 11. Migration of the held branch

`bulldozer/feat/251-unattended-approval-policy` (local, not pushed) keeps the proven probe and gets
Part A + Part B. The round-1..4 complex-gate commits are net-reverted by the §4 shrink. History is
winding (denylist → allow-list → gates → shrink+model) — squash/clean before the eventual PR; the net
diff vs `bulldozer/main` is what matters. PR base = `bulldozer/main`.

## 12. Open decisions (small — defaults chosen, flag at implementation)

1. **Fast-path scope default** — start `"reads"` (pure reads + trivial in-project writes; NOT
   `pytest`/`git status`). `"local-work"` is the opt-in looser mode. (§8 knob.)
2. **Amendments depth** — the `decisions[]` from `build_command_approval_labels` IS the amendment
   surface (accept / allow-always-this-command / grant-network / …); the model picks an opaque id.
   v1 can surface just accept + decline if the richer ids prove confusing.
3. **`_PARK_CAP_S_DEFAULT`** — 1800 s (30 min). Env-overridable via `BULLDOZER_PARK_CAP_S`.
4. **`codex_approve` wire name** — `codex_approve` unless a better name surfaces.
