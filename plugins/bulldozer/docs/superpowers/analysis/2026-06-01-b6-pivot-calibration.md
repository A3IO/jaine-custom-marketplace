# B6 — Calibrated Pivot Trigger: Corpus Analysis

Issue [#128](https://github.com/A3IO/jaine-plugins/issues/128). Deliverable for the
research half of B6 (roadmap item from #110). Question: re-derive/validate the
PR #95 calibrated pivot trigger (`exhaustive + round ≥ 5 + avg last-3 fresh
findings ≥ 3.0`) against the real session corpus, then a small wrapper change.

## Method

Parsed every `.bulldozer/<session>/state.json` under `/0` (65 sessions with real
round history) — each has `depth` + `history[]` with per-round `findings` count
and `verdict`. **Convergence definition:** a session *converged* if any logged
round has `verdict == "GO"`; otherwise *non-converged* (hit max rounds, abandoned,
or ended REVISE/ITERATE/NO-GO).

**Trigger simulation:** for each session, the calibrated trigger fires at the
first round `r` where `r ≥ MINROUND` and the mean of the last 3 rounds' `findings`
≥ `THRESH`. Classify: fired on a non-converged session = **TP** (correct early
pivot); fired on a converged session = **FP** (premature — would have interrupted
a review that did reach GO).

> **Caveat:** `state.json` records total `findings` per round, not *fresh* (new)
> findings as PR #95's wording implied. The corpus doesn't distinguish them, so
> this uses total findings as the proxy. The trigger is also an AskUserQuestion
> dialog (continue / restructure / accept), **not** an abort — an FP is an
> unnecessary prompt, not lost work.

## Corpus

| Depth | Sessions |
|-------|----------|
| standard | 48 |
| quick | 12 |
| exhaustive | 5 |
| **total** | **65** (33 converged) |

Only **16** sessions ever reached round ≥ 5 (the calibration-relevant set); the
other **49** end before round 5, where the flat `round == max_rounds` trigger
already handles them. Of the 5 exhaustive sessions, only 2 reached round ≥ 5.

## Key finding — PR #95's "0 FP / 60% TP" does NOT reproduce any-depth

| Scope | minR | thr | TP | FP | TN | FN | precision | recall |
|-------|------|-----|----|----|----|----|-----------|--------|
| any-depth | 5 | 3.0 | 5 | **4** | 29 | 27 | 0.56 | 0.16 |
| any-depth | 5 | 4.0 | 3 | 2 | 31 | 29 | 0.60 | 0.09 |
| any-depth | 6 | 4.0 | 2 | 0 | 33 | 30 | 1.00 | 0.06 |
| any-depth | 7 | 3.0 | 2 | 0 | 33 | 30 | 1.00 | 0.06 |
| **exhaustive** | **5** | **3.0** | **2** | **0** | 1 | 2 | **1.00** | **0.50** |
| exhaustive | 5 | 5.0 | 1 | 0 | 1 | 3 | 1.00 | 0.25 |

At PR #95's nominal threshold (`r≥5, avg≥3.0`) the **any-depth** corpus shows
**4 false positives** (40% of the 10 converged sessions that reached round 5) —
mostly user-*extended* standard reviews that dipped and recovered. PR #95's 0-FP
claim held only because it was measured on exhaustive sessions, where converging-
yet-long runs are rare. To get 0 FP any-depth you must push to `r≥6, avg≥4.0` or
`r≥7`, collapsing recall to **0.06** (fires on 2 of 65) — negligible benefit.

## Decision (Chris, 2026-06-01): ship narrow — exhaustive-only

Trigger = **`depth == exhaustive` AND `round ≥ 5` AND `verdict != GO` AND
avg(last-3 findings) ≥ 3.0**.

Rationale:
1. **0 FP on the corpus** (5 exhaustive sessions; the 2 that reached round 5 were
   both non-converging and correctly caught — recall 0.50).
2. **Avoids the standard-depth FPs** by construction — the false positives all
   came from non-exhaustive runs, which this scope excludes.
3. **Low blast radius:** the pivot is an AskUser dialog, not an abort, and the
   flat `round == max_rounds` trigger still fires at round 10 as the backstop. The
   calibrated trigger only moves the dialog *earlier* on clearly-doomed
   exhaustive runs.
4. Closest to PR #95's original intent.

**Honest limitation:** validation is thin (n=5 exhaustive, 2 at round ≥ 5). This
ships as a conservative, strictly-safe-on-corpus heuristic, not a
statistically-robust threshold. Re-evaluate when the exhaustive corpus grows.

## Implementation note

The wrapper already computes `avg last 3` for the trajectory line
(`render-trajectory.py`). The trigger reuses the same metric (mean of the last 3
`history[].findings`) read from `state.json` after `log-round`, gated on
`DEPTH == exhaustive` and `ROUND >= 5`, OR-ed with the existing max-rounds pivot
condition. Threshold `3.0` is a module constant for easy re-tuning.

*Version: 1.0.0 | 2026-06-01*
