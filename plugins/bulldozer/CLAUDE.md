# Bulldozer Plugin

Adversarial review (`/bulldozer:check`) + visual browser verification (`/bulldozer:look`).

## Skills

| Skill | Command | What it does |
|-------|---------|-------------|
| check | `/bulldozer:check` | Adversarial review loop with external AI reviewer (model selection → `-c` reasoning overrides → structured ledger) |
| look | `/bulldozer:look [URL]` | Browser automation via CDP, AppleScript, macOS native |

## Architecture: /look

`cdp.py` — 17 CDP commands, 3 communication channels:

| Channel | When | Commands |
|---------|------|----------|
| CDP WebSocket | websocket-client available (bundled) | All 17 |
| AppleScript + DOM injection | websocket missing | js, title, click, fill, wait, navigate, reload, viewport |
| macOS native | screenshot without websocket | screenshot only |

JAINE Browser = separate Chrome instance on CDP port 9333.

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

## Known Issues (2026-05-11 review)

### Open

No known issues.

### Fixed

All findings from 2026-05-11 review resolved: B1-B9 bugfixes, D1-D6 documentation fixes. See git log for details.

*Version: 1.5.0 | Last Updated: 2026-05-12*
