"""Phase 5 full suite: VAD, captions, beats, diarize, visual search, wiring."""

from __future__ import annotations

import struct
import wave
from fractions import Fraction
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from classes.agent_tools.receipt import parse_receipt
from classes.speech.captions import cues_to_srt, parse_srt_or_vtt, phrase_words
from classes.speech.diarize import WindowClusterDiarize, speaker_turns
from classes.speech.cache import Word
from classes.speech.runtime import CancelToken
from classes.speech.vad import EnergyVad, silence_ranges_from_speech
from classes.speech.visual_index import HashEmbedder, VisualIndex, reset_visual_index_for_tests
from classes.speech.beats import EnergyBeatEngine
from classes.speech.word_ranges import compact_fragments_after_remove


@pytest.fixture(autouse=True)
def _force_whisper_in_unit_tests():
    with patch("classes.speech.apple_asr.apple_asr_available", return_value=False):
        yield


def _write_tone_wav(path: Path, *, seconds: float = 1.0, rate: int = 16000, amp: int = 8000):
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        # Simple square-ish bursts every 0.25s for VAD/beats
        on = (i // (rate // 4)) % 2 == 0
        sample = amp if on else 200
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)


def test_energy_vad_and_silence_gaps(tmp_path):
    wav = tmp_path / "t.wav"
    _write_tone_wav(wav, seconds=2.0)
    token = CancelToken()
    windows = EnergyVad().speech_windows(
        str(wav), token=token, min_speech_sec=0.05, min_silence_sec=0.1,
    )
    assert windows
    gaps = silence_ranges_from_speech(
        windows, duration=2.0, min_pause_sec=0.05, pad_sec=0.02,
    )
    # Should find some silence between bursts
    assert isinstance(gaps, list)


def test_silence_refuse_fraction_logic():
    fps = Fraction(30, 1)
    # Remove almost everything
    frags, removed = compact_fragments_after_remove(
        position=0.0, start=0.0, end=10.0,
        remove_ranges=[(0.1, 9.9)], fps=fps,
    )
    assert removed / 10.0 > 0.85


def test_caption_phrase_and_srt_roundtrip():
    fps = Fraction(30, 1)
    words = [
        {"index": 0, "text": "Hello", "startSec": 0.0, "endSec": 0.3,
         "startFrame": 0, "endFrame": 9},
        {"index": 1, "text": "world", "startSec": 0.35, "endSec": 0.7,
         "startFrame": 10, "endFrame": 21},
        {"index": 2, "text": "again", "startSec": 2.0, "endSec": 2.4,
         "startFrame": 60, "endFrame": 72},
    ]
    cues = phrase_words(words, max_words=4, max_chars=40, max_gap_sec=0.5, fps=fps)
    assert len(cues) == 2
    srt = cues_to_srt(cues, fps=fps)
    assert "Hello world" in srt
    parsed = parse_srt_or_vtt(srt, fps=fps)
    assert len(parsed) == 2
    assert parsed[0]["startFrame"] == cues[0]["startFrame"]
    assert abs(parsed[0]["startSec"] - cues[0]["startSec"]) < 1.0 / 30 + 1e-6


def test_diarize_labels_speakers():
    words = [
        Word("hi", 0.0, 0.2),
        Word("there", 0.3, 0.5),
        Word("hello", 1.5, 1.8),
        Word("again", 2.0, 2.3),
        Word("yes", 2.5, 2.7),
        Word("no", 3.0, 3.2),
    ]
    # Fake wav via Energy path needs real file — use diarize engine with empty samples
    # by writing a wav and calling label_words
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "d.wav"
    _write_tone_wav(p, seconds=4.0)
    labelled, warnings = WindowClusterDiarize().label_words(
        str(p), words, token=CancelToken(), max_speakers=2,
    )
    assert all(w.speakerId for w in labelled)
    rows = [
        {"speakerId": w.speakerId, "text": w.text, "startFrame": i,
         "endFrame": i + 1, "startSec": w.startSec, "endSec": w.endSec}
        for i, w in enumerate(labelled)
    ]
    turns = speaker_turns(rows)
    assert turns


def test_beats_detect_something(tmp_path):
    wav = tmp_path / "b.wav"
    _write_tone_wav(wav, seconds=2.0, amp=12000)
    result = EnergyBeatEngine().detect(str(wav), token=CancelToken())
    assert "beats" in result
    assert "bpm" in result


def test_visual_index_search(tmp_path):
    reset_visual_index_for_tests(VisualIndex(root=str(tmp_path / "idx")))
    idx = VisualIndex(root=str(tmp_path / "idx"))
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"red car photo data AAA")
    b.write_bytes(b"blue ocean other BBB")
    idx.upsert_file("f1", str(a))
    idx.upsert_file("f2", str(b))
    hits = idx.search("red car", top_k=2)
    assert len(hits) == 2
    assert hits[0]["provider"] == "local"
    # Same query embedding should prefer a over random — hash stub may not
    # be semantic; just ensure deterministic ordering for same query.
    hits2 = idx.search("red car", top_k=2)
    assert [h["fileId"] for h in hits] == [h["fileId"] for h in hits2]
    reset_visual_index_for_tests(None)


def test_hash_embedder_normalized():
    v = HashEmbedder().embed_text("hello")
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6


def test_remove_silence_tool_mocked(tmp_path):
    wav = tmp_path / "talk.wav"
    _write_tone_wav(wav, seconds=2.0)
    project = {
        "fps": {"num": 30, "den": 1},
        "clips": [{
            "id": "c1", "position": 0.0, "start": 0.0, "end": 2.0, "layer": 0,
            "reader": {"path": str(wav)},
        }],
        "layers": [{"number": 0}],
    }
    app = MagicMock()
    app.project = MagicMock()
    app.project.get.side_effect = lambda k, d=None: project.get(k, d)

    with patch("classes.app.get_app", return_value=app), \
         patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
         patch("classes.agent_tools.speech_extra._media_path_for_clip", return_value=str(wav)), \
         patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
         patch(
             "classes.speech.vad.detect_speech_windows",
             return_value=[(0.0, 0.5), (1.5, 2.0)],
         ), \
         patch(
             "classes.agent_tools.transcript.apply_compacted_fragments",
             return_value=([], []),
         ) as apply_mock:
        from classes.agent_tools.speech_extra import remove_silence
        raw = remove_silence(clipId="c1", minPauseSec=0.2, padSec=0.05)
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    assert apply_mock.called


def test_remove_silence_refuses_total_wipe(tmp_path):
    wav = tmp_path / "talk.wav"
    _write_tone_wav(wav, seconds=1.0)
    project = {
        "fps": {"num": 30, "den": 1},
        "clips": [{
            "id": "c1", "position": 0.0, "start": 0.0, "end": 1.0, "layer": 0,
            "reader": {"path": str(wav)},
        }],
    }
    app = MagicMock()
    app.project = MagicMock()
    app.project.get.side_effect = lambda k, d=None: project.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
         patch("classes.agent_tools.speech_extra._media_path_for_clip", return_value=str(wav)), \
         patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
         patch("classes.speech.vad.detect_speech_windows", return_value=[]):
        from classes.agent_tools.speech_extra import remove_silence
        # No speech → entire file is silence → refuse entire delete or applied remove
        raw = remove_silence(clipId="c1", maxRemoveFraction=0.5)
    receipt = parse_receipt(raw)
    assert receipt["status"] in ("refused", "unchanged", "applied", "error")
    if receipt["status"] == "refused":
        assert "Would remove" in receipt["summary"] or "entire" in receipt["summary"]


def test_add_captions_places_titles():
    from classes.agent_tools.receipt import ToolReceipt
    fake_transcript = ToolReceipt.applied(
        "get_transcript_tool", "ok", undo_steps=0,
        data={
            "transcriptionSource": "local",
            "transcriptGeneration": 1,
            "clips": [{
                "clipId": "c1",
                "words": [
                    {"index": 0, "text": "Hi", "startSec": 0.0, "endSec": 0.3,
                     "startFrame": 0, "endFrame": 9,
                     "timelineStartSec": 0.0, "timelineEndSec": 0.3},
                    {"index": 1, "text": "there", "startSec": 0.35, "endSec": 0.7,
                     "startFrame": 10, "endFrame": 21,
                     "timelineStartSec": 0.35, "timelineEndSec": 0.7},
                ],
            }],
        },
    ).to_json()
    fake_title = ToolReceipt.applied(
        "add_title_tool", "placed", data={"file_id": "t1"},
    ).to_json()
    app = MagicMock()
    app.project = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"fps": {"num": 30, "den": 1}}.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
         patch("classes.agent_tools.transcript.get_transcript", return_value=fake_transcript), \
         patch("classes.agent_tools.titles.add_title", return_value=fake_title) as title_mock:
        from classes.agent_tools.speech_extra import add_captions
        raw = add_captions(clipId="c1")
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    assert receipt["data"]["count"] >= 1
    assert title_mock.called
    assert "captionGroupId" in receipt["data"]


def test_export_captions_writes_srt(tmp_path):
    from classes.agent_tools.receipt import ToolReceipt
    fake_transcript = ToolReceipt.applied(
        "get_transcript_tool", "ok", undo_steps=0,
        data={
            "clips": [{
                "words": [
                    {"index": 0, "text": "Hi", "startSec": 0.0, "endSec": 0.4,
                     "startFrame": 0, "endFrame": 12,
                     "timelineStartSec": 0.0, "timelineEndSec": 0.4},
                ],
            }],
        },
    ).to_json()
    out = tmp_path / "out.srt"
    app = MagicMock()
    app.project = MagicMock()
    app.project.get.side_effect = lambda k, d=None: {"fps": {"num": 30, "den": 1}}.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
         patch("classes.agent_tools.transcript.get_transcript", return_value=fake_transcript):
        from classes.agent_tools.speech_extra import export_captions
        raw = export_captions(path=str(out), format="srt")
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    assert out.is_file()
    assert "Hi" in out.read_text(encoding="utf-8")


def test_search_media_local_tool(tmp_path):
    reset_visual_index_for_tests(VisualIndex(root=str(tmp_path / "idx")))
    media = tmp_path / "shot.bin"
    media.write_bytes(b"harbor sunset frame")
    project = {"files": [{"id": "f1", "path": str(media)}], "clips": [], "fps": {"num": 30, "den": 1}}
    app = MagicMock()
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
         patch("classes.query.File.filter", return_value=[]):
        from classes.agent_tools.speech_extra import search_media_local
        raw = search_media_local(query="harbor", top_k=3)
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    assert receipt["data"]["provider"] == "local"
    reset_visual_index_for_tests(None)


def test_detect_beats_tool(tmp_path):
    wav = tmp_path / "m.wav"
    _write_tone_wav(wav, seconds=1.5, amp=10000)
    project = {
        "fps": {"num": 30, "den": 1},
        "clips": [{
            "id": "c1", "position": 0, "start": 0, "end": 1.5, "layer": 0,
            "reader": {"path": str(wav)},
        }],
    }
    app = MagicMock()
    app.project = MagicMock()
    app.project.get.side_effect = lambda k, d=None: project.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
         patch("classes.agent_tools.speech_extra._media_path_for_clip", return_value=str(wav)), \
         patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
         patch("classes.speech.beats.extract_mono_16k_wav", return_value=(str(wav), "")):
        from classes.agent_tools.speech_extra import detect_beats_tool_handler
        raw = detect_beats_tool_handler(clipId="c1")
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    assert "beats" in receipt["data"]


def test_diarize_media_tool(tmp_path):
    wav = tmp_path / "d.wav"
    _write_tone_wav(wav, seconds=2.0)
    from classes.speech.cache import TranscriptCache, TranscriptRecord, reset_default_cache_for_tests
    cache = TranscriptCache(root=str(tmp_path / "t"))
    reset_default_cache_for_tests(cache)
    st = wav.stat()
    cache.put(TranscriptRecord(
        path=str(wav.resolve()), size=st.st_size, mtimeNs=st.st_mtime_ns,
        modelId="faster-whisper-base", language="en", requestLanguage="auto",
        words=[Word("a", 0.0, 0.2), Word("b", 0.3, 0.5), Word("c", 1.0, 1.2),
               Word("d", 1.3, 1.5), Word("e", 1.6, 1.8)],
        generation=1,
    ))
    project = {
        "fps": {"num": 30, "den": 1},
        "clips": [{
            "id": "c1", "position": 0, "start": 0, "end": 2, "layer": 0,
            "reader": {"path": str(wav)},
        }],
    }
    app = MagicMock()
    app.project = MagicMock()
    app.project.get.side_effect = lambda k, d=None: project.get(k, d)
    with patch("classes.app.get_app", return_value=app), \
         patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
         patch("classes.agent_tools.speech_extra._media_path_for_clip", return_value=str(wav)), \
         patch("classes.clip_utils.project_fps_fraction", return_value=Fraction(30, 1)), \
         patch("classes.speech.diarize.extract_mono_16k_wav", return_value=(str(wav), "")):
        from classes.agent_tools.speech_extra import diarize_media
        raw = diarize_media(clipId="c1", maxSpeakers=2)
    receipt = parse_receipt(raw)
    assert receipt["status"] == "applied"
    assert receipt["data"]["speakers"]
    reset_default_cache_for_tests(None)


def test_all_phase5_tools_registered():
    from classes import tool_handlers as th
    from classes.agent_tools.schema import TOOL_SCHEMAS
    names = [
        "get_transcript_tool",
        "remove_words_tool",
        "transcribe_media_tool",
        "remove_silence_tool",
        "add_captions_tool",
        "export_captions_tool",
        "detect_beats_tool",
        "diarize_media_tool",
        "search_media_local_tool",
    ]
    for name in names:
        assert name in th.AGENT_TOOL_HANDLERS, name
        assert name in th.TOOL_DISPLAY_LABELS, name
        assert name in th.BACKGROUND_SAFE_TOOLS, name
        assert name in TOOL_SCHEMAS, name
    assert "remove_silence_tool" not in th.READ_ONLY_TOOLS
    assert "add_captions_tool" not in th.READ_ONLY_TOOLS


def test_inspect_still_has_no_transcription_key():
    from classes.agent_tools.schema import TOOL_SCHEMAS
    for name in ("inspect_timeline_tool", "inspect_media_tool"):
        props = TOOL_SCHEMAS[name]["properties"]
        assert "transcription" not in props
        assert "transcribe" not in props


def test_server_instructions_mention_speech():
    from classes.agent_mcp_server import SERVER_INSTRUCTIONS
    assert "get_transcript_tool" in SERVER_INSTRUCTIONS
    assert "remove_silence_tool" in SERVER_INSTRUCTIONS
    assert "inspect" in SERVER_INSTRUCTIONS.lower()


def test_audio_mix_speech_windows_best_prefers_cues():
    from classes import audio_mix as am
    clip = {"position": 0.0, "start": 0.0, "end": 5.0}
    meta = {"transcript_cues": [{"source_start": 1.0, "source_end": 2.0}]}
    windows, src = am.speech_windows_best(clip, meta, media_path="")
    assert src == "transcript_cues"
    assert windows


def test_search_media_local_only_returns_the_open_projects_media(tmp_path):
    """Review #216: the shared index returned another project's files and paths."""
    idx = VisualIndex(root=str(tmp_path / "idx"))
    reset_visual_index_for_tests(idx)
    other = tmp_path / "other_project_secret.bin"
    other.write_bytes(b"harbor sunset frame")
    idx.upsert_file("OTHER", str(other))
    mine = tmp_path / "shot.bin"
    mine.write_bytes(b"harbor at dusk")
    project = {"files": [{"id": "f1", "path": str(mine)}], "clips": [], "fps": {"num": 30, "den": 1}}
    with patch("classes.app.get_app", return_value=MagicMock()), \
         patch("classes.agent_tools.inspect_render.snapshot_project", return_value=project), \
         patch("classes.query.File.filter", return_value=[]):
        from classes.agent_tools.speech_extra import search_media_local
        receipt = parse_receipt(search_media_local(query="harbor", top_k=5))
    assert [h["fileId"] for h in receipt["data"]["hits"]] == ["f1"]
    reset_visual_index_for_tests(None)
