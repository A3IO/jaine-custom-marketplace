# PR-3a — Structural foundation (B3 + B8) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: inline TDD (RED→GREEN per task) chosen over subagent-driven (surgical refactor, established method for this plugin). Steps use checkbox (`- [ ]`) syntax for tracking. Dogfood `bulldozer:check standard` is the review gate before merge.

**Goal:** Extract the wrapper's two inline `python3` heredocs into standalone unit-testable scripts (B3), and move verdict derivation from the wrapper into the parser as a canonical `meta.verdict` (B8) — with zero change to the wrapper's observable output.

**Architecture:** `bulldozer-round.sh` currently embeds ~70 lines of bash-quoted Python (trajectory render + pivot emit) and computes GO/NO-GO itself from `meta.verdict`-or-findings. B3 moves the two heredocs to `skills/check/scripts/render-trajectory.py` + `emit-pivot.py` (called as subprocesses, with existence-guards symmetric to the existing `PARSER`/`LOG_ROUND` R1-F3c checks). B8 makes `parse-ledger-patch.py` always emit a canonical `meta["verdict"]` ("go"/"no_go") on exit 0, so the wrapper reads it directly and drops its fallback inference. The verdict canonicalization is constructed to be **output-identical** to today's wrapper computation, so no behavioral change ships.

**Tech Stack:** bash (`set -euo pipefail`), Python 3 stdlib (json, sys), PyYAML (parser only), pytest (black-box wrapper tests + direct-invocation script tests).

---

## Spec reference

`docs/superpowers/specs/2026-05-28-issue-110-roadmap-design.md` → PR-3, items **B3** and **B8**. PR-3a is the pre-authorized foundation split (B3 + B8). B1/B2/B4/B7 ship separately as PR-3b and build on this.

## Invariants to preserve (from PR-1/PR-2, see handoff)

- **PR-2 A4:** codex fed via stdin **redirect** (`codex … - < "$codex_stdin"`), never a pipe. Do not touch.
- **PR-2 A1/A2/A3:** empty-parser_out→70; reviewer `^[^/]+/.+$`→64; round `^[1-9][0-9]*$`→64. Do not touch.
- **PR-1:** recovery commands shell-escape paths via `REVIEW_DIR_Q`/`UPDATE_STATE_Q`. New script paths the wrapper invokes are NOT printed in copy-paste recovery commands, so they need no `%q` quoting — but their `python3 "$VAR"` callsite uses a double-quoted var, which is split-safe.
- **BUG-2 (verdict):** an explicit NO-GO with empty findings must NOT flip to GO. The B8 canonicalization must preserve this exactly.
- **Exit-code contract:** 0 ok / 1-5 parser / 10 pivot / 11 manual / 64 usage / 70 wrapper-internal / 71 codex. New script failures map to **70** (wrapper-internal), matching the existing trajectory/pivot `_emit_stop 70` handling.

## File structure

- Create: `skills/check/scripts/render-trajectory.py` (trajectory render, B3)
- Create: `skills/check/scripts/emit-pivot.py` (pivot JSON emit, B3)
- Modify: `skills/check/scripts/parse-ledger-patch.py` (canonical `meta.verdict`, B8)
- Modify: `skills/check/scripts/bulldozer-round.sh` (call subprocesses + path guards + read verdict + drop fallback)
- Modify: `tests/test_parse_ledger_patch.py` (B8 parser tests)
- Modify: `tests/test_check_round_wrapper.py` (B3 direct-script tests + fixture audit for B8)

All anchors are grep-stable strings (no line numbers — they drift, and this session's `cat -n` line numbering is unreliable).

---

## Task 1 — B8 parser: always emit canonical `meta.verdict`

**Files:**
- Modify: `skills/check/scripts/parse-ledger-patch.py` (the `parse()` structured return; grep `"source": "empty_findings" if not findings else "ledger_patch"`)
- Test: `tests/test_parse_ledger_patch.py`

**Design:** In `parse()`, after building `meta = {k: v for k, v in body.items() if k != "findings"}`, normalize/inject a canonical verdict:
- If `meta` has a `verdict` key (reviewer-supplied), canonicalize it: `"go"` if `str(meta["verdict"]).strip().lower() == "go"` else `"no_go"`.
- Else infer: `"go" if not findings else "no_go"`.

This is **output-identical** to the wrapper's current computation (`meta_verdict present → lower()=="go" ? GO : NO-GO ; else not findings ? GO : NO-GO`), so the wrapper's downstream `COUNT|VERDICT` line and `state.json` verdict are byte-for-byte unchanged. The bare-GO synthesis path already sets `meta: {verdict: "go", ...}` — leave it (it is already canonical "go").

- [ ] **Step 1: Write failing parser tests**

Add to `tests/test_parse_ledger_patch.py` a class `TestCanonicalVerdict` (use the file's existing `parse()`/`_parse_text` invocation pattern — match the surrounding tests' call style):

```python
class TestCanonicalVerdict:
    """B8 (#110): parser emits canonical meta.verdict on every exit-0 parse."""

    def test_findings_no_explicit_verdict_infers_no_go(self):
        body = (
            "LEDGER_PATCH:\n"
            "  findings:\n"
            "    - id: R1-F1\n"
            "      severity: high\n"
            "      status: open\n"
            "      title: x\n"
            "      files: [{path: a.py, lines: '1'}]\n"
        )
        code, payload = parse(body, None)
        assert code == 0
        assert payload["meta"]["verdict"] == "no_go"

    def test_empty_findings_no_explicit_verdict_infers_go(self):
        body = "LEDGER_PATCH:\n  findings: []\n"
        code, payload = parse(body, None)
        assert code == 0
        assert payload["meta"]["verdict"] == "go"

    def test_explicit_no_go_with_empty_findings_stays_no_go(self):
        # BUG-2 regression: explicit NO-GO must NOT flip to go just because
        # findings is empty.
        body = "LEDGER_PATCH:\n  verdict: no-go\n  findings: []\n"
        code, payload = parse(body, None)
        assert code == 0
        assert payload["meta"]["verdict"] == "no_go"

    def test_explicit_go_canonicalized_lowercase(self):
        body = "LEDGER_PATCH:\n  verdict: GO\n  findings: []\n"
        code, payload = parse(body, None)
        assert code == 0
        assert payload["meta"]["verdict"] == "go"

    def test_bare_go_synthesis_has_canonical_verdict(self):
        # characterization: synthesized bare-GO already canonical.
        code, payload = parse("GO\n", None)
        assert code == 0
        assert payload["meta"]["verdict"] == "go"
```

(Confirm the actual `parse()` signature/import by matching existing tests in the file before writing — adapt `parse(body, None)` to the real call if it differs.)

- [ ] **Step 2: Run tests, verify RED**

Run: `python3 -m pytest tests/test_parse_ledger_patch.py::TestCanonicalVerdict -v`
Expected: `test_findings_no_explicit_verdict_infers_no_go` and `test_empty_findings_no_explicit_verdict_infers_go` FAIL (KeyError: 'verdict' — parser does not inject it today). `test_explicit_*` may already pass (reviewer-supplied passes through but NOT normalized — `verdict: GO` stays "GO" not "go", so `test_explicit_go_canonicalized_lowercase` FAILs; `verdict: no-go` stays "no-go" so `test_explicit_no_go...` FAILs on `== "no_go"`). Bare-GO test passes (already canonical).

- [ ] **Step 3: Implement canonical verdict in parser**

In `parse()`, replace the `meta = {...}` line region (grep `meta = {k: v for k, v in body.items() if k != "findings"}`) by adding immediately after it:

```python
    # B8 (#110): emit a canonical meta.verdict on every exit-0 parse so the
    # wrapper reads it directly instead of re-deriving GO/NO-GO. Output is
    # identical to the wrapper's prior computation: reviewer-supplied verdict
    # canonicalized (lower()=="go" → "go" else "no_go"), else inferred from
    # the findings list. BUG-2 invariant preserved: an explicit NO-GO with
    # empty findings stays "no_go".
    if "verdict" in meta:
        meta["verdict"] = "go" if str(meta["verdict"]).strip().lower() == "go" else "no_go"
    else:
        meta["verdict"] = "go" if not findings else "no_go"
```

- [ ] **Step 4: Run tests, verify GREEN**

Run: `python3 -m pytest tests/test_parse_ledger_patch.py -q`
Expected: `TestCanonicalVerdict` all PASS; the full parser suite still PASS (no existing test should assert `meta` lacks a `verdict` key — confirm; if one does, it was asserting an implementation detail that B8 intentionally changes — update it to expect the canonical value).

- [ ] **Step 5: Commit**

```bash
git add skills/check/scripts/parse-ledger-patch.py tests/test_parse_ledger_patch.py
git commit -m "feat(check): B8 — parser emits canonical meta.verdict (#110)"
```

---

## Task 2 — B8 wrapper: read `meta.verdict`, drop fallback inference

**Files:**
- Modify: `skills/check/scripts/bulldozer-round.sh` (grep `meta_verdict = (data.get("meta") or {}).get("verdict")`)
- Test: `tests/test_check_round_wrapper.py` (fixture audit)

**Design:** The wrapper's `parser_out=$(python3 -c '…')` block computes verdict from `meta.verdict`-or-findings. After Task 1 the parser always supplies `meta.verdict`, so the block reduces to reading it. Keep the `print(f"{len(findings)}|{verdict}")` contract identical.

- [ ] **Step 1: Audit existing fixtures for parsed JSON lacking `meta.verdict`**

Before changing the wrapper, find any test that hand-writes a `parsed-rN.json` (or stubs parser output) without a `meta.verdict`, because dropping the findings-fallback changes those cases (empty findings + no meta.verdict: old→GO, new→NO-GO).

Run:
```bash
grep -nE 'parsed-r|"meta"|meta\b|findings.*\[\]|"verdict"' tests/test_check_round_wrapper.py | grep -iE 'parsed|meta|verdict' | head -60
```
For each fixture feeding the wrapper a parsed JSON, confirm it includes `"meta": {"verdict": ...}`. These fixtures model **parser output** — post-B8 the real parser always emits `meta.verdict`, so any fixture missing it is now unrealistic. Update such fixtures to include the canonical `meta.verdict` matching their findings (this keeps them faithful to the real parser and to the wrapper's unchanged behavior). Record the list of edited fixtures in the commit message.

- [ ] **Step 2: Write/adjust the failing wrapper test**

Add a test asserting the wrapper reads the parser's `meta.verdict` (not findings) — e.g. a parsed JSON with `findings: []` but `meta.verdict: "no_go"` must yield NO-GO in `state.json` (proving the wrapper trusts the parser verdict, the BUG-2 direction). Place it in `TestStateOutput` or `TestParserExitHandling` (match how those build a stubbed parsed file / run the wrapper):

```python
    def test_wrapper_trusts_parser_meta_verdict_over_findings(self, tmp_path):
        # parser says no_go even with empty findings → wrapper logs NO-GO.
        # (post-B8 the wrapper no longer infers GO from empty findings.)
        ...  # build parsed-r1.json with {"findings": [], "meta": {"verdict": "no_go"}}
             # run wrapper, assert state.json round-1 verdict == "NO-GO"
```

- [ ] **Step 3: Run it, verify RED**

Run the new test. Pre-change the wrapper's `else: verdict = "GO" if not findings` makes empty-findings → GO regardless of meta.verdict only when meta.verdict is absent; with meta.verdict present it already reads it — so confirm the test is RED by constructing the fixture WITHOUT relying on the fallback. If the wrapper already passes (because meta.verdict present is honored today), this test is a **characterization** test; the real RED→GREEN for "drop fallback" is the fixture-audit cases from Step 1 (empty findings + no meta.verdict). Make at least one such case RED before the edit.

- [ ] **Step 4: Implement — read verdict directly**

Replace the verdict block (grep `meta_verdict = (data.get("meta") or {}).get("verdict")` through `print(f"{len(findings)}|{verdict}")`) with:

```python
findings = data.get("findings", [])
# B8 (#110): parser now emits a canonical meta.verdict on every exit-0 parse,
# so the wrapper reads it directly (single source of truth). Missing key is
# treated as NO-GO — never falsely GO — preserving the BUG-2 fail-safe.
verdict = "GO" if (data.get("meta") or {}).get("verdict") == "go" else "NO-GO"
print(f"{len(findings)}|{verdict}")
```

Update the surrounding comment block (grep `BUG-2 fix: pure structural inference`) to reflect that derivation now lives in the parser.

- [ ] **Step 5: Run wrapper suite, verify GREEN**

Run: `python3 -m pytest tests/test_check_round_wrapper.py -q`
Expected: all PASS (with audited fixtures updated).

- [ ] **Step 6: Commit**

```bash
git add skills/check/scripts/bulldozer-round.sh tests/test_check_round_wrapper.py
git commit -m "refactor(check): B8 — wrapper reads canonical meta.verdict, drops fallback (#110)"
```

---

## Task 3 — B3: extract trajectory heredoc → `render-trajectory.py`

**Files:**
- Create: `skills/check/scripts/render-trajectory.py`
- Modify: `skills/check/scripts/bulldozer-round.sh` (grep `python3 - "$ROUND" "$max_rounds" "${REVIEW_DIR}/state.json"`)
- Test: `tests/test_check_round_wrapper.py` (new `TestRenderTrajectoryScript`)

**Design:** The heredoc body uses `sys.argv[1]=round`, `argv[2]=max_rounds`, `argv[3]=state_path`. A script file has the same argv indices, so the Python body is copied **verbatim** (only adding a shebang + docstring + a minimal arity guard). Output stays byte-identical.

- [ ] **Step 1: Write failing direct-invocation tests**

Add `class TestRenderTrajectoryScript` to `tests/test_check_round_wrapper.py`. Reference the new script via a module constant (add near the existing `WRAPPER`/`PARSER` constants): `RENDER_TRAJECTORY = Path(__file__).resolve().parent.parent / "skills" / "check" / "scripts" / "render-trajectory.py"`.

```python
class TestRenderTrajectoryScript:
    """B3 (#110): trajectory render extracted to a standalone script."""

    def _state(self, tmp_path, history):
        import json
        sp = tmp_path / "state.json"
        sp.write_text(json.dumps({"history": history}))
        return sp

    def test_renders_round_line_and_trajectory(self, tmp_path):
        sp = self._state(tmp_path, [
            {"round": 1, "verdict": "NO-GO", "findings": 4},
            {"round": 2, "verdict": "NO-GO", "findings": 2},
        ])
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(sp)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "Round 2/3" in r.stdout
        assert "verdict: NO-GO" in r.stdout
        assert "2 findings open" in r.stdout
        assert "Trajectory: 4 → 2" in r.stdout
        assert "avg last 3: 3.0" in r.stdout

    def test_singular_finding_noun(self, tmp_path):
        sp = self._state(tmp_path, [
            {"round": 1, "verdict": "NO-GO", "findings": 2},
            {"round": 2, "verdict": "NO-GO", "findings": 1},
        ])
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(sp)],
            capture_output=True, text=True,
        )
        assert "1 finding open" in r.stdout  # singular

    def test_corrupt_state_exits_nonzero(self, tmp_path):
        sp = tmp_path / "state.json"
        sp.write_text("{not json")
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2", "3", str(sp)],
            capture_output=True, text=True,
        )
        assert r.returncode != 0  # wrapper maps this to _emit_stop 70

    def test_bad_arity_exits_nonzero(self, tmp_path):
        r = subprocess.run(
            [sys.executable, str(RENDER_TRAJECTORY), "2"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
```

- [ ] **Step 2: Run, verify RED**

Run: `python3 -m pytest tests/test_check_round_wrapper.py::TestRenderTrajectoryScript -v`
Expected: all FAIL (file `render-trajectory.py` does not exist → non-zero / FileNotFound).

- [ ] **Step 3: Create `render-trajectory.py`**

Copy the heredoc body verbatim (grep the wrapper block between `python3 - "$ROUND" "$max_rounds" "${REVIEW_DIR}/state.json" <<'PYEOF' >&2` and `PYEOF`), add shebang + docstring + arity guard:

```python
#!/usr/bin/env python3
"""Render the bulldozer review trajectory line (B3 extraction, #110).

Usage: render-trajectory.py <round> <max_rounds> <state_json_path>
Prints the 2-line trajectory summary to stdout (the wrapper redirects to
stderr). Exits non-zero on bad arity or unreadable/corrupt state.json —
the wrapper maps any non-zero here to its _emit_stop 70 path.
"""
import json
import sys

if len(sys.argv) != 4:
    print("usage: render-trajectory.py <round> <max_rounds> <state_json_path>", file=sys.stderr)
    sys.exit(2)

round_num = int(sys.argv[1])
max_rounds = int(sys.argv[2])
state_path = sys.argv[3]

with open(state_path) as fp:
    state = json.load(fp)

history = state.get("history", [])
trajectory = [h.get("findings", 0) for h in history]
last = history[-1] if history else {"verdict": "UNKNOWN", "findings": 0}
last_verdict = last.get("verdict", "UNKNOWN")
last_findings = last.get("findings", 0)

noun = "finding" if last_findings == 1 else "findings"
print(
    f"[bulldozer/check] Round {round_num}/{max_rounds} — "
    f"verdict: {last_verdict} — {last_findings} {noun} open"
)

traj_str = " → ".join(str(f) for f in trajectory)
window = trajectory[-3:]
avg = sum(window) / len(window) if window else 0
print(f"Trajectory: {traj_str}  (avg last 3: {avg:.1f})")
```

Make executable: `chmod +x skills/check/scripts/render-trajectory.py`.

- [ ] **Step 4: Run direct tests, verify GREEN**

Run: `python3 -m pytest tests/test_check_round_wrapper.py::TestRenderTrajectoryScript -v` → all PASS.

- [ ] **Step 5: Rewire the wrapper to call the script**

Add the dependency-path constant near `PARSER`/`LOG_ROUND` (grep `PARSER="${SCRIPT_DIR}/parse-ledger-patch.py"`):

```bash
RENDER_TRAJECTORY="${SCRIPT_DIR}/render-trajectory.py"
```

Add an existence guard symmetric to the PARSER/LOG_ROUND R1-F3c checks (grep `if [[ ! -f "$LOG_ROUND" ]]; then`), after the LOG_ROUND guard:

```bash
if [[ ! -f "$RENDER_TRAJECTORY" ]]; then
    _emit_stop 70 "render-trajectory.py not found at ${RENDER_TRAJECTORY}." \
        "Likely a stale CLAUDE_PLUGIN_ROOT or partial plugin install." \
        "Fix: jaine-sync plugins update bulldozer"
fi
```

Replace the heredoc (the whole `python3 - "$ROUND" "$max_rounds" "${REVIEW_DIR}/state.json" <<'PYEOF' >&2 || trajectory_exit=$?` … `PYEOF` block) with a single subprocess call, preserving the surrounding `trajectory_exit` capture and `_emit_stop 70` handler:

```bash
    python3 "$RENDER_TRAJECTORY" "$ROUND" "$max_rounds" "${REVIEW_DIR}/state.json" >&2 || trajectory_exit=$?
```

- [ ] **Step 6: Run full wrapper suite, verify GREEN**

Run: `python3 -m pytest tests/test_check_round_wrapper.py -q`
Expected: `TestTrajectoryDisplay` (black-box through wrapper) still PASS — proves byte-identical output via the subprocess.

- [ ] **Step 7: Commit**

```bash
git add skills/check/scripts/render-trajectory.py skills/check/scripts/bulldozer-round.sh tests/test_check_round_wrapper.py
git commit -m "refactor(check): B3 — extract trajectory render to render-trajectory.py (#110)"
```

---

## Task 4 — B3: extract pivot heredoc → `emit-pivot.py`

**Files:**
- Create: `skills/check/scripts/emit-pivot.py`
- Modify: `skills/check/scripts/bulldozer-round.sh` (grep `python3 - "$ROUND" "$max_rounds" "$findings_count" "$DEPTH" "$ARTIFACT" "$PIVOT_FILE"`)
- Test: `tests/test_check_round_wrapper.py` (new `TestEmitPivotScript`)

**Design:** Heredoc argv: `argv[1]=round, [2]=max_rounds, [3]=open_findings, [4]=depth, [5]=artifact, [6]=pivot_path`. Same indices in a script file → copy verbatim. Output JSON byte-identical. (PR-3b/B4 later moves the hardcoded options into a YAML file; PR-3a keeps them inline in `emit-pivot.py`.)

- [ ] **Step 1: Write failing direct-invocation tests**

Add `class TestEmitPivotScript` and a module constant `EMIT_PIVOT = … / "emit-pivot.py"`:

```python
class TestEmitPivotScript:
    """B3 (#110): pivot JSON emit extracted to a standalone script."""

    def test_writes_pivot_json_with_expected_shape(self, tmp_path):
        import json
        pivot = tmp_path / "pivot-r3.json"
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3", "5", "standard",
             "src/x.py", str(pivot)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        data = json.loads(pivot.read_text())
        assert data["trigger"] == "max_rounds_reached"
        assert data["round"] == 3
        assert data["max_rounds"] == 3
        assert data["open_findings"] == 5
        assert data["depth"] == "standard"
        assert data["artifact"] == "src/x.py"
        assert data["header"] == "Pivot"
        assert data["multiSelect"] is False
        labels = [o["label"] for o in data["options"]]
        assert labels == ["continue", "restructure", "accept-with-TODO"]

    def test_bad_arity_exits_nonzero(self, tmp_path):
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0

    def test_unwritable_pivot_path_exits_nonzero(self, tmp_path):
        bad = tmp_path / "nope" / "pivot.json"  # parent missing
        r = subprocess.run(
            [sys.executable, str(EMIT_PIVOT), "3", "3", "5", "standard",
             "a", str(bad)],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
```

- [ ] **Step 2: Run, verify RED** — `…::TestEmitPivotScript -v` all FAIL (no file).

- [ ] **Step 3: Create `emit-pivot.py`** — copy the heredoc body verbatim (grep between `python3 - "$ROUND" "$max_rounds" "$findings_count" "$DEPTH" "$ARTIFACT" "$PIVOT_FILE" <<'PYEOF'` and `PYEOF`), add shebang + docstring + arity guard:

```python
#!/usr/bin/env python3
"""Emit the bulldozer max-rounds pivot file (B3 extraction, #110).

Usage: emit-pivot.py <round> <max_rounds> <open_findings> <depth> <artifact> <pivot_path>
Writes an AskUserQuestion-compatible pivot JSON to <pivot_path>. Exits
non-zero on bad arity or an unwritable path — the wrapper maps non-zero
(or a missing pivot file) to its _emit_stop 70 path.
"""
import json
import sys

if len(sys.argv) != 7:
    print(
        "usage: emit-pivot.py <round> <max_rounds> <open_findings> <depth> <artifact> <pivot_path>",
        file=sys.stderr,
    )
    sys.exit(2)

round_num, max_rounds, open_findings, depth, artifact, pivot_path = (
    int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]),
    sys.argv[4], sys.argv[5], sys.argv[6],
)
pivot = {
    "trigger": "max_rounds_reached",
    "round": round_num,
    "max_rounds": max_rounds,
    "depth": depth,
    "artifact": artifact,
    "open_findings": open_findings,
    "question": (
        f"Reached max rounds ({max_rounds}) without GO — "
        f"{open_findings} finding(s) open. How to proceed?"
    ),
    "header": "Pivot",
    "multiSelect": False,
    "options": [
        {"label": "continue",
         "description": "Run another round (exceeds max for this depth)"},
        {"label": "restructure",
         "description": "Pause review, restructure the artifact, re-launch /bulldozer:check"},
        {"label": "accept-with-TODO",
         "description": "Accept current state, log open findings as project TODOs"},
    ],
}
with open(pivot_path, "w") as fp:
    json.dump(pivot, fp, indent=2)
```

`chmod +x skills/check/scripts/emit-pivot.py`.

- [ ] **Step 4: Run direct tests, verify GREEN** — `…::TestEmitPivotScript -v` all PASS.

- [ ] **Step 5: Rewire the wrapper**

Add constant near `RENDER_TRAJECTORY`:
```bash
EMIT_PIVOT="${SCRIPT_DIR}/emit-pivot.py"
```
Add existence guard symmetric to the others:
```bash
if [[ ! -f "$EMIT_PIVOT" ]]; then
    _emit_stop 70 "emit-pivot.py not found at ${EMIT_PIVOT}." \
        "Likely a stale CLAUDE_PLUGIN_ROOT or partial plugin install." \
        "Fix: jaine-sync plugins update bulldozer"
fi
```
Replace the pivot heredoc (`python3 - … <<'PYEOF' || pivot_exit=$?` … `PYEOF`) with:
```bash
    python3 "$EMIT_PIVOT" "$ROUND" "$max_rounds" "$findings_count" "$DEPTH" "$ARTIFACT" "$PIVOT_FILE" || pivot_exit=$?
```
Keep the existing `if (( pivot_exit != 0 )) || [[ ! -f "$PIVOT_FILE" ]]; then _emit_stop 70 …` handler and the `echo "PIVOT: …" >&2; exit 10` after it.

- [ ] **Step 6: Run full wrapper suite, verify GREEN** — `tests/test_check_round_wrapper.py -q`; `TestPivotSignal` (black-box) still PASS → byte-identical pivot JSON.

- [ ] **Step 7: Commit**

```bash
git add skills/check/scripts/emit-pivot.py skills/check/scripts/bulldozer-round.sh tests/test_check_round_wrapper.py
git commit -m "refactor(check): B3 — extract pivot emit to emit-pivot.py (#110)"
```

---

## Verification (whole PR-3a)

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer
python3 -m pytest tests/ -q -k "not e2e"     # full non-e2e regression, expect prior 358 + new, all green
# Confirm no heredocs remain in the wrapper:
grep -n "<<'PYEOF'" skills/check/scripts/bulldozer-round.sh   # expect: no matches
# Confirm wrapper has no findings-fallback verdict inference:
grep -n 'GO" if not findings' skills/check/scripts/bulldozer-round.sh  # expect: no matches
```

Per task: watch each new test FAIL (RED) before the edit, PASS (GREEN) after. Commit per task.

## Dogfood + merge

Branch `bulldozer/feat/pr3a-b3-b8-foundation` off `bulldozer/main` (plain feature branch; compare against `bulldozer/main`, NOT `main`).

1. `bulldozer:check standard` on the diff (or on `skills/check/scripts/bulldozer-round.sh` + `render-trajectory.py` + `emit-pivot.py` + `parse-ledger-patch.py`). Reviewer per Step-1 model selection. **Read `parsed-rN.json` (`meta.verdict` + `len(findings)`) before any GO/NO-GO claim. Never merge at NO-GO.** Verify confirmed findings empirically (`/receiving-code-review`), fix, one confirming round after a late fix. Expect 2-4 rounds (refactor-heavy).
2. Open PR; verify number via `gh pr list --head bulldozer/feat/pr3a-b3-b8-foundation`.
3. `gh pr merge <N> --admin --squash` into `bulldozer/main`. CalVer auto-bumps post-merge (do NOT bump manually).
4. Update handoff `.remember/remember.md` + comment on #110 noting B3+B8 closed; PR-3b (B1+B2+B4+B7) is next. Re-verify merge from remote (`git ls-remote` + `gh pr view --json state`) before reporting done — harness can mangle large output.

## Self-review (writing-plans)

- **Spec coverage:** B3 (both heredocs → 2 scripts) ✓ Task 3+4. B8 (parser canonical verdict + wrapper drops fallback) ✓ Task 1+2. B1/B2/B4/B7 explicitly deferred to PR-3b ✓.
- **Placeholder scan:** none (all code shown; the only `...` is in the Task-2 fixture-build test where the exact stub pattern must match the file's existing `TestStateOutput` helpers — flagged inline to adapt at implementation).
- **Type consistency:** `render-trajectory.py` argv (round, max_rounds, state_path) and `emit-pivot.py` argv (round, max_rounds, open_findings, depth, artifact, pivot_path) match the wrapper callsites exactly. Module constants `RENDER_TRAJECTORY`/`EMIT_PIVOT` mirror existing `WRAPPER`/`PARSER` style. Canonical verdict strings `"go"`/`"no_go"` consistent across parser inject + wrapper read (`== "go"`).
