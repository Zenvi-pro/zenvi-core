"""What is said: sentences, per-shot speech, and a transcript kept on the shelf by content."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import speech_facts as sf  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from classes.speech.cache import TranscriptCache, Word  # noqa: E402

SHA = "d" * 64


def W(text, start, end, speaker=None, conf=0.9):
    d = {"text": text, "startSec": start, "endSec": end, "confidence": conf}
    if speaker:
        d["speakerId"] = speaker
    return d


WORDS = [W("Hello", 0.5, 0.9), W("there.", 0.95, 1.4), W("This", 3.0, 3.2), W("is", 3.25, 3.4), W("a", 3.45, 3.5),
         W("test", 3.55, 3.9), W("of", 3.95, 4.1), W("speech", 4.15, 4.6)]


# ============================ sentences, ranges, stats ============================
def test_sentences_break_at_end_punctuation_and_long_pauses():
    got = sf.sentences_from_words(WORDS)
    assert [s["text"] for s in got] == ["Hello there.", "This is a test of speech"]
    assert got[0]["start"] == 0.5 and got[0]["end"] == 1.4 and got[1]["end"] == 4.6


def test_sentences_break_on_a_speaker_change_and_carry_a_single_speaker():
    words = [W("Yes", 0, 0.3, "A"), W("sure", 0.35, 0.7, "A"), W("no", 0.75, 1.0, "B"), W("way", 1.05, 1.4, "B")]
    got = sf.sentences_from_words(words)
    assert [(s["text"], s["speaker"]) for s in got] == [("Yes sure", "A"), ("no way", "B")]


def test_a_run_on_without_punctuation_is_capped():
    words = [W(f"w{i}", i * 0.2, i * 0.2 + 0.15) for i in range(75)]
    got = sf.sentences_from_words(words)
    assert len(got) >= 3 and all(len(s["text"].split()) <= sf.SENTENCE_MAX_WORDS for s in got)


def test_punctuation_that_arrives_alone_attaches_to_the_word_before():
    words = [W("Wait", 0, 0.3), W(",", 0.3, 0.3), W("what", 0.5, 0.8), W("?", 0.8, 0.8)]
    assert sf.sentences_from_words(words)[0]["text"] == "Wait, what?"


def test_no_words_is_no_sentences():
    assert sf.sentences_from_words([]) == []
    assert sf.speech_ranges([]) == []
    assert sf.speech_stats([], 10.0)["word_count"] == 0


def test_speech_ranges_join_short_pauses_but_not_long_ones():
    ranges = sf.speech_ranges(WORDS)
    assert ranges == [[0.5, 1.4], [3.0, 4.6]]


def test_stats_are_measured_from_the_ranges():
    stats = sf.speech_stats(WORDS, 10.0)
    assert stats["word_count"] == 8
    assert stats["speech_seconds"] == pytest.approx(0.9 + 1.6, abs=0.01)
    assert stats["speech_ratio"] == pytest.approx(0.25, abs=0.01)
    assert stats["words_per_minute"] == pytest.approx(8 / (2.5 / 60.0), abs=0.5)


def test_speech_is_apportioned_to_the_shots_it_falls_in():
    shots = [{"id": 0, "start": 0.0, "end": 2.0}, {"id": 1, "start": 2.0, "end": 4.0}, {"id": 2, "start": 4.0, "end": 6.0}]
    got = sf.per_shot_speech(WORDS, shots)
    assert got[0]["words"] == 2 and got[0]["speech_seconds"] == pytest.approx(0.9, abs=0.01)
    assert got[1]["words"] == 4 and got[1]["speech_seconds"] == pytest.approx(1.0, abs=0.01)   # "of" starts at 3.95, its midpoint is past 4.0
    assert got[2]["words"] == 2 and got[2]["speech_ratio"] == pytest.approx(0.3, abs=0.01)
    assert sum(g["words"] for g in got.values()) == len(WORDS)


# ============================ the transcript on the shelf ============================
class Recorder:
    """A fake transcriber: counts calls and returns a TranscriptRecord for the path it is given."""

    def __init__(self, model="whisper.cpp-base-q5_1"):
        self.calls = []
        self.model = model

    def __call__(self, path, **kw):
        self.calls.append((path, kw))
        from classes.speech.cache import TranscriptRecord, file_identity
        abspath, size, mtime = file_identity(path)
        return TranscriptRecord(path=abspath, size=size, mtimeNs=mtime, modelId=self.model, language="en",
                                words=[Word(w["text"], w["startSec"], w["endSec"], w["confidence"]) for w in WORDS],
                                generation=1, transcriptionSource="local")


@pytest.fixture
def media(tmp_path):
    p = tmp_path / "talk.mp4"
    p.write_bytes(b"x" * 64)
    return str(p)


PROBE = {"has_audio": True, "duration": 10.0}


def test_a_new_file_is_transcribed_once_and_kept_on_the_shelf(tmp_path, media):
    shelf, rec = Shelf(str(tmp_path / "shelf")), Recorder()
    got = sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=rec)
    assert len(rec.calls) == 1 and got["kind"] == "inferred" and got["engine"] == "local"
    assert [s["text"] for s in got["sentences"]] == ["Hello there.", "This is a test of speech"]
    assert got["stats"]["word_count"] == 8 and got["words"][0]["confidence"] == 0.9
    assert shelf.layer_ready(SHA, "speech", version=1)
    assert shelf.read_json(SHA, "speech.json")["words"][0]["text"] == "Hello"


def test_the_same_content_at_another_path_is_not_transcribed_again(tmp_path, media):
    shelf, rec = Shelf(str(tmp_path / "shelf")), Recorder()
    sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=rec)
    moved = tmp_path / "elsewhere" / "renamed.mov"
    moved.parent.mkdir()
    moved.write_bytes(b"x" * 64)
    got = sf.load_or_transcribe(str(moved), PROBE, SHA, shelf, transcribe=rec)
    assert len(rec.calls) == 1, "the shelf answered; the engine was not run again"
    assert got["stats"]["word_count"] == 8


def test_a_transcript_from_the_shelf_is_handed_to_the_path_cache(tmp_path, media, monkeypatch):
    from classes.speech import asr

    monkeypatch.setattr(asr, "resolve_engine", lambda engine="auto": "whisper")
    monkeypatch.setattr(asr, "_cache_model_id", lambda engine, model_id: "whisper.cpp-base-q5_1")
    shelf, rec = Shelf(str(tmp_path / "shelf")), Recorder("whisper.cpp-base-q5_1")
    sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=rec)
    moved = tmp_path / "elsewhere" / "renamed.mov"
    moved.parent.mkdir()
    moved.write_bytes(b"x" * 64)
    cache = TranscriptCache(root=str(tmp_path / "cache"))
    sf.load_or_transcribe(str(moved), PROBE, SHA, shelf, transcribe=rec, cache=cache)
    hit = cache.get(str(moved), model_id="whisper.cpp-base-q5_1", language="auto")
    assert hit is not None and len(hit.words) == 8, "get_transcript for the moved file must not re-run ASR"


def test_a_transcript_from_another_engine_is_not_forced_into_the_path_cache(tmp_path, media, monkeypatch):
    from classes.speech import asr

    monkeypatch.setattr(asr, "resolve_engine", lambda engine="auto": "whisper")
    monkeypatch.setattr(asr, "_cache_model_id", lambda engine, model_id: "whisper.cpp-base-q5_1")
    shelf = Shelf(str(tmp_path / "shelf"))
    sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=Recorder("apple-speech-analyzer"))
    cache = TranscriptCache(root=str(tmp_path / "cache"))
    sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=Recorder(), cache=cache)
    assert cache.get(media, model_id="whisper.cpp-base-q5_1", language="auto") is None
    assert cache.get(media, model_id="apple-speech-analyzer", language="auto") is None, "nothing from the other engine is primed"
    import os
    assert not os.path.isdir(str(tmp_path / "cache")) or os.listdir(str(tmp_path / "cache")) == []


def test_no_audio_means_no_speech_layer(tmp_path, media):
    rec = Recorder()
    assert sf.load_or_transcribe(media, {"has_audio": False, "duration": 5}, SHA, Shelf(str(tmp_path / "s")), transcribe=rec) is None
    assert rec.calls == []


def test_a_missing_engine_is_reported_not_raised(tmp_path, media):
    def boom(path, **kw):
        raise RuntimeError("faster-whisper is not installed")

    shelf = Shelf(str(tmp_path / "s"))
    got = sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=boom)
    assert "not installed" in got["unavailable"]
    assert not shelf.layer_ready(SHA, "speech"), "nothing is claimed as done"


def test_cancelling_propagates_so_the_caller_can_stop(tmp_path, media):
    def cancelled(path, **kw):
        raise InterruptedError("speech job cancelled")

    with pytest.raises(InterruptedError):
        sf.load_or_transcribe(media, PROBE, SHA, Shelf(str(tmp_path / "s")), transcribe=cancelled)


def test_a_very_long_file_is_skipped_not_transcribed(tmp_path, media):
    rec = Recorder()
    got = sf.load_or_transcribe(media, {"has_audio": True, "duration": 4 * 3600.0}, SHA, Shelf(str(tmp_path / "s")), transcribe=rec)
    assert "skipped" in got and rec.calls == []


def test_an_out_of_date_layer_is_transcribed_again(tmp_path, media):
    shelf, rec = Shelf(str(tmp_path / "shelf")), Recorder()
    sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=rec)
    shelf.set_layer(SHA, "speech", version=0, status="ready")      # an older algorithm's output
    sf.load_or_transcribe(media, PROBE, SHA, shelf, transcribe=rec)
    assert len(rec.calls) == 2


def test_it_works_without_a_fingerprint_but_saves_nothing(tmp_path, media):
    shelf, rec = Shelf(str(tmp_path / "shelf")), Recorder()
    got = sf.load_or_transcribe(media, PROBE, "", shelf, transcribe=rec)
    assert got["stats"]["word_count"] == 8 and shelf.list_entries() == []
