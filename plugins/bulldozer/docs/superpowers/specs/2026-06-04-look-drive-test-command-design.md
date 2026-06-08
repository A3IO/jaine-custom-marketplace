# `/look` → `/look` + `/drive`: Browser Test-Command Epic

*Status: design / umbrella-spec. One design; a separate implementation plan per sub-project (SP0–SP4).*
*Date: 2026-06-04*
*Origin: issue #164 (migrate JAINE Browser to Chrome for Testing).*

## 0. TL;DR

Split the single `/bulldozer:look` command into two, cut on **environment** (not on "human vs automaton"):

- **`/look`** — unchanged: the user's daily browser (stock Chrome, profile on CDP `:9333`, his real logins), headful, observational. "Show me what's on *my* screen."
- **`/drive`** — new: web-only, **Chrome for Testing** (CfT, isolated/version-pinned), product testing in a clean environment. Agent-driven with a **verify-core** (proactive self-verify loop + assertion primitive + console-gate) and two modes: *autonomous* (delegatable to subagents, headless) and *co-pilot* (headful, human confirmation checkpoints).

The boundary is **data-backed** (24 068 telemetry calls + 10-project transcript mine, 2026-06-04) and **hole-hardened** by two informed `consult --panel` rounds (codex+grok+gemini reading the real code).

## 1. Problem & origin (#164)

Root cause (#164, verified): JAINE Browser is a *second instance of the same bundle* `/Applications/Google Chrome.app` as the user's daily Chrome. Isolation rests only on `--user-data-dir` + `--remote-debugging-port`, not on the binary. Consequences: "unsupported command-line flag" infobar, auto-update screenshot drift (no version pinning), per-bundle managed-policy leak to daily Chrome, profile-lock risk. CfT (separate bundle `com.google.chrome.for.testing`, no auto-update, full CDP parity, mac-arm64) strikes the root.

Research (durable copy): `docs/superpowers/analysis/2026-06-04-chrome-for-testing-flags-research.md` (17-agent swarm, refuted claims flagged). Key verified facts:
- `--enable-automation` suppresses `ShowBadFlagsPrompt` (the infobar). `--test-type` does **not** (refuted). `--disable-infobars` removed Jan 2018 (refuted).
- **CfT alone does NOT hide the infobar** — CfT + `--enable-automation` both required.
- `--enable-automation` side effects: `navigator.webdriver=true`, suppressed password-save UI, no auto-reload on network errors → **must NOT go to the daily 9333 browser**.

## 2. The cut: environment, not human-vs-automaton (data-backed)

The empirical mine (§9) **disproved** a "human vs automaton" cut: heavy real users are already 65–70 % autonomous-drive *while a human is present and confirming* (ASSISTS 65/35, Ruslan 70/30, VRHOT 70/30, bulldozer-self 70/30). "Human in the loop" is a property of nearly every session → it cannot be the discriminator. The real separable axis is **environment**:

| | `/look` (casual) | `/drive` (test) |
|---|---|---|
| Browser | stock Chrome — **unchanged** | Chrome for Testing (pinned) |
| Profile | daily `:9333`, **user's real logins** | isolated/version-pinned (+ opt-in cookie-seed, §4.5) |
| Intent | observe *his* browser state | test the *product* in a clean env |
| Flags | no automation, headful | `--enable-automation` (+ opt-in fake-media), headless-capable |
| Run | human looks | agent-driven + subagent delegation |
| co-review (#3) | — | **here** (co-pilot mode) |

**Why co-review belongs to `/drive`:** its defining traits *are* `/drive`'s (runs on the product build / CfT, asserts/exercises functionality); a pause-for-"so? do you like it?" is a stop *inside a test flow*, not a different tool. Putting it in `/look` would drag CfT + assertion machinery into the casual browser and re-merge the scopes the redesign separates.

**Bonus:** this resolves the recurring pain "user sees X on his profile that the CDP browser doesn't reproduce" (ASSISTS, GOATsEXPLORER) — that gap *is* the `look`(his-profile) vs `drive`(clean-CfT) boundary. Honoring it makes the discrepancy expected, not a bug.

## 3. `/drive` overview

- **Web-only** (CDP). Native targets (terminals, Qt) are **out of scope** — see §7. The mine showed 4/10 projects are native with their own screenshotters; `/drive` does not pretend to cover them.
- **Chrome for Testing**, isolated/version-pinned profile.
- **Two modes** over one verify-core:
  - **autonomous** — runs to completion, emits pass/fail, delegatable to subagents (headless default for fan-out).
  - **co-pilot** — headful, stops at confirmation checkpoints, surfaces to the human ("so? does this look right?").
- **verify-core** (the real new value, from top pains §9): proactive self-verify loop + assertion primitive + console-gate.
- **Auth**: opt-in cookie-seed of selected domains into the CfT lane (§4.5).

## 4. Architecture

### 4.1 Shared engine, two fronts
`cdp.py` + `launch.sh` (already lane-parameterized: `CDP_PORT`, `LOOK_PROFILE_DIR`, `LOOK_HEADLESS`, `LOOK_INSECURE`) remain the shared engine. The two commands differ in their `SKILL.md` defaults, not in the engine. `/drive` inherits the already-shipped look-v2 fixes (console, wait, trusted-click `Input.dispatchMouseEvent` PR#140, `--target` tab-pin, `--insecure` isolated lane, full-page/`--clip`).

**Coupling guard (panel R2):** because the engine is shared, every CfT/automation behavior MUST be gated so it cannot reach the `/look` 9333 path. The boundary is enforced at launch: CfT binary + automation flags only on a non-9333, non-daily-profile lane.

### 4.2 CfT foundation (SP1, = #164)
- Parameterize the hardcoded Chrome app-name. In `cdp.py` it is the symbol `CHROME_APP = "Google Chrome"` (`cdp.py:52` + ~9 AppleScript callsites — grep `CHROME_APP`). In `launch.sh` there is **NO `CHROME_APP` symbol** — the 3 literals are the AppleScript strings `tell application/process "Google Chrome"` (grep the **string** `"Google Chrome"` — the `tell application`/`tell process` lines in the headful JS-enablement menu block). Thread both through a new env (`CHROME_APP_NAME`). Note the `CHROME_BIN` default in `launch.sh` is already an env-parameterized binary *path* — NOT one of the 3 app-name literals.
- `launch.sh` gains a CfT binary/argv switch + `--enable-automation` launch path (panel R2 Grok: SP1 cannot launch `/drive` without this — do NOT duplicate the launcher).
- Install/pin CfT to `/0/.jaine/.browser/cft/` (CfT mac-arm builds already present in `~/.cache/puppeteer/chrome` — verify with `ls`) + `scripts/update-cft.sh`.
- `--enable-automation` gate: **explicit drive-lane + non-daily-profile**, not port-only (panel R1-C / #160).
- CfT-aware `conftest.py`: `/drive` gets its **own** CfT e2e fixture — it must not rely on the stock-Chrome baseline (panel R1-A test-gap). `conftest.CHROME` baseline stays stock Chrome.

### 4.3 verify-core (SP2) — reliability is the hard part
Panel R2 showed the verify-core is non-trivial. Requirements:
- **assertion primitive**: emits pass/fail (not prose). Distinguishes flaky from real fail via an actionability + stability window — NOT bare DOM-presence polling (panel R2 SHARED). This is the strongest argument for Playwright (§4.6) whose `expect`/locators auto-wait + check actionability.
- **navigate waits for load** (DOMContentLoaded/networkidle + final-URL check) before any assert (panel R2-N: `cmd_navigate` currently returns immediately → assert races stale DOM).
- **console-gate** must capture errors from *prior* render, not only during the command window (panel R2-O: `cmd_console` enable timing). SP0 spike verified `cdp.py console` DOES catch a buffered retroactive error via `Console.enable` replay (the one-shot subprocess model suffices for the basic case); a streaming/subscription variant is still useful for ongoing async ops.
- **screenshot binding**: every self-verify screenshot is bound to the current navigation/result (timestamp/URL/loader id) so a stale screenshot can't false-pass (panel R2-T; the mine showed the AI makes false visual conclusions from stale screenshots).
- **trusted-click enforcement**: a fallback to untrusted `el.click()` must NOT report success via exit-0 (panel R2-U); the verify-core reads the trust signal, not just the exit code.
- **loop circuit-breaker + rebuild awareness**: the proactive self-verify loop has a hard max-iteration breaker (no endless fix-and-fail token burn) and waits for DevServer/HMR rebuild before testing (panel R2-Q).

### 4.4 Two modes — structural separation (panel R2-P)
A headless subagent that hits a co-pilot "confirm?" checkpoint would hang forever. Therefore:
- **co-pilot mode is main-session-only** (where a human channel exists).
- **subagents are ALWAYS autonomous** — co-pilot is structurally forbidden in a subagent (the delegation prompt hard-codes autonomous; `/drive` detects the absence of a human channel and refuses to pause).

### 4.5 Auth: cookie-seed (panel R2 SHARED Auth-gap)
Environment-cut leaves authenticated/SSO-gated products uncovered (clean CfT stops at login; the mine showed the user *does* test authenticated products — Ruslan oracle on LAN, Matrix Element, monitors — today via `/look` on 9333 with his logins). Resolution (user decision): `/drive` opt-in **imports cookies/storage-state of *selected domains*** into its isolated CfT lane (the `browse:cookie-sync` pattern). CfT stays clean/pinned/reproducible; it sees only the chosen domains' auth. Not the full daily profile.

### 4.6 Playwright overlay (SP3, conditional on SP0)
Playwright attaches to the running CfT lane via `connect_over_cdp` (or `@playwright/mcp --cdp-endpoint`) — it does **not** replace `launch.sh`, it enriches: auto-wait, robust locators, `expect`. Cost: a heavy dependency + a dual browser-stack.

**SP0 verdict (2026-06-04, measured): bounded both.** `cdp.py` is the **default** engine — reliability- and console-equivalent to Playwright in the spike (best cdp.py and Playwright both 10/10; both DETECT the console error), zero-dependency, single-stack with `/look`. Playwright is a **per-test, explicit opt-in** (`--engine playwright`) ONLY when a real test demonstrably hits a cdp.py wall (rich locators, actionability beyond `wait --js`, PW-only features) — "might be nicer" is not a trigger. Never mixed within a test; identical pass/fail contract across tests; Playwright isolated to `/drive`, never `/look`. This explicit boundary is what prevents the dual-stack divergence panel R2 warned about. Full data + boundary: `docs/superpowers/analysis/2026-06-04-sp0-engine-decision.md`.

## 5. Sub-projects (umbrella; per-SP implementation plans)

Order: SP0 → SP1 → SP2 → SP3 → SP4. Each gets its own `writing-plans` cycle → TDD → `bulldozer:check`.

| SP | Scope | Key requirements / holes addressed |
|---|---|---|
| **SP0** | ✅ **DONE (bounded both)** — Playwright-vs-`cdp.py` spike, measured on a real lane | Decision criteria measured (auto-wait reliability, actionability, console-gate detection, trusted-input, ergonomics/LOC, dependency cost, `connect_over_cdp` fidelity to the lane — CfT-specific re-verified in SP1). Verdict: cdp.py default + Playwright per-test opt-in; explicit boundary, no hidden divergence. See decision doc. |
| **SP1** | ✅ **DONE (2026-06-05)** — **CfT foundation (#164)** | `CHROME_APP_NAME` param (R2 G/J/B *how*: native owner by pid/bundle not `split()[0]="Google"`, shipped as pid-first `_chrome_pid_for_port` + exact-name fallback; English menu; `tell application` hang fail-safe — shipped as pid-targeted System Events + `_osascript_to` timeout). CfT install/pin + `update-cft.sh` (149.0.7827.54 pinned). `launch.sh` CfT binary + `--enable-automation` path. Gate = drive-lane + non-daily (R1-C). CfT-aware conftest (R1-A: `cft_browser`, port 9359). Temp profiles for drive lanes (R1-E: `$TMPDIR/jaine-drive-<port>`). Empirical (R1-I **answered**: `--enable-automation` adds NO visible UI — the 56 CSS-px headful banner is CfT's own, flag-independent → keep automation in all drive lanes; R1-K `--use-mock-keychain` silent; System Events name = `Google Chrome for Testing`): `docs/superpowers/analysis/2026-06-05-sp1-cft-empirical-findings.md`. |
| **SP2** | ✅ **DONE (2026-06-05)** — **`/drive` command + verify-core** | Shipped as opt-in cdp.py extensions (panel-validated vs separate verify.py): `navigate --wait` (unified `Page.lifecycleEvent` + loaderId match — prior-page race closed), `console --gate` (three-channel contract: exceptions retro + console.* live window + Log-domain live for CORS/CSP/net::ERR — the morning "everything replayed" claim was falsified during TDD and the Log-domain blindness was caught by code review, see `2026-06-05-sp2-console-gate-verification.md`), `assert` (stability window + flap diagnostics + `--actionable` = visible+enabled+hit-test with click's scroll parity), `click --require-trusted` (R2-U), `screenshot --bind` (url/loader/t), `skills/drive/scripts/cookie_seed.py` (two-port, never INTO the daily browser — 9333 or a CDP_PORT-overridden daily), `skills/drive/SKILL.md` (two modes §4.4 — subagents always autonomous; circuit-breaker 3; OAuth handoff R2-S; hole D pre-flight). Plan went bulldozer:check GO (E1×2 + 3 codex rounds, 4 findings all REAL). |
| **SP3** | **Playwright opt-in overlay** (SP0 verdict: bounded both — cdp.py default, Playwright per-test for complex UIs) | `connect_over_cdp` to CfT lane (`ATTACH_OK` spike-verified); built only when a real test hits a cdp.py wall (YAGNI), behind a per-test `--engine` flag with identical contract. |
| **SP4** | ✅ **DONE (2026-06-05)** — **Subagent delegation + model-routing calibration** | Ephemeral-by-construction lanes (`--remote-debugging-port=0` + `mktemp` profile; the unique profile IS the ownership token, holes R1-H + R2-R closed structurally — allocator/lock dropped) shipped in `launch.sh` (4-line contract, fail-loud LANE_FAIL) + `skills/drive/SKILL.md` "Subagent delegation". Calibration: 111-run corpus × {haiku, sonnet, opus} via the Workflow tool, externally graded from runner-owned logs + T10 integrity re-runs. **Routing verdict: sonnet** (27/27 verify, honest classification; opus identical accuracy at ~5× cost = no benefit; haiku unreliable — 15/27 verify, "pass"-bias, overclaims 15/37). Breaker stays 3 (max 2 complete cycles observed, 0 censored). PR1 #178 (infra) + PR2 #180 (experiment + routing table). Full results: `docs/superpowers/analysis/2026-06-05-sp4-model-routing-calibration.md`. |

## 6. Holes addressed (panel R1 + R2)

| Hole | Round | Where addressed |
|---|---|---|
| A conftest CfT test-gap | R1 | SP1 — own CfT e2e fixture |
| B native_screenshot owner="Google" collision | R1+R2 | SP1 — owner by pid/bundle, not `split()[0]` |
| C gate port-only weak (#160) | R1 | SP1 — drive-lane + non-daily gate |
| D endpoint verification (wrong browser on port) | R1 | SP2 — verify endpoint = expected CfT binary/profile |
| E persistent vs temp profiles | R1 | SP1 — drive lanes use temp profiles + cleanup |
| F Playwright exclusive CDP → cdp.py MVP wasted | R1 | SP0 — decide engine first |
| G Russian menu on English CfT | R1+R2 | SP1 — English-aware menu / locale-robust enablement |
| H parallel subagents pkill collision | R1 | SP4 — ephemeral port (port=0) + unique mktemp profile (revised 2026-06-05) |
| I `--enable-automation` infobar/viewport shift | R1 | SP1 — empirical; drop in co-pilot-headful if confirmed |
| J AppleScript "Where is" hang | R1+R2 | SP1 — timeout fail-safe, path-based not name-based where possible |
| K keychain/proxy leak | R1 | SP1 — `--use-mock-keychain` + isolation flags |
| Auth gap (SSO/login-gated uncovered) | R2 SHARED | SP2 — cookie-seed selected domains (§4.5) |
| assertion flaky-vs-real | R2 SHARED | SP2/SP3 — actionability + stability window |
| navigate doesn't wait for load | R2 | SP2 — navigate waits DOMContentLoaded/networkidle |
| console-gate timing misses prior errors | R2 | SP2 — one-shot replay verified sufficient on pinned CfT (mid-flow exception + console.error replayed; buffer clears per-navigation; no streaming needed — `2026-06-05-sp2-console-gate-verification.md`) |
| screenshot not bound to navigation | R2 | SP2 — bind screenshot to result |
| click fallback exit-0 untrusted | R2 | SP2 — trust signal, not exit code |
| subagent co-pilot hang | R2 | SP2 — co-pilot main-only, structural (§4.4) |
| lane ownership/allocator | R2 | SP4 — unique profile IS the ownership token; allocator dropped (revised 2026-06-05) |
| OAuth/popup tab handoff | R2 | SP2 — target-handoff first-class |
| verify loop circuit-breaker + HMR | R2 | SP2 — max-iteration breaker + rebuild-await |
| dual-stack divergence | R2 | SP0 — bounded split, defined boundary |

## 7. Out of scope (explicit)

- **Native testing** (terminals, Qt, statusline) — the widest pain by coverage, but a *different* technical stack (Quartz/AppleScript/SIGUSR1, not CDP). `/drive` stays web-only; native is a separate future design if ever pursued. Their existing custom screenshotters work and are not broken.
- **`/look`** — zero changes (stock Chrome + 9333 daily profile, headful).
- **External `/0/.jaine/.browser/launch.sh`** (vault, legacy launcher on the same profile/port) — not touched.
- **`conftest.CHROME` baseline** — stays stock Chrome (casual baseline).
- **#160** (non-9333 lane can point at daily profile) — orthogonal; the cut weakens it naturally (drive always isolated), but it remains its own issue.

## 8. Testing

- TDD throughout. `LOOK_DRY_RUN` is the seam for gate/argv tests (no browser launch).
- `/drive` gets its own CfT e2e (SP1 fixture), not piggybacking the stock baseline.
- verify-core: tests for flaky-vs-real classification, navigate-wait, console prior-error capture, screenshot-binding, circuit-breaker termination.
- SP4 calibration is itself a test-driven experiment (corpus × models → metrics).
- Each SP closes with `bulldozer:check` (dogfood, the plugin reviewing its own change).

## 9. Empirical basis

- **Usage/pain mine** (workflow `wrm180w96`, 2026-06-04): 24 068 `look.log` telemetry events + transcripts of 10 projects (JTerm2, STATUSLINE, JKFDMon, JHOSTTY, jaine-speech, VRHOT, ASSISTS, PROBLEMSOLVER/Ruslan, LLM-CLUSTER, bulldozer-self). Findings that shaped this design: real use is 65–70 % drive (`js`+`navigate` ≫ `screenshot`); ~100 % headful; human present-but-not-per-step (boundary ≠ human-vs-automaton); native = web-only gap; #1 feature-pain = AI fails to self-verify its own fixes ("did you even check?"); one autonomous pass resolved what 18 manual screenshots could not.
- **Research**: `docs/superpowers/analysis/2026-06-04-chrome-for-testing-flags-research.md`.
- **Adversarial hardening**: two informed `consult --panel` rounds (codex+grok+gemini reading the real code) — R1 found 11 holes (A–K), R2 found the Auth gap + verify-core reliability class + structural-mode + lane-ownership holes. All mapped in §6.

## 10. drive feature roadmap (from ranked pains — guides SP2/SP4 + backlog)

1. Proactive self-verify loop (highest leverage).
2. Assertion primitive (pass/fail).
3. Console-gate in the test flow.
4. Headless + parallel-isolated lanes as the subagent default.
5. Trusted input end-to-end (mouse #140 shipped → keyboard/file-upload/drag).
6. Deterministic wait-for-condition wired into navigate/click + auto-reconnect after navigate.
7. Dynamic-state capture (multi-frame/video/event-triggered) for animations/transients/hover.
8. LAN/`file://` fetch in CfT isolated lanes (`--insecure` sub-D is exactly this fail-closed context).
9. Complete-capture defaults (full-page + element-clip-by-selector + scroll-to-element) + remote-target driving (SSH CDP).
