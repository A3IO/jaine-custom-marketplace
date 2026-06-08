# Issue #110 — Hardening Roadmap Design Spec

## Problem

GitHub issue [#110](https://github.com/A3IO/jaine-plugins/issues/110) accumulated 23 verified follow-up items from PR #109 (composer wrapper) across 6 categories: hardening, structural, efficiency, simplification, docs. PRs #111 + #112 closed 9 of the original ~30 items via exit-code namespace hardening (17 leak sites → typed contract) and `_emit_stop` helper extraction. The remaining 23 items are all empirically verified as REAL (see Phase 1 verification in 2026-05-28 session), but no single PR can ship them all without scope-creep risk.

This spec decomposes the remaining work into **22 items shipping across 6 sequenced PRs** + **1 deferred research item (B6 calibrated pivot trigger)** = **23 items total**. Goal: each PR shippable in ≤4 hours, each independently reviewable, dependencies explicit, with B6 deferred to its own issue because it needs 26-session re-analysis (research, not coding).

### Item-to-PR mapping (for issue #110 closure)

| PR / Issue | Items |
|-----------|-------|
| PR-1 | B5 |
| PR-2 | A1 (minor), A2, A3, A4 |
| PR-3 | B1, B2, B3, B4, B7, B8 |
| PR-4 | C1, C2, C3 |
| PR-5 | D1, D2, D3, D4, D5 |
| PR-6 | E1, E2, E3 |
| Research issue (new) | B6 |
| **Total** | **23 items** (4 A + 8 B + 3 C + 5 D + 3 E) |

## Solution: Critical-first decomposition

Six PRs, ordered by risk-of-leaving-unfixed × cost-to-fix. B5 (manual fallback discipline) ships first because it re-creates the exact #98/#102 discipline failure the wrapper was meant to eliminate. Hardening trio ships second because all four items are surgical, verified, and follow the proven PR #111 pattern. Structural cluster, efficiency, simplification, docs follow.

| PR | Scope | Items | Est. coding | Why this order |
|----|-------|-------|-------------|----------------|
| PR-1 | Manual fallback discipline | B5 | ~3h | Closes discipline gap that shipped in PR1b — top correctness item. Requires per-item design choice (3 options below) before coding. |
| PR-2 | Hardening trio + A1 minor | A1, A2, A3, A4 | ~1.5h | All surgical fixes, TDD per case, dogfood pattern proven by PR #111. |
| PR-3 | Structural cluster | B1, B2, B3, B4, B7, B8 | ~3-4h | Refactor heavy. B7 (digraph) reflects PR-1 outcome, so blocks on PR-1 merge. Possible split into PR-3a / PR-3b if review surface too large. |
| PR-4 | Efficiency | C1, C2, C3 | ~2h | Benefits from B3 extraction in PR-3 (test fixture and trajectory tooling become independently optimizable). |
| PR-5 | Simplification | D1, D2, D3, D4, D5 | ~2h | Helper extractions. Mostly independent of other PRs but cleanest after structural cluster lands. |
| PR-6 | Docs | E1, E2, E3 | ~30 min | Reflects everything above. Last so docs aren't immediately stale. |
| Research issue (new) | Calibrated pivot trigger | B6 | TBD (research + ~1h code) | 26-session analysis + threshold derivation, then small code change. Belongs in its own issue with research-first cadence. |

**Total coding: ~12-13h.** Including per-PR overhead (branch + dogfood + PR + review): ~18-19h calendar time.

## PR-1 — Manual Fallback Discipline (B5)

### Current state

`bulldozer-round.sh` parser-exit-1 branch (grep `warning: no LEDGER_PATCH block`): wrapper echoes a warning + `exit 1`. SKILL.md Step 3 exit table tells caller (Claude) to read `verdict-rN.txt` and extract findings from prose manually. This is exactly the discipline failure #98/#102 were designed to eliminate — control returns to Claude's judgment instead of the deterministic state pipeline.

### Design options

| Option | Mechanism | Pro | Con |
|--------|-----------|-----|-----|
| A — Parser regex extraction | Parser, when no LEDGER_PATCH present, tries regex-based finding extraction from prose. Returns parsed-rN.json with `source: "regex_extracted"` warning. Wrapper exits 0. | Discipline preserved. Wrapper unchanged. | Regex extraction brittle; reviewer-prose format not stable. May miss or hallucinate findings. Hard to validate. |
| B — Wrapper synthesizes UNKNOWN + exit 11 | Wrapper calls log-round with verdict="UNKNOWN" + findings=0 + manual_extraction_pending=true; emits state.json normally; exits 11 "manual extraction required". SKILL.md branches on 11 to extract from prose AND update ledger; round itself is already logged. | Discipline preserved (state.json + bulldozer.log written every round). Distinct exit code = no overloading. Caller still does extraction but the round artifact exists. | findings=0 placeholder may confuse trajectory display; post-extraction caller must update state.json (additional protocol). |
| C — Hybrid: synthesize empty findings + flag | Parser, on no LEDGER_PATCH, synthesizes `{verdict: unknown, findings: [], source: "missing_ledger_patch", flag_for_manual_review: true}`. Wrapper exits 0. Caller sees flag, optionally extracts from prose. | Maximum discipline (no exit-code branch). GO-path possible if reviewer truly converged but skipped block. | Silent failure if reviewer had real findings to report but skipped LEDGER. Worse than current state (current exit 1 at least screams). |

### Recommendation: Option B

Reasons:
1. **Discipline invariant preserved** — log-round runs every round regardless of LEDGER_PATCH presence. state.json + bulldozer.log always written. The whole point of PR1b.
2. **Distinct exit code** keeps the branch explicit instead of overloading exit 1 (which means "no LEDGER" today and would mean "discipline-preserved-but-extract-prose" under Option C).
3. **Caller still extracts from prose** when needed (the synthesized round records what we know — "reviewer responded but skipped the structured block"), but the round IS logged before the prompt to extract fires. This is the difference vs current behavior: today the round is silently lost.
4. Option A's regex extraction is the riskiest — false findings from prose pattern-matching is worse than asking Claude to do it once with judgment.

### State update semantics for manual-extraction branch (REQUIRED)

`update-state.py` mutation block (grep `findings_total \+= findings`) is currently append-only / additive: `state["findings_total"] += findings`, `state["history"].append(...)`. There is no replace / upsert mode. Manual extraction therefore needs an explicit protocol — Option B does not work as a one-line "log findings=0 then have Claude re-call later".

Protocol:

1. **Round-N initial log** — wrapper calls `update-state.py` with `verdict="UNKNOWN"`, `findings=0`, plus a new boolean column `manual_extraction_pending=true` written to that round's `history[]` entry. Totals add zero (no double-count later).
2. **Wrapper exits 11** — caller (Claude) reads `verdict-rN.txt`, extracts findings from prose, computes the real count `K`.
3. **Round-N replace** — Claude calls `update-state.py --review-dir "$REVIEW_DIR" --mode=replace-extraction ROUND K VERDICT`. The explicit `--review-dir PATH` flag is REQUIRED — `update-state.py` currently resolves its target via `BULLDOZER_REVIEW_DIR` env var (default `.bulldozer` cwd-relative); the wrapper only sets that env in the `log-round.sh` subprocess scope, so Claude's later shell-context invocation would silently mutate project-root `.bulldozer/state.json` instead of the per-review state. Add `--review-dir PATH` to `update-state.py` CLI as part of PR-1; SKILL.md command MUST pass it. VERDICT is the 4th positional (GO or NO-GO) computed by Claude from the extracted findings — replace step MUST update `history[round=N].verdict` from "UNKNOWN" to this value so trajectory/pivot logic see correct state. Implementation: locate `history[round=N]`, set `findings=K`, set `verdict=VERDICT`, clear `manual_extraction_pending` flag; then `findings_total += K` (delta from the 0 logged at step 1). Atomic via the same tmp+rename pattern (grep `tmp.write_text\|os.replace`). Errors if the matching round entry is absent OR if `manual_extraction_pending` is already false (idempotency guard).
4. **Terminal-round pivot after replace** — if `ROUND == max_rounds AND VERDICT == "NO-GO"` after the replace, Claude MUST trigger the same AskUserQuestion pivot dispatch the normal-flow exit-10 path emits. SKILL.md Step 7 branch documents this. Pre-B3: Claude inlines the pivot question using `pivot-rN.json` schema. Post-B3 (separate emit-pivot.py extracted): Claude calls `emit-pivot.py --review-dir "$REVIEW_DIR" --round N --max-rounds M --depth D` which writes pivot-rN.json + emits the PIVOT marker on stderr, then Claude reads pivot-rN.json and wraps in AskUserQuestion. Either way, manual-extraction at terminal round MUST NOT silently exit without the pivot dialog.
5. **Trajectory display** — render-trajectory.py (post-B3 extraction) reads `history[].findings` and `history[].verdict` as today; the replaced values flow naturally. Add a pre-replace bypass branch only if dogfood shows trajectory mid-stream is misleading.

Tests required:
- exit 11 path: round logged with `findings=0` + `verdict="UNKNOWN"` + `manual_extraction_pending=true`; bulldozer.log has the round; `findings_total` unchanged
- `--mode=replace-extraction ROUND K NO-GO` happy path: updates `findings=K` + `verdict=NO-GO`, increments totals correctly, clears flag
- `--mode=replace-extraction ROUND K GO` (UNKNOWN→GO transition after reviewer skipped block but extraction shows no real findings): `findings=K=0`, `verdict=GO`, totals unchanged
- explicit NO-GO with K=0 (reviewer found problems but couldn't enumerate cleanly): `findings=0`, `verdict=NO-GO`, totals unchanged but verdict reflects real state
- replace-extraction error: missing round → exit 1; already-cleared flag → exit 1 (idempotency); unrecognized VERDICT (neither GO nor NO-GO) → exit 1
- trajectory after manual extraction shows extracted count AND correct verdict (not "UNKNOWN")
- **terminal-round manual extraction pivot**: if `ROUND == max_rounds` and post-replace verdict is NO-GO, AskUser pivot dialog MUST fire (same options as wrapper's exit-10 path)
- **cross-state-isolation regression**: invoking `update-state.py --review-dir $REVIEW_DIR --mode=replace-extraction ...` from a shell where `BULLDOZER_REVIEW_DIR` is unset must mutate `$REVIEW_DIR/state.json` only, NOT create or modify any project-root `.bulldozer/state.json`

### Other open design questions for PR-1 detailed plan

- SKILL.md exit table needs a new row for exit 11. SKILL.md Step 7 flow needs the manual-extraction branch + the call to `update-state.py --mode=replace-extraction`. Estimate: ~50 lines SKILL.md edits.
- Should `findings_count` in `bulldozer.log` log line emitted from `log-round.sh` (grep `date -Iseconds` append) update on replace-extraction, or stay frozen at the initial 0 with a separate `findings_count_extracted=K` column? Decision: stay frozen (log line is append-only audit trail; updates go to state.json).

### Dogfood plan

Standard depth bulldozer:check on bulldozer-round.sh and parse-ledger-patch.py + SKILL.md changes. Expected: 1-3 rounds. Focus reviewer on: exit-code contract consistency, state.json shape under new branch, SKILL.md accuracy of new flow.

## PR-2 — Hardening trio + A1 minor

### Items

- **A2 (multi-slash reviewer)** — `bulldozer-round.sh` reviewer-parse block (grep `MODEL="\${REVIEWER#\*/}"`). Current accepts `codex/openrouter/gpt-5.1` → MODEL=`openrouter/gpt-5.1` silently. Fix: validate exactly 2 NON-EMPTY segments. `if [[ ! "$REVIEWER" =~ ^[^/]+/[^/]+$ ]]; then exit 64; fi`. (Naive `== */*/* || != */*` was inadequate — passes `/gpt`, `codex/`, `codex/gpt`. The regex form rejects all 4 bad shapes: leading slash, trailing slash, multi-slash, no slash.) Tests: `codex/gpt` valid, `codex/openrouter/gpt-5.1` reject, `/gpt` reject, `codex/` reject, no-slash `codex` reject. ~5 lines + 5 tests.
- **A3 (ROUND not numeric)** — preflight. Current: ROUND="abc" reaches log-round → update-state.py ValueError → wrapper exits 70 with misleading "log-round.sh failed" diagnostic. Fix: `[[ "$ROUND" =~ ^[1-9][0-9]*$ ]]` at preflight (positive integer per wrapper usage block — grep `--round N            Round number` — which documents "1-based"; `0` is invalid because filenames `verdict-r0.txt` and the pivot guard `(( ROUND >= max_rounds ))` don't handle it). Exit 64 with clear "ROUND must be positive integer (1-based)". Tests for `abc`, `0`, `-1`, valid `1`, valid `999`. ~5 lines + 4 tests.
- **A4 (prompt body $(<file) E2BIG)** — `bulldozer-round.sh` (grep `prompt_body="\$(<"\$PROMPT_FILE")"`). Strips trailing newlines and risks ARG_MAX on Linux for large prompts (128KB cap). Fix: pass prompt via codex stdin (if codex exec supports stdin-prompt mode) OR pass `--prompt-file` flag direct to codex if supported. Need to verify codex CLI API. Fallback: keep current behavior for now and document the limit (defers to A4 follow-up). ~10-30 lines + 2 tests depending on codex API.
- **A1 minor (empty parser_out)** — `bulldozer-round.sh` parser_out block (grep `parser_out=\$(python3`). Current: if python3 returns 0 with empty stdout, downstream `findings_count=""` triggers log-round failure with misleading diagnostic. Fix: `[[ -n "$parser_out" ]]` guard after python3 → exit 70 with "empty parser output" clear message. ~5 lines + 1 test.

### Verification empirical (already done)

A2 reproduced via wrapper stub: `codex/openrouter/gpt-5.1` → wrapper exit 0, MODEL_RECEIVED=`openrouter/gpt-5.1`. A3 reproduced via wrapper stub: ROUND=abc → wrapper exit 70 with "log-round.sh failed" (misleading). A4 reproduced standalone: 14-byte source with `\n\n\n` tail → 11 bytes in var (3 newlines stripped). ARG_MAX = 1MB on macOS, 128KB on Linux.

### Dogfood plan

Standard depth on bulldozer-round.sh. Expected: 0-1 rounds; pattern of small contract additions matches PR #111 exit-code hardening.

## PR-3 — Structural cluster

### Items

- **B1 (depth → max_rounds duplicated)** — wrapper has the mapping in 2 places: preflight `case "$DEPTH" in quick|standard|exhaustive) : ;;` validation, and `max_rounds=1 / max_rounds=3 / max_rounds=10` derivation; SKILL.md "Depth Levels and Codex Configuration" table is the third. Fix: extract to `skills/check/data/depth-config.json` (`{"quick": {"max_rounds": 1, "reasoning": "medium", "ephemeral": true, "prompt_prefix": "SKIP SKILLS. "}, "standard": ..., "exhaustive": ...}`). Wrapper reads via jq or inline python3. SKILL.md table rendered from same source (manually maintained, but linked via test). Contract test: depth-config.json matches both wrapper behavior and SKILL.md table.
- **B2 (7-branch case-statement)** — `bulldozer-round.sh` parser-exit case (grep `case "\$parser_exit" in`). Each new parser exit code requires 3 synced edits (parser docstring + wrapper case + SKILL.md table). NOTE: data-driven JSON table approach was considered and REJECTED — each branch has dynamic content the JSON couldn't express without a template engine: exit-2 computes `${VERDICT_FILE%.txt}.malformed.yml`, exit-5 references both VERDICT_FILE and FULL_LOG paths, default `*` interpolates `${parser_exit}`. A static table would lose this OR introduce a new templating bug source. Revised fix: extract the 7 branches into helper `_emit_parser_exit_diagnostic EXIT_CODE` that reads outer-scope VERDICT_FILE / FULL_LOG / parser_exit. Each `case` branch reduces to a single helper call + the exit. Source of truth remains parser docstring (`parse-ledger-patch.py` — grep `Exit codes:`). Contract test: every parser exit code mentioned in the docstring has a wrapper `case` branch.
- **B3 (inline python3 heredocs)** — wrapper has ~70 lines of bash-embedded Python: trajectory block (grep `python3 - "\$ROUND" "\$max_rounds" "\${REVIEW_DIR}/state.json"`) and pivot block (grep `python3 - "\$ROUND" "\$max_rounds" "\$findings_count"`). Not unit-testable. Fix: extract to `skills/check/scripts/render-trajectory.py` and `skills/check/scripts/emit-pivot.py`. Wrapper calls as subprocess. Tests directly invoke the scripts.
- **B4 (pivot options hardcoded)** — `emit-pivot.py` (after B3 extraction) loads options from `skills/check/data/pivot-options.yaml` (default in plugin). Project can override via `.bulldozer/pivot-options.yaml`. Adding 4th option = data-file edit, no code change. Contract test: at least 3 default options, schema validation.
- **B7 (digraph refresh)** — SKILL.md `digraph review_loop` block still shows pre-PR1b architecture (Send → Read → Extract → Apply → Commit + log as separate nodes). Fix: rewrite to show "bulldozer-round.sh wrapper" as a single node with internal composition labeled, AskUser branch, exit-code legend. After B5 ships, reflect the new exit-11 manual-extraction branch.
- **B8 (VERDICT in parser, not wrapper)** — parser already emits `meta.verdict` when present; wrapper has fallback inference (`verdict = "GO" if not findings else "NO-GO"`). Fix: parser always emits canonical `meta.verdict` (synthesize for synthesized-bare-GO too, and infer from findings-list for raw LEDGER_PATCH with no verdict key). Wrapper drops the fallback inference; reads verdict directly. ~20 lines parser + ~10 lines wrapper removed + 3 tests.

### Dependencies inside PR-3

B3 (extract heredocs) → enables independent testing of trajectory + pivot
B7 (digraph) → reflects PR-1 (B5) outcome, so blocks on PR-1 merge

If PR-3 surface feels too large at implementation time, split into:
- PR-3a — foundation: B3 + B8 (extract heredocs, canonical verdict) — ~2h
- PR-3b — cleanup: B1 + B2 + B4 + B7 (data-driven dispatch, digraph) — ~2h

### Dogfood plan

Standard depth on bulldozer-round.sh, render-trajectory.py, emit-pivot.py, depth-config.json, pivot-options.yaml, SKILL.md, the new `_emit_parser_exit_diagnostic` helper in bulldozer-round.sh, plus parser docstring (single source of truth for parser exit codes per B2). Expected: 2-4 rounds (refactor-heavy, more edge cases).

## PR-4 — Efficiency

### Items

- **C1 (3-4 python3 spawns)** — wrapper spawns python3 for parse-ledger-patch + verdict/findings extraction (grep `parser_out=\$(python3`) + trajectory + pivot per round. Plus log-round → update-state.py. ~150-300ms wall × N rounds. NAIVE FIX (adding fields to parsed-rN.json) DOES NOT WORK — bash still needs a JSON parser to read the new field, no spawn saved. Real fix requires parser CLI change: add `--summary` flag that prints shell-safe `COUNT|VERDICT` to stdout while JSON is written to `--out PARSED_FILE`. Wrapper then reads stdout directly with bash `${parser_out%|*}` (no python3 spawn). Backward-compatible (default still prints JSON to stdout for direct callers). Alternative: drop C1 entirely — 1 spawn ≈ 50ms, not catastrophic, and the parser CLI change is moderately invasive. Decision deferred to PR-4 plan time.
- **C2 (state.json read 3×)** — `update-state.py` writes via `os.replace(tmp, state_file)`, trajectory python3 reads via `with open(state_path) as fp`, wrapper `cat "${REVIEW_DIR}/state.json"` emits to stdout. NAIVE FIX (capture log-round stdout) DOES NOT WORK — `log-round.sh` suppresses update-state.py stdout to /dev/null (grep `update-state.py`/`> /dev/null` block) AND wrapper suppresses log-round.sh stdout (grep `bash "\$LOG_ROUND"`/`> /dev/null`). Captured var would be empty. Real fix requires opening BOTH redirects (drop `> /dev/null` at both sites) AND adding stdout JSON contract tests for normal-exit and pivot-exit paths. Alternative: drop C2 entirely — 3× state.json read costs ~15ms total, negligible. Decision deferred to PR-4 plan time.
- **C3 (per-test codex stub install)** — ~76 wrapper tests; each calls `_install_codex_stub` which writes 2 files + chmod. Fix: pytest session-scoped fixture that builds one stub binary once, parametrized via env vars (`CODEX_STUB_EXIT_CODE`, `CODEX_STUB_VERDICT_FILE`) for per-test verdict body and exit code. ~30 lines fixture + ~20 lines test refactor.

### Dependencies

C1 builds on B8 (parser emits canonical verdict). C2 builds on B3 (render-trajectory.py is a standalone subprocess that can accept stdin). C3 stands alone.

### Dogfood plan

Quick depth (focused on efficiency claims, not full review). Expected: 1 round. Spot-check that observable behavior unchanged.

## PR-5 — Simplification

### Items

- **D1 (pre-write probe duplication)** — wrapper has `: > "$FILE" 2>/dev/null || _emit_stop 70 ...` at 2 sites: `: > "$FULL_LOG"` and `: > "$PARSED_FILE"`. With `mkdir -p "$REVIEW_DIR"` probe, 3 similar guard sites. Fix: helper `_probe_writable PATH LABEL` collapses to 1 line per site. ~15-line helper + 3 callsite reductions. **Pre-flight (2026-06-01):** confirmed 3 sites (`$FULL_LOG`, `$PARSED_FILE`, `mkdir $REVIEW_DIR`). The mkdir site is a *directory*-create probe — semantically distinct from the two file truncate-create probes; folding all three into one `_probe_writable` is questionable. Likely split: helper for the 2 file probes, leave mkdir (or pass an op-mode arg). Probe-failure→exit-70 is test-covered (`test_unwritable_full_log_exits_70` + PARSED_FILE R4-F4).
- **D2 (PARSER / LOG_ROUND path resolution)** — wrapper duplicates `${CLAUDE_PLUGIN_ROOT:+${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/X}` + `${X:-${SCRIPT_DIR}/X}` pattern at 2 sites: PARSER assignment (grep `PARSER="\${CLAUDE_PLUGIN_ROOT`) and LOG_ROUND assignment (grep `LOG_ROUND="\${CLAUDE_PLUGIN_ROOT`). Fix: helper `_sibling NAME` returns the resolved path. ~10-line helper. **Pre-flight (2026-06-01) — scope correction:** the pattern grew from 2 sites (spec estimate) to **6** after PR-3 (B1/B3) landed: PARSER, LOG_ROUND, RENDER_TRAJECTORY, EMIT_PIVOT, READ_DEPTH_CONFIG are homogeneous (`scripts/X` + `${SCRIPT_DIR}/X`); DEPTH_CONFIG is special (`data/depth-config.json` + `${SCRIPT_DIR}/../data/`). Each site also carries its own `if [[ ! -f ]] → _emit_stop 70` diagnostic block — a *second* duplication layer the spec didn't account for. `_sibling NAME` cleanly covers the 5 homogeneous sites; DEPTH_CONFIG needs a subpath arg or stays inline. Whether to also fold the if-not-found block is a plan-time decision. Path-resolution is test-covered (19 assertions).
- **D3 (bash codegen in `_install_codex_stub`)** — _(original spec: test helper uses f-string with `{{` escaping for embedded bash; ship stub as static asset at `tests/fixtures/stubs/codex.sh` + copy/chmod.)_ **SUPERSEDED by C3 (PR #125, merged) — DO NOT implement as written.** Pre-flight (2026-06-01) confirms C3 already eliminated the f-string codegen, by a *different* mechanism than this spec proposed: the stub body is now a plain `textwrap.dedent` module string (no f-string, no `{{` doubling) + a per-process module-cached template `os.replace`d atomically + symlinked into each install dir + per-install `stub_config`/`verdict_body.txt` sidecars read via `$0`'s dirname. The spec's "static asset + copy+chmod" path would *revert* C3's architecture = regression. D3's goal (kill the fragile f-string) is **done**. **Drop from PR-5.**
- **D4 (per-test env setup repeated 3×)** — `_run_wrapper` takes positional args + sets env internally; test classes that need extra env (PATH variants, BULLDOZER_LOG redirects) repeat the boilerplate. Fix: `_run_wrapper(..., extra_env=None)` kwarg. ~5-line helper change.
- **D5 (FOREGROUND admonition in 3 places)** — wrapper `FOREGROUND ONLY` comment block + SKILL.md Step 3 wrapper invocation note + SKILL.md "Common Mistakes" table row ("Running codex in background"). The wrapper structurally enforces FOREGROUND (no background-execution path exists in code). Fix: drop the Common Mistakes row that's now unreachable; keep wrapper comment and SKILL.md Step 3 as architectural documentation.

### Pre-flight blast-radius (2026-06-01, verified against C3-merged code @ efea8cd)

Baseline before any PR-5 work: **413 tests passed, 0 fail / 0 error** (`pytest -m "not slow"`, browser-suite `test_e2e.py` excluded; verdict from parsed junit-xml, NOT trusted from stdout).

| Item | Real state vs spec | Test net | Risk | Verdict |
|------|--------------------|----------|------|---------|
| D1 | 3 sites; mkdir ≠ file-probe (distinct op) | exit-70 covered | low-med | GO, split helper |
| D2 | 6 sites not 2; 5 homogeneous + DEPTH_CONFIG special; +if-not-found 2nd layer | 19 path tests | low (fails loud) | GO, design non-trivial |
| D3 | **already done in C3, by a different mechanism** | n/a | HIGH if spec applied blind | **DROP — done** |
| D4 | not done; ~43 direct `subprocess.run` outside `_run_wrapper` = real dup | n/a | low (default-safe kwarg) | GO |
| D5 | SKILL.md "Common Mistakes" row exists; no background path in code; row untested | safe | ~zero | GO |

Net: PR-5 is **not uniform in risk** — D3 must be dropped (C3 superseded it; blind application = regression), D2 scope tripled, D1 needs a helper split. D4/D5 trivially safe. Behavior-preservation for the refactor is provable by the 413-test baseline staying green with no test edits.

### Dependencies

~~D3 builds on C3 …~~ **D3 dropped (already done in C3, PR #125).** Remaining items (D1, D2, D4, D5) are independent of each other.

### Dogfood plan

Quick depth. Refactor-only. Expected: 0-1 rounds.

## PR-6 — Docs

### Items

- **E1 (BULLDOZER_REVIEW_DIR override)** — wrapper sets `BULLDOZER_REVIEW_DIR="$REVIEW_DIR"` for log-round child process (grep `BULLDOZER_REVIEW_DIR="\$REVIEW_DIR" BULLDOZER_DEPTH`). Not documented in SKILL.md. Fix: add a "Wrapper Environment Variables" subsection under Step 3 documenting BULLDOZER_REVIEW_DIR scope (set per-round, doesn't leak to caller env) and BULLDOZER_DEPTH similarly.
- **E2 (two-channel pivot signal)** — exit 10 isn't the only signal: stdout has state.json + stderr has `PIVOT: ...` marker + sidecar `pivot-rN.json` written. SKILL.md Step 3 exit table mentions only the exit code. Fix: add a "Pivot Signal Channels" note explaining all 4 channels and which the caller must read.
- **E3 (exit code extension contract)** — when parser adds code 6+, 3 places need synced update: parser docstring (source of truth), wrapper `case` branch + helper from B2, SKILL.md table. Fix: document the contract under "Extending parser exit codes" in SKILL.md, including the contract test (every docstring-listed exit code has a wrapper `case` branch).

### Dogfood plan

Skip dogfood (docs-only). Reasonable check via `markdownlint` and `mdspell` if available.

## Deferred: B6 — Calibrated Pivot Trigger (Research issue)

### Why deferred

B6 needs:
1. Re-analyze 26 historical `.bulldozer/SESSION-ARTIFACT/state.json` files from past dogfood sessions
2. Classify convergence patterns (which sessions converged on which round, which never did)
3. Derive new threshold (the original PR #95 trigger: `exhaustive + round ≥ 5 + avg last 3 ≥ 3.0`)
4. Code change: ~10-line wrapper update to add the calibrated trigger alongside current `round == max_rounds`

Step 1-3 = research, not coding. The current simple trigger ships safely (it's strictly more conservative — fires later than the calibrated one). Mixing research into a code PR = scope creep and unbounded estimate. Open a new issue `[research] calibrated pivot trigger threshold derivation from session corpus`.

## Success criteria

Issue #110 closed when:

1. All 6 PRs above merged to `bulldozer/main`
2. New issue for B6 research opened with link from #110 closure comment
3. Comment on #110 enumerates final state: all items closed in code, or migrated to new issue
4. SKILL.md Step 3 exit table reflects current contract (post-PR-1's exit 11)
5. Tests green on bulldozer/main; CI passes; CalVer auto-bump applies normally

## Risks

- **PR-1 (B5)** is the highest-judgment item. Option B is recommended but the open design questions (findings=0 placeholder, post-extraction state.json update protocol) may surface scope expansion at plan time. Mitigation: spec acknowledges this; writing-plans (next step) will surface concrete design before code.
- **PR-3 (structural cluster)** is the largest by volume. If review surface feels too large at implementation, splitting into PR-3a + PR-3b is pre-authorized in this spec. Mitigation: keep splits independent (B3 + B8 standalone foundation; B1 + B2 + B4 + B7 build on top).
- **A4 (prompt-body)** depends on codex CLI accepting either stdin prompts or `--prompt-file` flag. If neither supported, mitigation is documenting the ARG_MAX limit (deferring real fix) rather than expanding scope. Verify codex API at PR-2 plan time.
- **B6 research** may stay open indefinitely if Chris's time is bottleneck. That's acceptable — current simple trigger is safe. Don't gate #110 closure on it.

## Open questions

- **Test parallelism:** Do we want to opt the wrapper test suite into xdist (`-n auto`)? Per JAINE testing doctrine the answer is yes, but it requires verifying `_install_codex_stub` is properly isolated per worker. Could be done in PR-4 (efficiency) or its own micro-PR.
- **Per-PR dogfood model selection:** PR #111 used gpt-5.5. Should subsequent PRs use a different model for triangulation, or stick with gpt-5.5 for consistency? Default: gpt-5.5 unless reviewer-model-fatigue observed.
- **Backport of B5 to in-flight sessions:** if a session is mid-review with stale wrapper, does the new exit 11 behavior affect them? No — `.bulldozer/SESSION/` state is per-review, wrapper update only affects subsequent rounds. Safe.

---

*Version: 1.5.0 | Last Updated: 2026-06-01*

*Changelog:*
*- 1.5.0 (2026-06-01) — Pre-flight blast-radius before PR-5 implementation, verified against C3-merged code (baseline 413 tests green, junit-xml parsed). Findings: **D3 SUPERSEDED by C3 (#125)** — drop, do not implement (the spec's copy+chmod static-asset path would revert C3's textwrap+symlink+sidecar architecture = regression); **D2 scope tripled 2→6 sites** (PR-3 B1/B3 added RENDER_TRAJECTORY/EMIT_PIVOT/READ_DEPTH_CONFIG/DEPTH_CONFIG) and is non-homogeneous (DEPTH_CONFIG special + per-site if-not-found block = 2nd dup layer) — helper design deferred to plan; **D1 mkdir site semantically distinct** from file probes — likely split helper. D4/D5 confirmed low-risk. No code changed — pre-flight analysis only.*
*- 1.4.0 (2026-05-28) — Round 4 dogfood (codex/gpt-5.5 standard, user-extended past max-rounds pivot) fixes: PR-2 A2 reviewer regex tightened from `*/*/* || != */*` (which still admitted `/gpt`, `codex/`, `codex/gpt` with empty provider/model segments) to `^[^/]+/[^/]+$` (R4-F2). Item count reconciled from "22 remaining" to "23 total (22 in 6 PRs + 1 research)" with explicit item-to-PR mapping table (R4-F1). PR-3 dogfood plan rewritten — no longer names rejected `parser-exit-table.json` (R3-F2 still_open completed).*
*- 1.3.0 (2026-05-28) — Round 3 dogfood (codex/gpt-5.5 standard, max-rounds pivot → user chose continue) fixes: PR-1 manual-extraction protocol now takes VERDICT as 4th positional arg (R3-F1) — replace step updates `history[].verdict` from UNKNOWN to actual GO/NO-GO so trajectory/pivot dispatch see correct state; added explicit terminal-round pivot step (step 4) requiring AskUser dialog if manual extraction lands at max-rounds with NO-GO; tests expanded to 8 cases including UNKNOWN→NO-GO, UNKNOWN→GO, explicit-NO-GO-zero-findings, terminal-pivot, cross-state-isolation. PR-3 dogfood plan no longer mentions rejected parser-exit-table.json (R3-F2).*
*- 1.2.0 (2026-05-28) — Round 2 dogfood (codex/gpt-5.5 standard) fixes: PR-1 manual-extraction `update-state.py` invocation now mandates explicit `--review-dir PATH` flag (R2-F1 — without it, Claude's shell-context call falls back to project-root `.bulldozer/state.json` instead of per-review state); standardized flag name on `manual_extraction_pending` (was inconsistently `manual_extraction_required` in design-options table vs `_pending` in semantics section); added cross-state-isolation regression test requirement. R1 findings R1-F1..R1-F5 confirmed verified by reviewer.*
*- 1.1.0 (2026-05-28) — Round 1 dogfood (codex/gpt-5.5 standard) fixes: PR-1 state update semantics specified (R1-F3); PR-2 A3 regex tightened to `^[1-9][0-9]*$` (R1-F4); PR-3 B2 JSON-table approach REJECTED, helper-extraction adopted instead (R1-F5); PR-4 C1+C2 marked plan-time decisions with realistic cost/benefit (R1-F1+R1-F2); PR-6 E3 updated to match B2 revision. All line-number refs replaced with grep patterns per CLAUDE.md drifting-counts rule.*
