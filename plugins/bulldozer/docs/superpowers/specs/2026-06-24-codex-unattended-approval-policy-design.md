# Codex Unattended Approval Policy — Design

**Status:** design (approved scope, awaiting plan → TDD impl)
**Supersedes:** #251 (the blind timer-auto-accept). The issue's own PREREQUISITE — "study real
usage FIRST, then decide" — produced a finding that changes the feature: a blind timer is the
wrong mechanism; a capability-gated judge under an explicit unattended toggle is right.
**Security:** inverts the safe-decline default in unattended mode → shipped only with Chris's
explicit GO (given 2026-06-24) + the escalation-gate safety floor below.

## Goal

When the operator is **away from the terminal**, codex's nested approval requests currently
stall an automated run: the bridge waits up to 300 s for a human, then **declines** (safe
default), which kills the automation. This feature adds an **opt-in unattended mode** in which a
deterministic **capability-judge** answers approvals in-process — auto-accepting routine
in-sandbox work and gating escalations — so unattended automation doesn't stall, while
destructive / out-of-sandbox actions stay blocked.

Attended behavior is **unchanged**: with the toggle off (the default), every approval still
renders today's CC dialog and the human presses Accept as now.

## Empirical basis (why this shape, not #251's timer)

Two studies done before design (this session, 2026-06-24):

**Behavior study — codex transcripts (`~/.codex/sessions/`, 2601 sessions, June):**
27,873 real `exec_command` invocations classified by command shape:

| Bucket | Share | Note |
|---|---|---|
| Read / diagnostic / test | **~90%+** | `nl -ba … \| sed -n` (6892×), `git -C … log/status/diff`, `ps/df/lsof/pgrep`, `pytest`, `shellcheck`, `cat/ls/grep/rg/find` |
| Write **inside project** | ~4% | `apply_patch`, `git add/commit`, in-workspace `mv/cp/touch` |
| **Network** | ~2% | `curl/wget/ssh/scp/git push/gh/pip install` — the one zone the operator ever hesitates on (the single real historical decline was `ssh … nvidia-smi`, aborted for latency, not danger) |
| **Real destructive** | **0** | every match of a loose `rm -rf\|dd\|force-push\|reset --hard` regex was a FALSE POSITIVE — `rg "push --force"` (grepping FOR the pattern, the jaine-sync audit), `trap 'rm -rf "$TMP"'` (cleanup of the script's OWN mktemp), in-project `rm -rf __pycache__` |

Across a month of real use the operator accepted essentially everything because codex never
asked for anything dangerous. So a deterministic judge that **accepts routine in-sandbox work and
gates escalations reproduces the operator's behavior on ~99%+ of commands**, and the
genuinely-ambiguous "needs a model to decide" residue is empirically negligible.

**Feasibility study — `codex_server.py` turn pump (Gap A):**
- Today an approval is handled **synchronously inside one `codex_run` call**: the pump (≈ line
  2965) calls `handle_server_request` → `bridge_approval` → blocks in `read_correlated` until a
  reply/timeout, answers the codex child, continues to `turn/completed`. All turn state lives on
  the local stack.
- A **deterministic judge** hooks at the top of `bridge_approval` and returns accept/decline
  **in-process** — a small, local change; the pump is untouched.
- A **delegate-to-orchestrator** path (return `{awaiting_approval}`, let the calling Claude
  decide, resume) would require **park-and-resume surgery**: hoisting the entire turn-loop state
  off the stack into the singleton, a new non-busy "parked" state, a resume entry, and
  multiplying the #218/#252 interrupt/drain edge cases (child-death / EOF / cancel / never-resumed
  while parked). Feasible but **invasive on the most delicate, recently-hardened code**, for a
  bucket the behavior study shows is ~empty.

**Conclusion:** ship the deterministic judge (this spec). The orchestrator-delegation tier is
**deferred** — revisit only if live use surfaces a meaningful ambiguous bucket.

## Non-goals

- **No timer.** Silence never flips to accept on a clock. The trigger is an explicit operator
  toggle, period.
- **No orchestrator delegation / park-and-resume** (deferred; see above).
- **No change to attended behavior.** Toggle off → byte-identical to today.
- **No change to `codex_review` / `codex_info`** (read-only paths, no approvals).

## Design

### 1. Unattended toggle (explicit, default OFF)

Two equivalent ways to arm it; either present → unattended. Default (neither) → today's behavior.

- **Env:** `BULLDOZER_APPROVAL_UNATTENDED=1` (truthy). For scripted / launched-unattended runs.
- **Sentinel file:** a path the operator `touch`es when leaving and `rm`s on return — convenient
  for "I'm stepping away" without restarting anything. Path: `BULLDOZER_APPROVAL_UNATTENDED_FILE`
  (default `~/.claude/bulldozer-unattended`). Presence = armed.

Resolved fresh **per approval** (so arming/disarming mid-run takes effect immediately, like the
sentinel is meant to). A helper `_unattended_active() -> bool` centralizes the check.

### 2. The capability-judge

Hooked at the **top of `bridge_approval`** (before any `elicitation/create`). If
`_unattended_active()` is False → fall straight through to today's dispatch (no behavior change).
If True → classify and return a decision **without** showing a dialog.

**Inputs available** (already received by `bridge_approval`): `method`
(`item/commandExecution/requestApproval` | `item/fileChange/requestApproval` |
`item/permissions/requestApproval` | legacy `execCommandApproval`/`applyPatchApproval`), `params`
(the command string / file path / `RequestPermissionProfile {fileSystem?, network?}`), the codex
`reason` field (null ≈ routine, populated for network/extra-write/escalation), and the run's
project root / cwd.

**POSTURE (v2, 2026-06-24, per #277): DECLINE-by-default ALLOW-LIST** (flipped from the v1
default-accept-modulo-gate denylist). The find-holes review proved a denylist is a non-converging
treadmill (14 bypasses / 3 rounds). New posture: **ACCEPT only forms provably in a known-safe
allow-list; DECLINE everything else** (network, out-of-project, arbitrary execution, unknown verbs).
Convergent + safe-by-construction. Cost: arbitrary execution (`python3 script.py`, `node x.js`,
`bash deploy.sh`) DECLINES → some automation stalls — that residue is **exactly what the deferred
Tier 2 model-delegation routes to the orchestrator**. Acceptable for v1.

**Decision ladder (DECLINE unless a rule ACCEPTS):**

1. **`permissions` request → DECLINE** always (escalation by definition: network / out-of-sandbox fs).
2. **Structured escalation → DECLINE** (`_has_escalation_amendment`: `networkApprovalContext`,
   proposed network/exec amendments, amendment-bearing `availableDecisions`).
3. **Command (`commandExecution`/`execCommandApproval`) → per-segment allow-list.** Split on shell
   separators; a segment ACCEPTS iff (after stripping `env`/`set`/`export`… prefixes and normalizing
   an absolute interpreter path like `/bin/zsh` → basename `zsh`):
   - **runner wrapper** (`sh|bash|zsh|dash|ksh -c '<payload>'`, `uv run <cmd>`, `timeout|time|nohup|env <cmd>`)
     → recurse the wrapped payload (a bare/script shell with no classifiable `-c` payload → DECLINE — opaque);
   - **READ-SAFE verb** (`cat ls pwd grep rg ag nl head tail wc sed[-n only] awk cut sort uniq tr jq
     yq diff stat file strings basename dirname realpath which type echo printf date env ps df du
     lsof pgrep find[NO -exec/-delete] pytest shellcheck`; `git <read-sub: status|log|diff|show|
     branch|rev-parse|ls-files|blame|remote -v>`) → ACCEPT, **unless** a mutation/egress gate fires
     (§3);
   - **WRITE-IN-PROJECT verb** (`apply_patch touch cp mv mkdir ln rm sed[-i]`; `git <write-sub:
     add|commit|checkout|switch|stash>`) → ACCEPT **iff every target resolves INSIDE the project
     root** (`_is_catastrophic_target` False);
   - else (network verbs, `python3|node|ruby|perl|…` arbitrary execution, `sudo`/`doas`, unknown
     verbs) → **DECLINE**.
   A malformed/unreadable command (no string AND no read-actions) → DECLINE (fail-safe).
   `commandActions` all-read → ACCEPT shortcut.
4. **`fileChange`/`applyPatch` → ACCEPT** (in-sandbox edit; an out-of-sandbox write arrives as a
   `permissions` request, declined at rule 1).

### 3. Why safe verbs still need mutation/egress gates

Under the allow-list, network/destructive/arbitrary-execution verbs need **no denylist** — they
simply aren't in the allow-list, so they DECLINE by default (this is what converges). But a
READ-SAFE verb can still mutate/egress, so these gates run on the safe-verb ACCEPT path (the
study's real cases) — the SAME helpers built in v1, now guarding accept rather than forming a
(unbounded) denylist:
- out-of-project `>`/`>>` redirect (`echo x > ~/.bashrc`) — `_redirects_to_unsafe`;
- `..` traversal / out-of-project absolute target (`rm -rf ../repo`, `cp x /etc/y`) — `_is_catastrophic_target`;
- `$(…)`/backtick substitution carrying network/destructive (`echo $(curl …)`) — `_substitution_is_dangerous`;
- `find -exec/-delete`, `sed -i` to an out-of-project file.

The study's false positives now ACCEPT for the right reason: `rg "rm -rf /"` — `rg` is READ-SAFE,
its args are data; `trap 'rm -rf "$TMP"'` — `trap` isn't a write verb and `$TMP` isn't catastrophic;
in-project `rm -rf __pycache__` — `rm` is WRITE-IN-PROJECT with an in-project target.

The judge logic + denylist live in a **pure, separately-unit-tested** function
(`classify_approval(method, params, reason, project_root) -> "accept"|"decline"`) so it is
testable without any codex/bridge plumbing.

### 4. Audit

Every unattended decision logs one best-effort line to the existing stable log
`~/.claude/hooks/bulldozer-codex.log` (the #251 step-0 `bridge_approval` channel already ships) —
reusing `_log_approval_event` with an added marker (e.g. `unattended=true | rule=<bucket>`) so the
operator can review what was auto-decided while away. Never blocks; sanitized (no newline / ` | `).

### 5. Attended mode — unchanged

`_unattended_active()` False → `bridge_approval` does exactly what it does today (elicitation
dialog, human reply, #18268 decision mapping, #224 narrative, #272 permission grant-echo). Zero
diff in the attended path's behavior.

## Security analysis

The feature **inverts the safe-decline default** — in unattended mode codex acts on its requests
without a human. Why it is defensible:
1. **Opt-in, default OFF** — nothing changes unless the operator explicitly arms it per session.
2. **Capability floor** — auto-accept is confined to actions that stay **within codex's already-
   granted sandbox / project root**; every escalation (network, out-of-project write, destructive
   shape) is **declined**, so unattended mode **cannot widen the blast radius** beyond what codex
   could already do silently.
3. **Empirical risk** — 0 real destructive escalations in 27,873 commands over a month; the
   operator already accepted everything. The judge reproduces that, plus insurance for the rare
   catastrophic shape (which is exactly what one wants caught while AFK).
4. **Audited** — every auto-decision is logged for after-the-fact review.

This also **dissolves the #251 open question** ("what happens to the stale CC dialog when our
timer self-resolves?") — in unattended mode we decide **before** calling `elicitation/create`, so
no dialog is ever shown.

## Testing strategy

- **Offline unit tests** (`classify_approval`, pure): every bucket — reads incl. codex idioms,
  in-project writes, network-decline, out-of-project write decline, denylist TRUE positives AND
  the FP cases from the study (grep-for-pattern, trap-cleanup-own-tmp, in-project `__pycache__`),
  default-unmatched, malformed params (fail-safe decline).
- **Toggle tests:** env truthy/falsey, sentinel present/absent, off → today's dispatch reached
  (judge bypassed), per-approval fresh resolution.
- **Bridge-integration tests** (direct import, fake codex): unattended on → judge return reaches
  the child without an `elicitation/create`; unattended off → elicitation path unchanged;
  #18268 decision mapping intact; audit line written.
- **Mandatory `pytest -m slow`** against real codex after every `codex_server.py` change.
- **TDD: visible RED before GREEN** for each unit.

## Open questions (resolve during implementation — not blockers)

- **O1 — toggle ergonomics:** env + sentinel both, or just one? (Spec ships both; trivial to drop
  one.)
- **O2 — codex on-request approval surface (LOAD-BEARING for the §2.4 default):** does
  `approval_policy=on-request` surface **routine in-sandbox commands** for approval (reason≈null),
  or **only escalations**? If only escalations, the judge's job narrows to "gate escalations" and
  the routine-accept buckets rarely fire (even simpler + safer). Resolve via codex docs + a live
  approval-shape capture (drive one `codex_run` that triggers an approval; the operator sees one
  dialog). The design is robust to **both** answers; O2 only tunes the §2.4 default.
- **O3 — network in unattended:** v1 declines all network. Confirm that's the desired v1 posture
  (vs. a trusted-host allow-knob) — deferred unless the operator wants it now.

## Review hardening (codex_review + multi-model panel, 2026-06-24)

Adversarial review of the diff found classifier bypasses; all fixed + pinned by tests:
- **Read-leading verb must not bypass mutation gates.** `echo x > ~/.bashrc`, `find … -exec rm -rf`,
  `find … -delete`, `sed -i … /etc/x` looked like reads. Fix: redirection / `find -exec|-delete` /
  command-substitution / `sed -i` gates run BEFORE the read fast-path (`_segment_verdict`).
- **`..` path traversal.** `rm -rf ../other-repo`, `mkdir -p ../outside` escaped the project.
  Fix: `_is_catastrophic_target` flags any `..` path component.
- **Out-of-project write verbs.** `touch /tmp/x`, `cp a /etc/x`, `mv s /tmp/exfil`. Fix: `_WRITE_VERBS`
  gated by `_is_catastrophic_target`.
- **Command substitution.** `echo $(curl …)`, `cat $(rm -rf ..)`. Fix: `_substitution_is_dangerous`.
- **git with global options.** `git -C sub push --force`, `git -c k=v fetch` bypassed the immediate-
  subcommand regex. Fix: token-aware `_is_git_network` scans past global options.
- **execpolicy amendment via `availableDecisions`.** Fix: `_has_escalation_amendment` flags any
  amendment-bearing dict decision.
- **FP fix:** `git remote -v` no longer declines (local config op).

## Known v1 limitations (documented, sandbox-mitigated — NOT chased)

The string classifier is **defense-in-depth on top of codex's sandbox**, which is the real floor
(an out-of-sandbox write/network attempt becomes a permissions request → declined at §2.1). These
residual gaps are accepted for v1 (the find-holes bar was floor-breaks + cheap FPs, then STOP):
- **Adversarial shell obfuscation** — `cu\rl`, `cu""rl`, deep nested `$(...)`. codex is our own
  agent, not an adversary; the sandbox gates real egress.
- **Network/destructive token inside a quoted string** — `python3 -c "print('curl …')"` may
  wrongly DECLINE (a stall, not a safety break). Rare; refine if it bites.
- **No realpath/symlink resolution** — `/proj/link-to-home/x` via a planted symlink. Requires a
  pre-planted symlink AND a permissive sandbox.
- **Minor FP** — `npm ci --offline` / `uv pip compile` decline despite being offline/resolve-only.
- These only matter under `sandbox=danger-full-access` (no sandbox) — an explicit YOLO choice.

## Files

- `mcp/codex_server.py` — add `_unattended_active()`, `classify_approval(...)`, the
  `bridge_approval` top hook, and the audit-marker extension to `_log_approval_event`.
- `tests/test_codex_mcp_v2.py` — new `TestUnattendedJudge` (offline) + bridge-integration cases;
  extend the slow-e2e if a live approval path is exercised.
