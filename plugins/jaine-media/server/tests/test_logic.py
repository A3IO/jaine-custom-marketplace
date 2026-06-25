"""Pure decision helpers for analyze_media — tested without network.

The thin httpx/MCP shell is verified live (Phase 1.5); everything that decides
*what* to send and *how* to read the reply is pulled out here so it has teeth.
"""
import pytest

import server
from agent.gemini_files import FileRef


# --- _effective_max_tokens (detail → maxOutputTokens, with override) ---

def test_detail_brief_maps_to_512():
    assert server._effective_max_tokens("brief") == 512


def test_detail_normal_maps_to_2048():
    assert server._effective_max_tokens("normal") == 2048


def test_detail_full_maps_to_8192():
    assert server._effective_max_tokens("full") == 8192


def test_max_tokens_override_beats_detail():
    assert server._effective_max_tokens("brief", max_tokens=5000) == 5000


def test_unknown_detail_falls_back_to_normal():
    assert server._effective_max_tokens("verbose") == 2048


# --- _language_instruction (Chris: default = question language; protect transcripts) ---

def test_default_language_instruction_targets_question_language():
    instr = server._language_instruction(None)
    assert "same language as the question" in instr.lower()


def test_default_language_instruction_protects_transcripts():
    # the carve-out Chris flagged: a verbatim English transcript must not get
    # translated just because the question was asked in Russian
    assert "transcrib" in server._language_instruction(None).lower()


def test_explicit_language_named_in_instruction():
    assert "russian" in server._language_instruction("Russian").lower()


# --- _build_request_body ---

def _ref():
    return FileRef(uri="files/u", name="files/u", mime_type="video/mp4", expires_at=0.0)


def test_body_wires_effective_max_tokens():
    body = server._build_request_body([_ref()], "what happens?", max_tokens=2048)
    assert body["generationConfig"]["maxOutputTokens"] == 2048


def test_body_uses_high_media_resolution():
    body = server._build_request_body([_ref()], "q", max_tokens=512)
    assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_HIGH"


def test_media_resolution_high_for_2_5_family():
    # #232: on the 2.5 family HIGH == default (same token cost) — keep HIGH, it's free.
    assert server._media_resolution_for("gemini-2.5-flash") == "MEDIA_RESOLUTION_HIGH"
    assert server._media_resolution_for("gemini-2.5-pro") == "MEDIA_RESOLUTION_HIGH"


def test_media_resolution_medium_for_3x_family():
    # #232: on 3.x, HIGH is ~3.4x their cheap default for zero benefit (OCR-only). Default MEDIUM.
    assert server._media_resolution_for("gemini-3.5-flash") == "MEDIA_RESOLUTION_MEDIUM"
    assert server._media_resolution_for("gemini-3.1-pro-preview") == "MEDIA_RESOLUTION_MEDIUM"


def test_media_resolution_unknown_family_keeps_high():
    # unknown/future model → preserve current behavior (HIGH), never silently downgrade quality.
    assert server._media_resolution_for("gemini-9-ultra") == "MEDIA_RESOLUTION_HIGH"


def test_body_respects_media_resolution_override():
    # an explicit media_resolution wins over the default, so OCR-on-3.x stays reachable.
    body = server._build_request_body([_ref()], "q", max_tokens=512,
                                      media_resolution="MEDIA_RESOLUTION_LOW")
    assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"


# --- native YouTube passthrough (#229): the URL IS the fileUri, no upload ---

def test_native_ref_points_at_the_url():
    ref = server._native_ref("https://youtu.be/abc")
    assert ref.uri == "https://youtu.be/abc"
    assert ref.state == "ACTIVE"            # no PROCESSING wait — Gemini ingests it server-side


def test_media_part_native_url_omits_mimetype():
    # #229: a native part is {fileData:{fileUri:url}} with NO mimeType — Gemini ingests the
    # YouTube URL itself; an mimeType on a URL part is wrong.
    part = server._media_part(server._native_ref("https://youtu.be/abc"), None)
    assert part["fileData"]["fileUri"] == "https://youtu.be/abc"
    assert "mimeType" not in part["fileData"]


def test_media_part_uploaded_ref_keeps_mimetype():
    # regression: an UPLOADED file part still carries its mimeType (only native URLs omit it).
    part = server._media_part(_ref(), None)
    assert part["fileData"]["mimeType"] == "video/mp4"


# --- _is_native routing decision (#229): native only for a one-shot single YouTube URL ---

def test_is_native_for_single_youtube_no_history():
    assert server._is_native(["https://youtu.be/abc"], None) is True


def test_is_native_false_with_history():
    # multi-turn re-pulls the native URL every turn → download+reuse instead.
    assert server._is_native(["https://youtu.be/abc"], [{"role": "user", "text": "x"}]) is False


def test_is_native_false_for_non_youtube_url():
    assert server._is_native(["https://vimeo.com/1"], None) is False


def test_is_native_false_for_local_path():
    assert server._is_native(["/x/clip.mp4"], None) is False


def test_is_native_false_for_multiple_inputs():
    # native supports at most ONE YouTube link per request.
    assert server._is_native(["https://youtu.be/a", "https://youtu.be/b"], None) is False


def test_body_omits_fps_by_default():
    part = server._build_request_body([_ref()], "q", max_tokens=512)["contents"][0]["parts"][0]
    assert "videoMetadata" not in part


def test_body_includes_fps_when_given():
    part = server._build_request_body([_ref()], "q", max_tokens=512, fps=5)["contents"][0]["parts"][0]
    assert part["videoMetadata"]["fps"] == 5


def test_body_carries_the_question_text():
    body = server._build_request_body([_ref()], "when does it flash red?", max_tokens=512)
    text = body["contents"][0]["parts"][1]["text"]
    assert "when does it flash red?" in text


def test_body_never_steers_answer_length():
    # #223: _build_request_body must NOT inject any answer-length steer — the model answers
    # freely and the cap is applied client-side in _frame_answer (a soft steer measurably
    # shortened Gemini's output, truncating even the full-text file dropped to disk).
    body = server._build_request_body([_ref()], "опиши видео", max_tokens=2048)
    text = body["contents"][0]["parts"][-1]["text"].lower()
    assert "concise" not in text
    assert "character" not in text


def test_body_multi_refs_make_one_filepart_each():
    # #202: several videos as several file-parts in ONE request (full res each)
    parts = server._build_request_body([_ref(), _ref()], "compare", max_tokens=512)["contents"][0]["parts"]
    assert sum(1 for p in parts if "fileData" in p) == 2
    assert "compare" in parts[-1]["text"]      # one shared question, last


def test_body_fps_applies_to_every_filepart():
    parts = server._build_request_body([_ref(), _ref()], "q", max_tokens=512, fps=5)["contents"][0]["parts"]
    assert all(p["videoMetadata"]["fps"] == 5 for p in parts if "fileData" in p)


# --- multi-turn: caller-passed history → Gemini user/model contents (#206) ---

def test_body_history_replays_prior_turns_then_new_question():
    history = [
        {"role": "user", "text": "опиши видео", "refs": [_ref()]},
        {"role": "model", "text": "на видео кот"},
    ]
    contents = server._build_request_body([_ref()], "а звук?", max_tokens=512, history=history)["contents"]
    assert len(contents) == 3                       # prior user, prior model, new user
    assert contents[0]["role"] == "user"
    assert any("fileData" in p for p in contents[0]["parts"])    # media rode the turn it was in
    assert contents[0]["parts"][-1]["text"] == "опиши видео"     # prior text verbatim
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["text"] == "на видео кот"
    assert contents[2]["role"] == "user"
    assert "а звук?" in contents[2]["parts"][-1]["text"]         # new question last


def test_body_history_model_turn_carries_no_media():
    history = [{"role": "model", "text": "ответ"}]
    model_turn = server._build_request_body([_ref()], "q", max_tokens=512, history=history)["contents"][0]
    assert not any("fileData" in p for p in model_turn["parts"])


def test_body_history_can_add_a_different_video_midconversation():
    # #202 core: video A in turn 0, video B in the NEW turn → both in one request
    a, b = _ref(), _ref()
    history = [{"role": "user", "text": "опиши A", "refs": [a]},
               {"role": "model", "text": "..."}]
    contents = server._build_request_body([b], "сравни с A", max_tokens=512, history=history)["contents"]
    assert any("fileData" in p for p in contents[0]["parts"])    # A in history
    assert any("fileData" in p for p in contents[-1]["parts"])   # B in new turn


def test_body_replays_history_verbatim_without_normalizing():
    # #206/finding-#2 INVARIANT: history should alternate and end on 'model' (we always
    # append the question as a 'user' turn). A malformed history ending on 'user' yields
    # consecutive user turns — we replay VERBATIM, never merge/normalize (that would mutate
    # caller-owned history and mask caller bugs; consult MINOR-FIXES). Gemini tolerates this
    # (empirically HTTP 200, not 400); any degradation is caught downstream by finish_reason
    # (EMPTY/MAX_TOKENS → complete=False), never silenced here.
    history = [{"role": "user", "text": "first question"}]       # ends on user (malformed)
    contents = server._build_request_body([_ref()], "second question",
                                          max_tokens=512, history=history)["contents"]
    assert [c["role"] for c in contents] == ["user", "user"]     # faithful replay, not merged


def test_body_skips_history_turn_with_empty_parts():
    # review #6: a model turn with text='' and no refs → parts=[] → Gemini rejects the
    # whole request with HTTP 400. Skip a content-less turn, don't emit empty parts.
    history = [{"role": "user", "text": "q", "refs": [_ref()]},
               {"role": "model", "text": ""}]            # empty model turn (e.g. EMPTY finish)
    contents = server._build_request_body([_ref()], "next", max_tokens=512, history=history)["contents"]
    assert all(c["parts"] for c in contents)             # no empty parts array survives
    assert len(contents) == 2                            # the empty model turn was dropped, not kept
    assert [c["role"] for c in contents] == ["user", "user"]   # prior user + new user, no empty model


# --- _collect_targets (path | paths → validated existing-file list) ---

def test_collect_single_path(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    assert server._collect_targets(str(f), None) == [f]


def test_collect_paths_list_preserves_order(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    assert server._collect_targets(None, [str(a), str(b)]) == [a, b]


def test_collect_empty_raises():
    with pytest.raises(ValueError):
        server._collect_targets(None, None)


def test_collect_too_many_raises(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    with pytest.raises(ValueError):
        server._collect_targets(None, [str(f)] * 11)      # over the per-request cap


def test_collect_missing_file_raises(tmp_path):
    with pytest.raises(ValueError):
        server._collect_targets(str(tmp_path / "nope.mp4"), None)


# --- _filter_models (#202: catalog of flash/pro generateContent models) ---

def test_filter_keeps_flash_pro_generatecontent_drops_rest():
    raw = [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"],
         "inputTokenLimit": 1048576, "outputTokenLimit": 65536},
        {"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/imagen-3", "supportedGenerationMethods": ["generateContent"]},      # no flash/pro
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}, # no generateContent
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]}, # retired family
    ]
    ids = [m["id"] for m in server._filter_models(raw)]
    assert "gemini-2.5-flash" in ids
    assert "gemini-3.1-pro-preview" in ids
    assert "gemini-2.0-flash" not in ids          # 2.0 retired → skipped
    assert not any("imagen" in i or "embedding" in i for i in ids)


def test_filter_marks_preview_and_limits():
    raw = [{"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"],
            "inputTokenLimit": 1048576, "outputTokenLimit": 65536}]
    m = server._filter_models(raw)[0]
    assert m["preview"] is True
    assert m["input_limit"] == 1048576


def test_filter_sorts_by_id():
    raw = [{"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
           {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]
    ids = [m["id"] for m in server._filter_models(raw)]
    assert ids == sorted(ids)


def test_filter_drops_learned_dead_models():
    # #233 learn-from-404: a catalog entry recorded as dead (404'd on use) is hidden, even
    # though models.list still advertises it (no retired-signal — that's the whole problem).
    raw = [{"name": "models/gemini-3-pro-preview", "supportedGenerationMethods": ["generateContent"]},
           {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]}]
    ids = [m["id"] for m in server._filter_models(raw, dead={"gemini-3-pro-preview"})]
    assert "gemini-3-pro-preview" not in ids
    assert "gemini-2.5-flash" in ids


def test_filter_default_dead_is_empty():
    # back-compat: _filter_models(raw) without `dead` keeps everything it used to.
    raw = [{"name": "models/gemini-3-pro-preview", "supportedGenerationMethods": ["generateContent"]}]
    ids = [m["id"] for m in server._filter_models(raw)]
    assert "gemini-3-pro-preview" in ids


# --- _parse_response (candidates text + AUDIO prompt tokens + finishReason) ---

def test_parse_extracts_text_audio_and_finish_reason():
    d = {
        "candidates": [{"finishReason": "STOP",
                        "content": {"parts": [{"text": "hello "}, {"text": "world"}]}}],
        "usageMetadata": {"promptTokensDetails": [
            {"modality": "VIDEO", "tokenCount": 100},
            {"modality": "AUDIO", "tokenCount": 1440},
        ]},
    }
    text, audio, finish = server._parse_response(d)
    assert text == "hello world"
    assert audio == 1440
    assert finish == "STOP"             # complete answer


def test_parse_flags_truncation_by_max_tokens():
    d = {"candidates": [{"finishReason": "MAX_TOKENS",
                         "content": {"parts": [{"text": "half a sen"}]}}]}
    text, audio, finish = server._parse_response(d)
    assert text == "half a sen"
    assert finish == "MAX_TOKENS"       # the dogfood bug: silently-truncated answer


def test_parse_surfaces_prompt_block_when_no_candidate():
    # whole prompt blocked by SAFETY → no candidate at all, reason in promptFeedback
    d = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
    text, audio, finish = server._parse_response(d)
    assert text == ""
    assert finish == "BLOCKED:SAFETY"   # not a silent empty STOP


def test_parse_missing_finish_reason_defaults_stop():
    d = {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}
    _, _, finish = server._parse_response(d)
    assert finish == "STOP"


def test_parse_handles_empty_response():
    text, audio, finish = server._parse_response({})
    assert text == ""
    assert audio == 0
    assert finish == "EMPTY"            # no candidate, no block reason


def test_parse_flags_empty_text_with_stop_as_empty():
    # #231 dogfood: a thinking model (2.5-pro) returned a candidate with finishReason=STOP but
    # NO text — thinking ate the output budget. A bare STOP reports complete success with an empty
    # analysis (the deceptive case). A present-but-textless STOP candidate must read EMPTY so the
    # caller flags it (complete=False + note), never a silent empty success.
    d = {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}
    text, _, finish = server._parse_response(d)
    assert text == ""
    assert finish == "EMPTY"


def test_parse_flags_whitespace_only_stop_as_empty():
    # whitespace-only parts strip to "" — same deceptive STOP, must surface as EMPTY.
    d = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "  \n "}]}}]}
    text, _, finish = server._parse_response(d)
    assert text == ""
    assert finish == "EMPTY"


def test_parse_keeps_max_tokens_when_text_empty():
    # guard: ONLY STOP+empty is the deceptive case. MAX_TOKENS already signals incompleteness and
    # carries a more specific note — an empty MAX_TOKENS must NOT be flattened to EMPTY.
    d = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]}
    _, _, finish = server._parse_response(d)
    assert finish == "MAX_TOKENS"


# --- _finish_note (structured cause diagnostics — B1/B2, panel-validated) ---

def test_finish_note_blames_thinking_when_it_ate_the_budget():
    # MAX_TOKENS with thought >> cand: thinking consumed the shared output pool
    # (empirically the common case on thinking models — probe + python-genai #2062).
    note = server._finish_note("MAX_TOKENS", thought=460, cand=5)
    assert note is not None
    assert "thinking" in note.lower()


def test_finish_note_plain_truncation_when_the_answer_itself_was_long():
    # cand dominates: the visible answer hit the cap, thinking wasn't the culprit
    note = server._finish_note("MAX_TOKENS", thought=10, cand=500)
    assert note is not None
    assert "thinking" not in note.lower()


def test_finish_note_safety_block_does_not_advise_switching_model():
    # panel consensus + B2: a safety block is content/prompt-driven; switching to pro is futile
    note = server._finish_note("BLOCKED:SAFETY")
    assert note is not None
    assert "pro" not in note.lower()


# --- _usage_tokens (thinking vs visible-answer token split — Grok's observability point) ---

def test_usage_tokens_extracts_thought_and_candidate_counts():
    d = {"usageMetadata": {"thoughtsTokenCount": 460, "candidatesTokenCount": 5}}
    thought, cand = server._usage_tokens(d)
    assert thought == 460
    assert cand == 5


def test_usage_tokens_default_zero_when_absent():
    # 2.5-family without a thinkingConfig may omit thoughtsTokenCount entirely
    thought, cand = server._usage_tokens({})
    assert thought == 0
    assert cand == 0


# --- _frame_answer (client-side visible-answer cap — Chris: think freely, frame the answer) ---

def test_frame_answer_keeps_short_answer_whole():
    visible, truncated = server._frame_answer("short answer", 100)
    assert visible == "short answer"
    assert truncated is False


def test_frame_answer_truncates_over_limit_and_flags():
    visible, truncated = server._frame_answer("a" * 100, 40)
    assert truncated is True
    assert visible.startswith("a" * 40)        # keeps the first `limit` chars verbatim
    assert "jaine-media" in visible.lower()    # marker tells the reader it was cut
    assert "full_answer_file" in visible       # default (saved) marker points at the file


def test_frame_answer_unsaved_marker_does_not_promise_file():
    # when the full text could NOT be saved (data-fs failure), the marker must be honest —
    # it must not point at a full_answer_file that does not exist.
    visible, truncated = server._frame_answer("a" * 100, 40, saved=False)
    assert truncated is True
    assert visible.startswith("a" * 40)
    assert "full_answer_file" not in visible


# --- _answer_char_limit (detail → visible char cap, with override) ---

def test_answer_char_limit_maps_detail():
    assert server._answer_char_limit("brief") == 2000
    assert server._answer_char_limit("normal") == 8000
    assert server._answer_char_limit("full") == 32000


def test_answer_char_limit_override_wins():
    assert server._answer_char_limit("full", 500) == 500


def test_answer_char_limit_unknown_detail_falls_back_to_normal():
    assert server._answer_char_limit("verbose") == 8000
