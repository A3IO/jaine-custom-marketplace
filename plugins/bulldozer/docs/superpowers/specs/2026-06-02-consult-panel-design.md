# Bulldozer Consult Panel — Multi-Model Find-Holes Mode

- **Status:** SHIPPED — 2026-06-02 (design preserved as §1–§7; post-ship corrections in §8). Follow-ups: #142 cleanup, #147/#148 grok real-HOME.
- **Issue:** shipped via the consult-panel PR; this doc's §8 records the implementation deltas + dogfood corrections.
- **Author:** JAINE + Crís
- **Scope:** New opt-in `--panel` mode for `/bulldozer:consult` (runs codex + grok + gemini), the summarizer merge layer, and a parsing-robustness fix for the existing single-consult verdict path. Does **NOT** change single-codex default behavior except for that parsing fix.

---

## 1. Problem / Origin

Original idea (Crís): "запихнуть в `/consult` не просто gpt, а ещё и gemini и grok". Hypothesis: three models from three labs (OpenAI / xAI / Google) give three perspectives → catch blind spots one model misses.

The whole feature hung on whether that diversity is **real** or imagined. So before any spec, we tested empirically (Crís: "погоди спеку... давай сначала протестируем как это работает! попробуй разные варианты в tmp!"). The empirical results **changed the design twice**. This spec records what we built and why.

## 2. Empirical Basis

All measurements taken in session 2026-06-01/02 on Crís's machine. Test artifacts lived in `/tmp/bulldozer-panel-test/` (ephemeral — gone now; numbers transcribed here). CLI versions at test time: `codex` 0.135.0, `grok` 0.2.14, `gemini` 0.42.0.

### Round 1 — Isolation (can we run the three CLIs hermetically?)

The whole value of `consult` rests on process-level isolation (empty tmpdir + flags) so the reviewer reasons about the prompt only, not the local project. We had to reproduce that for each CLI. Findings:

| CLI | Default model | Verified isolated command | Wall | Output parse |
|-----|--------------|---------------------------|------|--------------|
| **codex** | gpt-5.5 | `codex exec --skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral -s read-only -c model_reasoning_effort=X` | ~5s | stdout is clean when stderr split (`2>/dev/null`); banner/footer go to stderr |
| **grok** | grok-build | `HOME=<sandbox> grok -p "$W" --no-memory --no-subagents --disable-web-search --output-format json` | ~10s | JSON `.text` |
| **gemini** | gemini-3.1-pro-preview | `HOME=<sandbox> gemini -p "$W" --skip-trust --approval-mode plan -e none -o json` | ~19s | JSON `.response` |

**Surprises that would have broken a guessed design:**

1. **grok reads all of `~/.claude` without a HOME-override.** `grok inspect` in an empty tmpdir showed it pulling `~/.claude/CLAUDE.md` (~5437 tokens) + `CLAUDE.local.md`, `settings.json` permissions, **194 skills**, and MCP servers (with live auth errors). 27s + a wall of stderr noise. grok has **no** declarative `--ignore-*`; the fix is HOME-override. With `HOME` pointed at a sandbox, the `~/.claude` leak vanishes (`Project Instructions: 0`, `Permissions: 0`, `Plugins: 0`), MCP noise disappears, runtime drops to ~10s. **(Caveat, R1-F1):** testing used a *wide* `~/.grok` symlink — but `~/.grok` itself carries `memory/`, `sessions/`, `projects/`, `logs/`, `skills/`, `config.toml`, so a wide symlink leaks grok's own state. `grok inspect` even showed 12 residual skills under the wide symlink — a tell I under-weighted at test time. §3.3 mandates a NARROW auth-only allowlist instead.
2. **gemini refuses to start in an untrusted dir** without `--skip-trust` (EXIT=55). It also scans `~/.gemini/extensions` and loads `~/.gemini/GEMINI.md` (6.5KB context). Fix: `--skip-trust` + a **narrow** HOME sandbox that symlinks only auth files (`oauth_creds.json`, `google_accounts.json`, `projects.json`, `state.json`) — NOT `GEMINI.md` / `settings.json` / `extensions`.
3. **grok `--effort` breaks the default model** (`Model grok-build does not support parameter reasoningEffort`, HTTP 400). Drop `--effort`, or pin a model that supports it. We drop it.
4. **codex with split streams** puts the clean answer on stdout and the banner on stderr — simpler than the old single-consult `sed '^codex$ ↔ ^tokens used$'` hack.

Isolation rule (**corrected in R1 self-review — superseded the original "asymmetry" claim**): **both grok and gemini need a NARROW auth-only allowlist**, not a wide config-dir symlink. A non-real HOME fixes the `~/.claude` leak, but each tool's OWN config dir is itself context-bearing — `~/.gemini` holds `GEMINI.md`; `~/.grok` holds `memory/`, `sessions/`, `projects/`, `logs/unified.jsonl`, `skills/`, `config.toml`, marketplace/worktree caches. A wide symlink of either re-introduces a context leak AND can persist panel prompts/responses into the tool's real history/logs (violating consult's stateless metadata-only privacy contract). See §3.3 for the corrected per-tool allowlists. codex → flags only, no HOME trick.

Canary check: a `SECRET_CANARY.md` was planted in every test cwd. No model ever referenced it → filesystem isolation held in every run. **(Caveat, R1-F2 / R3-F1):** an unreferenced canary shows the model didn't *read* the file, not that it *couldn't*. The real isolation guarantee for all three is the **empty per-PID cwd + per-tool context isolation** — the *mechanism differs by tool* (R4-F1): codex uses `--ignore-user-config --ignore-rules --ephemeral` (flags, NO HOME override — it stays "flags only" per §3.3), while grok and gemini use an isolated HOME with a narrow auth-only allowlist. Separately on the read axis: codex `-s read-only` and gemini `plan` are read-only-WRITE, not no-read; grok additionally gets `--disallowed-tools` (true no-read). §5 splits the test into project-isolation (all 3, per-tool mechanism) + grok-only no-read.

### Round 2 — Diversity (is "three perspectives" real?)

Ran the panel on the same question across model triples and compared verdicts.

- **Abstract verdict questions (5):** k8s-for-hobby, build-your-own-auth, 100%-coverage-gate, monorepo@50, function-size. **5/5 full consensus** (15/15 individual verdicts agreed), often with near-identical reasoning. Verdict diversity ≈ 0 — even on questions picked specifically to be divisive.
- **Real consult questions (6, extracted from transcripts):** 3/6 showed a verdict split — but two (flags-vs-prompts, bug-#189) were **semantic consensus** split only by VERDICT-token formatting. One (q13, "be hostile, find holes in my perf diagnosis") was a **real** split where the three models found **three different holes**.
- **Generative / find-holes mode (real questions, "list the risks / what am I overlooking"):** **~50% of findings were unique to one model**, and the unique ones were regularly *killer*:
  - Q5 (docs triage): Gemini caught an arithmetic error (3+12+3=18 ≠ 23 findings, 5 unaccounted); Grok caught fork-upstream re-injection of drift.
  - Q9 (visual testing): Gemini — WindowServer focus-hijacking breaks dual-run; Grok — self-hosted CI = same physical Mac → "isolation" illusory; GPT — diff-mode-by-bug-class.
  - Q15 (git-date tests): Gemini independently named `GIT_CONFIG_GLOBAL=/dev/null` — **the literal real fix from issue #194** — confirming the uplift is not an artifact of my own analysis.

**The pivot:** verdict-mode diversity is near zero; **find-holes diversity is real and valuable**. Models also have distinct "review characters" — Gemini=detail/technical, Grok=systemic/meta, GPT=structure/taxonomy — which is *why* the uplift exists (genuine complementarity, not noise).

### Round 3 — Merge (how to combine three outputs?)

Tested Crís's idea: a summarizing agent with a hard prompt. A 4th isolated codex call, given the three raw critiques + a faithfulness-constrained prompt, reproduced the manual semantic merge of q9 **almost exactly**: correct `SHARED` core, correct `UNIQUE` attribution per model, invented nothing. Two independent merges (my manual analysis + the isolated summarizer) converged → the mechanism is reliable. Structured-output-from-models (the alternative idea) is unnecessary — the agent handles prose fine.

> Caveat (honesty): the "~50% unique" figure is **my semantic judgment**, not a strict metric — synonymous findings are matched by meaning, not string. It is anchored by objectively-checkable cases (the arithmetic miss; the `GIT_CONFIG_GLOBAL` match with the real #194 fix), so the direction is trustworthy even if the exact percentage is soft.

## 3. Design

### 3.1 Interface

```
/bulldozer:consult <question>                 → single codex (unchanged, ~4–15s)
/bulldozer:consult --panel <question>         → 3 models, FIND-HOLES mode (default for panel)
/bulldozer:consult --panel --verdict <question> → 3 models, VERDICT mode (opt-in)
```

`--panel` is strictly opt-in. The default single-codex path is untouched. Within `--panel`, **find-holes is the default** (where diversity pays off); `--verdict` is available for users who want the triad verdict as "expensive confirmation" (Crís chose to keep it optional despite the 0-uplift finding).

### 3.2 Architecture / Flow

Logic lives in a new Python script `skills/consult/scripts/consult_panel.py`. Rationale (Python over inline-bash or Claude-side orchestration, the two alternatives weighed): per-model isolation is non-trivial and *different* per CLI (three HOME setups, symlink management, three parsers, the `--effort` trap). That does not fit inline bash cleanly, and parallel bash with an exit-code contract is exactly the "leaky-by-default" pain that produced 17 leak sites in PR #111. Python gives clean `concurrent.futures` parallelism, deterministic per-model parsing, one place for isolation flags, and testability (precedent: `cdp.py` + `test_cdp.py`). The single-codex path stays inline in SKILL.md.

```
consult --panel [--verdict] <question>
  │
  ├─ 1. Build per-model isolated sandboxes under a per-PID tmpdir (§3.3)
  │       codex  → flags only (-s read-only, --ignore-user-config/--ignore-rules, --ephemeral)
  │       grok   → HOME=<sandbox>/grok, NARROW auth-only allowlist (auth+version+models_cache;
  │                NEVER whole ~/.grok — excludes memory/sessions/projects/logs/skills/config)
  │                + --sandbox + --permission-mode + --disallowed-tools (no file-read)
  │       gemini → HOME=<sandbox>/gem, NARROW auth-only allowlist + --skip-trust --approval-mode plan
  │
  ├─ 2. Wrap the prompt:
  │       find-holes (default): "list the most important holes/risks/things overlooked …"
  │       verdict (--verdict):  existing consult wrapper (GO/NO-GO/MINOR-FIXES)
  │
  ├─ 3. Run the 3 CLIs in parallel (concurrent.futures). Each in its own process,
  │     timeout 180s, stdin </dev/null, split streams. Per-model failure is isolated.
  │
  ├─ 4. Collect survivors: codex stdout · grok JSON .text · gemini JSON .response.
  │     A failed/timed-out model → "[<model>: failed — <reason>]" block rendered
  │     SEPARATELY (never fed to the summarizer). survivors = the successes.
  │
  ├─ 5. Merge (find-holes mode, §3.5-3.6) — gated on survivor count:
  │       ≥2 survivors → 4th isolated codex call (summarizer), N-aware prompt over the
  │                      SURVIVORS ONLY → "## SHARED (all N)" + "## UNIQUE" (per-reviewer tags)
  │       1 survivor   → print its raw block directly, NO summarizer
  │       0 survivors  → error (§3.6)
  │     (Whole step skipped in --verdict mode — see §3.4.)
  │
  └─ 6. Print: merged synthesis on top; raw survivor blocks + any failure blocks below.
       Cleanup the per-PID tmpdir.
```

### 3.3 Per-model isolation (verified commands)

Exactly as in §2 Round 1. The script owns sandbox construction:

- **codex:** `codex exec --skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral -s read-only -c model_reasoning_effort=<effort> "<wrapped>"` from the empty tmpdir, `</dev/null`, stdout captured (banner on stderr discarded). `<effort>` = `medium` for find-holes panel (speed; we run 3+1 calls), configurable.
- **grok** ⚠️ **SUPERSEDED — see §8:** the `--sandbox` / `--disallowed-tools` "no-read" controls in this bullet proved unachievable on macOS and were dropped; grok ships soft-no-read like codex/gemini. The shipped command is `--no-memory --no-subagents --disable-web-search --permission-mode plan --output-format json`. Original design intent preserved below. **(R1-F1/F2 — NARROW + hard-isolated):** `HOME=<sandbox>/grok grok -p "<wrapped>" --no-memory --no-subagents --disable-web-search --sandbox <fs-read-restricted/no-network profile> --permission-mode plan --disallowed-tools <file-read + shell tools> --output-format json </dev/null`. Sandbox: `mkdir <sandbox>/grok/.grok` then symlink **only the minimal auth/start files** (`auth.json`, `auth.json.lock`, `version.json`, `models_cache.json`) — deliberately **EXCLUDE** `memory/`, `sessions/`, `active_sessions.json`, `projects/`, `logs/`, `skills/`, `config.toml`, `marketplace-cache/`, `worktrees.db`. **No `--effort`** (breaks grok-build). Parse `.text`. **`read-only` ≠ `no-read` (R1-F2, round 2):** a read-only sandbox + `plan` mode block WRITES but still permit file READS/search — so the canary defense rests on `--disallowed-tools` stripping grok's file-read/shell tools (exact tool names from `grok --help`/docs, pinned in implementation), not on the sandbox/plan flags alone. **Implementation MUST validate**: (a) `grok inspect` under the sandbox shows `Project Instructions: 0`, `Permissions: 0`, zero user-scope skills, no sessions/memory; (b) the §5 hostile-canary test — plant a secret in cwd, prompt grok to read it via its file tool, assert it CANNOT. grok exposes `--sandbox <PROFILE>`, `--permission-mode <MODE>`, `--disallowed-tools <TOOLS>`; the original spec relied on prompt obedience alone — a process-isolation gap vs codex `-s read-only` / gemini `--approval-mode plan`.
- **gemini:** `HOME=<sandbox>/gem gemini -p "<wrapped>" --skip-trust --approval-mode plan -e none -o json </dev/null`. Sandbox: `mkdir <sandbox>/gem/.gemini` then symlink **only** `oauth_creds.json`, `google_accounts.json`, `projects.json`, `state.json` — deliberately NOT `GEMINI.md` / `settings.json` / `extensions/`. Parse `.response`. (gemini carries a ~9K-token baseline tools-schema regardless — accepted, not fixable.)

Wall-clock: ~12–19s for the parallel triad (gemini is the slowest leg — the `~19s` in §2 is its upper bound; the leg varies with the question/reasoning) + ~5–10s for the summarizer = **~17–30s per panel invocation, 4 model calls total** (typical). The summarizer runs serially *after* the triad, so the **worst case approaches 2×`timeout`** (default 180s) if a model nears its cap. This is intentionally heavier than single-consult (~4–15s); the `--panel` opt-in is the consent.

### 3.4 Prompt wrappers

**Find-holes (default):**
```
SKIP SKILLS. Do not inspect files or run tools. Text-only critique.
---
<user question, verbatim>
---
SKIP SKILLS. List the most important holes, risks, or things being overlooked in the
above. Be specific and concrete. Number each as a one-line point. Max 8 points.
```

**Verdict (`--verdict`):** the single-consult wrapper, **updated (R3-F2) so its required output line matches the §3.7 classifier exactly** — it must end with one anchored standalone line `VERDICT: GO` / `VERDICT: NO-GO` / `VERDICT: MINOR-FIXES` (uppercase, the form the parser accepts), replacing the old free `Verdict: GO / NO-GO / MINOR-FIXES` text. Run per model. The merge step is a simple per-model verdict line (`GPT=X · Grok=Y · Gemini=Z`) + the bodies — **no summarizer** (verdicts don't need semantic dedup). This wrapper change also lands in `skills/consult/SKILL.md` (the existing single-consult wrapper, Step 3 + Quick Invoke template); a structural test asserts the wrapper's required line matches the classifier regex so the prompt and parser contracts never drift (R3-F2).

### 3.5 Merge via summarizer agent (find-holes mode)

A 4th isolated codex call (`-s read-only`, same isolation flags, `medium` effort), run **only when ≥2 models succeeded** (R1-F4 — a "merge" of one critique is meaningless). Its input is the **successful critiques ONLY** — failure blocks are never fed in. The prompt is **N-aware** (substitutes the actual count and reviewer names; verified faithful in Round 3 at N=3):

```
You are merging the N independent critiques below (each block is labelled with its
reviewer) of the SAME proposal. Produce ONE deduplicated findings list. Rules: (1) each
distinct finding = one line. (2) Prefix [ALL] if EVERY supplied reviewer raised it;
[<REVIEWER>] if unique to one; [<R1>+<R2>] for a subset. (3) Section order: '## SHARED
(all N)' then '## UNIQUE'. (4) BE FAITHFUL: do not invent any finding not present in the
inputs; do not add your own opinions. The N critiques (N = <count>, reviewers = <list>):
<successful critiques only>
```

The summarizer is itself a codex call → subject to the same parsing/availability handling. If it fails, fall back to printing the raw survivor blocks with a note (the unique value is still there, just unmerged).

### 3.6 Error handling

- **Per-model failure** (auth, rate-limit, timeout, non-zero exit): caught per future, rendered as `[<model>: failed — <short reason>]`, panel continues with the survivors. Never fail the whole panel for one model.
- **Survivor count drives the merge (R1-F4):** **3 or 2 survivors** → summarizer merges them (N-aware prompt, §3.5); **exactly 1 survivor** → print its raw block directly, NO summarizer; **0 survivors** → error (below). Failure blocks render separately under the merge, NEVER inside summarizer input.
- **Zero models succeed:** print a clear error + the captured stderr tails; do not fabricate a merge.
- **Summarizer failure:** degrade to raw survivor-block output.
- Every model call: `timeout 180s`, `</dev/null`, split streams, per-PID tmpdir cleaned on exit.

### 3.7 Single-consult parsing fix (in scope, per Crís)

Independent of the panel but bundled here. On long real questions, codex frequently answers with a prose recommendation and **no `VERDICT:` line** (observed: q4/q8/q15 in testing). The current single-consult fail-closed maps "no token" → NO-GO, turning a substantive answer into a false block.

**Fix — an exact classifier** (R1-F3; replaces the current bare-token match that misfires on incidental prose):
1. **Verdict syntax:** only an anchored line matching `^\s*VERDICT:\s*(GO|NO-GO|MINOR-FIXES)\s*$` (case-insensitive) counts as a verdict. Incidental prose containing "go"/"no-go" is ignored — this also fixes the *existing* misclassification where a sentence like "it's a go-to pattern" tripped the old matcher.
2. **Precedence** (multiple anchored lines): NO-GO > MINOR-FIXES > GO. ⚠️ **SUPERSEDED (§8):** shipped code uses chronological finality (the FINAL anchored line wins), not this precedence — an echoed option earlier no longer forces a false NO-GO.
3. **No anchored verdict line:** strip the CLI banner/footer, then — if the remaining body has ≥1 non-empty line of ≥3 words → `INCONCLUSIVE`; else (empty, whitespace-only, or pure error banner) → `NO-GO` (fail-closed preserved for real failures).
4. **`INCONCLUSIVE` is NOT a 4th verdict downstream:** single-consult prints the prose + prompts the user to re-ask for a crisp verdict; it is **excluded** from the round≥3 / 2×-NO-GO escalation trigger (`INCONCLUSIVE ≠ NO-GO`, so it never counts toward escalation); in panel `--verdict` mode it renders as the per-model label `INCONCLUSIVE` (panel verdict mode only lists per-model labels — no aggregation, so no special handling needed).

This keeps fail-closed for real failures while not punishing valid prose, and adds no state the rest of consult must track.

## 4. What we DON'T do (YAGNI)

| Anti-feature | Why |
|--------------|-----|
| Per-model sub-model selection (gemini-flash vs -pro, etc.) | Use each CLI's default model. Add later if needed. |
| Verdict aggregation (majority / worst-case voting) | Empirically 0 uplift; and Crís chose "show side by side" originally. Verdict mode just lists the three. |
| Structured (JSON-findings) output from the models | Round 3 proved the prose summarizer is enough. |
| Persistent / resumable panel sessions | Same retention/contamination reasons consult is stateless. |
| Logging prompt/verdict bodies | Metadata-only, same privacy property as single-consult. |
| Making `--panel` the default | Heavier (4 calls, ~17–30s); diversity only pays in find-holes on concrete questions. Opt-in. |

## 5. Testing

Follows the `cdp.py` / `test_cdp.py` precedent (structural + behavioral split).

- **Structural** (`tests/test_consult_panel.py`, fast, offline): sandbox construction (**narrow allowlists for BOTH grok and gemini** — assert no `memory`/`sessions`/`projects`/`skills`/`config.toml` symlinked, only the auth/start files), wrapper builders (find-holes vs verdict, **N-aware summarizer prompt** at N=2 and N=3), output parsers (codex stdout / grok `.text` / gemini `.response`), graceful-degradation + **survivor-count paths** (3/2 survivors → summarizer; **1 survivor → raw, no summarizer**; 0 → clean error; summarizer fails → raw fallback), the parsing-fix classifier (anchored `VERDICT:` only; incidental "go-to" prose ignored; prose → INCONCLUSIVE; empty/banner-only → NO-GO; INCONCLUSIVE excluded from escalation count).
- **Behavioral** (opt-in, marked `slow`, needs real CLIs + auth): one real `--panel` find-holes run end-to-end (assert N blocks + a merged section); plus isolation tests (R3-F1, two distinct guarantees): **(a) project-isolation, all 3 models** — assert no model surfaces project-file content; holds *by construction* via per-tool context isolation (NOT one shared mechanism): codex by `--ignore-user-config --ignore-rules --ephemeral` from an empty cwd (flags, no HOME trick); grok/gemini by isolated HOME (narrow allowlist) + empty cwd. The test plants a project marker OUTSIDE the model's cwd and asserts no model surfaces it through inherited context — it does NOT assert codex has an isolated HOME (codex is flags-only by design, §3.3). **(b) grok no-read, grok-only** ⚠️ **SUPERSEDED (§8): NOT ported** — grok hard-no-read is unachievable on macOS, so this hostile-canary test does not exist; grok is soft-no-read like the other two. *(Original intent:)* plant a secret IN the empty cwd, prompt grok to read it via its file tool, assert `--disallowed-tools` blocks it. The stronger no-read check is grok-only **by design**: codex `-s read-only` and gemini `--approval-mode plan` are read-only-WRITE (they may read a cwd-local file), so their isolation contract is (a) the empty cwd, not (b). Gated like `test_check_e2e.py` (skip if codex/CLI/auth is missing).
- **No model ships without a structural test** (mirrors the cdp.py rule in CLAUDE.md).

## 6. Open risks → validate in implementation

1. **CLI auth drift / rate limits** — grok & gemini need their own logins; a panel run is 4 model calls. Behavioral test must skip-guard on missing auth.
2. **Default model drift** — gpt-5.5 / grok-build / gemini-3.1-pro-preview were the defaults on 2026-06-02; CLIs will move. Don't hardcode model IDs as eternal truth; read each CLI's default.
3. **Summarizer faithfulness at scale** — verified on one rich case (q9). Watch for invented findings on others; the hard prompt + `-s read-only` + the "BE FAITHFUL" constraint are the guardrails. If drift appears, fall back to raw blocks. The summarizer is itself a codex call — a theoretical bias toward GPT-authored findings; Round 3 did not show it (Grok/Gemini uniques were preserved with correct attribution), because the agent dedups/attributes rather than judging value, but the raw blocks are always shown below so any dropped finding is recoverable.
4. **gemini ~9K-token baseline** — every gemini call carries its tools-schema; panel cost is dominated by it. Accepted.
5. **`~50% unique` is a soft metric** — re-confirm informally during real use; the feature's worth tracks this number.

## 7. File changes

- **New:** `skills/consult/scripts/consult_panel.py` (panel orchestration + isolation + merge).
- **New:** `tests/test_consult_panel.py` (structural) + a `slow`-marked behavioral case — including the wrapper-line-matches-classifier-regex contract test (R3-F2) and the two isolation tests (R3-F1).
- **Modified:** `skills/consult/SKILL.md` — add `--panel` / `--verdict` to argument-hint + a "Panel mode" section (when to use: find-holes / "what am I missing", not verdict); document the parsing fix in Step 5; **update the verdict wrapper's required output to the anchored `VERDICT: <X>` line the classifier accepts (R3-F2)** — this is a behavior change to the existing single-consult verdict prompt, in scope per the bundled parsing fix.
- **Modified:** `CLAUDE.md` (bulldozer) — Architecture: consult gains an opt-in multi-model find-holes panel; note the empirical basis (verdict diversity ≈ 0, find-holes diversity real).

## 8. Implementation deltas (2026-06-02)

Reality found during TDD implementation + two dogfood rounds (the informed `--panel --repo` panel reviewing its own code). §1–§7 are preserved as built-intent; these are the corrections.

**grok hard-no-read is unachievable natively on macOS — dropped.** §3.3's `--sandbox` / `--disallowed-tools` no-read controls do not work: grok's sandbox model (like codex) governs WRITE + NETWORK, not read; macOS has no seccomp (Linux-only); and `--disallowed-tools` is whack-a-mole (grok reads via read_file/grep/shell/search_tool, and read_file can't be removed without crashing the agent). All three models are therefore **soft-no-read** (empty cwd + prompt + HOME isolation) — the level §3.3 already conceded for codex/gemini. A split-test confirmed models don't read spontaneously on normal questions (a `.env`+secret canary in cwd, full tools → untouched; leaks only on an explicit "read X"). grok command simplified to `--no-memory --no-subagents --disable-web-search --permission-mode plan --output-format json`. (systematic-debugging, 5 approaches.)

**`--repo` informed mode added.** Split-test: for repo-specific questions informed (models read the real code) ≫ isolated (codex honestly refuses "no code — send the file"); for abstract questions, a tie. So `--panel --repo <path>` is opt-in informed find-holes (+ `--verdict --repo` informed verdict via `wrap_verdict_repo`). Privacy: code reaches the cloud reviewers only when `--repo` is passed. This is the deliberate exception to the consult artifact-ban.

**§3.7 precedence → chronological.** `classify_verdict` returns the FINAL anchored VERDICT line, not set+precedence — an option echoed earlier on its own line no longer forces a false NO-GO (dogfood R2).

**Dogfood-driven hardening (2 rounds, 13 findings fixed via TDD):** per-model tempdir (no cross-model `../auth` reach), env allowlist (essentials + provider keys only; secrets / PWD / session dropped), auth copied not symlinked, `_run_one` error-guarded (per-model failure not panel crash), `--repo` preflight-validated, `main` top-level guard + non-zero exit on total failure, lenient JSON candidate-scan (banner braces tolerated), model cwd separated from sandboxes.

**Post-ship #142 cleanup (2026-06-03, TDD).** The deferred timeout-orphans + P2 polish, all behavior-preserving except where noted: per-model `_MODEL_SPECS` registry + unified `wrap(question, *, verdict, repo)` 2×2 table (collapse the per-model/per-mode duplication — adding a model/mode is one row); per-call **nonce** in the summarizer block delimiter (`=== <reviewer> <nonce> ===`) so a critique body can't spoof a reviewer boundary; typed `LegResult` (`output` XOR `reason`, built via `.ok()`/`.failed()`) so a failure can't lose its reason; ANSI-sanitized stderr/parse tails + a parse-failure output snippet; a "merge step failed" note when the summarizer dies with ≥2 survivors; and `run_model` reaping the whole process group on timeout (`start_new_session=True` + `os.killpg`) so model helper processes aren't orphaned. The ThreadPoolExecutor-thread-on-`Ctrl-C` orphan stays deferred (a distinct interactive-interrupt concern, not the timeout path).

**grok's HOME-sandbox removed → grok runs on the REAL HOME (2026-06-03). SUPERSEDES the §3.3 grok narrow-allowlist sandbox + the "HOME isolation" half of the soft-no-read note above.** The narrow auth-only HOME-sandbox for grok was both *broken* and *illusory*: in informed (`--repo`) mode grok must spawn a tool-worker to read files, and that worker fails auth inside the sandbox HOME (`Auth(AuthorizationRequired)`, `Transport channel closed`) → grok cancelled with empty text on **every** `--repo` run (the ce3f2f18 symptom). Empirically (systematic, n≫1, NOT one run): real HOME → grok survives **3/3** through the panel on `--repo` *and* isolated; the panel's copied-auth sandbox → **0/3**. The sandbox was leaky regardless (grok wrote to the real `~/.grok/active_sessions.json` despite the `HOME` override). grok auth is OAuth (`~/.grok/auth.json`, `auth.x.ai`), not an env key (`XAI_API_KEY` unset), so file-copying the 4 auth files never reproduced real auth anyway. **Fix:** drop grok's sandbox — `build_grok_cmd` returns no `HOME`/`XDG` override, so the real HOME is inherited via `run_model`'s env allowlist (which still strips arbitrary secrets). Isolation for grok now rests on `--no-memory`/`--no-subagents` (+ `--repo` already sends the code to the cloud). Removed `build_grok_sandbox` + `_GROK_ALLOWLIST`; **gemini keeps its sandbox** (it survives `--repo` sandboxed). *Three earlier hypotheses were refuted on the way (plan-mode blocks reads; missing allowlist files; plan vs always-approve) — each by one experiment too few; the real signal was only ever real-HOME-reliable vs sandbox-flaky.*

**Tests:** `tests/test_consult_panel.py` — offline (pure functions + orchestrator via injected runner + SKILL.md reachability/classifier guards; `pytest --co -q | tail -1` for the live count). Full-panel e2e (real CLIs) validated by the live dogfood runs; not yet a `@pytest.mark.slow` case (one real-subprocess timeout-reaping test does run in the default suite).

---

*This design is the terminal artifact of a brainstorm whose key decisions were each settled by an empirical test, not by argument. The tests are reproducible from the commands in §2–§3. §8 records where implementation + dogfood corrected the design.*
