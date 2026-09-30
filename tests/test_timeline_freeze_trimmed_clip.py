"""Speed > Reset / Freeze on a trimmed clip must key the freeze against the
clip's absolute trim end, not its duration (OpenShot #6043). Real-Qt test."""

from __future__ import annotations

import copy
import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PyQt5.QtWidgets")
openshot = pytest.importorskip("openshot")

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402


class _TestApp(QApplication):
    """Minimal stand-in for classes.app.OpenShotApp at module import time."""

    def _tr(self, text):
        return text


@pytest.fixture(scope="module")
def timeline_module():
    # qt_api imports QtWebEngine, which Qt requires before any QApplication exists.
    importlib.import_module("qt_api")
    app = QApplication.instance()
    if app is None:
        app = _TestApp([])
    elif not hasattr(app, "_tr"):
        app._tr = lambda text: text
    return importlib.import_module("windows.views.timeline")


def _make_time_helper(timeline_module):
    class Helper:
        def __init__(self):
            self.window = types.SimpleNamespace(
                timeline_sync=types.SimpleNamespace(timeline=types.SimpleNamespace(GetClip=lambda _id: None))
            )
            self.updated = []

        def get_uuid(self):
            return "tx-1"

        def AddPoint(self, keyframe, new_point):
            return timeline_module.TimelineView.AddPoint(self, keyframe, new_point)

        def update_clip_data(self, clip_data, **_kwargs):
            self.updated.append(copy.deepcopy(clip_data))

        def Show_Waveform_Triggered(self, clip_ids, transaction_id=None):
            self.updated.append({"waveform_refresh": list(clip_ids), "transaction_id": transaction_id})

    return Helper()


def test_freeze_uses_absolute_trim_end_for_right_side_split_clip(timeline_module):
    helper = _make_time_helper(timeline_module)
    clip = types.SimpleNamespace(
        id="C1",
        data={
            "id": "C1",
            "position": 10.0,
            "start": 10.0,
            "end": 52.0,
            "duration": 42.0,
            "time": {"Points": [{"co": {"X": 1, "Y": 1}, "interpolation": openshot.LINEAR}]},
            "volume": {"Points": [{"co": {"X": 1, "Y": 1.0}, "interpolation": openshot.LINEAR}]},
            "ui": {},
        },
    )
    app = types.SimpleNamespace(
        project=types.SimpleNamespace(get=lambda key: {"num": 30, "den": 1} if key == "fps" else None),
        updates=types.SimpleNamespace(apply_last_action_to_history=lambda _data: None),
    )

    with patch.object(timeline_module.Clip, "get", return_value=clip), \
            patch.object(timeline_module, "get_app", return_value=app):
        timeline_module.TimelineView.Time_Triggered(
            helper, timeline_module.MenuTime.FREEZE, ["C1"], 8, 10.0)

    assert clip.data["end"] == 60.0
    assert clip.data["duration"] == 50.0
    # Last freeze keyframe maps the new clip end (frame 1801) back to the
    # source frame at the original trim end (52 s -> frame 1561), not the
    # duration (42 s -> frame 1261) which froze the wrong frame.
    assert {"co": {"X": 1801.0, "Y": 1561.0}, "interpolation": openshot.LINEAR} in clip.data["time"]["Points"]
