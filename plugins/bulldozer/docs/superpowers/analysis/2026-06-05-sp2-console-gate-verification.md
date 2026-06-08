# SP2 — Console-Gate Re-Verification (SP0 footnote ¹ closed)

*Date: 2026-06-05 (revised same day — see Honest revision). CfT 149.0.7827.54
(pin `cft/current`), mac-arm64, Darwin 25.4.0. Probe lanes: 9360/9361 (transient,
per the conftest e2e port registry). Method: clean page → `cdp.py js` arms a
`setTimeout` error → error fires AFTER the js call returns (no CDP listener
attached) → one-shot `cdp.py console` reads retroactively.*

## Question (SP0 decision-doc footnote ¹)

SP0 measured console-gate detection at N=1 on a buffered-at-load error and flagged
the mechanism as uncertain: "`Runtime.exceptionThrown` is transient and is NOT
replayed to a late subscriber … treat console-gate robustness as an open cdp.py risk
to re-verify in SP2 against a *mid-flow* error."

## Honest revision (Task 3 TDD falsified the morning's P2 claim)

The morning run concluded "mid-flow `console.error` is replayed too" (P2). Writing
the e2e for it FALSIFIED that: the replay only appeared because the morning
sequence had performed a console-read while an exception already sat in storage.
The full factor split (probes A–K below) shows the real rule. The morning table is
superseded by this one; consequences are revised accordingly.

## Results (full factor split)

| Probe | Sequence | Result |
|---|---|---|
| **P1 / C / H / E / I** | mid-flow **uncaught exception**, read later (data:, file://, http; with/without --wait; before/after other reads) | **Replayed in EVERY variant** — `[exception] …` via the `Runtime.exceptionThrown` branch. Retroactive exception capture is unconditional on this build. |
| **P3** | second read after a replayed read | **Repeatable** — storage is not consumed by reading. |
| **P4** | error on page A → navigate B → read | **Buffer clears on navigate** — the gate is naturally scoped to the CURRENT navigation. |
| **P5** | headless vs headful | Identical behavior. |
| **A / B / D / G** | mid-flow **`console.error`**, read later (data:, http page, file://; with a PRIOR read on EMPTY storage in D) | **NOT replayed** — `(no console messages)`. |
| **E / I** | mid-flow `console.error` fired AFTER a console-read performed while storage was NON-empty (an exception sat there) | **Replayed** — the read-on-non-empty-storage "activates" console.* storage for the rest of the page. This is the quirk the morning P2 accidentally measured. |
| **K** | `console.error` fired 200ms after the gate STARTS (inside its 3s live listen window) | **Caught live, unconditionally.** |

## Rule (empirical, this build)

- **Uncaught exceptions**: stored and replayed to a late one-shot subscriber —
  always (file://, data:, http; independent of prior reads or navigation mode).
- **`console.*` messages**: stored for replay ONLY after a console-read has been
  performed on the page while its storage was already non-empty (activation
  quirk). A read on an empty storage does NOT activate. Too fragile to build on.
- **Live window**: anything fired during `cmd_console`'s 3s listen is caught,
  unconditionally.

## Consequences for SP2 (revised)

- **One-shot `console --gate` IS the gate** — no persistent subscription, no
  streaming, no Playwright wall — but its contract is now stated precisely:
  - **Retroactive guarantee: exceptions only** (the primary breakage signal —
    uncaught errors). Pinned by e2e (`test_gate_catches_midflow_exception`).
  - **`console.error` guarantee: live 3s window** — the verify-core workflow
    calls the gate IMMEDIATELY after the action it checks (click/js/navigate),
    so async `console.error` from that action lands inside the window. Pinned
    by e2e (`test_gate_catches_console_error_live`).
  - Retroactive `console.error` replay may happen (activation quirk) but is
    NOT promised and NOT tested for.
- **Per-navigation scoping is free** (P4) — no loaderId filtering needed in the
  gate. Pinned by e2e (`test_gate_scoped_by_navigation`).
- **Pin dependency:** verified behavior of CfT 149.0.7827.54. The e2e pins
  (exception-retro, live-window, nav-scoping) make a future `update-cft.sh`
  pin-bump that breaks any leg fail loudly instead of silently weakening the gate.
- **SP0 footnote ¹ status: CLOSED, with precision** — cdp.py's one-shot console
  suffices for the gate; the uncertainty resolved into a two-leg contract
  (exceptions retro + console.* live) instead of the morning's over-broad
  "everything is replayed".

## Post-review addendum (same day — third channel)

The PR #168 code review found (and a live probe confirmed) that **browser-generated
errors — CORS blocks, CSP violations, `net::ERR_*` — never surface via the
Console OR Runtime domains**: they are `Log.entryAdded` only, and were invisible
to the gate even inside its live window. The gate now also enables the **Log
domain** (live window; level=error gates). The one-shot verdict above stands
unchanged — this widens the *channel set*, not the subscription model. Final
shipped contract: **exceptions retro + console.* live + Log-domain live**, with
the FAIL line carrying the per-leg breakdown. Pinned by
`test_gate_catches_log_domain_cors`.
