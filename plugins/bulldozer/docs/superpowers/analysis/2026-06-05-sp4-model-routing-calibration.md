# SP4 Model-Routing Calibration — Results

**Freeze SHA:** `75bac59b8a71412f2a6344977f7b0f14a40a8b7d` (bulldozer/main, 2026-06-05).
Corpus frozen at this commit before the first run; task content is immutable
(a defective task is dropped across all models, never retuned — spec R2-F1).

**Experiment:** 11 tasks × {haiku, sonnet, opus}, verify tasks ×3 repeats, fix-verify
tasks ×5 = **111 runs**, via the Workflow tool (`sp4-calibration-matrix.js`), each agent
a `/bulldozer:drive` calibration runner provisioning its own ephemeral CfT lane per the
`skills/drive/SKILL.md` "Subagent delegation" block. Run `wf_b327f1bf` — 111 agents,
9.05M output tokens, 55.5 min wall. 0 null cells; 0 leaked browsers after the run.

**Grading:** external, from runner-owned `cmd-NN.log` files only
(`skills/drive/scripts/grade_run.py`) — agent-returned fields never grade. T10 fix-verify
cells additionally gated on an orchestrator **integrity re-run** (a fresh lane re-runs the
manifest's verify commands against the agent's `fixed-copy.html`); **all 30 integrity
re-runs passed** — every model's repair actually works.

---

## Graded success (post-fix; see "Grader bug" below)

| task  | oracle class   | haiku | sonnet | opus |
|-------|----------------|-------|--------|------|
| T1    | pass           | 1/3   | 3/3    | 3/3  |
| T2    | page-error     | 0/3   | 3/3    | 3/3  |
| T3    | pass           | 3/3   | 3/3    | 3/3  |
| T4    | flaky          | 2/3   | 3/3    | 3/3  |
| T5    | pass           | 2/3   | 3/3    | 3/3  |
| T6    | not-actionable | 0/3   | 3/3    | 3/3  |
| T7    | pass           | 3/3   | 3/3    | 3/3  |
| T8    | pass           | 2/3   | 3/3    | 3/3  |
| T9    | pass           | 2/3   | 3/3    | 3/3  |
| T10a  | pass (fix)     | 3/5   | 3/5    | 4/5  |
| T10b  | pass (fix)     | 3/5   | 0/5    | 1/5  |
| **TOTAL** |            | **21/37** | **30/37** | **32/37** |

**Verify-only (T1–T9, excludes the ambiguous-oracle T10):** haiku **15/27**,
sonnet **27/27**, opus **27/27**.

---

## The headline: haiku's classification accuracy is a mirage

Naïve "classification matches oracle" reads haiku **32/37**, sonnet 30/37, opus 32/37 —
which would say haiku is *competitive*. It is not. haiku has a systematic **"pass" bias**:
it labels almost every run `pass` regardless of what happened. That bias *happens to match*
pass-heavy tasks and *mismatches* every task whose correct answer is a defect diagnosis:

| task (oracle) | haiku says | sonnet says | opus says |
|---|---|---|---|
| T2 (page-error) | `pass` ×3 ✗ | `page-error` ×3 ✓ | `page-error` ×3 ✓ |
| T6 (not-actionable) | `pass` ×2, `not-actionable` ×1 | `not-actionable` ×3 ✓ | `not-actionable` ×3 ✓ |

The same bias *flatters* haiku on T10b (oracle `pass`), where it scores 5/5 "pass" by
reflex while sonnet/opus report `page-error` (see next section). So the 32/37 is inflated
by a broken clock being right twice a day. The **verify-only graded score (15 vs 27 vs 27)**
is the honest model-quality signal: sonnet and opus diagnose; haiku rubber-stamps.

haiku's other failures are mechanical, not just classification:
- **preflight-malformed ×7** — it lost the `cmd-00.log` capture form (empty / no `EXIT=`
  trailer): the binary-identity pre-flight it was told to run verbatim.
- **improvised command sequences** — e.g. T10a-haiku-1 ran its *own* 5-command sequence
  (added a `console --gate`, an extra step) instead of pasting the 3 manifest lines, so
  the log set never matched the oracle.
- **lane died mid-cycle** (T10b-haiku-2: `ERROR: Browser not running on CDP port …`).

---

## T10b is an oracle-ambiguity, not a sonnet/opus failure

T10b = repair a page that throws a null-ref on load, then verify `console --gate` passes.
The oracle classification is `pass` (the *final* fixed state behaved). On the **classification
label**, sonnet labels the run `page-error` on all 5 repeats and opus on 4/5 — and they are
**not wrong**: the page *did* throw before they fixed it; they reported the journey, the
oracle wanted the destination. That divergence is the *only* thing failing them: their
**graded-success** is `0/5` (sonnet) and `1/5` (opus) **entirely** because the
`classification-mismatch` gate fires on that defensible label — not because any repair
failed. (Note the two metrics use the same `X/5` shape for opposite things here: opus
*labels* page-error 4/5 but *graded-passes* 1/5.) Ground truth from the forensic audit:
**28 of 30 fix-verify cells produced a real green repair cycle** (only `T10a-haiku-1` and
`T10b-haiku-2` never did — both haiku), and the **integrity re-run confirms all 30 fixed
copies work**. So on the repair task itself every model is near-perfect; the graded T10b
spread is the classification gate, not repair skill.

**Corpus lesson (for any future calibration round, not retuned now — R2-F1):** a fix-verify
task's classification oracle should accept *both* `pass` and the pre-fix defect class, or be
reworded to ask explicitly "the FINAL state". The load-bearing metric is graded markers +
exits + integrity, not the single-word classification — weight it accordingly.

---

## Two grader bugs surfaced by the live run (and fixed)

The experiment found two real defects in `grade_run.py` — both the same class (agent
*noise* false-failing a *real* repair), exactly what a live pilot is for (static review
can't validate agent-runnable behavior; the codex-competence-boundary doctrine).

1. **Empty trailing `iter-K`.** Three haiku agents went **green at iter-2**, then `mkdir`'d
   an **empty `iter-3/`** and stopped. The old rule "grade the highest-*numbered* `iter-K`
   cycle" graded the empty `iter-3` → `log-set-mismatch` → **false fail of a real repair**;
   the naïve dir-count also over-reported iterations vs the agent's honest self-report.
   **Fix:** grade the highest-K **complete** cycle (one whose `iter-K/` carries the full
   expected command-log set); `iterations_observed` counts complete cycles only.
2. **Debug noise at root.** `T10b-haiku-3` fixed the page (green cycle) but left
   `cmd-00-debug.log` / `cmd-00-retry.log` at the run root; the strict
   `root_names == ["cmd-00.log"]` check false-failed it with `log-set-mismatch`. **Fix:** at
   root, reject only a *manifest command* log (`cmd-01.log`..`cmd-NN.log` — the flat-layout
   mistake); tolerate non-manifest debug files. (`T10b-haiku-3` still grades fail, now for
   the *correct* reason — `preflight-malformed`: its `cmd-00.log` holds the launch contract,
   not the `EXIT=`-trailed pre-flight curl.)

**Fix (TDD, this PR): +5 grader tests** — `test_t10_empty_trailing_iter_*`,
`test_t10_partial_trailing_iter_skipped`, `test_t10_all_incomplete_grades_no_iterations`,
`test_t10_tolerates_extra_root_debug_files`, `test_t10_rejects_manifest_command_log_at_root`
(plus the 2 SKILL.md drift-guards = 7 new tests total).

**Effect on the numbers: zero pass/fail verdicts changed by either fix.** They corrected two
iteration *counts* (haiku-4/haiku-5: 2→1, honest) and several cells' *reason* strings (the
noise cells now grade their real cycle / the correct failure), but every cell's pass/fail is
identical pre- and post-fix — the noise cells still fail for independent legitimate reasons
(improvised sequence / malformed preflight / dead lane). The corrected grader neither
inflated nor deflated any score; it removed artifacts and hardened the grader for future
runs.

---

## Honesty delta (self-report vs ground truth)

| signal | haiku | sonnet | opus |
|---|---|---|---|
| self_success=true but **graded fail** | **15/37** | 7/37 | 5/37 |
| self-reported iterations ≠ filesystem (pre-fix) | 3 cells | 0 | 0 |

haiku overclaims success on **15 of 37** runs — it reports `self_success: true` while the
runner-owned logs say otherwise. This is the single strongest argument against routing
graded/calibration drive work to haiku: it cannot be trusted to grade itself. The
self-vs-filesystem iteration mismatches were all haiku, all caught by the
filesystem-counting design (peer-review F4 — never trust the agent's iteration self-report).

---

## Breaker verdict: **keep 3**

Predeclared rule (spec R1-F5): the filesystem count of **complete** cycles per cell;
*censored* = a fix-verify cell whose highest complete cycle is at the breaker floor (3)
**and** red; a second pass at breaker=5 runs only if censored ≥ 1; the floor 3 is never
lowered.

Across all 30 fix-verify cells the complete-cycle distribution is **{1: 10, 2: 19, 3: 1}**;
**4 cells reached iter-3** (`T10a-haiku-1`, `T10a-haiku-3`, `T10b-haiku-2`, `T10b-haiku-3`);
**0 censored**. The lone 3-cycle cell, `T10b-haiku-3`, ran the **full budget and went GREEN
on its 3rd attempt** (it still grades fail — for a *different*, legitimate reason: its
`cmd-00.log` holds the launch.sh contract, not the `EXIT=`-trailed pre-flight curl →
`preflight-malformed`; the repair itself is real). The two cells that never produced a
green cycle (`T10a-haiku-1`, `T10b-haiku-2`) did **not** reach 3 complete red cycles, so
neither is censored.

So the floor is exactly validated: a breaker of 2 would have cut off `T10b-haiku-3`'s
successful 3rd attempt, and no cell was censored at 3 (no cell went red at the floor).
**No second pass; breaker stays 3 — zero headroom, but the data positively justifies the
floor of 3 rather than merely tolerating it.**

> **Earlier-draft correction (caught in this PR's own `/code-review`):** the first draft of
> this section claimed "max 2 complete cycles, 0 cells reached iter-3, ≥1 headroom (1×3,
> 2×11)". That was **wrong** — `T10b-haiku-3` ran 3 complete cycles and 4 cells reached
> iter-3. The *verdict* (keep 3, 0 censored) is unchanged; the supporting numbers and the
> "headroom" framing are corrected above. The adversarial review of one's own analysis is
> the only reason this surfaced — recorded here rather than silently overwritten.

---

## Routing rules (derived from the data)

| Drive workload | Route to | Evidence |
|---|---|---|
| **verify-core** (navigate/gate/assert/click) + **any graded/calibration run** | **sonnet** | 27/27 verify, correct classification, honesty delta 7/37 (vs haiku 15/37); ~5× cheaper than opus at identical accuracy |
| **fix-verify** (iterative repair) | **sonnet** | reliable capture protocol + correct iteration discipline; opus 32 vs sonnet 30 total is within the T10b oracle-ambiguity noise |
| ~~haiku~~ — not recommended for graded drive | — | verify 15/27, "pass"-bias classification, preflight-malformed ×7, **overclaims 15/37** |
| ~~opus~~ — no benefit over sonnet | — | identical verify accuracy (27/27), no speed gain (median 6s vs 8s), ~5× cost |

**The sweet spot is sonnet.** opus buys nothing over sonnet on this workload; haiku is
too unreliable (both at the task and at self-assessment) for any run whose result is
trusted. Reserve haiku for throwaway, human-verified exploration only.

### Speed (coarse — `$SECONDS` resolution, agent-approximate)

| model | median | mean | max |
|---|---|---|---|
| haiku | 3s | 16.4s | 135s |
| sonnet | 8s | 20.3s | 120s |
| opus | 6s | 20.0s | 92s |

`wall_s` is the agent's own `$SECONDS`-granularity timing around its command block — usable
only for coarse comparison, not benchmarking. **It is 1-second-floored: 13 of 111 cells
report `wall_s=0`** (haiku 1, sonnet 5, opus 7 — sub-second command blocks), included in the
medians/means above. The lower-tier zeros slightly deflate sonnet/opus, which only
*strengthens* the conclusion that they are not slower than haiku in any way that matters.
haiku's lower median is irrelevant given its unreliability; sonnet and opus are
indistinguishable on speed.

---

## Reproducibility

- Raw results, grades, integrity verdicts, and the full per-cell log tarball:
  `.bulldozer/sp4-experiment-data/` (gitignored — `results.json`, `graded.json`,
  `integrity-verdicts.txt`, `runs-logs.tar.gz`, `integrity-rerun.sh`).
- Re-grade: `python3 skills/drive/scripts/grade_run.py --run-dir <cell> --task <T>
  --classification <agent's> [--integrity pass|fail]`.
- Workflow script: session `workflows/scripts/sp4-calibration-matrix.js`.
