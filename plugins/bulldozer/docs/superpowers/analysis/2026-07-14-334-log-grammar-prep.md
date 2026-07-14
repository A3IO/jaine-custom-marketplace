# #334 prep — the three unmigrated log writers: current shapes, migration plans, redaction gap

**Provenance:** produced by codex `gpt-5.6-sol` @ `effort=ultra` (auto-delegation: root thread
`019f5ead-4b98-77f0-92b0-aba7d7e64b8b` + 3 sub-agent threads), 2026-07-14, through the facade
multiplexer — this run doubled as the LIVE verification of the #349 interleave fix (same
ultra-delegation scenario that produced unreadable mush for session 0ec020b6 now yields the
clean per-item output below; `TURN_OK … effort=ultra … worker=0`, 3.9M tokens, 10.8 min).
Read-only static analysis; codex modified nothing and ran no tests. Line references are the
2026-07-14 state (`bulldozer/main` @ 2026.07.14.2) — **re-grep before acting on them; they
drift** (repo doctrine: no line numbers in docs — kept here verbatim as codex emitted them,
this is a point-in-time analysis snapshot, not living documentation).

**Consumer:** the future #334 session (task #11). Verify each claim against the code before
implementing — this is a plan, not a verified diff.

---

## SUBTASK 1: the MCP server's own audit writer

### (a) Current state

`mcp/codex_server.py:_drift_warn()` owns a separate writer at `mcp/codex_server.py:311-339`:

```text
{datetime.now().isoformat(timespec="seconds")} | {code} | {detail} [| worker={BULLDOZER_WORKER}]
```

`_now_iso()` produces a timezone-naive local timestamp with seconds precision and no offset
(`mcp/codex_server.py:311-315`). `_drift_warn()` adds neither `event=` nor `session=`, has no
rotation/locking, appends with the locale-default encoding, and silently swallows failures.
`worker=` is raw and optional (`:327-339`).

Exact current audit shapes, each with optional final `worker=`:

| Event | Producer | Current shape |
|---|---|---|
| `TURN_OK` | `_turn_ok_log`, `:374-396` | `{ts} \| TURN_OK \| model=… \| effort=… \| mcp=… \| retries=N \| duration_ms=… \| tokens=… [\| setup_ms=… \| cold_spawn=true\|false]` |
| `TURN_ERROR` | `_turn_error_log`, `:348-369` | `{ts} \| TURN_ERROR \| model=… \| effort=… \| mcp=… \| retries=N \| msg=…` |
| `INTERRUPT` | `_interrupt_log`, `:401-410` | `{ts} \| INTERRUPT \| interrupted_by=… \| thread_warm=true\|false \| model=… \| mcp=…` |
| `PARK` | `build_awaiting_payload`, `:2927-2949` | `{ts} \| PARK \| kind=… \| method=… \| token8=<last-8>` |
| `APPROVAL` | `_log_approval_event`, `:2179-2204` | `{ts} \| APPROVAL \| method=… \| decision=… \| wait_ms=N \| timed_out=true\|false [\| unattended=true \| rule=…] [\| ui=<non-cc>]` |
| `WARNING` | `_warning_log`, `:438-452` | `{ts} \| WARNING \| <message-with-no-key>` |
| `INFO_ERROR` | `_info_error_log`, `:428-434` | `{ts} \| INFO_ERROR \| query=… \| msg=…` |

Runtime entry points:

- `TURN_OK`: completed turn, `mcp/codex_server.py:4037-4043`.
- `TURN_ERROR`: failed completion/error notifications at `:4030-4055`; resume/park teardown at
  `:4193`, `:4313-4314`; park/write/ACK/EOF/timeout/catch-all exits at `:4530`, `:4573`,
  `:4589`, `:4614`, `:4628`, `:4636`, `:4642`.
- `INTERRUPT`: `_build_interrupted_result()`, `:3917-3927`.
- `PARK`: `build_awaiting_payload()` from `_drive_turn()`, `:4496-4501`.
- `APPROVAL`: attended path `:3053-3073`; unattended fast-path `:4481-4487`; model resume
  `:4535-4539`; teardown auto-decline `:4315-4322`.
- `WARNING`: normal notifications `:4083-4086`; pre-ACK forwarding `:4594-4597`.
- `INFO_ERROR`: `codex_info_v2()` spawn/read failures, `:3785-3809`.

The same writer also emits generic positional codes such as `VERSION_MISMATCH`,
`TRANSLATE_FAILED`, `UNKNOWN_*`, `OUT_OF_ENUM_LABEL`, `NOTIFICATION_FIXTURE_MISSING`, and
`INTERRUPT_DISABLED`. Those must migrate too.

### (b) Proposed migration

Import `lib/bulldozer_log.py` through the resolved plugin root, not ambient `sys.path`, for
example:

```python
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "lib"),
)
from bulldozer_log import append_line as _append_line
```

Use a guarded import; on missing/broken helper, warn lazily once and drop the line. Do not
retain a raw legacy fallback.

Change `_drift_warn` to accept structured fields while preserving old accumulator semantics:

```python
def _drift_warn(acc, code, detail=None, **fields):
    if acc is not None:
        acc.append({"code": code, "detail": detail})

    if detail is not None:
        fields = {"detail": detail, **fields}

    worker = os.environ.get("BULLDOZER_WORKER")
    if worker:
        fields["worker"] = worker

    _append_line(_codex_log_path(), code, **fields)
```

Then replace prebuilt pipe strings:

```python
_drift_warn(
    None,
    "TURN_ERROR",
    model=model,
    effort=effort,
    mcp=mcp,
    retries=retries,
    msg=message,
)

_drift_warn(None, "WARNING", msg=message)
```

This gives offset timestamps, required `event=`/`session=`, token normalization, value
sanitation, UTF-8, locked 5 MB rotation, and one best-effort warning from the canonical helper.

Use the helper's default session value: token-normalized `CLAUDE_CODE_SESSION_ID[:8]`,
otherwise `NA` (`lib/bulldozer_log.py:52-56`). Do **not** use Codex `thread_id`: it can survive
across Claude sessions, while `INFO_ERROR` and generic drift can occur before a thread exists.
If useful later, add `thread=` as a separate field.

Transition miner:

1. Split each physical line on `" | "`.
2. If segment 2 starts with `event=`, parse canonical form.
3. Otherwise treat segment 2 as the legacy positional event.
4. Assign legacy records a missing-session sentinel; do not invent a session or timezone.
5. Map the bare legacy `WARNING` tail to `msg`.
6. Preserve malformed/bare legacy tails as `detail`.
7. Accept both aware colon-offset timestamps and naive legacy timestamps.

Detection must be per line: old and new records will coexist in the active file and `.1`. Do
not dual-write, because that would double-count rates.

### (c) Risks/test impact

Tests currently pin the positional markers more strongly than the complete timestamp shape:

- Approval filters: `tests/test_codex_mcp_v2.py:3055-3120` search for `| APPROVAL |`.
- Error/warning suite: `tests/test_codex_mcp_v2.py:5357-5677`.
- `_drift_warn` primitives: `tests/test_codex_mcp_v2.py:6154-6178`.
- Observability suite: `tests/test_codex_mcp_v2.py:7804-7916`.
- Facade compatibility: `tests/test_codex_facade.py:1009-1029`, especially the exact legacy
  suffix at `:1029`.
- README contract: `tests/test_skill_prompts.py:830-844` explicitly requires Codex to be
  documented as noncanonical.
- Forgery test `tests/test_codex_mcp_v2.py:7906-7916` should inject the canonical
  `event=TURN_ERROR | session=…` spelling too.

Add one exact test covering colon-offset timestamp, `event=`, normalized `session=`, field
order, and optional final `worker=`. Retain accumulator and never-raises checks.

Observable changes include `.lock`/`.1` files, helper-level 500-character truncation, sanitized
`worker=`, and one stderr warning where the old writer was completely silent.

## SUBTASK 2: the consult panel's completion line

### (a) Current state

`skills/consult/scripts/consult_panel.py:_log_completion()` directly builds and appends its
line at `:1065-1119`:

```text
{offset-ts}
| session={S}
| round=1
| verdict={V}
| tokens=NA
| time={X.X}s
| models={m,...}
| web={m,...}
[| survivors={N/M} | failures={...} | legtimes={...}]
[| agy_model={...}]
[| codex_effort=medium]
| project={P}
```

All production call sites currently pass `legs` (`:1165`, `:1182`, `:1192`, `:1247`, `:1265`,
`:1269`), so current production records include `survivors`, `failures`, and `legtimes`.

Unlike the MCP writer, its timestamp already has a colon offset (`:1087`) and its session
normalization matches the canonical helper (`:1081-1084`). The missing piece is `event=`. It
also bypasses universal value sanitation, explicit UTF-8, rotation, and locking (`:1105-1107`).

The start marker is already canonical: `hooks/log_skill_invoke.py:22-24,38-39,71-80` calls the
shared helper, so its actual shape is:

```text
{offset-ts} | event=consult-invoke | session={S} | project={P}
```

The displayed example in `skills/consult/SKILL.md:275-279` omits the helper-added `session=`.

### (b) Proposed migration

Use the explicit event token `consult-complete`:

```python
fields = {
    "round": 1,
    "verdict": ...,
    "tokens": "NA",
    "time": f"{elapsed:.1f}s",
    "models": ",".join(selected),
    "web": web,
    # optional outcome/model fields, then project
}
_append_line(CONSULT_LOG, "consult-complete", **fields)
```

Remove manual timestamp/session construction and direct file opening. Let the helper derive
the session and retain the current field order.

A bare import is unreliable. The documented invocation is `python3 "$PANEL" ...` from a
consumer project (`skills/consult/SKILL.md:219-249`), so `sys.path[0]` is
`skills/consult/scripts`, not the plugin's `lib`. Add:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
```

before a guarded import. `.resolve()` preserves cached/symlinked installation support.

If import fails, warn once only when `_log_completion()` is attempted, then drop the line. Do
not warn at module import, because `--help` deliberately produces no completion telemetry
(`consult_panel.py:1241-1248`). Do not fall back to raw append.

The transition miner should recognize:

- canonical `event=consult-complete`;
- legacy panel completion as no `event=` plus `session=` and plural `models=`;
- legacy inline completion by singular `model=`;
- older panel tails where `survivors`/`failures`/`legtimes` are absent.

### (c) Risks/test impact

Update or strengthen:

- `tests/test_consult_panel.py:1097-1157`: assert exact canonical prefix/order and
  `event=consult-complete`.
- Per-leg/error records: `tests/test_consult_panel.py:2141-2173,2218-2230`.
- Warning behavior: `tests/test_consult_panel.py:2176-2215` currently depends on panel-local
  `_LOG_WARNED` and the old warning text. The shared helper owns `_WARNED`.
- Session test: `tests/test_consult_panel.py:2184-2191` should remain valid.
- Documentation exception test: `tests/test_skill_prompts.py:830-844`.
- Add an arbitrary-cwd subprocess test and a broken-helper "warn once, drop line" test
  analogous to `tests/test_bulldozer_log.py:335-377`.

The helper will now sanitize/truncate fields such as `project` and create `.lock`/`.1`.

**Important residual:** `skills/consult/SKILL.md:375-383,406-410` still contains two direct
inline `echo >>` completion writers with singular `model=` and no `event=`. Migrating only
`_log_completion()` does not remove that third legacy shape; it needs its own CLI-helper change
if #334 is meant to close every producer.

## SUBTASK 3: the redaction gap

### (a) Current state

D2 in `cdp.py` is mostly implemented at call sites rather than inside the generic writer:

- `_redact_url()` at `skills/look/scripts/cdp.py:161-177` strips userinfo/query/fragment,
  preserves `?<redacted>`, and hashes payload/unknown schemes.
- URL uses: screenshot `:784`; navigate `:988,1002,1010,1016,1021`; open `:1040`.
- JS becomes `expr_len`/`expr_sha` at `:873` and `:900`.
- `assert --js` uses a hashed `what_log` at `:1525-1527`.
- `log()` itself only applies `_redact_target()` to `TARGET` at `:114-126,151-158`.

`lib/bulldozer_log.py:_value()` is only a structural sanitizer (`:45-49`): it replaces
CR/LF/pipe and truncates. It does not redact credentials.

The Codex channel's corresponding sanitizer is `_log_san()` (`mcp/codex_server.py:342-345`),
which also leaves URL userinfo, query, and fragment intact.

Exact payload sinks:

- `TURN_ERROR`: `_turn_error_log()` sends `emsg` through `_log_san(...)[:500]` at `:348-369`.
  - Sources include wire `turn.error` at `:4018-4035`;
  - `error.message` extracted by `_classify_error_notification()` at `:5014-5021` and logged
    at `:4053`/`:4589`;
  - start-response error dictionaries at `:4571-4576`;
  - resume/catch-all exception text at `:4192-4196,4641-4644`.
- `WARNING`: `_warning_log()` selects top-level `message`, nested `warning.message`, or
  `json.dumps(params)[:300]`, then only `_log_san()` at `:438-452`. Normal and pre-ACK paths
  enter at `:4083-4086,4594-4597`.
- `INFO_ERROR`: exception text enters `_log_san()` at `:428-433`.
- `APPROVAL`: a duplicate local `_san()` handles method/decision/rule/UI structurally at
  `:2190-2203`.
- Generic drift: `_drift_warn()` writes `detail` directly at `:318-337`. Direct
  external/exception inputs include user-agent mismatch `:1157-1158`, decision variants
  `:1465/:1483`, translation errors `:1923/:1982`, labels `:3251/:3285/:3322/:3383`, fixture
  errors `:3465`, unknown methods `:3491`, and unknown notifications `:4096`.

Thus a message such as:

```text
failed https://user:pass@example.test/api?token=SECRET#fragment
```

remains intact except for line-delimiter replacement.

### (b) Proposed migration

**Preferred design:** add opt-in public helpers to `lib/bulldozer_log.py`, but do not silently
put redaction inside `_value()`:

- `redact_url(url)`: port the already-tested `cdp.py` semantics.
- `redact_urls_in_text(text)`: locate scheme-prefixed URLs embedded in prose/JSON and redact
  each while preserving surrounding text.

Making `_value()` redact implicitly would unexpectedly alter every channel and fields that
intentionally carry public URLs. Redaction is producer policy; structural grammar enforcement
belongs to `append_line()`.

Apply redaction before structural sanitation and before truncation. For the unknown-warning
fallback, either:

```python
msg = redact_urls_in_text(json.dumps(p))
msg = msg[:300]
```

or let the canonical writer own the universal 500-character bound. Do not truncate raw
credential-bearing text first.

**MINIMAL-DIFF OPTION:** keep `cdp.py` unchanged, port a private `redact_urls_in_text()` into
`mcp/codex_server.py`, and apply it at the new structured `_drift_warn` boundary:

```python
safe_fields = {
    key: redact_urls_in_text(value)
    for key, value in fields.items()
}
_append_line(path, code, **safe_fields)
```

Keep the original `detail` in `acc`; redact only the durable copy. This one choke point covers
`TURN_ERROR`, `WARNING`, `INFO_ERROR`, approval fields, generic drift, and future callers.

If redaction remains call-site based, the minimum required routes are:

1. `_turn_error_log()`'s `msg` at `:369`;
2. all three `_warning_log()` message-selection paths before `:452`;
3. `_info_error_log()`'s `msg` at `:433`;
4. direct exception/wire `_drift_warn()` details listed above.

On redactor/import failure, drop the line or emit a fixed placeholder — never retry with the
raw payload.

**One channel-wide caveat:** `mcp/codex_facade.py:579-616` independently writes canonical
`FACADE_*` records to the same `bulldozer-codex.log`, including `repr(e)` fields. A
server-local `_drift_warn` fix does not cover those. If the claim becomes "new Codex-channel
records redact URLs," facade values must use the same opt-in helper.

### (c) Risks/test impact

Preserve existing D2 semantics pinned by `tests/test_logging_pr6.py:34-136,139-161`,
especially:

- userinfo/query/fragment removal;
- payload-scheme hashing;
- marker survival under URL truncation;
- surrogate-safe SHA generation;
- no verbatim JS at log call sites.

Add Codex tests beside `tests/test_codex_mcp_v2.py:5364-5429` for:

- a `TURN_ERROR` URL embedded in prose;
- top-level, nested, and unknown-shape `WARNING`;
- `INFO_ERROR` exception text;
- userinfo, query, fragment, multiple URLs, and long URLs;
- origin/path plus `?<redacted>` retained, secrets absent.

Add a `_drift_warn` test near `tests/test_codex_mcp_v2.py:6157-6178` proving that the
persisted copy is redacted while the accumulator retains its original detail. Keep the
existing one-line injection tests at `:5400-5409` and `:7906-7916`.

`README.md:129-133` and `tests/test_skill_prompts.py:846-851` explicitly assert redaction is
look-only and that Codex can leak URLs; update them without overclaiming "no secrets." Paths
remain visible, bare tokens are not detected, and historical records remain unredacted.

## UNIFIED TABLE

`ts₀` = current naive MCP timestamp; `ts+` = ISO timestamp with colon offset; `[worker]` is
optional and last.

| Writer | Event | Current shape | Target shape | Migration risk |
|---|---|---|---|---|
| `codex_server._turn_ok_log` | `TURN_OK` | `ts₀ \| TURN_OK \| model=… \| effort=… \| mcp=… \| retries=… \| duration_ms=… \| tokens=… [timing] [worker]` | `ts+ \| event=TURN_OK \| session=S \| model=… \| …` | Rate parsers expecting positional event; optional timing order |
| `codex_server._turn_error_log` | `TURN_ERROR` | `ts₀ \| TURN_ERROR \| model=… \| effort=… \| mcp=… \| retries=… \| msg=… [worker]` | `ts+ \| event=TURN_ERROR \| session=S \| … \| msg=<URL-redacted>` | Highest privacy exposure; many terminal paths |
| `codex_server._interrupt_log` | `INTERRUPT` | `ts₀ \| INTERRUPT \| interrupted_by=… \| thread_warm=… \| model=… \| mcp=… [worker]` | `ts+ \| event=INTERRUPT \| session=S \| …` | Must remain distinct from failure rates |
| `codex_server.build_awaiting_payload` | `PARK` | `ts₀ \| PARK \| kind=… \| method=… \| token8=… [worker]` | `ts+ \| event=PARK \| session=S \| kind=… \| method=… \| token8=…` | Never expose full park token |
| `codex_server._log_approval_event` | `APPROVAL` | `ts₀ \| APPROVAL \| method=… \| decision=… \| wait_ms=… \| timed_out=… [unattended/rule] [ui] [worker]` | `ts+ \| event=APPROVAL \| session=S \| same structured fields` | Preserve attended/unattended and `ui` variants |
| `codex_server._warning_log` | `WARNING` | `ts₀ \| WARNING \| <bare message> [worker]` | `ts+ \| event=WARNING \| session=S \| msg=<URL-redacted>` | Field-name change plus top-level/nested/fallback inputs |
| `codex_server._info_error_log` | `INFO_ERROR` | `ts₀ \| INFO_ERROR \| query=… \| msg=… [worker]` | `ts+ \| event=INFO_ERROR \| session=S \| query=… \| msg=<URL-redacted>` | Can occur with no Codex thread |
| `codex_server._drift_warn` | other codes | `ts₀ \| CODE \| <bare or prebuilt detail> [worker]` | `ts+ \| event=CODE \| session=S \| detail=<redacted>` | Preserve raw user-facing accumulator while sanitizing durable copy |
| `codex_facade.Facade._log` | `FACADE_*` | Already `ts+ \| event=FACADE_* \| session=S \| k=v…` | Same grammar; opt-in redaction for `err/detail` | Bypasses server `_drift_warn` despite sharing the same file |
| `hooks/log_skill_invoke.py` | `consult-invoke` | Already `ts+ \| event=consult-invoke \| session=S \| project=P` | Unchanged | Documentation currently omits actual `session=` |
| `consult_panel._log_completion` | proposed `consult-complete` | `ts+ \| session=S \| round=1 \| verdict=… \| tokens=NA \| time=… \| models=… \| web=… \| …` | `ts+ \| event=consult-complete \| session=S \| same fields` | Import path, warning ownership, value truncation |
| `skills/consult/SKILL.md` inline echo | proposed `consult-complete` | `ts+ \| session=S \| round=N \| verdict=… \| tokens=… \| time=… \| model=… \| project=…` | `ts+ \| event=consult-complete \| session=S \| … \| model=…` via CLI helper | Separate shell writer; otherwise remains the third legacy shape |
| `cdp.log` URL-bearing calls | `navigate`, `open`, `screenshot` | `ts+ \| event=E \| session=S \| port=P [target] \| … \| url=<D2-redacted>` | Same; optionally use shared `redact_url()` | Shared-helper refactor must preserve all D2 edge cases |
| `cdp.log` JS-bearing calls | `js`, `assert` | `ts+ \| event=E \| session=S \| port=P \| expr_len/expr_sha` or hashed `what=` | Unchanged | Never reintroduce verbatim JS |
