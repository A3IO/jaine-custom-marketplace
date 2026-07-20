"""Runtime write-path tests for lib/bulldozer_log.py (issue #322 PR1).

Spec: docs/superpowers/specs/2026-07-11-bulldozer-log-grammar-design.md.
These are BEHAVIORAL tests (tmp files, real writes) — not AST/source-text checks.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
HELPER = LIB_DIR / "bulldozer_log.py"
sys.path.insert(0, str(LIB_DIR))

import bulldozer_log  # noqa: E402
from bulldozer_log import append_line  # noqa: E402

from conftest import test_env

TS_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"


@pytest.fixture(autouse=True)
def _reset_warning_flag():
    """The once-per-process stderr warning flag must not leak between tests."""
    bulldozer_log._WARNED = False
    yield
    bulldozer_log._WARNED = False


def read_lines(p: Path):
    return p.read_text().splitlines()


# ── grammar exactness ──


def test_line_grammar_and_field_order(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abcdef1234567890")
    log = tmp_path / "x.log"
    assert append_line(log, "screenshot", port=9333, ok="yes") is True
    (line,) = read_lines(log)
    assert re.fullmatch(
        TS_RE + r" \| event=screenshot \| session=abcdef12 \| port=9333 \| ok=yes", line
    ), line


def test_timestamp_has_colon_offset_and_second_precision(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "e")
    ts = read_lines(log)[0].split(" | ")[0]
    assert re.fullmatch(TS_RE, ts), ts  # +07:00 form, no microseconds


def test_session_fallback_na(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    log = tmp_path / "x.log"
    append_line(log, "e")
    assert " | session=NA | " in read_lines(log)[0] + " | "


def test_explicit_session_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "envenvenv")
    log = tmp_path / "x.log"
    append_line(log, "e", session="explicit1")
    assert "session=explicit" in read_lines(log)[0]  # [:8]


# ── value sanitization ──


def test_value_newline_pipe_sanitized(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "e", expr="a\nb | c=d\re")
    lines = read_lines(log)
    assert len(lines) == 1
    assert "expr=a b / c=d e" in lines[0]


def test_value_truncation_exactly_500(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "e", v="x" * 501)
    val = read_lines(log)[0].split("v=", 1)[1]
    assert len(val) == 500
    assert val == "x" * 499 + "…"
    # boundary: exactly 500 stays intact
    log2 = tmp_path / "y.log"
    append_line(log2, "e", v="y" * 500)
    assert read_lines(log2)[0].split("v=", 1)[1] == "y" * 500


# ── token validation (event + keys) ──


def test_event_with_newline_pipe_spaces_normalized(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "x\nforged=1 | y")
    (line,) = read_lines(log)
    ev = line.split(" | ")[1]
    assert ev.startswith("event=")
    assert re.fullmatch(r"event=[A-Za-z0-9_-]{1,64}", ev), line


def test_empty_event_becomes_invalid(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "")
    assert " | event=invalid | " in read_lines(log)[0]


def test_non_string_event_never_raises(tmp_path):
    log = tmp_path / "x.log"
    assert append_line(log, object()) is True
    assert append_line(log, 42) is True
    assert " | event=42 | " in read_lines(log)[1]


def test_non_string_session_never_raises(tmp_path):
    log = tmp_path / "x.log"
    assert append_line(log, "e", session=object()) is True
    assert append_line(log, "e", session=42) is True
    assert "session=42" in read_lines(log)[1]


def test_adversarial_session_normalized(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "e", session="x | event=forged")
    (line,) = read_lines(log)
    sid = line.split(" | ")[2]
    assert re.fullmatch(r"session=[A-Za-z0-9_-]{1,8}", sid), line


def test_bad_key_normalized_and_collision_last_wins(tmp_path):
    log = tmp_path / "x.log"
    append_line(log, "e", **{"bad key": 1, "bad_key": 2})
    (line,) = read_lines(log)
    assert line.count("bad_key=") == 1
    assert "bad_key=2" in line  # input-order last wins


def test_python_api_reserved_key_raises_at_binding(tmp_path):
    # Caller-side Python semantics — documented, OUTSIDE the never-raises contract.
    with pytest.raises(TypeError):
        append_line(tmp_path / "x.log", "e", **{"event": "forged"})


def test_event_named_event_or_session_not_rewritten(tmp_path):
    # Reserved-name rewriting is a FIELD-KEY rule; the event VALUE keeps its identity.
    log = tmp_path / "x.log"
    append_line(log, "event")
    append_line(log, "session")
    lines = read_lines(log)
    assert lines[0].split(" | ")[1] == "event=event"
    assert lines[1].split(" | ")[1] == "event=session"


# ── rotation ──


def test_rotation_over_5mb(tmp_path):
    log = tmp_path / "x.log"
    with open(log, "w") as f:
        f.seek(5 * 1024 * 1024)
        f.write("x")
    append_line(log, "fresh")
    rotated = tmp_path / "x.log.1"
    assert rotated.exists()
    assert "event=fresh" in read_lines(log)[0]
    # second rotation overwrites .1  (r+ so seek works — "a" mode always writes at EOF)
    with open(log, "r+") as f:
        f.seek(5 * 1024 * 1024 + 10)
        f.write("y")
    append_line(log, "fresh2")
    assert "event=fresh2" in read_lines(log)[0]


def test_concurrent_rotation_never_discards_history(tmp_path):
    # Race: writer A rotates full log → appends to fresh log; writer B's replace
    # must not clobber .1 with the tiny fresh file. flock serializes the critical
    # section — total line count across log + log.1 (+ log.1 pre-race) is preserved.
    log = tmp_path / "x.log"
    n_seed = 60_000
    with open(log, "w") as f:
        for i in range(n_seed):
            f.write("seed line %06d %s\n" % (i, "x" * 80))  # ~5.5MB total
    assert log.stat().st_size > 5 * 1024 * 1024
    procs = [
        subprocess.Popen(
            [sys.executable, str(HELPER), str(log), "burst%d" % i],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for i in range(8)
    ]
    for p in procs:
        assert p.wait() == 0
    total = sum(
        len(read_lines(p)) for p in (log, Path(str(log) + ".1")) if p.exists()
    )
    assert total >= n_seed + 8, "rotated history was discarded (total=%d)" % total


# ── failure path ──


def test_write_failure_returns_false_and_warns_once(tmp_path, capsys):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    bad_log = blocker / "sub" / "x.log"  # parent is a file → mkdir/open fails
    assert append_line(bad_log, "e") is False
    assert append_line(bad_log, "e") is False
    err = capsys.readouterr().err
    assert err.count("could not write") == 1  # once per process


def test_write_failure_with_broken_stderr_never_raises(tmp_path, monkeypatch):
    # Detached/background process: stderr closed → the warning itself must not escape.
    class Broken:
        def write(self, *_):
            raise ValueError("stderr closed")
        def flush(self):
            raise ValueError("stderr closed")

    monkeypatch.setattr(sys, "stderr", Broken())
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    assert append_line(blocker / "sub" / "x.log", "e") is False  # no exception


# ── CLI shim ──


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(HELPER), *args], capture_output=True, text=True
    )


def test_cli_writes_line_and_exits_zero(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    r = run_cli(str(log), "round", "depth=quick", "findings=2")
    assert r.returncode == 0
    (line,) = read_lines(log)
    assert " | event=round | " in line
    assert "depth=quick" in line and "findings=2" in line


def test_cli_reserved_key_rewritten(tmp_path):
    log = tmp_path / "x.log"
    r = run_cli(str(log), "e", "event=forged", "session=forged")
    assert r.returncode == 0
    (line,) = read_lines(log)
    assert "event_=forged" in line and "session_=forged" in line
    assert line.split(" | ")[1] == "event=e"


def test_cli_malformed_fields_deterministic(tmp_path):
    log = tmp_path / "x.log"
    r = run_cli(str(log), "e", "noequals", "=value", "dup=1", "dup=2")
    assert r.returncode == 0
    (line,) = read_lines(log)
    assert "noequals=" in line          # no '=' → empty value
    assert "invalid=value" in line      # empty key → 'invalid'
    assert line.count("dup=") == 1 and "dup=2" in line  # last wins


def test_cli_exits_zero_even_on_unwritable_path(tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    r = run_cli(str(blocker / "sub" / "x.log"), "e")
    assert r.returncode == 0
    assert "could not write" in r.stderr


def test_writes_utf8_under_c_locale(tmp_path):
    # launchd/detached processes often run with LC_ALL=C (ASCII default encoding) —
    # the truncation ellipsis must not raise UnicodeEncodeError (Copilot #323).
    log = tmp_path / "x.log"
    env = test_env()
    env.update({"LC_ALL": "C", "LANG": "C"})
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    r = subprocess.run(
        [sys.executable, str(HELPER), str(log), "e", "v=" + "x" * 600],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0
    assert "could not write" not in r.stderr
    val = log.read_text(encoding="utf-8").strip().split("v=", 1)[1]
    assert len(val) == 500 and val.endswith("…")


# ── cdp.py migration (subprocess, matching test_cdp.py convention) ──

CDP_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "look" / "scripts"


def run_cdp_log(tmp_log, snippet, extra_env=None):
    env = test_env()
    env.pop("CDP_PORT", None)
    env["BULLDOZER_LOOK_LOG"] = str(tmp_log)
    env["CLAUDE_CODE_SESSION_ID"] = "cafebabe99"
    env.update(extra_env or {})
    code = "import sys; sys.path.insert(0, {!r}); import cdp; {}".format(
        str(CDP_SCRIPTS_DIR), snippet
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )


def test_cdp_log_canonical_grammar_with_port(tmp_path):
    log = tmp_path / "look.log"
    r = run_cdp_log(log, "cdp.log('probe', ok='yes')")
    assert r.returncode == 0, r.stderr
    (line,) = read_lines(log)
    assert re.fullmatch(
        TS_RE + r" \| event=probe \| session=cafebabe \| port=9333 \| ok=yes", line
    ), line


def test_cdp_log_includes_target_when_set(tmp_path):
    log = tmp_path / "look.log"
    r = run_cdp_log(log, "cdp.TARGET='ab12cd34ef56'; cdp.log('click', sel='a')")
    assert r.returncode == 0, r.stderr
    (line,) = read_lines(log)
    assert " | port=9333 | target=ab12cd34ef56 | sel=a" in line


def test_cdp_log_respects_cdp_port_env(tmp_path):
    log = tmp_path / "look.log"
    r = run_cdp_log(log, "cdp.log('probe')", extra_env={"CDP_PORT": "9341"})
    assert r.returncode == 0, r.stderr
    assert " | port=9341" in read_lines(log)[0]


def test_cdp_log_works_through_symlinked_skill_dir(tmp_path):
    # A symlinked look/ dir must not break helper resolution (codex review #323 r3):
    # abspath keeps the symlink path → parents[3] points outside the plugin → import
    # fails → every line silently dropped. realpath-resolve before walking up.
    log = tmp_path / "look.log"
    link = tmp_path / "look-linked"
    link.symlink_to(CDP_SCRIPTS_DIR.parent)  # symlink to skills/look/
    env = test_env()
    env.pop("CDP_PORT", None)
    env["BULLDOZER_LOOK_LOG"] = str(log)
    env["CLAUDE_CODE_SESSION_ID"] = "cafebabe99"
    code = "import sys; sys.path.insert(0, {!r}); import cdp; cdp.log('probe')".format(
        str(link / "scripts")
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0, r.stderr
    assert log.exists() and "event=probe" in read_lines(log)[0], r.stderr


def test_cdp_survives_broken_helper_module(tmp_path):
    # A helper that fails at import with anything (SyntaxError, not just missing
    # module) must not crash cdp.py — fail-open for the tool, drop the log line
    # (Copilot #323). Fake plugin layout: real cdp.py copy + broken lib helper.
    root = tmp_path / "plug"
    scripts = root / "skills" / "look" / "scripts"
    scripts.mkdir(parents=True)
    (root / "lib").mkdir()
    (scripts / "cdp.py").write_bytes((CDP_SCRIPTS_DIR / "cdp.py").read_bytes())
    (root / "lib" / "bulldozer_log.py").write_text("def broken(:\n")  # SyntaxError
    env = test_env()
    env.pop("CDP_PORT", None)
    env["BULLDOZER_LOOK_LOG"] = str(tmp_path / "x.log")
    code = "import sys; sys.path.insert(0, {!r}); import cdp; cdp.log('probe')".format(
        str(scripts)
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert r.returncode == 0, r.stderr
    assert "log line dropped" in r.stderr
    assert not (tmp_path / "x.log").exists()


def test_cdp_log_sanitizes_multiline_js(tmp_path):
    # the exact defect class from the live log: 696 unparseable continuation lines
    log = tmp_path / "look.log"
    r = run_cdp_log(log, "cdp.log('js', expr='typeof DATA\\n + x | y')")
    assert r.returncode == 0, r.stderr
    lines = read_lines(log)
    assert len(lines) == 1
    assert "expr=typeof DATA  + x / y" in lines[0]


# ── opt-in URL redaction helpers (#334) ──


class TestRedactUrl:
    def test_query_dropped_with_marker(self):
        assert bulldozer_log.redact_url(
            "https://x.test/api?token=SECRET") == "https://x.test/api?<redacted>"

    def test_userinfo_stripped(self):
        out = bulldozer_log.redact_url("https://user:pass@x.test/a")
        assert "user:pass" not in out and out.startswith("https://x.test/a")

    def test_userinfo_only_url_carries_marker(self):
        # codex_review r1 P2: stripped userinfo must be AUDIT-VISIBLE — without
        # the marker the output is indistinguishable from an originally clean URL.
        assert bulldozer_log.redact_url(
            "https://user:pass@x.test/a") == "https://x.test/a?<redacted>"

    def test_fragment_dropped(self):
        assert bulldozer_log.redact_url("https://x.test/a#frag") == "https://x.test/a?<redacted>"

    def test_plain_url_survives(self):
        assert bulldozer_log.redact_url("https://x.test/path/deep") == "https://x.test/path/deep"

    def test_marker_survives_long_url(self):
        out = bulldozer_log.redact_url("https://x.test/" + "p" * 300 + "?t=s")
        assert out.endswith("?<redacted>") and len(out) <= 120


class TestRedactUrlPayloadSchemes:
    """R1-F1: cdp parity — non-location schemes carry PAYLOAD in the path;
    they must hash-redact, never survive as 'path'."""

    def test_data_uri_is_hash_redacted(self):
        out = bulldozer_log.redact_url("data:text/html;base64,U0VDUkVU")
        assert "U0VDUkVU" not in out and out.startswith("data:<redacted:len=")

    def test_javascript_uri_is_hash_redacted(self):
        out = bulldozer_log.redact_url("javascript:fetch('/steal?t=SECRET')")
        assert "SECRET" not in out and out.startswith("javascript:<redacted:")

    def test_unknown_scheme_with_slashes_is_hash_redacted(self):
        out = bulldozer_log.redact_url("myapp://host/p?t=SECRET")
        assert "SECRET" not in out and out.startswith("myapp:<redacted:")

    def test_location_schemes_keep_origin_path(self):
        for u in ("https://x.test/a", "file:///tmp/x", "wss://x.test/s"):
            assert bulldozer_log.redact_url(u) == u


class TestRedactUrlsInText:
    def test_url_in_prose(self):
        out = bulldozer_log.redact_urls_in_text(
            "failed https://u:p@x.test/api?t=SECRET#f then died")
        assert "SECRET" not in out and "u:p" not in out
        assert "then died" in out and "?<redacted>" in out

    def test_multiple_urls(self):
        out = bulldozer_log.redact_urls_in_text(
            "a https://a.test/x?q=1 b http://b.test/y?q=2 c")
        assert out.count("?<redacted>") == 2 and "q=1" not in out and "q=2" not in out

    def test_pipe_delimiter_not_consumed(self):
        out = bulldozer_log.redact_urls_in_text("u=https://x.test/p?q=1 | k=2")
        assert out.endswith("| k=2") and "q=1" not in out

    def test_text_without_urls_unchanged(self):
        assert bulldozer_log.redact_urls_in_text("plain error text") == "plain error text"

    def test_non_string_coerced(self):
        assert bulldozer_log.redact_urls_in_text(None) == "None"

    def test_data_uri_in_prose_redacted(self):
        out = bulldozer_log.redact_urls_in_text(
            "codex sent data:text/plain;base64,U0VDUkVU to renderer")
        assert "U0VDUkVU" not in out and "to renderer" in out

    def test_javascript_uri_in_prose_redacted(self):
        out = bulldozer_log.redact_urls_in_text(
            "blocked javascript:alert(document.cookie) inline")
        assert "document.cookie" not in out and "inline" in out

    def test_bare_word_colon_not_matched(self):
        # false-positive guard: sha256:, error:, time= values must survive intact
        s = "sha256:abcdef error: boom time=1.5s"
        assert bulldozer_log.redact_urls_in_text(s) == s

    def test_embedded_scheme_inside_word_not_matched(self):
        # codex_review r1 P2: 'metadata:SECRET' must NOT trip the data: arm
        # mid-token — payload schemes anchor to a token START.
        s = "metadata:SECRET olddata:v1 xjavascript:noop stays"
        assert bulldozer_log.redact_urls_in_text(s) == s

    def test_underscore_identifier_not_matched(self):
        # codex_review r2 P2: '_' is a word char in identifiers — foo_data: must
        # not trip the data: arm either.
        s = "foo_data:SECRET x_javascript:noop stays"
        assert bulldozer_log.redact_urls_in_text(s) == s

    def test_match_after_real_separators_still_works(self):
        # the boundary guard must allow matches after (, =, quotes — real prose
        out = bulldozer_log.redact_urls_in_text(
            "url=(data:1,SECRET) q='https://u:p@x.test/?t=S'")
        assert "SECRET" not in out and "u:p" not in out and "t=S'" not in out

    def test_payload_scheme_at_token_start_still_redacts(self):
        # the boundary guard must not weaken real matches (start, after space)
        out = bulldozer_log.redact_urls_in_text("data:text/plain;base64,U0VDUkVU x data:1,S y")
        assert "U0VDUkVU" not in out and out.count("<redacted:") == 2

    def test_blob_wrapper_scheme_whole_token_redacted(self):
        # R1-F1 r2: the WHOLE blob: token must redact — the generic arm must not
        # grab the inner https:// and keep SECRET_BLOB_ID as 'path'
        out = bulldozer_log.redact_urls_in_text(
            "leak blob:https://x.test/SECRET_BLOB_ID here")
        assert "SECRET_BLOB_ID" not in out and "here" in out

    def test_view_source_wrapper_scheme_redacted(self):
        out = bulldozer_log.redact_urls_in_text(
            "via view-source:https://x.test/p?t=SECRET end")
        assert "SECRET" not in out and "end" in out

    def test_scheme_matching_is_case_insensitive(self):
        out = bulldozer_log.redact_urls_in_text("sent DATA:text/plain;base64,U0VDUkVU out")
        assert "U0VDUkVU" not in out and "out" in out

    def test_filesystem_wrapper_scheme_redacted(self):
        out = bulldozer_log.redact_urls_in_text(
            "got filesystem:https://x.test/persistent/SECRET end")
        assert "SECRET" not in out and "end" in out

    def test_percent_encoded_data_uri_captured_whole(self):
        # RFC-valid data: URIs %-encode whitespace — captured to the token end
        out = bulldozer_log.redact_urls_in_text("saw data:text/plain,a%20SECRET%20b tail")
        assert "SECRET" not in out and "tail" in out

    def test_failure_returns_placeholder_never_raw(self, monkeypatch):
        class Boom:
            def sub(self, *a, **k):
                raise RuntimeError("boom")
        monkeypatch.setattr(bulldozer_log, "_URL_RE", Boom())
        out = bulldozer_log.redact_urls_in_text("secret https://x.test/?t=s")
        assert "t=s" not in out and out.startswith("<redaction-failed")
