# consult: per-model selection + opt-in `--web` deep research — design

**Date:** 2026-06-21
**Status:** Design approved (brainstorm), pre-implementation
**Supersedes/expands:** A3IO/jaine-plugins#219 (the original ask was narrowly "opt-in web for the panel"; this design is a superset)
**Skill:** `bulldozer:consult` (`--panel` engine = `skills/consult/scripts/consult_panel.py`)

## 1. Motivation

Two capabilities, requested together:

1. **Per-model selection.** Today the only single-model path is **codex** (the inline path in `SKILL.md`); `--panel` is hard-wired to all three (codex+grok+agy). grok and agy cannot be run individually at all. The user wants to run **any one model alone** (codex / grok / agy), any subset, or all — uniformly.
2. **Opt-in `--web` deep research, per model or blanket.** Today consult is isolated from the network by design (#189 no-egress: codex isolated by flags, grok `--disable-web-search`, agy hook-allowlist has no web tool). The user wants an opt-in flag that lets selected models do **industrial-scale web research** (multiple searches, their own subagents) so a design question gets both find-holes diversity AND live community practices in one shot — without leaving the consultation for a separate research tool (#219).

## 2. Goals / Non-goals

**Goals**
- Per-model selection: codex / grok / agy individually, any subset, or all.
- `--web` (blanket) and `--web <list>` (per-model) enabling deep web research.
- `--web` is READ-side autonomy only (web search/fetch + subagents), never write/shell — so even `--web --repo` cannot mutate the repo.
- Reuse existing output machinery (synthesis-on-top summarizer); handle the large research volume without overflowing context or the summarizer prompt.
- Persist `--web` raw research for drill-down in a standard, git-clean location.
- Backward compatibility: bare `consult "Q"` and `--panel` keep working.

**Non-goals**
- No guard/block on the `--web` + `--repo` combo. The user explicitly accepted the egress risk ("можно и небезопасно"). We do NOT build a warning gate; we only refuse WRITE autonomy (a gratuitous, no-upside risk).
- No new `deep-research` skill and no dedup against one — none exists in the repo; "deep-research" in #219 was generic ("going off to research separately"). The goal is to fold research INTO consult.
- No persistence for non-`--web` consult — default consult stays no-trace (existing `/tmp` + `rm -rf`).

## 3. CLI surface (grammar)

Selection — boolean per-model flags (presence selects):

```
consult "Q"                  → codex            (default, unchanged — see §8 D1)
consult --grok "Q"           → only grok
consult --agy "Q"            → only agy
consult --codex --grok "Q"   → codex + grok
consult --panel "Q"          → all three        (alias = --codex --grok --agy; backward-compat)
```

Web — blanket or scoped:

```
consult --grok --agy "Q"              → both, NO web (isolated, #189)
consult --grok --agy --web "Q"        → both, web
consult --grok --agy --web grok "Q"   → web only grok; agy isolated
consult --panel --web codex,grok "Q"  → web codex+grok; agy isolated
```

With repo (egress-sensitive, allowed):

```
consult --grok --repo . "Q"            → grok reads real code, NO web
consult --grok --repo . --web grok "Q" → grok reads code AND web (reverses #189 — opt-in, accepted)
```

Existing `--verdict` / `--timeout` / `--repo` compose with all of the above.

### 3.1 argparse safety (`--web` must not eat the question)

`--web` is `nargs="?"`, `const="__ALL__"`, `default=None`. **Empirically confirmed (argparse probe, 2026-06-21):** with a required positional `question`, a bare `--web` placed BEFORE the question eats it — `consult --grok --web "Q"` → argparse binds `"Q"` to `--web`, then errors "question required". Only two forms are safe:
- **`--web=grok,agy`** (equals form) → the list, question untouched. ✓ — **this is the canonical scoped form.**
- **bare `--web` placed LAST** (`consult --grok "Q" --web`) → `"__ALL__"` (blanket). ✓

Therefore the **SKILL.md routing layer always emits the `=` form** — scoped as `--web=m1,m2`, blanket as `--web=<all-selected-models>` — so the positional-eating footgun is never triggered in the real call path (consult_panel.py is invoked by Claude, not typed by humans). A `--web` value that is neither `__ALL__` nor a comma-list of known model names → fail-loud `parser.error` (no silent misparse). A structural test documents the bare-`--web`-before-question rejection so a future change can't silently "fix" it into an ambiguity.

## 4. Per-model `--web` mechanics (empirically grounded — see §10)

| Model | Isolated (default) | `--web` |
|-------|--------------------|---------|
| **codex** | `-s read-only --ephemeral` + isolation flags | **+ `-c web_search="live"`** (canonical top-level key per OpenAI config-reference; `--search` is NOT valid for `codex exec`; `tools.web_search` is the deprecated legacy alias — §10) → native `web_search` tool, **live** (not cached) results, multiple queries within one turn. Isolation flags preserved (`--ignore-user-config` etc. stay). No subagents — depth = agentic web loop. |
| **grok** | `--no-memory --no-subagents --disable-web-search --permission-mode plan` | **drop `--no-subagents` + `--disable-web-search`; KEEP `--permission-mode plan`** → web search/fetch + parallel subagents, **read-only** (plan blocks writes). `--no-memory` stays. |
| **agy** | hook ALLOW = local-code reads only; `search_web`/`read_url_content`/`run_command` DENY | **ALLOW += `search_web`, `read_url_content`**; `run_command` and all write/exec tools stay DENY (read-side only). |

`--web` never grants write/shell autonomy for any model. `bypassPermissions`/`dontAsk` are NOT used for grok — `plan` already runs web + subagents and is read-only (empirically verified, §10).

## 5. Security model

- **Default = unchanged.** No model flag with web off → byte-identical to today's isolation (#189 no-egress holds).
- **`--web` reverses #189 no-egress** for the selected models, by design and opt-in (same posture as `--repo`: explicit, off by default).
- **READ-side only.** `--web` enables web fetch/search + subagents (reading), never write/shell. So `--web --repo` (worst case: model sees real code AND has a web channel) can leak/egress but **cannot modify the repo**. The user accepted egress; we still deny mutation because it has no research upside.
- **agy enforcement stays fail-closed:** the existing PreToolUse deny hook keeps its exact-name allowlist; `--web` only ADDS `search_web` + `read_url_content` to that allowlist. Everything not explicitly allowed (incl. `run_command`, writes, unknown/malformed) still denies.

## 6. Output handling (`--web` volume)

Empirically, one grok web run with subagents produced ~94 KB; ×3 models inline would blow context and overflow the summarizer prompt (§10).

Reuse the existing synthesis-on-top machinery (`decide_merge` → isolated summarizer-codex merge; `render_panel`), with two `--web`-only deltas:

1. **Per-model pre-compress before merge.** Each model's raw research → a condensed digest (key findings + URL citations) via a compression pass, BEFORE feeding the existing merge-summarizer. Fixes (a) summarizer-prompt overflow and (b) grok's parallel-subagent output corruption (re-synthesis from the messy raw into clean text).
2. **Raw goes to files, not inline.** In `--web` mode, `render_panel` shows the synthesis + the per-model condensed digests inline, and writes the full raw research to the bundle (§7) instead of the inline `## Raw critiques` block.

Non-`--web` output is unchanged.

## 7. `--web` file bundle

Persist raw research only in `--web` mode (default consult stays no-trace).

- **Location:** `.bulldozer/consult-<ts>/` (cwd-relative, the consumer project root — same convention as `check`'s `.bulldozer/<session>-<artifact>/`).
- **Git-clean:** ensure a self-ignoring `.bulldozer/.gitignore` containing `*` (idempotent; identical pattern to `check` Step 1c and `.remember/`) so contents never touch the consumer's project `.gitignore` and never enter git.
- **Format — unified markdown bundle (always the same layout):**
  ```
  .bulldozer/consult-<ts>/
    research.md      # synthesis (also shown inline) + per-model condensed digests + a consolidated URL/source index
    raw-codex.md     # full raw research, per model (only models that ran with web)
    raw-grok.md
    raw-agy.md
  ```
- **Lifecycle:** auto-prune keep-last-N (N=10) consult-`<ts>` dirs at start of each `--web` run (bounded growth; gitignored). `<ts>` is a sortable timestamp.

## 8. Unification decision

**D1 — bare `consult "Q"` keeps the inline single-codex path; explicit selection routes through the engine.**

- Bare `consult "Q"` (zero model flags, no `--panel`) → **unchanged** inline single-codex conversational path in `SKILL.md` (the cheapest path; preserves today's decisive-verdict UX and backward-compat).
- Any explicit `--codex` / `--grok` / `--agy` / `--panel` → `consult_panel.py` engine, which now accepts 1..3 models (it already degrades cleanly: `decide_merge` returns `raw` for a single survivor — no summarizer for N=1).

Rationale: this satisfies "run any model alone" (grok/agy/codex are all first-class via explicit flags) while keeping the ultra-light default path. The engine becomes the single machinery for any *explicit* selection; only the bare default stays inline, for cost. (Alternative considered: route bare consult through the engine too — rejected to avoid spawning the orchestrator for the common cheap case and to avoid reconciling the conversational-vs-find-holes default mode for the bare case. Revisit if the inline/engine split causes drift.)

**Mode orthogonality.** Model selection is orthogonal to output mode. The engine keeps its modes: find-holes (default) and `--verdict`. A single explicitly-selected model runs in the chosen mode (e.g. `consult --grok --verdict "Q"`).

## 9. Timeout

`--web` research runs long (grok subagent run = ~176 s; §10). The current `--timeout` default is 180 s — borderline. In `--web` mode, raise the effective default to ~600 s (per model), still overridable by `--timeout`. Non-`--web` default stays 180 s.

## 10. Empirical basis (probes, 2026-06-21, `/tmp/bz-web-probe*`)

- **codex** `--search` is NOT valid for `codex exec` (top-level flag only — probe got "unexpected argument"). Two config keys fire the native `web_search` tool, both verified WITH full isolation (`--ignore-user-config --ignore-rules --ephemeral -s read-only`):
  - **`-c web_search="live"`** — the DOCUMENTED canonical key (OpenAI config-reference: `web_search` = `"disabled"|"cached"|"live"`, default `cached`). Probe: 12 `web search:` markers, fresh real URLs (rfc9333, datatracker), **0 config errors**, 38 s. **This is what we ship.**
  - `-c tools.web_search=true` — the deprecated legacy alias ("Deprecated legacy toggle; prefer the top-level `web_search` setting"). Also fires on 0.141 (logged queries, real URLs from rfc-editor/envoyproxy/AWS/kong/redis), but we do NOT ship it.
- **grok** in **`--permission-mode plan`** (read-only), with `--disable-web-search`/`--no-subagents` dropped: **spawned 5 parallel subagents** (model self-reported: *"I spawned 5 subagents in parallel (using the `Task` tool with `generalPurpose` subagent type)"*), 73 URL citations, ~176 s, ~94 KB. `plan` mode is sufficient AND read-only — no `bypassPermissions` needed. Caveats: (a) some non-fatal `Auth(AuthorizationRequired)` worker churn in stderr; (b) at ~94 KB the final text showed interleaving/corruption from concurrent subagent writes → motivates per-model re-synthesis (§6).
- **agy** web tool names discovered via the deny-log: **`search_web`** and **`read_url_content`** (both DENY = wanted but blocked); `run_command` also attempted (kept DENY). With these added to ALLOW, agy gets web reads.
- **Volume/timing** drove §6 (output handling) and §9 (timeout).

**Online validation (2026-06-21, web research — dogfooding this very feature):**
- OpenAI config-reference confirms `web_search="live"` is canonical and `tools.web_search` is deprecated → spec ships the canonical key (caught a real version-fragility hole in the first draft).
- Grok Build docs confirm plan mode "blocks write tools except the session plan file" (read-only) and runs research/implementation/review subagents in parallel — matches the probe and §4/§5.
- Antigravity CLI docs + open issue google-antigravity/antigravity-cli#45 confirm `agy -p` auto-approves ALL tools incl. `write_file` with NO native read-only/plan equivalent → the PreToolUse deny hook is the ONLY enforcement, validating §5's fail-closed agy posture.

## 11. Testing plan

Offline/structural (default suite — no network):
- argparse: per-model flags select the right set; `--panel` == all three; bare → codex (D1 routing).
- `--web` parsing: bare `--web` → blanket; `--web=grok,agy` → scoped; `--web` does not consume the positional question; unknown model in `--web` list → fail-loud.
- Per-model command construction with `--web`: codex gains `-c web_search="live"` (canonical); grok drops `--no-subagents`/`--disable-web-search`, keeps `--permission-mode plan`; agy hook ALLOW gains `search_web`+`read_url_content` and still denies `run_command`/writes/unknown.
- Bundle: `.bulldozer/consult-<ts>/` layout, self-ignoring `.gitignore` created, keep-last-N prune, raw→file in `--web` (not inline), inline unchanged for non-`--web`.
- Pre-compress path invoked only in `--web`; merge unchanged for non-`--web`.

Dogfood-only (live, opt-in):
- A real `--web` run per model verifying web fires and the bundle is well-formed (mirrors §10 probes).

## 12. Risks / open items

- **grok subagent auth churn** — non-fatal in probes, but if it worsens, the swarm could degrade. Surfaced, not blocked.
- **codex web_search key** — we ship the DOCUMENTED canonical `web_search="live"` (not the deprecated `tools.web_search`); both verified to fire on 0.141. A structural test asserts the canonical flag is constructed; the dogfood run catches real breakage. Drift risk is low now that we're on the non-deprecated key, but `-c` quoting (`web_search="live"` is a TOML string) must be exact — the structural test pins the literal.
- **Pre-compress cost** — adds one compression pass per web model (a cheap codex/model call). Acceptable for the volume it tames.
- **`--web` egress is real** — documented in SKILL.md ("Web lane") so a consumer adopts it knowingly.
