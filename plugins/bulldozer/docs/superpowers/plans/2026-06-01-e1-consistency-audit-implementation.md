# E1 Pre-Review Consistency Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-round "consistency auditor" to `bulldozer:check` that catches cheap self-consistency defects (dead refs, internal contradictions, cross-spec drift, stale terms) in doc/spec artifacts before each codex round — soft-enforced (LLM locates, a script kills hallucinations, Claude judges).

**Architecture:** A read-only `consistency-auditor` agent RETURNS a uniform finding envelope `{id, class, file, quote, anchor}`; Claude writes it to `e1-findings-rN.json`; `verify-audit-findings.py` keeps only findings whose cited quotes are verbatim-present (the one deterministic, anti-hallucination guarantee) → `e1-verified-rN.json`; Claude judges the survivors and fixes the real ones. No wrapper change, no hash, no refuse-loop. Design spec: `docs/superpowers/specs/2026-06-01-e1-pre-review-consistency-audit-design.md`.

**Tech Stack:** Python 3 (stdlib only: `argparse`, `json`, `pathlib`), pytest, bash, Claude Code plugin agents/skills/data conventions.

**Branch:** already on `bulldozer/94-e1-consistency-audit` (off `bulldozer/main`). Run tests from the plugin root `/0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer/`. Parse pytest verdicts via `--junit-xml` (per project discipline), not stdout.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `skills/check/data/e1-evidence-schema.json` | Frozen contract for the finding envelope + per-class `anchor` shape. Single source of truth for the agent body and the verifier. |
| `skills/check/scripts/verify-audit-findings.py` | The verifier: quote-presence only; writes survivors; fail-open. The one deterministic, behaviorally-tested unit. |
| `agents/consistency-auditor.md` | The read-only locator agent (frontmatter + body). |
| `skills/check/SKILL.md` | New per-round step prose + `Task` in `allowed-tools`. |
| `tests/test_verify_audit_findings.py` | Behavioral tests for the verifier. |
| `tests/test_skill_prompts.py` | Structural: SKILL.md step present + `TestE1SchemaContract` drift guard. |
| `tests/test_plugin_structure.sh` | Agent-structure check (tools exclude Bash/Edit/Write). |

---

## Task 1: Frozen evidence schema + drift guard

**Files:**
- Create: `skills/check/data/e1-evidence-schema.json`
- Test: `tests/test_skill_prompts.py` (add `TestE1SchemaContract`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_skill_prompts.py`:

```python
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
E1_SCHEMA = PLUGIN_ROOT / "skills" / "check" / "data" / "e1-evidence-schema.json"

class TestE1SchemaContract:
    """Drift guard for the E1 finding envelope (mirrors TestDepthConfigContract)."""

    def test_schema_exists_and_parses(self):
        data = json.loads(E1_SCHEMA.read_text())
        assert data["envelope"] == ["id", "class", "file", "quote", "anchor"]

    def test_four_classes_with_anchor_shapes(self):
        data = json.loads(E1_SCHEMA.read_text())
        anchors = data["anchor_by_class"]
        assert set(anchors) == {
            "dead_ref", "internal_contradiction", "cross_spec_drift", "stale_term"
        }
        assert anchors["internal_contradiction"] == ["quote_b"]
        assert anchors["cross_spec_drift"] == ["other_file", "other_quote"]
        assert anchors["dead_ref"] == ["ref"]
        assert anchors["stale_term"] == ["exclude_section"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_skill_prompts.py::TestE1SchemaContract -p no:cacheprovider -q`
Expected: FAIL — `FileNotFoundError` (schema not created yet).

- [ ] **Step 3: Create the schema file**

Create `skills/check/data/e1-evidence-schema.json`:

```json
{
  "envelope": ["id", "class", "file", "quote", "anchor"],
  "anchor_by_class": {
    "dead_ref": ["ref"],
    "internal_contradiction": ["quote_b"],
    "cross_spec_drift": ["other_file", "other_quote"],
    "stale_term": ["exclude_section"]
  },
  "notes": "quote is the literal citing text (verbatim from file). The verifier confirms quote-presence ONLY (anti-hallucination); Claude judges whether the cited text is a real defect of its class. See docs/superpowers/specs/2026-06-01-e1-pre-review-consistency-audit-design.md."
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_skill_prompts.py::TestE1SchemaContract -p no:cacheprovider -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add skills/check/data/e1-evidence-schema.json tests/test_skill_prompts.py
git commit -m "feat(check): E1 evidence schema + drift guard (#94)"
```

---

## Task 2: The verifier — quote-presence (GATE-A)

**Files:**
- Create: `skills/check/scripts/verify-audit-findings.py`
- Test: `tests/test_verify_audit_findings.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_verify_audit_findings.py`:

```python
import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
VERIFY = PLUGIN_ROOT / "skills" / "check" / "scripts" / "verify-audit-findings.py"


def _run(tmp_path, findings):
    """Write findings to e1-findings.json, run the verifier, return (rc, survivors)."""
    fin = tmp_path / "e1-findings.json"
    fin.write_text(json.dumps({"findings": findings}))
    out = tmp_path / "e1-verified.json"
    r = subprocess.run(
        [sys.executable, str(VERIFY), "--findings", str(fin),
         "--out", str(out), "--project-root", str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    survivors = json.loads(out.read_text())["findings"] if out.exists() else None
    return r.returncode, survivors


def _doc(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return name


def test_present_quote_survives(tmp_path):
    _doc(tmp_path, "spec.md", "the default is paused\n...\nplayback begins immediately")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "stale_term", "file": "spec.md",
                             "quote": "the default is paused", "anchor": {"exclude_section": "Changelog"}}])
    assert rc == 0
    assert [f["id"] for f in s] == ["F1"]


def test_absent_quote_dropped(tmp_path):
    _doc(tmp_path, "spec.md", "real content only")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "stale_term", "file": "spec.md",
                             "quote": "hallucinated text not in file", "anchor": {}}])
    assert rc == 0
    assert s == []


def test_empty_quote_dropped(tmp_path):
    _doc(tmp_path, "spec.md", "anything")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "stale_term", "file": "spec.md",
                             "quote": "   ", "anchor": {}}])
    assert rc == 0
    assert s == []


def test_internal_contradiction_needs_both_quotes(tmp_path):
    _doc(tmp_path, "spec.md", "status X finalizes ended")  # only quote_a present
    rc, s = _run(tmp_path, [{"id": "F1", "class": "internal_contradiction", "file": "spec.md",
                             "quote": "status X finalizes ended",
                             "anchor": {"quote_b": "status X is a RuntimeError"}}])
    assert rc == 0
    assert s == []  # quote_b absent -> dropped


def test_internal_contradiction_survives_when_both_present(tmp_path):
    _doc(tmp_path, "spec.md", "status X finalizes ended\n...\nstatus X is a RuntimeError")
    rc, s = _run(tmp_path, [{"id": "F1", "class": "internal_contradiction", "file": "spec.md",
                             "quote": "status X finalizes ended",
                             "anchor": {"quote_b": "status X is a RuntimeError"}}])
    assert rc == 0
    assert [f["id"] for f in s] == ["F1"]


def test_cross_spec_drift_needs_other_file_quote(tmp_path):
    _doc(tmp_path, "a.md", "field optional")
    _doc(tmp_path, "b.md", "unrelated")  # other_quote absent here
    rc, s = _run(tmp_path, [{"id": "F1", "class": "cross_spec_drift", "file": "a.md",
                             "quote": "field optional",
                             "anchor": {"other_file": "b.md", "other_quote": "field required"}}])
    assert rc == 0
    assert s == []


def test_missing_findings_file_fail_open(tmp_path):
    out = tmp_path / "e1-verified.json"
    r = subprocess.run(
        [sys.executable, str(VERIFY), "--findings", str(tmp_path / "nope.json"),
         "--out", str(out), "--project-root", str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []


def test_unparseable_findings_fail_open(tmp_path):
    fin = tmp_path / "e1-findings.json"
    fin.write_text("{not json")
    out = tmp_path / "e1-verified.json"
    r = subprocess.run(
        [sys.executable, str(VERIFY), "--findings", str(fin),
         "--out", str(out), "--project-root", str(tmp_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert json.loads(out.read_text())["findings"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_verify_audit_findings.py -p no:cacheprovider -q`
Expected: FAIL — verifier script does not exist (errors / non-zero, `out` missing).

- [ ] **Step 3: Write the verifier**

Create `skills/check/scripts/verify-audit-findings.py`:

```python
#!/usr/bin/env python3
"""E1 consistency-audit verifier (#94). Quote-presence only — anti-hallucination.

Reads a findings JSON (the consistency-auditor agent's output, written by Claude),
keeps only findings whose cited quote(s) are verbatim-present where claimed, and
writes the survivors. The ONE deterministic guarantee: a fabricated / absent quote
is dropped. It does NOT judge whether the cited text is a real defect of its class
— that is Claude's semantic call on the survivors.

Usage:
  verify-audit-findings.py --findings <in.json> --out <out.json> --project-root <dir>

Fail-open: an unreadable / unparseable findings file => writes {"findings": []} and
exits 0 (a dead auditor degrades to "no pre-clean this round", never blocks).
"""
import argparse
import json
import sys
from pathlib import Path


def _present(text, quote):
    """quote is non-empty (after strip) and verbatim-present (fixed-string) in text."""
    return bool(quote) and quote.strip() != "" and quote in text


def _read(root, rel):
    if not rel:
        return None
    try:
        return (Path(root) / rel).read_text(encoding="utf-8")
    except OSError:
        return None


def survives(finding, root):
    """True iff every cited quote of the finding is verbatim-present where claimed."""
    cls = finding.get("class")
    text = _read(root, finding.get("file", ""))
    quote = finding.get("quote", "")
    anchor = finding.get("anchor") or {}
    if text is None or not _present(text, quote):
        return False
    if cls == "internal_contradiction":
        quote_b = anchor.get("quote_b", "")
        return quote_b != quote and _present(text, quote_b)
    if cls == "cross_spec_drift":
        other_text = _read(root, anchor.get("other_file", ""))
        return other_text is not None and _present(other_text, anchor.get("other_quote", ""))
    # dead_ref + stale_term: quote-presence (GATE-A) is the whole deterministic check;
    # whether `ref` resolves / the term is stale-vs-intentional is Claude's judgment.
    return True


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--findings", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-root", required=True)
    args = ap.parse_args(argv)

    try:
        findings = json.loads(Path(args.findings).read_text(encoding="utf-8")).get("findings", [])
        if not isinstance(findings, list):
            findings = []
    except (OSError, json.JSONDecodeError):
        findings = []  # fail-open

    survivors = [f for f in findings if isinstance(f, dict) and survives(f, args.project_root)]
    Path(args.out).write_text(json.dumps({"findings": survivors}, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_verify_audit_findings.py -p no:cacheprovider --junit-xml=/tmp/e1-verify.xml -q`
Expected: PASS (8 tests). Confirm via junit: `failures=0 errors=0`.

- [ ] **Step 5: Commit**

```bash
chmod +x skills/check/scripts/verify-audit-findings.py
git add skills/check/scripts/verify-audit-findings.py tests/test_verify_audit_findings.py
git commit -m "feat(check): E1 verifier — quote-presence anti-hallucination gate (#94)"
```

---

## Task 3: The read-only consistency-auditor agent

**Files:**
- Create: `agents/consistency-auditor.md`
- Test: `tests/test_plugin_structure.sh` (add agent-structure check)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_plugin_structure.sh` (before its final exit/summary; adapt to the file's existing assertion style — the check below is self-contained):

```bash
# --- E1 consistency-auditor agent structure (#94) ---
AGENT="$PLUGIN_ROOT/agents/consistency-auditor.md"
if [[ ! -f "$AGENT" ]]; then echo "FAIL: $AGENT missing"; exit 1; fi
# tools must be read-only: exclude Bash/Edit/Write
if grep -qE 'tools:.*(Bash|Edit|Write)' "$AGENT"; then
  echo "FAIL: consistency-auditor tools must exclude Bash/Edit/Write (read-only)"; exit 1
fi
if ! grep -qE '^model:' "$AGENT"; then echo "FAIL: consistency-auditor missing model:"; exit 1; fi
echo "PASS: consistency-auditor agent structure"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash tests/test_plugin_structure.sh`
Expected: FAIL — "consistency-auditor agent missing".

- [ ] **Step 3: Create the agent**

Create `agents/consistency-auditor.md`:

```markdown
---
name: consistency-auditor
description: Read-only pre-review consistency auditor for bulldozer:check. Locates self-consistency defects (dead refs, internal contradictions, cross-spec drift, stale terms) in a markdown spec/plan and returns them as a structured envelope. Does NOT edit and does NOT judge — it locates and quotes.
tools: [Read, Grep, Glob]
model: sonnet
---

You are a pre-review consistency auditor. You run BEFORE an expensive external
reviewer (codex). Your ONLY job is to LOCATE cheap self-consistency defects so the
expensive reviewer is not wasted on them. You do NOT review design correctness,
logic, feasibility, or completeness — that is codex's job. You do NOT edit anything.

Read the artifact (and its sibling specs in the same directory) and find ONLY these
four classes. For each finding, copy the LITERAL citing text verbatim — never
paraphrase, never invent. A downstream script confirms every quote you give is
actually present in the file, so a fabricated quote silently drops your finding.

- **dead_ref** — a cited file/path/section/anchor/symbol that does not resolve.
- **internal_contradiction** — two places in THIS document stating conflicting things.
- **cross_spec_drift** — a shared contract diverges from a SIBLING spec it depends on.
- **stale_term** — a leftover old version string / resolved finding-ID / obsolete term
  in ACTIVE prose (not a changelog/history/"rejected" section).

Return ONLY a JSON object (no prose, no markdown fence) of the form:

{"findings": [
  {"id": "A1", "class": "dead_ref", "file": "<path>", "quote": "<verbatim citing line>",
   "anchor": {"ref": "<the cited target, verbatim substring of quote>"}},
  {"id": "A2", "class": "internal_contradiction", "file": "<path>", "quote": "<verbatim line 1>",
   "anchor": {"quote_b": "<verbatim line 2, the conflicting statement>"}},
  {"id": "A3", "class": "cross_spec_drift", "file": "<this path>", "quote": "<verbatim line here>",
   "anchor": {"other_file": "<sibling path>", "other_quote": "<verbatim line in sibling>"}},
  {"id": "A4", "class": "stale_term", "file": "<path>", "quote": "<verbatim stale text>",
   "anchor": {"exclude_section": "<heading of any changelog/history section to ignore>"}}
]}

Rules: every `quote`/`quote_b`/`other_quote` MUST be copied byte-for-byte from the
file. No style/wording/missing-feature/design/logic opinions. If the document is
clean, return {"findings": []}.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash tests/test_plugin_structure.sh`
Expected: "PASS: consistency-auditor agent structure" (and the rest of the suite still passes).

- [ ] **Step 5: Commit**

```bash
git add agents/consistency-auditor.md tests/test_plugin_structure.sh
git commit -m "feat(check): E1 read-only consistency-auditor agent (#94)"
```

---

## Task 4: SKILL.md step + allowed-tools + structural guard

**Files:**
- Modify: `skills/check/SKILL.md` (frontmatter `allowed-tools`; add the per-round audit step)
- Test: `tests/test_skill_prompts.py` (add `TestE1Step`)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_skill_prompts.py`:

```python
SKILL = PLUGIN_ROOT / "skills" / "check" / "SKILL.md"

class TestE1Step:
    """Pins the E1 pre-review consistency-audit step in SKILL.md (drift guard)."""

    def test_task_in_allowed_tools(self):
        text = SKILL.read_text()
        # frontmatter allowed-tools must include Task (auditor dispatch)
        assert '"Task"' in text or "Task," in text or "Task]" in text

    def test_step_names_the_pieces(self):
        text = SKILL.read_text()
        assert "consistency-auditor" in text          # agent dispatch
        assert "verify-audit-findings.py" in text     # verifier invocation
        assert "e1-verified-r" in text                # sole-licensed-fix input
        assert "audit_model" in text                  # config knob (default sonnet)

    def test_sole_licensed_fix_input_clause(self):
        text = SKILL.read_text().lower()
        # fix ONLY from e1-verified; raw e1-findings is forbidden as a fix source
        assert "e1-verified" in text and "e1-findings" in text
        assert "only" in text  # "fix only ... e1-verified"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_skill_prompts.py::TestE1Step -p no:cacheprovider -q`
Expected: FAIL — the step text / Task tool are not in SKILL.md yet.

- [ ] **Step 3: Edit SKILL.md**

3a. In the frontmatter `allowed-tools` array, add `"Task"`:
`allowed-tools: ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Task"]`

3b. Add a new step at the top of the per-round loop (immediately before the existing "Build the round prompt" step), verbatim:

```markdown
### Step 1.7: Pre-review consistency audit (E1, doc rounds only)

For a **doc/spec artifact** (a `.md`/`.mdx`/`.rst` file, a `docs`/`specs` directory,
or a diff touching doc files — skip for pure code), run this BEFORE building the
round prompt, every round:

1. **Locate.** Read `audit_model` from `.bulldozer/config.md` frontmatter (default
   `sonnet`). Dispatch the read-only auditor:
   `Task(subagent_type: "bulldozer:consistency-auditor", model: <audit_model>)`.
   The agent RETURNS a JSON envelope; YOU write it to `${REVIEW_DIR}/e1-findings-r${ROUND}.json`
   (the agent is read-only — it cannot write the file).
2. **Verify (anti-hallucination).** Run:
   `python3 <plugin>/skills/check/scripts/verify-audit-findings.py --findings ${REVIEW_DIR}/e1-findings-r${ROUND}.json --out ${REVIEW_DIR}/e1-verified-r${ROUND}.json --project-root ${PROJECT_ROOT}`.
   It keeps only findings whose quotes are verbatim-present and writes
   `e1-verified-r${ROUND}.json`. (Fail-open: on any error it writes an empty set and
   exits 0 — skip the pre-clean and proceed.)
3. **Judge + fix.** You may edit the artifact for a consistency finding ONLY if it
   appears in `e1-verified-r${ROUND}.json` (the sole licensed fix input — NEVER fix
   from the raw `e1-findings` file). For each survivor, apply judgment: is the cited
   text a real defect of its class (the dead_ref genuinely unresolved? the two present
   quotes genuinely conflicting? the drift real? the term stale-not-intentional)? Fix
   the real ones; DECLINE the intentional ones (declining is fine — nothing blocks).
4. **Commit separately** as `docs: pre-review consistency fixes (N)`. These are E1
   fixes, NOT codex-round fixes: never set `BULLDOZER_FIXED`/`BULLDOZER_FP` for them;
   if noted in `review-ledger.yml`, use a distinct `e1_audit:` note, never `R{round}-F{n}`.

Then proceed to the codex round normally. (Enforcement is soft: the verifier is
cheap, this step is pinned by a structural test, and `e1-verified-r${ROUND}.json`'s
presence in `${REVIEW_DIR}` makes a skip detectable in-session.)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_skill_prompts.py::TestE1Step -p no:cacheprovider -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add skills/check/SKILL.md tests/test_skill_prompts.py
git commit -m "feat(check): E1 SKILL.md step + Task allowed-tool (#94)"
```

---

## Task 5: Config knob + full-suite regression + CLAUDE.md

**Files:**
- Modify: `.bulldozer/config.md` is created per-project at runtime (no repo file); document the `audit_model` key in `skills/check/SKILL.md` Configuration section.
- Modify: `CLAUDE.md` (plugin) — add E1 to the `/check` architecture overview.

- [ ] **Step 1: Document `audit_model`**

In `skills/check/SKILL.md`'s existing `## Configuration` section, add under the `reviewer_model` example:

```markdown
`audit_model` (optional, default `sonnet`) — the model for the E1 consistency-auditor
subagent (Step 1.7). Flip to `haiku` for lower cost (lower contradiction-catch rate —
see the design spec §2 split test).
```

- [ ] **Step 2: Update plugin CLAUDE.md**

In `CLAUDE.md` under `## Architecture: /check`, add one line:

```markdown
E1 (#94): a per-round read-only `consistency-auditor` agent + `verify-audit-findings.py`
(quote-presence anti-hallucination) pre-cleans doc artifacts before each codex round;
soft-enforced (SKILL step + structural test). See
`docs/superpowers/specs/2026-06-01-e1-pre-review-consistency-audit-design.md`.
```

- [ ] **Step 3: Run the full relevant suite (regression)**

Run: `python3 -m pytest tests/test_verify_audit_findings.py tests/test_skill_prompts.py -p no:cacheprovider --junit-xml=/tmp/e1-full.xml -q` and `bash tests/test_plugin_structure.sh`
Expected: all green (parse junit: `failures=0 errors=0`); plugin-structure PASS.

- [ ] **Step 4: Commit**

```bash
git add skills/check/SKILL.md CLAUDE.md
git commit -m "docs(check): document audit_model + E1 in CLAUDE.md (#94)"
```

---

## Task 6: Dogfood + PR

- [ ] **Step 1:** Dogfood the implementation with `bulldozer:check standard` on the changed files (the CODE this time, which converges — unlike the recursive design spec). Verify findings empirically, fix real ones.
- [ ] **Step 2:** Confirm full suite green from a `bulldozer/main` checkout (not a worktree — gotcha #1 in `worktree-dev-gotchas`): `python3 -m pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_check_e2e.py -p no:cacheprovider --junit-xml=/tmp/e1-final.xml -q`.
- [ ] **Step 3:** Open PR to `bulldozer/main`: "feat(check): E1 pre-review consistency audit (#94)". Body references the design spec + the dogfood evidence (§2 of the spec). Do NOT bump `plugin.json` (auto-calver). Merge only at MERGEABLE+CLEAN+CI-SUCCESS+dogfood-GO. `Closes #94` does NOT auto-close on `bulldozer/main` — but #94 is an umbrella RFC; close it only if E1 was its last open slice, else comment "E1 shipped" and leave E2-E6 open.

---

## Self-Review notes

- **Spec coverage:** Task 1 = `e1-evidence-schema.json` (§3.2); Task 2 = `verify-audit-findings.py` quote-presence + fail-open (§3.4, §4); Task 3 = read-only agent, RETURNS not writes (§3.2, R6-F2); Task 4 = SKILL step + sole-licensed-fix-input + Task allowed-tool + accounting (§3.1, §3.2); Task 5 = `audit_model` (§3.2) + CLAUDE.md. The `bulldozer-round.sh` is deliberately UNCHANGED (soft enforcement) — no wrapper task, by design.
- **No wrapper/exit-code/hash tasks** — those were the rejected hard design (spec §3 note).
- **Type/name consistency:** `survives(finding, root)` / `_present(text, quote)` / `_read(root, rel)` used consistently across Task 2 code + tests; envelope keys `{id,class,file,quote,anchor}` + anchor subkeys (`quote_b`, `other_file`/`other_quote`, `ref`, `exclude_section`) match the schema (Task 1), the agent body (Task 3), and the verifier (Task 2).
- **Known follow-up (not blocking):** the auditor's recall/FP is model behaviour, validated by the spec's empirical split test, not a unit test (intentional — §5).
