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
