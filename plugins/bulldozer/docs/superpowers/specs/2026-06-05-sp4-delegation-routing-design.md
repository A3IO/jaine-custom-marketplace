# SP4: Subagent Delegation + Empirical Model-Routing Calibration — Design

*Status: design (per-SP spec under the umbrella `2026-06-04-look-drive-test-command-design.md` §5 SP4).*
*Date: 2026-06-05*
*Decisions locked by Crys (AskUser, 2026-06-05): ephemeral-by-construction lanes; Workflow tool for calibration; compact fixed corpus.*

## 0. TL;DR

Two deliverables, one dependency arrow:

1. **Ephemeral lanes** (infra): a subagent gets its own CDP lane with **zero coordination** — `--remote-debugging-port=0` (the OS picks a guaranteed-free port) + `mktemp -d` profile (unique by construction). The unique profile IS the ownership token; `pkill`-by-profile can only ever kill its own browser. No allocator state, no lock files. Closes holes R1-H + R2-R *structurally* instead of via the spec's literal "allocator lock + ownership token" (intent satisfied, mechanism simpler — Crys approved the deviation).
2. **Model-routing calibration** (experiment): a compact fixed corpus of drive tasks × {haiku, sonnet, opus} × repeats, run through the Workflow tool — each agent provisions its own ephemeral lane (the experiment is the infra's first consumer). Output: a routing table in drive SKILL.md + a data-backed circuit-breaker verdict (keep 3 or change).

## 1. Empirical basis (spike, 2026-06-05, this session)

Verified live on pinned CfT 149.0.7827.54 (headless, automation flags, temp profile):

- `--remote-debugging-port=0` → OS assigned an ephemeral port (observed 63266; macOS ephemeral range 49152-65535 — can never collide with 9333 or the 9340-9349 interactive range).
- Chrome writes `<profile>/DevToolsActivePort`: **line 1 = port, line 2 = ws path; no trailing newline** (parse with `head -1` / `read`, not `$(cat)`-and-split assumptions).
- `curl http://localhost:<port>/json/version` answers immediately after the file appears (single observation; implementation still retries).
- `pkill -f -- "--user-data-dir=<mktemp-profile>"` killed only that instance; port released.

## 2. Part A — ephemeral lanes

### 2.1 launch.sh: ephemeral mode

- **Trigger:** `CDP_PORT=0` (explicit, parseable; no new flag — port semantics stay in the port variable).
- **Scope (fail-loud edges, R1-F1/R1-F2):** `CDP_PORT=0` is supported ONLY in the automation lane:
  - `CDP_PORT=0` without `--automation`/`LOOK_AUTOMATION` → `ERROR` + exit 1 (no `/look` use case; YAGNI).
  - `CDP_PORT=0` WITH a caller-supplied `LOOK_PROFILE_DIR` (or `--profile`) → `ERROR` + exit 1. A caller-supplied profile breaks the uniqueness invariant (two subagents passing the same dir would share a profile and kill each other) — the entire point of ephemeral mode is that the launcher owns the profile.
- **Profile derivation (exact invariant):** in the automation block, when `CDP_PORT == 0`, the temp-profile rule changes from the deterministic `jaine-drive-${CDP_PORT}` (which would collide as `jaine-drive-0` for every ephemeral lane) to `mktemp -d "${TMPDIR:-/tmp}/jaine-drive-eph-XXXXXX"` — unique by construction. After derivation the code sets `PROFILE_OVERRIDDEN=1`, **the same mechanism by which the existing automation temp profile satisfies the `--insecure`/`--cert-spki` explicit-isolated-profile requirement** (launch.sh: "the auto-temp profile below satisfies insecure's explicit-isolated-profile requirement"). The existing TMPDIR backslash/newline guard and `_resolves_to_daily_profile` check apply unchanged.
- **Gate invariant (exact, not prose):** all three gates keep their current shape and pass for the ephemeral lane via existing checks, none weakened: automation's `CDP_PORT == 9333` reject (0 ≠ 9333), `_resolves_to_daily_profile(mktemp-dir) == "0"`, and insecure/cert-spki's `PROFILE_OVERRIDDEN` requirement (satisfied by the derivation step above). Structural tests assert each composition (§2.3).
- **Readiness + contract:** after spawn, wait for `<profile>/DevToolsActivePort` (poll ≤10s), read line 1, then confirm CDP with one short curl retry loop. Print a parseable contract on stdout (final lines):
  ```
  CDP_PORT=63266
  LANE_PROFILE=/var/folders/.../jaine-drive-eph-Ab3dEf
  LANE_KILL_MATCH=--user-data-dir=/var/folders/\.\.\./jaine-drive-eph-Ab3dEf($|[[:space:]])
  LANE_BROWSER_BIN=/0/.jaine/.browser/cft/current/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing
  ```
  `LANE_KILL_MATCH` is the launcher-built, `_escape_ere`-escaped pkill pattern (R1-F3) — consumers paste it verbatim into `pkill -f -- "<LANE_KILL_MATCH>"` and never hand-roll regex escaping. `LANE_BROWSER_BIN` is the resolved binary the launcher actually spawned (R3-F1): `/json/version`'s Browser string cannot distinguish CfT from stock Chrome at the same version (spike: CfT reports plain `Chrome/149.0.7827.54`), so binary-path identity is the only reliable CfT check — consumers verify it lies under `/0/.jaine/.browser/cft/`. Failure (file never appears / curl never answers) → fail-loud `LANE_FAIL: …` + exit 1, kill the spawned pid, rm the mktemp profile.
- **Restart contract:** the pre-launch `pkill`-by-profile is a structural no-op on a fresh mktemp profile (nothing matches). *Implementation delta (PR #178 code-review):* it IS skipped conditionally for ephemeral lanes — not for the pkill itself but for the `sleep 1` that follows it (1s × every lane × every calibration run is material); the same review also gates the post-spawn settle `sleep 3` off for ephemeral lanes, whose liveness is already proven by the DevToolsActivePort + `/json/version` wait.
- **Non-goals:** interactive lanes 9340-9349 unchanged (humans can eyeball deterministic ports); `/look` 9333 path untouched; port registry in conftest.py keeps governing only *fixed* ports.

### 2.2 drive SKILL.md: subagent delegation section

Replaces the current "SP4 will automate lane allocation" placeholder in the Two-modes section:

- Delegation prompt template: the subagent **launches its own lane with a clean-env guard** (R3-F1): strip ALL lane env vars before launch — the canon is conftest.py's `LANE_ENV_VARS` hermeticity tuple (`CDP_PORT, LOOK_PROFILE_DIR, LOOK_HEADLESS, LOOK_INSECURE, LOOK_DRY_RUN, CHROME_BIN, LOOK_AUTOMATION, CHROME_APP_NAME, LOOK_CERT_SPKI`), then set only what the lane needs explicitly:
  ```bash
  env -u LOOK_PROFILE_DIR -u LOOK_INSECURE -u LOOK_DRY_RUN -u CHROME_BIN \
      -u LOOK_AUTOMATION -u CHROME_APP_NAME -u LOOK_CERT_SPKI \
      CDP_PORT=0 LOOK_HEADLESS=1 "<plugin>/skills/look/scripts/launch.sh" --automation
  ```
  Not just `CHROME_BIN`: an inherited `LOOK_DRY_RUN=1` would prevent the launch entirely, `LOOK_INSECURE`/`LOOK_CERT_SPKI` would silently alter browser flags, and a stray `CHROME_APP_NAME` would pollute later cdp.py calls. (`launch.sh` deliberately honors env-provided values — hermeticity is the caller's job, exactly as the test harness does it.) It then parses `CDP_PORT=`/`LANE_PROFILE=`/`LANE_KILL_MATCH=`/`LANE_BROWSER_BIN=` from stdout and **runs the hole-D pre-flight before trusting any result**: `LANE_BROWSER_BIN` must lie under `/0/.jaine/.browser/cft/` (binary-path identity — the `/json/version` Browser string CANNOT distinguish CfT from stock at the same version, so a version check alone is insufficient) — mismatch → STOP and report, never proceed on the wrong browser. The pre-flight `curl /json/version` output is still captured as `cmd-00.log` (liveness + post-hoc record). Then threads BOTH `CDP_PORT=<actual>` + `CHROME_APP_NAME` into every cdp.py call (lane contract unchanged), tears down by `pkill -f -- "<LANE_KILL_MATCH verbatim>"` — the launcher-escaped pattern, never a hand-built regex (R1-F3).
- The main session **never picks ports for subagents** — that was the collision source.
- Subagents are ALWAYS autonomous (§4.4 unchanged); delegation prompt hard-codes it.
- Model-routing table (Part B output) lives here too.

### 2.3 Tests (TDD)

- `test_launch.py` (structural, `LOOK_DRY_RUN`): port-0 + automation passes the gate; mktemp profile pattern in dry-run output; `--insecure` composition on an ephemeral lane; dry-run marks the mode (`ephemeral=1`) — the full 4-line contract cannot appear in a dry-run (the port exists only after a real spawn), so contract verification is e2e territory (below).
- `test_e2e_cft.py` or new `test_e2e_lanes.py` (behavioral, self-skips without CfT): **two ephemeral lanes launched in parallel** → distinct ports, both CDP-alive, teardown of lane A leaves lane B alive (the direct hole-H regression test), `DevToolsActivePort` parse correctness.

## 3. Part B — calibration experiment

### 3.1 Corpus (compact fixed; fixtures-deterministic)

Two new fixture elements first (from dogfood #172): a **shadow-DOM component** and a **reactive re-insert element** (vanilla JS re-insert cycle imitating Alpine `x-if`) added to `drive-page.html`.

**Every task has an executable oracle (R1-F4):** each task ships a **frozen command manifest** — the exact cdp.py CLI invocations the agent must run (T10 fix-iterations may add edit/re-verify cycles, but the *verify* commands themselves are fixed by the manifest). The agent returns the RAW verdict-marker lines those commands printed (`verdict_lines`) plus its self-assessment; **grading is external** — a deterministic post-processing script (not the agent) computes:

```
graded_success = expected_markers ⊆ markers(run_dir/cmd-*.log)             # parsed from runner-owned logs (§3.2), NEVER from agent-returned fields
               AND self.classification == oracle.expected_classification   # honest-classification tasks (T2/T4/T6)
               AND integrity_check passes                                   # T10 only, see below
```

Self-assessment is recorded but never the grade — a haiku that hallucinates "all good" past a CLICK_REQUIRE_TRUSTED_FAIL must score 0, and that divergence is itself the honesty signal.

Marker strings below are the LITERAL strings verify-core emits today (verified against `cdp.py` this session — R1-F4 r3): the gate prints `CONSOLE_GATE_OK` (not `_PASS`); trusted click prints `clicked <tag> (trusted)` — the `(trusted)` suffix is the oracle, because the untrusted fallback also exits 0 when `--require-trusted` is absent (all manifest clicks use `--require-trusted`, which removes the fallback, but grading still keys on the suffix, never on exit code alone).

| # | Task (one subagent run) | Class | Oracle: expected markers (external grade) |
|---|---|---|---|
| T1 | navigate --wait + console --gate on clean page | verify | navigate prints final URL + `loader=`; `CONSOLE_GATE_OK` (exit 0) |
| T2 | gate catches mid-flow error (error button) | verify | `CONSOLE_GATE_FAIL` with ≥1 exception leg; classification=page-error |
| T3 | assert async element (--visible --stable) | verify | `ASSERT_PASS` on `#async-elem` |
| T4 | flapping element → correct flaky diagnosis | verify | `ASSERT_FAIL` containing `flapped` (NOT `never true`); classification=flaky |
| T5 | delayed-enable → assert --actionable → click --require-trusted | verify | `ASSERT_PASS` (actionable) then `clicked <tag> (trusted)`, no `CLICK_REQUIRE_TRUSTED_FAIL` |
| T6 | occluded target → honest failure report | verify | `CLICK_REQUIRE_TRUSTED_FAIL` present; classification=not-actionable + explicit failure report |
| T7 | shadow DOM assert via --js (SKILL.md pattern) | verify | `ASSERT_PASS` on the `--js` shadowRoot expression |
| T8 | reactive re-insert assert via --js on state | verify | `ASSERT_PASS` on the `--js` state expression (selector-based assert would flap) |
| T9 | full mini-e2e: navigate→gate→assert→click→screenshot --bind→teardown | verify | all of: `loader=` token, `CONSOLE_GATE_OK`, `ASSERT_PASS`, `clicked <tag> (trusted)`, `BIND url=` line with matching `loader=`, port free after teardown |
| T10a | broken fixture copy (typo'd id → click fails) — agent fixes the local copy until green | fix-verify | final `ASSERT_PASS` + `clicked <tag> (trusted)` on the fixed copy; `iterations` = fix-verify cycles used |
| T10b | broken fixture copy (null-ref console error) — agent fixes until gate passes | fix-verify | final `CONSOLE_GATE_OK` on the fixed copy; `iterations` counted |

Runs: 9 verify × 3 models × 3 repeats = 81, plus 2 fix-verify × 3 models × **5 repeats** = 30 → **111 runs**. Fix tasks get 5 repeats (not 3) because the circuit-breaker calibrates on **fix-verify iteration counts** (R1-F5) — verify-only tasks never iterate, and 3 repeats per cell is too thin for a tail statistic.

**Honest-classification tasks (T2/T4/T6):** the correct outcome is a *failure or flaky diagnosis*, so the oracle includes the agent's classification, not just markers: T2 expects `classification=page-error`, T4 expects `classification=flaky` (a `never true`/"element absent" answer grades 0), T6 expects `classification=not-actionable` plus an explicit failure report. The classification enum lives in the result schema (§3.2).

**T10 integrity check (anti-gaming):** broken fixtures are pre-built and committed; each carries a frozen verify manifest (e.g. `assert '#target-btn' --actionable` + `click '#target-btn' --require-trusted`) targeting the ORIGINAL element id. Grading re-runs the manifest's verify commands against the agent's fixed copy: green manifest + target element still present (`assert '#target-btn'` is part of the manifest, so deleting the button cannot pass) = genuine fix. The agent's own ad-hoc commands never substitute for the manifest.

### 3.2 Mechanics

- **Workflow tool** script: `pipeline(tasks × models × repeats)`, `agent()` with `opts.model` per run, batches of 3-4 with barriers between batches (workflow-swarms throttle doctrine; schema-agent returning null ≈ rate-limit signal → sequential retry).
- Each agent: provisions its own ephemeral lane (Part A), runs its task's **frozen command manifest** against the shared fixture server. **Runner-owned capture (R1-F4 r3):** the orchestrator pre-creates `$EXPERIMENT_DIR/runs/<task>-<model>-<rep>/` and the delegation template wraps every manifest command in the standard capture form — stdout+stderr to `cmd-NN.log` plus a trailing `EXIT=<code>` line (`{ <cdp.py …>; echo "EXIT=$?"; } > cmd-NN.log 2>&1` — then `cat` it back for the agent's own eyes). **Grading reads ONLY these log files** — markers and exit codes come from artifacts the commands themselves wrote, never from the agent's retelling. The agent returns a **schema-forced** result:
  ```
  {task_id, model, run_dir: str,                    # where the cmd-NN.log files live — the grading input
   verdict_lines: [str],                            # agent's copy of the marker lines (cross-check convenience only)
   self_success: bool,                              # agent's own verdict — honesty signal only, never the grade
   classification: "pass"|"page-error"|"flaky"|"not-actionable"|"absent"|"other",  # honest-classification input (T2/T4/T6)
   iterations: int, wall_s: float, notes: str}
  ```
- **External grading (R1-F4):** a deterministic script (`scripts/` or inline in the analysis step) parses `run_dir`'s `cmd-NN.log` files and maps each run to `graded_success` via the oracle table — marker subset + `EXIT=` codes + expected classification (T2/T4/T6) + T10 integrity re-run. A run whose `run_dir` is missing/empty grades 0 (capture is part of the task). The routing table derives from `graded_success` only; the honesty-delta table derives from `self_success` vs `graded_success`.
- **Fixture server:** one read-only ThreadingHTTPServer on a fixed registry port (new entry, 9361 — transient experiment block), started before the workflow, serving `tests/fixtures/`. Shared by all agents safely (read-only). T10* agents copy the broken fixture to their own tmpdir and serve/file:// it locally — they must not write to the shared tree.
- **Metrics:** primary = graded_success rate, iterations (fix tasks; see breaker rule), wall_s, honesty delta (`success` vs `graded_success` divergence per model). Tokens = best-effort post-hoc from agent transcripts (`agent-*.jsonl`, session-reader stats); `budget.spent()` gives the aggregate.
- **Corpus freeze (R2-F1):** task manifests, broken fixtures, and the oracle table are **frozen at a named commit BEFORE the first calibration run** (the analysis doc records the SHA). The pre-matrix pilot is an *infrastructure smoke only* (lane provisioning + one trivial navigate on one model) — its runs are excluded from the data and it must NOT touch task content. After the freeze, task difficulty is never adjusted: if a task proves defective mid-experiment (fixture bug, unsolvable as authored), it is **dropped entirely across all models** and the drop is recorded — never retuned, because retuning against the models under evaluation contaminates the predeclared statistics.
- **Circuit-breaker decision rule (predeclared, R1-F5):** fix-verify runs execute with the current breaker = 3. A run that goes green records `iterations ∈ {1,2,3}`; a run that hits the breaker without green is **censored** (we know only `iterations > 3`). Report per-model: green-iteration distribution + censored count (n=10 fix runs per model). Rule:
  - censored = 0 for a model AND max(green iterations) ≤ 2 → breaker 3 has headroom for that model;
  - censored = 0 AND max = 3 → keep 3, flag "no headroom" in the analysis;
  - censored ≥ 1 for a model → **second pass**: re-run that model's censored task(s) once with breaker raised to 5 to measure the tail; if green at 4-5 → recommend model-dependent breaker (or "don't route fix tasks to this model" — routing beats raising); still censored at 5 → the task/model pair is a routing exclusion, not a breaker problem.
  - The breaker is never lowered below 3 regardless of data (conservative floor). n=10/model is acknowledged as small — the verdict is recorded with its n, and the analysis doc states the re-run trigger (a real-world breaker dispute or a new model) rather than overclaiming significance.
- **Analysis output:** `docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md` — per-task×model graded table, routing rules derivation, circuit-breaker verdict per the predeclared rule, honesty-delta table.

### 3.3 Routing rules (shape of the output — values come from data, not guessed)

Expected shape in SKILL.md: "delegate verify-only smoke to `haiku` if its success ≥ X%, else `sonnet`; fix-verify loops → `sonnet`/`opus` by iteration distribution; main-session co-pilot stays on session model." The actual cutoffs are the experiment's output — the spec deliberately does not pre-commit them (umbrella §5: "output of the experiment, not guessed").

## 4. Ship list

| File | Action |
|---|---|
| `skills/look/scripts/launch.sh` | ephemeral mode (CDP_PORT=0): mktemp profile, DevToolsActivePort wait, stdout contract, fail-loud |
| `skills/drive/SKILL.md` | Subagent delegation section + routing table; placeholder text replaced |
| `tests/fixtures/drive-page.html` | + shadow-DOM element, + reactive re-insert element |
| `tests/test_launch.py` | + ephemeral structural tests |
| `tests/test_e2e_lanes.py` (or extend `test_e2e_cft.py`) | parallel-lanes e2e (hole-H regression) |
| `tests/conftest.py` | port-registry comment: 9361 experiment fixture server |
| Workflow script (session artifact) | calibration runner (persisted under session dir; analysis doc references runId) |
| `docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md` | experiment results + derivations |
| `CLAUDE.md` | /drive architecture + skills table + changelog |
| umbrella spec §5 | SP4 row → ✅ DONE (PR2, after the experiment ships the routing table) |

## 5. Out of scope

- Lane allocation for **interactive** (human-watched) sessions — stays manual 9340-9349.
- localStorage seeding, keyboard/drag trusted input (#149), Playwright (SP3) — unchanged backlog.
- Cross-machine delegation (kosm4/itm4) — not in this SP.
- Re-running the calibration continuously — one-shot experiment; re-run criteria (new models, breaker disputes) noted in the analysis doc.

## 6. Risks

| Risk | Mitigation |
|---|---|
| DevToolsActivePort timing flake under load | ≤10s poll + curl retry; fail-loud LANE_FAIL (never a silent default port) |
| Workflow agents lack Bash permissions in some modes | infrastructure-smoke pilot (1 trivial run on one model, excluded from data — R2-F1 scope) before the 111-run matrix |
| 529 overload on fan-out | batches 3-4 + barrier cool-down + sequential null-retry (workflow-swarms) |
| T10 fix tasks too easy/hard → useless breaker data | author difficulty BEFORE the freeze (sanity-solve by hand, no model runs); post-freeze a defective task is dropped across all models, never retuned (R2-F1) |
| Shared fixture server port collision | registry entry + pre-flight curl check, same discipline as e2e |
