"""dead_models — a self-healing skip-list of model ids that returned a retired-model 404.

models.list keeps advertising retired ids (a retired and a working model are byte-identical
in the catalog — no retired-signal), so analyze_media records a dead id here and list_models
hides it next time (#233). Best-effort + fail-open: a read/write error never blocks a tool."""
from agent import dead_models


def test_load_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    assert dead_models.load() == set()


def test_record_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    dead_models.record("gemini-3-pro-preview")
    assert dead_models.load() == {"gemini-3-pro-preview"}


def test_record_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    dead_models.record("gemini-3-pro-preview")
    dead_models.record("gemini-3-pro-preview")
    assert dead_models.load() == {"gemini-3-pro-preview"}


def test_record_accumulates_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    dead_models.record("gemini-3-pro-preview")
    dead_models.record("gemini-9-ghost")
    assert dead_models.load() == {"gemini-3-pro-preview", "gemini-9-ghost"}


def test_record_empty_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    dead_models.record("")
    assert dead_models.load() == set()


def test_load_tolerates_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    (tmp_path / "dead-models.json").write_text("{not json")
    assert dead_models.load() == set()   # fail-open — a corrupt file must never crash a tool


def test_load_tolerates_unhashable_items(tmp_path, monkeypatch):
    # codex P2: valid JSON but with a non-str / unhashable item (e.g. {}) must NOT crash
    # set(raw) with TypeError — keep the str ids, drop the malformed ones (fail-open).
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    (tmp_path / "dead-models.json").write_text('[{}, "gemini-3-pro-preview", 5]')
    assert dead_models.load() == {"gemini-3-pro-preview"}
