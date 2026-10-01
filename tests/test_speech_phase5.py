"""Phase 5 speech: cache, word ranges, transcript tools (mocked ASR)."""

from __future__ import annotations

import json
import os
from fractions import Fraction
from unittest.mock import MagicMock, patch

import pytest

from classes.agent_tools.receipt import parse_receipt
from classes.speech.cache import (
    TranscriptCache,
    TranscriptRecord,
    Word,
    reset_default_cache_for_tests,
)
from classes.speech.map_timeline import words_for_clip
from classes.speech.word_ranges import (
    compact_fragments_after_remove,
    indices_for_filler_preset,
    merge_ranges,
    ranges_for_word_indices,
)


@pytest.fixture
def tmp_cache(tmp_path):
    cache = TranscriptCache(root=str(tmp_path / "transcripts"), memory_cap=4)
    reset_default_cache_for_tests(cache)
    yield cache
    reset_default_cache_for_tests(None)


@pytest.fixture(autouse=True)
def _force_whisper_in_unit_tests():
    """Unit tests inject fakes / seeded whisper caches — don't pick Apple helper."""
    with patch("classes.speech.apple_asr.apple_asr_available", return_value=False):
        yield


def test_cache_roundtrip_and_lru(tmp_cache, tmp_path):
    media = tmp_path / "a.wav"
    media.write_bytes(b"RIFF....")
    rec = TranscriptRecord(
        path=str(media.resolve()),
        size=media.stat().st_size,
        mtimeNs=media.stat().st_mtime_ns,
        modelId="faster-whisper-base",
        language="en",
        requestLanguage="en",
        words=[Word("hello", 0.0, 0.4), Word("um", 0.5, 0.7)],
        generation=1,
    )
    tmp_cache.put(rec)
    hit = tmp_cache.get(str(media), model_id="faster-whisper-base", language="en")
    assert hit is not None
    assert len(hit.words) == 2
    assert hit.words[1].text == "um"

    # Memory cap: insert 5 distinct records → oldest evicted from memory
    # (disk still present for the first).
    for i in range(5):
        p = tmp_path / f"m{i}.wav"
        p.write_bytes(b"x" * (10 + i))
        r = TranscriptRecord(
            path=str(p.resolve()),
            size=p.stat().st_size,
            mtimeNs=p.stat().st_mtime_ns,
            modelId="faster-whisper-base",
            language="en",
            words=[Word(f"w{i}", 0.0, 0.1)],
            generation=1,
        )
        tmp_cache.put(r)
    assert len(tmp_cache._mem) == 4


def test_merge_and_compact_fragments_close_gaps():
    fps = Fraction(30, 1)
    merged = merge_ranges([(1.0, 2.0), (1.5, 2.5), (4.0, 4.2)], fps=fps)
    assert len(merged) == 2
    assert merged[0][0] == pytest.approx(1.0)
    assert merged[0][1] == pytest.approx(2.5)

    frags, removed = compact_fragments_after_remove(
        position=10.0,
        start=0.0,
        end=6.0,
        remove_ranges=[(1.0, 2.0), (4.0, 5.0)],
        fps=fps,
    )
    assert removed == pytest.approx(2.0)
    assert len(frags) == 3
    # Packed back-to-back from position 10
    assert frags[0][0] == pytest.approx(10.0)
    assert frags[0][1:] == pytest.approx((0.0, 1.0))
    assert frags[1][0] == pytest.approx(11.0)
    assert frags[1][1:] == pytest.approx((2.0, 4.0))
    assert frags[2][0] == pytest.approx(13.0)
    assert frags[2][1:] == pytest.approx((5.0, 6.0))


def test_filler_preset_and_word_index_ranges():
    fps = Fraction(30, 1)
    words = [
        {"index": 0, "text": "So", "startSec": 0.0, "endSec": 0.2},
        {"index": 1, "text": "um", "startSec": 0.3, "endSec": 0.5},
        {"index": 2, "text": "hello", "startSec": 0.6, "endSec": 1.0},
        {"index": 3, "text": "uh", "startSec": 1.1, "endSec": 1.3},
    ]
    idxs = indices_for_filler_preset(words, "um_uh")
    assert idxs == [1, 3]
    ranges = ranges_for_word_indices(words, idxs, fps=fps)
    assert len(ranges) == 2


def test_indices_for_matches_is_case_and_punct_insensitive():
    from classes.speech.word_ranges import indices_for_matches

    words = [
        {"index": 0, "text": "built"},
        {"index": 1, "text": "FlowCut."},
        {"index": 2, "text": "FlowCut"},
        {"index": 3, "text": "is"},
        {"index": 4, "text": "flocut"},
    ]
    assert indices_for_matches(words, ["FlowCut", "flocut"]) == [1, 2, 4]
    assert indices_for_matches(words, ["FLOWCUT"]) == [1, 2]
    assert indices_for_matches(words, ["nope"]) == []


def test_words_for_clip_maps_frames():
    fps = Fraction(30, 1)
    words = [Word("hi", 1.0, 1.5), Word("there", 2.0, 2.5)]
    # Clip shows source [1.0, 3.0] at timeline position 5.0
    rows = words_for_clip(
        words,
        clip_id="c1",
        position=5.0,
        start=1.0,
        end=3.0,
        fps=fps,
        generation=2,
    )
    assert len(rows) == 2
    assert rows[0]["index"] == 0
    assert rows[0]["startFrame"] == 5 * 30  # 1.0s into clip at pos 5 → t=5.0
    assert rows[0]["transcriptGeneration"] == 2
    # Word at source 2.0 → timeline 6.0
    assert rows[1]["startFrame"] == 6 * 30


class _FakeTranscriber:
    def __init__(self, model_id: str = ""):
        self.model_id = model_id

    def transcribe(self, wav_path, *, language, token):
        return (
            [Word("hello", 0.0, 0.4), Word("um", 0.5, 0.7), Word("world", 0.8, 1.2)],
            "en",
        )


def test_get_transcript_clip_with_fake_asr(tmp_cache, tmp_path):
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)

        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "c1",
                "file_id": "f1",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)

        def _fake_extract(path, **kw):
            out = tmp_path / "out.wav"
            out.write_bytes(b"data")
            return str(out), ""

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media), "id": "f1"},
             ), \
             patch(
                 "classes.speech.asr.extract_mono_16k_wav",
                 side_effect=_fake_extract,
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ):
            from classes.agent_tools.transcript import get_transcript
            raw = get_transcript(clipId="c1")
        receipt = parse_receipt(raw)
        assert receipt["status"] == "applied"
        data = receipt["data"]
        assert data["transcriptionSource"] == "local"
        assert data["transcriptGeneration"] == 1
        clip = data["clips"][0]
        assert "words" not in clip  # compact by default
        assert "compactWords" not in clip  # matches=[] cuts don't need the array
        assert clip["wordCount"] == 3
        assert "script" in data
        assert "hello" in data["script"] and "world" in data["script"]
        assert "clipIds" in data and data["clipIds"] == ["c1"]
        assert data.get("fileIds") == []
        assert "clipId=c1" in receipt["summary"]
        assert "Transcribed" in receipt["summary"]
        assert "remove_words_tool" in receipt["summary"]
        assert len(receipt["summary"]) < 500  # never dump the full script into summary
        assert '"contract"' not in receipt["summary"]
        assert data["nextAction"] == "remove_words_tool"
    finally:
        asr_mod.reset_transcriber_factory()


def test_file_only_transcript_does_not_label_file_as_clip_id():
    from classes.agent_tools.transcript import (
        _transcript_data_payload,
        _short_transcript_summary,
    )

    summary = _short_transcript_summary(
        word_count=1, source="local", file_ids=["LTQM6B501R"],
    )
    assert "fileId=LTQM6B501R" in summary
    assert "clipId=LTQM6B501R" not in summary
    assert "NOT a timeline" in summary or "not a timeline" in summary.lower()

    data = _transcript_data_payload(
        script="FlowCut",
        word_count=1,
        clips_out=[{"fileId": "LTQM6B501R", "wordCount": 1, "compactWords": []}],
        source="local",
        model_out="faster-whisper-base",
        language_out="en",
        generation=2,
    )
    assert data["clipIds"] == []
    assert data["fileIds"] == ["LTQM6B501R"]
    assert data["nextAction"] != "remove_words_tool"
    assert "fileId" in data["nextAction"] or "timeline" in data["nextAction"]


def test_remove_words_resolves_file_id_to_single_timeline_clip(tmp_cache, tmp_path):
    """Passing a media-bin fileId as clipId must cut the timeline clip, not refuse."""
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "BT5C75FYT5",
                "file_id": "LTQM6B501R",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)
        app.window = MagicMock()
        updates = MagicMock()
        app.updates = updates

        def _fake_extract(path, **kw):
            out = tmp_path / "out.wav"
            out.write_bytes(b"data")
            return str(out), ""

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media), "id": "LTQM6B501R"},
             ), \
             patch(
                 "classes.agent_tools.transcript._file_for_id",
                 return_value=MagicMock(data={"path": str(media), "id": "LTQM6B501R"}),
             ), \
             patch(
                 "classes.speech.asr.extract_mono_16k_wav",
                 side_effect=_fake_extract,
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ), \
             patch(
                 "classes.agent_tools.transcript.apply_compacted_fragments",
                 return_value=(["BT5C75FYT5"], []),
             ) as apply_mock:
            from classes.agent_tools.transcript import remove_words
            # Fake ASR words don't include FlowCut — use filler or wordIndices
            raw = remove_words(clipId="LTQM6B501R", wordIndices="[1]")
        receipt = parse_receipt(raw)
        assert receipt["status"] == "applied", receipt.get("summary")
        assert apply_mock.called
    finally:
        asr_mod.reset_transcriber_factory()


def test_get_transcript_resolves_file_id_passed_as_clip_id(tmp_cache, tmp_path):
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "BT5C75FYT5",
                "file_id": "LTQM6B501R",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)

        def _fake_extract(path, **kw):
            out = tmp_path / "out.wav"
            out.write_bytes(b"data")
            return str(out), ""

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media), "id": "LTQM6B501R"},
             ), \
             patch(
                 "classes.speech.asr.extract_mono_16k_wav",
                 side_effect=_fake_extract,
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ):
            from classes.agent_tools.transcript import get_transcript
            raw = get_transcript(clipId="LTQM6B501R")
        receipt = parse_receipt(raw)
        assert receipt["status"] == "applied", receipt.get("summary")
        assert receipt["data"]["clipIds"] == ["BT5C75FYT5"]
        assert "clipId=BT5C75FYT5" in receipt["summary"]
        assert "Unknown clipId" not in receipt["summary"]
    finally:
        asr_mod.reset_transcriber_factory()


def test_get_transcript_include_words_adds_verbose_array(tmp_cache, tmp_path):
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk2.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "c1",
                "file_id": "f1",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)

        def _fake_extract(path, **kw):
            out = tmp_path / "out2.wav"
            out.write_bytes(b"data")
            return str(out), ""

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media), "id": "f1"},
             ), \
             patch(
                 "classes.speech.asr.extract_mono_16k_wav",
                 side_effect=_fake_extract,
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ):
            from classes.agent_tools.transcript import get_transcript
            raw = get_transcript(clipId="c1", includeWords=True)
        receipt = parse_receipt(raw)
        words = receipt["data"]["clips"][0]["words"]
        assert len(words) == 3
        assert words[1]["text"] == "um"
        assert "startFrame" in words[0]
    finally:
        asr_mod.reset_transcriber_factory()


def test_get_transcript_no_audio_is_unchanged_not_error(tmp_cache, tmp_path):
    from classes.agent_tools.present import NO_AUDIO_USER_MSG
    from classes.speech import asr as asr_mod

    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "silent.mp4"
        media.write_bytes(b"fake")

        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "c1",
                "file_id": "f1",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media), "id": "f1"},
             ), \
             patch(
                 "classes.speech.asr.extract_mono_16k_wav",
                 return_value=("", "no audio track in this media file (video-only / silent)."),
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ):
            from classes.agent_tools.transcript import get_transcript
            raw = get_transcript(clipId="c1")
        receipt = parse_receipt(raw)
        assert receipt["status"] == "unchanged"
        assert receipt["summary"] == NO_AUDIO_USER_MSG
        assert not receipt["summary"].startswith("Error")
    finally:
        asr_mod.reset_transcriber_factory()


def test_user_facing_receipt_shows_script_not_json():
    from classes.agent_tools.present import user_facing_receipt_text
    from classes.agent_tools.receipt import ToolReceipt

    receipt = ToolReceipt.applied(
        "get_transcript_tool",
        "Hi, I'm Yatharth.",
        undo_steps=0,
        data={
            "script": "Hi, I'm Yatharth.\n\nWe built FlowCut.",
            "clips": [{
                "clipId": "c1",
                "words": [
                    {"index": 0, "text": "Hi,", "startFrame": 0},
                    {"index": 1, "text": "I'm", "startFrame": 10},
                    {"index": 2, "text": "Yatharth.", "startFrame": 20},
                ],
            }],
        },
    ).to_json()
    shown = user_facing_receipt_text(receipt)
    assert shown == "Hi, I'm Yatharth.\n\nWe built FlowCut."
    assert "contract" not in shown
    assert "startFrame" not in shown


def test_user_facing_subagent_expands_full_script_artifact():
    from classes.agent_tools.present import user_facing_receipt_text

    long_script = "Word " * 200 + "Thanks."
    blob = json.dumps({
        "status": "ok",
        "summary": "Transcribed the clip.",
        "key_facts": [],
        "artifacts": {"script": long_script},
        "tools_used": ["get_transcript_tool"],
    })
    shown = user_facing_receipt_text(blob)
    assert shown == long_script
    assert "message limit" not in shown.lower()
    assert shown.endswith("Thanks.")


def test_user_facing_receipt_no_audio():
    from classes.agent_tools.present import NO_AUDIO_USER_MSG, user_facing_receipt_text
    from classes.agent_tools.receipt import ToolReceipt

    raw = ToolReceipt.unchanged(
        "get_transcript_tool", NO_AUDIO_USER_MSG, data={"reason": "no_audio"},
    ).to_json()
    assert user_facing_receipt_text(raw) == NO_AUDIO_USER_MSG


def test_words_to_script_paragraphs():
    from classes.agent_tools.present import words_to_script

    script = words_to_script([
        {"text": "Hi,"},
        {"text": "there."},
        {"text": "Next"},
        {"text": "line."},
    ])
    assert script == "Hi, there.\n\nNext line."


def test_remove_words_refuses_stale_generation(tmp_cache, tmp_path):
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        # Seed cache at generation 1
        st = media.stat()
        tmp_cache.put(TranscriptRecord(
            path=str(media.resolve()),
            size=st.st_size,
            mtimeNs=st.st_mtime_ns,
            modelId="faster-whisper-base",
            language="en",
            requestLanguage="auto",
            words=[Word("um", 0.0, 0.2)],
            generation=1,
        ))
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "c1",
                "file_id": "f1",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media)},
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ):
            from classes.agent_tools.transcript import remove_words
            raw = remove_words(
                clipId="c1", wordIndices=[0], transcriptGeneration=99,
            )
        receipt = parse_receipt(raw)
        assert receipt["status"] == "refused"
        assert "transcriptGeneration" in receipt["summary"]
    finally:
        asr_mod.reset_transcriber_factory()


def test_phase5_tools_registered():
    from classes import tool_handlers as th
    for name in (
        "get_transcript_tool",
        "remove_words_tool",
        "transcribe_media_tool",
    ):
        assert name in th.AGENT_TOOL_HANDLERS
        assert name in th.TOOL_DISPLAY_LABELS
        assert name in th.BACKGROUND_SAFE_TOOLS
    assert "get_transcript_tool" in th.READ_ONLY_TOOLS
    assert "transcribe_media_tool" in th.READ_ONLY_TOOLS
    assert "remove_words_tool" not in th.READ_ONLY_TOOLS

    from classes.agent_tools.schema import TOOL_SCHEMAS
    assert "get_transcript_tool" in TOOL_SCHEMAS
    assert "remove_words_tool" in TOOL_SCHEMAS
    assert "transcribe_media_tool" in TOOL_SCHEMAS


def test_remove_words_applies_fragments(tmp_cache, tmp_path):
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        st = media.stat()
        tmp_cache.put(TranscriptRecord(
            path=str(media.resolve()),
            size=st.st_size,
            mtimeNs=st.st_mtime_ns,
            modelId="faster-whisper-base",
            language="en",
            requestLanguage="auto",
            words=[
                Word("hello", 0.0, 0.4),
                Word("um", 0.5, 0.7),
                Word("world", 0.8, 1.2),
            ],
            generation=1,
        ))
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "c1",
                "file_id": "f1",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)
        app.updates.transaction_id = None

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media)},
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ), \
             patch(
                 "classes.agent_tools.transcript.apply_compacted_fragments",
                 return_value=(["c2"], []),
             ) as apply_mock:
            from classes.agent_tools.transcript import remove_words
            raw = remove_words(
                clipId="c1",
                wordIndices=[1],
                transcriptGeneration=1,
            )
        receipt = parse_receipt(raw)
        assert receipt["status"] == "applied"
        assert receipt["undoSteps"] == 1
        assert receipt["data"]["removedWordIndices"] == [1]
        assert receipt["data"]["fragmentCount"] == 2
        assert apply_mock.called
        kwargs = apply_mock.call_args.kwargs
        assert kwargs["clip_id"] == "c1"
        assert kwargs["removed"] == pytest.approx(0.2)
    finally:
        asr_mod.reset_transcriber_factory()


def test_cache_sibling_language_shares_record(tmp_cache, tmp_path):
    """auto and en-ca must resolve to the same ASR row (highest generation)."""
    media = tmp_path / "talk.wav"
    media.write_bytes(b"RIFF" + b"\x00" * 64)
    st = media.stat()
    path = str(media.resolve())
    stale = TranscriptRecord(
        path=path,
        size=st.st_size,
        mtimeNs=st.st_mtime_ns,
        modelId="apple-speech-analyzer",
        language="en-ca",
        requestLanguage="en-ca",
        words=[Word("FlowCut", 0.0, 0.3)],
        generation=1,
    )
    fresh = TranscriptRecord(
        path=path,
        size=st.st_size,
        mtimeNs=st.st_mtime_ns,
        modelId="apple-speech-analyzer",
        language="en-CA",
        requestLanguage="auto",
        words=[Word("FlowCut", 0.0, 0.3), Word("hello", 0.4, 0.6)],
        generation=9,
        createdAt=stale.createdAt + 10,
    )
    tmp_cache.put(stale)
    tmp_cache.put(fresh)
    # Drop memory so lookup must reconcile disk siblings.
    tmp_cache._mem.clear()
    via_auto = tmp_cache.get(path, model_id="apple-speech-analyzer", language="auto")
    via_enca = tmp_cache.get(path, model_id="apple-speech-analyzer", language="en-CA")
    assert via_auto is not None and via_enca is not None
    assert via_auto.generation == 9
    assert via_enca.generation == 9
    assert len(via_auto.words) == 2


def test_remove_words_matches_ignores_stale_generation(tmp_cache, tmp_path):
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        st = media.stat()
        tmp_cache.put(TranscriptRecord(
            path=str(media.resolve()),
            size=st.st_size,
            mtimeNs=st.st_mtime_ns,
            modelId="faster-whisper-base",
            language="en",
            requestLanguage="auto",
            words=[
                Word("hello", 0.0, 0.4),
                Word("FlowCut", 0.5, 0.9),
                Word("world", 1.0, 1.4),
            ],
            generation=1,
        ))
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{
                "id": "c1",
                "file_id": "f1",
                "position": 0.0,
                "start": 0.0,
                "end": 2.0,
                "layer": 0,
                "reader": {"path": str(media)},
            }],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)

        with patch("classes.app.get_app", return_value=app), \
             patch(
                 "classes.agent_tools.inspect_render.snapshot_project",
                 return_value=project,
             ), \
             patch(
                 "classes.agent_tools.transcript._file_data_for_clip",
                 return_value={"path": str(media)},
             ), \
             patch(
                 "classes.clip_utils.project_fps_fraction",
                 return_value=Fraction(30, 1),
             ), \
             patch(
                 "classes.agent_tools.transcript.apply_compacted_fragments",
                 return_value=(["c2"], []),
             ):
            from classes.agent_tools.transcript import remove_words
            raw = remove_words(
                clipId="c1",
                matches=["FlowCut", "flocut"],
                transcriptGeneration=99,
            )
        receipt = parse_receipt(raw)
        assert receipt["status"] == "applied", receipt.get("summary")
        assert receipt["data"]["removedWordIndices"] == [1]
        assert "FlowCut" not in receipt["data"]["script"]
        assert "hello" in receipt["data"]["script"]
        assert "world" in receipt["data"]["script"]
        assert any("stale" in w.lower() for w in receipt.get("warnings") or [])
    finally:
        asr_mod.reset_transcriber_factory()


def test_remove_words_refuses_stale_indices_even_with_matches(tmp_cache, tmp_path):
    """wordIndices override matches, so stale ones must refuse, not cut other words."""
    from classes.speech import asr as asr_mod
    asr_mod.set_transcriber_factory(_FakeTranscriber)
    try:
        media = tmp_path / "talk.wav"
        media.write_bytes(b"RIFF" + b"\x00" * 64)
        st = media.stat()
        tmp_cache.put(TranscriptRecord(
            path=str(media.resolve()), size=st.st_size, mtimeNs=st.st_mtime_ns,
            modelId="faster-whisper-base", language="en", requestLanguage="auto",
            words=[Word("hello", 0.0, 0.4), Word("FlowCut", 0.5, 0.9), Word("world", 1.0, 1.4)],
            generation=1,
        ))
        project = {
            "fps": {"num": 30, "den": 1},
            "clips": [{"id": "c1", "file_id": "f1", "position": 0.0, "start": 0.0, "end": 2.0,
                       "layer": 0, "reader": {"path": str(media)}}],
            "layers": [{"number": 0}],
        }
        app = MagicMock()
        app.project = MagicMock()
        app.project.get.side_effect = lambda k, d=None: project.get(k, d)
        with patch("classes.app.get_app", return_value=app), \
             patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
             patch("classes.agent_tools.transcript._file_data_for_clip", return_value={"path": str(media)}), \
             patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
             patch("classes.agent_tools.transcript.apply_compacted_fragments",
                   return_value=(["c2"], [])) as apply:
            from classes.agent_tools.transcript import remove_words
            raw = remove_words(clipId="c1", wordIndices=[2], matches=["FlowCut"], transcriptGeneration=99)
        receipt = parse_receipt(raw)
        assert receipt["status"] == "refused"
        assert "mismatch" in receipt["summary"]
        apply.assert_not_called()
    finally:
        asr_mod.reset_transcriber_factory()


def test_caption_cues_split_on_timeline_gaps_not_source_time():
    """Source time restarts in each clip; a cue must not span two far-apart clips."""
    from classes.speech.captions import phrase_words

    fps = Fraction(30, 1)
    words = [
        # clip A at 0 s
        {"index": 0, "text": "first", "startSec": 4.0, "endSec": 4.4,
         "timelineStartSec": 0.0, "timelineEndSec": 0.4},
        # clip B placed at 20 s, but its source starts earlier than A's
        {"index": 0, "text": "second", "startSec": 0.5, "endSec": 0.9,
         "timelineStartSec": 20.0, "timelineEndSec": 20.4},
    ]
    cues = phrase_words(words, fps=fps)
    assert [c["text"] for c in cues] == ["first", "second"]
    assert cues[0]["endSec"] <= 0.5 and cues[1]["startSec"] >= 20.0

    # Words a cut brought together on the timeline stay in one cue.
    joined = [
        {"index": 0, "text": "hello", "startSec": 0.0, "endSec": 0.4,
         "timelineStartSec": 0.0, "timelineEndSec": 0.4},
        {"index": 2, "text": "world", "startSec": 1.5, "endSec": 1.9,
         "timelineStartSec": 0.45, "timelineEndSec": 0.85},
    ]
    assert [c["text"] for c in phrase_words(joined, fps=fps)] == ["hello world"]
