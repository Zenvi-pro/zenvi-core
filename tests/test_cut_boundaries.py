"""Cuts must not land mid-sentence or mid-musical-phrase.

The reported symptom: the agent placed and trimmed clips straight through
dialogue and through the middle of music. Placement trims previously bypassed
the cue snapping entirely - only slice paths used it.
"""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_qt = MagicMock()
_qt.QObject = object
_qt.QThread = None
_qt.pyqtSignal = lambda *a, **k: MagicMock()
_qt.pyqtSlot = lambda *a, **k: (lambda fn: fn)
_qt.QEventLoop = MagicMock
_qt.QPointF = MagicMock
_qt.QTimer = MagicMock
sys.modules.setdefault("PyQt5.QtCore", _qt)
sys.modules.setdefault("PyQt5.QtWidgets", MagicMock(QApplication=MagicMock))

import pytest  # noqa: E402

from classes import audio_mix as am  # noqa: E402
from classes import tool_handlers  # noqa: E402

SPEECH = {"transcript_cues": [
    {"start": 5.0, "end": 8.0, "text": "hello there"},
    {"start": 12.0, "end": 14.0, "text": "and welcome"},
]}
MUSIC = {"chapters": [
    {"start": 0.0, "end": 16.0, "title": "intro"},
    {"start": 16.0, "end": 48.0, "title": "verse"},
]}


# --------------------------------------------------------------------------
# Speech is an avoid-region; chapters are only a magnet
# --------------------------------------------------------------------------

def test_speech_windows_are_the_avoid_regions():
    assert len(am.speech_windows(SPEECH)) == 2
    assert am.speech_windows(MUSIC) == []


def test_chapter_edges_are_points_not_spans():
    assert am.chapter_edges(MUSIC) == [0.0, 16.0, 48.0]


@pytest.mark.parametrize("meta", [None, {}, {"transcript_cues": None}, "junk", 7])
def test_missing_metadata_yields_nothing(meta):
    assert am.speech_windows(meta) == []
    assert am.chapter_edges(meta) == []


def test_malformed_chapters_are_skipped():
    assert am.chapter_edges({"chapters": ["x", None, {"start": 1.0, "end": 2.0}]}) == [1.0, 2.0]


# --------------------------------------------------------------------------
# The regression this replaced: chapters TILE, so treating them as avoid-regions
# yanked every cut to a scene edge and made mid-chapter cutting impossible.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("start,end", [(10.0, 30.0), (20.0, 40.0), (30.0, 44.0)])
def test_a_mid_chapter_window_is_left_exactly_where_it_was(start, end):
    s, e, moved = am.snap_window_to_boundaries(start, end, MUSIC)
    assert (s, e) == (pytest.approx(start), pytest.approx(end))
    assert moved is False


def test_a_cut_deep_inside_a_chapter_is_never_relocated():
    """Previously 30.0 was dragged to 16.0 - a 14 second move."""
    s, _e, _m = am.snap_window_to_boundaries(30.0, 44.0, MUSIC)
    assert s == pytest.approx(30.0)


def test_a_cut_next_to_a_scene_break_tidies_onto_it():
    s, _e, moved = am.snap_window_to_boundaries(16.3, 40.0, MUSIC)
    assert s == pytest.approx(16.0)
    assert moved is True


def test_the_magnet_does_not_reach_past_its_tolerance():
    s, _e, _m = am.snap_window_to_boundaries(16.0 + am.CHAPTER_MAGNET_SEC + 0.2, 40.0, MUSIC)
    assert s == pytest.approx(16.7)


# --------------------------------------------------------------------------
# Speech: clear a clipped word, respect a deliberate deep cut
# --------------------------------------------------------------------------

def test_a_window_clipping_the_end_of_a_line_is_cleared():
    s, _e, moved = am.snap_window_to_boundaries(7.4, 20.0, SPEECH)
    assert s == pytest.approx(8.0)
    assert moved is True


def test_a_window_ending_mid_sentence_is_cleared():
    _s, e, moved = am.snap_window_to_boundaries(0.0, 13.8, SPEECH)
    assert e == pytest.approx(14.0)
    assert moved is True


def test_a_cut_deep_inside_a_long_line_still_lands_on_a_boundary():
    """The reported bug: ends sat mid-sentence because the shift cap gave up.

    Landing on a boundary always wins over preserving the exact duration - a
    clipped word is what the user called "really bad dialog cutting".
    """
    long_line = {"transcript_cues": [{"start": 0.0, "end": 20.0, "text": "one long take"}]}
    s, e, moved = am.snap_window_to_boundaries(8.0, 12.0, long_line)
    assert moved is True
    for edge in (s, e):
        assert edge in (pytest.approx(0.0), pytest.approx(20.0))


def test_an_out_point_lets_the_phrase_finish():
    """Trailing edge is preferred for an out-point, within the shift cap."""
    cues = {"transcript_cues": [{"start": 0.0, "end": 10.0, "text": "x"}]}
    _s, e, _m = am.snap_window_to_boundaries(0.0, 8.5, cues)
    assert e == pytest.approx(10.0)


def test_an_in_point_opens_on_the_phrase():
    """Leading edge is preferred for an in-point, within the shift cap."""
    cues = {"transcript_cues": [{"start": 5.0, "end": 15.0, "text": "x"}]}
    s, _e, _m = am.snap_window_to_boundaries(6.5, 30.0, cues)
    assert s == pytest.approx(5.0)


def test_no_edge_is_ever_left_inside_a_phrase():
    cues = [{"start": 0.0, "end": 3.2}, {"start": 3.2, "end": 9.0}, {"start": 9.0, "end": 16.5}]
    meta = {"transcript_cues": cues}
    for start, end in [(1.42, 15.58), (1.0, 11.0), (0.5, 16.0), (3.9, 12.7)]:
        s, e, _m = am.snap_window_to_boundaries(start, end, meta)
        for edge in (s, e):
            assert not any(c["start"] < edge < c["end"] for c in cues), (
                f"{edge} from [{start},{end}] is mid-phrase"
            )


def test_the_shift_cap_chooses_the_nearer_edge_not_a_mid_word_cut():
    """Past the cap the preferred edge is dropped, but a boundary still wins."""
    assert am.MAX_CUE_SHIFT_SEC == 2.0
    cues = {"transcript_cues": [{"start": 0.0, "end": 10.0, "text": "x"}]}
    # 8.5 is 1.5s from the trailing edge: inside the cap, phrase finishes.
    assert am.snap_window_to_boundaries(0.0, 8.5, cues)[1] == pytest.approx(10.0)
    # Both edges share the phrase and would collapse onto 0.0 - widen instead
    # of giving up, which used to leave both cuts mid-word.
    s2, e2, moved = am.snap_window_to_boundaries(1.0, 4.0, cues)
    assert (s2, e2) == (pytest.approx(0.0), pytest.approx(10.0))
    assert moved is True


def test_speech_and_chapters_compose():
    both = {**SPEECH, **MUSIC}
    s, _e, moved = am.snap_window_to_boundaries(7.4, 40.0, both)
    assert s == pytest.approx(8.0)
    assert moved is True


def test_a_window_inside_one_phrase_widens_to_that_phrase():
    """Both edges would collapse onto one boundary; widening keeps them clean."""
    cues = {"transcript_cues": [{"start": 0.0, "end": 100.0, "text": "one long take"}]}
    s, e, moved = am.snap_window_to_boundaries(10.0, 11.0, cues)
    assert (s, e) == (pytest.approx(0.0), pytest.approx(100.0))
    assert moved is True


def test_an_unsnappable_window_is_returned_unchanged():
    """Degenerate cue data must not produce an inverted window."""
    cues = {"transcript_cues": [{"start": 5.0, "end": 5.0}]}
    assert am.snap_window_to_boundaries(10.0, 11.0, cues) == (10.0, 11.0, False)


def test_no_metadata_means_no_change():
    assert am.snap_window_to_boundaries(1.0, 2.0, {}) == (1.0, 2.0, False)


def test_an_inverted_input_window_is_left_alone():
    assert am.snap_window_to_boundaries(5.0, 5.0, SPEECH)[2] is False


# --------------------------------------------------------------------------
# Wired into placement - the path the agent actually uses
# --------------------------------------------------------------------------

def test_placement_snaps_off_a_mid_sentence_edge():
    file_data = {"path": "/m/a.mp4", "ai_metadata": SPEECH}
    start, end, moved = tool_handlers._snap_window_off_boundaries(file_data, 5.4, 20.0)
    assert start == pytest.approx(5.0)
    assert moved is True


def test_placement_tidies_onto_a_nearby_music_section_edge():
    file_data = {"path": "/m/a.mp3", "ai_metadata": MUSIC}
    start, _end, moved = tool_handlers._snap_window_off_boundaries(file_data, 16.3, 48.0)
    assert start == pytest.approx(16.0)
    assert moved is True


def test_placement_keeps_a_deliberate_mid_section_cut():
    file_data = {"path": "/m/a.mp3", "ai_metadata": MUSIC}
    start, _end, moved = tool_handlers._snap_window_off_boundaries(file_data, 30.0, 48.0)
    assert start == pytest.approx(30.0)
    assert moved is False


def test_placement_without_metadata_is_unchanged():
    start, end, moved = tool_handlers._snap_window_off_boundaries({"path": "/m/a.mp4"}, 5.4, 20.0)
    assert (start, end, moved) == (5.4, 20.0, False)


def test_placement_snap_never_raises_on_bad_metadata():
    bad = {"path": "/m/a.mp4", "ai_metadata": {"transcript_cues": "not a list"}}
    assert tool_handlers._snap_window_off_boundaries(bad, 5.4, 20.0) == (5.4, 20.0, False)


# --------------------------------------------------------------------------
# The watch width gate
# --------------------------------------------------------------------------

def _watch(win_s, win_e, matched=True):
    client = MagicMock()
    client.watch_window.return_value = {
        "matched": matched, "used_fallback": not matched, "cut_source": win_s + 1.0,
        "in_source": win_s + 0.5, "out_source": win_e - 0.5, "confidence": 0.9,
    }
    from classes import watch_frames as real_wf
    with patch.dict(sys.modules, {
        "classes.api_client": MagicMock(get_backend_client=lambda: client),
        "classes.watch_frames": MagicMock(
            WATCH_MIN_WINDOW_SEC=real_wf.WATCH_MIN_WINDOW_SEC,
            extract_watch_frames=lambda *a, **k: ([{"timestamp": 1.0, "image_base64": "x"}], ""),
        ),
    }):
        out = tool_handlers._watch_source_window("/m/a.mp4", win_s, win_e, "q")
    return out, client


def test_a_narrow_window_is_not_watched():
    """The index window is already tight - six stills are a weaker answer."""
    out, client = _watch(10.0, 13.0)
    assert client.watch_window.call_count == 0
    assert out["matched"] is False
    assert "threshold" in out["reason"]


def test_a_wide_window_is_watched():
    out, client = _watch(10.0, 40.0)
    assert client.watch_window.call_count == 1
    assert out["matched"] is True


def test_the_threshold_is_the_documented_one():
    from classes.watch_frames import WATCH_MIN_WINDOW_SEC

    assert WATCH_MIN_WINDOW_SEC == 8.0
    assert _watch(0.0, WATCH_MIN_WINDOW_SEC - 0.1)[1].watch_window.call_count == 0
    assert _watch(0.0, WATCH_MIN_WINDOW_SEC + 0.1)[1].watch_window.call_count == 1
