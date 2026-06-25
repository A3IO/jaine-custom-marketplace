# consult: per-model selection + opt-in `--web` deep research — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every panel model individually selectable and add an opt-in per-model `--web` deep-research mode (web search + subagents, read-side only), persisting raw research to a `.bulldozer/consult-<ts>/` bundle.

**Architecture:** All work is in the existing `_MODEL_SPECS`-driven engine. Selection = run a SUBSET of `_MODEL_SPECS` keys (today `run_panel` iterates all). Web = thread a per-model `web: bool` through `_run_one` → `ModelSpec.prepare` (codex/grok: command changes) and → `_seed_readonly_hook` (agy: hook ALLOW-set widens). Output reuses the existing synthesis path with a `--web`-only per-model pre-compress + a raw-to-file bundle. Bare `consult "Q"` keeps its inline SKILL.md path (D1); explicit selection routes to the engine.

**Tech Stack:** Python 3 stdlib (argparse, dataclasses, tempfile, ThreadPoolExecutor), pytest (offline, injected `runner`), bash SKILL.md glue. External CLIs: codex / grok / agy.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-21-consult-web-per-model-design.md`. Every task implicitly includes its requirements.
- `--web` is READ-side autonomy only — NEVER enable write/shell for any model. agy hook keeps denying `run_command`/writes/unknown/malformed (fail-closed).
- Default (no web) output must stay **byte-identical** to today (no regression in the existing 142 tests).
- codex web key = canonical **`-c web_search="live"`** (TOML string — exact quoting), NOT deprecated `tools.web_search`.
- grok web = drop `--no-subagents` + `--disable-web-search`; **always keep `--permission-mode plan`** (read-only) and `--no-memory`.
- Non-`--web` consult stays no-trace; the bundle is written ONLY when `web_models` is non-empty.
- Code refs by function name / grep anchor, never line numbers (repo doctrine).
- Frequent commits: one per task. Do NOT bump `plugin.json` (auto-calver handles it on merge).

## File Structure

- **Modify** `skills/consult/scripts/consult_panel.py` — the engine: command builders, agy hook generator, `ModelSpec.prepare` signature, `_run_one`, `run_panel`, argparse, `main`, pre-compress + bundle helpers.
- **Modify** `skills/consult/SKILL.md` — routing for per-model flags + `--web`; "Web lane" doc section; argument-hint/description.
- **Modify** `tests/test_consult_panel.py` — add offline tests per task (import pattern: `panel = _load_panel()`, call `panel.<fn>`; inject a capturing `runner`).

All tests run: `cd /0/ANTHROPICS_DEV/jaine-plugins/.claude/worktrees/consult-219 && python3 -m pytest tests/test_consult_panel.py -q` (offline; the slow live case self-skips).

---

### Task 1: Per-model `web` in the command builders (codex + grok)

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → `build_codex_cmd`, `build_grok_cmd`
- Test: `tests/test_consult_panel.py`

**Interfaces:**
- Produces: `build_codex_cmd(wrapped, effort="medium", web=False) -> list[str]`; `build_grok_cmd(wrapped, web=False) -> tuple[list[str], dict[str,str]]`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_codex_cmd_web_adds_live_search():
    cmd = panel.build_codex_cmd("Q", web=True)
    assert '-c' in cmd and 'web_search="live"' in cmd
    assert cmd[-1] == "Q"  # prompt stays last

def test_build_codex_cmd_no_web_has_no_search():
    cmd = panel.build_codex_cmd("Q")
    assert 'web_search="live"' not in cmd

def test_build_grok_cmd_web_drops_isolation_keeps_plan():
    cmd, _ = panel.build_grok_cmd("Q", web=True)
    assert "--no-subagents" not in cmd
    assert "--disable-web-search" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"  # read-only retained
    assert "--no-memory" in cmd

def test_build_grok_cmd_no_web_unchanged():
    cmd, _ = panel.build_grok_cmd("Q")
    assert "--no-subagents" in cmd and "--disable-web-search" in cmd
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "build_codex_cmd_web or build_grok_cmd_web" -q`
Expected: FAIL (`build_codex_cmd() got an unexpected keyword argument 'web'`).

- [ ] **Step 3: Implement**

Replace `build_codex_cmd`:
```python
def build_codex_cmd(wrapped: str, effort: str = "medium", web: bool = False) -> list[str]:
    """codex: full isolation via flags, no HOME override. ``web`` adds the canonical
    live web-search config (`-c web_search="live"`; NOT the deprecated tools.web_search)."""
    cmd = [
        "codex", "exec",
        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
        "--ephemeral", "-s", "read-only",
        "-c", f"model_reasoning_effort={effort}",
    ]
    if web:
        cmd += ["-c", 'web_search="live"']
    cmd.append(wrapped)
    return cmd
```

Replace `build_grok_cmd`:
```python
def build_grok_cmd(wrapped: str, web: bool = False) -> tuple[list[str], dict[str, str]]:
    """grok on the REAL HOME (no override) + JSON out, ALWAYS --permission-mode plan
    (read-only). Isolated (default): also --no-subagents/--disable-web-search. ``web``:
    drop those two → web search/fetch + parallel subagents, still read-only (plan blocks
    writes — verified). --no-memory always (cross-session memory off)."""
    cmd = ["grok", "-p", wrapped, "--no-memory"]
    if not web:
        cmd += ["--no-subagents", "--disable-web-search"]
    cmd += ["--permission-mode", "plan", "--output-format", "json"]
    return cmd, {}
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `python3 -m pytest tests/test_consult_panel.py -k "build_codex_cmd or build_grok_cmd" -q`
Expected: PASS (incl. any pre-existing build_*_cmd tests — no regression).

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py tests/test_consult_panel.py
git commit -m "feat(consult): per-model --web in codex/grok command builders"
```

---

### Task 2: Per-model `web` in the agy read-only hook

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → `_AGY_READONLY_HOOK` (→ generator), `_seed_readonly_hook`
- Test: `tests/test_consult_panel.py`

**Interfaces:**
- Produces: `_agy_readonly_hook_src(web: bool=False) -> str`; `_seed_readonly_hook(workdir: Path, web: bool=False) -> None`
- The hook ALLOW-set = base local-read tools, plus `search_web`+`read_url_content` iff `web`; everything else (incl. `run_command`, writes, unknown, malformed) still denies.

- [ ] **Step 1: Write the failing tests**

```python
def test_agy_hook_no_web_denies_search_web(tmp_path):
    panel._seed_readonly_hook(tmp_path, web=False)
    src = (tmp_path / ".agents" / "readonly-hook.py").read_text()
    assert "search_web" not in src
    assert "view_file" in src           # base reads kept
    assert "run_command" not in src     # never allowed

def test_agy_hook_web_allows_search_web_and_url(tmp_path):
    panel._seed_readonly_hook(tmp_path, web=True)
    src = (tmp_path / ".agents" / "readonly-hook.py").read_text()
    assert "search_web" in src and "read_url_content" in src
    assert "run_command" not in src     # still denied — read-side only

def test_agy_hook_denies_run_command_at_runtime(tmp_path):
    import subprocess as sp
    panel._seed_readonly_hook(tmp_path, web=True)
    hook = tmp_path / ".agents" / "readonly-hook.py"
    out = sp.run(["python3", str(hook)], input='{"toolCall":{"name":"run_command"}}',
                 capture_output=True, text=True).stdout
    assert '"deny"' in out
    out2 = sp.run(["python3", str(hook)], input='{"toolCall":{"name":"search_web"}}',
                  capture_output=True, text=True).stdout
    assert '"allow"' in out2
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "agy_hook" -q`
Expected: FAIL (`_seed_readonly_hook() got an unexpected keyword argument 'web'`).

- [ ] **Step 3: Implement**

Replace the module-level `_AGY_READONLY_HOOK = '''...'''` constant with base/web sets + a generator (keep the existing fail-closed body verbatim, only the ALLOW set becomes parameterized):
```python
_AGY_ALLOW_BASE = (
    "read_file", "view_file", "view_code_item", "list_dir", "glob",
    "grep_search", "code_search", "codebase_search", "search_file_content", "find_by_name",
)
_AGY_ALLOW_WEB = ("search_web", "read_url_content")  # added ONLY with --web (read-side egress)

def _agy_readonly_hook_src(web: bool = False) -> str:
    """Fail-closed PreToolUse deny hook source. EXACT-name allowlist of read tools →
    allow; everything else (writes, run_command, unknown, malformed) → deny. ``web`` adds
    the two web READ tools — never any write/exec tool (#189)."""
    allow = list(_AGY_ALLOW_BASE) + (list(_AGY_ALLOW_WEB) if web else [])
    allow_literal = "{\n    " + ", ".join(repr(a) for a in allow) + ",\n}"
    return (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"ALLOW = {allow_literal}\n"
        "try:\n"
        "    name = json.load(sys.stdin)[\"toolCall\"][\"name\"]\n"
        "    ok = isinstance(name, str) and name.lower() in ALLOW\n"
        "except Exception:\n"
        "    ok = False\n"
        "print('{\"decision\":\"allow\"}' if ok\n"
        "      else '{\"decision\":\"deny\",\"reason\":\"bulldozer consult: read-only review, mutation blocked\"}')\n"
    )
```

Update `_seed_readonly_hook` to take `web` and write the generated source:
```python
def _seed_readonly_hook(workdir: Path, web: bool = False) -> None:
    agents = workdir / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    script = agents / "readonly-hook.py"
    script.write_text(_agy_readonly_hook_src(web))
    script.chmod(0o755)
    hooks = {
        "bulldozer-readonly": {
            "enabled": True,
            "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": str(script)}]}],
        }
    }
    (agents / "hooks.json").write_text(json.dumps(hooks))
```

(Delete the old `_AGY_READONLY_HOOK` constant; if any test referenced it by name, point it at `_agy_readonly_hook_src(False)`.)

- [ ] **Step 4: Run tests — verify they pass**

Run: `python3 -m pytest tests/test_consult_panel.py -k "agy_hook or readonly" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py tests/test_consult_panel.py
git commit -m "feat(consult): widen agy read-only hook ALLOW for --web (search_web/read_url_content)"
```

---

### Task 3: Thread `web` through `ModelSpec.prepare` + `_run_one`

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → `ModelSpec.prepare` type, the three `_MODEL_SPECS` lambdas, `_run_one`
- Test: `tests/test_consult_panel.py`

**Interfaces:**
- Consumes: `build_codex_cmd(..., web)`, `build_grok_cmd(..., web)`, `_seed_readonly_hook(..., web)` (Tasks 1–2).
- Produces: `prepare: Callable[[str, Path|None, int, bool], tuple[list[str], dict[str,str]]]`; `_run_one(name, wrapped, repo, timeout, runner, web=False) -> LegResult`.

- [ ] **Step 1: Write the failing test** (capturing runner records argv)

```python
def _capture_runner(box):
    def runner(cmd, env, cwd, timeout):
        box.append(cmd)
        return panel.ModelResult(ok=True, output="ok", reason=None)
    return runner

def test_run_one_codex_web_threads_live_search():
    box = []
    panel._run_one("codex", "WRAPPED", None, 60, _capture_runner(box), web=True)
    assert 'web_search="live"' in box[0]

def test_run_one_grok_no_web_keeps_isolation():
    box = []
    panel._run_one("grok", "WRAPPED", None, 60, _capture_runner(box), web=False)
    assert "--disable-web-search" in box[0]
```

- [ ] **Step 2: Run — verify fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "run_one_codex_web or run_one_grok_no_web" -q`
Expected: FAIL (`_run_one() got an unexpected keyword argument 'web'`).

- [ ] **Step 3: Implement**

Change `ModelSpec.prepare` annotation to 4-arg:
```python
    prepare: Callable[[str, "Path | None", int, bool], tuple[list[str], dict[str, str]]]
```
Update the three lambdas in `_MODEL_SPECS`:
```python
    "codex": ModelSpec("GPT", parse_codex, lambda w, repo, t, web: (build_codex_cmd(w, web=web), {})),
    "grok": ModelSpec("Grok", parse_grok, lambda w, repo, t, web: build_grok_cmd(w, web=web),
                      session_clean=_grok_post_run_clean),
    "agy": ModelSpec("Gemini", parse_agy, lambda w, repo, t, web: build_agy_cmd(w, repo, t),
                     readonly_hook=True),
```
Add `web: bool = False` to `_run_one`, pass it to every `spec.prepare(..., web)` call (3 sites) and to `_seed_readonly_hook(Path(mt), web)` in the `readonly_hook` branch.

- [ ] **Step 4: Run — verify pass** (full file, no regression)

Run: `python3 -m pytest tests/test_consult_panel.py -q`
Expected: PASS (all, incl. the 142 pre-existing).

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py tests/test_consult_panel.py
git commit -m "feat(consult): thread per-model web through ModelSpec.prepare + _run_one"
```

---

### Task 4: Per-model selection + `--web` parsing in `run_panel` and the CLI

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → `run_panel`, `_build_parser`, `main`
- Test: `tests/test_consult_panel.py`

**Interfaces:**
- Produces: `run_panel(question, *, models=None, web_models=None, verdict_mode=False, repo=None, timeout=180, runner=run_model) -> tuple[str, bool]`. `models` defaults to all `_MODEL_SPECS` keys; `web_models ⊆ models`.
- CLI: `--codex/--grok/--agy` (store_true), `--panel` (store_true, = all three), `--web` (`nargs="?"`, `const="__ALL__"`, default `None`).

- [ ] **Step 1: Write failing tests**

```python
def test_run_panel_runs_only_selected_models():
    seen = []
    def runner(cmd, env, cwd, timeout):
        seen.append(cmd[0])
        return panel.ModelResult(ok=True, output="VERDICT: GO", reason=None)
    panel.run_panel("Q", models=["grok"], verdict_mode=True, runner=runner)
    assert set(seen) == {"grok"}

def test_parser_web_bare_is_blanket_sentinel():
    args = panel._build_parser().parse_args(["--grok", "--web", "my question"])
    assert args.web == "__ALL__"
    assert args.question == "my question"   # --web did NOT eat the positional

def test_parser_web_scoped_list():
    args = panel._build_parser().parse_args(["--panel", "--web=codex,grok", "Q"])
    assert args.web == "codex,grok"

def test_main_rejects_unknown_web_model():
    import pytest
    with pytest.raises(SystemExit):
        panel.main(["--grok", "--web=nope", "Q"], runner=lambda *a: panel.ModelResult(True, "x", None))
```

- [ ] **Step 2: Run — verify fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "run_panel_runs_only_selected or parser_web or main_rejects_unknown_web" -q`
Expected: FAIL (`run_panel() got an unexpected keyword argument 'models'` / `--web` unrecognized).

- [ ] **Step 3: Implement**

`run_panel` — accept `models`/`web_models`, iterate the selection:
```python
def run_panel(question, *, models=None, web_models=None, verdict_mode=False,
              repo=None, timeout=180, runner=run_model):
    selected = list(models) if models else list(_MODEL_SPECS)
    webset = set(web_models or ())
    ...  # repo validation unchanged
    wrapped = wrap(question, verdict=verdict_mode, repo=repo is not None)
    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as ex:
        results = list(ex.map(
            lambda n: _run_one(n, wrapped, repo, timeout, runner, n in webset), selected))
    ...  # survivors/failures/merge unchanged
```

`_build_parser` — add flags:
```python
    p.add_argument("--codex", action="store_true", help="Run codex")
    p.add_argument("--grok", action="store_true", help="Run grok")
    p.add_argument("--agy", action="store_true", help="Run agy (Gemini)")
    p.add_argument("--panel", action="store_true", help="Run all three (alias)")
    p.add_argument("--web", nargs="?", const="__ALL__", default=None,
                   help='Opt-in deep web research; bare = all selected, or --web=grok,agy')
```

`main` — resolve selection + web, validate, call `run_panel`:
```python
def main(argv=None, runner=run_model):
    args = _build_parser().parse_args(argv)
    parser = _build_parser()
    selected = [m for m in ("codex", "grok", "agy") if getattr(args, m)]
    if not selected:
        selected = ["codex", "grok", "agy"]          # --panel or bare panel call = all
    if args.web is None:
        web_models = set()
    elif args.web == "__ALL__":
        web_models = set(selected)
    else:
        req = [s.strip() for s in args.web.split(",") if s.strip()]
        bad = [r for r in req if r not in ("codex", "grok", "agy")]
        if bad:
            parser.error(f"--web: unknown model(s): {', '.join(bad)}")
        not_sel = [r for r in req if r not in selected]
        if not_sel:
            parser.error(f"--web names non-selected model(s): {', '.join(not_sel)}")
        web_models = set(req)
    try:
        output, ok = run_panel(args.question, models=selected, web_models=web_models,
                               verdict_mode=args.verdict, repo=args.repo,
                               timeout=args.timeout, runner=runner)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except Exception as e:
        print(f"panel error: {e}", file=sys.stderr); return 2
    print(output)
    return 0 if ok else 1
```
(Keep the existing print/return contract — verify against current `main` body and preserve it.)

- [ ] **Step 4: Run — verify pass** (full file)

Run: `python3 -m pytest tests/test_consult_panel.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py tests/test_consult_panel.py
git commit -m "feat(consult): per-model selection flags + --web parsing (blanket/scoped, argparse-safe)"
```

- [ ] **Step 6: DOGFOOD checkpoint (live)**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/.claude/worktrees/consult-219
python3 skills/consult/scripts/consult_panel.py --grok "Is a token bucket better than a leaky bucket for bursty API traffic?"
python3 skills/consult/scripts/consult_panel.py --grok --web "Latest 2026 community practice for distributed rate limiting?"
```
Expected: first runs grok-only, no web (fast, from weights). Second runs grok with web → cited URLs in output. If either fails → STOP, diagnose (do not proceed to Task 5).

---

### Task 5: `--web` per-model pre-compress before merge

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → new `_compress_research`, wire into `run_panel` (web path only)
- Test: `tests/test_consult_panel.py`

**Interfaces:**
- Produces: `_compress_research(raw: str, timeout: int, runner: Runner) -> str` — isolated codex call condensing raw research → "findings + URL citations"; on failure returns the raw unchanged (degrade, never crash).
- In `run_panel`: when `webset` non-empty, each survivor whose model is in `webset` has its `output` replaced by the compressed digest BEFORE `decide_merge`/summarizer. Non-web survivors untouched. Keep the original raw for the bundle (Task 6).

- [ ] **Step 1: Write failing test** (injected runner returns a short digest)

```python
def test_compress_research_uses_codex_and_returns_digest():
    def runner(cmd, env, cwd, timeout):
        assert cmd[0] == "codex"                 # isolated codex
        return panel.ModelResult(ok=True, output="DIGEST: 3 findings + 2 URLs", reason=None)
    out = panel._compress_research("...94KB of raw...", 60, runner)
    assert "DIGEST" in out

def test_compress_research_degrades_to_raw_on_failure():
    def runner(cmd, env, cwd, timeout):
        return panel.ModelResult(ok=False, output=None, reason="boom")
    assert panel._compress_research("RAWTEXT", 60, runner) == "RAWTEXT"
```

- [ ] **Step 2: Run — verify fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "compress_research" -q`
Expected: FAIL (`module has no attribute '_compress_research'`).

- [ ] **Step 3: Implement**

```python
_COMPRESS_PROMPT = (
    "Condense the following web-research notes into a tight briefing: the key findings "
    "as bullets, then a '## Sources' list of every URL cited. Preserve all URLs. Drop "
    "filler and any duplicated/garbled fragments. Output markdown only.\n\n---\n"
)

def _compress_research(raw: str, timeout: int, runner: Runner) -> str:
    """Per-model --web pre-compress: an isolated codex pass turning a large/possibly-
    garbled raw research dump into findings + a URL index. Degrades to the raw text on
    any failure (never crashes the panel)."""
    try:
        with tempfile.TemporaryDirectory(prefix="panel-compress-") as mt:
            cmd = build_codex_cmd(_COMPRESS_PROMPT + raw)   # no web — just summarize
            result = runner(cmd, {}, mt, timeout)
    except Exception:
        return raw
    if not result.ok:
        return raw
    return parse_codex(result.output or "") or raw
```

In `run_panel`, after building `survivors` and before `decide_merge`, when `webset`:
```python
    raw_by_display = {d: o for d, o in survivors}   # keep originals for the bundle (Task 6)
    if webset:
        web_displays = {_MODEL_SPECS[n].display for n in webset}
        survivors = [
            (d, _compress_research(o, timeout, runner) if d in web_displays else o)
            for d, o in survivors
        ]
```

- [ ] **Step 4: Run — verify pass** (full file)

Run: `python3 -m pytest tests/test_consult_panel.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py tests/test_consult_panel.py
git commit -m "feat(consult): per-model --web pre-compress before merge (volume + grok-corruption fix)"
```

---

### Task 6: `--web` raw bundle — `.bulldozer/consult-<ts>/` + self-ignore + keep-last-10

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → new `_write_web_bundle`, `_prune_bundles`; call from `run_panel` (web path)
- Test: `tests/test_consult_panel.py`

**Interfaces:**
- Produces: `_write_web_bundle(base: Path, ts: str, synthesis: str, raw_by_display: dict[str,str], web_displays: set[str]) -> Path` (returns the bundle dir); `_prune_bundles(base: Path, keep: int = 10) -> None`.
- `base` defaults to `Path(".bulldozer")` (cwd-relative, consumer project root). Bundle dir `consult-<ts>/`. Writes `research.md` (synthesis) + `raw-<model>.md` for each web display. Ensures self-ignoring `.bulldozer/.gitignore` (`*`). Prunes to keep-last-10. Best-effort: a write failure must NOT crash the panel (log to stderr, continue).

- [ ] **Step 1: Write failing tests**

```python
def test_write_web_bundle_layout(tmp_path):
    base = tmp_path / ".bulldozer"
    d = panel._write_web_bundle(base, "20260621-120000", "SYNTH",
                                {"Grok": "rawgrok", "GPT": "rawgpt"}, {"Grok"})
    assert (d / "research.md").read_text() == "SYNTH"
    assert (d / "raw-grok.md").read_text() == "rawgrok"     # web model → raw file
    assert not (d / "raw-gpt.md").exists()                   # non-web model → no raw file
    assert (base / ".gitignore").read_text().strip() == "*"  # self-ignoring

def test_prune_bundles_keeps_last_n(tmp_path):
    base = tmp_path / ".bulldozer"; base.mkdir()
    for i in range(13):
        (base / f"consult-2026062{i:02d}").mkdir()
    panel._prune_bundles(base, keep=10)
    left = sorted(p.name for p in base.glob("consult-*"))
    assert len(left) == 10 and left[-1] == "consult-20260612"   # newest kept
```

- [ ] **Step 2: Run — verify fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "write_web_bundle or prune_bundles" -q`
Expected: FAIL (`no attribute '_write_web_bundle'`).

- [ ] **Step 3: Implement**

```python
def _prune_bundles(base: Path, keep: int = 10) -> None:
    """Keep only the newest ``keep`` consult-<ts> dirs (sortable ts → lexical sort)."""
    dirs = sorted((p for p in base.glob("consult-*") if p.is_dir()), key=lambda p: p.name)
    for old in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(old, ignore_errors=True)

def _write_web_bundle(base, ts, synthesis, raw_by_display, web_displays):
    """Persist a --web research bundle. Best-effort: never raises."""
    try:
        base.mkdir(parents=True, exist_ok=True)
        gi = base / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n")                 # self-ignoring; consumer .gitignore untouched
        _prune_bundles(base)
        d = base / f"consult-{ts}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "research.md").write_text(synthesis)
        for display, raw in raw_by_display.items():
            if display in web_displays:
                (d / f"raw-{display.lower()}.md").write_text(raw)
        return d
    except Exception as e:
        print(f"warning: could not write consult bundle: {e}", file=sys.stderr)
        return base / f"consult-{ts}"
```
Add `import shutil` near the other stdlib imports if absent. In `run_panel` (web path, after rendering `output`): compute `ts` from a passed-in/`run_model`-side timestamp — `run_panel` must NOT call `Date.now`-style impurity in a way that breaks tests, so accept an optional `ts: str | None = None`; when None, derive once via `time.strftime("%Y%m%d-%H%M%S")` (import `time` already present). Call `_write_web_bundle(Path(".bulldozer"), ts, output, raw_by_display, web_displays)` and append a one-line `\n\n_Raw research: <dir>/_` pointer to `output`.

- [ ] **Step 4: Run — verify pass** (full file)

Run: `python3 -m pytest tests/test_consult_panel.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py tests/test_consult_panel.py
git commit -m "feat(consult): --web raw bundle (.bulldozer/consult-<ts>/, self-ignore, keep-last-10)"
```

---

### Task 7: `--web` timeout default + SKILL.md routing & docs

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py` → `main` (web timeout default)
- Modify: `skills/consult/SKILL.md` → routing for `--codex/--grok/--agy/--panel/--web`, "Web lane" section, argument-hint/description
- Test: `tests/test_consult_panel.py` (timeout default)

**Interfaces:**
- When `web_models` non-empty and `--timeout` not explicitly given, effective per-model timeout defaults to 600 (else the user's `--timeout`). Non-web default stays 180.

- [ ] **Step 1: Write failing test** (timeout default under web)

To know whether `--timeout` was explicitly passed, change its argparse default to `None`:
```python
def test_web_raises_default_timeout():
    captured = {}
    def runner(cmd, env, cwd, timeout):
        captured["t"] = timeout
        return panel.ModelResult(True, "VERDICT: GO", None)
    panel.main(["--grok", "--web", "Q"], runner=runner)
    assert captured["t"] == 600

def test_no_web_keeps_180_default():
    captured = {}
    def runner(cmd, env, cwd, timeout):
        captured["t"] = timeout
        return panel.ModelResult(True, "VERDICT: GO", None)
    panel.main(["--grok", "Q"], runner=runner)
    assert captured["t"] == 180
```

- [ ] **Step 2: Run — verify fail**

Run: `python3 -m pytest tests/test_consult_panel.py -k "web_raises_default_timeout or no_web_keeps_180" -q`
Expected: FAIL (timeout is 180 under web, or None).

- [ ] **Step 3: Implement**

In `_build_parser`, change: `p.add_argument("--timeout", type=int, default=None, ...)`.
In `main`, after resolving `web_models`:
```python
    timeout = args.timeout if args.timeout is not None else (600 if web_models else 180)
```
Pass `timeout=timeout` to `run_panel`.

Update `SKILL.md`:
- Add routing: the skill parses the user's `/bulldozer:consult` line → if any of `--codex/--grok/--agy/--panel/--web` present, run `consult_panel.py` with those flags (map `--web grok` to the safe `--web=grok` form); else bare = inline single-codex (unchanged D1).
- Add a "## Web lane" section: `--web` enables deep web research per model (web search + subagents, READ-side only), **reverses #189 no-egress** for the selected models (opt-in, like `--repo`); `--web --repo` can egress code but cannot mutate the repo; raw research persists to `.bulldozer/consult-<ts>/` (self-ignoring); default `--timeout` rises to 600 s.
- Update frontmatter `description` (add triggers: "research the web", "what do people do", "current best practices", "поищи в интернете", "как делают в комьюнити") and `argument-hint`: `[design question] — or: [--codex|--grok|--agy|--panel] [--web[=models]] [--repo PATH] [--verdict] <question>`.

- [ ] **Step 4: Run — verify pass** (full file)

Run: `python3 -m pytest tests/test_consult_panel.py -q`
Expected: PASS (all; the existing `--timeout default` behavior preserved for non-web).

- [ ] **Step 5: Commit**

```bash
git add skills/consult/scripts/consult_panel.py skills/consult/SKILL.md tests/test_consult_panel.py
git commit -m "feat(consult): --web timeout default 600s + SKILL.md routing & Web-lane docs"
```

---

### Task 8: End-to-end dogfood (live, all three) + final verification

**Files:** none (verification only)

- [ ] **Step 1: Full offline suite green**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (no regressions across all bulldozer suites; live cases self-skip).

- [ ] **Step 2: Live dogfood — each model alone + web + panel**

```bash
cd /0/ANTHROPICS_DEV/jaine-plugins/.claude/worktrees/consult-219
python3 skills/consult/scripts/consult_panel.py --codex --web "Current 2026 best practice for distributed API rate limiting? cite URLs"
python3 skills/consult/scripts/consult_panel.py --agy --web "Same question — cite URLs"
python3 skills/consult/scripts/consult_panel.py --panel --web=grok "Design Q + community practice; web only grok"
ls -la .bulldozer/consult-*/
```
Expected: codex/agy each return web-cited output; panel runs all three with web only on grok; a `.bulldozer/consult-<ts>/` bundle exists with `research.md` + `raw-grok.md` (and raw files only for web models). `.bulldozer/.gitignore` == `*`. If any step fails or the bundle is malformed → STOP, diagnose (systematic-debugging), do not declare done.

- [ ] **Step 3: Verify no-trace default unchanged**

```bash
python3 skills/consult/scripts/consult_panel.py --grok "non-web question"   # no --web
test -d .bulldozer/consult-* && echo "UNEXPECTED bundle (non-web should not persist)" || echo "OK: no bundle for non-web"
```
Expected: "OK: no bundle for non-web" (non-web path stays no-trace).

- [ ] **Step 4: codex_review on the committed diff**

Run `codex_review` (target=branch) over the branch diff vs `bulldozer/main`; address any P1/P2 findings (empirically verify each per `/receiving-code-review` discipline before accepting). Re-run the offline suite after fixes.

- [ ] **Step 5: Final commit (if review fixes)**

```bash
git add -A && git commit -m "fix(consult): address codex_review findings on --web/per-model"
```

---

## Self-Review (plan vs spec)

- **§3 grammar** → Tasks 1–4 (builders, hook, prepare, CLI). ✓
- **§4 per-model mechanics** → Tasks 1 (codex/grok), 2 (agy). ✓
- **§5 security (read-side only)** → Task 2 (agy denies run_command/writes always) + grok keeps `plan` (Task 1). ✓
- **§6 output (synthesis + pre-compress)** → Task 5. ✓
- **§7 bundle** → Task 6. ✓
- **§8 D1 (bare inline, explicit→engine)** → Task 7 SKILL.md routing. ✓
- **§9 timeout** → Task 7. ✓
- **§11 testing** → tests in Tasks 1–7 + dogfood Tasks 4/8. ✓
- **Backward-compat** (no-web byte-identical) → asserted by full-suite green at each task + Task 8 Step 3. ✓
- Placeholder scan: no TBD/TODO; all code literal. Type consistency: `web: bool` and `models`/`web_models` names consistent across Tasks 3–6. ✓
