"""The Index dock's reading view: paragraphs, the word lit at a frame, and click-to-seek."""

from __future__ import annotations

import pytest

from classes.speech import transcript_view as V


def clip(words, cid="C1"):
    return {"clipId": cid, "words": words}


def w(text, frame, speaker=""):
    return {"text": text, "startFrame": frame, "speakerId": speaker}


def test_words_come_out_in_timeline_order_across_clips():
    model = V.reading_model([clip([w("later", 300)], "B"), clip([w("first", 10), w("second", 40)], "A")])
    assert [x["text"] for x in model["words"]] == ["first", "second", "later"]
    assert [x["clip"] for x in model["words"]] == ["A", "A", "B"]


def test_a_long_pause_or_a_new_speaker_starts_a_new_paragraph():
    model = V.reading_model([clip([w("a", 0, "S1"), w("b", 20, "S1"), w("c", 200, "S1"), w("d", 220, "S2")])], fps=30.0)
    assert model["paragraphs"] == [[0, 1], [2], [3]], "a 6 s pause splits S1; S2 speaks next"


def test_a_word_without_a_frame_gets_one_from_its_seconds_and_one_with_neither_is_left_out():
    model = V.reading_model([clip([{"text": "x", "timelineStartSec": 2.0}, {"text": "lost"}, {"text": "y", "startSec": 3.0}])], fps=30.0)
    assert [(x["text"], x["frame"]) for x in model["words"]] == [("x", 60), ("y", 90)]


def test_text_is_escaped_so_a_word_cannot_inject_markup():
    html = V.reading_html(V.reading_model([clip([w("<b>&", 1)])]))
    assert "<b>" not in html.replace("<a ", "").replace("</a>", "") and "&lt;b&gt;&amp;" in html


def test_every_word_is_a_link_to_its_own_index_and_speakers_are_labelled():
    html = V.reading_html(V.reading_model([clip([w("Hi", 1, "S1"), w("there", 10, "S1")])]))
    assert "href='w:0'" in html and "href='w:1'" in html and "S1" in html


@pytest.mark.parametrize("frame,expected", [(0, -1), (9, -1), (10, 0), (39, 0), (40, 1), (500, 2), (9999, -1)])
def test_the_lit_word_is_the_last_one_that_has_started_and_the_final_word_does_not_stay_lit(frame, expected):
    assert V.word_at([10, 40, 500], frame) == expected


def test_nothing_is_lit_for_an_empty_transcript():
    assert V.word_at([], 10) == -1


def test_a_click_turns_into_the_frame_to_seek_to_and_foreign_links_are_ignored():
    model = V.reading_model([clip([w("a", 10), w("b", 70)])])
    assert V.seek_frame(model, "w:1") == 70
    assert V.seek_frame(model, "https://example.org") is None
    assert V.seek_frame(model, "w:99") is None and V.seek_frame(model, "w:x") is None
