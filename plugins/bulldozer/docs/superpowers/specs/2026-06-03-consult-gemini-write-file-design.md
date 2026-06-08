# Consult Panel — gemini `write_file` → empty `response` fix

*Spec | 2026-06-03 | branch `bulldozer/fix/consult-gemini-write-file`*

## Problem

In `/bulldozer:consult --panel --repo` (informed multi-model find-holes), the **gemini**
leg intermittently fails with:

```
[Gemini: failed — unparseable output: {
  "session_id": "...",
  "response": "",
  "stats": { "models": { "gemini-3.1-pro-preview": { "api": { "totalRequests": 2, ... } } } } }]
```

Observed in production session `df1fcf9a` (goat repo, large design-spec review). The panel
itself degraded correctly — codex + grok survived and produced `## SHARED (all 2)` — but the
gemini perspective was lost on exactly the kind of large, high-stakes question where
multi-model diversity matters most, and the failure message is misleading.

## Root cause (empirically established)

gemini, in its agentic plan-mode, calls the **`write_file`** tool to save its findings into a
`plans/*.md` file, and returns its `response` field **empty** (or a one-line "documented in
file" pointer). Our `parse_gemini` reads only `.response`, sees an empty string, and reports a
failure. The behaviour is non-deterministic: sometimes gemini answers inline (`response`
populated), sometimes it "saves a plan" (`response` empty). codex and grok never do this — they
return their critique as text.

**Evidence — 8 runs of the same prod question (gemini leg, exact panel conditions):**

| Run | Model | Outcome | `write_file` |
|-----|-------|---------|--------------|
| prod | gemini-3.1-pro-preview | `response:""` | (truncated, empty resp) |
| repro #1 (tiny 1-file question) | preview | success, full | 0 |
| repro #2 | preview | timeout >320s | — |
| battery A | preview | success | — |
| battery B | gemini-2.5-flash | **`response:""`** | — |
| battery C | gemini-2.5-pro | success | — |
| flash dump | gemini-2.5-flash | full resp, BUT ends "documented in `…/plans/bulldozer_loop_review.md`" | **1** |
| anti-write ×3 | flash | **3/3 non-empty** | **0** |

The flash dump is the smoking gun: `stats.tools.byName.write_file = {count:1, success:1}`, the
`response` body literally says it wrote findings to `plans/bulldozer_loop_review.md`. Adding an
explicit "do not write files, answer as text" instruction flips it: **3/3 non-empty,
`write_file=0`.**

**Ruled out empirically:**
- *Not* the sandbox / missing `settings.json` — repro #1 succeeded in identical sandbox conditions.
- *Not* context overflow — codex + grok handled the same 838 MB / 3369-file repo (`## SHARED all 2`).
- *Not* one model — both `preview` and `flash` emptied; `pro-2.5` succeeded once. Model-independent.
- *Not* our regression — gemini flags (`-e none`, `--approval-mode plan`) unchanged since `08aad50`;
  cause is external gemini-CLI agentic behaviour triggered by large informed questions.

Two distinct defects fall out:

- **Defect B (root):** gemini writes its answer to a file instead of `response`.
- **Defect A (diagnostics):** when `response` is a present-but-empty string, `_run_one` reports
  `unparseable output: <json>` — a lie. The JSON parsed fine; the *field* was empty.

## Design

### Fix B (root) — anti-`write_file` instruction in the informed prompt

Add the no-write instruction to both informed cells of `_WRAP_TABLE`
(`(verdict=False, repo=True)` find-holes-informed and `(verdict=True, repo=True)`
verdict-informed), via a shared constant:

```python
_INFORMED_NO_WRITE = (
    "Output your entire answer as plain text in this response — do NOT call write_file, "
    "do NOT create or save any file, do NOT defer your findings to a plan or report document."
)
```

**Placement is per-cell — NOT a blind append (consult panel finding, critical).** The
verdict-informed footer ends with `_VERDICT_TAIL` (`VERDICT: GO\nVERDICT: NO-GO\nVERDICT:
MINOR-FIXES`), and `classify_verdict` / `_VERDICT_LINE` require the anchored `VERDICT:` line to be
the **final standalone line**. Appending `_INFORMED_NO_WRITE` *after* `_VERDICT_TAIL` would push
text past the anchor and break that contract. So:

- **find-holes-informed `(False, True)`** — footer ends with "…Number each as a one-line point.
  Max 8 points." → the no-write clause is **appended** (it becomes the trailing instruction, which
  is exactly the empirically-validated suffix position).
- **verdict-informed `(True, True)`** — the no-write clause is **inserted before** the "End with
  exactly one final standalone line — one of:\n" + `_VERDICT_TAIL` segment, so the prompt still
  **ends with `_VERDICT_TAIL`**. New footer shape: `"Give a decisive verdict … citing specific
  files. " + _INFORMED_NO_WRITE + " End with exactly one final standalone line — one of:\n" +
  _VERDICT_TAIL`.

Rationale for the trailing position in the find-holes cell: the empirically-validated instruction
was a *suffix* at the very end of the wrapped prompt (`anti-write ×3 → 3/3`); a trailing directive
is the most reliable position for an LLM. In the verdict cell the anchor contract wins over ideal
position — but the no-write clause still sits late in the prompt (just before the verdict tail),
and the empirical bench-test (below) **must** exercise this exact placement, not just the test-only
suffix (the bench-test is a pre-merge gate, not yet run at spec time).

Isolated cells are **not** touched — the bug requires repo access + tool use; the isolated wrapper
already says "Do not inspect files or run tools. Text-only critique," and the cwd is an empty
tempdir with nothing to write. Adding the clause to every prompt would be noise (YAGNI).

The instruction is expected to be benign for codex (`-s read-only`) and grok (`--permission-mode
plan`) — they already answer as text and writes are blocked at the process level, so a "do not
write" directive should be a no-op for them. The bench-test (below) **must confirm** this rather
than the spec merely asserting it.

### Fix A (diagnostics) — distinguish empty-field from unparseable

Today `_parse_json_field` returns `None` for both "no JSON / field absent" and "field present but
empty". `_run_one` then renders the misleading `unparseable output: <json>`. Change `_parse_json_field`
to a three-way result so the caller can tell them apart:

- JSON found, target field is a **non-empty** string → return that string (unchanged).
- JSON found, target field **present but empty/whitespace** → return the empty string `""`
  (sentinel: "structured output, but the model produced no text").
- No parseable JSON object with the field at all → return `None` (genuinely unparseable).

**Multi-candidate ordering (R1-F1, codex):** `_json_candidates` may yield several `{...}` objects
(banner/telemetry noise before the real payload). The existing contract is "the LAST top-level
non-empty `field` wins" — a non-empty value must NOT be lost just because a later candidate carries
an empty one. So the three-way precedence is: **any non-empty value seen → return the last
non-empty one** (unchanged priority); **else if the field was present as a string in some candidate
but every such value was empty → return `""`**; **else (field never present as a string) →
`None`**. Concretely, track both "last non-empty" and "field-was-seen-as-string", and only fall
back to `""` when a non-empty was never found:

```python
found: str | None = None
saw_field = False
for data in _json_candidates(stdout):
    if isinstance(data, dict):
        value = data.get(field)
        if isinstance(value, str):
            saw_field = True
            if value.strip():
                found = value.strip()   # last non-empty wins — unchanged
if found is not None:
    return found
return "" if saw_field else None         # present-but-all-empty → ""; never present → None
```

This keeps `{"response":"ok"} … {"response":""}` → `"ok"` (no regression), and only a genuinely
empty-only payload (`{"response":""}`, the actual bug) → `""`.

In `_run_one`, the parse-failure branch becomes:

```python
output = spec.parser(result.output or "")
if output:                       # non-empty answer
    return LegResult.ok(spec.display, output)
# failure: distinguish the two empty cases
if output == "":                 # valid structure, empty text field
    reason = "empty response — model returned structured output with no text"
else:                            # output is None → unparseable
    snippet = _sanitize(result.output)[:200]
    reason = f"unparseable output: {snippet}" if snippet else "empty output"
return LegResult.failed(spec.display, reason)
```

The reason is **model-neutral** (consult panel finding): `parse_grok` can also return `""`, and
"may have written to a file" is gemini-specific — it would mislead for grok's `.text` field. The
gemini-specific cause (the `write_file` agentic behaviour) is documented in SKILL.md, not baked
into a shared runtime reason. "structured output with no text" is true for any JSON model that
returns an empty target field.

`parse_codex` is unchanged — it has no JSON structure, so "empty stdout" is just `None` → the
existing `empty output` reason still applies. The sentinel only flows from the JSON parsers
(`parse_grok`, `parse_gemini`), and both benefit from the honest reason symmetrically.

**Contract note (corrects a false claim caught by the panel):** `parse_*` now returns `str | None`
where `""` is a *meaningful* value (empty-field), distinct from `None` (unparseable). The
empty-string sentinel from a parser is consumed at **exactly one call site — `_run_one`** — which
converts both `""` and `None` into `LegResult.failed(..., output=None)` (with different reasons).
Everything downstream (`run_panel`'s survivor/failure list comprehensions, `decide_merge`) reads
`LegResult.output`, which is **always `None` for a failure** — so the existing `r.output is not
None` filters keep working unchanged. The earlier draft's claim that "all call sites use `if
output:`" was wrong: downstream sites use `is not None` on `LegResult.output`, and they are
correct precisely because `_run_one` never emits a `LegResult` carrying `""`.

## Testing (TDD, RED first)

Offline, injected-runner style (no real subprocess) consistent with existing `test_consult_panel.py`:

1. **Fix B structural:**
   - `wrap(q, repo=True)` (find-holes informed) contains the no-write clause; `write_file` appears,
     and the prompt **ends with** the no-write clause (trailing-suffix position).
   - `wrap(q, verdict=True, repo=True)` (verdict informed) contains the no-write clause **AND still
     ends with `_VERDICT_TAIL`** (the anchor-contract regression for the critical panel finding —
     this is the test that would have caught a blind append).
   - `classify_verdict(wrap(q, verdict=True, repo=True) + "\n<model fills in>\nVERDICT: GO")` style
     check is overkill; the `endswith(_VERDICT_TAIL)` assertion on the wrapped prompt is the guard.
   - `wrap(q)` (isolated) and `wrap(q, verdict=True)` (verdict isolated) do **not** contain it.
2. **Fix A diagnostics (single-object):**
   - `parse_gemini('{"response":""}')` → `""` (not `None`).
   - `parse_gemini('{"stats":{}}')` (field absent) → `None`.
   - `parse_gemini('<<<')` (not JSON) → `None`.
   - `_run_one` with a runner returning `{"response":""}` → `reason` contains `empty response`,
     does **not** contain `unparseable`.
   - `_run_one` with a runner returning non-JSON → `reason` contains `unparseable`.
   - `parse_codex("")` → `None` unchanged; its `_run_one` failure reason stays `empty output`.
2b. **Fix A multi-candidate ordering (R2-F1 — the rule defined in Fix A MUST be pinned by tests,
    or an implementation can pass the single-object cases while regressing the real banner+payload
    stream):**
   - **empty-last:** `parse_gemini('{"response":"ok"} {"response":""}')` → `"ok"` (last non-empty
     wins; the trailing empty does NOT clobber it — this is the no-regression guard).
   - **empty-first:** `parse_gemini('{"response":""} {"response":"ok"}')` → `"ok"`.
   - **present-but-all-empty (multi):** `parse_gemini('{"response":""} {"response":""}')` → `""`
     (sentinel, not `None`).
   - **grok symmetry:** the same three orderings on `parse_grok` over the `.text` field (empty-last
     → last non-empty, present-but-all-empty → `""`), since both share `_parse_json_field`.
3. **Regression:** a populated `{"response":"finding"}` still yields a survivor with that text;
   existing grok trailing-noise / missing-field tests still pass.

## Empirical verification (beyond unit tests)

Before declaring done, re-run the live legs in panel conditions (the instruction is a prompt
change — must be bench-tested per project doctrine, not shipped on unit tests alone). The
empirical anti-write run used a *suffix after the whole wrapped prompt*; the in-code change puts
the clause in the **footer** (find-holes: trailing; verdict: before `_VERDICT_TAIL`). Those exact
positions must be the ones tested, not the suffix:

- **gemini flash, prod question, find-holes-informed, n ≥ 2** with the in-code footer → `response`
  non-empty, `write_file=0`. (Primary: the bug being fixed.)
- **gemini flash, verdict-informed, n ≥ 1** → `response` non-empty AND the model's final line is a
  `VERDICT:` token (the anchor survived the no-write clause sitting before it).
- **gemini preview, prod question, n = 1** → confirm model-independence (best-effort; preview is
  slow/non-deterministic, so one clean run is the bar, not a guarantee).
- **codex + grok, informed, n = 1 each** → both still return a normal critique with the no-write
  clause present (verifies the "benign for codex/grok" assertion rather than asserting it).

This guards against (a) the footer position being less effective than the test-only suffix and
(b) the no-write clause perturbing codex/grok or the verdict anchor.

## Docs

- `skills/consult/SKILL.md` — one short caveat in the Panel section: on large informed questions
  gemini may try to write findings to a file; the informed prompt now instructs text-only output.
- `CLAUDE.md` (the bulldozer plugin's CLAUDE.md — at the worktree/plugin root; it is
  `plugins/bulldozer/CLAUDE.md` only when viewed from the full jaine-plugins repo) — one line
  under the consult panel notes pointing at this spec.

## Non-goals (YAGNI)

- **No retry-on-empty.** The prompt fix removes the cause (3/3). Retry adds new infrastructure
  (none exists today), doubles worst-case gemini time, and the panel already degrades gracefully
  to surviving models. Rejected per scope decision.
- **No anti-write in isolated mode.** The bug is informed-only; isolated has no repo and a
  text-only wrapper.
- **No model pinning.** `flash` emptied just like `preview` — the model is not the variable, so
  pinning a "stable" model fixes nothing.

## Acceptance criteria

1. Informed wrapped prompts (both find-holes and verdict) contain the no-write clause; isolated do not.
2. The verdict-informed wrapped prompt **still ends with `_VERDICT_TAIL`** (anchor contract intact).
3. A present-but-empty gemini/grok `response`/`text` yields a failure reason containing `empty
   response`, never `unparseable output`; the reason is model-neutral (no "written to a file").
4. All existing `test_consult_panel.py` tests pass; new tests added per the Testing section.
5. Full offline suite green (modulo the 2 pre-existing `marketplace.json` env-fails in the look domain).
6. Live bench-test: gemini flash find-holes-informed n≥2 → non-empty `response`, `write_file=0`;
   verdict-informed n≥1 → non-empty + final `VERDICT:` line; codex+grok informed n≥1 → normal critique.

## Panel review (consult --panel --repo, 2026-06-03)

This spec was reviewed by codex + grok (gemini failed — the very bug, reproduced live). Zero false
positives; one critical finding (blind append breaks the verdict anchor) and five valid refinements
were folded in: per-cell placement, model-neutral reason, corrected call-site claim, verdict
anchor regression test, and a bench-test that exercises the real footer position + codex/grok.

**`/bulldozer:check` standard (codex/gpt-5.5), Round 1 → NO-GO, 2 findings, 0 FP, both fixed:**
- R1-F1 (medium): three-way `_parse_json_field` did not define multi-candidate ordering for the
  `""` sentinel → added explicit "last non-empty wins; `""` only if present-but-all-empty; `None`
  if never present" rule with reference code.
- R1-F2 (low): Docs path `plugins/bulldozer/CLAUDE.md` is wrong in the worktree (root = the plugin)
  → corrected to `CLAUDE.md`.
- E1 pre-clean: 3 cross_spec_drift declined (future-spec vs current code, by-design); 1 tense fix.

**Round 2 → NO-GO, R1-F1 + R1-F2 both verified-fixed; 1 new, 0 FP, fixed:**
- R2-F1 (medium): the multi-candidate rule added for R1-F1 was defined in Fix A but the Testing
  section only pinned single-object cases → added test 2b (empty-last / empty-first /
  present-but-all-empty, for both gemini and grok) so an implementation can't pass while
  regressing the banner+payload stream. Fresh review otherwise clean (Fix B placement, sentinel
  flow, parse_grok symmetry all confirmed feasible against real code).

**Round 3 → GO** (`source: empty_findings`). R2-F1 verified-fixed; fresh review found nothing new.
Trajectory 2 → 1 → 0, **0 false positives across all 3 rounds**. Spec is implementation-ready.

*Version: 1.3.0 | Last Updated: 2026-06-03*
