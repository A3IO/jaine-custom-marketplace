# Consult Panel — gemini `write_file` → empty `response` fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the gemini leg of `/bulldozer:consult --panel --repo` from failing with an empty `response`, and stop mislabeling an empty response as "unparseable".

**Architecture:** Two surgical changes to `skills/consult/scripts/consult_panel.py`. **Fix B (root):** add a shared `_INFORMED_NO_WRITE` clause to the footer of both informed `_WRAP_TABLE` cells (so gemini answers as text instead of calling `write_file` to save a plan file) — appended in the find-holes cell, inserted *before* `_VERDICT_TAIL` in the verdict cell so the anchored `VERDICT:` final line survives. **Fix A (diagnostics):** make `_parse_json_field` three-way (non-empty → string, present-but-empty → `""`, absent/unparseable → `None`) with multi-candidate "last non-empty wins" precedence, and have `_run_one` report a present-but-empty field as an honest "empty response" instead of "unparseable output".

**Tech Stack:** Python 3, pytest (offline injected-runner tests). Tests import the script as the `panel` module via `conftest.PLUGIN_ROOT`. Source of truth: `docs/superpowers/specs/2026-06-03-consult-gemini-write-file-design.md` (v1.3.0, bulldozer:check GO).

**Hard constraints:**
- Do NOT bump `plugin.json` — `auto-calver` post-merge hook does it on merge (manual bump = double-bump).
- No retry, no isolated-cell clause, no model pinning (YAGNI — see spec Non-goals).
- TDD strict: write the test, RUN it, SEE it fail (RED), then implement, RUN, SEE it pass (GREEN). Never skip the RED run.
- Baseline before starting: `python3 -m pytest tests/test_consult_panel.py -q` → 107 passed.

---

### Task 1: Fix A parser — `_parse_json_field` three-way + multi-candidate ordering

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` (`_parse_json_field`)
- Test: `tests/test_consult_panel.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_consult_panel.py` after `test_parse_gemini_missing_response_is_none`:

```python
def test_parse_gemini_present_but_empty_returns_empty_sentinel():
    # the actual bug payload: valid JSON, response present but empty → "" sentinel, NOT None
    assert panel.parse_gemini(json.dumps({"session_id": "x", "response": "", "stats": {}})) == ""


def test_parse_gemini_multi_candidate_empty_last_keeps_non_empty():
    # banner/payload stream: a trailing empty candidate must NOT clobber an earlier non-empty one
    raw = json.dumps({"response": "ok"}) + "\n" + json.dumps({"response": ""})
    assert panel.parse_gemini(raw) == "ok"


def test_parse_gemini_multi_candidate_empty_first_keeps_non_empty():
    raw = json.dumps({"response": ""}) + "\n" + json.dumps({"response": "ok"})
    assert panel.parse_gemini(raw) == "ok"


def test_parse_gemini_multi_candidate_all_empty_returns_sentinel():
    raw = json.dumps({"response": ""}) + "\n" + json.dumps({"response": ""})
    assert panel.parse_gemini(raw) == ""


def test_parse_gemini_field_absent_still_none():
    # regression: field never present (not just empty) stays None
    assert panel.parse_gemini(json.dumps({"stats": {}})) is None


def test_parse_grok_present_but_empty_returns_empty_sentinel():
    assert panel.parse_grok(json.dumps({"text": ""})) == ""


def test_parse_grok_multi_candidate_empty_last_keeps_non_empty():
    raw = json.dumps({"text": "ok"}) + "\n" + json.dumps({"text": ""})
    assert panel.parse_grok(raw) == "ok"


def test_parse_grok_multi_candidate_empty_first_keeps_non_empty():
    # full grok symmetry with gemini (spec test 2b: "the same three orderings")
    raw = json.dumps({"text": ""}) + "\n" + json.dumps({"text": "ok"})
    assert panel.parse_grok(raw) == "ok"


def test_parse_grok_multi_candidate_all_empty_returns_sentinel():
    raw = json.dumps({"text": ""}) + "\n" + json.dumps({"text": ""})
    assert panel.parse_grok(raw) == ""
```

- [ ] **Step 2: Run the tests, verify they FAIL (RED)**

Run: `python3 -m pytest tests/test_consult_panel.py -k "present_but_empty or multi_candidate or field_absent_still_none" -v`
Expected: the `present_but_empty` and `all_empty` cases FAIL — current `_parse_json_field` returns `None` for `{"response":""}` (asserts expect `""`). The `empty_last`/`empty_first`/`field_absent` cases pass already (last non-empty wins / absent → None). At least 3 failures.

- [ ] **Step 3: Implement the three-way parser** — replace the body of `_parse_json_field` in `skills/consult/scripts/consult_panel.py`:

```python
def _parse_json_field(stdout: str, field: str) -> str | None:
    """Return the LAST top-level string ``field`` across JSON candidates in stdout.

    Three-way result (R1-F1): the last NON-EMPTY value wins (a later empty candidate
    never clobbers an earlier non-empty one — the real payload may trail banner noise
    either way); if the field appeared as a string in some candidate but every such
    value was empty/whitespace, return ``""`` (sentinel: "structured output, no text" —
    the gemini write_file bug); if the field was never present as a string, return
    ``None`` (genuinely unparseable / model failure)."""
    found: str | None = None
    saw_field = False
    for data in _json_candidates(stdout):
        if isinstance(data, dict):
            value = data.get(field)
            if isinstance(value, str):
                saw_field = True
                if value.strip():
                    found = value.strip()  # last non-empty wins — unchanged priority
    if found is not None:
        return found
    return "" if saw_field else None  # present-but-all-empty → ""; never present → None
```

- [ ] **Step 4: Run the tests, verify they PASS (GREEN)**

Run: `python3 -m pytest tests/test_consult_panel.py -k "parse_gemini or parse_grok" -v`
Expected: ALL pass (new three-way cases + existing parse_gemini/parse_grok tests — extract, malformed→None, missing→None, banner tolerance, last-object-wins).

- [ ] **Step 5: Commit**

```bash
git add tests/test_consult_panel.py skills/consult/scripts/consult_panel.py
git commit -m "fix(consult): _parse_json_field three-way — empty field → '' sentinel

Present-but-empty target field returns '' (was None); last-non-empty still wins
across multi-candidate streams; absent field still None. Lets _run_one tell a
model that produced no text apart from genuinely unparseable output (R1-F1)."
```

---

### Task 2: Fix A diagnostics — `_run_one` honest "empty response" reason

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` (`_run_one`, the parse-failure branch)
- Test: `tests/test_consult_panel.py`

**Depends on Task 1** (the `""` sentinel must already flow from the parser).

- [ ] **Step 1: Write the failing tests** — append after `test_run_one_empty_output_reason`:

```python
def test_run_one_empty_response_field_is_honest_not_unparseable():
    """A valid-JSON-but-empty-field (the gemini write_file bug) → honest 'empty
    response', NOT the misleading 'unparseable output'."""
    def runner(cmd, env, cwd, timeout):
        return panel.ModelResult(True, json.dumps({"text": ""}), None)  # grok empty field
    r = panel._run_one("grok", "W", None, 10, runner)
    assert r.output is None
    assert "empty response" in (r.reason or "")
    assert "unparseable" not in (r.reason or "")


def test_run_one_non_json_still_unparseable():
    """Regression: genuinely unparseable output keeps the 'unparseable output: <snippet>'
    reason (None path, not the '' sentinel path)."""
    def runner(cmd, env, cwd, timeout):
        return panel.ModelResult(True, "GARBLED_NOT_JSON_xyz", None)
    r = panel._run_one("grok", "W", None, 10, runner)
    assert r.output is None
    assert "unparseable" in (r.reason or "")
    assert "GARBLED_NOT_JSON_xyz" in (r.reason or "")
```

- [ ] **Step 2: Run the tests, verify they FAIL (RED)**

Run: `python3 -m pytest tests/test_consult_panel.py -k "empty_response_field_is_honest or non_json_still_unparseable" -v`
Expected: `empty_response_field_is_honest` FAILS — current `_run_one` checks `if output is None`, so a `""` from the parser hits `LegResult.ok(..., "")` (a survivor with empty text), never reaching a failure reason → no "empty response". `non_json_still_unparseable` passes already.

- [ ] **Step 3: Implement the honest reason branch** — replace the parse-failure tail of `_run_one` in `skills/consult/scripts/consult_panel.py`:

```python
    if not result.ok:
        return LegResult.failed(spec.display, result.reason)
    output = spec.parser(result.output or "")
    if output:
        return LegResult.ok(spec.display, output)
    # failure: distinguish a present-but-empty field ("") from unparseable (None).
    # Model-neutral reason — parse_grok can also yield ""; the gemini-specific
    # write_file cause is documented in SKILL.md, not baked into a shared reason.
    if output == "":
        reason = "empty response — model returned structured output with no text"
    else:  # output is None
        snippet = _sanitize(result.output)[:200]
        reason = f"unparseable output: {snippet}" if snippet else "empty output"
    return LegResult.failed(spec.display, reason)
```

- [ ] **Step 4: Run the tests, verify they PASS (GREEN)**

Run: `python3 -m pytest tests/test_consult_panel.py -k "run_one" -v`
Expected: ALL pass — new honest/unparseable tests + existing `test_run_one_returns_legresult`, `test_run_one_parse_failure_includes_output_context`, `test_run_one_empty_output_reason` (empty stdout → None → "empty output" unchanged).

- [ ] **Step 5: Commit**

```bash
git add tests/test_consult_panel.py skills/consult/scripts/consult_panel.py
git commit -m "fix(consult): honest 'empty response' reason for present-but-empty field

_run_one switches to truthiness success check; a '' sentinel from the parser now
reports 'empty response — model returned structured output with no text' instead
of the misleading 'unparseable output'. None still → unparseable/empty output."
```

---

### Task 3: Fix B root — anti-`write_file` clause in the informed prompt footer

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` (`_INFORMED_NO_WRITE` constant + two `_WRAP_TABLE` informed cells)
- Test: `tests/test_consult_panel.py`

- [ ] **Step 1: Write the failing tests** — append after `test_wrap_verdict_repo_tokens_classify_correctly`:

```python
def test_wrap_find_holes_repo_appends_no_write_clause():
    w = panel.wrap("q", repo=True)  # find-holes informed
    assert "do NOT call write_file" in w
    assert w.rstrip().endswith("plan or report document.")  # trailing-suffix position


def test_wrap_verdict_repo_has_no_write_clause():
    w = panel.wrap("q", verdict=True, repo=True)
    assert "do NOT call write_file" in w


def test_wrap_verdict_repo_still_ends_with_verdict_tail():
    # CRITICAL (consult panel finding): the no-write clause must sit BEFORE the
    # verdict tail so the prompt still ends with the anchored VERDICT line that
    # classify_verdict requires. A blind append would break this.
    w = panel.wrap("q", verdict=True, repo=True)
    assert w.rstrip().endswith(panel._VERDICT_TAIL)


def test_wrap_isolated_cells_have_no_no_write_clause():
    # bug is informed-only; isolated has no repo + a text-only wrapper already
    assert "do NOT call write_file" not in panel.wrap("q")               # find-holes isolated
    assert "do NOT call write_file" not in panel.wrap("q", verdict=True)  # verdict isolated
```

- [ ] **Step 2: Run the tests, verify they FAIL (RED)**

Run: `python3 -m pytest tests/test_consult_panel.py -k "no_write_clause or verdict_repo_still_ends" -v`
Expected: exactly **2 FAIL** — `test_wrap_find_holes_repo_appends_no_write_clause` and
`test_wrap_verdict_repo_has_no_write_clause` (the clause is not present yet). The other two PASS
already: `test_wrap_verdict_repo_still_ends_with_verdict_tail` (the current verdict-informed footer
already ends with `_VERDICT_TAIL` — this test guards that the fix doesn't *break* the anchor, so it
is green before AND after) and `test_wrap_isolated_cells_have_no_no_write_clause` (no clause
anywhere). Seeing only 2 FAIL here is correct, not a problem.

- [ ] **Step 3: Implement the constant + per-cell placement** — in `skills/consult/scripts/consult_panel.py`, add the constant right after `_INFORMED_HEADER`:

```python
_INFORMED_NO_WRITE = (
    "Output your entire answer as plain text in this response — do NOT call write_file, "
    "do NOT create or save any file, do NOT defer your findings to a plan or report document."
)
```

Then change ONLY the two informed cells of `_WRAP_TABLE` (leave both isolated cells untouched):

```python
    (False, True): (  # find-holes, informed
        _INFORMED_HEADER,
        "List the most important holes, risks, or bugs in the code relevant to "
        "the question. Be specific and concrete — cite file and function names. "
        "Number each as a one-line point. Max 8 points. " + _INFORMED_NO_WRITE,
    ),
```

```python
    (True, True): (  # verdict, informed
        _INFORMED_HEADER,
        "Give a decisive verdict under 200 words, citing specific files. "
        + _INFORMED_NO_WRITE
        + " End with exactly one final standalone line — one of:\n" + _VERDICT_TAIL,
    ),
```

- [ ] **Step 4: Run the tests, verify they PASS (GREEN)**

Run: `python3 -m pytest tests/test_consult_panel.py -k "wrap" -v`
Expected: ALL wrap tests pass — new no-write/anchor tests + existing `test_wrap_verdict_repo_tokens_classify_correctly`, `test_wrap_selects_2x2_header_and_footer`, `test_wrap_named_views_match_unified`, isolated/find-holes wrap tests.

- [ ] **Step 5: Commit**

```bash
git add tests/test_consult_panel.py skills/consult/scripts/consult_panel.py
git commit -m "fix(consult): anti-write_file clause in informed prompt footer

gemini in plan-mode called write_file to save findings to a plans/*.md file and
left response empty. Append a no-write clause to the find-holes informed footer
and INSERT it before _VERDICT_TAIL in the verdict informed footer so the anchored
VERDICT line stays last (classify_verdict contract). Isolated cells untouched."
```

---

### Task 4: Docs — SKILL.md caveat + CLAUDE.md pointer

**Files:**
- Modify: `skills/consult/SKILL.md` (Panel Mode section)
- Modify: `CLAUDE.md` (consult panel notes)

- [ ] **Step 1: Add the SKILL.md caveat** — in `skills/consult/SKILL.md`, in the "Panel Mode (`--panel`)" section, after the "**Isolation:**" paragraph, add:

```markdown
**gemini large-context caveat:** on big informed (`--repo`) questions gemini's agentic
plan-mode may call `write_file` to save its findings to a `plans/*.md` file and leave
`response` empty (non-deterministic). The informed prompt footer now instructs text-only
output (no `write_file`); an empty field still degrades to a `[Gemini: failed — empty
response …]` block and the panel continues with the surviving models.
```

- [ ] **Step 2: Add the CLAUDE.md pointer** — in `CLAUDE.md` (the bulldozer plugin root, under the consult panel notes / "Panel mode" paragraph), add one sentence:

```markdown
On large informed questions gemini may write findings to a file instead of `response`
(empty-response bug); the informed prompt now forces text-only output. Design: `docs/superpowers/specs/2026-06-03-consult-gemini-write-file-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add skills/consult/SKILL.md CLAUDE.md
git commit -m "docs(consult): document gemini write_file empty-response caveat + fix"
```

---

### Task 5: Empirical bench-test (acceptance gate — NOT a unit test)

**Files:** none modified. This is the pre-merge gate from the spec: the prompt change must be confirmed against the live models, in the **in-code footer position** (not the test-only suffix).

- [ ] **Step 1: Full offline suite green**

Run: `python3 -m pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py -q`
Expected: all pass except the 2 pre-existing `marketplace.json` env-fails in `test_cdp.py` (look domain, unrelated — documented). consult tests all green.

- [ ] **Step 2: Live gemini find-holes-informed bench (n≥2)** — run the gemini leg through the real module functions on a large informed question (the prod question in `/tmp/prod_q.txt`, or any spec+repo question), via the in-code footer:

```bash
cd /0/.aitemp/bulldozer-consult-gemini-fix
python3 - <<'PY'
import sys, os, subprocess, tempfile, json
from pathlib import Path
sys.path.insert(0, "skills/consult/scripts")
from consult_panel import build_gemini_sandbox, build_gemini_cmd, wrap, _filter_env
# R2-F1: keep the question and the cwd repo coherent — the prod question is about goat;
# the fallback question is about THIS repo (which actually contains consult_panel.py). goat does
# NOT contain consult_panel.py, so pairing the fallback question with a goat cwd would validate
# an incoherent pair.
if os.path.exists("/tmp/prod_q.txt"):
    q = open("/tmp/prod_q.txt").read().strip()
    repo_cwd = "/0/SANDBOX/ASSISTS/goat"          # prod question targets the goat repo
else:
    q = "Review consult_panel.py and find the biggest design risks. Be specific."
    repo_cwd = os.getcwd()                        # this worktree — has skills/consult/scripts/consult_panel.py
wrapped = wrap(q, repo=True)                 # find-holes informed — uses the new footer
for i in (1, 2):
    with tempfile.TemporaryDirectory() as mt:
        home = build_gemini_sandbox(Path(mt))
        cmd, ov = build_gemini_cmd(wrapped, home); cmd = cmd[:1] + ["-m", "gemini-2.5-flash"] + cmd[1:]
        env = _filter_env(os.environ.copy()); env.update(ov)
        p = subprocess.run(cmd, env=env, cwd=repo_cwd, capture_output=True, text=True, timeout=200)
        d = json.loads(p.stdout); resp = d.get("response", "")
        wf = d.get("stats", {}).get("tools", {}).get("byName", {}).get("write_file", {}).get("count", 0)
        print(f"run{i}: resp_len={len(resp)} EMPTY={resp==''} write_file={wf}")
PY
```
Expected: both runs `EMPTY=False`, `write_file=0`.

- [ ] **Step 3: Live gemini preview model-independence bench (n=1)** — re-run the Step 2 harness
  once with the default preview model (drop the `cmd = cmd[:1] + ["-m", "gemini-2.5-flash"] + cmd[1:]`
  line so the user's configured `gemini-3.1-pro-preview` is used). Expected: `EMPTY=False`,
  `write_file=0`. This is the spec's model-independence check — best-effort, since preview is
  slow/non-deterministic; one clean run is the bar, not a guarantee. If it still empties with
  `write_file≥1`, note it but do not block solely on preview (flash + codex/grok + the unit tests
  are the hard gate).

- [ ] **Step 4: Live gemini verdict-informed bench (n≥1)** — same Step 2 flash harness with
  `wrap(q, verdict=True, repo=True)`; assert `response` non-empty AND its final non-empty line is a
  `VERDICT:` token (the anchor survived the clause sitting before it).

- [ ] **Step 5: codex + grok benign (n≥1 each)** — run a full `python3 skills/consult/scripts/consult_panel.py --repo <path> "<question>"` and confirm codex + grok still return normal critiques with the no-write clause present (they appear as survivors in `## SHARED` / `## Raw critiques`).

- [ ] **Step 6: Record results** — append the bench numbers (flash find-holes / preview / verdict-line / codex+grok survival) to the PR description or a short note in the review dir. If any gemini **flash find-holes** run is still empty with `write_file≥1`, STOP — the footer position is insufficient; do NOT merge (re-open the design, the suffix-vs-footer gap is real). (Preview-only failure is a soft signal per Step 3, not a merge blocker.)

---

## Self-Review

**Spec coverage:**
- Fix B (informed footer, per-cell placement, isolated untouched) → Task 3. ✓
- Fix A parser three-way + multi-candidate ordering → Task 1. ✓
- Fix A `_run_one` honest reason (model-neutral) → Task 2. ✓
- Testing single-object + 2b multi-candidate → Task 1 (parser) + Task 2 (run_one). ✓
  (grok symmetry: all three orderings — empty-last / empty-first / all-empty — pinned, R1-F1.)
- Verdict-anchor regression test → Task 3 `test_wrap_verdict_repo_still_ends_with_verdict_tail`. ✓
- Docs (SKILL.md + CLAUDE.md) → Task 4. ✓
- Empirical bench (footer position + gemini flash + preview model-independence + codex/grok benign) → Task 5. ✓ (R1-F2 added the preview run.)
- Acceptance criteria 1–6 → Tasks 1–5 + offline suite. ✓

**Type/name consistency:** `_INFORMED_NO_WRITE` (Task 3), `_parse_json_field` returning `str | None` with `""` sentinel (Task 1) consumed by `_run_one` (Task 2) — names match across tasks. Tests use the existing `panel.ModelResult(ok, output, reason)` / `panel._run_one(name, wrapped, repo, timeout, runner)` / `panel.wrap(q, verdict=, repo=)` signatures verified against the current test file.

**Placeholder scan:** none — every code/test step carries full code; every run step carries the exact command + expected outcome.
