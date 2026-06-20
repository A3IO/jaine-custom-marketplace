"""Centralized JSONL tool-call log: one parseable line per tool call to a stable
path (env-overridable), stdlib rotation, best-effort (logging NEVER breaks a tool).
Replaces the scattered per-workspace answers.jsonl with one log over all 4 tools."""
import json

import pytest

from agent import tool_log


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    p = tmp_path / "jaine-media.jsonl"
    monkeypatch.setenv("JAINE_MEDIA_LOG", str(p))
    tool_log._reset()                       # drop any logger cached by a prior test
    return p


def test_writes_one_parseable_jsonl_line(log_path):
    tool_log.log_tool("analyze_media", True, digest="abcdef1234567890deadbeef",
                      model="gemini-2.5-flash")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["tool"] == "analyze_media"
    assert rec["ok"] is True
    assert rec["digest"] == "abcdef12"      # truncated to sha8 — correlates with workspace/<sha8>/
    assert rec["model"] == "gemini-2.5-flash"
    assert "ts" in rec                      # envelope always carries a timestamp


def test_appends_one_line_per_call(log_path):
    tool_log.log_tool("extract_frame", True, frame_count=3)
    tool_log.log_tool("prepare_media", False, error="ffmpeg failed")
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["ok"] is False


def test_preserves_cyrillic_pipes_and_newlines(log_path):
    q = "Кто спорит | о чём?"               # pipe — would corrupt pipe-kv
    a = "Собчак и Соловьёв...\nспорят о стиле"   # newline — would be stripped by pipe-kv
    tool_log.log_tool("analyze_media", True, question=q, answer=a)
    rec = json.loads(log_path.read_text().splitlines()[0])
    assert rec["question"] == q            # pipe intact
    assert rec["answer"] == a              # full answer recoverable, newline and all


def test_path_from_env_override(tmp_path, monkeypatch):
    other = tmp_path / "sub" / "custom.jsonl"   # nested dir must be created
    monkeypatch.setenv("JAINE_MEDIA_LOG", str(other))
    tool_log._reset()
    tool_log.log_tool("fetch_media", True, url="https://example.com/v.mp4")
    assert other.is_file()


def test_best_effort_never_raises(monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_LOG", "/dev/null/nope/x.jsonl")   # unwritable
    tool_log._reset()
    tool_log.log_tool("analyze_media", True)    # must NOT raise


def test_rotation_caps_unbounded_growth(log_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_LOG_MAXBYTES", "500")
    tool_log._reset()
    for i in range(50):
        tool_log.log_tool("analyze_media", True, i=i, blob="x" * 50)
    assert log_path.stat().st_size <= 1500          # main file bounded by rotation
    assert (log_path.parent / "jaine-media.jsonl.1").exists()   # a backup was rolled
