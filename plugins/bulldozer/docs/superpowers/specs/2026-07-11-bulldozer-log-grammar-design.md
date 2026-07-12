# Bulldozer log grammar + shared helper (`lib/bulldozer_log.py`)

**Issue:** #322 (section E). **Scope:** the canonical line grammar, one shared writer, and the first migration (cdp.py). Everything else in #322 builds on this.

## Problem

8+ independent writers append to 4 stable logs with no shared conventions: 3+ timestamp formats (`+07:00` vs `+0700` vs no-tz), no universal `event=`/`session=`, unsanitized values (~696 unparseable multiline JS lines in look.log, 4.4%), silent write-failure swallowing, and zero rotation (18MB incident #318; look.log at 1.7MB and growing). Every point-fix without a shared helper will drift again.

## Canonical line grammar

```
{ts} | event={event} | session={sid} | k1=v1 | k2=v2 ...
```

- **ts** — `datetime.now().astimezone().isoformat(timespec="seconds")` → `2026-07-11T05:41:03+07:00`. One producer; never `time.strftime` (`+0700`), never microseconds.
- **event** — REQUIRED, first field after ts. Kebab-or-snake token, e.g. `screenshot`, `round`, `consult-invoke`, `TURN_OK` (a planned PR2 line — does not exist yet; MCP will keep its UPPER codes as event values when it migrates).
- **session** — REQUIRED, `CLAUDE_CODE_SESSION_ID[:8]` else `NA`. Always present so lines join across files. The resolved value — explicit arg AND env-derived alike — is first coerced via `str()` (any type accepted, upholding never-raises), then passes token normalization (same regex/rewrite as event/keys, then `[:8]`) BEFORE serialization, so an adversarial `session="x | event=forged"` or `session=object()` cannot break the grammar or raise (R2-F1).
- **k=v pairs** — writer-chosen, order-stable. Values sanitized (below).
- **Token validation (event + keys):** the input is first coerced via `str()` (any type accepted — same never-raises discipline as session, R4-F1), then must match `^[A-Za-z0-9_-]{1,64}$`. A non-conforming event/key is NOT rejected (logging never fails the tool) — it is **normalized**: every disallowed char → `_`, then truncated to 64; an empty result → `invalid`. `event` and `session` are reserved **field-key** names — the rewrite never applies to the event VALUE itself (`append_line(log, "event")` logs `event=event`, identity preserved; codex review #323). **Reserved-key rewriting (`<key>` → `<key>_`) is a CLI-shim rule only** — the Python API structurally cannot receive them: `**{"event": ...}` fails Python argument binding at the CALL SITE, before the helper runs (a caller bug, outside the helper's never-raises contract — documented, R3-F1). **Collision resolution runs AFTER normalization, uniformly for both surfaces where collisions can occur:** the pairs are serialized in input order and a key equal to an already-serialized key **overwrites it (last wins)** — this covers CLI duplicate spellings, distinct inputs normalizing to one key (`"bad key"` + `"bad_key"`), and a reserved rewrite landing on a literal `event_` (R2-F2). Deterministic: the surviving value is always the last occurrence in input order.
- **Value sanitization** (extends `_san` from codex_server.py): value → `str(v)`, `\n`/`\r` → space, `|` → `/`, then hard-truncate: a value longer than 500 chars becomes its first 499 chars + `…` (result is exactly 500 — the suffix counts toward the ceiling; R4-F3). Guarantees: one line per record, ` | ` splits fields exactly, greppable — enforced for event, keys, and values alike (R1-F1).

## Helper API (stdlib-only, py3.9+, single file `lib/bulldozer_log.py`)

```python
append_line(log_path, event, session=None, **kv) -> bool
```

- Resolves `session` from `CLAUDE_CODE_SESSION_ID` when None; builds the line per grammar; appends.
- **Best-effort + one warning:** never raises; on failure prints ONE `warning: could not write <basename>: {e}` to stderr **once per process** (module-level flag) — the `_write_web_bundle` pattern, not `except: pass`.
- **Rotation before append:** if `log_path` size > 5 MB → `os.replace(log, log + ".1")` (one level, overwrite old `.1`). Rotation + append run as ONE critical section under `fcntl.flock` on a `<log>.lock` sidecar — without it a second concurrent writer could replace `.1` with a freshly-rotated tiny log and discard the whole history (codex review #323; each cdp.py command is its own process, so the race is real). Non-POSIX platforms fall back to unserialized best-effort.
- **CLI shim for .sh writers:** `python3 lib/bulldozer_log.py <log_path> <event> [k=v ...]` → same code path, exit 0 always (fail-open for hooks/wrappers). Field parsing is `arg.split("=", 1)`: an arg without `=` becomes a key with empty value (`foo` → `foo=`); an empty key (`=value`) normalizes to `invalid` per the token rules above — deterministic, never rejected (R4-F2).

### Import pattern for scattered scripts

The plugin ships as one directory tree (source and cache alike), so consumers locate the helper relative to `__file__`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))  # skills/<x>/scripts/ → root
from bulldozer_log import append_line
```

Fallback: if the import fails (unexpected layout), the writer prints ONE stderr warning and **drops the line — it never appends outside the helper** (R1-F2: a plain-append fallback would reintroduce exactly the unsanitized/multiline records this design eliminates; fail-open for the tool, fail-closed for the grammar). The layout is deterministic inside the shipped plugin tree (source and cache alike), so this path is expected dead code — losing a line in a broken layout is preferable to writing an unparseable one.

## First migration (this PR): cdp.py `log()`

- `log(event, **kw)` delegates to `append_line(LOG_FILE, event, port=CDP_PORT, **kw)`; adds `target=` when `--target` is active.
- Existing call sites unchanged (same signature). Output gains `session=`, `port=`, colon-offset ts, sanitized values — miners keying on `event=` keep working; positional parsers were already broken by the two coexisting shapes.
- NOT in this PR: ok= vocabulary and error-path coverage (PR1b), other writers (PR2-PR5).

## Non-goals

- No JSON lines (grep-first house style), no external deps, no logrotate integration, no retention policy beyond one `.1` level, no async/locking (single-machine, best-effort).

## Tests (TDD)

`tests/test_bulldozer_log.py` — runtime write-path (tmp file), not AST: grammar exactness (ts format, field order, session fallback NA), sanitization (newline/pipe/truncate), **adversarial token cases** (event with embedded `\n`/`|`/spaces, CLI key `"bad | field"`, empty event, reserved-key collision, duplicate CLI keys → last wins, post-normalization collisions `"bad key"`+`"bad_key"` → last wins, adversarial session values — explicit `"x | event=forged"`, non-string `session=object()`/`session=42`, and env-derived with newline/pipe/overlength; CLI reserved-key rewrite `event=forged` positional kv → `event_=forged`; a Python-API `**{"event": ...}` call asserted to raise at binding — caller-side, documented), **forced-import-failure path** (line dropped + one warning, nothing appended), rotation trigger + `.1` overwrite, once-per-process stderr warning on unwritable dir, CLI shim exit-0 + line written. Plus cdp.py structural: `log()` delegates, `port=` present in a written line (behavioral via a tmp log path; cdp.py's `LOG_FILE` is currently hardcoded — this PR adds a `BULLDOZER_LOOK_LOG` env override, matching `BULLDOZER_CONSULT_LOG`/`BULLDOZER_CODEX_LOG` convention, which the tests then use for isolation).
