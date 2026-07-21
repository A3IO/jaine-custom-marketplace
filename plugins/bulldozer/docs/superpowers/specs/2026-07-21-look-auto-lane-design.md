# /look auto-lane — default-to-isolated for agent-own tasks (#187 Proposal A)

**Status:** design for review (v9 — R8 rework: `bin` dropped from the config
signature, aligning it with the barrier vehicle)
**Issue:** #187 (Proposal B — doc half — shipped in PR #337; this spec covers the
remaining Proposal A mechanics)
**Scope:** `skills/look/scripts/launch.sh`, `skills/look/scripts/cdp.py` (one hint
line), `skills/look/SKILL.md` (Quick Invoke routing), `tests/` (new tests only —
the port registry is NOT touched, see §3).

## 1. Problem

Two live incidents (#187 body + recurrence comment): an agent iterating its own
file:// / localhost task on the default port 9333 navigated, reloaded and injected
JS into the USER'S live browser tab. Proposal B (shipped) fixed the guidance; this
spec ships the **mechanics** so defaults stop depending on agent discipline:

1. `launch.sh --auto-lane` — one flag that yields a session-owned isolated lane
   (OS-assigned port + session-keyed temp profile + contract lines), no manual
   port bookkeeping.
2. Quick Invoke routing — agent-own tasks default to the isolated lane; 9333
   becomes explicit opt-in ("посмотри в МОЁМ браузере").
3. `cdp.py` DX hint — a flag-like token received as the command prints a zsh
   word-splitting hint (the `T="--target <id>"; python3 cdp.py $T js …` trap).

## 2. Non-goals / non-regression constraints (from #187, binding)

1. **Co-browsing on 9333 stays canonical.** Explicit "открой у меня / co-browse /
   нужны мои куки-логины" NEVER routes to a lane.
2. **User-context tasks** (cookies, logins, "как выглядит под моим аккаунтом") —
   only 9333; no attempt to reproduce in a lane.
3. **Transparency** — lane choice is logged (`lane-start … auto_lane=1`) and
   announced by the agent in its reply ("работаю в изолированной lane :<port>").
4. **Backward compatibility** — every existing invocation without the new flag
   behaves byte-identically, INCLUDING dry-run stdout and log-line shapes (the
   `auto_lane=` dry-run key and the `auto_lane=1` log field appear ONLY when the
   flag is used). `--auto-lane` is a NEW flag; the Quick Invoke heuristic is the
   sanctioned routing change (constraint 4 of the issue names both as the
   allowed vehicles). cdp.py defaults do not change.
5. **Headful debugging stays on 9333** — human-watching requests and the
   headful-only window commands (`window upper/lower/activate`) route to 9333 in
   Quick Invoke (§6 gives them routing priority over agent-own signals).
   `--auto-lane --headful` exists as a LOW-LEVEL escape hatch (parity with
   `--headless/--headful` on every other lane) but Quick Invoke NEVER selects
   it — an auto-lane chosen by routing is always headless.

**Acceptance:** no existing SKILL.md recipe changes behavior without a new flag;
`launch.sh` without `--auto-lane` produces byte-identical dry-run stdout (pinned
by a full-stdout golden test, §8.1); existing log lines carry no new fields.

## 3. Allocation model: `--remote-debugging-port=0`, NOT a fixed-port range

The issue sketch said "порт из hash(...) в диапазоне 9340–9390". That model is
**unimplementable as specified and is replaced**:

- **R1-F1 (empirically confirmed 2026-07-21):** Chromium writes
  `DevToolsActivePort` into the profile ONLY when launched with
  `--remote-debugging-port=0`; on a fixed port the file is never created (probe:
  stock Chrome 150, fixed port 9391 → CDP answers, NO file; the live daily 9333
  profile has no file either). Fixed ports therefore have NO in-profile identity
  proof, and any `/json/version` readiness check can be satisfied by a THIEF
  that bound the port between probe and launch.
- A fixed range also needed: a registry slot (conflicting with /drive 9340–9349
  and the `9360+` transient-probe reservation), a free-port probe, an
  exhaustion path, and a widened `test_e2e_lanes.py` assertion.

**Port 0 dissolves all of it.** The OS assigns the port; Chrome writes
`DevToolsActivePort` (line 1 = port) into OUR profile — identity-strong by
construction, the exact mechanism the SP4 ephemeral arm already ships
(`launch.sh` `EPHEMERAL` block). No probe, no range, no exhaustion, no
stolen-port race class, no port-registry edits, and the `test_e2e_lanes.py`
`range(9330, 9370)` assertion is untouched. Under the DEFAULT macOS ephemeral
port range (`net.inet.ip.portrange`: 49152–65535) an OS-assigned port cannot
land in the registry band; a host whose sysctl portrange was tuned down below
9370 is OUT OF SCOPE — that exposure is pre-existing and shared verbatim with
the SP4 `CDP_PORT=0` ephemeral lanes (same allocation mechanism, same
assertion), not introduced by this feature (R3-F4).

**Session determinism moves from the port to the PROFILE** (§4.2): the lane's
identity is its session-keyed profile path; the port is an OUTPUT (contract
lines, §4.6), not an input. Lane reuse is pass-0 by profile (§4.4), which a
hash-derived port only ever approximated.

## 4. `launch.sh --auto-lane`

### 4.1 Flag surface and exclusions (all fail-loud)

`--auto-lane` is recognized in the existing arg parser. Exclusions use
**presence** checks (`${VAR+x}` — Bash-3.2-safe), not truthiness: a set-but-empty
value is still "set" and still errors (R1-F8).

| Combination | Behavior | Rationale |
|---|---|---|
| `--auto-lane` + env `CDP_PORT` present (any value: 9333, 0, empty, garbage) | ERROR | auto-lane owns port selection; an explicit port contradicts it. Also sidesteps every gate-ordering question: with no env, the top-of-file config resolves to 9333+daily and all early gates pass vacuously before the auto-lane arm overrides (§4.3 re-runs the profile-dependent ones). |
| `--auto-lane` + env `LOOK_PROFILE_DIR` present (any value) | ERROR | auto-lane owns the profile (ownership token, §4.2) — mirrors the SP4 ephemeral guard. |
| `--auto-lane` + `--automation` / `LOOK_AUTOMATION` truthy | ERROR | the CfT automation path already has its own isolated-lane mechanics (`CDP_PORT=0 --automation`); error text points there. |
| `--auto-lane` + `--insecure` / `--cert-spki=` | **allowed** | the auto-lane profile is isolated by construction; the arm sets `PROFILE_OVERRIDDEN=1` (the same move the automation arm makes for its temp profile), so the existing insecure/cert gates pass naturally (non-9333 + overridden + non-daily). This composition is the original `--insecure` use case (file:// page fetching LAN http) on exactly the task class auto-lane targets. |

The internal port-0 launch does NOT weaken the SP4 gate: env `CDP_PORT=0`
without `--automation` still errors exactly as today (the auto-lane arm engages
the port-0 path internally, after its own exclusions already rejected any env
`CDP_PORT`). No env alias for the flag (`LOOK_AUTO_LANE` is NOT provided) — one
spelling, arg-only, keeps the exclusion matrix small.

**Two-phase structure (R2-F2 — exclusions must FIRE before older gates):**

- **Phase A, presence snapshot** — at the very top of the file, BEFORE the
  `CDP_PORT="${CDP_PORT:-9333}"` defaulting line (which destroys the
  unset-vs-set distinction), capture `_CDP_PORT_WAS_SET=${CDP_PORT+x}` and
  `_LOOK_PROFILE_DIR_WAS_SET=${LOOK_PROFILE_DIR+x}`.
- **Phase B, exclusion preflight** — IMMEDIATELY after the argument-parsing
  loop (which sets `AUTO_LANE_ARG`), BEFORE the ephemeral (`CDP_PORT=0`) and
  automation gates: if `--auto-lane` was given, reject env-`CDP_PORT`-present /
  env-`LOOK_PROFILE_DIR`-present / automation-requested with the auto-lane
  error texts. Phase B computes the automation request LOCALLY from
  `AUTOMATION_ARG` + `LOOK_AUTOMATION` (the existing `AUTOMATION_REQUESTED`
  resolution runs later, after the ephemeral block — R3's ordering note).
  Without this ordering, `CDP_PORT=0 --auto-lane` dies in the SP4 ephemeral
  gate with a message that never mentions auto-lane, and `--automation
  --auto-lane` runs the automation arm's mutations before the conflict is
  detected. (Error-attribution tests: §8.1.5.)

  **Attribution contract (R2-F2, refined):** auto-lane error attribution is
  promised for env values that SURVIVE the pre-parser legacy validation:
  `CDP_PORT` ∈ {valid ports, `0`, and `""` — the top-of-file
  `${CDP_PORT:-9333}` collapses empty to 9333 (`:-` substitutes on
  unset-OR-null), so an empty value reaches Phase B; the Phase-A snapshot
  still records it as SET}; any `LOOK_PROFILE_DIR` that passes the
  backslash/newline guard AND does not trip a pre-parser SAFETY gate. Two
  legacy-error classes are carved out and PINNED as accepted (§8.1.5): (a)
  values legacy VALIDATION rejects before the parser (non-numeric/overflow
  `CDP_PORT`, backslash profile); (b) COMBINATIONS that trip the unconditional
  #160 daily-profile SAFETY gate before the parser (e.g. `CDP_PORT=9350
  LOOK_PROFILE_DIR=<daily> --auto-lane` — R2-F2 v3) — a safety refusal
  should not be delayed for the sake of message attribution. The INVARIANT is
  "auto-lane + any env override never launches", not error-text ownership.
- **Phase C, mutation arm** — after the automation arm's position, before the
  insecure/cert-spki gates: the port/profile override of §4.3.

### 4.2 Session key and profile (the ownership token)

```
key   = CLAUDE_CODE_SESSION_ID if present (non-empty), else $PPID
key8  = printf '%08x' $(printf '%s' "$key" | cksum | cut -d' ' -f1)   # 8 hex chars
PROFILE_DIR = ${TMPDIR:-/tmp}/look-lane-<key8>
```

- `CLAUDE_CODE_SESSION_ID` is exported into the Claude Code Bash-tool
  environment (verified live 2026-07-21) → stable per session → same profile →
  pass-0 reuse across the session's invocations. Fallback `$PPID`: stable per
  interactive terminal (launch.sh's parent = the interactive zsh); in exotic
  wrappers it may vary per call — honest consequence: no cross-call reuse, a
  fresh lane per call; the printed contract (§4.6) remains the authoritative
  handle either way. Documented in SKILL.md, not worked around.
- The session key in the profile path is the load-bearing ownership decision
  (SP4 precedent: "the unique profile IS the ownership token"): every kill in
  launch.sh is profile-scoped, so a foreign session's lane can never be
  identified as ours, let alone killed. `<key8>` is hex by construction; the
  TMPDIR-derived path still gets the backslash/newline guard (§4.3).
- TMPDIR is per-user on macOS and reboot-cleaned → no persistent profile
  accumulation (hole E precedent).

### 4.3 The auto-lane arm: placement and recomputation set (R1-F3)

The arm runs AFTER argument parsing and the automation/ephemeral arms (so its
exclusions can see them), BEFORE the insecure/cert-spki gates (so composition
works) — i.e. exactly where the automation arm sits in the flow today. Because
config-time resolution already ran with the vacuous defaults (9333 + daily
profile), the arm must recompute EVERY profile/port-dependent value, mirroring
the automation arm precedent:

1. `CDP_PORT=0` (internal; enables the port-0 launch + readiness path);
2. `PROFILE_DIR=${TMPDIR:-/tmp}/look-lane-<key8>`; `PROFILE_OVERRIDDEN=1`;
3. backslash/newline guard on the derived path (mirror of the automation arm's);
4. **daily-profile re-check, fail-closed**: `_resolves_to_daily_profile
   "$PROFILE_DIR"` must echo exactly `0`, else ERROR — a TMPDIR alias/symlink
   that resolves into the daily profile must die here, not launch (the
   config-time #160 gate ran before the arm and checked the WRONG values);
5. `LOG="$PROFILE_DIR/chrome.log"` (the `PROFILE_OVERRIDDEN` LOG rule — without
   this the lane appends to the daily `/0/.jaine/.browser/chrome.log`);
6. window position (headful escape hatch only): derived from **`key8`**, NOT
   the port — the OS port is unknown until after launch, while
   `--window-position` must be in `CHROME_ARGV` before Chrome starts (R1-F3):
   `_off = (0x<key8> % 1200)`, normalized non-negative exactly like the
   existing port formula, `WINDOW_X = WINDOW_Y = 100 + _off`. Deterministic
   per session, available pre-launch, distinct across sessions. Pinned at the
   ARGV level (§8.1): dry-run `window_position` + the
   `--window-position=<derived>` token in ARGV. A real-GUI position assertion
   is deliberately OUT of the test plan: Chrome honoring its own
   `--window-position` flag is not this feature's contract, and a headful e2e
   would flash a window on the dev machine (the suite keeps non-9333 lanes
   headless). Headless (the default) never uses the value.

`KILL_MATCH` is computed after all arms (existing code order) and therefore
derives from the FINAL profile — stated here so the invariant is explicit, no
code move needed.

### 4.4 Pass 0 — true reuse, no restart, config-compatible only (R1-F6, R2-F1)

**Cmdline-derived config signature (R2-F1 v3 — replaces the v3–v5 sidecar
file).** The signature of a RUNNING lane is derived from the main browser
process's OWN command line — the one artifact a parallel-launch race cannot
falsify (the v5 sidecar could lie: with two same-profile first launches racing
into Chrome's user-data-dir singleton, the LOSER's file could describe the
WINNER's browser). Fields, all visible in argv:

```
headless  = "--headless=new" present
insecure  = "--disable-web-security" present
cert_spki = value of "--ignore-certificate-errors-spki-list=<...>" (or empty)
```

**`bin` (argv[0]) is deliberately NOT a signature field (R2-F1 v6):** it is
not security-bearing (the security-relevant state is the three flags above),
no SKILL.md recipe composes `CHROME_BIN` with `--auto-lane`, and comparing
argv[0] would make any exec-style test vehicle (or a Chrome relaunch of
itself) self-mismatch — the r8 review showed the §8.4.7 barrier wrapper would
fail BOTH racers on it. A caller who really changes `CHROME_BIN` mid-session
tears the lane down explicitly (SKILL.md teardown recipe) — documented, not
silently restarted.

**Main-process selection:** among `pgrep -f -- "--user-data-dir=<escaped
profile>…"` matches, the main browser is the one whose argv contains NO
`--type=` flag (Chromium convention: every child — renderer/gpu/utility —
carries `--type=…`); zero or multiple main-process candidates → NOT reusable
(fall through to the restart path). No sidecar file exists at all: no atomic
write, no write-failure LANE_FAIL, nothing to keep truthful.

Before launching, the arm checks for OUR live lane:

```
main browser process found for $PROFILE_DIR (rule above)
  AND  its cmdline-derived signature equals the CURRENT request's signature
       (field-by-field)                        — else reason=config-mismatch
  AND  $PROFILE_DIR/DevToolsActivePort exists, line 1 parses as a port,
       line 2 is a non-empty browser websocket path (/devtools/browser/<uuid>)
  AND  curl -s -m 2 http://localhost:<line1>/json/version answers AND its
       webSocketDebuggerUrl PATH equals line 2 exactly (identity binding)
       — else reason=identity-mismatch / unhealthy
→ REUSE: print the human line ("JAINE Browser reused (…)") + LANE_REUSED=1 +
  the full contract block (§4.6) and exit 0. NO pkill, NO relaunch — the
  browser keeps its tabs and page state.
```

**Identity binding (R1-F1, pass-0 side):** unlike the fresh-launch path, pass 0
never spawned the process it is about to trust — `pgrep -f` matches ANY cmdline
carrying the profile string (helper processes, editors, decoys), and a stale
`DevToolsActivePort` can name a port that some UNRELATED CDP endpoint has since
been assigned. The browser-uuid comparison closes this: `DevToolsActivePort`
line 2 is the writer's unique `/devtools/browser/<uuid>` path, and the SAME
uuid appears in the answering endpoint's `/json/version →
webSocketDebuggerUrl` (`ws://localhost:<port>/devtools/browser/<uuid>` —
byte-verified live 2026-07-21 on stock Chrome 150, port-0 launch). Equal →
the endpoint that answered IS the browser that wrote the file. Any mismatch /
missing line 2 → NOT reusable, `reason=identity-mismatch`, fall through to
the restart path (§4.5, incl. the fail-closed survivor check).

- **Config mismatch** (live main process whose cmdline signature differs —
  e.g. plain→`--insecure`, `--insecure`→plain, cert-pin change,
  headless→headful): the running Chrome's flags cannot be changed in place, so
  this is a **restart-by-design**: profile-scoped `pkill` + fresh launch with
  the new configuration, logged `lane-stop … reason=config-mismatch` (§4.6). Both
  directions of the insecure transition are pinned in tests (§8.4) — a stale
  `--disable-web-security` surviving into a plain request is the security case
  this closes. **The restart is fail-closed on a surviving process (R2-F1):**
  the existing pkill wait-loop tolerates a survivor and proceeds — acceptable
  for a plain relaunch, NOT here: a surviving old-config Chrome would win the
  user-data-dir singleton and the "new" lane would silently BE the old-config
  browser. After the pkill wait-loop, the auto-lane path re-checks
  `pgrep -f -- "$KILL_MATCH"`; still alive → LANE_FAIL ("old lane would not
  terminate; refusing to launch with a changed config") + `lane-fail` log,
  exit 1 — never proceed to spawn.
- **The URL argument is NOT applied on reuse** (launch.sh only passes a URL via
  Chrome argv at process start). The contract line `LANE_REUSED=1` tells the
  caller; SKILL.md's isolated branch always navigates explicitly via cdp.py
  after a reuse (§6), so the URL still lands — via the right tool.
- **Unhealthy lane** (pgrep hit but the file is missing/garbled or CDP does not
  answer) → fall through to the standard profile-scoped `pkill` + fresh launch
  (§4.5). This is the existing restart semantics, applied only to a dead lane.
- No pgrep hit → fresh launch (§4.5).
- e2e pins the reuse semantics by PROCESS IDENTITY and PAGE STATE, not port
  equality: same browser PID before/after the second invoke, and a page
  navigated before the second invoke still there after (§8.4).

### 4.5 Fresh launch — SP4-style readiness, deterministic profile

```
rm -f "$PROFILE_DIR/DevToolsActivePort"
[ -e "$PROFILE_DIR/DevToolsActivePort" ] → LANE_FAIL, pre-spawn   # R1-F1: rm is
  # NOT trusted — launch.sh has no `set -e`, and a surviving stale file would let
  # readiness read a STALE port whose endpoint may be answered by an UNRELATED
  # browser (false success + wrong-browser contract). Fail-closed before spawn.
launch Chrome with --remote-debugging-port=0 (headless unless --headful)
wait ≤10s for a non-empty $PROFILE_DIR/DevToolsActivePort   (else LANE_FAIL)
CDP_PORT=$(line 1); validate ^[0-9]{1,5}$                    (else LANE_FAIL)
curl /json/version readiness loop (10×0.3s)                  (else LANE_FAIL)
post-readiness effective-config verification (below)         (else LANE_FAIL)
```

The stale-evidence guard is the FIRST profile mutation, so the §8.1.11
regression test can REACH it: an unwritable profile dir (`chmod 555`) fails
`rm` at the first step and must die on the absence check (the R3
test-reachability point).

**Post-readiness effective-config verification (R2-F1 v3):** readiness proving
"a browser with OUR profile is up" is NOT the same as "OUR configuration is
up" — in the parallel-race interleaving below, the surviving browser can be
the OTHER call's. After readiness, locate the main browser process for the
profile (§4.4 rule), derive its cmdline signature, and compare with THIS
request's. Mismatch (or no unique main process) → LANE_FAIL ("a different
launch won this profile; re-invoke to reuse or restart it") + `lane-fail`
log — never print a contract that misdescribes the running browser.

Every LANE_FAIL: stderr diagnostic + `lane-fail` log line (§4.6) + kill
`$CHROME_PID` (post-spawn failures only) + exit 1. The profile dir is NOT removed on failure (unlike SP4's
mktemp lanes, it is deterministic and reusable; TMPDIR cleanup owns it). On
success the blind `sleep 3` is skipped (readiness already proved CDP up — the
ephemeral-arm optimization). Identity needs no extra proof: the file lives in
OUR profile and was written by the process WE spawned into that profile.

**Same-session parallel-launch race** (two Bash calls, both miss pass 0, both
launch): Chrome's per-user-data-dir process singleton resolves who runs — the
second Chrome hands off and exits. The post-readiness verification above makes
the outcome HONEST for both callers: whichever call's configuration lost, its
launch.sh compares the SURVIVOR's cmdline against its own request and
LANE_FAILs loudly instead of printing a wrong contract (with same-config
racers the comparison passes and both contracts are correct). Because the
signature lives in the surviving process's cmdline — not in a file either
racer could overwrite — the next pass 0 also judges the survivor by what it
actually IS (the v5 sidecar's stale-lie window is gone). Accepted residue:
loud failure, rare (an agent launches its lane once), self-healing (the next
call reuses or restarts the winner via pass 0). Documented here so a future
session does not "fix" it into a lock.

**Accepted residual check-then-act windows (post-ship codex review, 2 × P1,
wontfix-by-design):** with UNSYNCHRONIZED same-session parallel invocations —
a pattern outside the documented Quick Invoke workflow, which is sequential —
two millisecond-scale windows remain: (a) a reuse call revalidates, then a
concurrent config-restart kills the lane before the contract prints → the
caller gets a dead `CDP_PORT` with exit 0; the very next cdp.py call fails
loudly and one re-invoke recovers (SKILL.md says so); (b) two staggered first
launches can interleave pkill/rm/spawn so that one deletes the other's justwritten
`DevToolsActivePort` → one or both LANE_FAIL loudly, the surviving
browser is restarted by the next call's pass-0 `unhealthy` arm. Fully closing
either requires a per-profile lock lifecycle (macOS ships no flock(1); a
mkdir-lock brings staleness recovery) — the exact complexity this section
already declined for the same race class. Failure direction in both: loud or
self-healing, never a silently wrong browser.

### 4.6 Outputs, logging, headless precedence

- **Headless precedence** (R1-F8, presence-aware): `--headless/--headful` arg >
  `LOOK_HEADLESS` present (`${LOOK_HEADLESS+x}`, resolved by the existing
  truthy regex — so an explicit `LOOK_HEADLESS=0` yields headFUL) > auto-lane
  default **1** (vs the global default 0). Only the last default changes, and
  only under the new flag — additive.
- **Contract lines** (stdout, after the human line; grammar of the SP4
  ephemeral contract + one new key):

  ```
  LANE_REUSED=0|1
  CDP_PORT=<os-assigned port>
  LANE_PROFILE=<profile>
  LANE_KILL_MATCH=<escaped pkill pattern>
  LANE_BROWSER_BIN=<chrome bin>
  ```

- **Dry-run:** with `--auto-lane`, the config block gains `auto_lane=1` and
  reports `port=0` (allocation is a launch-time outcome), the derived profile,
  `headless` per the precedence above, and the pass-0 result from a read-only
  check (pgrep + file reads + curl are all reads):
  `auto_lane_reuse=0|1` + `auto_lane_reuse_reason=ok|no-process|
  config-mismatch|identity-mismatch|unhealthy` — the reason key is what makes
  the pass-0 arms independently testable offline (§8.1.12: a decoy process
  whose cmdline carries the profile string exercises the pgrep arm without
  Chrome; a fake HTTP endpoint with a wrong uuid exercises the identity arm).
  WITHOUT the flag the dry-run stdout is byte-identical to today — no
  `auto_lane*` key at all (constraint §2.4); pinned by a full-stdout golden
  test (§8.1).
- **Logging:** `lane-start` / `lane-fail` lines gain `auto_lane=1` ONLY on
  auto-lanes (no-flag lines unchanged). A pass-0 reuse logs `lane-reuse`
  (same canonical writer, port + profile k=v) so lane lifetime stays minable.
  `_log_lane`'s 9333 early-return is never in play: every auto-lane log call
  happens with `CDP_PORT` already 0 (pre-launch failures) or the OS-assigned
  port (post-launch) — both non-9333. They land in `bulldozer-drive.log` like
  every non-9333 lane today (pre-existing naming quirk, unchanged — noted, not
  fixed here).
- **Teardown:** deliberately NOT automatic. Lane reuse across invocations
  within a session is the feature. Bounded: one lane per session key,
  TMPDIR reboot-cleaned, explicit teardown recipe in SKILL.md
  (`pkill -f -- "<LANE_KILL_MATCH>"`).

## 5. `cdp.py` flag-like-token hint

The unknown-command arm (`cmd not in COMMANDS`, currently a single stderr line +
`return 1`) gains one conditional line when `cmd.startswith("--")`:

```
Unknown: --target <id>. Available: …
Hint: flag-like token received as the command. In zsh, $VAR does not word-split —
pass flags inline (python3 cdp.py --target ID js …) or use ${=VAR}.
```

Exit code and the first line stay byte-identical. Reality of the trap (verified
2026-07-21): the zsh failure `T="--target <id>"; python3 cdp.py $T js 1` delivers
`--target <id>` as ONE argv token — bare `--target` is consumed earlier by the
global parser as a missing-selector error and never reaches this arm. The hint
therefore fires exactly for the single-token flag-like shape (and for any other
`--…` token the parser did not consume, e.g. a misplaced `--headless`).
`--target`/`--tab` parsing is untouched.

## 6. SKILL.md Quick Invoke routing (the sanctioned heuristic change)

Quick Invoke is restructured into **route-first, two complete branches**
(R1-F4/F5). The routing step comes IMMEDIATELY after `$ARGUMENTS` parsing —
BEFORE any `cdp.py status` call (today's step 1 probes 9333 unconditionally;
that probe moves inside the 9333 branch):

**Routing rule (priority order):**

1. **9333 branch** — any of: explicit user-context ("у меня / мой браузер /
   co-browse / посмотри со мной / нужны логины-куки / как под моим
   аккаунтом"), the task needs the user's session state, OR the task is
   headful/human-watching (user will watch; `window upper/lower/activate` —
   headful-only commands live on 9333 per constraint §2.5).
2. **auto-lane branch** — agent-own signals: file://, localhost/127.0.0.1
   preview, UI iteration, screenshots for the agent's own verification.
3. **Ambiguous → auto-lane** (that IS default-to-isolated), and the reply must
   NAME the choice: "работаю в изолированной lane :<port>" — the user redirects
   with one word if they wanted co-browsing (cheap to reverse, unlike JS
   already executed in the user's tab).

**9333 branch:** the existing steps 1–5 VERBATIM (status → launch-if-offline →
open own tab → pin every command `--target` — the PR #337 rules). No text
change beyond becoming a named branch.

**auto-lane branch (complete, executable):**

```bash
# resolver as today, then:
"$LAUNCH" --auto-lane --headless   # LITERAL --headless: the routing contract
    # (§2.5) promises a Quick-Invoke-chosen lane is ALWAYS headless, and the
    # arg outranks an inherited LOOK_HEADLESS=0 that would otherwise flip the
    # lane headful (R1-F5 v2). No URL arg — navigation is explicit below.
# Parse CDP_PORT=<port> from the contract lines (LANE_REUSED tells you whether
# the browser kept its previous state).
# If a URL was parsed from $ARGUMENTS (skip the navigate when there is none —
# the lane sits at about:blank):
CDP_PORT=<port> python3 "$CDP" navigate "<parsed URL>" --wait load   # loader-aware,
    # blocks until OUR navigation's lifecycle completes (R3-F2) — fresh AND reused
CDP_PORT=<port> python3 "$CDP" screenshot /tmp/jaine-look.jpg
```

`navigate --wait` is the loader-aware primitive cdp.py already ships (the
/drive verify-core navigate-wait — filters lifecycle events by OUR loaderId);
its behavior against delayed pages is behaviorally covered by the existing
engine e2e (`test_e2e_drive.py`), so the recipe needs the structural `--wait`
pin (§8.3), not a duplicate delayed-page e2e. A task-relevant extra wait
(`wait SELECTOR` / `--js`) stays available for async content, as everywhere in
this skill.

The no-URL invocation (`/bulldozer:look` with only a task description, or an
ambiguous request routed here) skips the navigate+sleep pair — that path is
part of the branch text, not an implied special case (R3-F3).

Every cdp.py call in this branch carries the literal `CDP_PORT=<port>` prefix
(shell state does not persist across Bash calls — #221; the port comes from the
contract output, there is no env fallback). `--target` pinning is NOT required
in the lane (no user tabs exist there); the branch says so to keep the 9333
rule's weight where it belongs. Launching without the URL argument makes fresh
and reused lanes uniform: navigation is always an explicit cdp.py step, so the
`LANE_REUSED=1` case cannot silently skip it.

**Structural boundedness (R1-F4):** the two branches are delimited by fixed
markdown headings — `### 9333 branch (user context / co-browsing / headful)`
and `### Auto-lane branch (agent-own tasks)`, each ending at the next `###` /
`##` heading — so the §8.3 test can EXTRACT the auto-lane branch text and
assert the invariant over EVERY `"$CDP"` occurrence inside it, not just spot
phrases.

Frontmatter `description` gains the mechanism pointer: "…goes to an isolated
lane instead (launch.sh --auto-lane)". The shared-mode 9333 rules from PR #337
are not touched. The zsh hint example (§5) lands in the Quick Reference
`--target` note (one line), per the issue's "строка-пример в SKILL.md".

## 7. Alternatives considered

- **Fixed-port hash range (the issue's sketch, v1 of this spec)** — rejected:
  `DevToolsActivePort` is not written on fixed ports (R1-F1, empirically
  confirmed), so fixed ports have no in-profile identity proof and the
  probe→bind window is a real stolen-port race; a range also costs registry
  edits, a probe, and an exhaustion path. Port 0 provides identity for free.
- **Reuse `CDP_PORT=0 --automation` ephemeral lanes** — rejected: CfT binary +
  `--enable-automation` + mktemp-per-call (no session reuse); /look wants stock
  Chrome rendering. The readiness mechanics and contract grammar ARE reused.
- **Locking for the parallel-launch race** — rejected: Chrome's user-data-dir
  singleton already serializes; the loser fails loud and the next call reuses
  (§4.5).
- **cdp.py-side lane defaulting** — rejected: constraint 4 (cdp.py defaults
  must not change; env-less calls keep hitting 9333).
- **Env alias `LOOK_AUTO_LANE`** — rejected: one spelling keeps the exclusion
  matrix small; the flag is agent-typed, not fixture-threaded.

## 8. Test plan (TDD)

**Session id (R3-F1 v2):** `CLAUDE_CODE_SESSION_ID` is NOT added to any scrub
list: conftest overwrites it with the deterministic test sentinel at import
time AND lists it in `PROTECTED_ENV_VARS`; the `test_env` guard rejects BOTH
dropping and emptying it (F11). Tests get determinism from the sentinel for
free. The single PPID-fallback case goes through the guard's OWN extension
point — a `CENTRAL_ALLOWLIST["unsafe_env"]` entry
`("CLAUDE_CODE_SESSION_ID", "test_launch.py", "auto-lane PPID-fallback
derivation — dry-run only, no log writer runs in the child")` + `test_env(...,
unsafe_allow=("CLAUDE_CODE_SESSION_ID",))` in that one test — NEVER by
mutating the env dict after `test_env` returns (that would bypass the D3a
contract). The justification is honest: the case is a DRY-RUN (no launch, no
`_log_lane`, nothing writes a stable-log line whose session attribution could
be lost).

**Parallel isolation (R6-F1):** every auto-lane test passes its OWN
`TMPDIR=<tmp_path>` into the child env (`TMPDIR` is not a protected var), so
each test derives a DISJOINT `look-lane-…` profile even though the session
sentinel is shared across xdist workers — decoys, stale files, chmods and
pkills cannot cross test boundaries (house doctrine: parallel-safe by
design). Only the deliberate concurrent-race test (§8.4.7) shares one TMPDIR
between its OWN two launches. A cheap pair test pins the mechanism: two
dry-runs with different `TMPDIR` values report different `profile=` lines.

### 8.1 `tests/test_launch.py` (dry-run harness, offline)

1. **No-flag golden:** default dry-run FULL stdout (config keys + argv) pinned
   byte-for-byte (new test; catches ANY additive drift, incl. an accidental
   `auto_lane=` key — R1-F2).
2. `--auto-lane` → `auto_lane=1`, `port=0`, `profile=$TMPDIR/look-lane-<key8>`,
   `profile_overridden=1`, `headless=1`, `auto_lane_reuse=0` (scrubbed env, no
   live lane).
3. Determinism: the conftest sentinel (already in env) → identical profile on
   two runs; golden `key8` equals the cksum formula (computed in-test via
   subprocess `cksum` over the sentinel value); PPID fallback: env built with
   `test_env(set_vars={"CLAUDE_CODE_SESSION_ID": ""},
   unsafe_allow=("CLAUDE_CODE_SESSION_ID",))` under the pinned allowlist
   entry (§8 preamble) → profile still well-formed
   (`look-lane-[0-9a-f]{8}`) and differs from the sentinel-derived one.
4. Headless precedence: `--auto-lane --headful` → `headless=0`;
   `LOOK_HEADLESS=0` env → `headless=0`; `LOOK_HEADLESS=1` → `headless=1`;
   env absent → `headless=1`.
5. Exclusions (presence semantics + ATTRIBUTION, R2-F2): env `CDP_PORT` ∈
   {9350, 9333, 0, ""} → error each, and the stderr is the AUTO-LANE error
   (mentions `--auto-lane`), NOT the SP4 ephemeral-gate text — pins the
   Phase-B preflight ordering; env `CDP_PORT=abc` (legacy-invalid) → error
   with the LEGACY validation text, exit 1 — pinned as the accepted garbage
   outcome (§4.1 attribution contract: the invariant is no-launch); combined
   `CDP_PORT=9350 LOOK_PROFILE_DIR=<daily profile> --auto-lane` → exit 1 with
   the LEGACY #160 daily-profile text — pinned safety-gate carve-out (R2-F2
   v3); env `LOOK_PROFILE_DIR` ∈ {/tmp/x, ""} → auto-lane error;
   `--automation` → error mentioning `CDP_PORT=0 --automation`;
   `LOOK_AUTOMATION=1` → error.
6. Composition: `--auto-lane --insecure` → `insecure=1`;
   `--auto-lane --cert-spki=<valid pin>` → `cert_spki` set.
7. Guards: backslash-TMPDIR → error; TMPDIR symlinked so the profile resolves
   into the daily profile → error (fail-closed §4.3.4).
8. Bash 3.2: at least the flag+exclusion+golden-key8 cases run under
   `/bin/bash` (harness `bash="/bin/bash"` param, existing pattern).
9. Log: a failed auto-lane launch (nonexistent `CHROME_BIN`) writes `lane-fail`
   with `auto_lane=1`; a no-flag failure line carries NO `auto_lane` key.
10. Window placement (R1-F3): `--auto-lane --headful` dry-run →
    `window_position` equals the key8 formula AND ARGV contains the derived
    `--window-position=` token; `--auto-lane` (headless) unaffected.
11. Stale-evidence fail-closed (R1-F1, real-launch mode with a benign
    `CHROME_BIN=/usr/bin/true`): pre-create `DevToolsActivePort` in the derived
    profile and make the dir un-removable-from (`chmod 555`) → exit 1 BEFORE
    any spawn, stderr names the stale file (the rm-guard is the FIRST profile
    mutation — §4.5 — which is what makes this case reach it); restore
    permissions in teardown.
12. Cmdline signature (R2-F1, offline, via `auto_lane_reuse_reason`): a decoy
    main process whose argv carries `--user-data-dir=<derived profile>` plus
    signature flags (e.g. `python3 -c 'import time; time.sleep(30)'
    --user-data-dir=… [--headless=new] [--disable-web-security] …`, no
    `--type=`) differing from the request in exactly ONE field —
    `headless`-only, `insecure`-only, `cert_spki`-only — →
    `reason=config-mismatch` for EACH (catches an implementation that omits a
    field from the derivation; argv[0] is NOT compared — §4.4); a decoy
    carrying a `--type=` flag → NOT a main process → `reason=no-process`;
    full-match decoy but no `DevToolsActivePort` → `reason=unhealthy`; no
    decoy → `reason=no-process`. The live reuse/restart half is §8.4.
13. Identity binding (R1-F1, offline): signature-matching decoy + a
    `DevToolsActivePort` whose line 1 names a port served by a TEST-OWNED
    fake HTTP endpoint answering `/json/version` with a
    `webSocketDebuggerUrl` uuid that does NOT equal the file's line 2 →
    `reason=identity-mismatch`, `auto_lane_reuse=0` — the stale-file /
    recycled-port impostor is never reused; same setup with the uuid MATCHING
    line 2 → `reason=ok`, `auto_lane_reuse=1` (proves the comparison, not
    just the rejection).

### 8.2 `tests/test_cdp.py` (offline)

- Single-token flag-like command (`"--target abc123"` as ONE argv element) →
  stderr line 1 byte-identical to today's `Unknown: …`, line 2 = the hint;
  exit 1.
- Non-flag unknown command (`frobnicate`) → NO hint line; exit 1.
- Bare `--target` (no selector) → existing missing-selector error, NO hint
  (regression pin for the parser path).

### 8.3 `tests/test_skill_prompts.py` (structural)

- Routing section appears BEFORE the first `cdp.py" status` occurrence in
  SKILL.md (ordering assertion — R1-F4).
- **Branch-bounded all-calls invariant (R1-F4):** extract the auto-lane branch
  text between its fixed heading (`### Auto-lane branch`) and the next
  `###`/`##` heading; EVERY line in that slice containing `"$CDP"` must match
  the `CDP_PORT=` prefix form (regex over each occurrence — zero unprefixed
  `"$CDP"` invocations of ANY command: status, navigate, open, screenshot,
  js, …). Extraction failure (heading missing/renamed) fails the test — the
  marker is part of the contract.
- The auto-lane branch contains `--auto-lane`; its launch line carries the
  LITERAL `--headless` token (R1-F5 v2 — the arg outranks an inherited
  `LOOK_HEADLESS=0`) and NO URL argument (navigation is an explicit cdp.py
  step — §6); the branch's `navigate` line carries `--wait` (loader-aware
  readiness, R3-F2); the no-URL conditional wording ("skip … when there is
  none") is present (R3-F3).
- **Routing-table pinning (R1-F4 v3):** within the routing section, assert the
  three classes land under the right branch: the 9333 routing bullet contains
  the user-context keywords (`куки`/`логин` or cookie/login, co-browse, and
  the headful/window-intent wording); the auto-lane bullet contains `file://`
  and `localhost`; the ambiguous rule sentence explicitly names auto-lane as
  the default AND the announce instruction ("работаю в изолированной lane"
  / port announcement) is present. An implementation that keeps the headings
  but routes ambiguous work to 9333 fails these assertions.
- The 9333 branch retains the PR #337 markers (open-own-tab + `--target` rule)
  — pins that the shared branch survived the restructure.
- Routing rule lists headful/window intents under the 9333 branch (R1-F5).
- Frontmatter description mentions `--auto-lane`.

### 8.4 `tests/test_e2e.py` (Chrome-required suite)

1. Fresh: `--auto-lane` (no URL) → contract lines parse; `LANE_REUSED=0`;
   `CDP_PORT=<port> cdp.py status` → ONLINE; `navigate` + `title` work.
2. Reuse: second `--auto-lane` invoke (same env session id) → `LANE_REUSED=1`,
   same browser PID (pgrep by profile before/after), page navigated in step 1
   still current (title unchanged) — process identity + page state, not port
   equality (R1-F6).
3. Config-mismatch restart (R2-F1), both insecure directions: third invoke
   with `--auto-lane --insecure` → `LANE_REUSED=0`, NEW PID, and the new
   Chrome's cmdline (ps) CONTAINS `--disable-web-security`; fourth invoke
   plain `--auto-lane` → `LANE_REUSED=0`, new PID again, cmdline does NOT
   contain `--disable-web-security` — the stale-flag survival case; the
   cmdline IS the signature, so these ps assertions are the signature
   assertions. (Per-field signature
   coverage for headless/cert lives in the offline §8.1.12 cases — the
   e2e exercises the security-critical insecure pair; a headful e2e would
   flash a GUI window, and a cert-pin flip adds a third full relaunch for a
   transition the offline per-field cases already pin.)
4. Unhealthy-relaunch: kill the lane's Chrome, leave the stale
   `DevToolsActivePort`, re-invoke → fresh launch succeeds (`LANE_REUSED=0`,
   new PID, file rewritten).
5. Teardown: `pkill -f -- "$LANE_KILL_MATCH"` → port released (poll), profile
   dir remains.
6. Log assertions: `lane-start`/`lane-reuse` lines carry `auto_lane=1`;
   the config-mismatch restart logs `lane-stop … reason=config-mismatch`.
7. Concurrent first-launch race (R2-F1 v6 — DETERMINISTIC via a spawn
   barrier; makes the post-readiness verification load-bearing): the test
   sets `CHROME_BIN` to a wrapper script that (1) touches a per-invocation
   `spawn-reached-<n>` marker, (2) blocks until a shared `go` file appears,
   (3) `exec`s the real Chrome with `"$@"`. The wrapper is signature-sound
   because argv[0] is NOT a signature field (§4.4) — the compared fields
   (headless/insecure/cert) pass through `"$@"` verbatim, so the survivor's
   cmdline carries exactly the winner's flags. On a FRESH shared TMPDIR start `--auto-lane`
   (plain) and `--auto-lane --insecure` via `subprocess.Popen`; WAIT for both
   `spawn-reached` markers — this PROVES both launchers passed pass 0 and
   both pre-spawn guards before any Chrome exists (the sequential
   mismatch-restart interleaving is structurally excluded) — then create
   `go`. Both Chromes start, the user-data-dir singleton picks a winner;
   deterministic assertions: EXACTLY ONE call prints a contract, and that
   contract's surviving main-browser ps signature equals THAT call's request;
   the other call exits 1 with the survivor-mismatch LANE_FAIL and NO
   contract; a follow-up plain `--auto-lane` invoke never reuses a survivor
   carrying `--disable-web-security` (config-mismatch restart, ps
   re-checked).

### 8.5 Structural (registry)

- Assert `tests/conftest.py` port-registry block and `test_e2e_lanes.py` range
  assertion are UNCHANGED by this feature (no new fixed ports) — a cheap grep
  pin documenting §3's "no registry edits" claim.
