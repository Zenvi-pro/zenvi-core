"""A slice must not land in the middle of a spoken line.

The reported case: the agent sliced a clip through the middle of a sentence, and
the follow-up conversation to fix it went nowhere.
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

# "hello there" runs 5.0-8.0s, "and welcome" runs 12.0-14.0s of the source.
CUES = [
    {"start": 5.0, "end": 8.0, "text": "hello there"},
    {"start": 12.0, "end": 14.0, "text": "and welcome"},
]


# --------------------------------------------------------------------------
# The pure snap
# --------------------------------------------------------------------------

def test_a_cut_just_after_a_line_starts_snaps_back_to_its_start():
    cut, cue = am.snap_cut_out_of_speech(5.4, CUES)
    assert cut == pytest.approx(5.0)
    assert cue["text"] == "hello there"


def test_a_cut_near_the_end_of_a_line_snaps_forward():
    cut, cue = am.snap_cut_out_of_speech(7.6, CUES)
    assert cut == pytest.approx(8.0)
    assert cue is not None


def test_the_midpoint_snaps_backwards():
    """Ties go to the start, so the whole line stays on the later half."""
    cut, _cue = am.snap_cut_out_of_speech(6.5, CUES)
    assert cut == pytest.approx(5.0)


@pytest.mark.parametrize("cut", [0.0, 4.9, 9.0, 11.0, 15.0, 100.0])
def test_a_cut_in_a_gap_is_left_alone(cut):
    snapped, cue = am.snap_cut_out_of_speech(cut, CUES)
    assert snapped == pytest.approx(cut)
    assert cue is None


@pytest.mark.parametrize("cut", [5.0, 8.0, 12.0, 14.0])
def test_a_cut_exactly_on_a_boundary_is_already_clean(cut):
    snapped, cue = am.snap_cut_out_of_speech(cut, CUES)
    assert snapped == pytest.approx(cut)
    assert cue is None


def test_the_second_line_is_honoured_too():
    cut, cue = am.snap_cut_out_of_speech(13.8, CUES)
    assert cut == pytest.approx(14.0)
    assert cue["text"] == "and welcome"


def test_no_cues_means_no_change():
    assert am.snap_cut_out_of_speech(6.0, [])[0] == pytest.approx(6.0)
    assert am.snap_cut_out_of_speech(6.0, None)[0] == pytest.approx(6.0)


def test_rebased_cue_keys_are_understood():
    """materialize_clip_ai_metadata emits source_start/source_end as well."""
    cues = [{"source_start": 5.0, "source_end": 8.0, "start": 0.0, "end": 3.0}]
    assert am.snap_cut_out_of_speech(5.4, cues)[0] == pytest.approx(5.0)


def test_malformed_cues_are_skipped_not_fatal():
    cues = [{"start": None, "end": 3.0}, {"start": 8.0, "end": 5.0}, "junk", None]
    assert am.snap_cut_out_of_speech(6.0, cues) == (6.0, None)


def test_max_shift_declines_a_move_that_is_too_disruptive():
    cut, cue = am.snap_cut_out_of_speech(6.5, CUES, max_shift=0.5)
    assert cut == pytest.approx(6.5)
    assert cue is None


# --------------------------------------------------------------------------
# Wired into the handler
# --------------------------------------------------------------------------

def _clip_data():
    return {
        "id": "C1",
        "file_id": "F1",
        "position": 0.0,
        "start": 0.0,
        "end": 30.0,
        "layer": 1,
    }


def _patched(cues):
    """Patch the clip/file lookup so _snap_cut_off_speech sees *cues*."""
    clip_obj = MagicMock()
    clip_obj.data = _clip_data()
    file_obj = MagicMock()
    file_obj.data = {
        "media_type": "video",
        "ai_metadata": {"analyzed": True, "transcript_cues": cues},
    }
    query = MagicMock()
    query.Clip.get.return_value = clip_obj
    query.File.get.return_value = file_obj
    return patch.dict(sys.modules, {"classes.query": query})


def test_the_handler_helper_snaps_a_mid_sentence_cut():
    with _patched(CUES):
        cut, moved = tool_handlers._snap_cut_off_speech("C1", 5.4)
    assert cut == pytest.approx(5.0)
    assert moved is True


def test_the_handler_helper_leaves_a_clean_cut_alone():
    with _patched(CUES):
        cut, moved = tool_handlers._snap_cut_off_speech("C1", 10.0)
    assert cut == pytest.approx(10.0)
    assert moved is False


def test_an_unindexed_clip_is_never_moved():
    with _patched([]):
        cut, moved = tool_handlers._snap_cut_off_speech("C1", 5.4)
    assert cut == pytest.approx(5.4)
    assert moved is False


def test_a_lookup_failure_degrades_to_the_original_cut():
    query = MagicMock()
    query.Clip.get.side_effect = RuntimeError("no project")
    with patch.dict(sys.modules, {"classes.query": query}):
        cut, moved = tool_handlers._snap_cut_off_speech("C1", 5.4)
    assert cut == pytest.approx(5.4)
    assert moved is False
