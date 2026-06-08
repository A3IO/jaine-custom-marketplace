# SP1 — CfT Empirical Findings (Task 8 probes)

*Date: 2026-06-05. Machine: mac-arm64, Darwin 25.4.0. CfT installed by `update-cft.sh`: **149.0.7827.54** (pin `cft/current`). Spec: `2026-06-04-look-drive-test-command-design.md` §5 SP1 row ("Empirical: …"). Probe lanes: 9360 (CfT+automation headful), 9361 (stock headful), 9362 (CfT no-automation headful) — per the conftest e2e port registry.*

## Results

| Probe | Question | Result |
|---|---|---|
| **A** | CfT process name in System Events (grounds hole B exact-name fallback + the lane contract) | **`Google Chrome for Testing`** — exactly `CFT_APP_NAME`. The same listing showed 3× `Google Chrome` + helpers from the daily browser running alongside: the old `'Google' in ownerName` substring would have matched all of them; exact-match/pid is load-bearing. |
| **B / R1-I** | Does `--enable-automation` show an infobar / shift the viewport in headful? | **No — automation adds NO visible UI.** At `--window-size=1440,900` (dpr 2): CfT+automation `innerHeight=757`; CfT *without* automation `innerHeight=757` (identical); stock Chrome `innerHeight=813`. The 56 CSS-px delta is **entirely CfT's own built-in "for automated testing only" banner** (RU: «Версия Chrome for Testing … предназначена только для автоматизированного тестирования … [Скачать Chrome]»), present in headful CfT regardless of our flags. |
| **B'** | Is the CfT testing banner suppressible? | Not by `--enable-automation` (shown WITH it) and not flag-dependent (shown WITHOUT it). It is CfT's hardcoded headful banner: closable per-session via its ✕, absent in headless (no UI), absent in stock Chrome (`/look` never sees it). Treat as a cosmetic constant of headful CfT. |
| **C / R1-K** | Keychain silence | `--use-mock-keychain` present in the lane argv (ps-verified). No macOS keychain prompt observed during the headful probe session (incl. https navigation). |
| **D / hole J** | "Where is" hang eliminated | Pid-targeted System Events call against a bogus `unix id 999999` returned in **0.27s** (error swallowed by `try`), no LaunchServices picker. launch.sh no longer resolves apps by name at all; cdp.py's `tell application` JS channel keeps its shipped `timeout=10`. |
| **Install** | Gatekeeper/quarantine on curl-downloaded CfT | None — the binary ran `--version` immediately after unzip (curl sets no quarantine xattr). |

## Raw numbers

```
9360 CfT + --enable-automation --use-mock-keychain  headful: {"w":1440,"h":757,"dpr":2}
9362 CfT (no automation)                            headful: {"h":757}
9361 stock Chrome (no automation)                   headful: {"w":1440,"h":813,"dpr":2}
System Events: Google Chrome, Google Chrome Helper ×3, Google Chrome for Testing, Google Chrome for Testing Helper
osascript bogus-pid probe: 0.266s total
```

Native desktop capture (banner visible): `/tmp/sp1-desktop-cft.png` (ephemeral; the banner text is quoted above).

## Consequences

- **SP1 constants confirmed:** `CFT_APP_NAME = "Google Chrome for Testing"` (System Events verbatim); exact-name/pid matching is mandatory next to a running daily Chrome.
- **R1-I closed with a decision input for SP2:** dropping `--enable-automation` in co-pilot-headful would buy NOTHING visually (the banner is CfT's, not automation's) while losing the bad-flags suppression and the automation semantics — default to KEEPING it in all drive lanes. The 56px banner is a headful-only cosmetic; CDP screenshots (viewport-only) never contain it, but **headful viewport height ≠ window-size minus stock chrome** — verify-core (SP2) must read `innerHeight` live, never assume it.
- **R1-K:** `--use-mock-keychain` ships in the automation argv; no prompts observed. Good enough for SP1; SP2's cookie-seed work revisits storage isolation.
- **Hole J closed constructively** (pid-targeting + `_osascript_to` timeout); the only name-resolving AppleScript left in the engine is cdp.py's JS channel (timeout-guarded) — the spec §6 "where possible" boundary.
- **pgrep `--` separator** (found live during Task 7): patterns starting with `--` require `pgrep -f -- <pat>` on BSD; without it pgrep exits 2 (usage) and `_chrome_pid_for_port` silently degraded to name-fallback. Fixed + pinned by unit test.
- **Headless teardown latency** (found live during Task 7): headless=new Chrome keeps serving CDP for >1s after SIGTERM; the `cft_browser` fixture now waits for actual port release (condition-based), making back-to-back e2e runs safe.
