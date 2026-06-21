# Codex MCP v2 Bridge Hardening + Surface — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mcp/codex_server.py` a flexible, well-isolated codex bridge — caller picks per-call which MCP servers codex sees (REQUIRED, no silent default), the child's environment is scrubbed of CC secrets, the result carries token/usage/metadata, three control knobs are first-class, the self-imposed turn cap is gone, and fake-grounded protocol divergences are closed — all verified against live codex 0.141.

**Architecture:** The bridge fronts a persistent `codex app-server` child via one `codex_run` tool. Isolation is achieved with **per-server `-c` flags at spawn (no `CODEX_HOME` relocation)**. Because those flags are process-level and the child is a session singleton, the manager carries an *isolation signature* and **respawns only when a call requests a different `mcp` selection** (warm reuse otherwise). Changes touch the spawn argv builder, the child env, the manager lifecycle, `start_thread`, the turn loop, the notification-allowlist source, and the test fake — preserving the shipped invariants (#18268 approvals, happy-path-no-`_drift`, additive result shape).

**Tech Stack:** Python stdio MCP server (stdlib only); `codex app-server` v2 JSON-RPC (jsonrpc_lite frames — no `"jsonrpc"` key); pytest (offline + `@pytest.mark.slow` live-codex e2e); `tomllib` (stdlib, Python 3.11+) for `config.toml` parsing.

## Global Constraints

- **Empirical basis is authoritative.** Every mechanism was verified against live codex 0.141. The `@pytest.mark.slow` live-codex gate is mandatory after any `mcp/codex_server.py` change — do NOT regress to fake-only verification.
- **Verify isolation by TOOLS-COUNT, not name-absence.** A disabled server STILL appears in `mcpServerStatus/list` by name with `tools: 0`. The empirical "disabled" metric is `tools == 0`, never absence from the list.
- **Do NOT mutate the user's `~/.codex`** — no relocation, no `codex plugin remove`, no writing `config.toml` or `auth.json`. All isolation is ephemeral spawn argv (`-c` / `--disable`) + a scrubbed child `env`.
- **Preserve invariants:** (a) the #18268 approval bridge keeps working — `{decision:…}` to app-server with dynamic `availableDecisions` labels incl. the `acceptWithExecpolicyAmendment` dict + `cancel`; (b) the happy path returns NO `_drift`; (c) result-shape changes are ADDITIVE only — existing keys `thread_id`/`verdict`/`findings`/`schema_ok`/`result` stay; (d) `mcp` isolation is a REQUIRED explicit per-call choice — no silent default.
- **No manual `plugin.json` bump** (auto-calver on merge to `bulldozer/main`).
- **`error`/`warning` notifications stay OUT of the benign set** (they signal problems — surfaced as before).
- **computer-use REMAINS in `isolated`** (bundled plugin; no clean ephemeral disable; documented limitation).
- The spec's "Decision Rationale — do NOT naively revert" (10 locked decisions) governs every task. Read it before changing anything: `docs/superpowers/specs/2026-06-20-codex-mcp-bridge-hardening-design.md`.

---

## File Structure

| File | Responsibility | Tasks |
|------|----------------|-------|
| `mcp/codex_server.py` | The bridge. New pure helpers (`_enumerate_config_mcp_servers`, `_build_isolation_argv`, `_build_child_env`), isolation-aware spawn + manager, the `mcp` knob in `codex_run_v2`, additive result metadata, first-class control knobs, removed turn cap, schema-loaded notification allowlist. | 1–8 |
| `mcp/gen_notifications.py` | NEW maintainer tool: dump `codex app-server generate-json-schema`, extract the `ServerNotification` method set, write the fixture. | 9 |
| `mcp/codex-notifications.json` | NEW checked-in generated fixture: the authoritative benign-notification set. **Sibling of `codex_server.py` so it always ships in the plugin cache** (NOT under `tests/`, which the cache may prune → silent fallback). Runtime loads it. | 9 |
| `tests/fixtures/fake_appserver.py` | Align the fake to the real app-server schema (approval `availableDecisions` shape, real param keys, real notification set). | 10 |
| `tests/fixtures/codex-protocol-fingerprint.json` | Coherence tripwire — extend with the notification-fixture pointer where useful. | 9 |
| `tests/test_codex_mcp_v2.py` | All offline + slow tests for the above. | 1–11 |
| `CLAUDE.md` (bulldozer) | Doc: finalize the isolation section (shipped, not "fix pending"), the `mcp` knob, the new surface, no-cap. | 12 |

**Decomposition note (read before Task 5):** isolation is process-level spawn argv, the child is a singleton, and `mcp` is per-call. The manager therefore tracks an *isolation signature* and respawns on change. Consequence the implementer + reviewer must understand: **switching `mcp` between calls pays codex's one-time cold start again (~28–80s); same-`mcp` calls stay warm.** Resuming a `thread_id` under a different `mcp` than it was created with is legitimate — the thread's conversation history (rollout) is preserved while the available tool set for that turn reflects the new selection.

---

## Task 1: Isolation resolution primitives (`_enumerate_config_mcp_servers` + `_build_isolation_argv`)

Pure functions that turn the user's `config.toml` + the caller's `mcp` value into spawn argv. No process, no I/O beyond reading `config.toml`. This is the core of decisions #1/#2/#3.

**Files:**
- Modify: `mcp/codex_server.py` (add two module-level functions near the other spawn helpers, above `_spawn_appserver`)
- Test: `tests/test_codex_mcp_v2.py` (new offline tests)

**Interfaces:**
- Produces:
  - `_enumerate_config_mcp_servers(codex_home: str | None = None) -> list[str]` — sorted list of `[mcp_servers.*]` table names from `$CODEX_HOME/config.toml` (default `CODEX_HOME` env or `~/.codex`). Returns `[]` on any error (missing file, malformed TOML).
  - `_build_isolation_argv(mcp, config_servers: list[str]) -> list[str]` — argv tokens to append after `app-server`. `mcp` is one of `"isolated"`, `"all"`, or `list[str]`. Raises `ValueError` for anything else (callers validate first; this is defense-in-depth).
  - `VALID_MCP_MODES = frozenset({"isolated", "all", "list"})` — module constant (string modes; a `list` value is validated separately).

- [ ] **Step 0: Add the offline `CODEX_HOME` isolation fixture (F10)**

Offline tests must not read the dev's real `~/.codex/config.toml` (non-hermetic — `_enumerate_config_mcp_servers` is now reached via the `call_codex_run` helper). Add this autouse fixture near the top of `tests/test_codex_mcp_v2.py` (after the imports / `skip_if_no_codex`). It points `CODEX_HOME` at an empty tmp dir for every NON-slow test; slow tests are exempt (they need real codex auth). Tests that need a specific config still override via their own `monkeypatch.setenv("CODEX_HOME", …)` (the in-test setenv runs after the fixture and wins).

```python
@pytest.fixture(autouse=True)
def _isolate_codex_home(request, tmp_path_factory, monkeypatch):
    """Hermetic offline tests: CODEX_HOME → empty tmp dir. Slow (live-codex) tests
    are exempt — they need the real ~/.codex auth (F10)."""
    if request.node.get_closest_marker("slow"):
        return
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_codex_mcp_v2.py`:

```python
# ---------------------------------------------------------------------------
# Task 1: isolation resolution primitives (#204 Group 1)
# ---------------------------------------------------------------------------

def test_enumerate_config_mcp_servers_reads_table(tmp_path, monkeypatch):
    import codex_server as cs
    (tmp_path / "config.toml").write_text(
        'model = "gpt-5.5"\n\n'
        '[mcp_servers.dash]\ncommand = "dash-mcp"\n\n'
        '[mcp_servers.deepwiki]\nurl = "https://example/mcp"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cs._enumerate_config_mcp_servers() == ["dash", "deepwiki"]  # sorted

def test_enumerate_config_mcp_servers_missing_file_is_empty(tmp_path, monkeypatch):
    import codex_server as cs
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))   # no config.toml
    assert cs._enumerate_config_mcp_servers() == []

def test_enumerate_config_mcp_servers_malformed_is_empty(tmp_path, monkeypatch):
    import codex_server as cs
    (tmp_path / "config.toml").write_text("this is = = not valid toml [[[")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cs._enumerate_config_mcp_servers() == []   # never raises

def test_build_isolation_argv_all_disables_nothing():
    import codex_server as cs
    assert cs._build_isolation_argv("all", ["dash", "deepwiki"]) == []

def test_build_isolation_argv_isolated_disables_every_server_and_apps():
    import codex_server as cs
    argv = cs._build_isolation_argv("isolated", ["dash", "deepwiki"])
    # BARE keys (no quotes): codex's -c parser splits on '.' naively and does NOT honor
    # TOML quoting — a quoted mcp_servers key CRASHES app-server startup on 0.141.
    assert argv == [
        "-c", "mcp_servers.dash.enabled=false",
        "-c", "mcp_servers.deepwiki.enabled=false",
        "--disable", "apps",
    ]

def test_build_isolation_argv_subset_keeps_named_disables_rest():
    import codex_server as cs
    # keep deepwiki + apps; disable dash
    argv = cs._build_isolation_argv(["deepwiki", "apps"], ["dash", "deepwiki"])
    assert argv == ["-c", "mcp_servers.dash.enabled=false"]   # apps kept → not disabled

def test_build_isolation_argv_subset_disables_apps_when_not_listed():
    import codex_server as cs
    argv = cs._build_isolation_argv(["dash"], ["dash", "deepwiki"])
    assert argv == ["-c", "mcp_servers.deepwiki.enabled=false", "--disable", "apps"]

def test_build_isolation_argv_rejects_unknown_mode():
    import codex_server as cs
    import pytest
    with pytest.raises(ValueError):
        cs._build_isolation_argv("nonsense", ["dash"])

def test_build_isolation_argv_skips_untargetable_server_name(capsys):
    """A server name containing '.' or '"' cannot be targeted by
    `-c mcp_servers.<name>.enabled=false`: codex's CLI parser splits the key path on '.'
    naively and does NOT honor TOML quoting (a quoted mcp_servers key even CRASHES
    app-server startup on 0.141 — empirically verified). Such a name is SKIPPED with a
    stderr warning (left enabled), never silently mis-targeted or crashed."""
    import codex_server as cs
    argv = cs._build_isolation_argv("isolated", ["weird.name"])
    err = capsys.readouterr().err
    assert argv == ["--disable", "apps"]          # server skipped; apps still disabled
    assert "weird.name" in err and "WARNING" in err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /0/ANTHROPICS_DEV/jaine-plugins/plugins/bulldozer && pytest tests/test_codex_mcp_v2.py -k "isolation_argv or enumerate_config" -v`
Expected: FAIL — `AttributeError: module 'codex_server' has no attribute '_enumerate_config_mcp_servers'`.

- [ ] **Step 3: Implement the helpers**

In `mcp/codex_server.py`, add `import tomllib` to the import block (after `import time`). Then add, just above `def _spawn_appserver(...)`:

```python
VALID_MCP_MODES = frozenset({"isolated", "all", "list"})


def _codex_home() -> str:
    """The effective CODEX_HOME (env override or ~/.codex)."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _enumerate_config_mcp_servers(codex_home: str | None = None) -> list[str]:
    """Return the sorted names of [mcp_servers.*] tables in $CODEX_HOME/config.toml.

    Best-effort: a missing/unreadable/malformed config.toml yields []. Never raises.
    These are the user's configured MCP servers — the set we may disable at spawn.
    """
    home = codex_home or _codex_home()
    path = os.path.join(home, "config.toml")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        servers = data.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            return []
        return sorted(servers.keys())
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return []


def _is_targetable_server_name(name: str) -> bool:
    """True if `name` can be targeted by `-c mcp_servers.<name>.enabled=false`.

    EMPIRICAL (codex 0.141, probe-verified): codex's CLI override parser splits the dotted
    key path on '.' NAIVELY and does NOT parse TOML quoted keys. A BARE identifier works
    (`-c mcp_servers.dash.enabled=false` → dash tools:4→0). A QUOTED key
    (`-c mcp_servers."dash".enabled=false`) does NOT merely mis-target — it CRASHES
    app-server startup: `Error: error loading default config after config error: invalid
    transport in mcp_servers."dash"` (rc=1). So we NEVER quote; a name containing '.'
    (path-separator ambiguity) or '"' is unreachable via -c → skip it.
    """
    return "." not in name and '"' not in name


def _build_isolation_argv(mcp, config_servers: list[str]) -> list[str]:
    """Build the `codex app-server` argv tokens (after 'app-server') for an mcp selection.

    'all'      → [] (nothing disabled; codex's full normal setup).
    'isolated' → disable every config server + `--disable apps` (computer-use remains).
    list[str]  → keep only the named servers/builtins; disable the rest; `--disable apps`
                 unless 'apps' is in the list.

    Server names are used BARE (unquoted) — quoting CRASHES app-server (see
    _is_targetable_server_name). A name with '.'/'"' is unreachable via -c and is SKIPPED
    with a stderr warning (left enabled), never silently mis-targeted or crashed. Raises
    ValueError for any other `mcp` value (callers validate first; defense-in-depth).
    """
    def _disable(name):
        if not _is_targetable_server_name(name):
            print(f"[bulldozer-codex] WARNING: cannot disable MCP server {name!r} via -c "
                  "(name contains '.' or '\"', unreachable by codex's key-path parser); "
                  "leaving it enabled", file=sys.stderr)
            return []
        return ["-c", f"mcp_servers.{name}.enabled=false"]

    if mcp == "all":
        return []
    if mcp == "isolated":
        argv: list[str] = []
        for name in config_servers:
            argv += _disable(name)
        argv += ["--disable", "apps"]
        return argv
    if isinstance(mcp, list):
        keep = set(mcp)
        argv = []
        for name in config_servers:
            if name not in keep:
                argv += _disable(name)
        if "apps" not in keep:
            argv += ["--disable", "apps"]
        return argv
    raise ValueError(f"invalid mcp value: {mcp!r}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_codex_mcp_v2.py -k "isolation_argv or enumerate_config" -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): isolation resolution primitives (#204 group 1)"
```

---

## Task 2: Child environment allowlist (`_build_child_env`)

Fail-closed env scrub so a CC secret cannot leak into codex's shell subprocesses (decision #5). Pure function; offline.

**Files:**
- Modify: `mcp/codex_server.py` (add `_CHILD_ENV_ALLOW_EXACT`, `_CHILD_ENV_ALLOW_PREFIX`, `_build_child_env` near the spawn helpers)
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: nothing from prior tasks.
- Produces: `_build_child_env(parent_env: dict) -> dict` — the allowlisted env to hand the spawned child.

- [ ] **Step 0: Confirm the `shell_environment_policy` key + values (recon, F1)**

Run (requires codex): `rm -rf /tmp/cas-schema && codex app-server generate-json-schema --out /tmp/cas-schema && grep -riE "shell_environment_policy|inherit" /tmp/cas-schema | sort -u`. Confirm `shell_environment_policy.inherit` accepts `core` (codex's minimal-safe set: PATH/HOME/locale). If the enum spells it differently (e.g. `all`/`core`/`none` vs other), use the schema's exact value in `_SHELL_ENV_POLICY_ARGV`. The slow smoke (Task 11) is the live confirmation that implement-mode shells still work with the policy.

> **R1-F1 decision (recon-verified, not auto-tested).** Layer 1 (the env allowlist) is the
> automated security boundary — `test_e2e_env_secret_does_not_leak_to_codex_shell` proves an
> unrelated CC secret never reaches codex's shell. Layer 2 (`shell_environment_policy`) is
> belt-and-suspenders that additionally hides codex's OWN allowlisted creds (`OPENAI_*`/proxy)
> from shell subprocesses; a robust automated test would need a harmless allowlisted-but-non-core
> canary (not auth/TLS/proxy, or the turn itself breaks), which is fragile. Decision: KEEP the
> layer-2 flag, VERIFY it by this recon (codex accepts `inherit=core`) + the Task 11 implement
> smoke (shells still work) — do NOT build the fragile canary test. The threat layer 2 covers (a
> prompt-injected codex turn exfiltrating its own key) is real but low-probability; layer 1 is the
> load-bearing defense.

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Task 2: child env allowlist (#204 1c — secret-leak fix)
# ---------------------------------------------------------------------------

def test_build_child_env_keeps_essentials():
    import codex_server as cs
    parent = {"PATH": "/usr/bin", "HOME": "/Users/x", "CODEX_HOME": "/Users/x/.codex",
              "TMPDIR": "/tmp", "LANG": "en_US.UTF-8", "LC_ALL": "C", "TERM": "xterm"}
    env = cs._build_child_env(parent)
    for k in parent:
        assert env.get(k) == parent[k], f"{k} must pass the allowlist"

def test_build_child_env_drops_secrets():
    import codex_server as cs
    parent = {"PATH": "/usr/bin", "FORGEJO_API_TOKEN": "secret",
              "ANTHROPIC_API_KEY": "sk-xxx", "MY_CUSTOM_TOKEN": "leak"}
    env = cs._build_child_env(parent)
    assert "PATH" in env
    assert "FORGEJO_API_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "MY_CUSTOM_TOKEN" not in env

def test_build_child_env_keeps_codex_own_credentials_and_proxy():
    import codex_server as cs
    parent = {"OPENAI_API_KEY": "ok", "OPENAI_BASE_URL": "https://api",
              "HTTPS_PROXY": "http://p", "https_proxy": "http://p",
              "SSL_CERT_FILE": "/c.pem", "CODEX_CA_CERTIFICATE": "/corp-ca.pem"}
    env = cs._build_child_env(parent)
    for k in parent:
        assert k in env, f"codex needs {k}"

def test_shell_env_policy_argv_is_a_c_override():
    """F1: layer-2 shell_environment_policy is a `-c` spawn override (defense-in-depth)."""
    import codex_server as cs
    assert cs._SHELL_ENV_POLICY_ARGV[0] == "-c"
    assert cs._SHELL_ENV_POLICY_ARGV[1].startswith("shell_environment_policy.inherit=")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "build_child_env" -v`
Expected: FAIL — `_build_child_env` not defined.

- [ ] **Step 3: Implement the allowlist**

In `mcp/codex_server.py`, near the other spawn helpers:

```python
# Fail-closed child-env allowlist (#204 1c). Only what codex itself needs passes;
# every unrelated CC secret (FORGEJO/ANTHROPIC/custom *_TOKEN/*_KEY) is dropped.
# codex's OWN credentials (OPENAI_*) are allowlisted — codex reading its own key is
# not the leak we guard against (unrelated CC secrets are).
_CHILD_ENV_ALLOW_EXACT = frozenset({
    "HOME", "PATH", "TMPDIR", "TMP", "TEMP", "TERM", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "CODEX_HOME",
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
    "https_proxy", "http_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "CODEX_CA_CERTIFICATE",   # codex-client custom CA — takes PRECEDENCE over SSL_CERT_FILE
                              # (codex-rs/codex-client custom_ca). Omitting it breaks TLS for
                              # corp users on a custom CA. (OPENAI_API_KEY is the only auth env
                              # codex reads; OPENAI_BASE_URL is config-only — kept as harmless.)
})
_CHILD_ENV_ALLOW_PREFIX = ("LC_",)


def _build_child_env(parent_env: dict) -> dict:
    """Return the fail-closed allowlisted env for the spawned codex child.

    Only known-needed vars pass; everything else (CC secrets) is dropped.
    """
    out = {}
    for k, v in parent_env.items():
        if k in _CHILD_ENV_ALLOW_EXACT or k.startswith(_CHILD_ENV_ALLOW_PREFIX):
            out[k] = v
    return out


# Layer 2 (spec 1c, defense-in-depth, F1): constrain what codex's SHELL SUBPROCESSES
# inherit, independent of the process env. Layer 1 (above) already drops CC secrets from
# the child env; layer 2 additionally keeps even allowlisted codex creds (OPENAI_*) out of
# arbitrary shell commands codex runs. `inherit=core` = codex's minimal-safe set (PATH/HOME/
# locale), which keeps implement-mode shells functional. Applied at spawn for ALL mcp modes.
_SHELL_ENV_POLICY_ARGV = ["-c", "shell_environment_policy.inherit=core"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_codex_mcp_v2.py -k "build_child_env" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): fail-closed child-env allowlist (#204 1c)"
```

---

## Task 3: Isolation-aware spawn + manager (rewrite `_spawn_appserver`, signature-respawn `ensure`)

Wire the Task 1/2 primitives into the process: spawn with the resolved isolation argv + scrubbed env, and make the manager respawn when the isolation signature changes. Fix the two stale code comments (decision rationale + the refuted "51.6 s → 34.7 s").

**Files:**
- Modify: `mcp/codex_server.py` — `_spawn_appserver`, `AppServerManager.__init__`, `AppServerManager.ensure`, the `STERILE_INSTRUCTIONS`/`ISOLATION_CONFIG` comment block.
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `_build_child_env` (Task 2).
- Produces:
  - `_spawn_appserver(codex_bin: str, isolation_argv: list[str] | None = None) -> _ChildHandle` — argv = `[codex_bin, "app-server", *_SHELL_ENV_POLICY_ARGV, *isolation_argv]`, `env=_build_child_env(os.environ)`.
  - `AppServerManager.ensure(isolation_argv: list[str] | None = None) -> object` — warm reuse iff alive AND signature matches; else (re)spawn + re-initialize. Signature = `tuple(isolation_argv or [])`.

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Task 3: isolation-aware spawn + signature respawn
# ---------------------------------------------------------------------------

def test_spawn_appserver_appends_isolation_argv_and_scrubs_env(monkeypatch):
    """_spawn_appserver builds `app-server <isolation_argv>` and passes a scrubbed env."""
    import codex_server as cs
    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["env"] = kw.get("env")
            self.stdin = self.stdout = self.stderr = None
            self.returncode = None
        def poll(self): return None
        def kill(self): pass

    monkeypatch.setattr(cs.subprocess, "Popen", _FakeProc)
    monkeypatch.setenv("FORGEJO_API_TOKEN", "secret")
    cs._spawn_appserver("/bin/codex", ["-c", "mcp_servers.dash.enabled=false", "--disable", "apps"])
    # layer-2 shell_environment_policy (F1) is prepended, then the isolation argv
    assert captured["argv"] == ["/bin/codex", "app-server",
                                *cs._SHELL_ENV_POLICY_ARGV,
                                "-c", "mcp_servers.dash.enabled=false", "--disable", "apps"]
    assert "FORGEJO_API_TOKEN" not in captured["env"]   # scrubbed (layer 1)
    assert "PATH" in captured["env"]

def test_manager_warm_reuse_same_isolation_signature(fake_child):
    from codex_server import AppServerManager
    m = AppServerManager(bin=fake_child)
    c1 = m.ensure(["-c", "a=b"])
    c2 = m.ensure(["-c", "a=b"])      # same signature → warm reuse
    assert c2 is c1

def test_manager_respawns_on_isolation_signature_change():
    from codex_server import AppServerManager
    fc = FakeChild()
    try:
        m = AppServerManager(bin=fc)
        c1 = m.ensure(["-c", "x=1"])
        c2 = m.ensure(["-c", "y=2"])   # different signature → respawn
        assert c2 is not c1
    finally:
        fc.kill()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "spawn_appserver_appends or warm_reuse or respawns_on_isolation" -v`
Expected: FAIL — `_spawn_appserver` takes 1 arg; `ensure` ignores argv / no signature tracking.

- [ ] **Step 3: Rewrite `_spawn_appserver` and the stale comment block**

Replace the `STERILE_INSTRUCTIONS` trailing NOTE comment + `ISOLATION_CONFIG` line (the block currently spanning the `# NOTE (2026-06-20, A3IO/jaine-plugins#204): this mcp_servers={} override is a NO-OP …` comment through `ISOLATION_CONFIG = {"mcp_servers": {}}`) with:

```python
# Isolation lives at SPAWN now (#204): per-server `-c mcp_servers.<n>.enabled=false` (BARE
# keys — quoting CRASHES app-server, verified) + `--disable apps`, built by
# _build_isolation_argv from the caller's `mcp` knob.
# We do NOT relocate CODEX_HOME (auth/sessions stay intact) and we do NOT clear the
# whole mcp_servers table via `-c mcp_servers={}` — an empty-table deep-merge is a
# verified NO-OP (codex 0.141). computer-use is a bundled plugin with no ephemeral
# disable, so it remains even in "isolated" (documented limitation).
```

Then replace the whole `def _spawn_appserver(...)` function with:

```python
def _spawn_appserver(codex_bin: str, isolation_argv: list[str] | None = None) -> _ChildHandle:
    """Spawn `codex app-server` with the resolved isolation argv and a scrubbed env.

    isolation_argv (from _build_isolation_argv) disables the user's MCP servers /
    `apps` at the PROCESS level — the only mechanism that actually works on codex
    0.141 (per-thread `config.mcp_servers` overrides are no-ops). The child env is
    fail-closed allowlisted (_build_child_env, layer 1) and `_SHELL_ENV_POLICY_ARGV`
    (layer 2, F1) constrains what codex's shell subprocesses inherit — both spec 1c
    layers, applied for ALL mcp modes.
    """
    argv = [codex_bin, "app-server", *_SHELL_ENV_POLICY_ARGV, *(isolation_argv or [])]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_build_child_env(os.environ),
    )
    return _ChildHandle(proc)
```

- [ ] **Step 4: Make `AppServerManager` signature-aware**

In `AppServerManager.__init__`, add after `self._codex_version = None`:

```python
        self._isolation_sig = None   # tuple(isolation_argv); None forces first spawn
```

Replace `def ensure(self) -> object:` and its body with:

```python
    def ensure(self, isolation_argv: list[str] | None = None) -> object:
        """Return the live child, spawning (and initializing) if necessary.

        Warm reuse iff the child is alive AND its isolation signature matches the
        request. A changed signature (different `mcp` selection) kills the old child
        and respawns with the new spawn argv — re-paying codex's one-time cold start.
        Crash (dead child) also respawns. Idempotent for the same signature.
        """
        sig = tuple(isolation_argv or [])
        alive = _is_child_alive(self._child)
        if alive and self._isolation_sig == sig:
            return self._child
        if alive:                          # signature changed → kill the old child
            try:
                self._child.kill()
            except Exception:
                pass

        # Spawn a new child
        if isinstance(self._bin, str):
            child = _spawn_appserver(self._bin, list(isolation_argv or []))
        elif self._child is None:
            # First call: use the provided bin object directly (FakeChild in tests)
            child = self._bin
        else:
            # Respawn (crash or signature change): fresh instance via the same class
            child = self._bin.__class__()

        self._isolation_sig = sig
        self._adopt(child)
        self._do_initialize()
        return self._child
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_codex_mcp_v2.py -k "spawn_appserver_appends or warm_reuse or respawns_on_isolation or child_death_respawns or manager_initialize" -v`
Expected: PASS — incl. the existing `test_child_death_respawns_on_next_ensure` and `test_manager_initialize_sends_clientInfo_and_experimentalApi` (backward compatible: `ensure()` with no args → sig `()`).

- [ ] **Step 6: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): isolation-aware spawn + signature respawn; fix stale comments (#204)"
```

---

## Task 4: Drop the no-op per-thread `mcp_servers` clear from `start_thread`

The per-thread `config: {"mcp_servers": {}}` merge is a verified no-op (decision #2). Remove the `ISOLATION_CONFIG` overwrite; keep `_CONFIG_DENY` (still block a caller injecting `mcp_servers`/instructions into per-thread config). Update the three tests that asserted `config == ISOLATION_CONFIG` / `mcp_servers == {}`.

**Files:**
- Modify: `mcp/codex_server.py` — remove `ISOLATION_CONFIG`; simplify the config merge in `start_thread`.
- Modify: `tests/test_codex_mcp_v2.py` — `test_start_thread_is_nonephemeral_and_isolated`, `test_config_merge_scrubs_isolation_keys`, `test_codex_run_v2_forwards_parity_args_to_thread_start`.

**Interfaces:**
- Consumes: `_CONFIG_DENY` (unchanged).
- Produces: `start_thread(...)` sends a `config` that contains the caller's benign keys, with `mcp_servers`/`mcpServers`/instructions scrubbed, and **no injected `mcp_servers: {}`**.

- [ ] **Step 1: Update the failing tests first (they encode the new contract)**

In `test_start_thread_is_nonephemeral_and_isolated`, drop the `ISOLATION_CONFIG` import and the two config assertions, replacing with the scrub contract:

```python
def test_start_thread_is_nonephemeral_and_isolated(tmp_path, fake_child):
    from codex_server import AppServerManager, STERILE_INSTRUCTIONS
    m = AppServerManager(bin=fake_child)
    m.ensure()
    m.start_thread(sandbox="read-only", approval_policy="on-request",
                   base_instructions=STERILE_INSTRUCTIONS,
                   config={"model_reasoning_effort": "high"}, cwd=str(tmp_path))
    p = fake_child.received("thread/start")["params"]
    assert p.get("ephemeral") in (None, False)
    assert p["baseInstructions"] == STERILE_INSTRUCTIONS
    assert p["config"] == {"model_reasoning_effort": "high"}  # benign passthrough; no injected mcp_servers
    assert p["sandbox"] == "read-only"
    assert p["cwd"] == str(tmp_path)
```

In `test_config_merge_scrubs_isolation_keys`, the deny-keys are still scrubbed but `mcp_servers` is no longer injected as `{}`:

```python
def test_config_merge_scrubs_isolation_keys():
    """Deny-keys are scrubbed; benign keys pass; no per-thread mcp_servers injection."""
    sent = _started_params(config={"mcp_servers": {"evil": 1}, "mcpServers": {"evil": 1},
                                   "baseInstructions": "x", "developerInstructions": "y",
                                   "model_reasoning_effort": "high"})["config"]
    assert "mcp_servers" not in sent                    # caller injection scrubbed (no re-inject)
    assert "mcpServers" not in sent                     # alias scrubbed
    assert "baseInstructions" not in sent and "developerInstructions" not in sent
    assert sent["model_reasoning_effort"] == "high"     # benign key passes
```

In `test_codex_run_v2_forwards_parity_args_to_thread_start`, change the two `config["mcp_servers"] == {}` / `"mcpServers" not in` assertions:

```python
    assert "mcp_servers" not in p["config"]            # caller injection scrubbed, not re-injected
    assert "mcpServers" not in p["config"]
    assert p["config"]["model_reasoning_effort"] == "high"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "start_thread_is_nonephemeral or config_merge_scrubs or forwards_parity_args" -v`
Expected: FAIL — current code injects `mcp_servers: {}` (the assertions `"mcp_servers" not in sent` fail).

- [ ] **Step 3: Remove the no-op merge**

In `mcp/codex_server.py`, delete the `ISOLATION_CONFIG = {"mcp_servers": {}}` constant (the comment block was already rewritten in Task 3). In `start_thread`, replace:

```python
        caller_cfg = config if isinstance(config, dict) else {}
        merged = {k: v for k, v in caller_cfg.items() if k not in _CONFIG_DENY}
        merged.update(ISOLATION_CONFIG)     # our keys always win
        config = merged
```

with:

```python
        caller_cfg = config if isinstance(config, dict) else {}
        # Scrub deny-keys (mcp_servers/instructions); pass benign keys through.
        # NOTE (#204): we do NOT inject mcp_servers={} — that per-thread override is a
        # no-op on codex 0.141. MCP isolation is enforced at SPAWN (_build_isolation_argv).
        config = {k: v for k, v in caller_cfg.items() if k not in _CONFIG_DENY}
```

- [ ] **Step 4: Fix the remaining `ISOLATION_CONFIG` import**

`test_start_thread_is_nonephemeral_and_isolated` no longer imports `ISOLATION_CONFIG` (Step 1 already removed it). Grep to confirm no other reference remains:

Run: `grep -rn "ISOLATION_CONFIG" mcp/ tests/`
Expected: no matches.

- [ ] **Step 5: Run to verify they pass**

Run: `pytest tests/test_codex_mcp_v2.py -k "start_thread or config_merge or forwards_parity or codex_run_v2" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "refactor(codex-mcp): drop no-op per-thread mcp_servers clear; keep _CONFIG_DENY scrub (#204)"
```

---

## Task 5: Wire the REQUIRED `mcp` knob + `list` discovery into `codex_run_v2` + inputSchema

The caller passes `mcp` every call. `"list"` returns the available servers without spawning. Otherwise resolve argv (Tasks 1) and pass it to `ensure(argv)` (Task 3). Capture `mcp_mode`/`mcp_servers_enabled` for the Task 6 metadata. Update the test helper + slow-test calls for the new required param.

**Files:**
- Modify: `mcp/codex_server.py` — `TOOLS[0].inputSchema` (+ description), `codex_run_v2`.
- Modify: `tests/test_codex_mcp_v2.py` — `call_codex_run` (inject `mcp`), the three slow e2e calls, `test_codex_run_no_codex_returns_error` ordering note, new offline tests.

**Interfaces:**
- Consumes: `_enumerate_config_mcp_servers`, `_build_isolation_argv`, `VALID_MCP_MODES` (Task 1); `manager.ensure(argv)` (Task 3).
- Produces:
  - `codex_run_v2` accepts `args["mcp"]` (REQUIRED: `"isolated"` | `"all"` | `"list"` | `list[str]`).
  - `mcp="list"` returns `{"available_mcp_servers": [...], "builtins": ["apps", "computer-use"], "computer_use_note": "...", "thread_id": None}` (additive; no spawn).
  - A resolved `(isolation_argv, mcp_mode, mcp_servers_enabled)` is computed before `ensure` and threaded forward (Task 6 reads `mcp_mode`/`mcp_servers_enabled`).

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Task 5: the REQUIRED mcp knob + list discovery
# ---------------------------------------------------------------------------

def test_mcp_required_missing_is_error(ext_child):
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi"}, manager=m, cc_write_fn=lambda f: None,
                     cc_read_fn=lambda timeout=10: None)   # no mcp
    assert "error" in r and "mcp" in r["error"].lower()

def test_mcp_invalid_value_is_error(ext_child):
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi", "mcp": "wat"}, manager=m,
                     cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert "error" in r

def test_mcp_list_returns_available_without_spawn(tmp_path, monkeypatch):
    """mcp='list' enumerates config servers + builtins and returns WITHOUT a turn."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # No manager needed — list never spawns. But the no-codex check precedes it,
    # so point CODEX at a real-enough path via monkeypatch.
    monkeypatch.setattr(cs, "CODEX", sys.executable)   # any existing file passes the binary check
    # codex_run_v2(manager=None) creates the module singleton _v2_manager with bin=CODEX.
    # Register it with monkeypatch so teardown restores it — else this test's sys.executable
    # manager would poison a later singleton-using (slow) test.
    monkeypatch.setattr(cs, "_v2_manager", None)
    r = cs.codex_run_v2({"prompt": "ignored", "mcp": "list"})
    assert r.get("available_mcp_servers") == ["dash"]
    assert "apps" in r["builtins"] and "computer-use" in r["builtins"]
    assert "thread_id" not in r or r["thread_id"] is None   # never ran a turn

def test_mcp_isolated_resolves_argv_and_passes_to_ensure(tmp_path, monkeypatch, ext_child):
    """codex_run_v2 with mcp='isolated' calls ensure() with the disable argv."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    captured = {}
    m = cs.AppServerManager(bin=ext_child)
    orig = m.ensure
    def spy(argv=None):
        captured["argv"] = argv
        return orig(argv)
    m.ensure = spy
    cs.codex_run_v2({"prompt": "hi", "mcp": "isolated"}, manager=m,
                    cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert captured["argv"] == ['-c', 'mcp_servers.dash.enabled=false', '--disable', 'apps']
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "mcp_required or mcp_invalid or mcp_list or mcp_isolated_resolves" -v`
Expected: FAIL — `codex_run_v2` ignores `mcp`.

- [ ] **Step 3: Update the `call_codex_run` helper to inject `mcp`**

In `tests/test_codex_mcp_v2.py`, add a `mcp="isolated"` parameter to `call_codex_run` so the dozens of existing tests keep a valid required value. Change the signature line:

```python
def call_codex_run(fake_child_inst, prompt, mode="review", sandbox=None,
                   approval_policy=None, effort=None, cwd=None, model=None,
                   thread_id=None, _force_bad_final=None, turn_variant=None,
                   base_instructions=None, developer_instructions=None, config=None,
                   mcp="isolated"):
```

and add, in the args-building block (after `args = {"prompt": prompt, "mode": mode}`):

```python
    args["mcp"] = mcp
```

- [ ] **Step 4: Implement the `mcp` knob in `codex_run_v2`**

In `codex_run_v2`, after `mode = args.get("mode", "review")` and before the "Explicit caller values" block, insert:

```python
    # ── mcp knob (REQUIRED, no default) ──────────────────────────────────────
    mcp = args.get("mcp")
    if mcp is None:
        return _stamp_drift({"error": (
            "mcp is required (no default). Pass mcp='isolated' (review/implement clean), "
            "mcp='all' (full toolset), mcp=['dash', ...] (subset), or mcp='list' to "
            "discover the available servers."
        )}, acc)
    # Type-check BEFORE membership (F2): `["x"] in frozenset` raises TypeError (lists are
    # unhashable), so a list/dict mcp must NOT reach the `in` test.
    if isinstance(mcp, str):
        if mcp not in VALID_MCP_MODES:
            return _stamp_drift({"error": f"invalid mcp value {mcp!r}; use 'isolated'/'all'/'list'/[list]"}, acc)
    elif isinstance(mcp, list):
        if not all(isinstance(s, str) for s in mcp):
            return _stamp_drift({"error": "mcp list items must be strings"}, acc)
    else:
        return _stamp_drift({"error": f"invalid mcp value {mcp!r}; use 'isolated'/'all'/'list'/[list]"}, acc)

    config_servers = _enumerate_config_mcp_servers()

    # Reject unknown subset names BEFORE spawn (F2): a typo must fail loud, not silently
    # disable the server the caller meant to keep.
    if isinstance(mcp, list):
        valid_targets = set(config_servers) | {"apps", "computer-use"}
        unknown = [s for s in mcp if s not in valid_targets]
        if unknown:
            return _stamp_drift({"error": (
                f"unknown mcp server(s) {unknown}; valid: {sorted(valid_targets)} "
                "(call mcp='list' to discover the available servers)")}, acc)

    if mcp == "list":
        return _stamp_drift({
            "available_mcp_servers": config_servers,
            "builtins": ["apps", "computer-use"],
            "computer_use_note": ("computer-use is a bundled codex plugin and cannot be "
                                  "disabled — it remains even in 'isolated'."),
            "thread_id": None,
        }, acc)

    try:
        isolation_argv = _build_isolation_argv(mcp, config_servers)
    except ValueError as e:
        return _stamp_drift({"error": str(e)}, acc)

    # Observability for the result metadata (Task 6). computer-use is a bundled plugin
    # that CANNOT be disabled (decision #6), so it is ALWAYS enabled — reflect that (F2b).
    # (Unknown subset names were already rejected pre-spawn above, so the subset here is
    # all valid.)
    if mcp == "isolated":
        mcp_servers_enabled = ["computer-use"]
    elif mcp == "all":
        mcp_servers_enabled = config_servers + ["apps", "computer-use"]
    else:  # subset list (names validated above)
        mcp_servers_enabled = [s for s in config_servers if s in set(mcp)]
        if "apps" in set(mcp):
            mcp_servers_enabled.append("apps")
        mcp_servers_enabled.append("computer-use")
    mcp_mode = mcp if isinstance(mcp, str) else "subset"
```

Then change the single `manager.ensure()` call (currently `manager.ensure()` at the "Ensure child is alive" block) to:

```python
    try:
        manager.ensure(isolation_argv)
    except Exception as e:
        return _stamp_drift({"error": f"app-server ensure failed: {e}"}, acc)
```

> NOTE on the `list` precedence vs the no-codex check: the existing no-codex binary check runs first (only when `manager is None`). `mcp='list'` therefore requires codex installed — acceptable (you can't enumerate isolation for an absent codex). The `test_mcp_list_returns_available_without_spawn` test points `CODEX` at an existing file so the binary check passes.

- [ ] **Step 5: Add the `mcp` field to the tool inputSchema + description**

In `TOOLS[0]["inputSchema"]["properties"]`, add (after `"prompt"`):

```python
                "mcp": {
                    "description": (
                        "REQUIRED. Which MCP servers codex sees this call: 'isolated' "
                        "(disable all user servers + apps; clean review/implement), 'all' "
                        "(full normal toolset), a list like ['dash','deepwiki','apps'] (keep "
                        "only those), or 'list' to discover the available servers without "
                        "running codex. computer-use (bundled) always remains."
                    ),
                    "oneOf": [
                        {"type": "string", "enum": ["isolated", "all", "list"]},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
```

Add `"mcp"` to `TOOLS[0]["inputSchema"]["required"]`:

```python
            "required": ["prompt", "mcp"],
```

In `TOOLS[0]["description"]`, replace the sentence `"Isolated per-thread from ~/.codex config and codex-plugin skills by default. "` with:

```python
            "REQUIRED mcp arg selects which MCP servers codex sees: 'isolated'/'all'/"
            "'list'/[subset] (call mcp='list' first to discover servers). "
```

- [ ] **Step 6: Update the slow e2e calls + the no-codex test**

The slow e2e tests call `codex_run_v2({...})` without `mcp` → they must pass `mcp`. Edit:

- `test_e2e_review_real_appserver`: change the call to `codex_run_v2({"prompt": "...", "mode": "review", "mcp": "isolated"})`.
- `test_e2e_implement_real_appserver`: add `"mcp": "isolated"`.
- `test_live_codex_version_matches_pin`: change to `cs.codex_run_v2({"prompt": "ping", "mode": "review", "mcp": "isolated"})`.

`test_codex_run_no_codex_returns_error` calls `codex_run_v2({"prompt": "test"})` with `manager=None`, so the no-codex check fires before mcp validation → it still returns an error. Leave it, but add a one-line assertion comment that it tests the no-codex branch specifically:

```python
    assert "error" in r
    assert "codex binary not found" in r["error"]   # no-codex branch precedes mcp validation
```

- [ ] **Step 7: Add the `test_tools_list_includes_v2_params` mcp assertion**

In `test_tools_list_includes_v2_params` (dispatcher test), after the `approval_policy` assertion, add:

```python
            assert "mcp" in schema_props, f"inputSchema missing 'mcp': {list(schema_props)}"
            required = codex_tool.get("inputSchema", {}).get("required", [])
            assert "mcp" in required, f"mcp must be REQUIRED, required={required}"
```

- [ ] **Step 8: Run the full offline suite (no regressions)**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -v`
Expected: PASS (all offline tests, including the updated helper-driven ones).

- [ ] **Step 9: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): REQUIRED per-call mcp knob + list discovery (#204 group 1)"
```

---

## Task 6: tokenUsage + per-call metadata in the result (additive)

Capture the last `thread/tokenUsage/updated` snapshot during the turn loop and the thread's model/tier/effort, then add ADDITIVE `usage`/`codex`/`timing`/`status` fields to the result. Existing keys unchanged.

**Files:**
- Modify: `mcp/codex_server.py` — `start_thread` (stash thread meta), the turn loop (capture tokenUsage + duration), `_shape_result` (add fields), `codex_run_v2` (thread the metadata in).
- Modify: `tests/test_codex_mcp_v2.py` — extend `ExtendedFakeChild` to emit `thread/tokenUsage/updated`; new tests.

**Interfaces:**
- Consumes: the `mcp_mode`/`mcp_servers_enabled` locals (Task 5).
- Produces: result gains `usage` (dict), `codex` (dict: `model`, `service_tier`, `effort`, `approvals_reviewer`, `mcp_mode`, `mcp_servers_enabled`), `timing` (`{duration_ms}`), `status` (str). `AppServerManager._last_thread_meta` dict stashed from thread/start + thread/resume responses.

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Task 6: tokenUsage + metadata (additive)
# ---------------------------------------------------------------------------

def test_result_carries_usage_and_metadata(ext_child):
    ext_child.script_turn_variant("with_usage")     # emits thread/tokenUsage/updated
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp="isolated")
    assert "error" not in r
    assert r["usage"]["total_tokens"] == 123
    assert r["codex"]["mcp_mode"] == "isolated"
    # F2b: computer-use is bundled (never disabled) → always present, even in isolated
    assert r["codex"]["mcp_servers_enabled"] == ["computer-use"]
    assert "duration_ms" in r["timing"]
    assert r["status"] == "completed"
    # additive: existing keys still present
    assert "result" in r

def test_metadata_absent_keys_do_not_break_review_shape(ext_child):
    r = call_codex_run(ext_child, prompt="hi", mode="review", mcp="all")
    assert "error" not in r
    assert {"thread_id", "verdict", "findings", "schema_ok"} <= set(r)  # unchanged core
    assert r["codex"]["mcp_mode"] == "all"
    assert "computer-use" in r["codex"]["mcp_servers_enabled"]   # F2b

def test_subset_unknown_names_rejected_pre_spawn(ext_child, monkeypatch, tmp_path):
    """F2: a subset name matching no config server / builtin fails loud BEFORE spawn —
    a typo must not silently disable the server the caller meant to keep."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp=["dahs"])  # typo of dash
    assert "error" in r and "dahs" in r["error"]
    assert ext_child.turn_start_params is None   # never ran a turn

def test_mcp_list_value_does_not_crash(ext_child, monkeypatch, tmp_path):
    """F2: a valid list subset must NOT raise TypeError (`list in frozenset` is unhashable)."""
    import codex_server as cs
    (tmp_path / "config.toml").write_text('[mcp_servers.dash]\ncommand="x"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp=["dash"])
    assert "error" not in r   # no TypeError; dash kept

def test_mcp_dict_value_is_error(ext_child):
    """F2: a dict mcp is rejected cleanly (not a TypeError crash)."""
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi", "mcp": {"x": 1}}, manager=m,
                     cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert "error" in r and "invalid mcp" in r["error"].lower()

def test_failed_turn_carries_metadata(ext_child):
    """F11: a terminal turn failure still returns usage/codex/timing/status (token cost
    visibility on failure)."""
    ext_child.script_turn_variant("failed")
    r = call_codex_run(ext_child, prompt="hi", mcp="isolated")
    assert "error" in r and "turn failed" in r["error"]
    assert r["status"] == "failed"
    assert r["codex"]["mcp_mode"] == "isolated"
    assert "timing" in r and "usage" in r
```

- [ ] **Step 2: Extend `ExtendedFakeChild` with a `with_usage` variant**

In `ExtendedFakeChild._dispatch`, inside the `turn/start` handling, after the TurnStartResponse `_write_msg` and before the existing variant branches, add:

```python
            if self._turn_variant == "with_usage":
                # Real wire shape: params.tokenUsage = {last, total}, camelCase breakdown (spec 2a).
                _bd = {"inputTokens": 100, "cachedInputTokens": 0, "outputTokens": 23,
                       "reasoningOutputTokens": 0, "totalTokens": 123}
                self._write_msg({"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread_id, "turnId": turn_id,
                    "tokenUsage": {"last": _bd, "total": _bd},
                }})
                self._write_msg({"method": "item/agentMessage/delta", "params": {
                    "delta": self._final_message, "threadId": thread_id,
                    "turnId": turn_id, "itemId": item_id,
                }})
                self._write_msg({"method": "turn/completed", "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "items": [], "itemsView": "loaded",
                             "status": "completed", "error": None,
                             "startedAt": 0, "completedAt": 0, "durationMs": 10},
                }})
                return
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "carries_usage or absent_keys_do_not_break" -v`
Expected: FAIL — result has no `usage`/`codex`/`timing`/`status`.

- [ ] **Step 4: Stash thread meta in `start_thread` and `resume_thread`**

In `AppServerManager.__init__`, add:

```python
        self._last_thread_meta = {}
```

In `start_thread`, after `result = resp.get("result", {})` and before `return result["thread"]["id"]`, add:

```python
        self._last_thread_meta = {
            "model": result.get("model"),
            "service_tier": result.get("serviceTier"),
            "effort": result.get("reasoningEffort"),
            "approvals_reviewer": result.get("approvalsReviewer"),
        }
```

In `resume_thread`, before `return resp.get("result", {})`, add:

```python
        _r = resp.get("result", {}) or {}
        self._last_thread_meta = {
            "model": _r.get("model"),
            "service_tier": _r.get("serviceTier"),
            "effort": _r.get("reasoningEffort"),
            "approvals_reviewer": _r.get("approvalsReviewer"),
        }
        return _r
```

(Remove the original bare `return resp.get("result", {})` — the `_r` block replaces it.)

- [ ] **Step 5: Capture tokenUsage + duration in the turn loop; thread the metadata**

In `codex_run_v2`, just before `state_machine.turn_started(cc_id)`, add:

```python
    usage_snapshot: dict = {}
    turn_start_t = time.time()
```

In the Phase-2 notification handling, add a branch for tokenUsage. Change:

```python
                if kind == "notification":
                    if method == "item/agentMessage/delta":
                        final_message_parts.append(frame.get("params", {}).get("delta", ""))
                    elif method == "turn/completed":
```

to:

```python
                if kind == "notification":
                    if method == "item/agentMessage/delta":
                        final_message_parts.append(frame.get("params", {}).get("delta", ""))
                    elif method == "thread/tokenUsage/updated":
                        # Wire: params.tokenUsage = {last, total} (NOT params.usage). See spec 2a.
                        tu = frame.get("params", {}).get("tokenUsage")
                        if isinstance(tu, dict):
                            usage_snapshot = tu
                    elif method == "turn/completed":
```

In the `turn/completed` success return, replace:

```python
                        state_machine.turn_completed()
                        return _stamp_drift(_shape_result(mode, thread_id, "".join(final_message_parts)), acc)
```

with:

```python
                        state_machine.turn_completed()
                        meta = _build_result_meta(manager, usage_snapshot, turn_start_t,
                                                  mcp_mode, mcp_servers_enabled,
                                                  effort_val, model_val, "completed")
                        return _stamp_drift(
                            _shape_result(mode, thread_id, "".join(final_message_parts), meta), acc)
```

Also merge metadata onto the TERMINAL-FAILURE path (F11) — a failed turn still consumed
tokens, so usage/timing/status observability matters there too. Replace the existing
turn/completed failure branch:

```python
                        if t.get("status") not in ("completed", "success") or t.get("error"):
                            state_machine.turn_completed()
                            return _stamp_drift({
                                "error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                                "thread_id": thread_id}, acc)
```

with:

```python
                        if t.get("status") != "completed" or t.get("error"):  # TurnStatus has no "success" (codex 0.141: completed/interrupted/failed/inProgress)
                            state_machine.turn_completed()
                            meta = _build_result_meta(manager, usage_snapshot, turn_start_t,
                                                      mcp_mode, mcp_servers_enabled,
                                                      effort_val, model_val, "failed")
                            return _stamp_drift({
                                "error": f"turn failed: status={t.get('status')!r} error={t.get('error')!r}",
                                "thread_id": thread_id, **meta}, acc)
```

- [ ] **Step 6: Implement `_build_result_meta` and extend `_shape_result`**

Add a helper above `_shape_result`:

```python
def _build_result_meta(manager, usage_snapshot: dict, turn_start_t: float,
                       mcp_mode: str, mcp_servers_enabled: list, effort_val,
                       model_val, status: str) -> dict:
    """Build the ADDITIVE result metadata (usage/codex/timing/status).

    `status` is "completed" on success or "failed" on a terminal turn failure (F11) —
    a failed turn still consumed tokens, so usage/timing observability matters there too.
    """
    tm = getattr(manager, "_last_thread_meta", {}) or {}
    # Wire: usage_snapshot is params.tokenUsage = {last, total}, each a TokenUsageBreakdown
    # with camelCase keys. Map the cumulative `total` into our snake_case result API (spec 2a).
    breakdown = (usage_snapshot or {}).get("total") or (usage_snapshot or {}).get("last") or {}
    return {
        "usage": {
            "input_tokens": breakdown.get("inputTokens"),
            "cached_input_tokens": breakdown.get("cachedInputTokens"),
            "output_tokens": breakdown.get("outputTokens"),
            "reasoning_output_tokens": breakdown.get("reasoningOutputTokens"),
            "total_tokens": breakdown.get("totalTokens"),
        },
        "codex": {
            "model": model_val or tm.get("model"),
            "service_tier": tm.get("service_tier"),
            "effort": effort_val or tm.get("effort"),
            "approvals_reviewer": tm.get("approvals_reviewer"),
            "mcp_mode": mcp_mode,
            "mcp_servers_enabled": mcp_servers_enabled,
        },
        "timing": {"duration_ms": int((time.time() - turn_start_t) * 1000)},
        "status": status,
    }
```

Change `_shape_result`'s signature and body to merge the meta:

```python
def _shape_result(mode: str, thread_id: str, final_text: str, meta: dict | None = None) -> dict:
    """Return the mode-shaped result dict, merged with additive meta (usage/codex/timing/status).

    review mode: {thread_id, verdict, findings, schema_ok, **meta}
    implement mode: {thread_id, result, **meta}
    """
    if mode == "review":
        try:
            parsed = json.loads(final_text)
            base = {"thread_id": thread_id, "schema_ok": True,
                    "verdict": parsed.get("verdict", "UNKNOWN"),
                    "findings": parsed.get("findings", [])}
        except (json.JSONDecodeError, AttributeError):
            base = {"thread_id": thread_id, "schema_ok": False,
                    "verdict": "UNKNOWN", "findings": [], "raw": final_text}
    else:
        base = {"thread_id": thread_id, "result": final_text}
    if meta:
        base.update(meta)
    return base
```

(The slow e2e `test_e2e_review_real_appserver` already only checks `schema_ok`/`verdict`/`thread_id`/`findings`, so the additive keys don't break it.)

- [ ] **Step 6b: Add a slow test — real usage is non-null (anti-divergence guard, spec 2a)**

The offline fake emits the real `tokenUsage.{last,total}` camelCase shape, but only a LIVE run proves the reader's wire keys match codex. Without this, a wrong wire key (e.g. reading `params.usage` / snake_case) ships all-null usage silently. Add:

```python
@skip_if_no_codex
@pytest.mark.slow
def test_e2e_usage_is_populated_by_real_appserver():
    """Real codex emits thread/tokenUsage/updated; the result's usage.total_tokens must be a
    real positive int — proves the params.tokenUsage.total.<camelCase> wire mapping (spec 2a)."""
    from codex_server import codex_run_v2
    r = codex_run_v2({"prompt": "Say OK.", "mode": "implement", "mcp": "isolated"})
    assert "error" not in r, f"turn errored: {r.get('error')}"
    assert isinstance(r.get("usage"), dict), f"no usage block: {r}"
    assert isinstance(r["usage"].get("total_tokens"), int) and r["usage"]["total_tokens"] > 0, \
        f"usage.total_tokens not populated (wire-key drift? reading params.tokenUsage.total?): {r['usage']}"
```

- [ ] **Step 7: Run to verify they pass + offline regression**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -v`
Expected: PASS (incl. the two new metadata tests and all prior ones). The Step-6b slow test runs under the mandatory `-m slow` gate.

- [ ] **Step 8: Commit**

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): additive usage/codex/timing/status result metadata (#204 2a)"
```

---

## Task 7: First-class control knobs — `approvals_reviewer`, `service_tier`

Promote two knobs to first-class `codex_run` params wired to `thread/start`. Pin the exact wire keys from the live schema FIRST (empirical-basis-authoritative), then wire them. (Output `verbosity` stays config-passthrough as `model_verbosity` — R1-F5 demote.)

**Files:**
- Modify: `mcp/codex_server.py` — `TOOLS[0].inputSchema`, `start_thread` (new kwargs), `codex_run_v2` (forward args).
- Modify: `tests/test_codex_mcp_v2.py` — new offline tests + a slow acceptance test.

**Interfaces:**
- Consumes: `start_thread` (extended).
- Produces: `start_thread(..., approvals_reviewer=None, service_tier=None)` wires `approvalsReviewer` / `serviceTier` to thread/start params (omitted when None). `codex_run_v2` forwards `args["approvals_reviewer"]`/`args["service_tier"]`. Output verbosity is NOT a param — callers use `config={"model_verbosity": …}` (R1-F5 demote).

- [ ] **Step 1: Pin the exact wire keys from the live schema (recon, do this first)**

Run (requires codex installed):

```bash
rm -rf /tmp/cas-schema && codex app-server generate-json-schema --out /tmp/cas-schema
grep -riE "approvalsReviewer|serviceTier|verbosity|model_verbosity" /tmp/cas-schema | sort -u
```

Expected: confirms `approvalsReviewer` and `serviceTier` are `ThreadStartParams` properties (and present on `ThreadStartResponse`, so they echo back — Step 6 relies on this), and that output verbosity is `model_verbosity` (config key). **Also record the ENUM/allowed values for `serviceTier` and `approvalsReviewer`** — the Step 6 slow echo test passes one valid `serviceTier` and asserts it round-trips. If a key differs, use the schema's exact spelling in Step 4 and adjust the assertions in Step 2. (If codex is not installed, the offline tests below still pin the bridge's chosen wire keys; the slow echo test in Step 6 is the live confirmation.)

- [ ] **Step 2: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Task 7: first-class control knobs (#204 2b)
# ---------------------------------------------------------------------------

def test_approvals_reviewer_and_service_tier_reach_thread_start():
    p = _started_params(approvals_reviewer="auto_review", service_tier="flex")
    assert p["approvalsReviewer"] == "auto_review"
    assert p["serviceTier"] == "flex"

def test_control_knobs_omitted_when_none():
    p = _started_params()
    assert "approvalsReviewer" not in p
    assert "serviceTier" not in p

def test_verbosity_is_config_passthrough():
    # R1-F5 demote: verbosity is NOT first-class; it rides the existing config passthrough.
    p = _started_params(config={"model_verbosity": "low"})
    assert p["config"]["model_verbosity"] == "low"   # benign key, not scrubbed by _CONFIG_DENY

def test_codex_run_v2_forwards_control_knobs(ext_child):
    # F5a: exactly ONE run on a fresh ext_child so received("thread/start") returns THIS
    # call's thread/start, not a stale earlier one (the prior version's spurious first
    # call_codex_run made `received` return the wrong, knob-less thread/start → false pass).
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    codex_run_v2({"prompt": "hi", "mcp": "isolated", "approvals_reviewer": "auto_review",
                  "service_tier": "flex", "config": {"model_verbosity": "high"}},
                 manager=m, cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    p = ext_child.received("thread/start")["params"]
    assert p["approvalsReviewer"] == "auto_review"
    assert p["serviceTier"] == "flex"
    assert p["config"]["model_verbosity"] == "high"   # via config passthrough

def test_control_knobs_on_resume_fail_loud(ext_child):
    """F4: thread-level knobs are set at thread/start; on resume there is no thread/start,
    so they must fail loud (not silently no-op)."""
    from codex_server import codex_run_v2, AppServerManager
    m = AppServerManager(bin=ext_child)
    r = codex_run_v2({"prompt": "hi", "mcp": "isolated", "thread_id": "T1",
                      "service_tier": "flex"},
                     manager=m, cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
    assert "error" in r
    assert "resum" in r["error"].lower() or "new thread" in r["error"].lower()
```

- [ ] **Step 3: Run to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "approvals_reviewer or service_tier or verbosity or forwards_control" -v`
Expected: FAIL — `_started_params` rejects the new kwargs / params absent.

- [ ] **Step 4: Extend `start_thread`**

Change the `start_thread` signature to add the two first-class kwargs (after `one_shot: bool = False`):

```python
        approvals_reviewer: str | None = None,
        service_tier: str | None = None,
```

(Output `verbosity` is NOT first-class — R1-F5 demote: it has the lowest operational value and a
wrong wire key would silently no-op with no correctness impact. Callers set it via the existing
`config` passthrough: `config={"model_verbosity": "low"}` — `model_verbosity` is benign, not in
`_CONFIG_DENY`, so it passes through unchanged.)

In the `params` dict construction, after `if cwd is not None: params["cwd"] = cwd`, add:

```python
        if approvals_reviewer is not None:
            params["approvalsReviewer"] = approvals_reviewer
        if service_tier is not None:
            params["serviceTier"] = service_tier
```

- [ ] **Step 5: Forward from `codex_run_v2` + add to inputSchema**

In `codex_run_v2`, in the NEW-thread `manager.start_thread(...)` call, add the three forwards:

```python
            thread_id = manager.start_thread(
                sandbox=sandbox_for_start,
                approval_policy=approval_policy_for_start,
                base_instructions=args.get("base_instructions"),
                developer_instructions=args.get("developer_instructions"),
                config=args.get("config"),
                cwd=cwd_for_start,
                approvals_reviewer=args.get("approvals_reviewer"),
                service_tier=args.get("service_tier"),
            )
```

In `TOOLS[0]["inputSchema"]["properties"]`, add (after `"config"`):

```python
                "approvals_reviewer": {
                    "type": "string",
                    "enum": ["user", "auto_review", "guardian_subagent"],
                    "description": "Who approves codex actions: user (CC elicitation), "
                                   "auto_review / guardian_subagent (risk-based auto-approve). "
                                   "NOTE: auto_review is a serde ALIAS of guardian_subagent — "
                                   "codex accepts both as input but ECHOES back guardian_subagent "
                                   "(so r['codex']['approvals_reviewer'] reads guardian_subagent "
                                   "even when you sent auto_review).",
                },
                "service_tier": {"type": "string", "description": "codex speed/cost tier."},
```

- [ ] **Step 5b: Resume-guard for thread-level knobs (F4)**

`approvals_reviewer`/`service_tier` are wired at `thread/start`. On resume there is no `thread/start`, so they'd silently no-op. Fail loud instead. In `codex_run_v2`, immediately after the `mcp` knob block (Task 5) and before the busy guard, add:

```python
    # F4: thread-level control knobs apply only to a NEW thread (set at thread/start).
    # On resume there is no thread/start → they'd silently no-op. Fail loud.
    if args.get("thread_id") is not None and any(
        args.get(k) is not None for k in ("approvals_reviewer", "service_tier")):
        return _stamp_drift({"error": (
            "approvals_reviewer/service_tier apply only to a NEW thread; "
            "omit them when resuming a thread_id.")}, acc)
```

- [ ] **Step 6: Add a slow echo test (live codex honors the knobs — F5b)**

A "no error" assertion is too weak: codex silently ignores an unknown config/param key, so a WRONG wire key would pass. Assert the knobs ROUND-TRIP via the thread metadata (codex echoes `serviceTier`/`approvalsReviewer` on `ThreadStartResponse` → surfaced in `r["codex"]`). Use a `serviceTier` value confirmed by the Step-1 recon enum; the assertion is value-agnostic (echo == what was passed).

```python
@skip_if_no_codex
@pytest.mark.slow
def test_e2e_control_knobs_echoed_by_real_appserver():
    """Real codex honors approvals_reviewer/service_tier — proven by metadata echo, not
    just absence of error (a wrong wire key would silently no-op). F5b."""
    from codex_server import codex_run_v2
    TIER = "flex"   # must be a value from the Step-1 recon serviceTier enum
    r = codex_run_v2({"prompt": "Say OK.", "mode": "implement", "mcp": "isolated",
                      "approvals_reviewer": "user", "service_tier": TIER})
    assert "error" not in r, f"control knobs rejected: {r.get('error')}"
    assert r.get("thread_id")
    # live echo: codex reflects the keys back in the thread metadata → the wire keys landed
    assert r["codex"]["service_tier"] == TIER, f"serviceTier not echoed: {r['codex']}"
    assert r["codex"]["approvals_reviewer"] == "user", f"approvalsReviewer not echoed: {r['codex']}"
```

- [ ] **Step 7: Run offline + commit**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -v`
Expected: PASS.

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): first-class approvals_reviewer/service_tier knobs (#204 2b)"
```

---

## Task 8: Remove the self-imposed turn cap; add opt-in `timeout`

Match stock (no work-duration cap). Keep the setup timeouts (thread/start cold-start, turn/start ACK). Add an opt-in `timeout` param (default none) that re-imposes a work cap when set (decision #7).

**Files:**
- Modify: `mcp/codex_server.py` — the turn loop in `codex_run_v2`; `TOOLS[0].inputSchema`.
- Modify: `tests/test_codex_mcp_v2.py` — new offline tests + a slow long-turn test.

**Interfaces:**
- Consumes: nothing new.
- Produces: `codex_run_v2` reads `args["timeout"]` (seconds, float|None). When None → no work-duration deadline. When set → deadline `now + timeout`, credited back across approval waits; on expiry returns `{"error": "turn timed out after Ns"}`.

- [ ] **Step 1: Write the failing tests**

```python
# ---------------------------------------------------------------------------
# Task 8: no self-imposed turn cap; opt-in timeout
# ---------------------------------------------------------------------------

def test_default_has_no_work_duration_deadline(ext_child):
    """With no timeout arg, codex_run_v2 must not impose a work-duration deadline
    (the loop condition is unbounded; the turn ends on turn/completed)."""
    import inspect, codex_server as cs
    src = inspect.getsource(cs.codex_run_v2)
    assert "time.time() + 120.0" not in src, "the hard 120s cap must be gone"
    # A normal turn still completes fine with no timeout:
    r = call_codex_run(ext_child, prompt="hi", mode="implement", mcp="isolated")
    assert "error" not in r and r.get("result") is not None

def test_opt_in_timeout_fires_when_set():
    """A turn that never completes + a tiny timeout → clean timeout error (no hang)."""
    from codex_server import codex_run_v2, AppServerManager

    class _NeverCompletes(ExtendedFakeChild):
        def _dispatch(self, msg):
            method = msg.get("method"); mid = msg.get("id")
            params = msg.get("params") or {}
            if method == "turn/start":
                self.turn_start_params = params
                # ACK the turn but NEVER send turn/completed
                self._write_msg({"id": mid, "result": {"turn": {"id": "T", "items": [], "status": "inProgress"}}})
                return
            super()._dispatch(msg)

    fc = _NeverCompletes()
    try:
        m = AppServerManager(bin=fc)
        r = codex_run_v2({"prompt": "hi", "mcp": "isolated", "timeout": 0.3},
                         manager=m, cc_write_fn=lambda f: None, cc_read_fn=lambda timeout=10: None)
        assert "error" in r and "timed out" in r["error"]
    finally:
        fc.kill()


class _PreAckApprovalChild(ExtendedFakeChild):
    """Emits an approval REQUEST before the TurnStartResponse (pre-ACK), then waits for the
    bridge's decision reply before ACKing — exercises F6 (the human approval wait must be
    credited to ack_deadline, not counted as a setup stall)."""
    def __init__(self):
        super().__init__()
        self._reply_event = _threading.Event()

    def _dispatch(self, msg):
        # a decision reply (id + result, no method) unblocks the pending pre-ACK approval
        if msg.get("id") is not None and "method" not in msg and ("result" in msg or "error" in msg):
            self._reply_event.set()
            return
        if msg.get("method") == "turn/start":
            params = msg.get("params") or {}
            self.turn_start_params = params
            tid = params.get("threadId", "T1")
            turn_mid = msg.get("id")
            def _flow():
                self._write_msg({"id": "PREACK-1",
                                 "method": "item/commandExecution/requestApproval",
                                 "params": {"threadId": tid, "turnId": "TURN1", "itemId": "I1",
                                            "command": "echo hi", "cwd": "/tmp",
                                            "availableDecisions": ["accept", "cancel"]}})
                self._reply_event.wait(timeout=5.0)   # block until the bridge replies (human time)
                self._write_msg({"id": turn_mid, "result": {"turn": {"id": "TURN1", "items": [], "status": "inProgress"}}})
                self._write_msg({"method": "item/agentMessage/delta",
                                 "params": {"delta": "ok", "threadId": tid, "turnId": "TURN1", "itemId": "I1"}})
                self._write_msg({"method": "turn/completed",
                                 "params": {"threadId": tid, "turn": {"id": "TURN1", "status": "completed",
                                                                       "error": None, "durationMs": 1}}})
            _threading.Thread(target=_flow, daemon=True).start()
            return
        super()._dispatch(msg)


def test_pre_ack_approval_does_not_trip_ack_deadline(monkeypatch):
    """F6: an approval arriving BEFORE the turn/start ACK, with a human reply slower than the
    ACK window, must NOT cause an ACK timeout — the wait is credited to ack_deadline."""
    import codex_server as cs
    monkeypatch.setattr(cs, "_ACK_TIMEOUT", 0.3)   # shrink the setup window
    fc = _PreAckApprovalChild()
    try:
        m = cs.AppServerManager(bin=fc)
        written = []
        def cc_write(msg): written.append(msg)
        def cc_read(timeout=10.0):
            import time as _t; _t.sleep(0.5)        # human takes longer than _ACK_TIMEOUT
            eid = written[-1]["id"] if written else 1
            return {"id": eid, "result": {"action": "accept", "content": {"label": "Allow once"}}}
        r = cs.codex_run_v2({"prompt": "hi", "mcp": "isolated", "mode": "implement"},
                            manager=m, cc_write_fn=cc_write, cc_read_fn=cc_read)
        assert "error" not in r, f"pre-ACK approval tripped a timeout: {r}"
        assert r.get("result") == "ok"
    finally:
        fc.kill()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_codex_mcp_v2.py -k "no_work_duration or opt_in_timeout" -v`
Expected: FAIL — the `120.0` literal is present; no `timeout` handling.

- [ ] **Step 3: Rewrite the turn-loop deadline logic**

In `codex_run_v2`, replace:

```python
        # Unified pump: Phase 1 = waiting for turn/start ACK; Phase 2 = event stream
        turn_acked = False
        deadline = time.time() + 120.0  # generous timeout for real codex
        ack_deadline = time.time() + 10.0

        while time.time() < deadline:
```

with:

```python
        # Unified pump: Phase 1 = waiting for turn/start ACK; Phase 2 = event stream.
        # No work-duration cap by default (match stock — stock has none; codex's own
        # stream_idle_timeout_ms handles a genuinely hung model). Opt-in `timeout`
        # re-imposes a work cap. The turn/start ACK timeout is a SETUP check (the
        # engine must answer turn/start), distinct from limiting WORK duration.
        turn_acked = False
        turn_timeout = args.get("timeout")
        deadline = (time.time() + turn_timeout) if turn_timeout else None
        ack_deadline = time.time() + _ACK_TIMEOUT   # SETUP check (engine must answer turn/start)

        while deadline is None or time.time() < deadline:
```

Add the `_ACK_TIMEOUT` module constant (near the other module constants, e.g. by `_v2_state_machine`) so the test can shrink it:

```python
_ACK_TIMEOUT = 10.0   # turn/start ACK setup timeout (injectable for tests; F6)
```

In the approval credit-back block, credit BOTH `deadline` AND `ack_deadline` (F6 — a pre-ACK approval blocks on HUMAN time, which must not count against the turn/start ACK setup window):

```python
                    _t0 = time.time()
                    manager._write(handle_server_request(frame, cc_write_fn, cc_read_fn, acc=acc))
                    _elapsed = time.time() - _t0
                    if deadline is not None:
                        deadline += _elapsed
                    ack_deadline += _elapsed   # F6: pre-ACK approval is human time, not a setup stall
                    continue
```

Replace the final post-loop "Deadline exceeded" block:

```python
        # Deadline exceeded
        state_machine.turn_completed()
        return _stamp_drift({"error": "turn timed out after 120 s"}, acc)
```

with:

```python
        # Opt-in work-duration deadline exceeded (only reachable when timeout was set).
        state_machine.turn_completed()
        return _stamp_drift({"error": f"turn timed out after {turn_timeout} s"}, acc)
```

The terminal-failure `turn/completed` success path already passes `"completed"` to `_build_result_meta` (Task 6). For the opt-in timeout return, no meta is built (it's an error). Good.

- [ ] **Step 4: Add `timeout` to inputSchema**

In `TOOLS[0]["inputSchema"]["properties"]`, add:

```python
                "timeout": {
                    "type": "number",
                    "description": "Optional work-duration cap in seconds. Omit for no cap "
                                   "(matches stock codex; the engine's stream-idle timeout "
                                   "still catches a hung model).",
                },
```

- [ ] **Step 5: Add a slow long-turn completion test**

```python
@skip_if_no_codex
@pytest.mark.slow
def test_e2e_long_turn_completes_without_self_cap():
    """A real review turn that may exceed 120s must still complete (no self-imposed cap).
    Uses a deliberately heavier prompt; default = no timeout."""
    from codex_server import codex_run_v2
    r = codex_run_v2({
        "prompt": ("Carefully review this for correctness, security, performance, and edge "
                   "cases, enumerating every issue: def parse(s): return eval(s)"),
        "mode": "review", "mcp": "isolated", "effort": "xhigh",
    })
    assert "error" not in r, f"long turn errored (regression: self-cap?): {r.get('error')}"
    assert r.get("verdict") in ("GO", "NO-GO", "MINOR-FIXES")
```

- [ ] **Step 6: Run offline + commit**

Run: `pytest tests/test_codex_mcp_v2.py -m "not slow" -v`
Expected: PASS.

```bash
git add mcp/codex_server.py tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): match-stock no turn cap + opt-in timeout (#204 3a)"
```

---

## Task 9: Schema-generated notification allowlist

Replace the hand-curated `_KNOWN_NOTIFICATIONS` (14) with the full `ServerNotification` set (66 in codex 0.141; count is version-dependent → schema-generated, not load-bearing) generated from the protocol schema; runtime loads a checked-in fixture; coherence test guards drift (decision #8). `error`/`warning` stay excluded.

**Files:**
- Create: `mcp/gen_notifications.py` (maintainer generator).
- Create: `mcp/codex-notifications.json` (generated; checked in — **sibling of `codex_server.py`** so it ships in the plugin cache; F7).
- Modify: `mcp/codex_server.py` — load the fixture into `_KNOWN_NOTIFICATIONS` (with a stdlib fallback).
- Modify: `tests/test_codex_mcp_v2.py` — coherence test.

**Interfaces:**
- Produces:
  - `mcp/gen_notifications.py`: a CLI that dumps the schema and writes the fixture.
  - `_load_known_notifications() -> frozenset` in codex_server.py: loads the fixture, removes `error`/`warning`; falls back to the hand-curated baseline if the file is absent.

- [ ] **Step 1: Recon the schema structure (do this first)**

Run:

```bash
rm -rf /tmp/cas-schema && codex app-server generate-json-schema --out /tmp/cas-schema
ls /tmp/cas-schema
python3 - <<'PY'
import json, glob
# Find the ServerNotification definition and how method names are encoded.
for f in glob.glob("/tmp/cas-schema/*"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    s = json.dumps(d)
    if "ServerNotification" in s:
        print("FILE:", f)
PY
```

Inspect the matched file's `ServerNotification` definition: it is a union (`oneOf`/`anyOf`) where each variant carries a `method` (a `const` or single-value `enum`). Confirm the exact JSON path before writing the extractor in Step 2; adjust the walk if the encoding differs.

- [ ] **Step 2: Write the generator**

Create `mcp/gen_notifications.py`:

```python
#!/usr/bin/env python3
"""Maintainer tool: regenerate mcp/codex-notifications.json from the
live codex app-server protocol schema (the ServerNotification method set).

Usage:  python3 mcp/gen_notifications.py
Requires codex installed. Run after a codex upgrade alongside bumping
LAST_VERIFIED_CODEX_VERSION; commit the regenerated fixture.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))   # mcp/
OUT = os.path.join(HERE, "codex-notifications.json")  # sibling of codex_server.py (ships in cache)


def _walk_consts(node, acc):
    """Collect every {'method': {'const': X}} / {'method': {'enum': [X]}} value."""
    if isinstance(node, dict):
        m = node.get("method")
        if isinstance(m, dict):
            if "const" in m and isinstance(m["const"], str):
                acc.add(m["const"])
            elif isinstance(m.get("enum"), list) and len(m["enum"]) == 1:
                acc.add(m["enum"][0])
        for v in node.values():
            _walk_consts(v, acc)
    elif isinstance(node, list):
        for v in node:
            _walk_consts(v, acc)


def main():
    codex = os.environ.get("JAINE_CODEX_BIN") or "codex"
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([codex, "app-server", "generate-json-schema", "--out", d], check=True)
        methods = set()
        for fn in os.listdir(d):
            try:
                doc = json.load(open(os.path.join(d, fn)))
            except Exception:
                continue
            # ONLY walk the ServerNotification union — NEVER fall through to whole-doc,
            # or ClientRequest.json (85)/ServerRequest.json (10)/ClientNotification.json (1)
            # pollute the set → 162 method names instead of 66 (empirically verified).
            defs = doc.get("definitions") or doc.get("$defs") or {}
            if "ServerNotification" in defs:
                # combined-schema file: restrict to the ServerNotification subtree
                _walk_consts(defs["ServerNotification"], methods)
            elif doc.get("title") == "ServerNotification":
                # ServerNotification.json: the top-level oneOf IS the union
                _walk_consts(doc, methods)
            # else: a request/other schema file — skip entirely
    if not methods:
        print("ERROR: no notification methods extracted — schema shape changed?", file=sys.stderr)
        sys.exit(1)
    payload = {"server_notifications": sorted(methods)}
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"wrote {len(methods)} notifications → {OUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Generate the fixture**

Run: `python3 mcp/gen_notifications.py`
Expected: `wrote 66 notifications → .../codex-notifications.json` (66 in codex 0.141; the exact count is version-dependent — do not hard-assert it). Eyeball the file: it must include `item/agentMessage/delta`, `turn/completed`, `thread/tokenUsage/updated`, `turn/plan/updated`, `item/commandExecution/outputDelta`, and `error`/`warning` (they're in the protocol; the runtime excludes them next).

> If codex is not installed in this environment, hand-author `mcp/codex-notifications.json` from the `/tmp/cas-schema` dump captured in Step 1, with the same `{"server_notifications": [...]}` shape.

- [ ] **Step 4: Write the failing coherence test**

```python
# ---------------------------------------------------------------------------
# Task 9: schema-generated notification allowlist (#204 3b)
# ---------------------------------------------------------------------------

def test_known_notifications_loaded_from_fixture_excludes_error_warning():
    import json, os
    import codex_server as cs
    # F7: fixture lives in mcp/ (sibling of codex_server.py), NOT tests/fixtures/, so
    # it ships in the plugin cache. Read it from MCP_DIR — the SAME path the runtime uses.
    fixture_path = os.path.join(MCP_DIR, "codex-notifications.json")
    assert os.path.isfile(fixture_path), (
        "fixture must live in mcp/ so it ships in the plugin cache (F7)")
    fp = json.load(open(fixture_path))
    fixture = set(fp["server_notifications"])
    # Tight range catches generator pollution: the buggy whole-doc walk yields 162 (it also
    # swept ClientRequest/ServerRequest method names); the correct ServerNotification-only
    # walk yields 66 in codex 0.141. `> 30` passed vacuously for BOTH — too loose.
    assert 60 <= len(fixture) <= 100, (
        f"fixture has {len(fixture)} methods — expected ~66 (codex 0.141 ServerNotification set). "
        "Far from 66 => re-run gen_notifications.py; the walk must be restricted to the "
        "ServerNotification union only (not whole-doc).")
    # runtime constant == fixture minus error/warning (NOT the 14-name fallback)
    assert cs._KNOWN_NOTIFICATIONS == frozenset(fixture - {"error", "warning"})
    assert cs._KNOWN_NOTIFICATIONS != cs._NOTIFICATION_FALLBACK, (
        "must load the generated fixture, not the stdlib fallback (F7 prod-fallback guard)")
    assert "error" not in cs._KNOWN_NOTIFICATIONS
    assert "warning" not in cs._KNOWN_NOTIFICATIONS
    # the previously-spurious ones are now known
    assert "turn/plan/updated" in cs._KNOWN_NOTIFICATIONS

def test_missing_notification_fixture_warns_and_falls_back(tmp_path, monkeypatch):
    """F7: a missing fixture (prod cache miss) must LOG NOTIFICATION_FIXTURE_MISSING and
    degrade to the fallback — not silently regress without a trace."""
    import codex_server as cs
    logf = tmp_path / "d.log"
    monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    result = cs._load_known_notifications(path=str(tmp_path / "nope.json"))
    assert result == cs._NOTIFICATION_FALLBACK
    assert "NOTIFICATION_FIXTURE_MISSING" in logf.read_text()

@pytest.mark.parametrize("bad", [
    '["a","b"]',                         # top-level list, not dict → data.get would AttributeError
    '{"server_notifications": "abc"}',   # string, not list → set("abc") char-soup
    '{"server_notifications": []}',      # empty list
    '{"server_notifications": [1, 2]}',  # non-string entries
    'not json at all {',                 # decode error
])
def test_malformed_notification_fixture_warns_and_falls_back(bad, tmp_path, monkeypatch):
    """F7: a valid-JSON-but-wrong-shape (or undecodable) fixture must warn+fallback, never
    crash at import or load a bogus allowlist."""
    import codex_server as cs
    logf = tmp_path / "d.log"; monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(logf))
    fx = tmp_path / "codex-notifications.json"; fx.write_text(bad)
    result = cs._load_known_notifications(path=str(fx))   # must not raise
    assert result == cs._NOTIFICATION_FALLBACK
    assert "NOTIFICATION_FIXTURE_MISSING" in logf.read_text()
```

- [ ] **Step 5: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -k "known_notifications_loaded" -v`
Expected: FAIL — `_KNOWN_NOTIFICATIONS` is still the hardcoded frozenset.

- [ ] **Step 6: Load the fixture at runtime**

In `mcp/codex_server.py`, replace the `_KNOWN_NOTIFICATIONS = frozenset({...})` block with a loader that keeps the current set as a fallback:

```python
# Benign lifecycle notifications a healthy turn may emit. Sourced from the protocol's
# ServerNotification union (generated by mcp/gen_notifications.py → the checked-in
# fixture), so it stays complete across codex versions. "error"/"warning" are
# deliberately EXCLUDED (they signal problems — surfaced as before, #204). A stdlib
# fallback keeps the server functional if the fixture is missing.
_NOTIFICATION_FALLBACK = frozenset({
    "item/agentMessage/delta", "item/completed", "turn/completed",
    "turn/started", "item/started",
    "thread/settings/updated", "thread/status/changed", "thread/tokenUsage/updated",
    "mcpServer/startupStatus/updated", "hook/started", "hook/completed",
    "account/rateLimits/updated", "skills/changed", "remoteControl/status/changed",
})


def _load_known_notifications(path: str | None = None) -> frozenset:
    # F7: fixture is a SIBLING of this file (mcp/codex-notifications.json) so it ships
    # in the plugin cache. Loading from tests/fixtures/ risked a silent fallback to the
    # 14-name set in production (cache may prune tests/) → spurious _drift (defeats #8).
    # `path` override is for tests.
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "codex-notifications.json")
    # F7: validate SHAPE, not just open/decode — a valid-JSON-but-wrong-shape fixture
    # (a list, or {"server_notifications": "abc"}) must warn+fallback, never crash at import
    # (`data.get` on a list → AttributeError) or load a bogus char-set allowlist (`set("abc")`).
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            reason = f"top-level JSON is {type(data).__name__}, expected object"
        else:
            raw = data.get("server_notifications")
            if not isinstance(raw, list) or not raw:
                reason = "server_notifications missing/empty/not-a-list"
            elif not all(isinstance(x, str) for x in raw):
                reason = "server_notifications has non-string entries"
            else:
                return frozenset(set(raw) - {"error", "warning"})
    except (OSError, json.JSONDecodeError) as e:
        reason = f"{type(e).__name__}: {e}"
    # A missing/malformed fixture in PRODUCTION (cache miss) silently regressing to the
    # 14-name set reintroduces spurious _drift (defeats #8). Make the fallback LOUD in the
    # stable log so a packaging miss is diagnosable, then degrade gracefully.
    _drift_warn(None, "NOTIFICATION_FIXTURE_MISSING", f"{path} ({reason}) — using fallback set")
    return _NOTIFICATION_FALLBACK


_KNOWN_NOTIFICATIONS = _load_known_notifications()
```

> The fixture is a sibling of `codex_server.py` in `mcp/`, so it always ships with the server in the plugin cache (`${CLAUDE_PLUGIN_ROOT}/mcp/`). The coherence test asserts `_KNOWN_NOTIFICATIONS != _NOTIFICATION_FALLBACK`, so a missing/unshipped fixture (silent fallback) fails CI rather than silently regressing production drift.

- [ ] **Step 7: Run to verify it passes + the benign-lifecycle test still holds**

Run: `pytest tests/test_codex_mcp_v2.py -k "known_notifications_loaded or missing_notification_fixture or malformed_notification_fixture or benign_lifecycle or unknown_notification or item_completed_not_flagged" -v` (R4-F1: includes the missing/malformed-fixture fallback regressions)
Expected: PASS.

- [ ] **Step 8: Add the fingerprint pointer + commit**

Add to `tests/fixtures/codex-protocol-fingerprint.json` a pointer key so the coherence story is discoverable (does not change existing keys):

```json
  "notification_set_source": "mcp/codex-notifications.json (generated by mcp/gen_notifications.py)",
```

(Insert before `"last_verified_codex_version"`. No code-constant compares against this string — it's documentation within the coherence file.)

```bash
git add mcp/codex_server.py mcp/gen_notifications.py mcp/codex-notifications.json tests/fixtures/codex-protocol-fingerprint.json tests/test_codex_mcp_v2.py
git commit -m "feat(codex-mcp): schema-generated notification allowlist, fixture in mcp/ (#204 3b, F7)"
```

---

## Task 10: Fake fidelity — align `fake_appserver.py` to the real schema

The fake's approval used `availableDecisions: ["accept", "decline"]`; real codex sends `['accept', {acceptWithExecpolicyAmendment:{…}}, 'cancel']`. Align it so fake-grounded divergences can't ship (decision/spec 3c). Add an offline test exercising the dict-variant + cancel reverse-mapping through the fake.

**Files:**
- Modify: `tests/fixtures/fake_appserver.py` — `_handle_turn_start_with_approval`.
- Modify: `tests/test_codex_mcp_v2.py` — a fake-driven approval round-trip test.

**Interfaces:**
- Consumes: nothing new.
- Produces: the fake's approval request carries the real `availableDecisions` shape + real param keys.

- [ ] **Step 0: Confirm the real `item/commandExecution/requestApproval` param keys (recon, F8) — ALREADY DONE (codex 0.141, 2026-06-20); re-verify only if codex upgraded**

⚠️ **`availableDecisions` is `#[experimental]` → it is NOT in `generate-json-schema`** (the schema generator excludes experimental fields). Its absence from the dump is NOT evidence it's unreal — see spec 3c "Wire-fact (LOCKED)". **Do NOT conclude "availableDecisions doesn't exist" from the schema dump.** Verify it via codex source (`app-server-protocol/src/protocol/v2/item.rs` → `available_decisions: Option<Vec<CommandExecutionApprovalDecision>>`, `#[serde(skip_serializing_if = "Option::is_none")]`) or a live approval probe.

Verified facts (codex 0.141 `CommandExecutionRequestApprovalParams`, top-level keys): `approvalId` (OPTIONAL — present in schema, not in `required`; codex omits it in practice → fake omits it), `command`, `commandActions`, `cwd`, `itemId`, `networkApprovalContext`, `proposedExecpolicyAmendment`, `proposedNetworkPolicyAmendments`, `reason`, `startedAtMs`, `threadId`, `turnId` (`required`: `itemId`/`startedAtMs`/`threadId`/`turnId`). PLUS the experimental `availableDecisions` (absent from the dump, present on the wire when populated). The decision union — SIX variants: `accept`/`acceptForSession`/`{acceptWithExecpolicyAmendment:{execpolicy_amendment:[…]}}`/`{applyNetworkPolicyAmendment:{network_policy_amendment:…}}`/`decline`/`cancel` — lives in `CommandExecutionRequestApprovalResponse → CommandExecutionApprovalDecision` (`decline`=deny+continue, distinct from `cancel`=interrupt; the existing reactor test replies `decline`).

The fake (Steps 1 & 3) therefore models BOTH: the always-present keys (`commandActions`/`proposedExecpolicyAmendment`/`proposedNetworkPolicyAmendments`, FALLBACK-readable) AND the experimental `availableDecisions` in its real shape (string + `acceptWithExecpolicyAmendment` dict + `cancel`, PRIMARY path) — complementary, not contradictory. It drops the (in-practice-omitted) `approvalId`. To re-verify after a codex upgrade: `rm -rf /tmp/cas-schema && codex app-server generate-json-schema --out /tmp/cas-schema && grep -riE "commandActions|proposedExecpolicyAmendment|proposedNetworkPolicyAmendments|approvalId" /tmp/cas-schema/CommandExecutionRequestApprovalParams.json` (and read `item.rs` for the experimental `available_decisions`).

- [ ] **Step 1: Write the failing test**

```python
# ---------------------------------------------------------------------------
# Task 10: fake fidelity — real availableDecisions shape + param keys (#204 3c)
# ---------------------------------------------------------------------------

def test_fake_appserver_approval_uses_real_available_decisions():
    """The fake's approval request must match real codex: string + amendment dict + cancel."""
    import json, subprocess, sys, os, time
    fake = os.path.join(FIXTURES_DIR, "fake_appserver.py")
    env = os.environ.copy(); env["FAKE_SCRIPT"] = "with_approval"
    proc = subprocess.Popen([sys.executable, fake], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        for m in ({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "t"}}},
                  {"id": 2, "method": "thread/start", "params": {"cwd": "/tmp"}},
                  {"id": 3, "method": "turn/start",
                   "params": {"threadId": "T1", "input": [{"type": "text", "text": "hi", "text_elements": []}]}}):
            proc.stdin.write((json.dumps(m) + "\n").encode()); proc.stdin.flush()
        deadline = time.time() + 10
        approval = None
        buf = b""
        while time.time() < deadline and approval is None:
            import select
            r, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not r:
                continue
            buf += os.read(proc.stdout.fileno(), 65536)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                f = json.loads(line)
                if f.get("method") == "item/commandExecution/requestApproval":
                    approval = f
                    # reply so the fake can finish
                    proc.stdin.write((json.dumps({"id": f["id"], "result": {"decision": "cancel"}}) + "\n").encode())
                    proc.stdin.flush()
        assert approval is not None
        params = approval["params"]
        avail = params["availableDecisions"]
        assert "accept" in avail and "cancel" in avail
        assert any(isinstance(x, dict) and "acceptWithExecpolicyAmendment" in x for x in avail)
        # F8 / spec 3c: phantom approvalId gone; real param keys present
        assert "approvalId" not in params, "phantom approvalId must be removed (spec 3c)"
        assert "commandActions" in params, "real codex sends commandActions (spec 3c)"
        assert "proposedExecpolicyAmendment" in params, "real codex sends proposedExecpolicyAmendment (spec 3c)"
    finally:
        proc.stdin.close(); proc.terminate(); proc.wait()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_codex_mcp_v2.py -k "fake_appserver_approval_uses_real" -v`
Expected: FAIL — fake sends `["accept", "decline"]` (no dict, no cancel).

- [ ] **Step 3: Update the fake's approval request**

In `tests/fixtures/fake_appserver.py`, in `_handle_turn_start_with_approval`, change the approval request's `availableDecisions`:

```python
    _server_request(approval_req_id, "item/commandExecution/requestApproval", {
        "threadId": thread_id,
        "turnId": turn_id,
        "itemId": item_id,
        "startedAtMs": int(time.time() * 1000),
        "command": "echo hello",
        "cwd": "/tmp",
        "reason": None,
        # F8 / spec 3c: real param keys (use the Step-0 recon shapes); NO phantom approvalId
        "commandActions": [],
        "proposedExecpolicyAmendment": ["allow echo"],
        "proposedNetworkPolicyAmendments": [],
        "availableDecisions": [
            "accept",
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["allow echo"]}},
            "cancel",
        ],
    })
```

Update the module docstring's "Key wire facts" note: change the approval `e.g.` line to the real shape — `availableDecisions: ['accept', {acceptWithExecpolicyAmendment:{…}}, 'cancel']`, param keys `commandActions`/`proposedExecpolicyAmendment`/`proposedNetworkPolicyAmendments`, and NO `approvalId`.

- [ ] **Step 4: Run to verify it passes + the existing reactor approval test still holds**

Run: `pytest tests/test_codex_mcp_v2.py -k "fake_appserver_approval_uses_real or reactor_sees_server_request" -v`
Expected: PASS — `test_reactor_sees_server_request_frame` still completes (it replies `{"decision": "decline"}`, which the fake accepts as a valid reply regardless of the decisions list).

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/fake_appserver.py tests/test_codex_mcp_v2.py
git commit -m "test(codex-mcp): align fake approval to real availableDecisions shape (#204 3c)"
```

---

## Task 11: Live-codex invariants gate (slow e2e)

The mandatory live verification (Global Constraints). One slow test that proves isolation **by tools-count**, env-no-leak, and auth-untouched — plus running the full slow suite.

**Files:**
- Modify: `tests/test_codex_mcp_v2.py` — new slow tests.

**Interfaces:**
- Consumes: the full bridge (Tasks 1–10).

- [ ] **Step 1: Write the slow invariant tests**

```python
# ---------------------------------------------------------------------------
# Task 11: live-codex invariants (#204) — tools-count isolation, env, auth
# ---------------------------------------------------------------------------

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_env_secret_does_not_leak_to_codex_shell(monkeypatch):
    """A secret in CC's env must NOT be visible to codex's shell (env allowlist)."""
    from codex_server import codex_run_v2
    monkeypatch.setenv("CANARY_SECRET_204", "leak-me-if-you-can")
    r = codex_run_v2({
        "prompt": "Run the shell command `printenv CANARY_SECRET_204 || echo ABSENT` "
                  "and report exactly what it printed, verbatim.",
        "mode": "implement", "mcp": "isolated", "sandbox": "workspace-write",
        "approval_policy": "never", "cwd": "/tmp",
    })
    assert "error" not in r, f"turn errored: {r.get('error')}"
    out = r.get("result") or ""
    # F9: prove the command ACTUALLY RAN (else the secret-absence below is vacuously true).
    assert "ABSENT" in out, f"printenv did not run / report — env test is vacuous: {out!r}"
    assert "leak-me-if-you-can" not in out, "secret leaked into codex shell"

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_auth_files_untouched_by_a_run():
    """A run must not write ~/.codex/auth.json or config.toml (no relocation/mutation)."""
    import os
    from codex_server import codex_run_v2
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    def _snap(p):
        try: return os.stat(p).st_mtime_ns
        except OSError: return None
    before = {f: _snap(os.path.join(home, f)) for f in ("auth.json", "config.toml")}
    r = codex_run_v2({"prompt": "Say OK.", "mode": "implement", "mcp": "isolated"})
    # R2-F2: prove a REAL run happened — else unchanged mtimes are a vacuous pass.
    assert "error" not in r and r.get("thread_id"), f"run did not really execute: {r}"
    after = {f: _snap(os.path.join(home, f)) for f in ("auth.json", "config.toml")}
    assert before == after, f"auth/config mtimes changed: {before} → {after}"
```

- [ ] **Step 1b: Recon the `mcpServerStatus/list` response shape (F9) — ALREADY DONE (codex 0.141, 2026-06-20); re-verify only if codex upgraded**

Verified facts (codex 0.141 `ListMcpServerStatusResponse`): the method string is `mcpServerStatus/list`; the response is `{"data": [McpServerStatus...], "nextCursor": str|null}` — the list is under **`data`** (NOT `servers`). Each `McpServerStatus` has `name` (str), `authStatus`, `resources`, `resourceTemplates`, and **`tools` — an OBJECT/map** (`{<toolName>: Tool}`), NOT a list (so `len(tools)` over the dict = tool count; `isinstance(tools, list)` is FALSE for the real shape). The helper below is corrected for this shape. To re-verify after a codex upgrade: `rm -rf /tmp/cas-schema && codex app-server generate-json-schema --out /tmp/cas-schema && python3 -c "import json;d=json.load(open('/tmp/cas-schema/v2/ListMcpServerStatusResponse.json'));print(sorted(d['properties']),d['definitions']['McpServerStatus']['properties']['tools']['type'])"`.

```python
def _server_tool_counts(manager) -> dict:
    """name → tool count via connection-level mcpServerStatus/list (no thread/start).

    codex 0.141 shape: {"data": [{"name": str, "tools": {<toolName>: Tool}, ...}]}.
    `tools` is a MAP (object), so its tool count is len(dict); the list lives under `data`.
    """
    import codex_server as cs
    mid = manager._next_id()
    manager._write({"id": mid, "method": "mcpServerStatus/list", "params": {}})
    resp = manager._pump_until(
        lambda f: cs.classify(f) == "response" and f.get("id") == mid, timeout=60.0)
    assert resp is not None, "mcpServerStatus/list timed out"
    assert "error" not in resp, f"mcpServerStatus/list errored: {resp.get('error')}"
    # codex 0.141 ListMcpServerStatusResponse: {"data": [McpServerStatus...]}. FAIL LOUD on a
    # shape change rather than silently returning {} (a tolerant `.get("servers")`/`toolCount`
    # fallback would make the F9 isolation test pass vacuously). `name` is required;
    # McpServerStatus.tools is an object MAP (len = tool count).
    raw = resp.get("result") or {}
    servers = raw.get("data") if isinstance(raw, dict) else None
    assert isinstance(servers, list), f"mcpServerStatus/list result.data missing/not-a-list: {raw}"
    counts = {}
    for s in servers:
        name = s.get("name")
        assert name, f"McpServerStatus entry missing required 'name': {s}"
        tools = s.get("tools")
        counts[name] = len(tools) if isinstance(tools, (dict, list)) else 0
    return counts

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_isolated_disables_user_servers_by_tools_count():
    """F9: mcp='isolated' → every user config.toml server reports tools==0. Verify by
    TOOLS-COUNT, NOT name-absence (a disabled server stays in the list)."""
    import codex_server as cs
    config_servers = cs._enumerate_config_mcp_servers()
    if not config_servers:
        pytest.skip("no user MCP servers configured to disable")
    m = cs.AppServerManager(bin=cs.CODEX)
    m.ensure(cs._build_isolation_argv("isolated", config_servers))
    counts = _server_tool_counts(m)
    for name in config_servers:
        # F9: verify by TOOLS-COUNT, not name-absence — a disabled server STAYS in the list.
        # A missing entry is a parse/response failure, NOT "disabled" → must fail, not pass.
        assert name in counts, f"{name} absent from mcpServerStatus/list (parse failure?) — not proof of disable"
        assert counts[name] == 0, f"{name} not disabled: tools={counts[name]} (isolated)"

@skip_if_no_codex
@pytest.mark.slow
def test_e2e_all_loads_user_servers_by_tools_count():
    """F9 counterpart: mcp='all' → at least one user server loads tools (>0), proving the
    isolated test's tools:0 is real disabling, not servers that never had tools."""
    import codex_server as cs
    config_servers = cs._enumerate_config_mcp_servers()
    if not config_servers:
        pytest.skip("no user MCP servers configured")
    m = cs.AppServerManager(bin=cs.CODEX)
    m.ensure(cs._build_isolation_argv("all", config_servers))
    counts = _server_tool_counts(m)
    assert any(counts.get(n, 0) > 0 for n in config_servers), (
        f"no user server loaded tools under 'all' — tools-count test would be vacuous: {counts}")
```

- [ ] **Step 2: Run the full slow suite (the mandatory gate)**

Run: `pytest tests/test_codex_mcp_v2.py -m slow -v` (allow 5–10 min; self-skips entirely without codex)
Expected: PASS — incl. `test_e2e_review_real_appserver`, `test_e2e_implement_real_appserver`, `test_e2e_control_knobs_echoed_by_real_appserver`, `test_e2e_long_turn_completes_without_self_cap`, `test_e2e_env_secret_does_not_leak_to_codex_shell`, `test_e2e_auth_files_untouched_by_a_run`, `test_e2e_isolated_disables_user_servers_by_tools_count`, `test_e2e_all_loads_user_servers_by_tools_count`, `test_live_codex_version_matches_pin`, `test_resume_by_thread_id_recalls_across_restart`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_codex_mcp_v2.py
git commit -m "test(codex-mcp): live-codex invariants — tools-count isolation, env, auth (#204)"
```

---

## Task 12: Documentation — finalize the CLAUDE.md codex-MCP section + tool description

Update the bulldozer `CLAUDE.md` "codex MCP server" section: the isolation is SHIPPED (not "fix pending"), document the `mcp` knob, the additive metadata, the first-class knobs, and the no-cap behavior.

**Files:**
- Modify: `CLAUDE.md` (bulldozer) — the "Architecture: codex MCP server" section.

**Interfaces:** docs only.

- [ ] **Step 1: Rewrite the isolation bullet**

In `CLAUDE.md`, replace the bullet that currently begins **"- Isolation — KNOWN-BROKEN, fix pending (A3IO/jaine-plugins#204):"** with:

```markdown
- **Isolation — per-call, spawn-level (#204, shipped):** the REQUIRED `mcp` arg selects which
  MCP servers codex sees each call — `"isolated"` (disable all user `config.toml` servers +
  `apps`), `"all"` (full toolset), `["dash", …]` (keep only those), or `"list"` (discover, no
  run). Enforced at SPAWN via `-c mcp_servers.<n>.enabled=false` (BARE keys — quoting crashes app-server) + `--disable apps` (the only
  mechanism that works on 0.141 — per-thread `config.mcp_servers` is a no-op). **Verify disabled
  by `tools:0`, NOT name-absence** (a disabled server stays in `mcpServerStatus/list`). No
  `CODEX_HOME` relocation → auth/sessions untouched. computer-use (bundled plugin) cannot be
  ephemerally disabled, so it remains even in `"isolated"` (documented limitation). Because the
  app-server is a session singleton, the manager respawns (re-paying cold start) only when the
  `mcp` selection changes; same-selection calls stay warm. The child env is fail-closed
  allowlisted so CC secrets never reach codex's shell.
```

- [ ] **Step 2: Update the surface + robustness notes**

Replace the parenthetical `(Related #204 findings: codex app-server inherits CC's full env — secrets reachable by codex shell commands; `thread/tokenUsage/updated` arrives but `codex_run`'s result drops it.)` with:

```markdown
  The result is ADDITIVE-extended with `usage` (token snapshot), `codex`
  (model/service_tier/effort/approvals_reviewer/mcp_mode/mcp_servers_enabled), `timing.duration_ms`,
  and `status` — existing keys (`thread_id`/`verdict`/`findings`/`schema_ok`/`result`) unchanged.
  First-class knobs: `approvals_reviewer`, `service_tier` (output `verbosity` is config-passthrough
  via `model_verbosity`). The self-imposed 120s turn cap is GONE (matches stock — codex's stream-idle
  timeout still catches a hung model); opt-in `timeout` re-imposes a cap. `_KNOWN_NOTIFICATIONS` is
  generated from the protocol schema (`mcp/gen_notifications.py` → `mcp/codex-notifications.json`, a
  sibling of `codex_server.py` so it ships in the cache; runtime logs `NOTIFICATION_FIXTURE_MISSING`
  if it ever falls back); `error`/`warning` stay out of the benign set.
```

- [ ] **Step 3: Verify no stale "fix pending"/"KNOWN-BROKEN" references remain + no doc line-numbers**

Run: `grep -nE "KNOWN-BROKEN|fix pending|mcp_servers=\{\}|51\.6|34\.7|120 s|120s" CLAUDE.md`
Expected: no matches (the no-op + refuted-timing + self-cap language is gone).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(codex-mcp): finalize #204 — shipped isolation, mcp knob, surface, no-cap"
```

---

## Self-Review (run after all tasks; checklist, not a subagent dispatch)

**1. Spec coverage** — map each spec section to a task:

| Spec section | Task(s) |
|---|---|
| 1a disable mechanism (per-server `-c`, `--disable apps`, computer-use remains) | 1, 3 |
| 1b flexible `mcp` knob — REQUIRED + `list` discovery | 5 |
| 1c env isolation (secret leak, allowlist) | 2, 3, 11 |
| no `CODEX_HOME` relocation; drop no-op per-thread clear | 4, (3 comment) |
| 2a tokenUsage + metadata (additive) | 6 |
| 2b first-class knobs (approvals_reviewer/service_tier/verbosity) | 7 |
| 3a no turn cap + opt-in timeout | 8 |
| 3b schema-generated notification allowlist | 9 |
| 3c fake fidelity | 10 |
| Invariants (tools-count isolation, env, auth, resume, version) | 11 |
| Stale code-comment fixes (ISOLATION_CONFIG / 51.6→34.7) | 3 |
| Docs finalize | 12 |

No spec section is unmapped.

**Round-1 `/bulldozer:check` (gpt-5.5, standard) findings — all 11 incorporated:**
F1 (shell_environment_policy layer 2) → T2/T3 · F2 (subset validation + computer-use metadata) → T5/T6 · F3 (TOML key escaping → wire-audit OVERTURNED: quoting CRASHES app-server, use bare keys) → T1 · F4 (knobs fail-loud on resume) → T7 · F5 (knob test validity: stale thread/start + live echo) → T7 · F6 (credit ack_deadline for pre-ACK approval) → T8 · F7 (fixture under `mcp/` so it ships) → T9 · F8 (fake completeness per spec 3c) → T10 · F9 (automated tools-count + env-ran assertions) → T11 · F10 (CODEX_HOME isolation fixture) → T1 · F11 (metadata on failure path) → T6. None contradicted a locked decision.

**Round-2 `/bulldozer:check` (gpt-5.5) — 5 round-1 fixes verified (F4/F6/F8/F10/F11); 6 still-open + 2 new triaged:**
R1-F2 (type-check before `in` to avoid `list in frozenset` TypeError + reject unknown subset pre-spawn) → T5/T6 · R1-F7 (runtime `NOTIFICATION_FIXTURE_MISSING` warn on fallback + all path pointers → `mcp/`) → T9/T12 · R1-F9 (tools-count test asserts name PRESENT + count==0, no error — not absence-as-zero) → T11 · R2-F2 (auth test asserts a real run before mtime compare) → T11. **By user decision:** R1-F1 (layer-2) kept-as-flag + recon-verified, no fragile auto-test; R1-F5 verbosity DEMOTED to `config={"model_verbosity": …}` passthrough; R1-F3 (TOML key escaping) — originally WONTFIX-as-pedantic, **OVERTURNED by the post-plan wire-audit** (see below): quoting the key doesn't just need escaping, it CRASHES app-server startup → `_toml_key` REMOVED entirely. **R2-F1 was a FALSE POSITIVE** (claimed syntax error in a test; `ast.parse` confirmed it compiles). None contradicted a locked decision.

**Post-plan wire-audit (2026-06-20, 9 agents vs live codex 0.141, then every load-bearing finding re-verified by hand via schema dump + Rust source + a real app-server probe):**
- **CRITICAL — `_toml_key` quoting CRASHES app-server** (`-c mcp_servers."dash".enabled=false` → `Error: …invalid transport in mcp_servers."dash"`, rc=1; bare `mcp_servers.dash.enabled=false` → dash tools 4→0; probe-verified). `mcp="isolated"` would hard-crash for any user with MCP servers. FIX: `_toml_key` removed; bare keys; `_is_targetable_server_name` skips names with `.`/`"` (warn, leave enabled). Overturns R1-F3.
- **CRITICAL — `gen_notifications.py` polluted the allowlist** (whole-doc fallback swept ClientRequest/ServerRequest method names → 162 instead of 66; walk-verified). FIX: walk ONLY the ServerNotification union (title/defs guard); coherence test tightened to `60 ≤ len ≤ 100`.
- **`tokenUsage` wire shape** (`params.tokenUsage.{last,total}` camelCase, not `params.usage` snake_case) — already fixed in Task 6 + slow assertion (schema-verified).
- **`CODEX_CA_CERTIFICATE`** added to the env allowlist (real codex CA var, precedence over `SSL_CERT_FILE`; grep-verified). `OPENAI_BASE_URL` confirmed config-only (no env read) — kept as harmless.
- **`success` phantom TurnStatus removed** (`!= "completed"`; TurnStatus = completed/interrupted/failed/inProgress, grep-verified). `decline` added to the decision-union prose (6 variants; schema-verified).

**2. Placeholder scan** — every code step shows complete code; every run step shows the exact command + expected result. The two recon steps (7.1, 9.1) are empirical-key-pinning prerequisites with concrete commands, not placeholders. The manual tools-count check in 11 is a documented one-time probe (no clean CI assertion exists for it), explicitly flagged.

**3. Type/name consistency** — `_build_isolation_argv`/`_enumerate_config_mcp_servers`/`_build_child_env`/`_spawn_appserver(codex_bin, isolation_argv)`/`AppServerManager.ensure(isolation_argv)`/`_build_result_meta`/`_shape_result(mode, thread_id, final_text, meta)`/`_load_known_notifications` are spelled identically across the tasks that define and consume them. `call_codex_run(..., mcp="isolated")` and `_started_params(**kwargs)` are the two test helpers extended in place. Result keys (`usage`/`codex`/`timing`/`status` + the unchanged core) match between Task 6's implementation and Task 11's assertions.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-20-codex-mcp-bridge-hardening-plan.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task + two-stage review between tasks. Best for this 12-task plan (each task is an independent reviewer gate).
2. **Inline Execution** — execute tasks in this session via `superpowers:executing-plans`, batched with checkpoints.

Which approach?
