"""Words in another language: a song or talk the machine's own language hears as silence is transcribed once more in the language the
recogniser heard, joined the way that language is written, and kept on the shelf. Found live: a Japanese ending came back with 0
words (Apple's engine listens in the machine's language) and 243 characters with locale ja-JP."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import speech_facts as sf  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from classes.speech.cache import TranscriptRecord, Word, file_identity  # noqa: E402

SHA = "e" * 64
PROBE = {"has_audio": True, "duration": 90.0}


def chars(text, start=1.0, step=0.4):
    return [Word(c, start + i * step, start + i * step + 0.3, 0.8) for i, c in enumerate(text)]


class Engine:
    """Its first pass hears nothing and reports *auto*; asked for Japanese it hears a line of lyrics. Records every call."""

    def __init__(self, japanese="思い出せない願いを手放して", auto="en-US"):
        self.calls, self.japanese, self.auto = [], japanese, auto

    def __call__(self, path, **kw):
        self.calls.append(kw.get("language", "auto"))
        abspath, size, mtime = file_identity(path)
        ja = kw.get("language") == "ja-JP"
        return TranscriptRecord(path=abspath, size=size, mtimeNs=mtime, modelId="apple-speech-analyzer", language="ja-JP" if ja else self.auto,
                                words=chars(self.japanese) if ja else [], generation=1, transcriptionSource="local-apple")


def media(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x" * 64)
    return str(p)


@pytest.fixture(autouse=True)
def machine_listens_in_english(monkeypatch):
    monkeypatch.setattr(sf, "_machine_language", lambda: "en")


# ---- the clue and the joining ----
def test_the_script_of_a_file_name_points_to_a_language():
    assert sf.language_hints("/a/【推しの子】エンディング.mp4")[0] == "ja-JP"
    assert sf.language_hints("/a/한국어 노래.mp4") == ["ko-KR"]
    assert sf.language_hints("/a/Русская песня.mp4") == ["ru-RU"]
    assert sf.language_hints("/a/中文歌曲.mp4") == ["zh-CN"]
    assert sf.language_hints("/a/holiday in lisbon.mp4") == []


def test_characters_that_arrive_one_at_a_time_are_joined_without_spaces_and_end_marks_split_lines():
    words = [{"text": t, "startSec": i * 0.4, "endSec": i * 0.4 + 0.3} for i, t in enumerate("思い出せない。願いを")]
    got = sf.sentences_from_words(words)
    assert [s["text"] for s in got] == ["思い出せない。", "願いを"]
    english = sf.sentences_from_words([{"text": "Hello", "startSec": 0, "endSec": 0.3}, {"text": "there.", "startSec": 0.35, "endSec": 0.7}])
    assert english[0]["text"] == "Hello there."


# ---- the retry ----
def test_a_first_pass_that_heard_japanese_is_transcribed_again_in_japanese_and_kept(tmp_path):
    shelf, engine = Shelf(str(tmp_path / "shelf")), Engine(auto="ja-JP")
    got = sf.load_or_transcribe(media(tmp_path, "【推しの子】ED.mp4"), PROBE, SHA, shelf, transcribe=engine)
    assert engine.calls == ["auto", "ja-JP"]
    assert got["retried_language"] == "ja-JP" and got["language"] == "ja-JP" and len(got["words"]) == 13
    assert got["sentences"][0]["text"].startswith("思い出せない")
    assert shelf.read_json(SHA, "speech.json")["retried_language"] == "ja-JP"


def test_a_latin_name_or_a_file_with_plenty_of_words_is_not_retried(tmp_path):
    shelf, engine = Shelf(str(tmp_path / "shelf")), Engine()
    sf.load_or_transcribe(media(tmp_path, "holiday.mp4"), PROBE, SHA, shelf, transcribe=engine)
    assert len(engine.calls) == 1

    talk = Engine()

    def plenty(path, **kw):
        talk.calls.append(kw.get("language", "auto"))
        abspath, size, mtime = file_identity(path)
        return TranscriptRecord(path=abspath, size=size, mtimeNs=mtime, modelId="m", language="en-US", words=chars("a" * 200, step=0.3),
                                generation=1, transcriptionSource="local")
    sf.load_or_transcribe(media(tmp_path, "日本語.mp4"), PROBE, "f" * 64, Shelf(str(tmp_path / "s2")), transcribe=plenty)
    assert talk.calls == ["auto"]


def test_the_retry_happens_once_even_when_it_finds_nothing(tmp_path):
    shelf = Shelf(str(tmp_path / "shelf"))
    engine = Engine(japanese="", auto="ja-JP")
    path = media(tmp_path, "日本語のうた.mp4")
    first = sf.load_or_transcribe(path, PROBE, SHA, shelf, transcribe=engine)
    assert first["language_tried"] == ["ja-JP"] and not first["words"]
    again = sf.load_or_transcribe(path, PROBE, SHA, shelf, transcribe=engine)
    assert engine.calls.count("ja-JP") == 1 and again["language_tried"] == ["ja-JP"]


def test_an_old_empty_layer_gets_its_one_retry_when_the_file_is_indexed_again(tmp_path):
    shelf = Shelf(str(tmp_path / "shelf"))
    old = {"version": 1, "kind": "inferred", "engine": "local-apple", "model": "m", "language": "ja-JP", "words": [], "sentences": [],
           "speech_ranges": [], "stats": {}, "generation": 1}
    shelf.write_json(SHA, "speech.json", old)
    shelf.set_layer(SHA, "speech", version=1, status="ready")
    engine = Engine()
    got = sf.load_or_transcribe(media(tmp_path, "【推しの子】ED.mp4"), PROBE, SHA, shelf, transcribe=engine)
    assert got["retried_language"] == "ja-JP" and len(got["words"]) == 13
    assert len(shelf.read_json(SHA, "speech.json")["words"]) == 13


def test_asking_for_a_language_keeps_the_better_transcript_only(tmp_path):
    shelf, engine = Shelf(str(tmp_path / "shelf")), Engine()
    path = media(tmp_path, "song.mp4")
    layer, replaced = sf.transcribe_in_language(path, PROBE, SHA, shelf, "ja-JP", transcribe=engine)
    assert replaced and len(layer["words"]) == 13 and shelf.layer_ready(SHA, "speech")
    layer2, replaced2 = sf.transcribe_in_language(path, PROBE, SHA, shelf, "en-US", transcribe=engine)
    assert not replaced2 and len(layer2["words"]) == 13
    assert shelf.read_json(SHA, "speech.json")["language"] == "ja-JP"


def test_whisper_gets_the_two_letter_code_and_apple_the_locale():
    from classes.speech.asr import _engine_language
    assert _engine_language("apple", "ja-JP") == "ja-JP" and _engine_language("whisper", "ja-JP") == "ja"
    assert _engine_language("apple", "auto") is None and _engine_language("whisper", "auto") is None
