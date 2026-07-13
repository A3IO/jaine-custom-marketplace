---
name: workflow-swarms
description: ALWAYS invoke when building or authoring a Workflow tool script, fanning out subagents, orchestrating agent swarms, spawning parallel/pipeline agents, running parallel codex jobs, or working under ultracode. Triggers on "build a workflow", "fan out agents", "оркеструй", "разбей на агентов", "parallel agents", "subagent swarm", "workflow", "ultracode", "параллельные codex". Covers per-role model routing (Haiku for grep/search/extraction, Sonnet for verify/judge/review, explicit Opus for synthesis), throttling to avoid 529 overload, token-budget scaling, concurrency caps, and codex fan-out via the lane pool (one subagent per mcp__codex-lane<N> — the shared plugin bridge serializes and rejects concurrent calls). Do NOT let agents inherit the session model — under a Fable session (Mythos tier, 2× Opus price) omitted model = fable everywhere. Route by role, set synthesis explicitly.
---

# Workflow Agent Swarms — Routing & Throttling Doctrine

How to build cost-efficient, overload-safe multi-agent swarms with the **Workflow tool** (`agent()` / `parallel()` / `pipeline()`).

## Core rule

**Fan-out agents inherit YOUR session model by default — and with the Mythos tier (Fable 5) that inheritance costs 2× Opus.** Route every agent to the cheapest model that does its job; NEVER leave `model` unset in a Fable session unless you explicitly want Mythos-class reasoning in that one agent.

Empirical (2026-05-30): three RE workflows burned ~514k+798k+729k output tokens on **Opus** subagents that were only grepping a binary. Haiku would have done it. Heavier models are also token-hungrier and slower, which *amplifies* burst-overload. Under Fable inheritance the same mistake costs 2× more.

## Pricing (docs-verified 2026-06-12)

| Model | $/MTok in | $/MTok out | vs Fable |
|---|---|---|---|
| Fable 5 (`fable`) | $10 | $50 | 1× |
| Opus 4.8 (`opus`) | $5 | $25 | 2× cheaper |
| Sonnet 4.6 (`sonnet`) | $3 | $15 | ~3.3× cheaper |
| Haiku 4.5 (`haiku`) | $1 | $5 | 10× cheaper |

Source: <https://platform.claude.com/docs/en/about-claude/models/overview>

## Routing by role

| Agent role | Model | Why |
|---|---|---|
| grep / search / extraction / lookup / read-only | **haiku** | no reasoning; 10× cheaper than Fable, 5× than Opus |
| verify / judge / review / classify | **sonnet** | needs judgment; ~3.3× cheaper than Fable |
| synthesis / planning / hard reasoning | **opus — set EXPLICITLY** | reasoning is the bottleneck; omit = inherit = fable in a Fable session (2× the price for usually no gain) |
| Mythos-class reasoning genuinely required | **fable** (alias exists, CC 2.1.170+) | only when the single hardest synthesis step measurably needs it |

Aliases (`haiku`/`sonnet`/`opus`/`fable`) are stable across CC versions — use them, not pinned model IDs. The old habit "synth: omit → inherit" was written when sessions ran Opus; it silently upgraded to Fable when Mythos sessions appeared. **Set synthesis to `opus` explicitly.**

### Fable-era community consensus (researched 2026-06-13)

- **Fable = orchestrator, never worker.** Anthropic positions Fable as "significantly more dependable at dispatching and sustaining parallel subagents"; in our workflows the MAIN LOOP already is the Fable orchestrator — fan-out agents don't need it. Reference numbers from the field: 12-worker audit all-Fable $14.50 vs Fable-orchestrator+Haiku-workers $3.70 (−74%); routed sessions ≈ −51% vs uniform.
- **Fable in a subagent is justified only for**: long-horizon autonomy (50+ steps — fewer human interventions), >500k-token contexts (Opus caps at 500k), or PhD-grade reasoning where GPQA-level deltas matter. NOT for extraction/classify/summarize/high-volume.
- **Shallow fan-outs (<5 agents, obvious plan): an all-Sonnet fleet** is often cheaper and faster than adding heavyweight synthesis at all.
- **Fable refusals**: benign cybersecurity / life-sciences tasks may be refused by Fable's safeguards — route those workers to `opus` deliberately.

## How to set the model — two levers

**Preferred: `opts.model` per call** (keeps per-role routing):
```js
agent(prompt, { model: "haiku", schema: FINDINGS })   // extractor
agent(prompt, { model: "sonnet", schema: VERDICT })   // verifier
agent(prompt, { model: "opus", schema: REPORT })      // synth: EXPLICIT — omit would inherit fable
```

**Global: `export CLAUDE_CODE_SUBAGENT_MODEL=haiku`** — pins ONE model for ALL agents.
⚠️ The env var is checked **first** and **beats `opts.model`** — was binary-verified (2.1.158), now also docs-confirmed: resolution order is env var → per-invocation `model` → frontmatter → session model (<https://code.claude.com/docs/en/model-config.md>). So the env var **kills per-role routing**. Use it only when you want "everything cheap"; for mixed swarms use `opts.model`.

Community gotchas (2026-06-13): the override is **silent** — no transcript signal, no warning (routing "didn't work"? `echo $CLAUDE_CODE_SUBAGENT_MODEL` first); and it's **inconsistent** — built-in agents (Explore, Plan, …) ignore it (anthropics/claude-code#25546).

## Canonical pipeline (route per stage)

```js
// extract (haiku) → verify (sonnet) → synthesize (opus)
const found = await pipeline(items,
  it => agent(`grep/extract: ${it}`, { model: "haiku", phase: "Extract", schema: FINDINGS }),
  rev => parallel(rev.findings.map(f => () =>           // ⚠️ this verify fan-out bursts at scale
    agent(`verify: ${f}`, { model: "sonnet", phase: "Verify", schema: VERDICT })))
);
// ⚠️ The plain parallel() verify above is fine for a handful of agents. Once a run totals
// many dozens of schema-agents (especially sonnet/opus), it 529s — the concurrency cap limits
// INSTANTANEOUS burst, not SUSTAINED pressure over a long run. At scale wrap it in mapThrottled
// (see Throttling) so nulls are retried, not silently dropped.
const report = await agent(`synthesize: ${JSON.stringify(found)}`, { model: "opus", schema: REPORT }); // explicit — never inherit in a Fable session
```

## Throttling — do NOT burst dozens of agents

A `parallel()` of ~70 schema-agents triggered a server **529 overload** ("Server is temporarily limiting requests — not your usage limit"): all returned the error as text, never called StructuredOutput, 0 results. Heavy Opus agents made it worse. **Recurred 2026-06-09** — the canonical `pipeline(items, map, parallel(verify))` shape above ran 99×2 = 198 sonnet agents → 32 votes 529'd, and **15 findings were silently lost because the nulls got `.filter(Boolean)`-ed away instead of retried**. The canonical example is the trap: copy it unmodified at scale and you reproduce this.

There are **two independent failures** here — treat BOTH, not just the first:
1. **Prevention** — chunk the fan-out so you never hold hundreds in flight. The concurrency cap (~10–16, see Caps) limits *instantaneous* burst, NOT *sustained* pressure over a long run, so it does **not** save you.
2. **Resilience** — 529 is server-side and partly out of your control, so a null *will* happen eventually. **Retry nulls, or keep them flagged — never silently drop them.** A dropped null is a finding you'll never know you missed (the 15 above).

Native levers (CC ≥ 2.1.166, community-verified 2026-06-13): `fallbackModel` setting / `CLAUDE_FALLBACK_MODEL` — chain up to 3 models (e.g. opus → sonnet → haiku) for automatic failover on overload; `CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` caps parallel tool use. 529 ≠ 429: retry-only breaks after ~5 min of sustained overload — back off ~30s, then fail over. Note: a fallback DOWNGRADES the model mid-run — fine for finders, think twice for verifiers whose judgment quality is the product.

**Reusable helper — chunk + retry, drop-safe, slots straight into a pipeline stage:**
```js
function chunk(a, n){ const o=[]; for(let i=0;i<a.length;i+=n) o.push(a.slice(i,i+n)); return o; }
// fan `items` through `run` in batches; retry rate-limited nulls once, sequentially.
async function mapThrottled(items, run, size = 4) {
  const out = [];
  for (const batch of chunk(items, size))          // barrier between batches = cool-down
    out.push(...await parallel(batch.map(x => () => run(x))));
  for (let i = 0; i < out.length; i++)             // recover throttled nulls — flag survivors, never drop
    if (out[i] == null) out[i] = (await run(items[i])) ?? { rate_limited: true };
  return out;
}
// canonical pipeline's verify stage becomes (drop-safe):
//   rev => mapThrottled(rev.findings, f => agent(`verify: ${f}`, { model: "sonnet", schema: VERDICT }))
```
A schema-agent returning **null** ≈ rate-limited (it emitted the API error as text, never called StructuredOutput) — a *throttle* signal, not a schema bug. The helper flags a twice-failed item `{ rate_limited: true }` instead of dropping it, so the loss is **visible downstream** — `.filter(x => !x.rate_limited)` to exclude, or count them; never `.filter(Boolean)` the gap into oblivion.

## Recovering a run that already 529'd

A burned run does NOT mean re-run from scratch — the work that succeeded is on disk:
1. **Reconcile first** — compute expected vs actual (e.g. `items.length × votes` vs what you got). Silent shrinkage is invisible until you count it: 2026-06-09 lost 15 mappings, caught only by checking 99×2 = 198 expected against 166 recovered.
2. **Pull cached output from the transcript** (verified) — every subagent's StructuredOutput is on disk in the run's transcript dir, one `agent-*.jsonl` per agent (`…/subagents/workflows/<runId>/`). Parse the assistant `tool_use` block named `StructuredOutput` per file → recover results spending zero tokens. Use when the data exists but the script dropped it.
3. **Resume** — `Workflow({scriptPath, resumeFromRunId})` recovers throttled nulls with the **identical script — no edit needed** (dogfooded 2026-06-09: verified 50→61). But it is BLUNT, not surgical: the cache matches by call **ORDER** (docs: "longest unchanged prefix of agent() calls"), and a nested `parallel()` fan-out completes in **race-dependent order**, so on resume the whole fan-out misses cache and **re-runs wholesale** — the entire 198-agent verify stage re-ran (~73% of the original token spend) though only 33 had failed and the prompts were byte-identical; the stable top-level harvest+map prefix cached cleanly. So resume *does* recover, but for a damaged fan-out it can cost most of the run again — for surgical recovery prefer #2 (transcript) or in-script `mapThrottled`.

## Token budget — scale to the turn target

`budget.total` comes from a "+500k"-style directive (null if unset). Guard loops on `budget.total` — `remaining()` is `Infinity` when unset, so an unguarded loop runs to the 1000-agent cap:
```js
while (budget.total && budget.remaining() > 50_000) { /* spawn another round */ }
const FLEET = budget.total ? Math.floor(budget.total / 100_000) : 5;
```

## Caps (binary-verified)

- **Concurrency:** `min(16, max(2, cores−2))` (hand-rolled semaphore). Excess `agent()` calls queue.
- **Lifetime:** **1000** `agent()` calls → throws `Workflow agent() call cap reached (1000)`.
- Pass 100s of items to `parallel()`/`pipeline()` freely — only ~10-16 run at once. **But** that cap is concurrency, NOT throttle protection: a long run *sustained* at ~10-16 schema-agents still 529s server-side. Wrap schema-agent fan-out in `mapThrottled` (see Throttling).

## pipeline vs parallel

- `pipeline()` = DEFAULT, no barrier — item flows through stages independently (wall-clock = slowest single chain).
- `parallel()` = barrier — awaits all. Use ONLY when a stage genuinely needs ALL prior results (dedup, count-zero early-exit, cross-item compare).

## Why this matters

Cheap correct fan-out: Haiku does the legwork, Sonnet judges, Opus only synthesizes. Throttling keeps the server from rejecting the burst. Together: most exhaustive answer per token, no 529.

## Find-holes / review at scale: recall-preserving swarm → rank → verify-all

For review / find-holes / audit (where a MISSED defect ships) a single strong pass leaves recall
on the table, and a naive cheap swarm trades precision for it. This gets both — validated by two
A/B runs on this machine's own hooks (2026-06-14):

1. **Recall — union a strong baseline + a cheap, DIVERSE swarm.** One `opus` baseline PLUS a
   mostly-`haiku` swarm (a few `sonnet` for depth), 3-5 samples per angle, DECORRELATED by varying
   framing/temperature and shuffling input order. Pool ALL findings, filter nothing yet. Haiku is
   rate-limit-safe at scale and ~1/7 the cost; it found real bugs the opus baseline MISSED (and
   vice-versa — ~complementary, like the consult panel's ~50%-unique).
2. **Rank, don't discard.** Cluster by location+root-cause (`sonnet`); `agreement_count` = a
   confidence PRIOR, never a filter. Dropping low-agreement findings is where recall dies (a real
   bug surfaced at agreement=2; a false positive surfaced at agreement=11).
3. **VERIFY EVERY cluster — not just the weak ones.** One skeptical `opus` adjudicator per cluster,
   against the actual code (it can run it to reproduce). Load-bearing step: agreement is NOT truth —
   homogeneous swarms make CORRELATED errors (6 haiku once agreed on a wrong fact). v1 (verify only
   weak-agreement) shipped that FP; v2 (verify ALL) refuted it plus 8 more, while confirming 6 real.
4. **STOP — this is a BOUNDED audit, not "find every bug".** find-holes never converges: each new
   method/angle surfaces more REAL findings (recall is unbounded — there is no "zero bugs" oracle).
   Set the severity bar BEFORE you start (e.g. "fix the breaks-X class; ignore weakens/edge/by-design")
   and STOP when K consecutive methods add nothing NEW above the bar. Without this it is an infinite,
   expensive bug-generator that feels broken (live 2026-06-14: swarm → panel → panel+agy each kept
   finding more real bugs — useful ONLY because the bar was "breaks-(d)" and we stopped there).

```js
const pool = [...baseline.findings, ...swarm.flat().filter(Boolean).flatMap(r => r.findings)]
const clusters = await agent(`cluster by location+root-cause; agreement_count each`, {model:'sonnet', schema:CLUSTERS})
const verdicts = await mapThrottled(clusters.clusters, c =>          // VERIFY ALL, not just weak
  agent(`adjudicate vs the file — real defect or correlated FP? ${c.issue}`, {model:'opus', schema:VERDICT}))
```

**Honest ceiling:** an imperfect verifier leaves a non-zero FP floor regardless of swarm size;
pure-haiku has correlated blind spots (mix sonnet/opus for depth). **Cost:** expensive (~2.5M
tokens / ~35 agents) — for recall-critical reviews, not quick checks. Routes haiku/sonnet/opus +
mapThrottled — cheap and overload-safe by construction (and clears a workflow-guard hook, if your setup has one). Research backing:
self-consistency, PoLL (panel of small models beats 1 big at 1/7 cost), k-Review (agreement =
severity rank + input-shuffle), inference-scaling-limits (the verifier ceiling).

**Complementary methods — observed (n small), NOT a law.** A verify-all swarm gives PRECISION (its
opus adjudicator cuts FPs that a no-verify `/consult` panel leaves); a heterogeneous panel (codex+
grok+agy) gives RECALL (different models catch the swarm's correlated blind spots). On one real audit
(2026-06-14) the panel caught breaks-bugs the swarm missed, while the swarm cut 8 FPs the panel kept —
neither alone was complete. For recall-critical work consider running BOTH, but this is a ~3-run
observation: validate per task, and mind the doubled cost (the swarm alone is already ~2.5-3M tokens).

## Codex fan-out — route subagents through the lane pool

Parallel CODEX jobs (reviews, consults, implement runs) do NOT parallelize through the
bulldozer plugin MCP server: subagents share the session's single connection to it, and its
serial dispatcher rejects a second concurrent call (`codex turn already in flight`). The
canonical parallel path is the **lane pool** (live-proven 2026-07-13: two turns overlapping
102 s, wall = max not sum):

- Assign **one subagent per lane**: subagent N calls `mcp__codex-lane<N>__codex_run` with
  `mcp:'isolated'` — name the lane EXPLICITLY in each subagent's prompt so two never share one.
- Lanes are named local registrations of the same bulldozer bridge (all features preserved:
  isolation, approvals, audit); each self-describes with a `FAN-OUT LANE <N>` preamble in its
  MCP instructions. No lane tools visible = the pool isn't registered in this project — then
  codex fan-out serializes; fall back to background CLI `codex exec` per subagent, or extend
  the pool: `claude mcp add codex-lane<K> --env BULLDOZER_LANE=<K> -- python3 <bulldozer>/mcp/codex_server.py`
  (+ session restart; mid-session registrations are invisible to subagents).
- More parallel jobs than lanes → queue per lane (several sequential calls inside one
  subagent) rather than sharing a lane between two subagents.
- **Parked approvals are lane-local:** an `awaiting_approval` from lane N must be resumed via
  `mcp__codex-lane<N>__codex_approve` — the park token lives in that lane's process; any other
  server answers `parked turn expired` and the original turn stays blocked. Keep the whole
  run→approve loop inside the SAME subagent (it only sees its own lane anyway).
- **Writable jobs need filesystem isolation too** — `mcp:'isolated'` restricts codex's MCP
  servers, NOT the filesystem. Two `sandbox:"workspace-write"` lanes pointed at the SAME `cwd`
  race the checkout (edits, tests, git state). For writable fan-out either OMIT `cwd` (each
  turn gets its own isolated tmpdir) or give each lane its OWN worktree; parallel jobs into one
  repo checkout must stay read-only (reviews/consults).
- **Consumer-project caveat:** registering lanes in a project that gets the bulldozer plugin's
  own MCP server SUPPRESSES that plugin registration (`excludeStalePluginClients`) — existing
  `mcp__plugin_bulldozer_codex__*` tools disappear for new sessions there, replaced by the lane
  names (same four tools, served from whatever path you registered). Fine in the bulldozer dev
  repo (lanes serve SOURCE); in consumer projects either accept the rename or skip lanes and
  fan out via background CLI `codex exec` instead.

## Feedback

If this skill's doctrine steered you wrong, or the `require-workflow` PreToolUse hook misfired, file a GitHub issue — this is how the routing heuristic and the doctrine get tuned over time.

**Create issue when:**
1. **Hook false-positive** — flagged/blocked a workflow that was already correctly routed or genuinely small (advisory noise, or a DENY under `BULLDOZER_ENFORCE_WORKFLOW_ROUTING`).
2. **Hook false-negative** — a fan-out that burst inherited Opus/Fable or `.filter(Boolean)`-dropped rate-limited nulls slipped through with no advisory.
3. **Escape/bypass broke** — a `// workflow-routing-ok` comment, or a cheap `CLAUDE_CODE_SUBAGENT_MODEL` pin, didn't take effect.
4. **Doctrine wrong/costly** — followed the routing/throttle guidance and still hit a 529 storm, a cost blow-up, or worse results; or a pricing/cap/alias number drifted from reality.
5. **Recovery pattern failed** — `mapThrottled`/resume still dropped findings or re-ran a stage wholesale unexpectedly.

**Do NOT create issue when:** own scripting mistake; a 529 you never throttled for (that's the documented failure mode, not a bug); or a known static-analysis limit of the hook (aliased/computed fan-out, partial 1-of-N routing, JSON-quoted `model`, scriptPath TOCTOU — see `require-workflow-skill.README.md`).

**Command** (attaches the hook's own decision log — that's the empirical data that improves it):

```bash
gh issue create --repo A3IO/jaine-plugins \
  --label "feedback,bulldozer,workflow-swarms" \
  --title "[feedback/workflow-swarms] short description" \
  --body "$(cat <<ISSUE
## What I was doing
{task / workflow shape}

## What I expected
{expected hook decision or doctrine outcome}

## What happened
{actual: false advisory/deny, missed burst, 529, cost blow-up, …}

## Hook decision log (recent)
$(tail -n 12 "${WORKFLOW_HOOK_LOG:-$HOME/.claude/hooks/require-workflow-skill.log}" 2>/dev/null || echo "none")

## Workaround used
{escape comment / env pin / manual routing, or "none — blocked"}

## Environment
- Plugin version: $(jq -r .version "$(ls -dt ~/.claude/plugins/cache/*/bulldozer/*/.claude-plugin/plugin.json 2>/dev/null | head -1)" 2>/dev/null || echo unknown)
- Enforce mode: ${BULLDOZER_ENFORCE_WORKFLOW_ROUTING:-unset}
- Skill: workflow-swarms
- Project: $(pwd)
ISSUE
)"
```

After creating the issue, tell the user:
> "Я создал feedback issue про workflow-swarms: {URL}. Продолжить с workaround или сначала пофиксим?"

## See also
- `bulldozer:require-workflow-skill` — the opt-in PreToolUse(Workflow) guardrail that enforces a slice of this doctrine (advisory by default).
- Reverse-engineered from CC binary 2.1.158 — minified internal names may drift across versions; the routing/throttle logic does not.
