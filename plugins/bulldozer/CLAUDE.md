# Bulldozer Plugin

Adversarial review (`/bulldozer:check`) + visual browser verification (`/bulldozer:look`) + lightweight design consultation (`/bulldozer:consult`).

## Skills

| Skill | Command | What it does |
|-------|---------|-------------|
| check | `/bulldozer:check` | Adversarial review loop with external AI reviewer (model selection → `-c` reasoning overrides → structured ledger). For artifacts on disk. |
| look | `/bulldozer:look [URL] [task description]` | Browser automation via CDP, AppleScript, macOS native |
| consult | `/bulldozer:consult [design question]` | Lightweight stateless codex consultation for abstract design Q&A. For inline text without artifacts. Process-isolated via `--skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral -s read-only` from empty tmpdir. |

## Architecture: /look

`cdp.py` — 17 CDP commands, 3 communication channels:

| Channel | When | Commands |
|---------|------|----------|
| CDP WebSocket | websocket-client available (bundled) | All 17 |
| AppleScript + DOM injection | websocket missing | js, title, click, fill, wait, navigate, reload, viewport |
| macOS native | screenshot without websocket | screenshot only |

JAINE Browser = separate Chrome instance on CDP port 9333.

**Design principle:** cdp.py is used by JAINE (Claude Code agent), not a human. Design accordingly:
- Explicit flags (`--js`, `--full-page`) over heuristic auto-detection
- Parseable, stable output format over pretty formatting
- No silent fallbacks — warn on stderr when requested behavior degrades
- SKILL.md = API docs for the agent; if a capability isn't described there, JAINE won't use it

## Testing

### Test suites

| File | Type | Speed | Requires browser |
|------|------|-------|-----------------|
| `tests/test_cdp.py` | Structural (source analysis) | ~2s | No |
| `tests/test_e2e.py` | Behavioral (real browser) | ~2min | Yes |

### Running tests

```bash
# Structural tests (fast, offline)
pytest tests/test_cdp.py -v

# E2E tests (auto-launches JAINE Browser if not running)
pytest tests/test_e2e.py -v

# All tests
pytest tests/ -v
```

### Test page

`tests/fixtures/test-page.html` — deterministic HTML with known selectors for each command. Served by `conftest.py` on random port during e2e runs.

### Adding new commands — MANDATORY

Every new `cmd_*` function in `cdp.py` MUST have:

1. An entry in `COMMANDS` dict
2. A structural test in `test_cdp.py` (function exists + registered)
3. A behavioral e2e test in `test_e2e.py` (command works against real browser)
4. A test element in `test-page.html` if the command interacts with DOM

No command ships without all 4.

## Architecture: /check

Codex reasoning via `-c` overrides (profiles not supported in Codex 0.130.0):
- quick: `-c model_reasoning_effort=medium --ephemeral` + prompt `SKIP SKILLS.`
- standard/exhaustive: `-c model_reasoning_effort=xhigh`

Inter-round context: structured `review-ledger.yml` (cumulative, append-only) + full previous verdict as appendix. Codex outputs `LEDGER_PATCH` YAML block at the end of each verdict — Claude applies it to the ledger.

**CRITICAL:** Codex exec runs in FOREGROUND only. `-o` verdict file is written last — background + polling causes false truncation diagnosis.

## Feedback Protocol

Before working on skill improvements, check for feedback issues from other JAINE sessions:

```bash
gh issue list --repo A3IO/jaine-plugins --label feedback,bulldozer --state open
```

Feedback issues are created by JAINE-consumer sessions that encountered friction while using bulldozer skills. Each issue follows a structured template: what was attempted, what went wrong, workaround used, and plugin version.

When fixing a feedback issue:
1. Read the issue body for reproduction context
2. Fix the skill/code/documentation
3. Refresh consumer's plugin cache (`jaine-sync plugins update` or `rm -rf ~/.claude/plugins/cache/jaine-custom/bulldozer/`)
4. Close the issue with a reference to the fix commit

**CRITICAL:** Step 3 prevents stale cache — the root cause of false positives in issue #46 where 3/6 feedback items were invalid because JAINE-consumer was running an old plugin version.

## Versioning

`plugin.json` version auto-bumps on every merge to `bulldozer/main` via the `auto-calver` post-merge hook. Format: `YYYY.MM.DD` (first merge of the day) → `YYYY.MM.DD.N` (subsequent merges same day). **Do NOT bump manually** — it causes double-bump (your bump + auto-bump = `.1` suffix for a cosmetic-only PR).

## Architecture: skills-only

Plugin uses `skills/` directory exclusively. No `commands/` directory — per Claude Code docs, commands and skills are the same mechanism, and having both with the same name causes one to be silently dropped. Each `/bulldozer:*` invocation loads `skills/*/SKILL.md` directly.

## Architecture: consult vs check (issue #96)

`consult` and `check` solve overlapping problems with disjoint scopes:

| Aspect | `bulldozer:check` | `bulldozer:consult` |
|--------|-------------------|---------------------|
| Input | File/dir/diff on disk | Inline text in conversation |
| State | `.bulldozer/<session>-<artifact>/` (per-review dir, ledger, state) | None (each invocation independent) |
| Codex sandbox | Read-only at `-C $PROJECT_ROOT` | Read-only at empty `/tmp/bulldozer-consult-$$/` |
| Codex isolation flags | `--ephemeral` (quick only) | Always `--skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral` |
| Verdict format | LEDGER_PATCH YAML in clean `-o` file | Inline prose + GO/NO-GO/MINOR-FIXES + basis sentence, parsed fail-closed |
| Multi-round | Reviewer sees ledger + previous verdict as appendix | Each round independent; user re-prompts manually |
| Empirical verification | Required (`/receiving-code-review` discipline) | Not applicable — codex has no file access |
| Per-round cost | ~30-80s, ~50-100K tokens | ~4-15s, ~5-15K tokens |

**Routing rule (claude-side, enforced by pre-flight in consult Step 2):** if user's prompt mentions any artifact (file path, `.md`/`.py`/etc., "see specs/X", "attached"), redirect to `check`. Otherwise consult.

**Escalation rule** (consult Step 7): if `round_count >= 3` AND last 2 verdicts contain NO-GO, prompt the user to consider `/bulldozer:check`. User decides — we do NOT auto-invoke.

**REMOVED features** (validated against shipping by 3 of 4 independent codex dogfood runs):
- Persistent mode (`codex exec resume`) — data retention risk, stale context contamination, 2× implementation surface. Users wanting continuity copy prior verdict text into a new prompt.
- Session log with prompt content — only metadata in `bulldozer-consult.log`. Raw prompts and verdict bodies are not persisted.

## Known Issues (2026-05-11 review)

### Open

No known issues.

### In Progress

- **#94** — Trajectory analysis for exhaustive reviews. Step 8 now shows findings-per-round trajectory after every round ≥ 2. Step 8a prompts a pivot dialog when exhaustive + round ≥ 5 + avg last 3 rounds ≥ 3.0 fresh findings. Threshold calibrated against 26 historical sessions (0 FP on converged, 60% TP on non-converging). Addresses slow convergence on stateful concurrent control specs (state×event matrix class).

### Fixed

- **2026-05-16:** Three feedback issues from a JAINE-consumer session in `/0/SANDBOX/BRANCHLAB` (PR #57):
  - **#54** — `launch.sh` no longer passes `--force-dark-mode` or `--enable-features=WebContentsForceDark`. WebContentsForceDark was the sole cause of content recoloring; `--force-dark-mode` was verified inert on Dark-OS for CDP screenshots (chrome is never captured) and a latent risk on Light-OS — both dropped.
  - **#55** — `screenshot` gained `--clip X Y W H` (CSS-pixel region capture, mutex with `--full-page`) and `--scale N` (opt-in output resolution via `clip.scale = N / window.devicePixelRatio`). Every screenshot prints `PATH  W×H` on stdout. Empirical lesson encoded in cdp.py comments: `Emulation.setDeviceMetricsOverride{deviceScaleFactor:1}` does **not** change `Page.captureScreenshot` output size — only `clip.scale` does.
  - **#56** — SKILL.md Quick Invoke now instructs the agent to parse `$ARGUMENTS` into "URL token + task description". Previously the whole string was substituted into the URL slot, producing malformed navigation when a description was present.
  - **Post-review polish** (silent-failure-hunter + pr-test-analyzer + code-reviewer + comment-analyzer found 21 real findings, 1 FP): `_image_dimensions` rejects 0×0 from truncated headers (M2); WARN on stderr when `devicePixelRatio` read fails (SF1) or `_image_dimensions` returns None (SF3); `log("screenshot", …)` records `clip=`/`scale=` (M1); +14 structural tests covering arg-parse error paths, zero-DPR guard, native-path rejection of `--clip/--scale`; +5 unit tests for `_image_dimensions` (truncated JPEG/PNG, non-image, missing file, valid PNG); +1 e2e combo test (`--clip + --scale 1` → exact CSS-pixel output); tightened stdout format regex; `test_e2e.py` now imports `_image_dimensions` from `cdp.py` instead of duplicating the JPEG SOF parser; SKILL.md description/argument-hint updated per `/en/skills` docs (added triggers `capture region`, `check if X is aligned`, `захватить область`, `UI detail check`; argument-hint `[URL] [task description]`).

- **2026-05-14:** Commands/skills architecture — `commands/` directory removed, content merged into `skills/*/SKILL.md`. Root cause: `commands/check.md` and `skills/check/SKILL.md` both registered `/bulldozer:check`, only one loaded — consumer never saw Feedback section, Anti-patterns, Red Flags (336 lines of dead content). Same bug in look. (PR #51)
- **2026-05-11:** B1-B9 bugfixes, D1-D6 documentation fixes. See git log for details.

*Version: 1.8.0 | Last Updated: 2026-05-16*
