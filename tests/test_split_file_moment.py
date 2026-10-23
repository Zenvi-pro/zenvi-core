"""split_file_add_clip cuts its subclip at the index's best hit for the query, and keeps the window it was given when
there is none. A dialogue window and an explicit time skip the lookup."""

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

from classes import tool_handlers  # noqa: E402


def _hit(start, end, peak=None):
    return {"file_id": "parent", "start": start, "end": end,
            "peak": (start + end) / 2.0 if peak is None else peak, "why": ""}


def _split(*, start, end, query, moment, duration=30.0, cues=()):
    """split_file_add_clip(start, end) on a parent whose transcript has *cues*; *moment* is what the index answers.

    Returns (out, the lookups made, the subclips saved).
    """
    lookups, saved = [], []

    def fake_moment(query, file_id=None, window=None):
        lookups.append((float(window[0]), float(window[1]), query))
        return moment

    parent = MagicMock()
    parent.data = {
        "fps": {"num": 30, "den": 1},
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "path": "/v.mp4",
        "name": "v.mp4",
        "ai_metadata": {"transcript_cues": list(cues)},
    }

    class FakeFile:
        def __init__(self):
            self.data = {}
            self.id = None
            self.key = None
            self.type = None

        def save(self):
            self.id = "new-sub"
            saved.append(dict(self.data))

        @staticmethod
        def get(id=""):
            return parent

    query_mod = MagicMock()
    query_mod.File = FakeFile
    with patch.object(tool_handlers, "_v2_moment", fake_moment):
        with patch.dict(sys.modules, {"classes.query": query_mod}):
            out = tool_handlers.split_file_add_clip(
                file_id="parent", start_seconds=start, end_seconds=end, query=query, name="handshake",
            )
    return out, lookups, saved


def test_split_file_add_clip_cuts_at_the_moment_the_index_finds():
    out, lookups, saved = _split(start="12", end="18", query="handshake", moment=_hit(12.5, 16.0, peak=14.0))
    assert lookups == [(12.0, 18.0, "handshake")], out
    assert saved and saved[0]["start"] == 12.5 and saved[0]["end"] == 16.0, out
    assert "new-sub" in out
    assert "duration_seconds" in out


def test_split_explicit_time_skips_the_lookup():
    out, lookups, saved = _split(start="4", end="10", query="from 4 seconds to 10 seconds", moment=_hit(0.0, 1.0))
    assert lookups == []
    assert saved and saved[0]["start"] == 4.0 and saved[0]["end"] == 10.0, out
    assert "new-sub" in out


def test_split_wide_window_with_no_moment_is_refused():
    out, _lookups, saved = _split(start="0", end="40", query="the moment", moment=None, duration=60.0)
    assert out.startswith("Error:")
    assert "visual match" in out.lower()
    assert saved == []


def test_split_snaps_a_moment_off_mid_sentence():
    """The index's edges are shot cuts, not words - the in-point must still open on a phrase."""
    out, lookups, saved = _split(start="12", end="18", query="handshake", moment=_hit(12.5, 16.0),
                                 cues=[{"start": 11.0, "end": 13.0, "text": "a spoken line"}])
    assert lookups, out
    assert saved, out
    assert saved[0]["start"] == 11.0
    assert saved[0]["end"] == 16.0


def test_split_skips_the_lookup_on_a_dialogue_window_and_cuts_on_phrases():
    """Stills of a talking head echo the span back - the transcript is the better edge."""
    out, lookups, saved = _split(start="12", end="18", query="handshake", moment=_hit(15.0, 17.0),
                                 cues=[{"start": 11.0, "end": 19.0, "text": "one long spoken line"}])
    assert lookups == []
    assert saved, out
    assert saved[0]["start"] == 11.0
    assert saved[0]["end"] == 19.0
