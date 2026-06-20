# Our own codex MCP server (bulldozer module) — design + findings

**Date:** 2026-06-18 · **Status:** v1 shipped (wrap `codex exec`), v2 designed (front `codex app-server`)

Consolidation of a full investigation into building OUR own codex MCP server instead
of relying on the stock `codex mcp-server`. Everything here is empirically verified
against **codex-cli 0.140.0** unless flagged `[inferred]`.

## 1. Motivation — three problems with stock `codex mcp-server`

1. **No isolation.** It inherits `~/.codex/config.toml` + codex-plugin skills
   (superpowers contamination — observed live in agent output).
2. **Prose output.** Verdicts land as free text in context, not structured.
3. **Broken approval — [openai/codex#18268](https://github.com/openai/codex/issues/18268) (OPEN, ≤0.140.0).**
   Verified by reading `codex-rs/mcp-server/src/exec_approval.rs`: the server
   deserializes the elicitation reply into a flat `ExecApprovalResponse { decision }`,
   **ignores the MCP-standard `action` field**, and on the missing-`decision` parse
   failure silently defaults to `ReviewDecision::Denied` (source carries a
   self-acknowledging TODO). Empirically reproduced 3/3: user clicks **Accept** →
   Claude Code sends a spec-correct `{action:"accept",content:{}}` → codex still
   reports "rejected by user", command never runs. Control: Accept AND Decline both
   → rejected (codex can't tell them apart — neither has `decision`). Fault is 100%
   codex-side; Claude Code is correct.

   Diagnostic captured via a CC `Elicitation`/`ElicitationResult` raw-dump hook
   (`~/.claude/hooks/elicitation-log.py`): real schema is
   `{hook_event_name, mcp_server_name, message, mode:"form", requested_schema}` /
   `{..., action:"accept|decline", content:{}}`.

## 2. Build options considered

| Option | Verdict |
|---|---|
| **A. Link `codex-core` (Rust), reimplement the server** | ❌ AVOID. `codex-core` is unpublished, version `0.0.0`, ~145 internal workspace crates, no API stability. A maintenance treadmill. |
| **B. Wrap `codex exec` (CLI) behind our MCP shim** | ✅ v1 SHIPPED. Zero Rust, full control of flags/output, immune to #18268 (exec is non-interactive). Loses: streaming, resume, interactive approval. |
| **C. Thin shim over `codex app-server`** | ✅ v2 TARGET. The real stable programmatic protocol; gives correct approvals + resume without linking Rust. |

## 3. v1 — wrap `codex exec` (SHIPPED)

`mcp/codex_server.py` — zero-dep Python stdlib stdio MCP server. Tool
`mcp__plugin_bulldozer_codex__codex_run(prompt, mode=review|implement, sandbox, effort, model?, cwd?)`.

Bakes in:
- Isolation: `--skip-git-repo-check --ignore-user-config --ignore-rules --ephemeral`.
- Structured output: `mode=review` → `codex exec --output-schema` → guaranteed
  `{verdict,findings}` JSON (the killer find — structured output is a codex
  guarantee, not prose parsing).
- `approval_policy=never` + scoped sandbox → never enters the broken elicitation path.

**Verified engine facts (codex 0.140.0):**
- `codex exec --output-schema FILE` forces the final message to conform to a schema.
- `-o FILE` writes ONLY the final agent message (clean parse channel).
- `codex exec` **blocks on stdin** unless closed → server runs child with `stdin=DEVNULL`.
- `-a/--ask-for-approval` is NOT valid on `exec`; use `-c approval_policy=never`.
- `resume` is finicky (exit 2 on our flag combo) → omitted.

Status: 7 offline tests (handshake, argv mapping, graceful missing-codex) GREEN;
real e2e against codex returned a valid `{verdict:"MINOR-FIXES", findings:[...]}`.

**Limitation (by design):** v1 is NON-interactive — no live streaming, no mid-run
approval, no resume. An MCP tool is request/response, so streaming/steering can
never map anyway; the interactivity an MCP tool *can* deliver is correct approvals
(via elicitation) + resume — that needs v2.

## 4. v2 — front `codex app-server` (DESIGNED, not built)

`codex app-server` (bare `codex app-server` = stdio JSONL **bidirectional** JSON-RPC;
"jsonrpc_lite" — the `"jsonrpc"` field is omitted) is the rich, (semi-)stable protocol
that powers the VS Code extension. Proven interactive end-to-end via
`mcp/appserver_probe.py` (a direct client): the exact task that #18268 breaks
(write outside sandbox, Accept→Denied) **succeeded** through app-server — we replied
`{decision:"accept"}`, the command ran, the file was created. Streaming deltas
observed live.

Key protocol facts (read from a checked-out `codex-rs` tree):
- **Handshake:** `initialize` → `{userAgent,codexHome,platformFamily,platformOs}` →
  notification `initialized`. `capabilities.experimentalApi:true` gates experimental
  methods (without it, experimental outbound fields are stripped).
- **Interactive methods:** `thread/start|resume|fork`, `turn/start|steer|interrupt`.
- **Streaming notifications:** `turn/started`, `item/started|completed`,
  `item/agentMessage/delta`, `item/reasoning/textDelta`,
  `item/commandExecution/outputDelta`, `item/fileChange/patchUpdated`, `turn/completed`.
- **Server→client requests (the approval fix):** `item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`, `item/tool/requestUserInput`,
  `mcpServer/elicitation/request`. Reply `{decision:"accept"}` (accept / acceptForSession
  / decline / cancel); elicitation reply `{action,content}`. **Correctly typed —
  #18268 does not apply.**
- **Casing:** `approvalPolicy` kebab (`on-request`/`on-failure`/`untrusted`/`never`);
  `sandbox` = SandboxMode (`read-only`/`workspace-write`/`danger-full-access`).

**v2 architecture:** our MCP server spawns/holds a `codex app-server` child, and on a
`codex_run` tool call drives a thread/turn. When app-server asks for approval, our
server issues an MCP `elicitation/create` to Claude Code, reads CC's spec-correct
`{action:"accept"}`, and translates it to `{decision:"accept"}` for app-server — i.e.
**our server does what codex's mcp-server should have, fixing #18268 for MCP-in-CC.**
Adds resume (tool param `thread_id` → `thread/resume`) + isolation (`base_instructions`/
`config` at `thread/start`). Streaming still cannot cross the MCP-tool boundary.

codex's own source-grounded recommendation was to drive app-server DIRECTLY from a
standalone process (not an MCP facade) for FULL interactivity (live streaming +
steering); the MCP facade is the right call only because the consumer here is Claude
Code itself, where streaming/steering don't map to a tool anyway.

## 5. Home & distribution

Shipped as a **bulldozer plugin MCP server** (`.mcp.json` at plugin root → server at
`${CLAUDE_PLUGIN_ROOT}/mcp/codex_server.py`). Rides bulldozer's existing jaine-custom
distribution + mirroring; bulldozer becomes both owner and first consumer.

**Open concern (accepted):** a public bulldozer now ships a codex-dependent MCP server
that spawns per session for all users. Mitigated: the server is fail-graceful (returns
a clear error if the codex binary is absent; never crashes). Could be made opt-in later
(env gate) if it bothers downstream users.

## 6. Registration & activation

Plugin MCP servers register when the plugin (re)loads. After this lands in the
bulldozer cache: `/reload-plugins` or a CC restart activates the server. Tool appears
as `mcp__plugin_bulldozer_codex__codex_run`; verify with `/mcp`.

## 7. References

- Stock server internals + #18268: `codex-rs/mcp-server/src/{exec_approval.rs,codex_tool_runner.rs,message_processor.rs}`
- app-server protocol: `codex-rs/app-server/README.md`, `codex-rs/app-server-protocol/src/protocol/{common.rs,v2/{thread,turn,item,mcp}.rs}`
- v1 server: `mcp/codex_server.py` · tests `tests/test_codex_mcp.py`
- v2 PoC (direct app-server client): `mcp/appserver_probe.py`
- memory: `reference_codex_mcp_vs_cli`
