"""Per-media working folder: one dir per content hash holding a symlink to the
source (frames land here later for extract_frame). Tool-call logging is central
(agent/tool_log.py), not per-workspace."""
import pytest

from agent import paths, workspace


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    return tmp_path


def test_workspace_dir_is_sha8_under_data_dir(data_dir):
    ws = paths.workspace_dir("abcdef1234567890deadbeef")
    assert ws == data_dir / "workspace" / "abcdef12"
    assert ws.is_dir()


def test_prepare_symlinks_source_with_lowercased_ext(data_dir, tmp_path):
    src = tmp_path / "Спор трёх людей.MP4"   # cyrillic + spaces + uppercase ext
    src.write_bytes(b"video-bytes")
    ws = workspace.prepare("abcdef1234567890", src)
    link = ws / "source.mp4"
    assert link.is_symlink()
    assert link.resolve() == src.resolve()
