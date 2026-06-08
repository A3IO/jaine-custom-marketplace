# SP0 Decision: /drive automation engine

*Date: 2026-06-04. Measured live via `spike/` (plan `2026-06-04-sp0-engine-spike.md`), reproducible (RUN1==RUN2, ports free after).*

## Measured criteria

| Criterion | cdp.py naive | cdp.py best | Playwright | Winner |
|---|---|---|---|---|
| Auto-wait reliability (×10) | 0/10 | **10/10** | **10/10** | tie (best cdp.py ≡ Playwright) |
| Actionability (#delayed race) | fail (no wait) | pass (explicit `wait --js !disabled`) | pass (auto) | tie on outcome; PW implicit |
| Console-gate detection (N=1, not ×10) | — | DETECTED¹ | **DETECTED** | qualified¹ — cdp.py caught it once here, mechanism uncertain; Playwright's persistent subscribe is more robust |
| Assertion ergonomics (LOC) | — | ~41 best-mode² | **33** | Playwright (~8-line gap, not 17²) |
| `connect_over_cdp` fidelity | — | n/a | **ATTACH_OK** | Playwright attaches cleanly to the lane |
| Dependency cost | — | **none (vendored)** | venv + 4 pkgs | cdp.py |
| Trusted input | untrusted js-click | **trusted (#140)** | native | tie |
| Single- vs dual-stack | — | **single (== /look)** | dual (/look cdp + /drive pw) | cdp.py (no R2-F divergence) |

**Key empirical finding:** best-effort cdp.py is **reliability-equivalent** to Playwright (both 10/10 over ×10). The naive 0/10 is a discipline strawman (missing explicit waits) — the verify-core writes best-mode by construction, so it never applies.

**Two supporting legs are weaker than a first read suggests** (code-review, honest correction — the spike ran correctly and the numbers are real, but the interpretation over-claimed):

- **¹ Console parity is N=1, not a proven tie.** `cmd_console` opens a *fresh* CDP connection AFTER the error fired and listens 3s for *live* events; `Runtime.exceptionThrown` is transient and is NOT replayed to a late subscriber. The single DETECTED here is most likely an artifact of *this* Chrome buffering the uncaught error as a replayable Console message — it may NOT generalize to a mid-flow error, a rotated buffer, or another Chrome build, and it was measured once (the reliability rows are ×10). Playwright's persistent `page.on('console')` is genuinely more robust. **Treat console-gate robustness as an open cdp.py risk to re-verify in SP2**, not a settled tie.
- **² The LOC gap is ~8, not 17.** The 50-line cdp.py scenario includes BOTH the naive and best branches plus the parity block; best-mode alone is ~41 lines. Best-vs-best is ~41 vs 33 → ~8 lines — half the raw 50-vs-33. Playwright's one measured advantage is real but ~2× smaller than the table's first read.

cdp.py's real advantages stand on their own: zero dependency, single-stack, already integrated (wait/trusted-click/console/--target/--insecure all shipped in look-v2).

## Verdict: **bounded both**

`cdp.py` is the **default** engine for the `/drive` verify-core; Playwright is an **explicit, per-test opt-in** for a bounded class.

### The boundary (this is what prevents the panel R2-F dual-stack divergence)

1. **Default = cdp.py** for every drive test: navigate / wait / click / console / assert. Single-stack with `/look`.
2. **Playwright opt-in ONLY** when a real product test *demonstrably* hits a cdp.py wall that needs a capability cdp.py lacks:
   - rich locators (`get_by_role`/`text`/`test_id`) where CSS selectors are too brittle;
   - actionability/stability checks beyond `cdp.py wait --js`;
   - Playwright-only features (trace viewer, richer network interception).
   "Might be nicer" is NOT a trigger — a measured cdp.py wall is.
3. **Per-test, explicit, never mixed:** a test declares one engine (default cdp.py; `--engine playwright` opt-in). No test is half-cdp/half-pw → no divergence *within* a flow.
4. **Identical pass/fail contract** regardless of engine — same assertion semantics, same `*_PASS`/`*_FAIL` + exit-code output → reports never diverge *across* tests.
5. **Playwright stays isolated to `/drive`** — `/look` never uses it. No dual-stack in the casual path.

### Consequence for SP2
- verify-core (proactive self-verify loop + assertion primitive + console-gate) is built on **cdp.py** — no new dependency, single-stack.
- SP2 may add a thin best-practice **auto-wait helper** over cdp.py to close most of the ~8-line best-mode ergonomic gap (the raw 50-vs-33 includes cdp.py's naive branch, which the verify-core never ships) without the dependency.
- **Console-gate is the one leg to re-verify in SP2** (see ¹): build it on cdp.py but test it against a *mid-flow* error (not just a buffered-at-load one) and consider a persistent subscription instead of the one-shot `console` read. If cdp.py's one-shot console proves unreliable there, that is a concrete cdp.py wall → Playwright opt-in for console-heavy tests.
- The Playwright opt-in path (`connect_over_cdp` to the CfT lane — `ATTACH_OK` verified) is documented but **not built until a real test needs it** (YAGNI). When built, it lives behind the per-test `--engine` flag with the identical contract above.

### Caveat
The spike tested a **simple** fixture (one async element, one delayed-enable, one console error). Reliability parity holds for basics; a complex real-product UI (many dynamic elements, iframes, shadow DOM) is where Playwright's mature locators/actionability could justify the opt-in. That's exactly what the bounded-both boundary is for — start cdp.py, escalate to Playwright per-test when a wall is hit.
