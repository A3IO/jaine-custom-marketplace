"""analyze_media must degrade gracefully on real Gemini failures (grab #6:
free-tier 429/503 happen; an invalid model 404s). The MCP tool should return a
structured {success: false, error} — never crash the tool with an exception.

The network boundary (upload + generate) is mocked; everything else is real.
"""
import json

import pytest

import server
from agent.gemini_files import FileRef


@pytest.fixture
def local_video(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"not-a-real-video")

    async def _hash(_p):
        return "deadbeefdeadbeef"

    async def _upload(*_a, **_k):
        return FileRef(uri="files/u", name="files/u", mime_type="video/mp4",
                       expires_at=0.0, state="ACTIVE")

    monkeypatch.setattr(server.gemini_files, "compute_file_hash", _hash)
    monkeypatch.setattr(server.gemini_files, "get_or_upload", _upload)
    return f


async def test_gemini_http_error_returns_structured_error(local_video, monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("Gemini HTTP 404: model no longer available")

    monkeypatch.setattr(server, "_generate", _boom)
    d = json.loads(await server.analyze_media(str(local_video), "q"))

    assert d["success"] is False
    assert "404" in d["error"]


async def test_upload_failure_returns_structured_error(local_video, monkeypatch):
    async def _boom(*_a, **_k):
        raise server.gemini_files.GeminiFilesError("Files API upload failed: HTTP 503")

    monkeypatch.setattr(server.gemini_files, "get_or_upload", _boom)
    d = json.loads(await server.analyze_media(str(local_video), "q"))

    assert d["success"] is False
    assert "503" in d["error"]


async def test_missing_file_returns_structured_error(local_video, monkeypatch):
    # review #1/#2: a file deleted between validation and hashing (TOCTOU, or a
    # vanished history-turn path) raises OSError, NOT RuntimeError. The contract is
    # "never crash" — must still return {success:false}, not propagate to FastMCP.
    async def _gone(*_a, **_k):
        raise FileNotFoundError("clip.mp4 vanished")

    monkeypatch.setattr(server.gemini_files, "compute_file_hash", _gone)
    d = json.loads(await server.analyze_media(str(local_video), "q"))
    assert d["success"] is False


async def test_httpx_timeout_returns_structured_error(local_video, monkeypatch):
    # review #3: a slow/throttled Gemini raises httpx.ReadTimeout (an httpx.HTTPError,
    # NOT a RuntimeError) — the old except (RuntimeError, GeminiFilesError) let it escape.
    import httpx

    async def _timeout(*_a, **_k):
        raise httpx.ReadTimeout("Gemini took too long")

    monkeypatch.setattr(server, "_generate", _timeout)
    d = json.loads(await server.analyze_media(str(local_video), "q"))
    assert d["success"] is False


async def test_fetch_media_oserror_returns_structured_error(tmp_path, monkeypatch):
    # review #4: fetch_media had try/finally with NO except — an OSError from a full
    # disk (compute_file_hash / mkdir / shutil.move) crashed the tool. Must be structured.
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.fetch, "validate_url", lambda u: None)
    monkeypatch.setattr(server.media, "has_tool", lambda n: True)
    dl = tmp_path / "dl.mp4"
    dl.write_bytes(b"x")
    monkeypatch.setattr(server.fetch, "download", lambda *a, **k: dl)

    async def _gone(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(server.gemini_files, "compute_file_hash", _gone)
    d = json.loads(await server.fetch_media("https://example.com/v.mp4"))
    assert d["success"] is False


async def test_cached_before_reflects_actual_upload_result(local_video, monkeypatch):
    # review #15: cached_before must come from the upload result (set AFTER the cache's
    # ACTIVE-verify), not a pre-check that reports a hit even when the entry was dead and
    # re-uploaded. A FileRef served from cache carries cached=True.
    async def _cached(*_a, **_k):
        return FileRef(uri="files/u", name="files/u", mime_type="video/mp4",
                       expires_at=0.0, state="ACTIVE", cached=True)

    async def _ok(*_a, **_k):
        return ("answer", 0, "STOP")

    monkeypatch.setattr(server.gemini_files, "get_or_upload", _cached)
    monkeypatch.setattr(server, "_generate", _ok)
    d = json.loads(await server.analyze_media(str(local_video), "q"))
    assert d["cached_before"] is True


async def test_truncated_answer_flagged_not_silent(local_video, monkeypatch):
    # the dogfood bug: Gemini stops mid-sentence (MAX_TOKENS / SAFETY) but we
    # used to return success with no warning. Now: success + complete=False + note.
    async def _truncated(*_a, **_k):
        return ("Фигура на заднем", 0, "MAX_TOKENS")

    monkeypatch.setattr(server, "_generate", _truncated)
    d = json.loads(await server.analyze_media(str(local_video), "q"))

    assert d["success"] is True            # we still return the partial text
    assert d["complete"] is False
    assert d["finish_reason"] == "MAX_TOKENS"
    assert "truncat" in d["note"].lower()  # actionable hint surfaced


async def test_result_carries_thinking_and_answer_token_split(local_video, monkeypatch):
    # Grok's observability point + structured diag: expose thinking vs visible-answer
    # tokens so a starved answer is diagnosable, and the note names thinking as the cause.
    async def _gen(*_a, **_k):
        return ("partial", 0, "MAX_TOKENS", 460, 5)   # text, audio, finish, thought, cand

    monkeypatch.setattr(server, "_generate", _gen)
    d = json.loads(await server.analyze_media(str(local_video), "q"))

    assert d["thinking_tokens"] == 460
    assert d["answer_tokens"] == 5
    assert "thinking" in d["note"].lower()  # thought >> cand → blame thinking, not a long answer


async def test_long_answer_framed_and_full_dropped_to_file(local_video, monkeypatch):
    # Chris: the model may think/answer freely, but the VISIBLE reply must stay framed so
    # it can't blow Claude Code's context. The full text is preserved in a workspace file.
    from pathlib import Path

    long = "ответ. " * 1000                    # ~7000 chars, well over brief's 2000-char cap
    async def _gen(*_a, **_k):
        return (long, 0, "STOP", 50, 1500)

    monkeypatch.setattr(server, "_generate", _gen)
    d = json.loads(await server.analyze_media(str(local_video), "q", detail="brief"))

    assert d["truncated"] is True
    assert len(d["analysis"]) < len(long)      # visible answer is capped
    assert "full_answer_file" in d
    assert Path(d["full_answer_file"]).read_text() == long   # nothing lost — full text on disk


async def test_short_answer_not_framed_no_file(local_video, monkeypatch):
    async def _gen(*_a, **_k):
        return ("краткий ответ", 0, "STOP", 0, 5)

    monkeypatch.setattr(server, "_generate", _gen)
    d = json.loads(await server.analyze_media(str(local_video), "q", detail="brief"))

    assert d["analysis"] == "краткий ответ"     # whole answer, untouched
    assert "truncated" not in d
    assert "full_answer_file" not in d


async def test_clean_answer_has_no_warning_note(local_video, monkeypatch):
    async def _ok(*_a, **_k):
        return ("a full clean answer", 1440, "STOP")

    monkeypatch.setattr(server, "_generate", _ok)
    d = json.loads(await server.analyze_media(str(local_video), "q"))

    assert d["complete"] is True
    assert d["finish_reason"] == "STOP"
    assert "note" not in d                 # clean success stays quiet
    assert d["file"] == str(local_video)   # single-file shape preserved (back-compat)
    assert "fileUri" in d


async def test_workspace_failure_does_not_sink_successful_analysis(local_video, monkeypatch):
    # re-review nit (PR #209): analyze_media's workspace.prepare sits on the POST-success
    # path, OUTSIDE the try/except. workspace_dir().mkdir raising OSError (full / read-only /
    # permission-denied data-fs) escapes to FastMCP AFTER a paid, successful Gemini call —
    # breaking the never-crash contract AND discarding a good answer. Unlike extract_frame/
    # prepare_media (which NEED their workspace dir), here the source symlink is cosmetic, so
    # a failure must be best-effort: drop the workspace key, still return the analysis.
    async def _ok(*_a, **_k):
        return ("a clean answer", 0, "STOP")

    def _boom(*_a, **_k):
        raise OSError("Read-only file system")

    monkeypatch.setattr(server, "_generate", _ok)
    monkeypatch.setattr(server.workspace, "prepare", _boom)
    d = json.loads(await server.analyze_media(str(local_video), "q"))

    assert d["success"] is True            # the (paid) analysis stands
    assert d["analysis"] == "a clean answer"
    assert "workspace" not in d            # cosmetic step skipped, not fatal


async def test_extract_frame_nonfinite_window_does_not_crash(local_video):
    # codex security round-2 (NaN/inf finding) — confirmed MORE severe than "minor".
    # validate_frame_request runs at server.py:416, OUTSIDE extract_frame's try (starts 421).
    # window=NaN → int((2*NaN)/step) raises ValueError; window=inf → OverflowError. BOTH escape
    # to FastMCP = never-crash break. The validate layer must reject non-finite input cleanly.
    for bad in (float("nan"), float("inf")):
        d = json.loads(await server.extract_frame(str(local_video), timecode=10.0, window=bad))
        assert d["success"] is False
        # "finite number" (with the word) — NOT bare "finite", which the tmp_path leaks
        # (the test dir name contains "nonfinite"), a false-positive that would mask the bug.
        assert "finite number" in d["error"].lower()


async def test_prepare_media_nonfinite_rejected_at_validate(local_video):
    # same finding, prepare side: start=NaN slips through validate_prepare_request (NaN>=end is
    # False) and reaches ffmpeg as `-ss nan`. Must be rejected at the validate layer instead.
    d = json.loads(await server.prepare_media(str(local_video), start=float("nan"), end=10.0))
    assert d["success"] is False
    assert "ffprobe" not in d["error"].lower()      # rejected at validate, never reached ffprobe
    assert "finite number" in d["error"].lower()


async def test_multi_paths_compared_in_one_request(local_video, monkeypatch, tmp_path):
    # #202: paths=[a,b] → one Gemini request, multi-file result shape
    second = tmp_path / "clip2.mp4"
    second.write_bytes(b"another-clip")

    captured = {}

    async def _capture(_key, refs, *_a, **_k):
        captured["n_refs"] = len(refs)     # both clips sent in ONE call
        return ("left clip jitters, right is smooth", 0, "STOP")

    monkeypatch.setattr(server, "_generate", _capture)
    d = json.loads(await server.analyze_media(
        question="compare left vs right", paths=[str(local_video), str(second)]))

    assert d["success"] is True
    assert captured["n_refs"] == 2         # one request, two file-parts
    assert d["n_inputs"] == 2
    assert len(d["inputs"]) == 2
    assert "file" not in d                 # multi uses inputs, not single file


async def test_no_file_given_is_structured_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    d = json.loads(await server.analyze_media(question="q"))
    assert d["success"] is False
    assert "path" in d["error"].lower()


async def test_list_models_returns_catalog(monkeypatch):
    # #202: a tool so the consumer sees model options instead of guessing (404s)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    async def _fake(_key):
        return [{"id": "gemini-2.5-flash", "preview": False}]

    monkeypatch.setattr(server, "_list_models", _fake)
    d = json.loads(await server.list_models())
    assert d["success"] is True
    assert d["models"][0]["id"] == "gemini-2.5-flash"


async def test_invalid_model_error_lists_available(local_video, monkeypatch):
    # #202 alt: a bad model 404s — surface the available list in the error
    async def _boom(*_a, **_k):
        raise RuntimeError("Gemini HTTP 404: model gemini-bogus is not available")

    async def _fake(_key):
        return [{"id": "gemini-2.5-flash"}, {"id": "gemini-3.5-flash"}]

    monkeypatch.setattr(server, "_generate", _boom)
    monkeypatch.setattr(server, "_list_models", _fake)
    d = json.loads(await server.analyze_media(str(local_video), "q", model="gemini-bogus"))

    assert d["success"] is False
    assert "gemini-2.5-flash" in d["available_models"]


async def test_history_paths_resolved_and_passed_to_generate(local_video, monkeypatch):
    # #206: caller passes prior turns; server resolves their paths to refs and
    # threads the history into _generate (multi-turn, stateless).
    captured = {}

    async def _cap(_key, _refs, _q, *, history=None, **_k):
        captured["history"] = history
        return ("ответ", 0, "STOP")

    monkeypatch.setattr(server, "_generate", _cap)
    hist = [{"role": "user", "text": "опиши", "paths": [str(local_video)]},
            {"role": "model", "text": "кот"}]
    d = json.loads(await server.analyze_media(str(local_video), "а звук?", history=hist))

    assert d["success"] is True
    assert len(captured["history"]) == 2
    assert captured["history"][0]["role"] == "user"
    assert "refs" in captured["history"][0]          # paths → resolved FileRefs
    assert captured["history"][1]["role"] == "model"
    assert "refs" not in captured["history"][1]       # model turn: text only


async def test_followup_text_only_uses_history_no_new_file(local_video, monkeypatch):
    # #206 core: dozadat' a question with NO new video — the media is in history.
    async def _ok(_key, _refs, _q, *, history=None, **_k):
        return ("русский", 0, "STOP")

    monkeypatch.setattr(server, "_generate", _ok)
    hist = [{"role": "user", "text": "что слышно?", "paths": [str(local_video)]},
            {"role": "model", "text": "Привет мир"}]
    d = json.loads(await server.analyze_media(question="на каком языке?", history=hist))

    assert d["success"] is True            # no path/paths, but history present → allowed


async def test_no_file_and_no_history_is_error(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    d = json.loads(await server.analyze_media(question="q"))
    assert d["success"] is False
    assert "history" in d["error"].lower() or "path" in d["error"].lower()


async def test_analyze_does_not_steer_gemini_answer_length(local_video, monkeypatch):
    # review #223 finding: the soft "aim for at most N characters" steer actually shortens
    # Gemini's output (confirmed live), so the full answer dropped to disk is itself truncated
    # — defeating "think freely, frame client-side". The Gemini request must carry NO length
    # steer; the cap is applied ONLY client-side in _frame_answer.
    import httpx

    captured = {}

    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                    "finishReason": "STOP"}]}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, _url, **kw):
            captured["body"] = kw.get("json")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await server.analyze_media(str(local_video), "транскрибируй дословно", answer_chars=500)

    sent = captured["body"]["contents"][-1]["parts"][-1]["text"].lower()
    assert "concise" not in sent           # no conciseness steer
    assert "characters" not in sent        # no "aim for at most N characters" steer


async def test_followup_truncated_drops_full_answer_file(local_video, monkeypatch):
    # sister-review edge: a follow-up via history has NO media anchor (targets=[]), yet a long
    # answer is still framed and the marker promises full_answer_file. The file MUST be written
    # (keyed on the question hash) — otherwise the marker lies AND the full answer is lost.
    from pathlib import Path

    long = "ответ. " * 1000
    async def _gen(_key, _refs, _q, *, history=None, **_k):
        return (long, 0, "STOP", 50, 1500)

    monkeypatch.setattr(server, "_generate", _gen)
    hist = [{"role": "user", "text": "опиши", "paths": [str(local_video)]},
            {"role": "model", "text": "кот"}]
    d = json.loads(await server.analyze_media(question="подробнее?", history=hist, detail="brief"))

    assert d["success"] is True
    assert d["truncated"] is True
    assert "full_answer_file" in d                          # follow-up still preserves the full text
    assert Path(d["full_answer_file"]).read_text() == long  # nothing lost


async def test_truncated_with_failed_save_has_honest_marker(local_video, monkeypatch):
    # sister-review edge path 2: truncated, but the workspace write fails (data-fs OSError).
    # The marker must NOT promise a full_answer_file that was never written — and must not crash.
    long = "ответ. " * 1000
    async def _gen(*_a, **_k):
        return (long, 0, "STOP", 50, 1500)

    def _boom(*_a, **_k):
        raise OSError("Read-only file system")

    monkeypatch.setattr(server, "_generate", _gen)
    monkeypatch.setattr(server.workspace, "prepare", _boom)
    monkeypatch.setattr(server.agent_paths, "workspace_dir", _boom)
    d = json.loads(await server.analyze_media(str(local_video), "q", detail="brief"))

    assert d["success"] is True                       # the (paid) analysis still stands
    assert d["truncated"] is True                      # we DID frame the visible answer
    assert "full_answer_file" not in d                 # nothing written
    assert "full_answer_file" not in d["analysis"]     # marker must not promise the missing file


async def test_full_answer_file_written_as_utf8(local_video, monkeypatch):
    # review #223: full_file.write_text(answer) used the platform default encoding — on a
    # non-UTF-8 locale (minimal Linux/Docker) a Cyrillic answer raises UnicodeEncodeError,
    # swallowed → the full answer is lost. The write MUST pin encoding='utf-8'.
    import pathlib

    calls = []
    real = pathlib.Path.write_text

    def spy(self, data, *a, **k):
        calls.append(k)
        return real(self, data, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", spy)
    long = "ответ. " * 1000
    async def _gen(*_a, **_k):
        return (long, 0, "STOP", 50, 1500)

    monkeypatch.setattr(server, "_generate", _gen)
    await server.analyze_media(str(local_video), "q", detail="brief")

    assert any(c.get("encoding") == "utf-8" for c in calls)   # the full-answer write pins utf-8


async def test_followup_full_answer_file_distinct_per_conversation(local_video, monkeypatch):
    # review #223: a follow-up's full_answer_file was keyed ONLY on the question text, so two
    # different conversations asking the same question ("подробнее?") overwrote each other.
    # The key must include the conversation (history) so they don't collide.
    long = "ответ. " * 1000
    async def _gen(_key, _refs, _q, **_k):
        return (long, 0, "STOP", 50, 1500)

    monkeypatch.setattr(server, "_generate", _gen)
    histA = [{"role": "user", "text": "видео A", "paths": [str(local_video)]},
             {"role": "model", "text": "A"}]
    histB = [{"role": "user", "text": "видео B", "paths": [str(local_video)]},
             {"role": "model", "text": "B"}]
    a = json.loads(await server.analyze_media(question="подробнее?", history=histA, detail="brief"))
    b = json.loads(await server.analyze_media(question="подробнее?", history=histB, detail="brief"))

    assert a["full_answer_file"] != b["full_answer_file"]   # distinct conversations don't collide


async def test_fetch_media_replace_failure_cleans_staged(tmp_path, monkeypatch):
    # review #223: if shutil.move stages the .incoming file but os.replace then fails (fs went
    # read-only mid-op), the staged file was orphaned in the workspace. It must be cleaned up.
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("JAINE_MEDIA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(server.fetch, "validate_url", lambda u: None)
    monkeypatch.setattr(server.media, "has_tool", lambda n: True)
    dl = tmp_path / "dl.mp4"
    dl.write_bytes(b"x")
    monkeypatch.setattr(server.fetch, "download", lambda *a, **k: dl)

    async def _hash(*_a, **_k):
        return "deadbeefdeadbeef"

    monkeypatch.setattr(server.gemini_files, "compute_file_hash", _hash)

    def _replace_boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(server.os, "replace", _replace_boom)
    d = json.loads(await server.fetch_media("https://example.com/v.mp4"))

    assert d["success"] is False
    ws = server.agent_paths.workspace_dir("deadbeef")
    assert list(ws.glob("*.incoming")) == []      # no orphaned .incoming left behind
