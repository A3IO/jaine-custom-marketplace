"""Configurable model selection (Chris's call: env-driven, single source).

No hardcoded model matrix scattered across tools: defaults live in one constant,
each role reads its env var, an explicit per-call override wins. New models drop
in via env with no code change.
"""
import server


def test_analyze_default_is_audio_capable_flash(monkeypatch):
    monkeypatch.delenv("JAINE_MEDIA_MODEL", raising=False)
    assert server._model_for("analyze") == "gemini-2.5-flash"


def test_locate_default_is_stable_flash(monkeypatch):
    # eval: timecode accuracy is ~uniform across models; default is a stable pick,
    # never a preview model (refined for extract_frame in Phase 2)
    monkeypatch.delenv("JAINE_MEDIA_LOCATE_MODEL", raising=False)
    assert server._model_for("locate") == "gemini-2.5-flash"


def test_env_overrides_default(monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_MODEL", "gemini-9-future")
    assert server._model_for("analyze") == "gemini-9-future"


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv("JAINE_MEDIA_MODEL", "gemini-from-env")
    assert server._model_for("analyze", override="gemini-explicit") == "gemini-explicit"
