# Codex MCP: permissions-grant echo + test-log hygiene — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two real defects in the bulldozer codex MCP bridge found by mining `~/.claude/hooks/bulldozer-codex.log`: (1) an accepted `item/permissions/requestApproval` grants an EMPTY permission profile (a silent no-op — #4), and (2) the test suite writes drift/approval lines to the REAL monitoring log, polluting it and making the #251 corpus uncalibratable.

**Architecture:** Both fixes are surgical. (1) On accept, echo the codex-requested `RequestPermissionProfile` (`params["permissions"]`) into the response instead of `{}` — the request and response profiles share an identical shape (verified against the codex 0.141 JSON schema: `PermissionsRequestApprovalParams.permissions` ↔ `PermissionsRequestApprovalResponse.permissions`). (2) Add an autouse pytest fixture redirecting `BULLDOZER_CODEX_LOG` to a per-test tmp path for EVERY test (offline AND slow), so no test ever appends to the real log.

**Tech Stack:** Python 3.11+ stdlib only (`mcp/codex_server.py`), pytest (`tests/test_codex_mcp_v2.py`).

## Global Constraints

- **Python 3.11+** — bridge requires `tomllib`; no new deps (stdlib only).
- **After ANY `mcp/codex_server.py` edit:** run the FULL suite including `-m slow` (real codex 0.141, ~3 min) before claiming done. Offline-only is insufficient.
- **No manual `plugin.json` version bump** — CalVer auto-bumps on merge to `bulldozer/main`.
- **Decline/timeout safe-default is unchanged** — `PERM_DECLINE = {"permissions": {}, "scope": "turn"}` stays the non-accept fallback. This plan does NOT touch the #251 auto-accept question (blocked, separate).
- **Fingerprint:** `tests/fixtures/codex-protocol-fingerprint.json` pins only the method NAME in the bridged-methods list, NOT the permissions response shape — so no fingerprint edit is required (verified by grep). Do not add one.
- **TDD:** every code change starts with a failing test that is RUN and seen to fail before the fix.

---

## File Structure

- `mcp/codex_server.py` — the `item/permissions/requestApproval` handler inside `_bridge_approval_dispatch` (currently ~the block beginning `if method == "item/permissions/requestApproval":`, `perm_pairs` list). Only that block changes.
- `tests/test_codex_mcp_v2.py` — add one autouse fixture + one regression test for the log redirect (Task 1), and one new behavioral test for the grant-echo (Task 2). Near the existing autouse fixtures `_isolate_codex_home` / `_reset_cc_stream_fixture`.
- `CLAUDE.md` — Task 2 Step 6 updates the "Known v1-of-v2 limitation" paragraph (the #4 gap it documents is now closed).

No other files change. The existing permissions tests (`test_permissions_and_legacy_human_labels_round_trip`, `test_permissions_request_gets_permissions_scope_not_decision`, `test_every_server_request_gets_schema_valid_response`) all pass `permissions: {}` and assert only key-presence/scope, so the echo-fix keeps them green (echoing `{}` ⇒ unchanged for them).

---

### Task 1: Test suite never writes to the real monitoring log

> **Ordering (R2-F1):** this task MUST land before Task 2. The permission tests in Task 2 call `handle_server_request` → `bridge_approval`, whose logger defaults to the REAL `~/.claude/hooks/bulldozer-codex.log` when `BULLDOZER_CODEX_LOG` is unset. Installing the autouse redirect FIRST means no approval-bridge test (Task 2's new test, or the existing permission/server-request subset) ever appends to the real log while we are mid-fix.

**Files:**
- Modify: `tests/test_codex_mcp_v2.py` — add an autouse fixture + a regression test.

**Interfaces:**
- Consumes: pytest `tmp_path_factory`, `monkeypatch`.
- Produces: `BULLDOZER_CODEX_LOG` env set to a per-test tmp path for every test; tests that assert log contents still override it inline (their body-level `monkeypatch.setenv` runs after fixtures, so it wins).

**Why an autouse fixture (root cause):** `_drift_warn`, `bridge_approval`'s logger, and the third log site all default to `~/.claude/hooks/bulldozer-codex.log` when `BULLDOZER_CODEX_LOG` is unset. There is no autouse redirect today, and `@pytest.mark.slow` tests run real codex → every drift/approval they trigger appends to the REAL log. Mining confirmed the pollution: 92% of 2429 APPROVAL lines are `wait_ms=0` (programmatic), and 1877 `OUT_OF_ENUM_LABEL` / 248 uniform `TRANSLATE_FAILED` are test injections — all leaked from the suite. Redirecting the log does NOT touch codex auth (that is `CODEX_HOME`), so the fixture applies to slow tests too (no exemption).

- [ ] **Step 1: Write the failing regression test**

```python
def test_real_codex_log_is_never_touched_by_tests():
    """Hygiene guard: the autouse redirect must point BULLDOZER_CODEX_LOG OFF the real
    monitoring log for EVERY test (offline + slow), so the suite never pollutes it
    (the cause of the uncalibratable #251 corpus)."""
    import os
    p = os.environ.get("BULLDOZER_CODEX_LOG")
    assert p, "BULLDOZER_CODEX_LOG must be set by the autouse redirect fixture"
    assert "/.claude/hooks/" not in p, f"test log must not be under the real hooks dir: {p}"
    assert not p.endswith("bulldozer-codex.log") or "/tmp" in p or "pytest" in p, p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py::test_real_codex_log_is_never_touched_by_tests -v`
Expected: FAIL — `BULLDOZER_CODEX_LOG` is unset (no autouse fixture yet) → first assert fails.

- [ ] **Step 3: Add the autouse fixture**

In `tests/test_codex_mcp_v2.py`, beside the existing autouse fixtures (after `_reset_cc_stream_fixture`):

```python
@pytest.fixture(autouse=True)
def _redirect_codex_log(tmp_path_factory, monkeypatch):
    """Hygiene: NO test (offline OR slow) writes drift/approval lines to the real
    ~/.claude/hooks/bulldozer-codex.log. Tests that assert log contents override this
    with their own monkeypatch.setenv in the body (runs after fixtures, so it wins).
    Independent of CODEX_HOME, so slow/live-codex tests are NOT exempt."""
    logdir = tmp_path_factory.mktemp("codexlog")
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logdir / "bulldozer-codex.log"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py::test_real_codex_log_is_never_touched_by_tests -v`
Expected: PASS.

- [ ] **Step 5: Verify existing log-assertion tests still pass (their inline setenv wins)**

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py -k "log or drift or approval or notification_fixture" -v`
Expected: all PASS — the inline `monkeypatch.setenv("BULLDOZER_CODEX_LOG", ...)` in those test bodies overrides the autouse default. (`notification_fixture` selects `test_missing_notification_fixture_warns_and_falls_back` + `test_malformed_notification_fixture_warns_and_falls_back`, which assert `NOTIFICATION_FIXTURE_MISSING` is logged.)

- [ ] **Step 6: Commit**

```bash
git add tests/test_codex_mcp_v2.py
git commit -m "test(codex-mcp): autouse-redirect BULLDOZER_CODEX_LOG off the real log (suite hygiene)"
```

---

### Task 2: Permissions accept echoes the requested profile (#4)

> Run this AFTER Task 1 — the autouse log redirect (Task 1) must already be in `tests/test_codex_mcp_v2.py` so the new permission test (which routes through `bridge_approval`) writes to a tmp log, not the real one (R2-F1).

**Files:**
- Modify: `mcp/codex_server.py` — the `perm_pairs` construction + accept-return in the `item/permissions/requestApproval` handler.
- Modify: `CLAUDE.md` — the "Known v1-of-v2 limitation" paragraph documents this exact gap as a "future improvement"; #4 closes it, so the note must be updated (else it drifts stale).
- Test: `tests/test_codex_mcp_v2.py` — new `test_permissions_accept_echoes_requested_profile`.

**Interfaces:**
- Consumes: `params["permissions"]` (a `RequestPermissionProfile` dict: `{fileSystem?, network?}`) and the chosen label.
- Produces: response dicts — grant: `{"permissions": <requested-profile>, "scope": "turn"|"session"}`; decline: `PERM_DECLINE` (unchanged).

- [ ] **Step 1: Write the failing test**

```python
def test_permissions_accept_echoes_requested_profile():
    """#4: accepting item/permissions/requestApproval must GRANT what codex asked
    for — echo params['permissions'] into the response — not an empty {} (a silent
    no-op). Schema: request/response profiles share the {fileSystem?,network?} shape."""
    from codex_server import handle_server_request, LBL_GRANT_TURN, LBL_GRANT_SESSION, LBL_DONT_GRANT
    requested = {"network": {"enabled": True},
                 "fileSystem": {"entries": [{"access": "read",
                                             "path": {"type": "path", "path": "/x"}}]}}

    def run(label):
        cc = FakeCC()
        cc.set_answer("accept", {"label": label})
        msg = {"id": "perm", "method": "item/permissions/requestApproval",
               "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                          "startedAtMs": 1, "cwd": "/tmp", "reason": None,
                          "permissions": requested}}
        return handle_server_request(msg, cc.write, cc.read)["result"]

    grant_turn = run(LBL_GRANT_TURN)
    assert grant_turn == {"permissions": requested, "scope": "turn"}, grant_turn

    grant_session = run(LBL_GRANT_SESSION)
    assert grant_session == {"permissions": requested, "scope": "session"}, grant_session

    # Decline still grants nothing (safe default preserved).
    cc = FakeCC()
    cc.set_answer("accept", {"label": LBL_DONT_GRANT})
    msg = {"id": "perm2", "method": "item/permissions/requestApproval",
           "params": {"threadId": "T", "turnId": "U", "itemId": "I",
                      "startedAtMs": 1, "cwd": "/tmp", "reason": None,
                      "permissions": requested}}
    declined = handle_server_request(msg, cc.write, cc.read)["result"]
    assert declined == {"permissions": {}, "scope": "turn"}, declined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py::test_permissions_accept_echoes_requested_profile -v`
Expected: FAIL — current code returns `{"permissions": {}, "scope": "turn"}` for grant (empty, not echoed).

- [ ] **Step 3: Write minimal implementation**

In `mcp/codex_server.py`, in the `item/permissions/requestApproval` handler, replace the static `perm_pairs` with one built from the requested profile:

```python
    if method == "item/permissions/requestApproval":
        # #4: grant exactly what codex asked for (echo the requested RequestPermissionProfile).
        # An accept that returned {} granted NOTHING (silent no-op). Request/response profiles
        # share the {fileSystem?,network?} shape (codex 0.141 schema), so the echo is valid.
        requested = params.get("permissions") or {}
        perm_pairs = [
            (LBL_GRANT_TURN, {"permissions": requested, "scope": "turn"}),
            (LBL_GRANT_SESSION, {"permissions": requested, "scope": "session"}),
            (LBL_DONT_GRANT, PERM_DECLINE),
        ]
```

Leave the rest of the handler (labels/elicitation/read_correlated/accept fallback to `perm_map[LBL_GRANT_TURN]`, non-accept → `PERM_DECLINE`) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py::test_permissions_accept_echoes_requested_profile -v`
Expected: PASS.

- [ ] **Step 5: Run the existing permissions tests (no regression)**

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py -k "permission or server_request" -v`
Expected: all PASS (they use `permissions: {}`, so the echo is `{}` — unchanged behavior for them).

- [ ] **Step 6: Update the CLAUDE.md known-limitation note (A3 — prevent doc drift)**

The "Architecture: codex MCP server" section of `CLAUDE.md` currently documents this exact gap as a future improvement:

> **Known v1-of-v2 limitation**: the `item/permissions/requestApproval` bridge returns `{"permissions": {}, "scope": "turn"}` as a minimal safe default (`PERM_DECLINE`; the `perm_map` only maps permission labels to scope, not the actual permissions object — this is adequate for most cases but a future improvement would populate `permissions` from the request params).

Replace that paragraph with the post-#4 reality. (Multi-angle review hardened the unknown-label path to fail **closed** — an accept with an unrecognized label declines, not grants; only a *bare* accept grants-for-turn. The tracker is #272.)

> **Permissions-grant semantics (#272):** on accept, the `item/permissions/requestApproval` bridge echoes the codex-requested `RequestPermissionProfile` (`params["permissions"]`) into the response — granting exactly what codex asked for — with `scope` set per the chosen label (turn/session). The empty `{"permissions": {}, "scope": "turn"}` (`PERM_DECLINE`) is the **non-accept** fallback (decline / timeout / cancel). A **bare** accept (no dropdown label) grants for the turn — CC's plain Accept — but an accept carrying an **unrecognized** label fails **closed** to `PERM_DECLINE` (`perm_map.get(chosen, PERM_DECLINE)`, with an `OUT_OF_ENUM_LABEL` drift note): #4 made the grant load-bearing, so an ambiguous label must not silently grant. The dialog renders the requested profile (`_summarize_permissions`, network first so the egress grant survives truncation of a large fileSystem list); a malformed non-dict `permissions` is not echoed (fail-open to `{}`). Request and granted profiles share the `{fileSystem?, network?}` shape (codex 0.141 schema), so the echo is structurally valid. (Pre-#4 the bridge granted an empty `{}` on accept — a silent no-op.)

- [ ] **Step 7: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py CLAUDE.md
git commit -m "fix(codex-mcp): #4 grant the requested permission profile, not empty {}"
```

---

### Task 3: Full verification + empirical log-clean check

- [ ] **Step 1: Full offline suite**

`tests/conftest.py` does NOT deselect `slow` by default (no `addopts`), so a bare `pytest` would run the 15 real-codex `@pytest.mark.slow` tests too — the offline phase MUST filter them explicitly.

Run: `cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py -m "not slow" -v`
Expected: all PASS (254 offline tests collected, 15 slow deselected — no regressions).

- [ ] **Step 2: Slow suite (mandatory after codex_server.py edit)**

Capture the real log's size BEFORE, run slow, confirm it did NOT grow:

```bash
LOG=~/.claude/hooks/bulldozer-codex.log
before=$(wc -l < "$LOG")
cd plugins/bulldozer && python3 -m pytest tests/test_codex_mcp_v2.py -m slow -v
after=$(wc -l < "$LOG")
echo "real log lines: before=$before after=$after (MUST be equal)"
```
Expected: slow tests PASS, and `before == after` — empirical proof the redirect holds for slow/live-codex tests.

- [ ] **Step 3: Broader cross-file confidence run (best-effort — Step 1 is the authoritative surface)**

**Authoritative regression surface for THIS PR = Step 1.** `mcp/codex_server.py` is imported by exactly one test file (`tests/test_codex_mcp_v2.py` — verify: `grep -rl "import codex_server\|from codex_server" tests/`), so Step 1's offline run + Step 2's slow run fully cover what this PR can break. Step 3 is only extra cross-file confidence.

Caveat (why this is "best-effort", not "genuinely offline"): this repo has NO default `-m "not slow"` filter (no `addopts`), AND `-m "not slow"` is not equivalent to offline — several non-slow tests touch real resources and merely SELF-SKIP when the resource is absent (the browser/CfT e2e files `test_e2e.py`/`test_e2e_cft.py`/`test_e2e_drive.py`/`test_e2e_lanes.py`; and `test_launch.py::test_update_cft_dry_run_resolves_stable`, which makes a network attempt before skipping). Exclude the browser/CfT files and deselect the network test; a few remaining skips are EXPECTED, not failures.

Run:
```bash
cd plugins/bulldozer && python3 -m pytest tests/ -m "not slow" \
  --ignore=tests/test_e2e.py --ignore=tests/test_e2e_cft.py \
  --ignore=tests/test_e2e_drive.py --ignore=tests/test_e2e_lanes.py \
  -k "not update_cft_dry_run_resolves_stable" -q
```
Expected: green (passes + expected skips; no failures). If you are offline and a stray non-slow test still attempts a real resource, that is a pre-existing test-suite property, NOT a regression from this PR — Step 1 remains the authoritative check.

---

## Notes for the reviewer (out of scope, do NOT implement here)

- **#251** (auto-accept approval on human-timeout) stays blocked: the corpus is uncalibratable (n=1 real human-scale timeout in 2429 logged approvals) and the feature inverts the security safe-default. This plan's Task 1 (log hygiene) is the *prerequisite* that lets a future clean corpus accumulate; it does not implement #251.
- The `OUT_OF_ENUM_LABEL=accept` noise (1688×) comes partly from `test_every_server_request_gets_schema_valid_response` answering with `{"label": "accept"}` (not a permissions display-label) → harmless fallback to grant-turn. Task 1 stops it polluting the real log; the fallback behavior itself is correct and unchanged.
