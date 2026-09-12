"""Audio must be placed as a SECTION, not stretched over the whole timeline.

add_clip_to_timeline used to default an audio file to position 0 with no trim,
so every "add music" landed one full-length bed under everything - which is why
the mix always came out the same.
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

from classes import tool_handlers  # noqa: E402

MUSIC_LAYER = 1000000


def _music_file(path="/media/theme.mp3", duration=180.0):
    return {
        "id": "F_MUSIC",
        "path": path,
        "media_type": "audio",
        "fps": {"num": 1, "den": 1},
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "has_audio": True,
        "has_video": True,  # cover art, as libopenshot reports it
    }


def _video_file(duration=60.0):
    return {
        "id": "F_VIDEO",
        "path": "/media/broll.mp4",
        "media_type": "video",
        "fps": {"num": 30, "den": 1},
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "has_audio": True,
        "has_video": True,
    }


def _mock_app():
    app = MagicMock()
    project = {
        "fps": {"num": 30, "den": 1},
        "layers": [{"number": MUSIC_LAYER, "id": "L1", "label": "Music", "lock": False}],
    }
    app.project.get.side_effect = lambda key, default=None: project.get(key, default)
    app.window.selected_tracks = []
    return app


def _place(file_data, **kwargs):
    """Run add_clip_to_timeline; return (result, the clip dict addClip produced)."""
    app = _mock_app()
    placed = {}

    class _Point:
        """Stand-in for QPointF so the placement position is observable."""

        def __init__(self, x, y):
            self._x = x

        def x(self):
            return self._x

    def _add_clip(file_id, pos, track_num):
        placed.update(
            {"file_id": file_id, "layer": track_num, "position": pos.x(),
             "start": 0.0, "end": 0.0}
        )
        return placed

    app.window.timeline.addClip.side_effect = _add_clip

    file_obj = MagicMock()
    file_obj.data = file_data
    query = MagicMock()
    query.File.get.return_value = file_obj
    query.Clip.filter.return_value = []
    query.Track.get.return_value = None

    with patch.object(tool_handlers, "QThread", None), \
            patch.object(tool_handlers, "QPointF", _Point), \
            patch.object(tool_handlers, "_get_app", return_value=app), \
            patch.object(tool_handlers, "format_track_label_for_llm", return_value="Music"), \
            patch.dict(sys.modules, {"classes.query": query}):
        result = tool_handlers.add_clip_to_timeline(file_id="F_MUSIC", **kwargs)
    return result, placed


# --------------------------------------------------------------------------
# Audio without a section is refused
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"position_seconds": "20"},                       # no duration
        {"duration_seconds": "20"},                       # no position
        {"start_seconds": "8"},                           # in-point only
    ],
    ids=["nothing", "position-only", "duration-only", "start-only"],
)
def test_audio_without_a_section_is_refused(kwargs):
    result, placed = _place(_music_file(), **kwargs)
    assert result.startswith("Error:")
    assert "position_seconds" in result and "duration_seconds" in result
    assert placed == {}, "nothing should have been placed"


def test_the_refusal_names_the_escape_hatch():
    result, _ = _place(_music_file())
    assert "full_file" in result


@pytest.mark.parametrize("flag", ["true", "TRUE", "yes", "1"])
def test_full_file_places_a_deliberate_continuous_bed(flag):
    result, placed = _place(_music_file(), full_file=flag)
    assert not result.startswith("Error"), result
    assert placed["position"] == pytest.approx(0.0)


def test_a_complete_section_is_placed_with_its_source_in_point():
    result, placed = _place(
        _music_file(), start_seconds="8", duration_seconds="4", position_seconds="20"
    )
    assert not result.startswith("Error"), result
    assert placed["start"] == pytest.approx(8.0)
    assert placed["end"] == pytest.approx(12.0)
    assert placed["position"] == pytest.approx(20.0)


def test_two_sections_of_one_file_are_independent():
    _r1, first = _place(
        _music_file(), start_seconds="8", duration_seconds="4", position_seconds="0"
    )
    _r2, second = _place(
        _music_file(), start_seconds="40", duration_seconds="20", position_seconds="20"
    )
    assert (first["start"], first["end"]) != (second["start"], second["end"])
    assert first["position"] != second["position"]


def test_a_section_is_clamped_to_the_source_length():
    result, placed = _place(
        _music_file(duration=30.0),
        start_seconds="20", duration_seconds="600", position_seconds="0",
    )
    assert not result.startswith("Error"), result
    assert placed["end"] == pytest.approx(30.0)


# --------------------------------------------------------------------------
# Video is untouched by the new rule
# --------------------------------------------------------------------------

def test_video_still_appends_without_explicit_args():
    result, placed = _place(_video_file())
    assert not result.startswith("Error"), result
    assert placed != {}


# --------------------------------------------------------------------------
# Unparseable times never surface as `could not convert string to float`
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["12s ish", "end of last clip", "auto"])
def test_bad_position_names_the_value(bad):
    result, placed = _place(
        _music_file(), position_seconds=bad, duration_seconds="4", start_seconds="0"
    )
    assert result.startswith("Error:")
    assert "position_seconds" in result
    assert "could not convert string to float" not in result
    assert placed == {}


def test_a_timecode_position_is_accepted():
    result, placed = _place(
        _music_file(), position_seconds="1:00", duration_seconds="4", start_seconds="0"
    )
    assert not result.startswith("Error"), result
    assert placed["position"] == pytest.approx(60.0)


def test_bad_duration_names_the_value():
    result, _ = _place(
        _music_file(), position_seconds="0", duration_seconds="a while", start_seconds="0"
    )
    assert result.startswith("Error:")
    assert "duration_seconds" in result
