# #334 Log-Grammar Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the three legacy log producers (codex `_drift_warn` writers, consult `_log_completion`, consult SKILL.md inline echoes) onto the canonical `lib/bulldozer_log.py` grammar, and close the redaction gap (codex channel + the facade's payload fields `err`/`reason`/`detail`/`msg`), with a miner-facing contract test.

**Architecture:** One shared writer (`lib/bulldozer_log.py`) gains two opt-in redaction helpers; `codex_server.py`'s `_drift_warn` becomes a thin adapter over `append_line` (choke-point redaction, acc contract preserved); consult's Python and bash writers converge on the same helper (Python import / CLI shim). No dual-write anywhere — legacy and canonical lines coexist per-line in history; the README documents the transition rules.

**Tech Stack:** Python stdlib only in `lib/` (py3.9+ per its header); `mcp/` is py3.11+. pytest via `uv run pytest` (xdist default).

## Global Constraints

- **In-process logging only** in the MCP server — the helper is imported, never shelled out per line (issue ask #1).
- **No dual-write, ever** — a record lands in exactly ONE shape; dual-writing would double-count miner rates (prep, hard rule).
- **No raw-payload fallback** — if the helper or redactor is unavailable, warn once + drop the line; never keep a second legacy writer alive (prep, hard rule).
- **acc contract byte-identical** — `_drift_warn`'s accumulator feeds the user-facing `_drift` result field; it must keep receiving `{"code", "detail"}` with the ORIGINAL (un-redacted) detail.
- **cdp.py untouched** — its D2 redaction semantics are pinned by `tests/test_logging_pr6.py`; this plan ports semantics, it does not refactor the look channel.
- **Event-token case (decision D1):** codex channel KEEPS `TURN_OK`/`APPROVAL`/… UPPERCASE — deviation from issue ask #1's literal `turn-ok` kebab, deliberate: (a) the transition miner greps ONE token across legacy (`| TURN_OK |`) and canonical (`event=TURN_OK`) lines; (b) `event=FACADE_*` UPPERCASE precedent already lives in the same file. Consult channel uses kebab `consult-complete` (consistent with its `consult-invoke`). Record the deviation in the issue-closing comment.
- **Field value parity:** boolean-ish values stay literal `true`/`false` strings; `tokens=NA` sentinel stays; `worker=` stays LAST on the line.
- Line references in this plan are 2026-07-19 state — re-grep before editing (repo doctrine).

## Design decisions (settled 2026-07-19, sequentialthinking pass)

| # | Decision |
|---|----------|
| D1 | Codex events stay UPPERCASE; consult completion = `consult-complete` (see Global Constraints). |
| D2 | `_drift_warn(acc, code, detail=None, **fields)` hybrid — generic codes keep `detail=`, named writers decompose into kwargs. All `acc≠None` call sites are plain-detail codes (verified), so the accumulator never sees structured fields. |
| D3 | Redaction helpers are PUBLIC opt-in functions in `lib/bulldozer_log.py` (`redact_url`, `redact_urls_in_text`) — NOT wired into `_value()` (would corrupt channels that legitimately log public URLs). Applied at two choke points: codex `_drift_warn` and facade `_log_writer`. |
| D4 | consult SKILL.md echoes migrate to the CLI shim with ONE `$BLOG` resolver added early in the `## Quick Reference — Full Invocation Template` section's bash template (the #221 pattern already used by its step-5 parser block); shim derives `session=` itself from env. |
| D5 | Miner-facing contract test (`tests/test_log_grammar_contract.py`) exercises the REAL producers into a temp log and regex-validates every emitted line — the gap that let #322 close as "53/53". |
| D6 | One PR, commits per task below; codex plan-review before implementation, `codex_review` on the diff after; every fix mutation-checked. |

## File Structure

- Modify: `lib/bulldozer_log.py` — add `_URL_RE`, `redact_url()`, `redact_urls_in_text()`
- Modify: `mcp/codex_server.py` — guarded helper import; `_drift_warn` rewrite; 7 named writers decomposed; `_log_san` removed if unused after
- Modify: `mcp/codex_facade.py` — `_log_writer` redacts `_REDACT_KEYS` payload fields only (identifiers preserved)
- Modify: `skills/consult/scripts/consult_panel.py` — guarded import; `_log_completion` via helper
- Modify: `skills/consult/SKILL.md` — `$BLOG` resolver + 2 echo → CLI shim; log-format section update
- Modify: `README.md` — `## Log Format` inversion (all producers canonical; honest redaction scope)
- Modify: `CLAUDE.md` — `## Architecture: logging` NOT-migrated paragraph removal + redaction scope update
- Create: `tests/test_log_grammar_contract.py`
- Modify: `tests/test_bulldozer_log.py`, `tests/test_codex_mcp_v2.py`, `tests/test_consult_panel.py`, `tests/test_skill_prompts.py`, `tests/test_codex_facade.py` (pinned shapes)

---

### Task L: redaction helpers in `lib/bulldozer_log.py`

**Files:**
- Modify: `lib/bulldozer_log.py`
- Test: `tests/test_bulldozer_log.py`

**Interfaces:**
- Produces: `redact_url(url: str) -> str`; `redact_urls_in_text(text) -> str` — later tasks import both from `bulldozer_log`.

- [ ] **Step L1: failing tests**

```python
# tests/test_bulldozer_log.py (append)
from bulldozer_log import redact_url, redact_urls_in_text


class TestRedactUrl:
    def test_query_dropped_with_marker(self):
        assert redact_url("https://x.test/api?token=SECRET") == "https://x.test/api?<redacted>"

    def test_userinfo_stripped(self):
        out = redact_url("https://user:pass@x.test/a")
        assert "user:pass" not in out and out.startswith("https://x.test/a")

    def test_fragment_dropped(self):
        assert redact_url("https://x.test/a#frag") == "https://x.test/a?<redacted>"

    def test_plain_url_survives(self):
        assert redact_url("https://x.test/path/deep") == "https://x.test/path/deep"

    def test_marker_survives_long_url(self):
        out = redact_url("https://x.test/" + "p" * 300 + "?t=s")
        assert out.endswith("?<redacted>") and len(out) <= 120


class TestRedactUrlPayloadSchemes:
    """R1-F1: cdp parity — non-location schemes carry PAYLOAD in the path;
    they must hash-redact, never survive as 'path'."""
    def test_data_uri_is_hash_redacted(self):
        out = redact_url("data:text/html;base64,U0VDUkVU")
        assert "U0VDUkVU" not in out and out.startswith("data:<redacted:len=")

    def test_javascript_uri_is_hash_redacted(self):
        out = redact_url("javascript:fetch('/steal?t=SECRET')")
        assert "SECRET" not in out and out.startswith("javascript:<redacted:")

    def test_unknown_scheme_with_slashes_is_hash_redacted(self):
        out = redact_url("myapp://host/p?t=SECRET")
        assert "SECRET" not in out and out.startswith("myapp:<redacted:")

    def test_location_schemes_keep_origin_path(self):
        for u in ("https://x.test/a", "file:///tmp/x", "wss://x.test/s"):
            assert redact_url(u) == u

    def test_blob_wrapper_scheme_whole_token_redacted(self):
        # R1-F1 r2: the WHOLE blob: token must redact — the generic arm must not
        # grab the inner https:// and keep SECRET_BLOB_ID as 'path'
        out = redact_urls_in_text("leak blob:https://x.test/SECRET_BLOB_ID here")
        assert "SECRET_BLOB_ID" not in out and "here" in out

    def test_view_source_wrapper_scheme_redacted(self):
        out = redact_urls_in_text("via view-source:https://x.test/p?t=SECRET end")
        assert "SECRET" not in out and "end" in out

    def test_scheme_matching_is_case_insensitive(self):
        out = redact_urls_in_text("sent DATA:text/plain;base64,U0VDUkVU out")
        assert "U0VDUkVU" not in out and "out" in out

    def test_filesystem_wrapper_scheme_redacted(self):
        out = redact_urls_in_text("got filesystem:https://x.test/persistent/SECRET end")
        assert "SECRET" not in out and "end" in out

    def test_percent_encoded_data_uri_captured_whole(self):
        # RFC-valid data: URIs %-encode whitespace — captured to the token end
        out = redact_urls_in_text("saw data:text/plain,a%20SECRET%20b tail")
        assert "SECRET" not in out and "tail" in out


class TestRedactUrlsInText:
    def test_url_in_prose(self):
        out = redact_urls_in_text("failed https://u:p@x.test/api?t=SECRET#f then died")
        assert "SECRET" not in out and "u:p" not in out
        assert "then died" in out and "?<redacted>" in out

    def test_data_uri_in_prose_redacted(self):
        out = redact_urls_in_text("codex sent data:text/plain;base64,U0VDUkVU to renderer")
        assert "U0VDUkVU" not in out and "to renderer" in out

    def test_javascript_uri_in_prose_redacted(self):
        out = redact_urls_in_text("blocked javascript:alert(document.cookie) inline")
        assert "document.cookie" not in out and "inline" in out

    def test_bare_word_colon_not_matched(self):
        # false-positive guard: sha256:, error:, time= values must survive intact
        s = "sha256:abcdef error: boom time=1.5s"
        assert redact_urls_in_text(s) == s

    def test_multiple_urls(self):
        out = redact_urls_in_text("a https://a.test/x?q=1 b http://b.test/y?q=2 c")
        assert out.count("?<redacted>") == 2 and "q=1" not in out and "q=2" not in out

    def test_pipe_delimiter_not_consumed(self):
        out = redact_urls_in_text("u=https://x.test/p?q=1 | k=2")
        assert out.endswith("| k=2") and "q=1" not in out

    def test_text_without_urls_unchanged(self):
        assert redact_urls_in_text("plain error text") == "plain error text"

    def test_non_string_coerced(self):
        assert redact_urls_in_text(None) == "None"

    def test_failure_returns_placeholder_never_raw(self, monkeypatch):
        import bulldozer_log as bl
        class Boom:
            def sub(self, *a, **k):
                raise RuntimeError("boom")
        monkeypatch.setattr(bl, "_URL_RE", Boom())
        out = bl.redact_urls_in_text("secret https://x.test/?t=s")
        assert "t=s" not in out and out.startswith("<redaction-failed")
```

- [ ] **Step L2: run, verify FAIL** — `uv run pytest tests/test_bulldozer_log.py -n0 -k Redact -v` → ImportError/AttributeError.

- [ ] **Step L3: implementation** (append to `lib/bulldozer_log.py`, after `_value`)

```python
import hashlib  # top of file, stdlib
from urllib.parse import urlsplit, urlunsplit  # top of file, stdlib

# Full-parity port of the look channel's cdp.py redaction (#334, R1-F1):
# location schemes keep origin+path; everything else is PAYLOAD → hash-redact.
_LOCATION_SCHEMES = frozenset((
    "", "http", "https", "file", "ws", "wss", "about", "chrome",
    "chrome-extension", "devtools",
))
# Embedded-URL matcher: generic scheme:// forms + the payload/wrapper schemes
# that carry secrets WITHOUT '//' or WRAP an inner URL (data:, javascript:,
# blob:, view-source:, filesystem:). The wrapper schemes MUST be whole-token
# alternatives: without them the generic arm matches the INNER https:// of
# `blob:https://x/SECRET` and preserves the payload as 'path' (R1-F1 r2).
# IGNORECASE — schemes are case-insensitive per RFC 3986 (`DATA:` must not
# bypass). Deliberately NOT any `word:` — that would mangle sha256:…, error:…,
# k=v prose (R1-F1 test).
#
# DESIGN BOUNDARY (R1-F1 r3, user-ratified): tokens are matched to the next
# whitespace/`|`. RFC 3986 forbids literal whitespace in URIs (%-encoding is
# mandatory), so every VALID URI — including data: — is captured whole; a
# MALFORMED URI with embedded literal spaces is out of scope, as are non-URL
# secrets (bare tokens, emails). Chasing malformed inputs has no stopping
# point; the limitation is documented in README and pinned by test.
_URL_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|data:|javascript:|blob:|view-source:|filesystem:)[^\s|]+",
    re.IGNORECASE)


def _sha12(text):
    return hashlib.sha256(
        str(text).encode("utf-8", "surrogatepass")).hexdigest()[:12]


def redact_url(url):
    """Port of cdp.py:_redact_url (#334): scheme://host:port/path survives
    (minable); userinfo, query and fragment are dropped — one `?<redacted>`
    marker records that something was there. NON-location schemes (data:,
    javascript:, unknown app schemes) carry payload in the 'path' → replaced
    with `scheme:<redacted:len=N,sha=H>`. Opt-in producer policy; deliberately
    NOT called from _value()."""
    s = str(url)
    try:
        parts = urlsplit(s)
    except ValueError:
        return "unparseable:len={}".format(len(s))
    if parts.scheme.lower() not in _LOCATION_SCHEMES:
        return "{}:<redacted:len={},sha={}>".format(parts.scheme, len(s), _sha12(s))
    netloc = parts.netloc.rpartition("@")[2]  # strip user:pass@
    base = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    if parts.query or parts.fragment:
        # marker appended AFTER the cap so truncation can't eat it
        return base[:109] + "?<redacted>"
    return base[:120]


def redact_urls_in_text(text):
    """Replace every embedded URL (scheme:// forms + data:/javascript:) with
    redact_url() of it. `|` is excluded from the match so field delimiters
    survive. Limitations (documented, not detected): other scheme:opaque forms
    (mailto:) and bare tokens pass through. On ANY failure returns a
    placeholder, NEVER the raw text — a redaction failure must not leak the
    payload (#334)."""
    s = str(text)
    try:
        return _URL_RE.sub(lambda m: redact_url(m.group(0)), s)
    except Exception:
        return "<redaction-failed:len={}>".format(len(s))
```

- [ ] **Step L4: run, verify PASS** — same command; then full `uv run pytest tests/test_bulldozer_log.py -v` (no regressions).
- [ ] **Step L5: commit** — `git add lib/bulldozer_log.py tests/test_bulldozer_log.py && git commit -m "feat(lib): opt-in URL redaction helpers in bulldozer_log (#334)"`

---

### Task S: `codex_server.py` — `_drift_warn` onto the helper

**Files:**
- Modify: `mcp/codex_server.py`
- Test: `tests/test_codex_mcp_v2.py`

**Interfaces:**
- Consumes: `bulldozer_log.append_line`, `bulldozer_log.redact_urls_in_text` (Task L).
- Produces: `_drift_warn(acc, code, detail=None, **fields)` — every audit line becomes `{ts+offset} | event=CODE | session=S | k=v… [| worker=N]`.

- [ ] **Step S1: failing tests** — add to `tests/test_codex_mcp_v2.py` (class near the existing `_drift_warn` primitives; use the existing `BULLDOZER_CODEX_LOG` monkeypatch pattern). **R2-F1:** the file has NO module-level `re` import and NO `srv` alias — its convention is a function-local `import codex_server as cs` (used throughout the file — count live with `grep -c 'cs\.' tests/test_codex_mcp_v2.py`; repo doctrine forbids hardcoding the number, and Task S itself adds tests to this file); the snippet below follows it, with `re` imported inside the helper:

```python
class TestCanonicalAuditLines:
    CANON_LINE_SRC = (
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"
        r" \| event=[A-Za-z0-9_-]+ \| session=[A-Za-z0-9_-]{1,8}"
        r"( \| [A-Za-z0-9_-]+=[^|]*)*$")

    def _last_line(self, tmp_path, monkeypatch, fire):
        log = tmp_path / "codex.log"
        monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(log))
        fire()
        return log.read_text().splitlines()[-1]

    def _assert_canon(self, line):
        import re
        assert re.match(self.CANON_LINE_SRC, line), line

    def test_turn_ok_is_canonical(self, tmp_path, monkeypatch):
        import codex_server as cs
        line = self._last_line(tmp_path, monkeypatch, lambda: cs._turn_ok_log(
            {"model_val": "m1", "effort_val": "high", "mcp_mode": "isolated",
             "retries": 0, "setup_ms": 5, "cold_spawn": False},
            {"timing": {"duration_ms": 12}, "usage": {"total_tokens": 99}}))
        self._assert_canon(line)
        assert "event=TURN_OK" in line and "| model=m1 |" in line
        assert line.split(" | ")[1] == "event=TURN_OK"   # position pinned

    def test_turn_error_msg_is_url_redacted(self, tmp_path, monkeypatch):
        import codex_server as cs
        line = self._last_line(tmp_path, monkeypatch, lambda: cs._turn_error_log(
            {"model_val": "m1"}, "boom https://u:p@x.test/api?token=SECRET"))
        assert "SECRET" not in line and "u:p" not in line
        assert "?<redacted>" in line and "event=TURN_ERROR" in line

    def test_generic_drift_keeps_original_detail_in_acc(self, tmp_path, monkeypatch):
        import codex_server as cs
        acc = []
        raw = "label https://x.test/?t=SECRET"
        self._last_line(tmp_path, monkeypatch,
                        lambda: cs._drift_warn(acc, "OUT_OF_ENUM_LABEL", raw))
        assert acc == [{"code": "OUT_OF_ENUM_LABEL", "detail": raw}]  # UNredacted

    def test_generic_drift_detail_is_url_redacted(self, tmp_path, monkeypatch):
        # R1-F4: the DURABLE copy of a generic detail= must redact — TRANSLATE_FAILED /
        # UNKNOWN_* details carry exception text that can embed authenticated URLs.
        import codex_server as cs
        line = self._last_line(tmp_path, monkeypatch, lambda: cs._drift_warn(
            None, "TRANSLATE_FAILED", "openai: HTTPError: https://u:p@api.test/v1?key=SECRET"))
        assert "SECRET" not in line and "u:p" not in line
        assert "?<redacted>" in line and "detail=" in line

    def test_worker_field_is_last(self, tmp_path, monkeypatch):
        import codex_server as cs
        monkeypatch.setenv("BULLDOZER_WORKER", "3")
        line = self._last_line(tmp_path, monkeypatch,
                               lambda: cs._drift_warn(None, "TURN_OK", model="m"))
        assert line.endswith(" | worker=3")

    def test_nonstring_wire_value_cannot_smuggle_url(self, tmp_path, monkeypatch):
        # R7-F1: a wire-derived dict/list must not bypass redaction via a type
        # gate — append_line stringifies AFTER the redaction decision.
        import codex_server as cs
        line = self._last_line(tmp_path, monkeypatch, lambda: cs._drift_warn(
            None, "TURN_OK", tokens={"url": "https://u:p@x.test/api?token=SECRET"}))
        assert "SECRET" not in line and "u:p" not in line

    def test_helper_unavailable_warns_once_and_drops(self, tmp_path, monkeypatch, capsys):
        # R5-F1: the hard warn-once-and-drop contract when the import failed —
        # exactly ONE stderr warning, NO durable line, acc contract untouched,
        # no exception. (The unwritable-path tests keep the helper present and
        # never reach this branch.)
        import codex_server as cs
        log = tmp_path / "codex.log"
        monkeypatch.setenv("BULLDOZER_CODEX_LOG", str(log))
        monkeypatch.setattr(cs, "_bl_append", None)
        monkeypatch.setattr(cs, "_HELPER_WARNED", False)
        acc = []
        cs._drift_warn(acc, "OUT_OF_ENUM_LABEL", "d1")
        cs._drift_warn(None, "TURN_ERROR", "d2")
        assert not log.exists()                                   # dropped, no legacy fallback
        assert acc == [{"code": "OUT_OF_ENUM_LABEL", "detail": "d1"}]  # acc still fed
        assert capsys.readouterr().err.count("audit disabled") == 1   # once, not per call

    def test_long_value_single_truncation_after_redaction(self, tmp_path, monkeypatch):
        # R3-F1: ONE truncation point (the helper's), AFTER redaction. With the
        # URL early and a long tail, the redacted head + marker fit the cap and
        # MUST survive; the secret must be gone; no writer-side double-slice.
        import codex_server as cs
        long_msg = "boom https://u:p@x.test/api?token=SECRET " + ("x" * 600)
        line = self._last_line(tmp_path, monkeypatch,
                               lambda: cs._turn_error_log({"model_val": "m1"}, long_msg))
        assert "SECRET" not in line and "u:p" not in line
        assert "?<redacted>" in line          # marker inside the cap survives
        assert "…" in line                    # helper (not the writer) truncated
```

Also cover: `_interrupt_log`, `_log_approval_event` (attended, unattended+rule, `ui=dialog`), `_warning_log` (top-level / nested / unknown-shape → `msg=`), `_info_error_log`, and `PARK` via the REAL producer `build_awaiting_payload` (R1-F5 — a direct `_drift_warn(None, "PARK", …)` parity call is NOT sufficient, it keeps passing if `build_awaiting_payload` still emits the old positional string; same call recipe as Step M1 item 1).

- [ ] **Step S2: run, verify FAIL** — legacy lines have no `event=` → regex fails.

- [ ] **Step S3: implementation.** At module top (after the existing imports):

```python
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
try:
    from bulldozer_log import (append_line as _bl_append,
                               redact_urls_in_text as _bl_redact)
except Exception:  # helper missing/broken: warn once at write time, drop lines
    _bl_append = None
    _bl_redact = None
```

Rewrite `_drift_warn` (keep `_now_iso` only if still referenced elsewhere; delete if orphaned):

```python
_HELPER_WARNED = False


def _drift_warn(acc, code: str, detail=None, **fields) -> None:
    """Canonical audit line (#334): routed through lib/bulldozer_log.append_line —
    offset ts, event=, session= (from CLAUDE_CODE_SESSION_ID), sanitized values,
    5MB locked rotation. acc keeps the ORIGINAL detail (user-facing _drift);
    only the durable copy is URL-redacted. NEVER raises."""
    global _HELPER_WARNED
    if acc is not None:
        acc.append({"code": code, "detail": detail})
    try:
        if _bl_append is None:
            if not _HELPER_WARNED:
                _HELPER_WARNED = True
                log("audit disabled: bulldozer_log helper unavailable")
            return
        path = os.environ.get("BULLDOZER_CODEX_LOG") or os.path.expanduser(
            "~/.claude/hooks/bulldozer-codex.log")
        kv = {}
        if detail is not None:
            kv["detail"] = _bl_redact(detail)
        for k, v in fields.items():
            # UNCONDITIONAL redaction (R7-F1): redact_urls_in_text str()-coerces
            # internally, so a WIRE-derived dict/list (e.g. a malformed
            # tokenUsage.totalTokens carrying a URL) cannot bypass the choke
            # point — append_line would stringify it AFTER the redaction
            # decision otherwise. Output-equivalent for clean values (the
            # helper's _value str()-coerces anyway).
            kv[k] = _bl_redact(v)
        worker = os.environ.get("BULLDOZER_WORKER")
        if worker:
            kv["worker"] = worker
        _bl_append(path, code, **kv)
    except Exception:
        pass
```

Decompose the 7 named writers into kwargs — value expressions unchanged, `_log_san`/`_san` wrappers dropped (the helper sanitizes; the redactor runs in `_drift_warn`):

```python
# TRUNCATION ORDER (R3-F1): writers pass values UNtruncated — the raw [:500]
# pre-slices are REMOVED. The single truncation point is the helper's _value
# cap, which runs AFTER _drift_warn's redaction, so redaction always sees the
# FULL text (a raw pre-slice could cut a URL mid-token). The `?<redacted>`
# marker itself is NOT guaranteed to survive the length cap on very long
# values — secrets are still removed either way; documented in M2.

# _turn_error_log body:
        _drift_warn(None, "TURN_ERROR",
                    model=ts.get('rerouted_model') or ts.get('model_val') or tm.get('model') or 'default',
                    effort=ts.get('effort_val') or tm.get('effort') or 'default',
                    mcp=ts.get('mcp_mode') or '?',
                    retries=ts.get('retries') or 0,
                    msg=str(emsg or 'unknown error'))

# _turn_ok_log body:
        kw = dict(
            model=ts.get('rerouted_model') or ts.get('model_val') or tm.get('model') or 'default',
            effort=ts.get('effort_val') or tm.get('effort') or 'default',
            mcp=ts.get('mcp_mode') or '?',
            retries=ts.get('retries') or 0,
            duration_ms=timing.get('duration_ms'),
            tokens=tokens if tokens is not None else 'NA',
        )
        if ts.get("setup_ms") is not None:
            kw["setup_ms"] = ts["setup_ms"]
            kw["cold_spawn"] = 'true' if ts.get('cold_spawn') else 'false'
        _drift_warn(None, "TURN_OK", **kw)

# _interrupt_log body:
        _drift_warn(None, "INTERRUPT",
                    interrupted_by=interrupted_by,
                    thread_warm='true' if thread_warm else 'false',
                    model=ts.get('rerouted_model') or ts.get('model_val') or tm.get('model') or 'default',
                    mcp=ts.get('mcp_mode') or '?')

# _info_error_log body:
        _drift_warn(None, "INFO_ERROR", query=query,
                    msg=str(msg or 'unknown error'))

# _warning_log: same msg-selection logic (the json.dumps(p)[:300] fallback loses
# its [:300] too — helper caps at 500 after redaction), then:
        _drift_warn(None, "WARNING", msg=str(msg))

# PARK (in build_awaiting_payload):
        _drift_warn(None, "PARK", kind=kind, method=method,
                    token8=str(park_token)[-8:])

# _log_approval_event body:
        kw = dict(method=method, decision=_approval_decision_label(decision),
                  wait_ms=wait_ms, timed_out='true' if timed_out else 'false')
        if unattended:
            kw["unattended"] = 'true'
            kw["rule"] = rule
        if ui and ui != "cc":
            kw["ui"] = ui
        _drift_warn(None, "APPROVAL", **kw)
```

Generic call sites (`VERSION_MISMATCH`, `TRANSLATE_FAILED`, `UNKNOWN_*`, `OUT_OF_ENUM_LABEL`, `NOTIFICATION_FIXTURE_MISSING`, `INTERRUPT_DISABLED`) keep their 3-arg form (now lands as `detail=` field) — **EXCEPT their raw pre-slices (R3-F1 r4): the TRUNCATION ORDER rule applies to generic sites too.** The two `TRANSLATE_FAILED` producers slice exception text at the call site (`f"openai: {type(e).__name__}: {str(e)[:100]}"` and the provider-loop twin) — a `[:100]`-cut can leave a partial URL that the redactor cannot mark. Drop BOTH `[:100]` slices (`str(e)` bare; the helper caps at 500 after redaction), then sweep for stragglers: `grep -n '_drift_warn' mcp/codex_server.py | grep '\[:'` must come back empty. Pin structurally in S1:

```python
    def test_no_raw_preslice_at_drift_warn_call_sites(self):
        # R3-F1 r4: no _drift_warn call may pre-slice its raw text — the ONE
        # truncation point is the helper's _value cap, AFTER redaction.
        # Balanced-paren extraction of each call's args (a fixed window would
        # bleed into neighboring code and false-positive on legit [:N] slices).
        import inspect
        import re as _re
        import codex_server as cs
        src = inspect.getsource(cs)
        for m in _re.finditer(r"_drift_warn\(", src):
            depth, i = 1, m.end()
            while depth and i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            args = src[m.end():i]
            assert not _re.search(r"\[:\d+\]", args), (
                "raw [:N] pre-slice inside a _drift_warn call near offset %d" % m.start())
```

After migration: `grep -n '_log_san' mcp/codex_server.py` — if only its def remains, delete it.

- [ ] **Step S4: run new tests → PASS; run `uv run pytest tests/test_codex_mcp_v2.py` → fix every legacy-shape pin** (search patterns: `" | APPROVAL | "`, `" | TURN_`, `"| worker="`, forgery/injection tests — update expectations to canonical shape, keep their INTENT: forgery must still be impossible, injection still neutralized). Also `uv run pytest tests/test_codex_facade.py` — the engine-line `worker=` suffix compatibility test moves to canonical expectation.
- [ ] **Step S5: mutation-check** — FIVE separate mutations (R1-F4 + R5-F1 + R7-F1: the `detail=`/`fields` redaction paths, the helper-unavailable warn contract, and the type-unconditional coverage are independent and must each be pinned):
  1. revert `_bl_redact` on **fields** values only → `test_turn_error_msg_is_url_redacted` MUST fail;
  2. revert `_bl_redact` on **detail** only → `test_generic_drift_detail_is_url_redacted` MUST fail;
  3. remove the `_HELPER_WARNED` once-guard (warn EVERY call) → `test_helper_unavailable_warns_once_and_drops` MUST fail on the count;
  4. remove the `_bl_append is None` branch entirely (fall through to the broad `except`) → same test MUST fail on the missing warning;
  5. reintroduce the `isinstance(v, str)` gate on fields redaction → `test_nonstring_wire_value_cannot_smuggle_url` MUST fail (R7-F1);
  restore all.
- [ ] **Step S6: commit** — `git add mcp/codex_server.py tests/test_codex_mcp_v2.py tests/test_codex_facade.py && git commit -m "feat(mcp): codex audit writers onto canonical grammar + URL redaction (#334)"`

---

### Task C: consult channel — `_log_completion` + SKILL.md echoes

**Files:**
- Modify: `skills/consult/scripts/consult_panel.py`, `skills/consult/SKILL.md`
- Test: `tests/test_consult_panel.py`, `tests/test_skill_prompts.py`

- [ ] **Step C1: failing tests** — extend the existing completion-line tests (`test_run_panel_appends_one_completion_line` cluster + `#322 PR3` cluster) to require `| event=consult-complete | session=` right after ts; add: import-failure → warn once at write attempt, line dropped, panel exit unaffected; `--help` still writes nothing.

- [ ] **Step C2: implementation.** Module top of `consult_panel.py`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
try:
    from bulldozer_log import append_line as _bl_append
except Exception:
    _bl_append = None
```

`_log_completion` — replace manual ts/session/open with:

```python
    try:
        if _bl_append is None:
            raise RuntimeError("bulldozer_log helper unavailable")
        elapsed = time.perf_counter() - t0
        web = ",".join(m for m in selected if m in web_set)
        fields = {
            "round": 1,
            "verdict": _verdict_label(ok, verdict_mode, survivors),
            "tokens": "NA",
            "time": f"{elapsed:.1f}s",
            "models": ",".join(selected),
            "web": web,
        }
        if legs is not None:
            fails = [l for l in legs if l.output is None]
            fields["survivors"] = f"{len(legs) - len(fails)}/{len(legs)}"
            fields["failures"] = ",".join(f"{l.display}:{_reason_class(l.reason)}" for l in fails)
            fields["legtimes"] = ",".join(f"{l.display}:{l.elapsed_s}" for l in legs if l.elapsed_s is not None)
        if "agy" in selected:
            fields["agy_model"] = re.sub(r"[^A-Za-z0-9._()-]", "_", _AGY_MODEL)
        if "codex" in selected:
            fields["codex_effort"] = "medium"  # build_codex_cmd's default — not threaded through
        fields["project"] = _project_root()
        _bl_append(CONSULT_LOG, "consult-complete", **fields)
    except Exception as e:
        # (keep the existing _LOG_WARNED once-per-process stderr warning block verbatim)
```

Session derivation moves INTO the helper (same normalization, verified identical). The helper's own `_warn_once` covers write failures; `_LOG_WARNED` now covers import failure + unexpected shapes.

- [ ] **Step C3: SKILL.md.** In the `## Quick Reference — Full Invocation Template` section's bash template, add ONE resolver early (with the other setup, before the step-4 codex invocation):

```bash
# 0b. Resolve the shared log writer once (#221: $CLAUDE_PLUGIN_ROOT is NOT in the Bash env)
BLOG=""
for d in ${CLAUDE_PLUGIN_ROOT:+"$CLAUDE_PLUGIN_ROOT"} $(ls -dt ~/.claude/plugins/cache/*/bulldozer/*/ 2>/dev/null); do
  [ -f "$d/lib/bulldozer_log.py" ] && BLOG="$d/lib/bulldozer_log.py" && break
done
```

Replace BOTH echoes (failure branch 4b + step 6) with the CLI shim (it derives `session=` from `CLAUDE_CODE_SESSION_ID` itself; `S=` prep lines are deleted). **R1-F3: resolution failure must WARN, not silently drop** — the plan's warn-once-and-drop rule applies to bash writers too:

```bash
if [ -n "$BLOG" ]; then
    python3 "$BLOG" "${BULLDOZER_CONSULT_LOG:-$HOME/.claude/hooks/bulldozer-consult.log}" consult-complete \
      "round=$ROUND" "verdict=$VERDICT" "tokens=NA" "time=${ELAPSED}s" "model=$MODEL" \
      "project=$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || true
else
    echo "warning: bulldozer_log.py not found — consult completion line NOT logged" >&2
fi
```

Both shim calls (4b and step 6) use the `${BULLDOZER_CONSULT_LOG:-…}` form — the SAME env override `consult_panel.py` honors, normal path as fallback (R5-F2 r6: a hardcoded path made the inline leg unobservable in the V2 tmpfile smoke).

The R1-F3 detection gap (the contract test bypasses the resolver) is closed structurally: Step M1 item 3 asserts the SKILL.md template contains BOTH the `$BLOG` guard AND the warning branch (`consult completion line NOT logged`), so a template regression that silently drops telemetry fails the suite.

(step 6 passes `"tokens=$T"` instead of `NA`.) `model=` stays SINGULAR — inline-vs-panel discrimination is by field name, documented. Update the SKILL.md "Two kinds of line" section (~line 273) to show both `event=` forms.

- [ ] **Step C4: run** `uv run pytest tests/test_consult_panel.py tests/test_skill_prompts.py` → fix remaining pins.
- [ ] **Step C5: commit** — `git add skills/consult/scripts/consult_panel.py skills/consult/SKILL.md tests/test_consult_panel.py tests/test_skill_prompts.py && git commit -m "feat(consult): completion lines onto canonical grammar, both writers (#334)"`

---

### Task F: facade `_log_writer` redaction

**Files:**
- Modify: `mcp/codex_facade.py`
- Test: `tests/test_codex_facade.py`

- [ ] **Step F1: failing test** — through the facade fixture or `Facade._log` + `flush_log` directly:
  1. **all four payload keys, parametrized (R4-F1):** for EACH `key in ("err", "reason", "detail", "msg")`: `_log("FACADE_ERROR", **{key: "X https://u:p@x.test/?t=SECRET"})` → line contains `?<redacted>`, no `SECRET`/`u:p` — dropping ANY single key from `_REDACT_KEYS` must fail exactly that key's case (this IS the per-key mutation check);
  2. **correlation-id preservation (R1-F2):** `_log("FACADE_DONE", call="https://weird.test/id?x=1", tool="codex_run")` → the `call=` value is byte-IDENTICAL in the line (correlation ids are opaque tokens — rewriting one breaks call-chain joins; only payload fields redact);
  3. **non-string payload under a redacted key (R7-F1):** `_log("FACADE_ERROR", err={"u": "https://u:p@x.test/?t=SECRET"})` → no `SECRET`/`u:p` in the line (type-unconditional under payload keys).
- [ ] **Step F2: implementation** — TARGETED keys, not blanket (R1-F2). In `_log_writer`, immediately before `bulldozer_log.append_line(...)`:

```python
# module level:
_REDACT_KEYS = frozenset({"err", "reason", "detail", "msg"})  # payload-bearing fields only —
# call=/token=/tool=/worker= are opaque correlation ids a miner joins on (R1-F2)

# in _log_writer — key-gated but TYPE-unconditional (R7-F1): under a payload
# key, EVERY value type redacts (redact_urls_in_text str()-coerces); an
# isinstance(str) gate would let a dict/list smuggle a URL past the choke
# point, since append_line stringifies later:
                kv = {k: (bulldozer_log.redact_urls_in_text(v)
                          if k in _REDACT_KEYS else v)
                      for k, v in kv.items()}
```

(Background thread — off the hot path. Any FUTURE `_log` call site adding a free-text field must use one of the `_REDACT_KEYS` names — add a one-line comment at `_log()` saying so.)
- [ ] **Step F3: PASS + mutation-check** — (a) drop the dict-comp entirely → all four key cases fail; (b) remove ONE key (e.g. `msg`) from `_REDACT_KEYS` → exactly that key's parametrized case fails, the rest pass (R4-F1); (c) reintroduce an `isinstance(v, str)` gate → the non-string payload case fails (R7-F1); restore. Run full `tests/test_codex_facade.py`.
- [ ] **Step F4: commit** — `git add mcp/codex_facade.py tests/test_codex_facade.py && git commit -m "feat(facade): URL-redact payload log fields (#334)"`

---

### Task M: miner-facing contract test + docs inversion

**Files:**
- Create: `tests/test_log_grammar_contract.py`
- Modify: `README.md`, `CLAUDE.md`, `tests/test_skill_prompts.py`

- [ ] **Step M1: contract test** — the ask-#4 gap-closer: REAL producers, real files, one regex:

```python
"""#334: miner-facing log-shape contract. Exercises the REAL producers (not the
helper) into temp logs and validates EVERY emitted line against the ONE canonical
regex — the gap that let #322 close as 53/53 while unmigrated producers
drifted (the issue named two; the prep surfaced a third shape)."""
import re
CANON = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"
    r" \| event=[A-Za-z0-9_-]+ \| session=[A-Za-z0-9_-]{1,8}"
    r"( \| [A-Za-z0-9_-]+=[^|]*)*$")
```

Producers fired (each → assert every produced line matches CANON; the TURN_ERROR/WARNING/INFO_ERROR ones ALSO assert `SECRET` absent + `?<redacted>` present):
1. codex: `_turn_ok_log`, `_turn_error_log` (URL-secret msg), `_interrupt_log`, `_log_approval_event` (all three variants), `_warning_log` (three shapes), `_info_error_log`, generic `_drift_warn(acc, "UNKNOWN_NOTIFICATION", …)` AND generic-with-URL `_drift_warn(None, "TRANSLATE_FAILED", "… https://…?t=SECRET")` — via `BULLDOZER_CODEX_LOG` monkeypatch. **PARK via the REAL producer (R1-F5):** call `build_awaiting_payload("item/commandExecution/requestApproval", {}, {}, "", "tok-abcdefgh")` (offline-safe: params/ts empty dicts) and assert the produced PARK line has `event=PARK`, parsed `kind=`/`method=`/`token8=` FIELDS (not one positional blob) and `token8=` is exactly the last 8 chars, never the full token — a direct `_drift_warn(None, "PARK", …)` parity call would keep passing if `build_awaiting_payload` itself still emitted the old positional string.
2. consult: `_log_completion` with and without `legs` — via `BULLDOZER_CONSULT_LOG` monkeypatch + module reload (CONSULT_LOG binds at import).
3. SKILL.md structural: the consult SKILL contains NO `echo "$(date -Iseconds)` writer to `bulldozer-consult.log` anymore (anti-regression of the third shape), DOES document `consult-complete`, its template carries both the `$BLOG` guard and the resolver-failure warning branch (`consult completion line NOT logged`) — R1-F3's silent-drop detector — AND both shim calls use the `${BULLDOZER_CONSULT_LOG:-` override form (R5-F2 r6: a hardcoded log path makes the inline leg unobservable in tmpfile smokes).
4. CLI shim: `python3 lib/bulldozer_log.py <tmp> consult-complete round=1 …` in a subprocess → line matches CANON (this is what SKILL.md now runs).

- [ ] **Step M2: README `## Log Format` inversion** — replace the three-row "NOT on the shared writer" table with: all active producers emit the canonical grammar as of #334; transition rules for MINERS (history + un-restarted sessions still contain legacy lines): per-line detection — segment 2 `event=`→canonical, else positional legacy; same event tokens both shapes; legacy `WARNING` bare tail → `msg`; **legacy GENERIC positional records (`VERSION_MISMATCH`, `TRANSLATE_FAILED`, `UNKNOWN_*`, `OUT_OF_ENUM_LABEL`, `NOTIFICATION_FIXTURE_MISSING`, `INTERRUPT_DISABLED`) carried their payload as an unkeyed third segment → maps to `detail=` (R4-F2; the prep's "preserve malformed/bare legacy tails as `detail`" rule)**; **legacy codex lines carry NO `session=` → assign a missing-session SENTINEL, never invent an identity, and their naive no-offset timestamps must NOT be assigned a timezone (R4-F2 r5; prep miner rules 4+7)**; consult legacy completion = no `event=` + `session=` first; never dual-written. **The cutover is PER-PRODUCER, not one date (R4-F2 r5): check/look went canonical with #322 (2026-07-11); the facade's `FACADE_*` writer was BORN canonical with #344 (registered as plugin default 2026-07-18 — it did not exist at #322 time); the codex `TURN_*`/generic and consult completion writers stayed legacy until #334 — the README must not imply a single #322 cutover.** Honest redaction scope (**R2-F2 — no channel-wide overclaim**): the codex channel redacts URL query/userinfo/fragment in `_drift_warn` values; the **facade redacts ONLY the payload fields `err`/`reason`/`detail`/`msg` — opaque identifier fields (`call=`, `token=`, `tool=`, `worker=`) are intentionally preserved verbatim**, so a URL-shaped correlation id persists as-is; the look channel keeps its D2 semantics; paths remain visible; bare secrets outside URLs, emails, and MALFORMED URIs with literal whitespace (RFC 3986 mandates %-encoding) are NOT detected; on very long values the length cap may truncate the `?<redacted>` marker (the secret itself is removed BEFORE truncation — R3-F1); history is NOT rewritten.
- [ ] **Step M3: invert `tests/test_skill_prompts.py` contract tests** — `test_non_canonical_producers_are_named` → `test_all_producers_canonical_and_transition_documented` (asserts the NEW claims + miner transition rules present); `test_redaction_claim_is_scoped_to_where_it_exists` → asserts the new honest scope wording: "paths remain visible" present, no "plugin-wide, no secrets" overclaim, **AND the facade payload-key qualifier is pinned (README must name `err`/`reason`/`detail`/`msg` as the ONLY facade-redacted fields and state identifier fields are preserved — R2-F2)**, **AND the three transition properties are pinned (R4-F2 r5): missing-session sentinel wording, naive-timestamp/no-invented-timezone wording, and the per-producer cutover wording (#322 vs #334) must each be present in the README section**.
- [ ] **Step M4: CLAUDE.md** — `## Architecture: logging`: drop the "NOT migrated (verified 2026-07-12…)" passage, state: all producers canonical since #334; miner transition note stays (history); redaction = URL-scoped with the SAME per-channel qualifiers as M2 (**facade: payload keys only, identifiers preserved — R2-F2**), bare tokens undetected. Update the `## Architecture: codex MCP server` `_drift_warn` mentions.
- [ ] **Step M5: run full suite** — `uv run pytest tests/ -m "not slow"` green.
- [ ] **Step M6: commit — TWO Conventional commits** (R2-F3: `test+docs` is not a valid Conventional Commit type):

```bash
git add tests/test_log_grammar_contract.py tests/test_skill_prompts.py
git commit -m "test: miner-facing log-grammar contract over real producers (#334)"
git add README.md CLAUDE.md
git commit -m "docs: log-format inversion — all producers canonical, honest redaction scope (#334)"
```

---

### Task V: verification protocol

- [ ] **V1: full suite** incl. slow-safe: `uv run pytest tests/ -v` (codex present → slow e2e run; `test_e2e.py` needs JAINE Browser — run on this machine as usual).
- [ ] **V2: live smoke — through a FRESH process from the COMMITTED branch source (R5-F2:** server code does NOT hot-reload — the dev session's already-running MCP server predates these edits, and a fresh log mtime only proves SOME loaded producer wrote a line**):**
  1. **codex channel:** headless vehicle (mcp-server-dev §1) — `claude -p` with `--strict-mcp-config` and `--mcp-config` naming a config that launches `python3 <THIS-WORKTREE>/mcp/codex_facade.py` with env `BULLDOZER_CODEX_LOG=<tmpfile>`; this SPAWNS a new server from the committed source. Run one small `codex_run` (`mcp:'isolated'`) → assert the tmpfile's `TURN_OK` line matches CANON with real `session=` and `worker=`. Record `git rev-parse HEAD` alongside the log excerpt as the smoke evidence.
  2. **consult channel — per-producer observability (R5-F2 r6):** run the two legs as fresh subprocesses from this worktree with SEPARATE tmpfiles so neither can mask the other:
     - **inline (SKILL.md template):** run with `CLAUDE_PLUGIN_ROOT=<THIS-WORKTREE>` (pins the `$BLOG` resolver to the worktree helper — without it the resolver falls back to an installed cache that predates these edits) AND `BULLDOZER_CONSULT_LOG=<tmpfile-A>` (the C3 shim honors it via `${BULLDOZER_CONSULT_LOG:-…}`) → assert EXACTLY ONE canonical `consult-complete` line with `model=` singular in tmpfile-A;
     - **panel:** `python3 skills/consult/scripts/consult_panel.py "<explicit smoke question>" …` with `BULLDOZER_CONSULT_LOG=<tmpfile-B>` — **pass the positional question, assert subprocess EXIT 0, and require a NON-EMPTY `models=` value** (R8-F1: the argparse-error path also writes a canonical `consult-complete` line with empty `models=` — without these three asserts the smoke can false-green on CLI validation failure) → assert EXACTLY ONE canonical `consult-complete` line with non-empty `models=` plural in tmpfile-B.
     The M1 item-3 structural test ALSO pins the template's `${BULLDOZER_CONSULT_LOG:-` form, so a hardcoded-path regression is caught offline.
- [ ] **V3: `codex_review`** on the committed diff — **`target=branch:bulldozer/main`** (R3-F2: `branch:<name>` names the BASE to diff against; passing the feature branch itself would compare it to itself → empty diff), run with the working tree ON `bulldozer/fix/334-log-grammar`; rounds until clean; every accepted finding fixed with a mutation-checked test (`feedback_mutation_check_every_fix`); every round after the first re-verifies prior fixes (`feedback_review_loop_closes_on_clean_round`).
- [ ] **V4: PR — two steps (R7-F2:** `create-pr.sh` accepts only `--title`/`--draft`/`--dry-run` and generates its body solely from bound `Closes #N` lines — it cannot carry the handoff body**):**
  1. `../../scripts/create-pr.sh --title "feat(bulldozer): #334 canonical log grammar for codex/consult + redaction"` — creates the PR with the auto `Closes #N` body;
  2. write the full body to a file (what/why, the D1 UPPERCASE deviation note, miner transition rules, test evidence, the `Closes #334` line PRESERVED from step 1's body) and `gh pr edit <N> --body-file <file>`; then verify published: `gh pr view <N> --json body` contains all four sections AND the `Closes #334` line.
  After merge: close #334 MANUALLY (bulldozer/main merges don't auto-close), comment with the D1 deviation rationale, refresh consumer caches, note the restart caveat (running MCP servers keep the old writer until respawn).

## Self-Review (done 2026-07-19)

- Issue asks: ask1 → Task S (in-process ✓, UPPERCASE deviation documented); ask2 → Task C; ask3 → decided: redact codex+facade, honest README wording (Task L/S/F/M2); ask4 → Task M1. Prep extras: SKILL.md third shape → Task C3; facade payload fields (`err`/`reason`/`detail`/`msg`) → Task F. NOTE: the prep's UNIFIED TABLE sketched the narrower `err/detail` pair — the four-key `_REDACT_KEYS` set deliberately SUPERSEDES it (widened through review R1-F2→R4-F1; the prep is a point-in-time snapshot by its own header, not living documentation).
- No placeholders; interfaces named; existing-test fix lists are search-driven by design (line pins drift — repo doctrine).
- Known open risk for the plan-reviewer: consult `CONSULT_LOG` binds at import from env — the contract test needs module reload; SKILL.md `$BLOG` resolver must not break when NO cache dir exists (fresh dev machine) — the C3 `if/else` guard covers it: shim runs when resolved, stderr warning (`consult completion line NOT logged`) when not (R1-F3 — never a silent drop).
