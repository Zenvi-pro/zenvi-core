"""Captions are HyperFrames caption components fed with Zenvi's own word timings."""

import json

import pytest

from classes import hyperframes_captions as cap

COMPONENT = """<!doctype html>
<html><body>
<div data-composition-id="caption-pill-karaoke" data-start="0" data-duration="8" data-width="1920">
</div>
<script>
  var DURATION = 8;
  var MAX_WORDS_PER_GROUP = 4;
  var ACCENT_COLORS = ["#FF76FF", "#FF0002"];
  var TRANSCRIPT = [
    { text: "Every", start: 0.0, end: 0.3 },
    { text: "great", start: 0.3, end: 0.55 },
  ];
  window.__timelines = {};
</script>
</body></html>
"""


def _words(html):
    start = html.index("var TRANSCRIPT = ") + len("var TRANSCRIPT = ")
    return json.loads(html[start:html.index(";", start)])


def test_styles_cover_karaoke_and_word_highlight_looks():
    assert "pill-karaoke" in cap.STYLES and "highlight" in cap.STYLES and "kinetic-slam" in cap.STYLES
    assert cap.DEFAULT_STYLE in cap.STYLES
    for style_id, style in cap.STYLES.items():
        assert style["label"] and style["description"], style_id
    assert cap.component_id("pill-karaoke") == "caption-pill-karaoke"
    with pytest.raises(cap.CaptionError):
        cap.component_id("comic-sans")


def test_compile_swaps_in_the_words_and_the_duration_and_nothing_else():
    words = [{"text": "Hello", "start": 0.5, "end": 0.9}, {"text": 'say "hi"', "start": 1.0, "end": 1.4}]
    html = cap.compile_composition(COMPONENT, words, duration=12.5)

    assert _words(html) == words                       # quotes survive as JSON, not as broken JS
    assert 'data-duration="12.5"' in html and "var DURATION = 12.5;" in html
    assert 'data-duration="8"' not in html and "Every" not in html
    assert 'var ACCENT_COLORS = ["#FF76FF", "#FF0002"];' in html and "MAX_WORDS_PER_GROUP = 4" in html


@pytest.mark.parametrize("name", ["WORDS", "W"])
def test_compile_handles_the_other_names_components_use_for_the_word_list(name):
    html = cap.compile_composition(COMPONENT.replace("var TRANSCRIPT", "var " + name).replace("  var DURATION = 8;\n", ""),
                                   [{"text": "Hi", "start": 0, "end": 1}], duration=3)
    assert ("var %s = " % name) in html and '"Hi"' in html and 'data-duration="3"' in html


def test_compile_refuses_a_component_it_cannot_drive():
    with pytest.raises(cap.CaptionError, match="word list"):
        cap.compile_composition("<html><script>var NOTHING = 1;</script></html>",
                                [{"text": "Hi", "start": 0, "end": 1}], duration=3)
    with pytest.raises(cap.CaptionError, match="no words"):
        cap.compile_composition(COMPONENT, [], duration=3)


def test_words_are_cut_to_the_clip_and_made_relative_to_it():
    transcript = [
        {"text": "before", "startSec": 8.0, "endSec": 8.4},        # ends before the clip starts
        # source-media times differ once a clip is trimmed or moved: the timeline ones win
        {"text": "Hello", "startSec": 3.2, "endSec": 3.6, "timelineStartSec": 10.2, "timelineEndSec": 10.6},
        {"word": "there", "startSec": 10.6, "endSec": 11.1},
        {"text": "  ", "startSec": 11.2, "endSec": 11.3},           # nothing to draw
        {"text": "late", "startSec": 14.9, "endSec": 15.6},         # runs past the end: clamped
        {"text": "after", "startSec": 16.0, "endSec": 16.5},
    ]
    assert cap.clip_words(transcript, clip_start=10.0, clip_end=15.0) == [
        {"text": "Hello", "start": 0.2, "end": 0.6},
        {"text": "there", "start": 0.6, "end": 1.1},
        {"text": "late", "start": 4.9, "end": 5.0},
    ]


def test_editing_the_text_keeps_the_timing():
    words = [{"text": "Helo", "start": 0.2, "end": 0.6}, {"text": "ther", "start": 0.6, "end": 1.1},
             {"text": "wrld", "start": 2.0, "end": 2.5}]
    # same number of words: one-for-one, timings untouched
    assert cap.retime_words(words, "Hello there world") == [
        {"text": "Hello", "start": 0.2, "end": 0.6}, {"text": "there", "start": 0.6, "end": 1.1},
        {"text": "world", "start": 2.0, "end": 2.5}]
    # a different number of words: spread evenly over the same span
    out = cap.retime_words(words, "Hello world")
    assert [w["text"] for w in out] == ["Hello", "world"]
    assert out[0]["start"] == 0.2 and out[-1]["end"] == 2.5 and out[0]["end"] == out[1]["start"]
    assert cap.retime_words(words, "   ") == []


GROUPED = """<div data-duration="8"></div><script>
  var WORDS = [
    { text: "Every", start: 0.0, end: 0.3 },
  ];
  var RAW_GROUPS = [
    [0, 3],
    [25, 27],
  ];
  var GROUPS = RAW_GROUPS.map(function (pair, gi) {
    var nextStart = gi + 1 < RAW_GROUPS.length ? WORDS[RAW_GROUPS[gi + 1][0]].start : 8.0;
    return pair;
  });
</script>"""
LITERAL_GROUPS = """<div data-duration="8"></div><script>
  var WORDS = [
    { text: "Every", start: 0.0, end: 0.3 },
  ];
  var GROUPS = [
    { wordStart: 0, wordEnd: 3, start: 0.0, end: 1.05 },
    { wordStart: 25, wordEnd: 27, start: 7.0, end: 7.9 },
  ];
</script>"""
SPEECH = [{"text": t, "start": s, "end": e} for t, s, e in [
    ("So", 0.0, 0.2), ("this", 0.2, 0.4), ("is", 0.4, 0.5), ("the", 0.5, 0.6), ("story", 0.6, 1.0),
    ("of", 1.0, 1.1), ("it.", 1.1, 1.4),                       # sentence ends, then a pause
    ("Next", 2.6, 2.9), ("part", 2.9, 3.2)]]


def test_words_are_grouped_into_short_phrases_at_pauses_and_sentence_ends():
    groups = cap.group_words(SPEECH, duration=5.0)
    assert [(g["wordStart"], g["wordEnd"]) for g in groups] == [(0, 3), (4, 6), (7, 8)]
    assert groups[0]["start"] == 0.0 and groups[0]["end"] <= groups[1]["start"]      # never two groups at once
    assert groups[-1]["end"] <= 5.0


def test_components_with_hard_coded_word_groups_get_groups_for_the_real_words():
    """Their sample tables index 28 words; left alone, any other word count renders nothing."""
    html = cap.compile_composition(GROUPED, SPEECH, duration=5.0)
    assert "var RAW_GROUPS = [[0, 3], [4, 6], [7, 8]];" in html and "[25, 27]" not in html
    assert ".start : 5;" in html and "8.0" not in html                    # the sample's 8 s fallback
    assert "var GROUPS = RAW_GROUPS.map(" in html                         # derived code is left alone

    html = cap.compile_composition(LITERAL_GROUPS, SPEECH, duration=5.0)
    start = html.index("var GROUPS = ") + len("var GROUPS = ")
    groups = json.loads(html[start:html.index(";", start)])
    assert [(g["wordStart"], g["wordEnd"]) for g in groups] == [(0, 3), (4, 6), (7, 8)]


def test_phrase_cues_become_evenly_spaced_words():
    words = cap.cue_words([{"text": "Hello there world", "start": 3.0, "end": 4.5},
                           {"text": "  ", "start": 5.0, "end": 6.0}, {"text": "late", "start": 7.0, "end": 7.0}])
    assert words == [{"text": "Hello", "start": 3.0, "end": 3.5}, {"text": "there", "start": 3.5, "end": 4.0},
                     {"text": "world", "start": 4.0, "end": 4.5}]
