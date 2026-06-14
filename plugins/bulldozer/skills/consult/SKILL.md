---
name: consult
description: "Lightweight conversational design consultation via external AI reviewer(s) — for abstract design questions, architectural tradeoffs, and 'should I X or Y?' decisions before any artifact exists. Single-codex by default; add --panel for a 3-model (codex+grok+agy) parallel find-holes panel, or --panel --repo for informed multi-model review of a real codebase. Triggers on 'help me choose between', 'compare options', 'talk through this architecture', 'what tradeoffs am I missing', 'what am I overlooking', 'find the holes', 'sanity check', 'ask all three models', 'Помоги выбрать', 'обсудим архитектурное решение', 'какие тут компромиссы', 'спроси codex', 'спроси все три модели'. Do NOT use single-consult when the question references files/code on disk — use bulldozer:check, or --panel --repo for a multi-model read of real code."
argument-hint: "[design question] — or: --panel [--repo PATH] [--verdict] <question>"
allowed-tools: ["Bash", "Read", "AskUserQuestion"]
---

# Bulldozer Consult — Conversational Design Validation

**Core principle:** Send a design question as inline text to an isolated codex process, get a decisive verdict, iterate cheaply. No files, no state, no ledger.

This skill is the lightweight sibling of `/bulldozer:check`. `check` is for artifact-grounded adversarial review (files, specs, configs); `consult` is for abstract design Q&A before any artifact exists.

## When to Use

- "Help me choose between X and Y" — architectural decisions
- "What tradeoffs am I missing in this approach?" — design exploration
- "Is this approach reasonable before I build it?" — pre-implementation sanity check
- "Should we use pattern A or B for this problem?" — pattern selection
- "Talk through this design with me" — second opinion on an idea

**Do NOT use for:**
- Anything referencing files, paths, diffs, code, or artifacts on disk → use `/bulldozer:check`
- Quick factual questions with deterministic answers → ask directly without codex
- Code review where tests exist → run the tests
- Implementation details (variable naming, exact syntax) → consult is design-level

## Step 1: Model Selection (every launch)

Same flow as `/bulldozer:check`. Read saved preference from `.bulldozer/config.md` (key: `reviewer_model`), show user 4 options via AskUserQuestion, save choice.

**Selection rules** (in order):
1. ALWAYS include current global model from `~/.codex/config.toml`
2. ALWAYS include last used model from `.bulldozer/config.md`
3. Fill remaining slots from: gpt-5.5, gpt-5.3-codex-spark, gpt-5.4-mini
4. Mark saved choice as "(Recommended)"

Save choice → use as `-m <model>` argument to codex.

## Step 2: Pre-flight Artifact Detection (CRITICAL)

Before invoking codex, scan the user's question for artifact references. If found, **STOP and recommend `/bulldozer:check` instead** — do not proceed with consult.

**Artifact patterns to detect** (case-insensitive):

| Pattern | Example |
|---------|---------|
| Absolute or relative file path | `/0/DEV/foo.md`, `src/bar.py`, `./config.json` |
| File extensions in prose | `*.md`, `*.py`, `*.ts`, `*.swift`, `*.yml`, `*.sql` |
| Repo-style references | "see specs/X", "in path Y", "at line N" |
| Artifact pointers | "attached", "this spec", "this code", "the diff", "the file", "@file" |
| Code identifiers | function names with parens, `class.method`, fenced code blocks with language tags |

**If detected**: tell the user:

> Я заметил, что вопрос ссылается на конкретный артефакт (`<excerpt>`). `consult` работает только с inline-текстом без чтения файлов. Запусти `/bulldozer:check <path>` для file-based ревью, или переформулируй вопрос как абстрактный design tradeoff.

Then exit without invoking codex. Why this matters: consult runs codex in a fully isolated tmpdir with no project access by design (see Step 4). If we let it through, codex will reason about something it can't see and return confident hallucination.

## Step 3: Wrap the User Prompt

Build the prompt with **belt-and-suspenders skill suppression** (SKIP SKILLS at both ends) and a structured verdict requirement:

```
SKIP SKILLS. Do not inspect files or run tools. Text-only consultation.
---
<user's design question, verbatim>
---
SKIP SKILLS. Give a decisive verdict. Under 200 words. End with one sentence
stating the basis or limits of this advice, then exactly one final standalone
line — one of:
VERDICT: GO
VERDICT: NO-GO
VERDICT: MINOR-FIXES
```

The required final line is the **anchored** `VERDICT: <X>` form the Step 5
classifier accepts — keeping the prompt and parser in lock-step (no drift).

The `SKIP SKILLS.` prefix is prompt-level suppression (weak on its own — codex may still load skills if the prompt mentions skill design). The flags in Step 4 give process-level enforcement (strong).

## Step 4: Invoke Codex in Full Isolation

```bash
TMPDIR_RUN="/tmp/bulldozer-consult-$$"
mkdir -p "$TMPDIR_RUN"
SESSION="${CLAUDE_CODE_SESSION_ID:0:8}"
OUT="$TMPDIR_RUN/verdict-r${ROUND}.txt"

(
  cd "$TMPDIR_RUN"
  timeout 180s codex exec \
    --skip-git-repo-check \
    --ignore-user-config \
    --ignore-rules \
    --ephemeral \
    -s read-only \
    -c model_reasoning_effort=xhigh \
    -m "$MODEL" \
    "$WRAPPED_PROMPT" \
    < /dev/null > "$OUT" 2>"$OUT.err"
)
EXIT_CODE=$?
```

**Every flag is load-bearing** (validated empirically — see "Why this isolation" below):

| Flag | Purpose | Without it |
|------|---------|------------|
| `--skip-git-repo-check` | Allow exec from non-git tmpdir | codex refuses to start outside trusted dir |
| `--ignore-user-config` | Skip `~/.codex/config.toml` & user skills | codex loads superpowers/skill-creator into reasoning |
| `--ignore-rules` | Skip AGENTS.md hierarchy | codex inherits project rules into context |
| `--ephemeral` | No rollout, not resumable | Session persisted (data retention risk) |
| `-s read-only` | Block all writes | codex may inadvertently modify files |
| Empty `$TMPDIR_RUN` cwd | No project file access | codex reads project files via tools |
| `timeout 180s` | Hard cap on runtime | Runaway xhigh reasoning hangs the skill |
| `< /dev/null` | Block stdin re-auth prompt | codex hangs waiting for input |
| `2>"$OUT.err"` | Split stderr to a side file (NOT `2>&1`) | Chatty errors land in `$OUT` and get misread as a substantive prose answer → false INCONCLUSIVE instead of fail-closed NO-GO |

`gtimeout` (GNU coreutils on macOS) is an acceptable fallback if plain `timeout` is missing.

**Check exit code:**
- `0` → proceed to Step 5
- `124` → timeout hit; tell user "codex exceeded 180s, try a shorter prompt or lower reasoning effort"
- Other non-zero → read last 20 lines of `$OUT`, report to user, do NOT silently retry

## Step 5: Parse the Verdict (Fail-Closed)

Apply the verdict classifier — the §3.7 logic shared with `scripts/consult_panel.py::classify_verdict`. The old `sed` + loose `\bGO\b` matching is gone (it misfired on prose like "a go-to pattern"):

1. Only an **anchored standalone** line matching `^\s*VERDICT:\s*(GO|NO-GO|MINOR-FIXES)\s*$` (case-insensitive) counts. Incidental prose is ignored.
2. Multiple anchored lines → the **FINAL** one wins (the model's conclusion beats an earlier echoed option).
3. No anchored line → strip the CLI banner/footer, then:
   - a substantive remaining line (≥3 words) → **INCONCLUSIVE** — show the prose and prompt the user to re-ask for a crisp verdict. INCONCLUSIVE does **not** count toward the Step 7 escalation trigger.
   - empty / whitespace / banner-only → **NO-GO** (fail-closed for real failures).

Run the classifier directly rather than matching by hand:

```bash
VERDICT=$(python3 - "$OUT" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["CLAUDE_PLUGIN_ROOT"], "skills/consult/scripts"))
from consult_panel import classify_verdict
print(classify_verdict(open(sys.argv[1]).read()))
PY
)
```

The `$CLAUDE_PLUGIN_ROOT` env var (set by Claude Code) is the **only** reliable path — the plugin code lives in `~/.claude/plugins/cache/…`, and the Bash tool's cwd is the consumer project, not the plugin root. A bare relative `skills/consult/scripts` would not resolve. (Same convention `/bulldozer:check` uses for its scripts.)

Why fail-closed: a missing/malformed verdict signals codex didn't follow instructions — silent GO would let bad advice through. **INCONCLUSIVE** is the middle path so a substantive prose answer that merely lacks the token is no longer a false NO-GO. Capture codex to `$OUT` with **split streams** (`> "$OUT" 2>"$OUT.err"`) so a chatty error on stderr can't be misread as a substantive prose answer — a real failure leaves `$OUT` empty/banner-only → fail-closed NO-GO.

## Step 6: Report to User

Show the cleaned verdict text (not the raw `$OUT` — too noisy). Format:

```
**Verdict: <GO|NO-GO|MINOR-FIXES|INCONCLUSIVE>** (round <N>, <time>s, <tokens> tokens, model <M>)

<verdict body>
```

Then prompt for next action:
- **GO** → "Готово. Применять рекомендацию?"
- **MINOR-FIXES** → "Учесть фиксы и переспросить? (новый round)"
- **NO-GO** → "Переформулировать или эскалировать на `/bulldozer:check`?"
- **INCONCLUSIVE** → codex gave a substantive prose answer but no crisp verdict line. Show the prose and ask: "Переспросить с явным требованием вердикта (`VERDICT: ...`)?" This is a re-ask, **not** a NO-GO, and per Step 7 it does **not** count toward the escalation trigger — it also does not occupy a slot in the `last_two_verdicts` window (skip it).

## Step 7: Multi-Round Escalation Rule

`consult` supports multiple rounds in the same Claude session — the user adjusts the question text and we re-invoke codex. Each round is independent (stateless by design).

**Track round state in this session's conversation context** (NOT in any file):
- `round_count` — how many codex invocations so far
- `last_two_verdicts` — sliding window

**Escalation trigger:** if `round_count >= 3` AND both `last_two_verdicts == "NO-GO"`:

> Третий раунд consult, два последних вердикта — NO-GO. Это означает, что вопрос вышел за рамки lightweight консультации. Рекомендую переключиться на `/bulldozer:check` с конкретным артефактом (создай design doc файл, потом проверь его). Продолжить consult всё равно?

User can choose to continue or escalate. We do NOT auto-invoke check.

## Step 8: Cleanup

After every invocation (success or failure):

```bash
rm -rf "$TMPDIR_RUN"
```

Tmpdir is per-PID so concurrent invocations don't collide. No state survives between invocations. No logs with prompt content (by design — see "What we don't do" below).

## Panel Mode (`--panel`) — Multi-Model Find-Holes

Opt-in: run **three models** (codex + grok + agy) in parallel instead of one codex, for diversity on hard questions. Orchestrator: `scripts/consult_panel.py` (Steps 1–8 above are the single-codex default; panel is a separate entrypoint). `agy` = Antigravity CLI (Gemini models); it replaced the retired gemini CLI (#189).

**When panel beats single-consult:**
- "What am I overlooking / what are the holes here?" — find-holes diversity (~50% of findings are unique to one model; verdict diversity is ≈0, so don't use panel just for a GO/NO-GO).
- A high-stakes design call where one reviewer's blind spot is costly. Cost: 4 model calls, ~17–60s typical. Worst case approaches **2×`--timeout`** (default 180s): the summarizer runs serially *after* the parallel triad, so a slow-but-not-failed model plus a slow merge stack up.

**Invocation:**

```bash
PANEL="${CLAUDE_PLUGIN_ROOT}/skills/consult/scripts/consult_panel.py"  # plugin cache, not cwd

# isolated find-holes (abstract question, no file access) — panel default
python3 "$PANEL" "<question>"

# informed find-holes (models READ the real repo) — for questions ABOUT a codebase
python3 "$PANEL" --repo <path> "<question>"

# verdict mode (per-model GO/NO-GO, no merge) — add --verdict (works with --repo too)
python3 "$PANEL" [--repo <path>] --verdict "<question>"
```

`$CLAUDE_PLUGIN_ROOT` is mandatory — the script lives in the plugin cache (`~/.claude/plugins/cache/…`), not the consumer project the Bash tool runs from.

Output: a merged `## SHARED` / `## UNIQUE` synthesis (find-holes) or a per-model verdict line (verdict), with raw per-model blocks below. Exits non-zero iff **every** model failed; one model failing degrades to a `[<model>: failed — …]` block and the panel continues with survivors.

**`--repo` is the deliberate exception to Step 2's artifact ban.** When the question is about real code, `--repo` grants the three models read access to that repo (informed mode). Split-test (2026-06-02): informed ≫ isolated for repo-specific questions, tie for abstract — so informed is opt-in (your code goes to the cloud reviewers only when you ask). Single-consult and isolated `--panel` stay file-free.

**Isolation:** all three run on the REAL HOME now — none gets a HOME sandbox. codex isolates via flags (`--ephemeral` etc.); **grok** via `--no-memory`/`--no-subagents` (a HOME-sandbox broke grok's `--repo` tool-worker auth — it cancelled on every informed run, #147); **agy**'s auth is macOS-Keychain-bound + OAuth-only, so there is no copyable token to seed a sandbox HOME and a fake HOME triggers re-auth (#189); non-interactive `-p` (stdin `/dev/null`) onboards an unknown repo with no OAuth prompt and starts no MCP server. **Read-only enforcement (#189):** `agy --print` AUTO-ACCEPTS every tool call — verified that NO flag/config disables it (not dropping `--dangerously-skip-permissions`, not `--sandbox`, not `tools.autoAccept:false`, not deny lists, not per-project settings; print mode can't prompt so it just executes). The deterministic gate is a **fail-closed PreToolUse hook**: the agy leg runs with cwd = a temp dir seeded with `.agents/hooks.json` whose hook ALLOWS only an EXACT set of known read tools (`read_file`/`view_file`/`list_dir`/`grep_search`/… — `view_file`+`list_dir` empirically confirmed) and DENIES everything else before it runs — any unlisted/mutating/command-exec tool, and any malformed input, returns `{"decision":"deny"}`. An exact allowlist fails closed; the earlier substring blocklist let `save_memory`/`shell`/`exec`/`save_file` and malformed stdin through (#189 code-review). The repo is reached ONLY via `--add-dir` (read), never as cwd — so a denied write can't touch it. Reads/search are allowed, so the review still works. grok/agy need their own logins.

**agy notes (#189):** agy prints plain text (no JSON envelope), so the parser is `parse_codex`. It persists each `-p` call's prompt+response in a per-call session dir `~/.gemini/antigravity-cli/brain/<conversationId>/` (plaintext transcripts — verified by a unique-marker probe) plus a `conversations/<id>.db`. For statelessness the run injects a unique NONCE into agy's prompt (which agy logs into its transcript); `_run_one` snapshots the brain id-set BEFORE the run and afterward deletes only NEW UUID-named dirs whose transcript carries the nonce — plus `conversations/<that-id>.db*`. A CONCURRENT agy session (e.g. the user's visual/IDE Antigravity app running at the same time) creates its own `brain/<uuid>` WITHOUT our nonce, so it is **never swept** — that is what makes the diff visual-safe (verified live: a single panel run while the GUI created 2 new dirs left all 40+ untouched). (Earlier id-via-hook capture failed in the panel-default isolated mode — agy makes ZERO tool calls there, so the hook never fired and the transcript LEAKED, #189 code-review; the nonce works in both modes. The id is validated as a full UUID and the glob anchored to `<id>.db*`, so a prefix-sibling conversation is never swept.) Note: the panel does NOT touch `~/.gemini/.../cache/projects.json`, which accumulates a harmless stale entry per run for the agy leg's temp cwd. Override the model with `BULLDOZER_AGY_MODEL` (an `agy models` label) if the default `Gemini 3.1 Pro (High)` is renamed. **Prompt framing matters:** the informed find-holes wrapper uses BEHAVIORAL wording ("where the code could behave incorrectly, surprisingly, or not as a caller expects"), NOT "holes/bugs/vulnerabilities/security" — the latter trips Gemini's safety refusal on security-flavoured code (proven on an auth file: trigger-word framing → refused ×2; behavioral framing → full review naming the same issues). The no-`write_file` clause is kept (agy, like the old gemini CLI, can otherwise save findings to a file and return empty). The retired gemini CLI returned empty `response` on informed `--repo` runs (agentic plan-mode) and was deprecated 2026-06-15; agy replaced it. Display label in the panel output stays "Gemini" (same models). Design: `docs/superpowers/specs/2026-06-02-consult-panel-design.md`.

## Logging

Append one line per invocation to `~/.claude/hooks/bulldozer-consult.log`:

```
2026-05-25T03:15:00+03:00 | session=f7186873 | round=1 | verdict=GO | tokens=4500 | time=4.3s | model=gpt-5.5 | project=/path/to/repo
```

**What we do NOT log:** the prompt content, the verdict body, any user-supplied text. Only metadata. This is a deliberate privacy property — see "What we don't do".

## What We Don't Do (and Why)

These are not oversights — they are validated design choices:

| Anti-feature | Why we don't have it |
|--------------|----------------------|
| Persistent mode (`codex exec resume`) | 3 of 4 dogfood runs voted REMOVE: data retention, stale context, 2× implementation surface, broken reproducibility. Realistic continuity scenarios are addressable by user copying prior verdict text into new prompt. |
| Session log with prompt content | Raw prompt snippets leak sensitive design text. Metadata-only log preserves observability without retention risk. |
| Auto-invoke `/bulldozer:check` on escalation | Escalation is advisory only — user decides. Silent skill chaining hides decisions. |
| File reading / project access | Process-level isolation (empty tmpdir + `--ignore-user-config --ignore-rules`) is the security boundary. Prompt-level "SKIP SKILLS." alone is theatrical. |
| Counting blockers in verdict prose | Empirically unparseable from short codex output (≤200 words). We use round count + NO-GO repetition instead — simple string match, reliable. |
| Custom config file in `.bulldozer/consult.md` | YAGNI. Model preference is shared with `check` via `.bulldozer/config.md` (single key: `reviewer_model`). Adding consult-specific config drifts both. |
| Scripts in `skills/consult/scripts/` | The bash flow above fits inline. Adding a wrapper script adds maintenance cost without benefit (no shared state, no complex logic). |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Running codex from project root (`-C $PROJECT_ROOT`) | Use empty tmpdir cwd — codex with project access reads files and hallucinates with false grounding |
| Skipping `--ignore-user-config` because `--ephemeral` was set | `--ephemeral` blocks rollout, NOT skill loading. Codex will still load user skills if the prompt mentions skill design. Use both. |
| Trusting "SKIP SKILLS." prefix alone | Empirically observed: codex loaded skill-creator anyway when prompt mentioned "skill design". Always combine prompt-level + process-level. |
| Letting user prompts with file paths through | Pre-flight detection (Step 2) is the routing primitive. Without it, consult becomes a worse check. |
| Treating missing verdict as silent success | Fail-closed: no anchored `VERDICT:` line AND no substantive prose → NO-GO. Substantive prose without the token → **INCONCLUSIVE** (re-ask), not a false NO-GO. |
| Parsing `full output` for verdict by hand | Use `classify_verdict` (§3.7): anchored `VERDICT:` line only, final one wins, banner stripped. The old `codex`↔`tokens used` sed misfired on prose like "a go-to pattern". |
| Running multi-round in background | FOREGROUND ONLY. User explicitly invokes each round. No automation, no cron. |

## Red Flags — STOP and Reassess

- Codex returns GO on first round with zero specifics → likely didn't engage; re-prompt with clearer scope
- Same NO-GO content repeats across rounds → the question may need a `check` artifact (escalation rule fires)
- Verdict references files you didn't mention → codex hallucinating; verify isolation flags are actually applied
- Tmpdir wasn't cleaned (find `/tmp/bulldozer-consult-*` from a prior run) → previous invocation crashed before cleanup; safe to `rm -rf` but investigate why
- User keeps adding artifact references to dodge pre-flight → the right answer is `check`, not a more clever consult

## Quick Reference — Full Invocation Template

```bash
# 1. Pre-flight (Claude-side regex on $ARGUMENTS)
# 2. Model selection (AskUserQuestion + .bulldozer/config.md)
# 3. Wrap prompt
WRAPPED_PROMPT=$(printf 'SKIP SKILLS. Do not inspect files or run tools. Text-only consultation.\n---\n%s\n---\nSKIP SKILLS. Give a decisive verdict. Under 200 words. End with one sentence stating the basis or limits of this advice, then exactly one final standalone line — one of:\nVERDICT: GO\nVERDICT: NO-GO\nVERDICT: MINOR-FIXES\n' "$USER_PROMPT")

# 4. Isolated invocation
TMPDIR_RUN="/tmp/bulldozer-consult-$$"
mkdir -p "$TMPDIR_RUN"
(
  cd "$TMPDIR_RUN"
  timeout 180s codex exec \
    --skip-git-repo-check \
    --ignore-user-config \
    --ignore-rules \
    --ephemeral \
    -s read-only \
    -c model_reasoning_effort=xhigh \
    -m "$MODEL" \
    "$WRAPPED_PROMPT" \
    < /dev/null > verdict.txt 2>verdict.err   # split streams: stderr noise out of the verdict file
)
EXIT=$?

# 5. Parse verdict (fail-closed) — §3.7 classifier, not sed/loose-regex
VERDICT=$(python3 - "$TMPDIR_RUN/verdict.txt" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["CLAUDE_PLUGIN_ROOT"], "skills/consult/scripts"))
from consult_panel import classify_verdict
print(classify_verdict(open(sys.argv[1]).read()))
PY
)

# 6. Log metadata, cleanup
echo "$(date -Iseconds) | session=${CLAUDE_CODE_SESSION_ID:0:8} | round=$ROUND | verdict=$VERDICT | tokens=$TOKENS | time=${ELAPSED}s | model=$MODEL | project=$(git rev-parse --show-toplevel 2>/dev/null || pwd)" >> ~/.claude/hooks/bulldozer-consult.log
rm -rf "$TMPDIR_RUN"
```

## Why This Isolation (Empirical Basis)

Each design choice traces back to a measured failure mode. Dogfooded against codex over 11 independent runs before shipping (see PR description for full reproducibility trail).

| Decision | Measurement |
|----------|-------------|
| Process-level isolation vs prompt-level | Without `--ignore-user-config`: 43s, 51K tokens, 1900 lines of skill-loading noise. With all 5 flags: **4s, 4.5K tokens, 0 noise**. ~10× faster, ~12× cheaper. |
| Stateless only | 3 of 4 cross-framing dogfood runs (neutral A2 + adversarial A3 + adversarial dogfood-2) independently voted REMOVE persistent. |
| Artifact pre-flight | Two independent runs (signal-pick B1 + collision-find C2) converged on "artifact reference is the kill-switch — route to check, not consult". |
| Fail-closed verdict parsing | Empirical: codex output noise can suppress `GO` matches; silent default to NO-GO forces user re-prompt rather than acting on absent advice. |
| Escalation rule (round≥3 + 2× NO-GO) | Alternative rule "count blockers ≥ 5" empirically unparseable from short prose (verified directly via B2). |

## Integration with Other Skills

- **`/bulldozer:check`** — escalation target when consult is too lightweight (artifact exists or stuck after 3 rounds)
- **`/receiving-code-review`** — discipline for evaluating codex's verdict honestly before applying it to the design
- **`/brainstorming`** — runs BEFORE consult to shape the question; consult validates the resulting design idea

## Feedback

If you encounter friction while using this skill — documentation mismatch, missing capability, unclear error, or need a workaround — create a GitHub issue.

**Create issue when:**
1. SKILL.md describes behavior X, reality is Y
2. Had to use a workaround instead of the standard path
3. Need a feature that doesn't exist
4. Script failed with unhelpful error
5. Codex returned consistently unusable output for a valid design question
6. Pre-flight artifact detection misfired (false positive or false negative)

**Do NOT create issue when:** own mistake in arguments, external problem (Codex CLI not installed, network down), or behavior documented as a known limitation.

**Command:**

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,consult" \
  --title "[feedback/consult] short description" \
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
- Skill: consult
- Project: $(pwd)
ISSUE
)"
```

After creating the issue, tell the user:
> "Я создал feedback issue про consult: {URL}. Продолжить с workaround или сначала пофиксим?"
