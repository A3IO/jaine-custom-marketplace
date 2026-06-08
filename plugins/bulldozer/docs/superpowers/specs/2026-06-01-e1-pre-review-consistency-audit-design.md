# E1 — Pre-review consistency audit (bulldozer:check)

- **Status:** Design (brainstorm complete, evidence-backed) — 2026-06-01
- **Issue:** `A3IO/jaine-plugins#94` (umbrella RFC, E1 slice). E5/E6 pivot slice already shipped (#95→#128); this is the E1 item.
- **Author:** JAINE + Crís
- **Scope:** E1 only. E2 (post-review critique), E3 (reviewer continuation), E4 (adaptive cap), E5-rest (cluster classification), E6-rest (advanced triggers) stay open on #94.

---

## 1. Problem

`bulldozer:check` sends a spec/plan to an expensive external reviewer (codex, xhigh) round after round (~30–80s + tokens each). A meaningful share of what codex flags is **cheap self-consistency grime in the artifact** — a reference to a renamed file, a leftover stale version, one section contradicting another — that a reader checking the document against itself could have caught in seconds, without burning a codex round.

Worse: these defects are frequently **author-introduced between rounds** — a fix in one section leaves a sibling section stale, and codex re-finds it next round. The skill has no step that catches this before the round.

## 2. Evidence (why this is worth building)

Three empirical passes over the real bulldozer corpus on this machine (2026-06-01), all reading ONLY the frozen `.bulldozer` snapshots (verdicts + ledger), never the since-drifted on-disk artifacts:

**Value ceiling** — 40 spec/plan review sessions, 501 distinct findings classified (conservative rubric: "could a reader checking the doc against itself + cited siblings, with zero domain knowledge, have flagged this?"):

| Bucket | Count | % |
|---|---|---|
| **E1-catchable (self-consistency)** | **210** | **41.9%** |
| — internal_contradiction | 127 | 25.3% |
| — cross_spec_drift | 64 | 12.8% |
| — dead_ref | 13 | 2.6% |
| — stale_term | 6 | 1.2% |
| NOT-E1 (deep logic / design / bug / missing-req / test) | 291 | 58.1% |

~42% of real findings were E1-class. Dominant class = **internal_contradiction**, and per-session notes repeatedly attribute it to **mid-session author edits** (a fix conflicting with adjacent prose) — directly validating an **every-round** audit, not round-1-only. Distribution is **bimodal**: cross-referential plans/specs (trace tables, phased tasks) yield 50–100%; deep technical impl plans yield 11–25%.

**Model split test** — the E1 audit prompt run with haiku vs sonnet on 2 real specs seeded with 11 documented defects (8 internal_contradiction graded easy→subtle, 2 dead_ref, 1 stale_term), scored by a sonnet judge vs ground truth:

| | Haiku | Sonnet |
|---|---|---|
| Recall | 6/11 (55%) | 9/11 (82%) |
| False positives | 0 | 0 |
| dead_ref / stale_term (mechanical) | 3/3 | 3/3 |
| internal_contradiction | 3/8 | 6/8 |
| subtle contradictions | 0/2 | 0/2 |

Takeaways: **both produced 0 false positives on the seeded split test** — low (not proven-zero) degradation risk. The gap is **entirely in contradiction detection** (the highest-value class). Sonnet recovers ~half-again more. **Neither catches subtle contradictions** → E1 is a **sieve, not a guarantee**; the subtle tail still goes to codex (matches #94's own ad-hoc audit missing R11-F2).

**Outcome + harm analysis** — the counterfactual that matters more than the count: 40 sessions traced round-by-round (FROZEN `verdict-r*.txt` + `review-ledger.yml` ONLY — the on-disk artifacts have since drifted to GO, so they are NOT the review-time state), with agents explicitly tasked to hunt for harm:

| | |
|---|---|
| **harmed sessions** | **0** |
| **harm_risk = real** | **0** |
| helped / neutral / harmed | 18 / 22 / 0 |
| rounds saved (conservative) | 21 (15×1 round, 3×2 rounds) |
| E1-class findings introduced mid-session (between-round author edits) | 92 |

**No session would have been made worse** (0 harmed, 0 real harm-risk under adversarial harm-hunting). The residual 37 "low" risk is per-round overhead (a cheap Sonnet no-op on converging/low-yield rounds), corroborated by the 0-observed-FP split-test result (no observed wrong-fix vector). The 21 saved rounds (a conservative counterfactual estimate, §7) trace to concrete causes — e.g. `master-spec` R5/R6/R7 were *only* AC-trace-table inconsistencies; `small-bodies-design` recurred a stale-architecture-table after each fix across R3/R4. The 92 mid-session introductions are the decisive argument for **every-round** (round-1-only would miss all 92). Conservative: 22 of 40 sessions saved 0 rounds (E1-findings tangled with deep blockers that would have run the round anyway — agents did not inflate).

## 3. Design (LLM locates, the script kills hallucinations, Claude judges)

> **Note (2026-06-01) — convergent design after the dogfood.** Rounds 1-4 of bulldozer:check on *this very spec* showed that pushing E1's verification into a **hard, un-skippable, fully-deterministic wrapper-enforced gate** is itself a non-converging stateful spec — the exact #94 pattern. Each armor layer spawned a new edge case: a deadlock when Claude declines a semantic-residual finding (R4-F1), an unassigned refuse exit code (R4-F3), and per-class deterministic gates that are either gameable or over-strict (R4-F2/F4). Consistency auditing has an **irreducible semantic core** ("do these two present quotes *truly* conflict?"). This design accepts that core instead of armoring against it — and converges.

Three roles:
- **LOCATE** — the read-only **`consistency-auditor` agent** emits `{id, class, file, quote, anchor}`: locators + the literal citing quote. No verdict, no check.
- **VERIFY (the one deterministic guarantee)** — **`verify-audit-findings.py`** confirms every cited quote is **verbatim-present** in its file, dropping any hallucinated/fabricated quote. That is its *whole* job — kill made-up evidence. It does NOT decide whether the cited text is really a defect.
- **JUDGE + FIX** — Claude reviews the quote-confirmed findings, applies judgment (is this `dead_ref`/`internal_contradiction`/`cross_spec_drift`/`stale_term` a real defect, or intentional?), fixes the real ones, commits separately. Declining a finding is fine — Claude just doesn't edit.

**Enforcement is SOFT:** a SKILL.md step + a structural test that the step exists, the verifier is invoked, and Claude may fix ONLY quote-confirmed findings; the `e1-findings`/`e1-verified` files land in the per-review `.bulldozer/<session>` dir (gitignored, like every review artifact), so a skip is detectable in-session (the `e1-verified-rN.json` is simply absent) — not via git history. No wrapper refuse-loop, no new exit code, no deadlock — the irreducible semantic residual stays with Claude, where it belongs.

**Honest tradeoff:** a determined Claude could skip the SKILL step (vs the rejected hard wrapper gate). Mitigation: the verifier is cheap and trivially runnable, the audit-trail files make a skip visible, and a structural test pins the discipline. Pragmatic for a *best-effort sieve* — and, unlike the fortress, it converges.

### 3.1 Flow (every round, for doc/spec artifacts)

**GATE — doc round?** `.md`/`.mdx`/`.rst` artifact, a docs/specs directory, or a diff touching doc files → E1 runs. A pure source file/dir or a no-doc diff → no E1 phase, normal round. (A single `.md` IS a valid target; the gate excludes non-doc *code*, not "small" docs.)

```
1. LOCATE — Claude dispatches the read-only consistency-auditor agent
     (Task subagent_type="bulldozer:consistency-auditor", model = audit_model, default sonnet).
     The agent RETURNS the findings as its structured output ({id, class, file, quote, anchor} —
     locators + literal quote(s)); CLAUDE writes them to e1-findings-rN.json (the agent is
     read-only with no Write/Bash — it cannot write the file itself).

2. VERIFY — Claude runs verify-audit-findings.py: it keeps only findings whose quote(s)
     (the `quote`, and `anchor.quote_b` / `anchor.other_quote` where the class has a second
     locator) are verbatim-present where claimed → writes e1-verified-rN.json. Fabricated /
     hallucinated quotes DROP. This is the ONE deterministic step (anti-hallucination).

3. JUDGE + FIX — Claude reviews e1-verified-rN.json (the SOLE licensed fix input — raw
     e1-findings is forbidden), and for each applies judgment: is the cited text actually a
     defect of its class (the dead_ref genuinely unresolved? the two present quotes genuinely
     conflicting? the drift real? the term genuinely stale-not-intentional)? It fixes the real
     ones, declines the intentional ones, and commits SEPARATELY.

4. ROUND — Claude invokes bulldozer-round.sh normally on the cleaned artifact.
```

**No deadlock, no exit code, no race:** a declined finding is simply not fixed — the soft step never blocks the round (this is what R4-F1 / R4-F3 dissolve). The verifier reads the artifact once per round; there is no hash and no pre/post comparison (R3-F2 stays resolved). Runs **every round** (between-round edits reintroduce drift).

**ACCOUNTING (R1-F3):** E1 fixes are NOT codex-round fixes — never counted in `BULLDOZER_FIXED`/`BULLDOZER_FP` (codex-only). The separate commit is the audit trail; recorded in `review-ledger.yml` (if at all) under a distinct `e1_audit:` note, never as `R{round}-F{n}` reviewer findings.

### 3.2 Components

| Component | Where | Change |
|---|---|---|
| `agents/consistency-auditor.md` | **new plugin agent** | The read-only **locator**. Frontmatter: `name`, `description`, `tools: [Read, Grep, Glob]` (NO `Bash`/`Edit`/`Write` — **R2-F1**: `Bash` is a mutation surface and plugin-agent `permissionMode`/hooks are ignored, so report-only is enforced by tool-list exclusion), `model: sonnet`. Body: check ONLY the 4 classes; RETURN the uniform envelope `{id, class, file, quote, anchor}` (locators + literal quote, NO verdict/check) as the agent's structured output — it does NOT write any file (read-only); no design/style/logic opinions. `Task(subagent_type: "bulldozer:consistency-auditor", model: <audit_model>)`. Claude writes the returned JSON to `e1-findings-rN.json`. Plugin's first `agents/` entry. |
| `skills/check/data/e1-evidence-schema.json` | **new frozen schema** | The uniform envelope `{id, class, file, quote, anchor}` + per-class `anchor` shape (§3.4). Drift-guarded by `TestE1SchemaContract`, like `TestDepthConfigContract` guards `depth-config.json`. Single source of truth for the agent body + the verifier. |
| `skills/check/scripts/verify-audit-findings.py` | **new helper** | The **anti-hallucination verifier** (§3.4). Reads `e1-findings-rN.json`, keeps only findings whose cited quote(s) are verbatim-present where claimed (a fixed `grep -nF` it builds — never an agent command), drops the rest. Writes `e1-verified-rN.json`. Always exits 0 when it ran (fail-open). No per-class semantic gate, no hash, no wrapper coupling. |
| `skills/check/SKILL.md` | step list | New **Pre-review consistency audit** step (every doc round): dispatch auditor (locate) → run verifier → JUDGE + fix ONLY `e1-verified-rN.json` entries (sole licensed input; raw forbidden), declining intentional ones → separate commit → normal round. Soft (no wrapper gate). |
| `skills/check/SKILL.md` | `allowed-tools` frontmatter | Add `Task`. Per `skill-dev.md` allowed-tools is permissive and the orchestrator already has `Task` in the main session — declaration/clarity, not a runtime unlock (R1-F1 right-sized). |
| `.bulldozer/config.md` | config key | New optional `audit_model` (default `sonnet`); passed as the `model` override on the auditor Task call. 1-line flip to `haiku`. |
| `tests/test_verify_audit_findings.py` | **new behavioral test** | quote-presence: a finding whose quote(s) are present → kept; empty/absent `quote` → dropped (a fabricated quote forfeits the finding — the one ungameable guarantee); `internal_contradiction` whose `anchor.quote_b` is absent → dropped; auditor file missing/unparseable → 0 kept + exit 0 (fail-open); `e1-verified-rN.json` written. |
| `tests/test_skill_prompts.py` | structural test | Pins the SKILL.md step: doc gate, auditor dispatch, the verifier invocation, the **sole-licensed-fix-input** clause (fix only from `e1-verified`, raw forbidden), the E1-accounting rule, `audit_model` default. Pattern of `TestDepthConfigContract` (which lives in `tests/test_check_round_wrapper.py`). Plus `TestE1SchemaContract` drift-guards `skills/check/data/e1-evidence-schema.json`. |
| `tests/test_plugin_structure.sh` | structural test | `agents/consistency-auditor.md` frontmatter `tools` excludes `Bash`, `Edit`, `Write`; `model` present. |

### 3.3 The auditor agent's instructions (the 4 classes)

The agent body tells it to check ONLY these and emit the uniform envelope `{id, class, file, quote, anchor}` (§3.4 — the literal citing `quote` + the class's second locator in `anchor`) per finding:
1. **dead_ref** — cited file/path/section/anchor/symbol that doesn't resolve.
2. **internal_contradiction** — two places in the same doc stating conflicting things.
3. **cross_spec_drift** — a shared contract diverges from a sibling spec it depends on.
4. **stale_term** — leftover old version string / resolved finding-ID / obsolete term in *active* prose (not changelog/history).

Hard rules: emit only LOCATORS + the literal `quote` (never a verdict, never a check — the verifier confirms the quote is present, Claude judges the defect, §3.4); the `quote` must be copied verbatim from the file; no style/wording/missing-feature/design/logic opinions; empty list if clean. (These rules produced 0 FP in the split test.)

### 3.4 The verifier: kill hallucinations, leave semantics to Claude

The auditor supplies the uniform envelope `{id, class, file, quote, anchor}` (frozen in `skills/check/data/e1-evidence-schema.json`, drift-guarded). `verify-audit-findings.py` does ONE deterministic thing — confirm every cited quote is real:

- **All classes:** `quote` is non-empty AND verbatim-present in `file` (fixed-string match the script builds — never an agent command).
- **Second locator where the class has one (must also be present):**
  - `internal_contradiction` → `anchor.quote_b` verbatim-present in `file`, distinct from `quote`.
  - `cross_spec_drift` → `anchor.other_quote` verbatim-present in `anchor.other_file`.
  - `dead_ref` → the citing `quote` present (the *target* `anchor.ref` is what Claude judges as resolved-or-not).
  - `stale_term` → the term `quote` present.

A finding survives ⇔ all its cited quotes are present. Any absent/empty quote → DROP — a fabricated quote forfeits the finding, it cannot buy a pass. **This is the single ungameable guarantee** (no agent command to game, no command to hang). The script (Python `pathlib` + a fixed `grep -nF` it constructs) writes `e1-verified-rN.json` (survivors) and **always exits 0 when it ran** — a dead/empty/unparseable auditor file → 0 survivors → proceed (fail-open).

**What the script deliberately does NOT do** (the convergence move): it does not decide whether the `dead_ref` is really unresolved, whether two present quotes really conflict, whether the drift is real, or whether a term is stale-vs-intentional. Those are the irreducible semantic judgments — Claude's, on the quote-confirmed set. A surviving finding means *"the cited text is real, now apply judgment"*, never *"must fix"*. Trying to make those judgments deterministic is exactly what failed to converge (R4-F1/F2/F4).

**Sole licensed fix input:** SKILL.md states Claude may edit a consistency finding ONLY if it appears in `e1-verified-rN.json`; fixing from the raw `e1-findings-rN.json` is forbidden (a structural test pins both clauses; a unit test feeds a fabricated-quote finding and asserts it is DROPPED).

## 4. Error handling / degradation

- **FP risk:** 0 false positives observed (both models) on the seeded split test. Defences against a bad edit: (1) the locator-only agent prompt (no verdict to inflate); (2) the verifier drops any finding with a fabricated/absent quote (anti-hallucination); (3) the agent's tool-list (`Read, Grep, Glob` — cannot mutate); (4) Claude's judgment on the survivors declines intentional non-defects. A *semantically* false finding whose quotes are present (see §7) is caught by (4), not by a gate — by design.
- **No deadlock, no exit code (R4-F1/R4-F3 dissolved):** enforcement is soft (a SKILL step, not a wrapper refuse-loop), so a declined finding simply isn't fixed and never blocks the round. The wrapper is unchanged — E1 runs entirely in SKILL.md before the normal `bulldozer-round.sh` invocation.
- **Fail-open:** a dead / empty / unparseable auditor file → the verifier keeps 0 → Claude has nothing to fix → normal round. A dead auditor degrades to "no pre-clean this round", never blocks. (Verifier's own IO crash → non-zero exit + one-line stderr; SKILL.md says skip the pre-clean and proceed — codex still catches real inconsistencies.)
- **Low-yield artifacts** (deep impl plans, ~11–25%): the auditor mostly finds nothing → one cheap subagent pass per round. The gate skips non-doc (code) artifacts entirely; a low-yield *doc* still gets one cheap pass.
- **Skip risk (the soft tradeoff):** a determined Claude could skip the SKILL step. Mitigation: the verifier is cheap to run, the structural test pins the discipline in SKILL.md, and the `e1-verified-rN.json` artifact's presence/absence in the `.bulldozer/<session>` review dir makes a skip detectable in-session (these review artifacts are gitignored, so this is in-session visibility, not a git-history guarantee). Accepted residual for a best-effort sieve.
- **Accounting (R1-F3):** E1's separate commit + `e1_audit:` ledger note keep its edits out of `BULLDOZER_FIXED/FP` — the trajectory/pivot math (codex findings only) is unaffected.
- **Sieve, not guarantee:** subtle contradictions (0/2 both models) still reach codex. E1 reduces wasted rounds; it doesn't eliminate them.

## 5. Testing

- **Behavioral — the verifier** (`tests/test_verify_audit_findings.py`): quote-presence against real bytes — a finding whose cited quote(s) are present → kept; empty/absent `quote` → dropped (the one ungameable guarantee — a fabricated quote forfeits the finding); `internal_contradiction` whose `anchor.quote_b` is absent → dropped; `cross_spec_drift` whose `anchor.other_quote` is absent in `anchor.other_file` → dropped; auditor file missing/unparseable → 0 kept + exit 0 (fail-open); `e1-verified-rN.json` written.
- **Structural** (`tests/test_skill_prompts.py`): the SKILL.md step present with the doc gate, auditor dispatch, the verifier invocation, the **sole-licensed-fix-input** clause, the E1-accounting rule, `audit_model` default. Plus `TestE1SchemaContract` drift-guards `skills/check/data/e1-evidence-schema.json` (pattern of `TestDepthConfigContract`, which lives in `tests/test_check_round_wrapper.py`).
- **Agent structure** (`tests/test_plugin_structure.sh`): `agents/consistency-auditor.md` `tools` excludes `Bash`/`Edit`/`Write`; `model` present.
- The auditor's **recall/FP** and Claude's **semantic-defect judgment** are model behavior — validated by the empirical split test (§2), not unit tests.

## 6. Scope / non-goals

**In:** the per-round SKILL.md step (doc gate + locate + verify + judge/fix); the read-only `consistency-auditor` agent (`agents/`); the frozen `skills/check/data/e1-evidence-schema.json`; the `verify-audit-findings.py` anti-hallucination verifier; the `audit_model` config knob (default sonnet); the behavioral + structural + agent-structure tests. (No `bulldozer-round.sh` change — enforcement is soft.)

**Out (stay on #94):** E2 post-review critique, E3 reviewer continuation, E4 adaptive cap, E5 cluster classification, E6 advanced pivot triggers. Auto-fixing by the agent (read-only — Claude owns edits). A deterministic *finder* (rejected: brittle ref-resolution on prose). A deterministic *defect prover* — ref-resolution / value-extraction / conflict-detection in the script or a wrapper-enforced gate (rejected: rounds 1-4 showed it does not converge). NOTE the soft three-role split: the auditor LOCATES (LLM judgment + literal quotes); the script CONFIRMS only that the cited quotes are verbatim-present (anti-hallucination — never re-derives refs, never runs an agent command); Claude JUDGES the semantic defect (refs / drift / conflict / staleness); the SKILL.md step is the enforcement boundary (the wrapper is unchanged).

## 7. Honest limitations

1. **Subtle contradictions slip through** (0/2 both models). The hardest semantic conflicts remain codex's job.
2. **Bimodal value:** deep technical impl plans get little from E1 (~11–25% ceiling). The gate can't tell high- from low-yield a priori; low-yield just costs one cheap no-op/round.
3. **Split-test sample is small** (11 defects, 2 specs) — directional, not definitive; but the haiku/sonnet gap (82 vs 55, concentrated in contradictions) is large and matches the prior.
4. **cross_spec_drift under-tested** in the split test (the seeded corpus was self-contained); real value-ceiling data (64 findings, 12.8%) shows it matters — the prompt covers it, but its recall is unmeasured here.
5. **"21 rounds saved" is a counterfactual estimate**, not measured ground truth — agents reasoned (conservatively) about what *would* have happened had E1 run. The per-case reasoning is grounded in the frozen verdicts, and the "0 harmed" finding is corroborated by the independent 0-FP split test, but the round-saving magnitude should be read as directional. All three analyses read ONLY the frozen `.bulldozer` snapshots (verdicts + ledger), never the drifted on-disk artifacts.
6. **Semantic false-positives the verifier cannot catch** (real, observed in this spec's own round-2 dogfood) — and the reason enforcement is *soft*, not a hard gate. The verifier confirms a quote is *present*, not that it is a *defect*. Example: this spec's own `(R1-Fx)` design-provenance tags are technically "resolved finding-IDs in active prose" — the `stale_term` quote is present, yet they are *deliberate* traceability annotations, not stale cruft. Such a finding SURVIVES the verifier (the quote is real); only Claude's judgment classifies it as an intentional non-defect and declines the fix. So "survives the verifier" ≠ "must fix" — it means "the cited text is real, now apply judgment." Trying to make this judgment deterministic is what failed to converge across rounds 1-4 (§3 note); the soft step keeps Claude in the loop for exactly this irreducible residual.
