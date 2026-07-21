---
name: consult
description: "Lightweight conversational design consultation via external AI reviewer(s) — for abstract design questions, architectural tradeoffs, and 'should I X or Y?' decisions before any artifact exists. Single-codex by default; add --panel for a 3-model (codex+grok+agy) parallel find-holes panel, or --panel --repo for informed multi-model review of a real codebase. Pick a single model (--codex/--grok/--agy), or add --web to ground the critique in live web research. Triggers on 'help me choose between', 'compare options', 'talk through this architecture', 'what tradeoffs am I missing', 'what am I overlooking', 'find the holes', 'sanity check', 'ask all three models', 'Помоги выбрать', 'обсудим архитектурное решение', 'какие тут компромиссы', 'спроси codex', 'спроси все три модели'. Do NOT use for literature/paper search or raw data gathering — every mode critiques or judges, never runs a search. Do NOT use single-consult to review a file/diff on disk — use bulldozer:check, or --panel --repo for real code."
argument-hint: "[design question] — or: [--codex|--grok|--agy|--panel] [--web[=models]] [--repo PATH] [--verdict] <question>"
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
- Reviewing a file/diff/artifact on disk *as the review target* → use `/bulldozer:check` (a question that merely *names* a file as context is fine — see Step 2, Case B)
- Quick factual questions with deterministic answers → ask directly without codex
- Code review where tests exist → run the tests
- Implementation details (variable naming, exact syntax) → consult is design-level

## Step 1: Model Selection (every launch)

Same flow as `/bulldozer:check`. Read saved preference from `.bulldozer/config.md` (key: `reviewer_model`), show user 4 options via AskUserQuestion, save choice.

**Selection rules** (in order):
1. ALWAYS include current global model from `~/.codex/config.toml`
2. ALWAYS include last used model from `.bulldozer/config.md`
3. Fill remaining slots from: gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna
4. Mark saved choice as "(Recommended)"

Save choice → use as `-m <model>` argument to codex.

## Step 2: Artifact Reference Notice (soft warn, NOT a hard stop)

Before invoking codex, scan the user's question for artifact references. If found, **classify intent — do not blanket-block.** The old hard-stop never once fired in 209 production launches (#107); the real hallucination guard is the Step 3 prompt directive + Step 4 isolation, not this step.

**Artifact patterns to detect** (case-insensitive):

| Pattern | Example |
|---------|---------|
| Absolute or relative file path | `/0/DEV/foo.md`, `src/bar.py`, `./config.json` |
| File extensions in prose | `*.md`, `*.py`, `*.ts`, `*.swift`, `*.yml`, `*.sql` |
| Repo-style references | "see specs/X", "in path Y", "at line N" |
| Artifact pointers | "attached", "this spec", "this code", "the diff", "the file", "@file" |
| Code identifiers | function names with parens, `class.method`, fenced code blocks with language tags |

**On a match, classify by INTENT:**

- **Case A — artifact as review TARGET** ("review `foo.py`", "what's wrong with this code", "check this diff"): **STOP**, recommend `/bulldozer:check <path>`. consult can't read files, so a review *of a file* belongs in `check` (or `--panel --repo` for a multi-model read).
  > Вопрос просит ревью самого артефакта (`<excerpt>`). `consult` не читает файлы — запусти `/bulldozer:check <path>` для file-based ревью (или `--panel --repo <path>` для multi-model чтения кода).
- **Case B — artifact as decisional CONTEXT** ("should we refactor `foo.py` because X?", "we have N call-sites in `bar.py` — pattern A or B?"): show a one-line notice, then **PROCEED**. The question is an abstract design tradeoff that merely *names* a file; codex answers from the supplied context and discloses its basis (verified: a clean verdict with `"Basis: from the supplied context only; I did not inspect files"`, zero file hallucination).
  > Вопрос упоминает артефакт (`<excerpt>`) как контекст, не как цель ревью — продолжаю consult (codex отвечает по тексту, файлы не читает). Нужно ревью файла → `/bulldozer:check <path>`.
- **Ambiguous** (can't tell target from context): ask the user with AskUserQuestion (rare path).

Why this is a soft warn, not a stop: consult runs codex in a fully isolated tmpdir with no project access (Step 4), and the Step 3 prompt directive ("Do not inspect files") already prevents grounded hallucination. The pre-flight is a routing aid for Case A, not a gate that blocks Case B.

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
OUT="$TMPDIR_RUN/verdict-r${ROUND}.txt"

START=$(python3 -c 'import time; print(time.time())')   # feeds ELAPSED for the Step 6 log line (#322 D5)
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
ELAPSED=$(python3 -c "import time; print(f'{time.time() - $START:.1f}')")   # tenths — matches the time=<X.X>s grammar
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
- `124` → timeout hit; **first write the Step 6 log line with `verdict=TIMEOUT`** (a failed invocation must not vanish from the log — the panel path logs `verdict=ERROR` on total failure, this flow previously logged only successes, #322 A6), then tell user "codex exceeded 180s, try a shorter prompt or lower reasoning effort"
- Other non-zero → **first write the Step 6 log line with `verdict=ERROR`**, then read last 20 lines of `$OUT`, report to user, do NOT silently retry

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
import os, sys, glob
# Resolve the consult scripts dir WITHOUT relying on $CLAUDE_PLUGIN_ROOT — it is NOT exported
# to the Bash tool's environment (empirically empty, CC 2.1.185 — #221). Prefer it if set
# (future-proof), else the newest installed plugin-cache copy.
cands = ([os.path.join(os.environ["CLAUDE_PLUGIN_ROOT"], "skills/consult/scripts")]
         if os.environ.get("CLAUDE_PLUGIN_ROOT") else [])
cands += sorted(glob.glob(os.path.expanduser(
    "~/.claude/plugins/cache/*/bulldozer/*/skills/consult/scripts")),
    key=os.path.getmtime, reverse=True)
scripts = next((d for d in cands if os.path.isdir(d)), None)
if not scripts:
    sys.exit("consult scripts dir not found (#221 resolver)")
sys.path.insert(0, scripts)
from consult_panel import classify_verdict
print(classify_verdict(open(sys.argv[1]).read()))
PY
)
```

**Do NOT use `os.environ["CLAUDE_PLUGIN_ROOT"]` here** — that var is resolved only in plugin manifests / markdown substitution, **NOT exported to the Bash tool's shell** (empirically empty in CC 2.1.185, `KeyError` — #221). The plugin code lives in `~/.claude/plugins/cache/…` and the Bash tool's cwd is the consumer project, so the snippet self-resolves the scripts dir: it honors `$CLAUDE_PLUGIN_ROOT` if ever set, else falls back to the newest plugin-cache copy (the abs path the skill header also prints as "Base directory for this skill"). A bare relative `skills/consult/scripts` would not resolve.

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

**NOT a research runner (#260):** every consult path — single and panel, with or without `--web` — wraps the question in **critique (find-holes) or verdict** framing; the models are told to list holes or judge, never to execute a task. A "find/list papers on X" or "search Y and return results" request gets its *query critiqued* (the literal max-8-points holes list) instead of results — the template working as designed, not a broken leg (verified live 2026-07-12: agy leg, zero tool calls, prompt critique only). `--web` grounds a critique/verdict in live sources; it does not turn consult into a search tool. Route literature/data-gathering requests to your own research tools (WebSearch/WebFetch), not consult.

**Invocation:**

```bash
# Resolve the plugin dir WITHOUT $CLAUDE_PLUGIN_ROOT (NOT exported to the Bash tool — #221):
# honor it if set, else the newest plugin-cache copy.
BULLDOZER_DIR=$( { [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -d "$CLAUDE_PLUGIN_ROOT/skills/consult" ] \
  && printf '%s\n' "$CLAUDE_PLUGIN_ROOT"; } || ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null | head -1 )
PANEL="$BULLDOZER_DIR/skills/consult/scripts/consult_panel.py"

# isolated find-holes (abstract question, no file access) — all three, panel default
python3 "$PANEL" "<question>"

# pick specific models (any combination); bare panel-script call = all three
python3 "$PANEL" --grok "<question>"            # one model alone
python3 "$PANEL" --codex --grok "<question>"    # a subset

# informed find-holes (models READ the real repo) — for questions ABOUT a codebase
python3 "$PANEL" --repo <path> "<question>"

# verdict mode (per-model GO/NO-GO, no merge) — add --verdict (works with --repo too)
python3 "$PANEL" [--repo <path>] --verdict "<question>"

# --web: opt-in DEEP web research (web search + subagents). ALWAYS use the =form so it
# can't eat the question; blanket = all selected, scoped = a comma-list.
# ⚠ ALWAYS launch --web via Bash run_in_background: true — total wall-clock routinely
# hits 600–660 s, past the 10-min foreground Bash cap (see "Web lane", #313).
python3 "$PANEL" --grok --web=grok "<question>"          # web on grok only
python3 "$PANEL" --panel --web=codex,grok "<question>"   # web on codex+grok; agy isolated
```

The `BULLDOZER_DIR` resolver above is required because `$CLAUDE_PLUGIN_ROOT` is **not exported to the Bash tool** (#221) and the script lives in the plugin cache (`~/.claude/plugins/cache/…`), not the consumer project the Bash tool runs from.

Output: a merged `## SHARED` / `## UNIQUE` synthesis (find-holes) or a per-model verdict line (verdict), with raw per-model blocks below. Exits non-zero iff **every** model failed; one model failing degrades to a `[<model>: failed — …]` block and the panel continues with survivors.

**`--repo` is the deliberate exception to Step 2's artifact ban.** When the question is about real code, `--repo` grants the three models read access to that repo (informed mode). Split-test (2026-06-02): informed ≫ isolated for repo-specific questions, tie for abstract — so informed is opt-in (your code goes to the cloud reviewers only when you ask). Single-consult and isolated `--panel` stay file-free.

**Isolation:** all three run on the REAL HOME now — none gets a HOME sandbox. codex isolates via flags (`--ephemeral` etc.); **grok** via `--no-memory`/`--no-subagents` (a HOME-sandbox broke grok's `--repo` tool-worker auth — it cancelled on every informed run, #147), pinned to `grok-4.5` + `--reasoning-effort high` (override: `BULLDOZER_GROK_MODEL`/`BULLDOZER_GROK_EFFORT`; requires grok CLI ≥ 0.2.107 — older CLIs reject the flags and the leg fails loud); **agy**'s auth is macOS-Keychain-bound + OAuth-only, so there is no copyable token to seed a sandbox HOME and a fake HOME triggers re-auth (#189); non-interactive `-p` (stdin `/dev/null`) onboards an unknown repo with no OAuth prompt and starts no MCP server. **Read-only enforcement (#189):** `agy --print` AUTO-ACCEPTS every tool call — verified that NO flag/config disables it (not dropping `--dangerously-skip-permissions`, not `--sandbox`, not `tools.autoAccept:false`, not deny lists, not per-project settings; print mode can't prompt so it just executes). The deterministic gate is a **fail-closed PreToolUse hook**: the agy leg runs with cwd = a temp dir seeded with `.agents/hooks.json` whose hook ALLOWS only an EXACT set of known read tools (`read_file`/`view_file`/`list_dir`/`grep_search`/… — `view_file`+`list_dir` empirically confirmed) and DENIES everything else before it runs — any unlisted/mutating/command-exec tool, and any malformed input, returns `{"decision":"deny"}`. An exact allowlist fails closed; the earlier substring blocklist let `save_memory`/`shell`/`exec`/`save_file` and malformed stdin through (#189 code-review). The repo is reached ONLY via `--add-dir` (read), never as cwd — so a denied write can't touch it. Reads/search are allowed, so the review still works. grok/agy need their own logins.

**agy notes (#189):** agy prints plain text (no JSON envelope), so the parser is `parse_codex`. It persists each `-p` call's prompt+response in a per-call session dir `~/.gemini/antigravity-cli/brain/<conversationId>/` (plaintext transcripts — verified by a unique-marker probe) plus a `conversations/<id>.db`. For statelessness the run injects a unique NONCE into agy's prompt (which agy logs into its transcript); `_run_one` snapshots the brain id-set BEFORE the run and afterward deletes only NEW UUID-named dirs whose transcript carries the nonce — plus `conversations/<that-id>.db*`. A CONCURRENT agy session (e.g. the user's visual/IDE Antigravity app running at the same time) creates its own `brain/<uuid>` WITHOUT our nonce, so it is **never swept** — that is what makes the diff visual-safe (verified live: a single panel run while the GUI created 2 new dirs left all 40+ untouched). (Earlier id-via-hook capture failed in the panel-default isolated mode — agy makes ZERO tool calls there, so the hook never fired and the transcript LEAKED, #189 code-review; the nonce works in both modes. The id is validated as a full UUID and the glob anchored to `<id>.db*`, so a prefix-sibling conversation is never swept.) Note: the panel does NOT touch `~/.gemini/.../cache/projects.json`, which accumulates a harmless stale entry per run for the agy leg's temp cwd. Override the model with `BULLDOZER_AGY_MODEL` (an `agy models` label) if the default `Gemini 3.1 Pro (High)` is renamed; if the label fails to resolve at leg start (agy ≥1.1.2 print mode hard-fails on an unresolvable `--model` — display labels resolve only via the server catalog, so a transient catalog/auth hiccup kills the leg even for a valid label, #359), the leg retries once WITHOUT `--model` on agy's own default and the completion line marks `agy_model=…_(fallback-default)`. **Prompt framing matters:** the informed find-holes wrapper uses BEHAVIORAL wording ("where the code could behave incorrectly, surprisingly, or not as a caller expects"), NOT "holes/bugs/vulnerabilities/security" — the latter trips Gemini's safety refusal on security-flavoured code (proven on an auth file: trigger-word framing → refused ×2; behavioral framing → full review naming the same issues). The no-`write_file` clause is kept (agy, like the old gemini CLI, can otherwise save findings to a file and return empty). The retired gemini CLI returned empty `response` on informed `--repo` runs (agentic plan-mode) and was deprecated 2026-06-15; agy replaced it. Display label in the panel output stays "Gemini" (same models). Design: `docs/superpowers/specs/2026-06-02-consult-panel-design.md`.

## Web lane (`--web`) — opt-in deep web research

`--web` lets the selected models do **deep web research** (web search + their own subagents), grounding the answer in CURRENT real-world practice — for questions that need live community practices, not just training knowledge. Per-model: `--web=grok,agy` (scoped) or bare `--web` (blanket = all selected). **Always emit the `=` form** (or put a bare `--web` LAST): argparse otherwise binds the question to `--web` and errors (empirically confirmed). Per-model effect: codex `-c web_search="live"` (canonical key); grok drops `--no-subagents`/`--disable-web-search` but KEEPS read-only `--permission-mode plan` (verified: still spawns parallel subagents); agy's read-only hook ALLOW gains `search_web`/`read_url_content`.

**READ-side only, by design.** `--web` enables web fetch/search + subagents (reading), NEVER write/shell. So `--web --repo` lets a model read your real code AND reach the web — it **reverses the #189 no-egress guarantee** (opt-in, like `--repo`: code/design can egress to the web), but it can NOT mutate the repo. Default (no `--web`) stays fully isolated / no-egress.

**Output & persistence:** each web model's raw research (large, sometimes garbled by parallel subagents) is pre-compressed to a findings+URL digest before the merge (inline shows the digest); the full raw is saved to `.bulldozer/consult-<ts>/` (self-ignoring `.gitignore`; `research.md` + `raw-<model>.md`; keep-last-10) for drill-down. The default `--timeout` rises to 600 s under `--web`.

**Run `--web` panels in the BACKGROUND (#313).** The research-compressor runs **serially per web survivor**, then the summarizer — all **after** the parallel triad: total = slowest web leg + N×compress (one per web survivor) + merge, every stage bounded by `--timeout` (so the 3-web-survivor worst case approaches 5×600 s = 50 min; typical runs land in the 600–660 s band, 12 runs ≥ 500 s in the completion log). Claude Code's foreground Bash cap is a hard 10-min SIGKILL — a foreground `--web` panel dies at exactly 600 s with the work nearly done. Invoke the panel with `run_in_background: true` on the Bash tool and read the output when the task completes — the background path has no cap (455 s run verified). Non-web panels (~17–60 s typical) stay foreground.

## Logging

**Channel split (#322 F1):** every consult codex leg — inline Step 4 and the panel's `--codex` — is a raw `codex exec` subprocess, entirely OUTSIDE the plugin's own MCP bridge (`mcp/codex_server.py`). The bridge's `TURN_OK`/`TURN_ERROR`/`APPROVAL` auditing in `bulldozer-codex.log` therefore never sees consult traffic: a codex capacity error during a consult surfaces ONLY here, as `verdict=TIMEOUT|ERROR` (or a panel `failures=codex:…` field). Accepted split — consult's value is process isolation from an empty tmpdir, which the session-singleton bridge cannot provide.

Two kinds of line land in `~/.claude/hooks/bulldozer-consult.log`, told apart by their fields:

**1. Start-marker** — the `UserPromptSubmit` hook (`hooks/hooks.json`) writes one lean line the moment `/bulldozer:consult` is typed. It records only that an invocation started — nothing has run yet, so it carries NO `verdict`/`tokens`/`model`:

```
<ISO8601-ts> | event=consult-invoke | project=<P>
```

(#107 lesson: the marker used to carry always-empty `verdict=` / `tokens=0` / `model=`; those were dropped — empty fields read as *missing completion data*, not telemetry.)

**2. Completion line** — one line per COMPLETED invocation, when the outcome is known. The panel path (`--panel` / per-model `--codex`/`--grok`/`--agy` / `--web`) writes it **deterministically** from `consult_panel.py::_log_completion`; the inline single-codex flow writes it from Step 6 below. Two shapes, both metadata-only:

**Inline single-codex** — `model=` (one model), written by Step 6 via the shared CLI shim (#334 — `event=`/`session=` prepended by the writer). Strict field order:

```
<ISO8601-ts> | event=consult-complete | session=<S> | round=<N> | verdict=<V> | tokens=<T> | time=<X>s | model=<M> | project=<P>
2026-07-20T03:15:00+07:00 | event=consult-complete | session=f7186873 | round=1 | verdict=GO | tokens=NA | time=4.3s | model=gpt-5.6-sol | project=/path/to/repo
```

(`tokens=` is honestly **NA today** — no extraction mechanism exists since the codex banner-parsing hack was dropped; matching the panel path. `verdict=` may also be `TIMEOUT`/`ERROR` from the Step 4 failure branches, #322 A6.)

**Panel / per-model / `--web`** — `models=` (comma list) + `web=`, written by the script via the shared writer (#334):

```
<ISO8601-ts> | event=consult-complete | session=<S> | round=1 | verdict=<V> | tokens=NA | time=<X>s | models=<m,…> | web=<m,…> | survivors=<N/M> | failures=<Display:class,…> | legtimes=<Display:sec,…> | agy_model=<id> | codex_effort=<e> | project=<P>
2026-07-20T12:10:00+07:00 | event=consult-complete | session=6b48be89 | round=1 | verdict=find-holes | tokens=NA | time=54.8s | models=codex,grok,agy | web= | survivors=2/3 | failures=Grok:timeout | legtimes=GPT:31.2,Grok:180.0,Gemini:44.9 | agy_model=Gemini_3.1_Pro_(High) | codex_effort=medium | project=/path/to/repo
```

**Miner note (#334 transition):** history and un-restarted sessions still contain the pre-#334 completion shape — same fields, NO `event=` key (`session=` first). Detect per-line: segment 2 `event=` → canonical, else legacy. Inline vs panel is discriminated by `model=` singular vs `models=` plural in BOTH shapes.

| Field | Value — and the ONLY accepted "no data" form |
|-------|---------|
| `session` | `CLAUDE_CODE_SESSION_ID` token-normalized (`tr -c 'A-Za-z0-9_-' '_'`) then first 8 chars; `NA` if unset — **never the project name** |
| `round` | integer ≥ 1 (panel is single-shot → always `1`) |
| `verdict` | inline: `GO` / `NO-GO` / `MINOR-FIXES` / `INCONCLUSIVE`. Panel: `find-holes` (no GO/NO-GO) · the collapsed per-model verdict or `mixed` (disagreement) in `--verdict` mode · `ERROR` (all models failed). **Never `TBD` or empty.** |
| `tokens` | inline: integer, `NA` if codex didn't report it. Panel: always `NA` (no per-model token capture yet). **One sentinel, not `N/A`/`na`/`-`/`~`/``/`0`.** |
| `time` | seconds, one decimal, `s` suffix (e.g. `4.3s`) |
| `model` / `models` | inline: the `-m` model id. Panel: comma-joined selected models (plus `web=` = the subset doing web research, empty when none) |
| `project` | `git rev-parse --show-toplevel 2>/dev/null \|\| pwd` |

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
| A wrapper script for the SINGLE-codex flow | The single-consult bash flow fits inline (no shared state, no complex logic). The `--panel` mode is the deliberate exception — it DOES ship `scripts/consult_panel.py` because 3 parallel models + per-model isolation + merge is genuinely complex logic that does not fit inline. |

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Running codex from project root (`-C $PROJECT_ROOT`) | Use empty tmpdir cwd — codex with project access reads files and hallucinates with false grounding |
| Skipping `--ignore-user-config` because `--ephemeral` was set | `--ephemeral` blocks rollout, NOT skill loading. Codex will still load user skills if the prompt mentions skill design. Use both. |
| Trusting "SKIP SKILLS." prefix alone | Empirically observed: codex loaded skill-creator anyway when prompt mentioned "skill design". Always combine prompt-level + process-level. |
| Hard-blocking every file-path mention | Step 2 is a soft warn: route Case A (artifact = review target) to `check`; PROCEED on Case B (artifact named as context). Blanket blocks are false positives — the hard-stop never fired in 209 prod launches (#107). |
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

# 3b. Resolve the shared log writer ONCE (#334; #221: $CLAUDE_PLUGIN_ROOT is NOT
#     in the Bash env — honor it when set, else newest installed cache)
BLOG=""
for d in ${CLAUDE_PLUGIN_ROOT:+"$CLAUDE_PLUGIN_ROOT"} $(ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null); do
  [ -f "$d/lib/bulldozer_log.py" ] && BLOG="$d/lib/bulldozer_log.py" && break
done

# 4. Isolated invocation
TMPDIR_RUN="/tmp/bulldozer-consult-$$"
mkdir -p "$TMPDIR_RUN"
START=$(python3 -c 'import time; print(time.time())')
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
ELAPSED=$(python3 -c "import time; print(f'{time.time() - $START:.1f}')")   # tenths — the time=<X.X>s grammar

# 4b. Failure branch BEFORE parsing (#322 A6): a timeout/error must not be
# misclassified by the verdict parser — log it and stop.
if [ "$EXIT" -ne 0 ]; then
    [ "$EXIT" -eq 124 ] && VERDICT=TIMEOUT || VERDICT=ERROR
    # #334: canonical line via the shared CLI shim (it derives session= from
    # CLAUDE_CODE_SESSION_ID itself); resolver failure WARNS, never a silent drop
    if [ -n "$BLOG" ]; then
        python3 "$BLOG" "${BULLDOZER_CONSULT_LOG:-$HOME/.claude/hooks/bulldozer-consult.log}" consult-complete \
          "round=$ROUND" "verdict=$VERDICT" "tokens=NA" "time=${ELAPSED}s" "model=$MODEL" \
          "project=$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || true
    else
        echo "warning: bulldozer_log.py not found — consult completion line NOT logged" >&2
    fi
    tail -20 "$TMPDIR_RUN/verdict.err"   # report the failure to the user; do NOT silently retry
    rm -rf "$TMPDIR_RUN"; exit "$EXIT"
fi

# 5. Parse verdict (fail-closed) — §3.7 classifier, not sed/loose-regex
#    (self-resolves the scripts dir — $CLAUDE_PLUGIN_ROOT is NOT in the Bash env, #221)
VERDICT=$(python3 - "$TMPDIR_RUN/verdict.txt" <<'PY'
import os, sys, glob
cands = ([os.path.join(os.environ["CLAUDE_PLUGIN_ROOT"], "skills/consult/scripts")]
         if os.environ.get("CLAUDE_PLUGIN_ROOT") else [])
cands += sorted(glob.glob(os.path.expanduser(
    "~/.claude/plugins/cache/*/bulldozer/*/skills/consult/scripts")),
    key=os.path.getmtime, reverse=True)
scripts = next((d for d in cands if os.path.isdir(d)), None)
if not scripts:
    sys.exit("consult scripts dir not found (#221 resolver)")
sys.path.insert(0, scripts)
from consult_panel import classify_verdict
print(classify_verdict(open(sys.argv[1]).read()))
PY
)

# 6. Log metadata, cleanup (#334: canonical line via the shared CLI shim — it
#    derives session= from CLAUDE_CODE_SESSION_ID itself; resolver failure WARNS)
T="${TOKENS:-NA}"; [ "$T" = "0" ] && T="NA"        # 0 is the no-data stub the schema forbids, not a count (#107)
if [ -n "$BLOG" ]; then
    python3 "$BLOG" "${BULLDOZER_CONSULT_LOG:-$HOME/.claude/hooks/bulldozer-consult.log}" consult-complete \
      "round=$ROUND" "verdict=$VERDICT" "tokens=$T" "time=${ELAPSED}s" "model=$MODEL" \
      "project=$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || true
else
    echo "warning: bulldozer_log.py not found — consult completion line NOT logged" >&2
fi
rm -rf "$TMPDIR_RUN"
```

## Why This Isolation (Empirical Basis)

Each design choice traces back to a measured failure mode. Dogfooded against codex over 11 independent runs before shipping (see PR description for full reproducibility trail).

| Decision | Measurement |
|----------|-------------|
| Process-level isolation vs prompt-level | Without `--ignore-user-config`: 43s, 51K tokens, 1900 lines of skill-loading noise. With all 5 flags: **4s, 4.5K tokens, 0 noise**. ~10× faster, ~12× cheaper. |
| Stateless only | 3 of 4 cross-framing dogfood runs (neutral A2 + adversarial A3 + adversarial dogfood-2) independently voted REMOVE persistent. |
| Artifact pre-flight | Soft routing aid, NOT a kill-switch (#107): the hard-stop framing never fired in 209 prod launches — Case A (review target) routes to `check`, Case B (context ref) proceeds. The real hallucination guard is the Step 3 directive + Step 4 isolation. |
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
- Plugin version: $(jq -r .version "$(ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json 2>/dev/null | head -1)" 2>/dev/null || echo unknown)
- Skill: consult
- Project: $(pwd)
ISSUE
)"
```

After creating the issue, tell the user:
> "Я создал feedback issue про consult: {URL}. Продолжить с workaround или сначала пофиксим?"
