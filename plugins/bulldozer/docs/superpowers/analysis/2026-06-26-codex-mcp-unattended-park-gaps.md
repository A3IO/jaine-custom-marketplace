# #277 unattended model-in-the-loop — park-path evidence/audit/fast-path gaps (umbrella)

## Theme (the single underlying problem)

The #277 feature lets a **model** stand in for the **human** when approving codex's risky actions
(unattended mode). The attended (human) dialog is information-rich: command, network destination,
file diff, reason. The unattended model-in-the-loop is supposed to get the SAME basis to decide.
**It doesn't** — the deciding model is under-informed vs the human dialog, and the auto-approve
fast-path is under-guarded. A/B/C/D below are four facets of that one asymmetry.

Found by dogfooding #277 on branch `bulldozer/feat/251-unattended-approval-policy` (after #278),
then a 3-channel find-holes sweep: `codex_review` (xhigh) + a consult panel (codex+grok+Gemini,
informed) + a 26-agent verify-all swarm. Each finding below is independently **verified against the
code** (not just model assertion).

## Confirmed findings

### A — Fast-path is bypassable by FLAGS (`rg --pre`, `sort -o`)  [severity: weakens→breaks]

`_is_trivially_safe` trusts the read VERB (in `_TRIVIAL_READS`) + a character allow-list
(`_TRIVIAL_SEG_RE`), but never inspects the FLAGS. So:
- `rg --pre <cmd> *` → fast-accepts, but `rg --pre` runs `<cmd>` as a per-file preprocessor =
  **arbitrary code execution** with no model oversight.
- `sort -o /path data` → fast-accepts, but `-o` **writes** to an arbitrary path.

Verified by trace: `_TRIVIAL_COMPLEXITY` (no `$`/`` ` ``/`~`) passes, `_TRIVIAL_SEG_RE` passes (all
chars in the allow-list), `toks[0]` ∈ `_TRIVIAL_READS` → `True`. The predicate's documented contract
("EVERY segment is a plain read … with NO shell-complexity") is violated. Fix: flag-aware — a read
verb carrying an exec/write flag (`rg --pre`, `--hostname-bin`, `sort -o`, …) → park, not fast-accept.

### B — Parked command approval omits `networkApprovalContext`  [severity: weakens]

`build_awaiting_payload` (kind == "commandExecution") copies `command`/`cwd`/`command_actions` but
NOT `params["networkApprovalContext"]` (host/protocol). The attended dialog DOES surface it — via the
human **message** builder `_build_command_approval_message` (~1589-1623, reads
`networkApprovalContext`). (Correction, /check-verified: it is NOT `build_command_approval_labels` —
that builder derives network *labels* from `availableDecisions`/`proposedNetworkPolicyAmendments`
at ~1263-1329, not from `networkApprovalContext`.) So a parked network command is decided by the
model **blind to the destination**, while a human would see it in the message. The data is present in
the request — this is our omission, fixable (include `networkApprovalContext` in the commandExecution
evidence; keep it network-first so bounding can't truncate it away).

### C — Inline unattended decisions are not audit-logged  [severity: weakens]

`_log_approval_event` (the `APPROVAL` audit line) is called ONLY from `bridge_approval`. Every other
decision path writes the response directly and never logs (/check-verified — broader than first
stated):
- inline `fast_accept` / `fail_closed_decline` (~3467-3472),
- model-resumed parked decisions (~3509-3512) and `codex_approve_v2` (no logging),
- **`_teardown_park` auto-declines** on cap / EOF / cancel / child-death (~3295-3307) — the
  unattended safety auto-decline, also unlogged (missed in the first pass).

So with `BULLDOZER_APPROVAL_UNATTENDED=1`, every auto-accept / auto-decline (incl. cap timeouts)
leaves NO audit trail — exactly the decisions an operator most wants logged. CORRECTION: the
`unattended=true` marker is a *parameter* of `_log_approval_event` (1741-1764) but NO current caller
passes `unattended=True` (bridge_approval omits it) — so the marker is presently unused, not "made for
this and bypassed". Fix: route ALL decision paths through logging, and have the unattended paths pass
`unattended=True` (+ the rule that fired).

### D — Fast-path reads are not path-bounded  [severity: weakens (threat-model-dependent)]

`cat /etc/passwd`, `cat /Users/<u>/.ssh/id_rsa` (absolute path) fast-accept: reads auto-approve
regardless of target sensitivity (`_is_catastrophic_target` guards only `mkdir`/`touch`). The model
never sees these reads. NOTE: `cat ~/.ssh/id_rsa` (a `~` path) PARKS — the `~` trips
`_TRIVIAL_COMPLEXITY` — so the panel's `~` example was wrong; the absolute-path form is the real one.
Threat model is LOCAL single-user with prompt-injection out of scope, so this is a design
consideration (should sensitive reads park even unattended?) rather than a clear break — but it is
the same "the fast-path is too trusting" root as A.

## Out of scope / edge / by-design (checked, NOT part of this issue)

- Isolation: a config server whose name contains `.`/`"`/`=` is left enabled under `mcp="isolated"` —
  DOCUMENTED & fail-loud (stderr warning in `_build_isolation_argv`); the metadata-overstate sub-case
  is edge (rare dotted names).
- Re-park resets `started_at` → park cap is per-approval not per-turn — arguably by-design (each park
  wait is individually bounded; the actual stuck-failure mode is bounded per-park).
- Unclosed quote `cat "x` fast-accepts a syntactically broken command — edge (no-op when run).
- `codex_approve(decision_id="decline")` accepts an id not in `decisions[]` — by-design (documented in
  the tool schema as "… or 'decline'").
- `_bound_evidence` "soft cap bypassable by dense nesting" — NOT a material #277 bug, but the earlier
  "FP / hard guarantee" wording was overstated (/check): the fallback is returned WITHOUT re-measuring
  (~2161-2170), so it does not *absolutely* bound the total — a 20KB `park_token` fed directly yields a
  20KB payload. It is safe only because `park_token` is internally generated short (`_park_token()`),
  so a dense real payload falls back to ~363 chars. Leave as-is unless park_token stops being internal;
  optionally add a final size assertion for defense-in-depth.

## Related

- #278 (pre-ACK ACK-timeout) — FIXED on this branch (commits 5eab836 + 7071291).
- #279 (fileChange approvals reach the model with no diff) — a codex-0.142 protocol limitation
  (codex sends no diff before the decision); same THEME as B but the data is genuinely unavailable,
  so it is tracked separately.

## Panel --web confirmation + expansions (2026-06-26, codex+grok+Gemini with live web research)

All three models confirmed A/B/C/D against the code AND web-grounded them. Material additions:

- **A is a CVE-class pattern, and its surface is BROADER than rg --pre / sort -o.** Verb-only
  allow-listing that ignores flags is a recognized-unsafe "Living off the Land"/GTFOBins pattern.
  Prior art: **`sort -o` bypassed Claude Code's command allow-list — CVE-2025-66032**; OpenClaw blocks
  `sort -o`, `grep -R`, `jq -f`, `wc --files0-from`. **/check-verified** (gpt-5.5 ran the actual
  `_is_trivially_safe`) — the dangerous forms reachable in OUR `_TRIVIAL_READS`, by class:
  - **EXEC / arbitrary-WRITE (the real A class):** `rg --pre <cmd>` (runs a preprocessor cmd),
    `rg -z`/`--search-zip`, `sort -o <path>` / `sort --compress-program <cmd>`,
    `uniq <input> <OUTPUT>` (2nd positional writes), `tree -o <path>`. These violate "plain read".
  - **Broad-READ only (this is the D class, NOT exec/write):** `grep -R … /`, `wc --files0-from`.
    Do not conflate with exec/write — they widen reads, matching finding D.
  - **Platform/privilege-dependent (reachable but often inert):** `date --set`/`-s`, `hostname -F`.
  - **NOT reachable:** `jq -f` — `jq` is not in `_TRIVIAL_READS` (the OpenClaw list named it, but our
    set doesn't include it). Drop from scope.
  The fix is not a 2-flag patch: conservatively reject argument/flag forms that turn a read verb into
  exec/write (per-verb dangerous-flag denylist, or reject unknown flags / output-position operands for
  these verbs). ⇒ severity **breaks (HIGH)**.
- **A2 — `route_approval` lets raw-verb parsing override codex's structured `commandActions`.** For a
  modern `item/commandExecution/requestApproval` with a string `command`, `route_approval` goes
  straight to `_is_trivially_safe` (raw verb), so a command codex tagged via `commandActions` as a
  WRITE (e.g. `{"command":"cat README.md","commandActions":[{"type":"write"}]}`) still fast-accepts on
  the read verb (/check-verified). SCOPE (corrected): this overrides only `commandActions` — it does
  NOT override escalation/network-amendment fields, because `_has_escalation_amendment` runs FIRST in
  `route_approval` and parks those. Part of the A fix: don't let raw-verb parsing upgrade a command
  codex's own `commandActions` flagged as more than a read.
- **B** — blind egress approval = zero-trust violation / exfiltration vector; comparable systems
  (OpenAI Codex, Claude Code sandboxing docs) surface+gate the network destination.
- **C** — OWASP logging + audit-trail guidance: log EVERY approval outcome incl. auto-exec and
  timeouts. Also flagged: **no immutable record** linking the original request frame ↔ the evidence
  shown to the model ↔ the chosen decision id ↔ the exact child response (provenance gap, on top of
  the missing inline/resumed log lines).
- **D** — real but threat-model-dependent; **severe when combined with B** (unbounded read + blind
  egress = exfiltration). Comparable sandboxes scope reads to the workspace root.

Sources (web): CVE-2025-66032 (Claude Code `sort -o`), OpenClaw exec-approvals playbook, OpenAI Codex
agent-approvals-security + permissions docs, Cursor GHSA-82wg-qcm4-fp2w, OWASP Logging Cheat Sheet,
VS Code agent approvals, GTFOBins-style man-page references (rg/sort/uniq/tree/date/hostname).

## #281 — the fast-path was DEAD LIVE (shell-wrapper mismatch), now fixed

Live dogfood of the #280 fixes surfaced a higher-level bug that SUBSUMES A/D: the codex app-server
sends EVERY command wrapped as `command = "/bin/zsh -lc '<actual>'"`. `route_approval` fed that literal
to `_is_trivially_safe`, which parks on the path-qualified shell verb (`if "/" in toks[0]`, R8-F1)
BEFORE reaching the A or D gate. So `fast_accept` never fired live and the A/D gates never executed live
— they were correct only against the BARE commands the unit tests send.

**Empirical proof (via `route_approval` directly):** BARE `cat README.md`→fast_accept, WRAPPED
`/bin/zsh -lc 'cat README.md'`→park; BARE `pytest tests/` (local-work)→fast_accept, WRAPPED→park.
This also CORRECTS the A finding above: `rg --pre`/`sort -o` parked live via the WRAPPER, NOT the A-gate
(the earlier "A is load-bearing live" read was wrong — the A code never runs for wrapped commands).

**Fix (commit `0e717f5`):** `_unwrap_shell_wrapper` returns the inner script for the EXACT app-server
shape (basename sh/bash/zsh bare-or-absolute, exactly one `-c`/`-lc`, exactly one script token, no
positional args after it); else the command unchanged → fail-closed park. `route_approval` evaluates the
inner, which runs the SAME predicate (`_split_segments`→A-gate→D-gate) as a bare command. Validated by
`consult --codex --web --repo` (code-grounded): root cause confirmed, unwrap recommended, and **codex
itself unwraps `bash -lc` the same way** (developers.openai.com/codex/rules) — established pattern, not
bespoke. Fail-closed against smuggling: `env zsh -lc`, `time sh -c`, multiple `-c`, `-s`, `--`, `-ic`,
relative-path shell, args-after-script all park.

**Scope-dependent payoff (honest):** under default `scope=reads` the gain is ~nil (codex auto-allows
reads upstream of the bridge); the real value is `scope=local-work` (auto-accept `pytest`/`make` instead
of round-tripping the orchestrating model every call). Trade-off accepted by the operator (Chris):
plus > minus given local-work intent + the fail-safe design. Threat model LOCAL single-user.

## #279 — parked fileChange showed no diff (FIXED, was thought unfixable)

A parked `item/fileChange` approval surfaced `note: "no diff captured yet — decide from reason, or
decline if unsure"` — the model approved a file change BLIND. First read: "codex protocol limitation,
diff unavailable pre-decision." That was **incomplete**. Re-investigated (deepwiki on codex source +
a standalone probe on 0.142):

- The V2 app-server protocol DECOUPLES the diff from the approval. `FileChangeRequestApprovalParams`
  carries only `itemId`/`reason`/`grantRoot` — no diff. (Legacy V1 `applyPatchApproval` carried
  `fileChanges` inline; both shapes exist in the 0.142 schema — so this is a V1-vs-V2 design
  difference, NOT 0.142-specific and NOT a fundamental codex limitation.)
- The diff arrives in the **`item/started`** notification (`item = {type:'fileChange', id,
  changes:[{path,kind,diff}], status}`) **BEFORE** the approval; `item.id` == the approval's `itemId`.
  Our bridge only captured `item/fileChange/patchUpdated`, which codex 0.142 does NOT emit before the
  park → the store stayed empty → the note.
- **Fix (`81e14d8`):** `_handle_child_frame` also captures `item/started` fileChange items into the
  same `ts['file_changes'][item.id]` store `build_awaiting_payload` already reads. Additive, gated to
  `item.type=='fileChange'`. Verified offline (346) + slow (17) + **LIVE** (probe: parked payload now
  carries `changes:[{path,kind,diff}]`).

## Verification provenance

3 independent channels converged: codex_review confirmed A-adjacent + B + C; the consult panel
(codex+grok+Gemini) confirmed A + B + C (+ raised the edge candidates); the verify-all swarm rejected
all of its own (precision channel, recall gap on intricate protocol code). All four were then
re-traced by hand against the real code.

## Post-PR codex_review (P2 / P3 — `9cff0df`)

A final `codex_review` (gpt-5.5, high) on the COMMITTED PR diff (`branch:bulldozer/main`) surfaced two
genuine defects in the just-shipped code — both verified by hand, both TDD-fixed:

- **P2 — untrusted absolute shell unwrap.** `_unwrap_shell_wrapper` (#281) gated only on the shell
  BASENAME (`sh`/`bash`/`zsh`), so an absolute `/tmp/sh -c '<trivial>'` unwrapped to the inner and
  fast-accepted — while the binary actually executed was the untrusted `/tmp/sh`. Fix: absolute shells
  unwrap ONLY from `_TRUSTED_SHELL_DIRS` (`/bin`, `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`);
  bare basenames (PATH-resolved) stay allowed; any unknown absolute path stays wrapped → parks. Like
  the rest of the fast-path this is MOOT LIVE (codex never sends a `/tmp/sh` wrapper), but it was a real
  trust hole in the defense-in-depth layer we deliberately kept (option C).
- **P3 — audit logged the opaque d-id.** The model-resume branch logged `decision=d0` (the id the model
  picked) instead of the resolved grant, so the #280-C audit trail could not tell WHAT was granted. Fix:
  log `_resp["result"]` → `_approval_decision_label` renders `accept`/`acceptForSession`/`perm:*`,
  identical to the attended path.

Both RED→GREEN; offline 349 / slow 17, 0 regressions. (The verify-all swarm's recall gap on protocol
code held again — neither P2 nor P3 came from it; codex_review's adversarial single-pass caught both.)
