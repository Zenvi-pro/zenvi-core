"""place_moment sends seconds; the desktop handler used to read only frames.

The result was a silently-missing trim plus an "Error: Frames are 1-based." that
the agent answered by converting seconds with the file's own fps - which is 1/1
for audio and some broken imports, so it looped forever.
"""

import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub Qt before importing tool_handlers (headless).
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


class _FakeFile:
    def __init__(self, file_id, data):
        self.id = file_id
        self.key = None
        self.type = None
        self.data = data
        self.saved = False

    def save(self):
        self.saved = True
        if self.id is None:
            self.id = "SUB1"


def _video_file(fps_num=30, fps_den=1, duration=180.0, path="/media/interview.mp4"):
    return {
        "id": "F1",
        "path": path,
        "fps": {"num": fps_num, "den": fps_den},
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "video_length": int(duration * (fps_num / fps_den)),
        "has_video": True,
        "has_audio": True,
        "media_type": "video",
    }


def _one_fps_audio_file(duration=180.0):
    """The looping case: an MP3 whose reader reports 1 fps."""
    return {
        "id": "F1",
        "path": "/media/theme.mp3",
        "fps": {"num": 1, "den": 1},
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "video_length": int(duration),
        "has_video": True,  # cover art
        "has_audio": True,
        "media_type": "audio",
    }


def _call(file_data, **kwargs):
    """Run split_file_add_clip against a fake project file, return (result, new_file)."""
    created = []
    source = _FakeFile("F1", file_data)

    def _new_file():
        f = _FakeFile(None, {})
        created.append(f)
        return f

    query = MagicMock()
    query.File = MagicMock(side_effect=_new_file)
    query.File.get = MagicMock(return_value=source)
    with patch.dict(sys.modules, {"classes.query": query}):
        result = tool_handlers.split_file_add_clip(file_id="F1", **kwargs)
    return result, (created[0] if created else None)


# --------------------------------------------------------------------------
# Seconds path
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fps_num,fps_den",
    [(30, 1), (30000, 1001), (1, 1), (25, 1)],
    ids=["30fps", "29.97fps", "1fps", "25fps"],
)
def test_seconds_window_is_fps_independent(fps_num, fps_den):
    """10s-20s must be 10 seconds of media whatever the file claims its fps is."""
    result, new_file = _call(
        _video_file(fps_num=fps_num, fps_den=fps_den), start_seconds="10", end_seconds="20"
    )
    assert not result.startswith("Error"), result
    assert new_file is not None
    assert new_file.data["start"] == pytest.approx(10.0)
    assert new_file.data["end"] == pytest.approx(20.0)
    assert new_file.data["end"] - new_file.data["start"] == pytest.approx(10.0)


def test_one_fps_audio_file_places_seconds_not_frames():
    """The exact reported loop: audio at 1 fps, a 10-second keep window."""
    result, new_file = _call(_one_fps_audio_file(), start_seconds="10", end_seconds="20")
    assert not result.startswith("Error"), result
    assert new_file.data["end"] - new_file.data["start"] == pytest.approx(10.0)


def test_seconds_are_offset_by_the_parents_own_in_point():
    data = _video_file()
    data["start"] = 5.0
    data["end"] = 65.0
    result, new_file = _call(data, start_seconds="10", end_seconds="20")
    assert not result.startswith("Error"), result
    assert new_file.data["start"] == pytest.approx(15.0)
    assert new_file.data["end"] == pytest.approx(25.0)


def test_seconds_window_is_clamped_to_the_source_length():
    result, new_file = _call(
        _video_file(duration=30.0), start_seconds="20", end_seconds="900"
    )
    assert not result.startswith("Error"), result
    assert new_file.data["end"] == pytest.approx(30.0)


def test_timecode_and_unit_suffixed_seconds_are_accepted():
    result, new_file = _call(_video_file(), start_seconds="0:10", end_seconds="20s")
    assert not result.startswith("Error"), result
    assert new_file.data["start"] == pytest.approx(10.0)
    assert new_file.data["end"] == pytest.approx(20.0)


def test_result_string_reports_seconds_not_frames():
    result, _ = _call(_video_file(), start_seconds="10", end_seconds="20")
    assert "10.00s to 20.00s" in result
    assert "frames" not in result


def test_an_explicit_range_in_the_query_is_used_when_seconds_are_absent():
    result, new_file = _call(_video_file(), query="from 4 seconds to 10 seconds")
    assert not result.startswith("Error"), result
    assert new_file.data["start"] == pytest.approx(4.0)
    assert new_file.data["end"] == pytest.approx(10.0)


# --------------------------------------------------------------------------
# Bad input becomes an Error string, never a raw ValueError
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["soon", "end of last clip", "auto"])
def test_unparseable_seconds_name_the_value(bad):
    result, new_file = _call(_video_file(), start_seconds=bad, end_seconds="20")
    assert result.startswith("Error:")
    assert repr(bad) in result
    assert new_file is None


def test_inverted_window_is_rejected():
    result, _ = _call(_video_file(), start_seconds="20", end_seconds="10")
    assert result.startswith("Error:")
    assert "greater than" in result


def test_start_beyond_the_file_is_rejected_with_the_duration():
    result, _ = _call(_video_file(duration=30.0), start_seconds="120", end_seconds="130")
    assert result.startswith("Error:")
    assert "duration_seconds=30.00" in result


def test_start_without_end_is_rejected():
    result, _ = _call(_video_file(), start_seconds="10")
    assert result.startswith("Error:")
    assert "end_seconds" in result


# --------------------------------------------------------------------------
# Frame path: still works, but refuses when the file's fps is a lie
# --------------------------------------------------------------------------

def test_frame_path_still_works_for_a_sane_video():
    result, new_file = _call(_video_file(), start_frame=301, end_frame=600)
    assert not result.startswith("Error"), result
    assert new_file.data["start"] == pytest.approx(10.0)
    assert new_file.data["end"] == pytest.approx(20.0)


def test_frame_path_refuses_a_one_fps_file_instead_of_looping():
    result, new_file = _call(_one_fps_audio_file(), start_frame=10, end_frame=20)
    assert result.startswith("Error:")
    assert "source_fps=1/1" in result
    assert "start_seconds" in result
    assert new_file is None


def test_missing_window_asks_for_seconds_not_frames():
    result, _ = _call(_video_file())
    assert result.startswith("Error:")
    # Seconds must lead; frames are only mentioned as the legacy fallback.
    assert result.index("start_seconds") < result.index("frames")
