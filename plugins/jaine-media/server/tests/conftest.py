"""Shared test fixtures.

Keep tool-call logging OUT of the real ~/.claude/logs/jaine-media.jsonl: any test
that exercises a tool calls tool_log.log_tool, which would otherwise append to the
user's live dogfood log. This autouse fixture redirects it to a per-test tmp file
(tests that need to assert on the log, like test_tool_log, repoint it themselves)."""
import pytest

from agent import tool_log


@pytest.fixture(autouse=True)
def _isolate_tool_log(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_LOG", str(tmp_path / "jaine-media.jsonl"))
    tool_log._reset()
    yield
    tool_log._reset()
