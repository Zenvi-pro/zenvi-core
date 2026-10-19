"""The edit's form, the stock search phrase and the speech retry follow explicit values, not words that happen to appear in free text."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from classes.media_index import reference as mref  # noqa: E402
from classes.media_index import review as R  # noqa: E402
from classes.media_index import speech_facts as sf  # noqa: E402
from classes.media_index.store import Shelf  # noqa: E402
from classes.speech.cache import TranscriptRecord, Word, file_identity  # noqa: E402

SHA = "9" * 64
PROBE = {"has_audio": True, "duration": 90.0}          # ninety seconds with nothing heard is a sparse first pass


# ---- review: the form is an explicit value ----
def test_free_text_never_sets_a_form_only_the_explicit_value_does():
    assert R.music_expected(R.Brief(vibe="made for radio")) is None
    assert R.music_expected(R.Brief(form="made for radio")) is None, "'ad' inside 'made' is not the ad form"
    assert R.music_expected(R.Brief(form="ad")) is True


def test_the_review_tool_takes_the_form_only_from_its_fixed_values():
    from classes.editor_tools import REGISTRY, media_index_tools_review  # noqa: F401  (importing registers review_edit_tool)
    form = REGISTRY["review_edit_tool"].schema["properties"]["form"]
    assert {"", "ad", "vlog", "tiktok", "podcast"} <= set(form.get("enum", []))
    assert "must be one of" in REGISTRY["review_edit_tool"].func(form="made for radio")


# ---- reference: the stock phrase is the description as written ----
def test_a_stock_phrase_keeps_anime_and_detail_as_written_and_detail_is_not_a_close_up():
    assert mref.stock_query_for("anime character in detail", None) == "anime character in detail"
    assert mref.tightness("detail shot") is None
    assert mref.tightness("extreme close-up") == 4.0


# ---- speech: the retry follows what the recogniser heard, not the file name ----
class Recogniser:
    """Hears nothing in its first pass and reports *heard* for it; asked for ja-JP it hears a line of lyrics."""

    def __init__(self, heard):
        self.heard, self.calls = heard, []

    def __call__(self, path, **kw):
        language = kw.get("language", "auto")
        self.calls.append(language)
        abspath, size, mtime = file_identity(path)
        japanese = language == "ja-JP"
        words = [Word(c, 1.0 + i * 0.4, 1.3 + i * 0.4, 0.8) for i, c in enumerate("思い出せない願いを手放して")] if japanese else []
        return TranscriptRecord(path=abspath, size=size, mtimeNs=mtime, modelId="m", language="ja-JP" if japanese else self.heard,
                                words=words, generation=1, transcriptionSource="local")


def _media(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"x" * 64)
    return str(p)


def test_a_japanese_file_name_starts_no_retry_when_the_recogniser_heard_the_machine_language(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "_machine_language", lambda: "en", raising=False)
    rec = Recogniser(heard="en-US")
    got = sf.load_or_transcribe(_media(tmp_path, "【推しの子】ED.mp4"), PROBE, SHA, Shelf(str(tmp_path / "shelf")), transcribe=rec)
    assert rec.calls == ["auto"] and "retried_language" not in got


def test_a_latin_file_name_is_retried_in_the_language_the_recogniser_heard(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "_machine_language", lambda: "en", raising=False)
    rec = Recogniser(heard="ja-JP")
    got = sf.load_or_transcribe(_media(tmp_path, "holiday.mp4"), PROBE, SHA, Shelf(str(tmp_path / "shelf")), transcribe=rec)
    assert rec.calls == ["auto", "ja-JP"] and got["retried_language"] == "ja-JP" and len(got["words"]) == 13
