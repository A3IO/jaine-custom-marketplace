---
name: check
description: "Adversarial review of specs, designs, configs, or code via external AI reviewer (Codex CLI) against artifacts on disk. Triggers on \"review this spec\", \"adversarial review\", \"check this design\", \"second opinion on file\", \"проверь спеку\", \"ревью артефакта\", \"bulldozer check\". Do NOT use for inline conversational design questions without an artifact on disk — use bulldozer:consult instead. Do NOT use for quick questions, trivial edits, or code with existing test coverage."
argument-hint: "[quick|standard|exhaustive] [file|dir|diff]"
allowed-tools: ["Bash", "Read", "Edit", "Write", "AskUserQuestion", "Task"]
---

# Adversarial Review Loop

**Core principle:** Send artifact to an external reviewer, verify each finding empirically, fix confirmed issues, re-send — repeat until GO.

## When to Use

- Spec/design docs before implementation (will scripts be built from this?)
- Config changes before deployment (will this break production?)
- Documentation claims before publishing (are facts verified?)
- Any artifact where "wrong = costly" and a second opinion helps

**Do NOT use for:** Quick questions, trivial edits, code that has tests covering it, free-form prompts without a concrete artifact.

## Step 1: Model Selection (EVERY launch)

Before any other action, discover available models and ask the user which one to use.

**1a. Get available models** — run:
```bash
if ! codex_output=$(codex debug models 2>&1); then
    echo "ERROR: 'codex debug models' failed. Check: codex installed? logged in? (codex login)" >&2
    exit 1
fi
printf '%s' "$codex_output" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    print('ERROR: codex returned non-JSON output', file=sys.stderr)
    sys.exit(1)
models = data.get('models', [])
if not models:
    print('ERROR: empty model catalog', file=sys.stderr)
    sys.exit(1)
listed = [m for m in models if m.get('visibility') == 'list']
if not listed:
    print(f'ERROR: no models with visibility=list ({len(models)} models found, schema may have changed)', file=sys.stderr)
    sys.exit(1)
for m in listed:
    slug = m.get('slug')
    if not slug: continue
    name = m.get('display_name', slug)
    print(f'{slug}|{name}|{m.get(\"priority\", 999)}')
"
```

If the snippet exits non-zero, tell the user to check `codex --version` and `codex login`, then ask them to type a model name manually.

**1b. Read saved preference** — if `.bulldozer/config.md` exists, read `reviewer_model` from its YAML frontmatter. If the file is malformed or missing the key, warn the user ("`.bulldozer/config.md` unreadable — ignoring saved preference") and let them pick fresh. Mark the saved model as "(Recommended)" in options.

**1c. Ask user** — via AskUserQuestion, show 4 models. Selection rules (in order):
1. **ALWAYS** include current global model from `~/.codex/config.toml` (line 1: `model = "..."`)
2. **ALWAYS** include last used model from `.bulldozer/config.md` (if different from global)
3. Fill remaining slots from: gpt-5.5, gpt-5.3-codex-spark, gpt-5.4-mini (skip gpt-5.4 and gpt-5.3-codex — redundant)
4. If global = last used, you get 3 slots for the above

This guarantees the user's configured model is never hidden by priority sorting.

**1d. Save choice** — update ONLY `reviewer_model` in `.bulldozer/config.md`, preserving any other keys. Pass to codex exec via `-m <model>`.

## Step 2: Parse Arguments

If no arguments provided, explain to the user:

> **Bulldozer** sends ваш артефакт (спеку, код, конфиг) на ревью внешнему AI-рецензенту (Codex CLI), затем каждый finding проверяется эмпирически в коде, фиксится, и отправляется на повторное ревью — и так до вердикта GO.
>
> **Использование:**
> ```
> /bulldozer:check path/to/spec.md              — standard (до 3 раундов)
> /bulldozer:check quick path/to/config.json     — один раунд, только блокеры
> /bulldozer:check exhaustive docs/design.md     — до полного GO (макс 10 раундов)
> /bulldozer:check standard src/gateway/         — ревью директории
> ```
>
> **Уровни глубины:**
> - `quick` — один раунд, только критичные проблемы
> - `standard` — до 3 раундов, баланс глубины и скорости (по умолчанию)
> - `exhaustive` — крутится пока рецензент не скажет GO (для спек, управляющих автоматизацией)

Then ask: what artifact to review, and which depth level.

If arguments provided, parse depth and artifact from $ARGUMENTS:
- First word matching `quick|standard|exhaustive` → depth (default: `standard`)
- Remaining → artifact path (file or directory)
- If only depth given, ask for artifact
- If only path given, use `standard` depth

## Artifact Types

The artifact must be something codex can read from the filesystem and something you can fix between rounds:

| Type | Example | Review dir name |
|------|---------|-----------------|
| File | `docs/specs/auth-design.md` | `{session}-auth-design` |
| Directory | `src/gateway/` | `{session}-gateway` |
| Git diff | current branch changes | `{session}-diff-{branch}` |

Free-form prompts without a file/dir/diff are NOT supported — the iterative fix→re-review loop requires a concrete artifact.

## Depth Levels and Codex Configuration

| Level | Max rounds | Reasoning | Prompt prefix | When |
|-------|-----------|-----------|---------------|------|
| `quick` | 1 | `-c model_reasoning_effort=medium --ephemeral` | `SKIP SKILLS.` | Sanity check, low stakes |
| `standard` | 3 | `-c model_reasoning_effort=xhigh` | (none) | Normal work, moderate stakes |
| `exhaustive` | until GO (cap 10) | `-c model_reasoning_effort=xhigh` | (none) | High stakes, spec drives automation |

> **Canonical source:** the wrapper reads these depth parameters from `skills/check/data/depth-config.json` (single source of truth, B1 / #110). This table mirrors that file — `TestDepthConfigContract` fails if they drift.

Default: `standard`. Override via argument: `/bulldozer:check exhaustive`

```dot
digraph review_loop {
  rankdir=TB;
  "Setup review dir + build prompt" -> "bulldozer-round.sh (codex, parse, log-round, trajectory, pivot)";
  "bulldozer-round.sh (codex, parse, log-round, trajectory, pivot)" -> "Branch on wrapper exit code";
  "Branch on wrapper exit code" -> "Verify findings empirically" [label="0"];
  "Branch on wrapper exit code" -> "Manual extraction (read verdict, replace-extraction)" [label="11"];
  "Branch on wrapper exit code" -> "STOP — inspect and fix invocation" [label="2/3/4/5/64/70/71"];
  "Branch on wrapper exit code" -> "AskUser pivot (continue / restructure / accept-with-TODO)" [label="10"];
  "Manual extraction (read verdict, replace-extraction)" -> "Verify findings empirically";
  "Verify findings empirically" -> "Real or false positive?";
  "Real or false positive?" -> "Fix confirmed issues" [label="real"];
  "Real or false positive?" -> "Note false positive" [label="false"];
  "Fix confirmed issues" -> "Apply findings to ledger";
  "Note false positive" -> "Apply findings to ledger";
  "Apply findings to ledger" -> "GO verdict?";
  "GO verdict?" -> "Done — write summary" [label="yes"];
  "GO verdict?" -> "Build Round N prompt" [label="no, round < max"];
  "Build Round N prompt" -> "bulldozer-round.sh (codex, parse, log-round, trajectory, pivot)";
  "AskUser pivot (continue / restructure / accept-with-TODO)" -> "Build Round N prompt" [label="continue"];
  "AskUser pivot (continue / restructure / accept-with-TODO)" -> "Done — user pivoted" [label="restructure / accept"];
}
```

### Step-by-step

**1. Setup** — create per-review directory using session ID + artifact name:
```bash
SESSION="${CLAUDE_CODE_SESSION_ID:0:8}"
ARTIFACT_NAME=$(basename "$ARTIFACT_PATH" .md)  # or dirname, or branch name for diff
REVIEW_DIR=".bulldozer/${SESSION}-${ARTIFACT_NAME}"
mkdir -p "$REVIEW_DIR"
```

Each review gets its own isolated directory — no collisions between sessions or artifacts.

**1b. Resolve project root** — before anything else:
```bash
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "ERROR: not in a git repository. /bulldozer:check requires git context." >&2
    exit 1
}
```
If this fails, STOP the review — do not proceed with empty `$PROJECT_ROOT`.
Use `$PROJECT_ROOT` in all `-C` flags and paths below.

**1c. Self-ignoring `.bulldozer/`** — drop a single-line `.gitignore` inside `.bulldozer/` so the directory hides its own contents from git, without touching the consumer's project-level `.gitignore`. Same pattern as `.remember/`. Idempotent. Path is cwd-relative for parity with `REVIEW_DIR` (Step 1) and the downstream scripts (`log-round.sh`, `update-state.py`) that all assume `cwd == $PROJECT_ROOT` when the skill runs.
```bash
mkdir -p .bulldozer
if [[ ! -f .bulldozer/.gitignore ]]; then
    if ! echo '*' > .bulldozer/.gitignore 2>/dev/null; then
        echo "WARNING: could not write .bulldozer/.gitignore — check permissions on $(pwd)/.bulldozer. Re-run after fixing." >&2
    fi
fi
```

**1.7. Pre-review consistency audit (E1, doc rounds only)** — for a
**doc/spec artifact** (a `.md`/`.mdx`/`.rst` file, a `docs`/`specs` directory,
or a diff touching doc files — skip for pure code), run this BEFORE building the
round prompt, every round:

1. **Locate.** Read `audit_model` from `.bulldozer/config.md` frontmatter (default
   `sonnet`). Preferred: dispatch the read-only auditor
   `Task(subagent_type: "bulldozer:consistency-auditor", model: <audit_model>)`.
   **If that Task errors `agent type ... not found`** — plugin agents register only at
   session-start / `/reload-plugins`, NOT from source the way skills do, so a
   just-shipped auditor stays unregistered until the consumer reloads — do the
   four-class locate **inline yourself** instead: read the artifact (and its sibling
   specs) and produce the SAME `{id, class, file, quote, anchor}` envelope, copying
   every quote verbatim. (Inline is equivalent for correctness — the verifier below
   drops hallucinated quotes regardless of who located them; the subagent is only a
   cost-isolation optimization.) Either way, YOU write the envelope to
   `${REVIEW_DIR}/e1-findings-r${ROUND}.json` (the agent is read-only — it cannot write
   the file).
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

**2. Build the round prompt** — pick the right template from the Reviewer Prompt Templates section below, substitute the artifact-specific placeholders (`<PATH>`, `<TYPE>`, `<PURPOSE>`, `<DEPTH>`, etc.), and write it to a file the wrapper can read:

```bash
PROMPT_FILE="${REVIEW_DIR}/prompt-r${ROUND}.txt"
# Write the round prompt body to $PROMPT_FILE — Round 1 quick/standard or
# Round N (continuation with ledger). See "Reviewer Prompt Templates" below.
```

For Round N continuation prompts, embed the current review-ledger.yml as APPENDIX A and the previous verdict-r{N-1}.txt as APPENDIX B (templates already include the headers).

**3. Run the round** — one call composes codex → parser → log-round → trajectory → pivot signal:

```bash
"${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/bulldozer-round.sh" \
  --round "$ROUND" \
  --review-dir "$REVIEW_DIR" \
  --artifact "$ARTIFACT" \
  --depth "$DEPTH" \
  --reviewer "codex/$MODEL" \
  --prompt-file "$PROMPT_FILE" \
  --project-root "$PROJECT_ROOT"
wrapper_exit=$?
```

The wrapper runs codex FOREGROUND with the right `-s read-only -m -o -C` flags + depth-specific reasoning effort, invokes `parse-ledger-patch.py` on the resulting `verdict-r${ROUND}.txt`, calls `log-round.sh` (which updates `state.json` and appends to `bulldozer.log`), prints the trajectory to stderr when `ROUND >= 2`, and on a pivot trigger — the flat `ROUND >= max_rounds && verdict != GO`, or the B6 calibrated exhaustive early-pivot (#128: `depth == exhaustive && ROUND >= 5 && verdict != GO && mean-last-3 findings >= 3.0`) — writes `pivot-r${ROUND}.json` with AskUserQuestion options and exits 10.

stdout carries the final `state.json` contents so you can read trajectory or open findings without re-reading the file.

**Wrapper environment variables (E1, #110).** The wrapper sets two vars in the `log-round.sh` child-process scope ONLY — they do not leak back to the caller's environment:
- `BULLDOZER_REVIEW_DIR="$REVIEW_DIR"` — pins where `log-round.sh` / `update-state.py` write `state.json` (the per-review sandbox), overriding their cwd-relative `.bulldozer/` default. Set fresh per round.
- `BULLDOZER_DEPTH="$DEPTH"` — records the depth in the round's `state.json` / `bulldozer.log` line.

The inverse pair `BULLDOZER_FIXED` / `BULLDOZER_FP` (Step 6) is set by YOU in the wrapper's env to record per-round fixed / false-positive counts; unset them after the round so they don't leak into the next.

**Wrapper exit codes — branch explicitly. Do NOT silently retry on non-zero exit.**

Codes are partitioned by origin so the caller can route mechanically without parsing stderr: `2-5` = parser outcomes, `10` = pivot, `11` = manual-extraction (wrapper-converted from internal parser exit 1), `64/70/71` = wrapper-side failures (sysexits.h convention). Reserved codes never overlap across origins — if you see `11`, the wrapper converted parser's internal "no LEDGER_PATCH" signal; if you see `64`, the wrapper rejected the call; if `71`, codex itself crashed. Raw wrapper exit 1 should never surface post-PR-1 — if it does, report as plugin bug.

| Exit | Origin | Meaning | Your action |
|------|--------|---------|-------------|
| `0` | wrapper | Round logged successfully | Continue to Step 4 (verify findings) |
| `1` | parser | (internal) No LEDGER_PATCH block — wrapper intercepts and converts to exit 11. Callers should never observe this exit directly. | If somehow surfaced (wrapper bug?), report to plugin maintainer. |
| `2` | parser | Malformed YAML in LEDGER_PATCH | STOP. Inspect `${REVIEW_DIR}/verdict-r${ROUND}.malformed.yml` (parser saved the raw block); ask user how to proceed (fix template, retry, or pivot) |
| `3` | parser | Schema violation in LEDGER_PATCH | STOP. Patch is structurally wrong; do NOT apply. Ask user how to proceed |
| `4` | parser | PyYAML not installed | Tell user the install command (printed by wrapper) and retry |
| `5` | parser/wrapper | Verdict file empty, missing, or unreadable | Usually transient. Check `${REVIEW_DIR}/full-r${ROUND}.txt` for codex output, then retry the round |
| `10` | wrapper | Max rounds reached without GO | Read `${REVIEW_DIR}/pivot-r${ROUND}.json` and wrap its `options` array in `AskUserQuestion` (continue / restructure / accept-with-TODO). Act on the user's choice |
| `11` | wrapper | No LEDGER_PATCH block in verdict — round logged with `verdict="UNKNOWN"` + `manual_extraction_pending=true`; caller must extract findings from prose and reconcile | Read `${REVIEW_DIR}/verdict-r${ROUND}.txt`, count findings (K) and determine VERDICT (GO/NO-GO) from the prose. Then call: `python3 <plugin>/skills/check/scripts/update-state.py --review-dir "${REVIEW_DIR}" --mode=replace-extraction ${ROUND} ${K} ${VERDICT}` (`update-state.py` is NOT on PATH — invoke via `python3` + full script path; the wrapper's exit-11 stderr prints the exact command with both the script path and `--review-dir` already shell-escaped — copy it verbatim). See Step 7 "manual-extraction branch" for the full flow including terminal-round pivot dispatch. |
| `64` | wrapper | Preflight / usage error (bad flag, missing flag, bad reviewer format, missing prompt file, invalid depth, non-numeric BULLDOZER_FIXED/FP) | Fix the invocation. Diagnostic on stderr names the offending input. Do NOT retry without correcting the caller — this is a contract violation, not a transient failure |
| `70` | wrapper | Wrapper-internal failure (parser/log-round script not at expected path, log-round.sh failed during execution) | Check stderr diagnostic — typically a stale `CLAUDE_PLUGIN_ROOT` (run `jaine-sync plugins update bulldozer`) or a corrupted `state.json` in the review dir |
| `71` | wrapper | codex exec crashed | Diagnostic on wrapper stderr names the original codex exit code (preserved) and the path to `full-r${ROUND}.txt`. Report to user — do not silently retry. Common codex exits: 1 (auth expired → `codex login`), other (network, rate limit) |

**Pivot signal channels (E2, #110).** At `ROUND >= max_rounds && verdict != GO` the wrapper signals a pivot on FOUR channels — the exit code alone is necessary but not sufficient:
1. **exit 10** — the routing signal (branch on it).
2. **stderr** — a human-readable `PIVOT: ...` marker line.
3. **`${REVIEW_DIR}/pivot-r${ROUND}.json`** — the load-bearing payload: the `options` array you MUST read and wrap in `AskUserQuestion`.
4. **stdout** — the final `state.json` (trajectory context).

The exit-10 row tells you to act; channel 3 carries the actual options. (The exit-11 manual-extraction path has NO auto-written pivot file at a terminal OR calibrated pivot — Step 7 step 5 builds the `AskUserQuestion` payload inline instead.)

**Extending parser exit codes (E3, #110).** The parser exit-code contract has a single source of truth: the `Exit codes:` docstring in `parse-ledger-patch.py`. Adding a code (6+) requires THREE synced edits:
1. **Parser docstring** (SSOT) — define the code and its meaning.
2. **Wrapper** `_emit_parser_exit_diagnostic` case branch (`bulldozer-round.sh`) — map it to an `_emit_stop` diagnostic (control-flow codes 0/1 stay in the main parser `case`).
3. **The exit-code table above** — document the caller's action.

`TestParserExitContract` (`tests/test_check_round_wrapper.py`) is the drift guard: `test_every_diagnostic_code_has_wrapper_emit_stop` fails if a docstring-listed code (other than 0/1) has no `_emit_stop N` in the wrapper, and `test_documented_codes_are_expected_set` pins the current set `{0,1,2,3,4,5}`.

Schema example codex emits (Round 1 standard / exhaustive — see "LEDGER_PATCH Protocol" below for the full schema):

```yaml
LEDGER_PATCH:
  findings:
    - id: R1-F1
      severity: high
      status: open
      title: "side effect before permission check"
      files: [{path: "src/a.py", lines: "120-148"}]
      original_verdict_excerpt: |
        The ACL check runs after the write...
      required_recheck:
        instructions: "Verify permission check happens before write"
        commands: ["grep -n 'check_acl' src/a.py"]
```

**4. Verify each finding** — use `/receiving-code-review` discipline. Read `${REVIEW_DIR}/parsed-r${ROUND}.json` (the wrapper wrote it; one entry per finding with `id`, `severity`, `files`, `original_verdict_excerpt`, `required_recheck.commands`). For each finding:

- Run the `required_recheck.commands` (or the closest equivalent) against the current code
- Classify: REAL or FALSE_POSITIVE
- Record evidence

**CRITICAL: do not blindly fix reviewer findings. Verify first.**

| Reviewer says | You do |
|---------------|--------|
| "File X doesn't exist" | `ls` / `git ls-tree` to check |
| "Query returns wrong count" | Run the exact query |
| "Pattern matches false positives" | Test the regex on real data |
| "Contradicts line N" | Read both lines, compare |

**5. Apply findings to the ledger** — append each verified finding from `parsed-r${ROUND}.json` to `${REVIEW_DIR}/review-ledger.yml`, mark status (`verified` / `still_open` / `false_positive` / `wontfix`) based on Step 4's evidence. JSON→YAML transcription is a Claude task (extraction is deterministic via the wrapper; ledger curation is judgment).

**6. Fix confirmed issues** — edit the artifact, commit with finding counts:

```
docs: artifact-name vN+1 (Mth review, K findings fixed)
```

If you want the next round's `log-round` line + `state.json` totals to record per-round fixed/false-positive counts (instead of the default 0/0), set the env vars BEFORE the next Step 3 wrapper invocation:

```bash
BULLDOZER_FIXED=K BULLDOZER_FP=M "${CLAUDE_PLUGIN_ROOT}/skills/check/scripts/bulldozer-round.sh" \
  --round "$((ROUND + 1))" ...
```

Unset them after the round (`unset BULLDOZER_FIXED BULLDOZER_FP`) so they don't leak into the round-after-next.

**7. Loop or stop — branch on the EXIT CODE first, not the round number** (B6, #128: a calibrated pivot can exit 10 at exhaustive round 5-9 even though `round < max_rounds`, so "round < max" no longer implies "keep going"):
- Verdict GO (wrapper exit 0, parsed-rN.json has `findings: []`) → done, write summary
- Wrapper exit 0 (round logged, NO pivot) AND verdict NO-GO AND `round < max_rounds` → build Round N+1 prompt from ledger, go to Step 2. This continuation applies ONLY on exit 0 — a pivot (exit 10) takes precedence.
- Wrapper exited 10 (pivot signal) → act on the user's AskUserQuestion choice from Step 3. Fires on EITHER the flat `ROUND >= max_rounds && verdict != GO` trigger OR the B6 calibrated trigger (`depth == exhaustive` AND `ROUND >= 5` AND NO-GO AND avg-last-3 findings `>= 3.0`), so exit 10 can occur at `round < max_rounds` on exhaustive runs. The wrapper always writes `pivot-rN.json` before exiting 10; if exit 10 ever arrives without a readable pivot file, treat it as a wrapper-state bug and report it.
- **Wrapper exited 11 (manual-extraction branch) — REQUIRED PROTOCOL:**
  1. Read `${REVIEW_DIR}/verdict-r${ROUND}.txt` — reviewer wrote prose but skipped the structured LEDGER_PATCH block
  2. Extract findings from prose: count `K` (number of real findings) and determine `VERDICT` (GO if no real findings; NO-GO if K > 0 OR reviewer narrated problems without enumerating cleanly)
  3. Append the extracted findings to `${REVIEW_DIR}/review-ledger.yml` with status `open` (use IDs `R${ROUND}-F${M}` matching wrapper convention)
  4. Reconcile state: `python3 <plugin>/skills/check/scripts/update-state.py --review-dir "${REVIEW_DIR}" --mode=replace-extraction ${ROUND} ${K} ${VERDICT}` — this updates `history[round=${ROUND}].findings`, `verdict`, and clears `manual_extraction_pending`; deltas `findings_total` correctly (`update-state.py` is NOT on PATH — invoke via `python3` + full script path). **IMPORTANT:** use the ABSOLUTE path the wrapper prints in the exit-11 stderr recovery command — it appears as the `--review-dir <path>` argument, with `REVIEW_DIR` already canonicalized to absolute (the wrapper runs `REVIEW_DIR="$(cd "$REVIEW_DIR" && pwd)"` before any diagnostic is emitted), NOT a relative path inferred from your Bash tool's current cwd. Claude's Bash tool invocations may run with different cwd across messages; the absolute path from stderr survives. `update-state.py` canonicalizes via `.resolve()` defensively, but the safest contract is to copy the absolute `--review-dir` value the wrapper printed.
  5. **Pivot check (REQUIRED):** the manual-extraction path exits 11 BEFORE the wrapper's Step 9, so the wrapper's pivot triggers never run — the caller MUST replicate BOTH here. Fire the AskUserQuestion pivot dialog if EITHER trigger holds:
      - **Terminal:** `ROUND >= max_rounds` AND `VERDICT == NO-GO` (use `>=`, not `==`, to mirror the wrapper's flat pivot — a user-continued over-max manual round, e.g. standard round 4, must still pivot); or
      - **Calibrated (B6, #128):** `depth == exhaustive` AND `ROUND >= 5` AND `VERDICT == NO-GO` AND the mean of the last 3 `history[].findings` (read from `state.json` after step 4's reconcile) `>= 3.0`. This is the manual-path mirror of the wrapper's calibrated early-pivot — without it, an exhaustive round 5-9 manually reconciled to NO-GO would silently continue, bypassing B6.

      The manual-extraction path does NOT have the wrapper write `pivot-rN.json` automatically — Claude constructs the payload inline using this template (matches the wrapper exit-10 sidecar shape). When the **calibrated** trigger is the one that fired, swap the question for the "not converging by round N" wording (mirroring emit-pivot.py's `calibrated_nonconvergence` text) instead of the "reached max rounds" wording below:

      ```python
      AskUserQuestion(questions=[{
          "question": f"Reached max rounds ({max_rounds}) without GO — {K} finding(s) open. How to proceed?",
          "header": "Pivot",  # ≤12 chars per AskUserQuestion schema
          "multiSelect": False,
          "options": [
              {"label": "continue",         "description": "Run another round (exceeds max for this depth)"},
              {"label": "restructure",      "description": "Pause review, restructure the artifact, re-launch /bulldozer:check"},
              {"label": "accept-with-TODO", "description": "Accept current state, log open findings as project TODOs"},
          ],
      }])
      ```

      Manual-extraction MUST NOT silently exit without this pivot dialog when EITHER trigger holds (parity with the non-manual exit-10 + calibrated early-pivot flows).
  6. Continue — **pivot precedence first** (mirrors Step 7's exit-code rule): if a pivot dialog fired in step 5 → act on the user's choice; else if `VERDICT == GO` → done; else if `ROUND < max_rounds` → build Round N+1 prompt and go to Step 2.

  > **Why this protocol exists:** Issue #110 (B5) — pre-PR-1, wrapper exit 1 silently lost the round (no state.json, no bulldozer.log) and handed control to Claude with zero discipline. The exit 11 + replace-extraction pair restores the discipline invariant (every round writes state.json + bulldozer.log) while preserving the human-readable prose-extraction path for reviewers that skip LEDGER_PATCH.

## Reviewer Prompt Templates

### Round 1 — quick

```
Before reviewing, read CLAUDE.md at the project root (and any sub-CLAUDE.md
in the artifact's directory). Apply project conventions when classifying
findings as material vs. defensive.

You are reviewing <PATH>.
This is a <TYPE> that will be used for <PURPOSE>.
Find correctness bugs, regressions, security risks, missing tests. Ignore style.
Keep each finding under 180 words.

End with the LEDGER_PATCH block — see LEDGER_PATCH Protocol below.
```

### Round 1 — standard / exhaustive

**CRITICAL: Adapt the prompt to the artifact type.** A design spec for a FUTURE feature must NOT be checked for "do these files exist" — they don't exist yet. Check internal consistency, feasibility, and completeness instead.

```
Before reviewing, read CLAUDE.md at the project root (and any sub-CLAUDE.md
in the artifact's directory). Apply project conventions when classifying
findings as material vs. defensive.

You are performing a <DEPTH> code review of <PATH>.
This is a <TYPE> that will be used for <PURPOSE>.

IMPORTANT: If this is an implementation plan or design spec for a FUTURE feature,
do NOT check whether described files/functions exist yet — they will be created.
Instead verify: internal consistency, feasibility, edge cases, missing requirements,
and whether the spec gives enough detail to implement correctly.

Read the relevant implementation, tests, configs, and docs before judging.
Prioritize behavioral bugs, regressions, data loss, security, concurrency, API incompatibility, and test gaps.

For every finding output:
- ID: R1-FN
- Severity: blocker|high|medium|low|info
- File/lines
- Problem
- Impact
- Required fix
- Required recheck (exact commands)
- Evidence

Do not pad. Do not include style-only comments.

End with the LEDGER_PATCH block — see LEDGER_PATCH Protocol below.
```

### Round N (continuation with ledger)

```
This is review round <N> of <PATH>.

Before reviewing, read CLAUDE.md at the project root (and any sub-CLAUDE.md
in the artifact's directory). Apply project conventions when classifying
findings as material vs. defensive.

Do BOTH:
1. Fresh review of current HEAD as if no previous review existed.
2. Ledger recheck of all non-terminal findings from previous rounds.

APPENDIX A — review-ledger.yml:
<FULL LEDGER CONTENT>

APPENDIX B — previous verdict:
<FULL verdict-r{N-1}.txt CONTENT>

For each open/fixed finding, decide: verified, still_open, false_positive, or wontfix.
If a claimed-fixed issue still reproduces, keep the original ID and explain why.
New findings use IDs R{N}-FN.
End with the LEDGER_PATCH block covering both recheck results and new findings — see LEDGER_PATCH Protocol below.
GO only when all material findings are terminal AND fresh review found nothing new.
```

### LEDGER_PATCH Protocol

Single source of truth for the LEDGER_PATCH block referenced by all three round templates above. Future changes to the directive go here, not into individual templates (drift between Round-1 standard and Round-N was the regression that #104 caught and PR #106 hot-patched).

Every round MUST end with a LEDGER_PATCH YAML block — REQUIRED for both NO-GO and GO. The wrapper's parser extracts findings deterministically from this block; a reviewer that skips it forces the consumer back to manual prose extraction (the discipline failure PR1a / #101 was meant to eliminate).

**NO-GO shape (one or more findings):**
```yaml
LEDGER_PATCH:
  findings:
    - id: R{N}-F{M}             # round-prefixed: R1-F1, R1-F2, R2-F1, ...
      severity: blocker|high|medium|low|info
      status: open               # status lifecycle managed by consumer
      title: "short description"
      files: [{path: "...", lines: "..."}]
      original_verdict_excerpt: "your finding text verbatim"
      required_recheck:
        instructions: "what to verify"
        commands: ["command1", "command2"]
```

**GO shape (REQUIRED — do NOT emit a bare "GO" line):**
```yaml
LEDGER_PATCH:
  verdict: go
  findings: []
```

A bare `GO` line (without the LEDGER_PATCH block) is auto-synthesized by the parser as `{verdict: go, findings: []}` with `source: synthesized_bare_go` and a warning — it still works, the parser exits 0. The synthesis is suppressed if any `NO-GO` variant also appears in the verdict (exit 1 wins so real findings aren't lost). Still: prefer the explicit structured block above. Synthesis is a graceful fallback, not a green light to skip the protocol — `source: synthesized_bare_go` is a code smell in audit logs, and any time a reviewer needed to write `GO` AND a `NO-GO` example (e.g. inline documentation), synthesis flips off and the consumer ends up in manual extraction anyway.

## Review Ledger Format

`review-ledger.yml` — cumulative, append-only. Findings never deleted, only status changes.

```yaml
schema: review-ledger/v1
artifact: "path/to/artifact"
depth: standard
model: gpt-5.5
rounds:
  - round: 1
    date: "2026-05-12"
    result: no-go          # go | no-go | crash
    verdict_file: "verdict-r1.txt"
  - round: 2
    date: "2026-05-12"
    result: go
    verdict_file: "verdict-r2.txt"
  # crash example:
  # - round: 3
  #   date: "2026-05-12"
  #   result: crash
  #   verdict_file: null
  #   error: "codex exit 1 — auth expired"
findings:
  - id: R1-F1
    severity: high
    status: verified  # open → fixed → verified
    introduced_round: 1
    last_seen_round: 2
    title: "side effect before permission check"
    files:
      - path: "src/a.py"
        lines: "120-148"
    original_verdict_excerpt: |
      The ACL check runs after the write...
    required_recheck:
      instructions: "Verify permission check happens before write"
      commands:
        - "grep -n 'check_acl' src/a.py"
    history:
      - round: 1
        status: open
        note: "Reported"
      - round: 2
        status: verified
        note: "Fix confirmed — check_acl moved before write_data"
```

**Status lifecycle:** `open` → `fixed` (user claims) → `verified` / `still_open` / `false_positive` / `wontfix`

## Review Directory Layout

```
.bulldozer/                                    # .gitignore inside ('*') hides this dir from git — no project-level entry needed
  .gitignore                                  # one line: *
  bf5a38d6-auth-design/                        # session prefix + artifact
    review-ledger.yml                          # cumulative ledger (managed by Claude)
    verdict-r1.txt                             # clean codex answer round 1
    verdict-r2.txt                             # clean codex answer round 2
    full-r1.txt                                # full codex output (debug only)
    full-r2.txt
    state.json                                 # round state (managed by scripts)
```

## Error Handling

| Situation | Action |
|-----------|--------|
| `verdict-r{N}.txt` is empty or missing | Mark round as `crash` in ledger. Check `full-r{N}.txt` for errors. Rerun same round number. Max 2 retries — if both fail, stop and report "codex produced no output twice; check `codex --version`, `codex login`, network, disk space". |
| Both verdict and `full-r{N}.txt` empty | Codex didn't start or crashed immediately. Check PATH, auth (`codex login`), disk space. Do NOT retry blindly. |
| GO on round 1 with zero findings | Red flag — likely didn't read the file. For standard/exhaustive: require second pass. For quick: accept if user explicitly chose quick. |
| Same finding reappears after "fixed" | Keep original ID, set `status: still_open`, append history note "fix insufficient". |
| 10 rounds without GO (exhaustive) | Stop automatic rounds. Produce escalation report grouped by root cause. |
| Codex timeout / network error | Retry once. If second failure, report specific exit code and last 20 lines of `full-r{N}.txt`. |
| Codex auth expired | `< /dev/null` blocks re-auth prompts. Tell user to run `codex login`, then retry. |

## Logging

**Deterministic log file:** `~/.claude/hooks/bulldozer.log`

Every round appends one line (append-only, never truncated):
```
2026-05-09T10:30:00+03:00 | session=bf5a38d6 | round=1 | artifact=docs/specs/auth.md | verdict=NO-GO | findings=8 | fixed=7 | fp=1 | reviewer=codex/gpt-5.5 | project=/path/to/repo
```

To review history: `column -t -s'|' ~/.claude/hooks/bulldozer.log`

## Configuration

Optional `.bulldozer/config.md` in project root:

```yaml
---
reviewer_model: gpt-5.5
audit_model: sonnet
---
```

`reviewer_model` is updated by the model selection prompt on each launch. Save updates ONLY `reviewer_model`, preserving all other keys if present.

`audit_model` (optional, default `sonnet`) — the model for the E1 consistency-auditor
subagent (Step 1.7). Flip to `haiku` for lower cost (lower contradiction-catch rate —
see the design spec §2 split test).

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Parsing `full-r{N}.txt` for verdict | Use `-o verdict-rN.txt` — clean answer, zero parsing |
| Trusting reviewer blindly | Verify EVERY finding with grep/read/run before fixing |
| Not using `-C` flag | Codex may run from wrong directory → false NO-GO |
| Not using `< /dev/null` | Codex may hang waiting for stdin |
| Fixing style issues | Tell reviewer "BLOCKERS only" — style is noise |
| Stopping after round 1 | Round 1 typically finds 30-50% of issues; iterate |
| Not committing between rounds | Reviewer needs to see the updated file |
| Losing state on compaction | State is in review dir and ledger, not conversation memory |
| Not calling log-round.sh every round | State becomes incomplete — call EVERY round |
| Claude summarizing findings in prose for next round | Use ledger + full previous verdict as appendix — don't lose nuance |
| Telling reviewer to "verify files exist" for a design spec | Spec describes FUTURE state — check consistency and feasibility, not filesystem |
| Modifying the consumer's project `.gitignore` | Step 1c writes a self-ignoring `.bulldozer/.gitignore` instead — no project-level changes |

## Red Flags — STOP and Reassess

- Reviewer gives GO on round 1 with zero findings (likely didn't read the file — check cwd)
- Same finding reappears after you "fixed" it (your fix was wrong — re-verify)
- Round > 5 with new HIGH findings each time — the wrapper prints the trajectory on stderr after every round ≥ 2 (`[bulldozer/check] Round N/M ... Trajectory: A → B → C  (avg last 3: X.X)`). If findings aren't shrinking, the AskUserQuestion pivot dialog fires automatically — at max-round NO-GO for any depth, OR (B6, #128) early at **exhaustive round ≥ 5** when the mean of the last 3 rounds' findings ≥ 3.0. (The calibrated exhaustive trigger was removed in PR1b, then re-derived from a 65-session corpus and re-added in #128 — see `docs/superpowers/analysis/2026-06-01-b6-pivot-calibration.md`.)
- Reviewer output is empty or errors (check `codex --version`, network, rate limits)
- `verdict-rN.txt` is empty (codex crashed or `-o` path wrong — check `full-rN.txt` for clues)

## Integration with Other Skills

- **`/receiving-code-review`** — REQUIRED for the verification step. Prevents blind implementation.
- **`/verification-before-completion`** — use after final GO to confirm artifact is truly ready.
- **`/brainstorming`** — use BEFORE this skill to design the artifact; this skill reviews it.

## Feedback

If you encounter friction while using this skill — documentation mismatch, missing capability, unclear error, or need a workaround — create a GitHub issue so JAINE-developer can fix it in real-time.

**Create issue when:**
1. SKILL.md describes behavior X, reality is Y
2. Had to use a workaround instead of the standard path
3. Need a feature that doesn't exist
4. Script failed with an unhelpful error message
5. No existing bulldozer skill covers the use case (use `[feedback/new-skill]` prefix)

**Do NOT create issue when:** own mistake in arguments, external problem (Codex CLI not installed, network down), or behavior documented as a known limitation.

**Command:**

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,check" \
  --title "[feedback/check] short description" \
  --body "$(cat <<ISSUE
## What I was doing
{task description}

## What I expected
{expected behavior}

## What happened
{actual behavior, errors}

## Workaround used
{what was done instead, or "none — blocked"}

## Environment
- Plugin version: $(jq -r .version "$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json")
- Skill: check
- Project: $(pwd)
ISSUE
)"
```

**For new-skill requests (trigger #5):** use title prefix `[feedback/new-skill]`, labels `feedback,bulldozer` (omit `check`).

After creating the issue, tell the user:
> "I created a feedback issue about the check skill: {URL}. Want me to continue with a workaround, or would you like to get this fixed first?"

<!-- test bump trigger -->

<!-- test skip -->
<!-- c9 multi-commit test code -->
