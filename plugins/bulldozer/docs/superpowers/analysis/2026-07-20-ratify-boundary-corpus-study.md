# Ratify-Boundary Pivot Option — Corpus Study (REFUTED)

**Question:** should `/bulldozer:check` get a "ratify boundary" pivot option — at
round-cap with a finding re-opening as variants of one theme, the user ratifies an
explicit scope boundary; the next round's prompt says "user-ratified — do not
re-litigate"; the ledger keeps the finding `still_open`+boundary?

**Answer: NO — do not implement as designed.** Verdict shared by the in-house
verify-all swarm and an independent codex xhigh adversarial pass (CONCLUSION
HOLDS). The idea originated from session `a8ff68b4` (2026-07-19, the #334 plan
review), where the manual version of this move legitimately stopped
goalpost-moving on finding R1-F1 — but the corpus shows that case is a ~1-in-150
exception, while the feature's trigger surface selects almost exclusively the
population where ratifying would have shipped real defects.

## Method

- **Corpus:** every parseable `review-ledger.yml` under `/0` — 148 sessions
  (all projects, reviewer = codex). 39 sessions had ≥1 *re-litigated* finding
  (status history spanning ≥2 rounds past introduction, or ≥2
  `still_open`/`reopened` entries) — 92 findings. Plus 8 sessions whose ledgers
  fail YAML parsing (including the `a8ff68b4` origin case) analyzed from raw
  text. 1 of 39 chains lost to rate-limiting.
- **Pipeline (Workflow tool, verify-all pattern):** per session a sonnet
  classifier read the ledger AND the per-round `verdict-r*.txt` files, then
  classified each re-litigated finding:
  - **helped** — ratifying after the first genuine fix would have saved rounds
    with no real defect lost (later variants produced no real fix);
  - **hurt** — a later variant was a REAL defect fixed by a REAL change; early
    ratification would have shipped it;
  - **neutral** — FP refutation, multi-round verification lag, other.
  Every helped/hurt verdict was re-checked by an independent sonnet skeptic
  against the same primary sources (default-to-refute). Opus synthesis.
- **External adversarial pass:** codex (gpt-5.6-sol, xhigh) attacked the
  methodology, spot-checked hurt exemplars against the on-disk ledgers/verdicts
  and resulting code, and audited the entire neutral bucket for miscarried
  helped cases.
- Scale: ~130 agents, ~17M subagent tokens, plus two codex xhigh turns.

## Results (post-skeptic)

| | helped | hurt | neutral |
|---|---|---|---|
| Main corpus (39 sessions) | **0** | 69 | 21 |
| Supplement (8 raw-text sessions) | **1** | 10 | 7 |
| After dedup (34 hurt = 2 deck defects × 17 parallel worktrees) | **1** | **~47** | 28 |

The single confirmed **helped** anywhere is `a8ff68b4` R1-F1 itself — and only
from round 3 onward: its rounds 1→2 and 2→3 were REAL redaction bypasses
(case-insensitive schemes, `blob:`/`view-source:` wrappers, `filesystem:`) fixed
with real code. Ratifying at the natural temptation points (rounds 1–2) would
have shipped secret leaks. The codex pass audited all 21 main-corpus neutral
findings individually: **no hidden helped case**.

## Exemplar hurt cases (codex-verified against disk)

- **deck `R2-F4` — the canonical trap.** SQLite collector made "read-only" via
  `mode=ro` (round 2), **VERIFIED GO at round 3**, silent through rounds 4–5 —
  the exact "fixed and settled" shape ratify keys on. Round 6's live probe:
  `ATTACH DATABASE` created `aux.db` on disk; `CREATE TEMP TABLE` also bypassed
  `mode=ro`. Real fix was a structurally different mechanism
  (`sqlite3.set_authorizer` allowlist) with regression tests. Observed
  identically across 17 parallel deck worktrees.
- **deck `R3-F3` — confirm-gate auth bypass.** Round-3 fix used
  `not body.get("confirmed")`; round 4 brought the exploit: `"false"`, `"0"`,
  `1` are Python-truthy → destructive `@action(risk="confirm")` executes without
  real confirmation. Real fix: exact `is not True` identity check + enumerated
  truthy/falsy test matrix.
- **branchlab `R7-F1` — origin-validation bypass.** Round-7 fix validated only
  the first push URL; round 8: a second `remote.origin.pushurl` still redirects
  live pushes. Real fix: `git remote get-url --all` (fetch AND push) with every
  URL validated.
- **tts-b6 `R1-F2` — seven rounds, seven DIFFERENT concurrency races** in one
  trim-modal pipeline, six fixed with real code, the seventh still open at the
  cap. Re-litigation can be justified even when it never converges.

## Patterns

1. **Zero trigger precision.** The surface signal the feature fires on
   (round-cap + re-opening variants of one theme) matched the hurt population
   essentially perfectly and the helped population ~once. It does not
   discriminate safe-to-ratify from ship-a-defect at all.
2. **"Fixed → verified → dormant → real bypass" is the danger state** — and it
   is exactly where the user is most tempted to ratify.
3. **The real discriminator is semantic and IS the review judgment.** Every hurt
   reopen carried a concrete-defect marker (live probe, exploit payload, new
   mechanism). But a "block ratify on defect markers" rule would ALSO have
   blocked the sole helped case: `a8ff68b4` round 3 still contained concrete
   whitespace-URI probes; the boundary became legitimate only via the judgment
   "RFC 3986 forbids these inputs". Distinguishing scope from defect is the
   judgment the feature tries to skip.
4. **Ledger metadata is unreliable for automation:** several findings carried
   stale `last_seen_round` values understating the true span (classifiers
   corrected by reading verdict files). Any automated detector built on ledger
   metadata would misfire — an independent implementation risk.

## Honest limitations

- Classifier and skeptic prompts were **asymmetrically skeptical toward
  "helped"** — this deflates helped counts. Codex judged the direction robust
  regardless (~19 distinct hurt findings would have to be mislabeled to flip it;
  spot-checks showed the opposite), but prevalence claims are bounded by it.
- **Corpus = completed, ledger-bearing, codex-reviewed sessions only.**
  Abandoned "spiral forever" sessions and noisier reviewers — where genuine
  scope-churn could exist — leave no signal here. The defensible claim is "no
  positive automatic class was demonstrated in this corpus", not "the target
  class universally does not exist".
- **Counterfactual, not observed:** "ratifying would have shipped the defect"
  assumes the user ratifies at the identified checkpoint.
- The 21 main-corpus neutral findings were single-rater in the swarm (codex
  audited them separately).

## Decision

- The ratify-boundary **pivot option is not built**. The existing mechanism —
  round-cap NO-GO, honest STOP, manual human decision — is already the correct
  shape.
- The **manual move stays legitimate** (as performed in `a8ff68b4`): a human may
  decide a boundary and put it in the next round's prompt. If it is ever
  formalized, the corpus dictates hard gates: explicit user command only (never
  suggested by the tool), the full finding chronology with probes/diffs shown
  before confirming, the exact boundary text confirmed verbatim (not a generic
  "continue"), ledger records it as accepted-unresolved-risk (never `fixed`),
  and it must not suppress review of adjacent code.

## Provenance

Study run 2026-07-20 in session `d3f045d4` (bulldozer worktree). Workflow runs
`wf_ffdc2bb3-67b` (main, 110 agents) and `wf_a61eaaaa-64c` (supplement, 21
agents); codex adversarial threads `019f7e06-2db0-…` (design NO-GO round) and
`019f7eed-77b4-…` (methodology pass, CONCLUSION HOLDS). Deterministic corpus
parse: 148/156 ledgers (8 raw-text), plus `~/.claude/hooks/bulldozer.log`
(2,863 lines since 2026-05-09) as the cross-project session index.
