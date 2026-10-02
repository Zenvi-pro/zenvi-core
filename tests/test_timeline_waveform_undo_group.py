"""An audio clip's automatic waveform joins the undo step that placed the clip.

Found live (ai-generation): after a tool placed narration, the waveform saved a
moment later from the worker was its own undo step, so the first Ctrl+Z (and
undo_tool) changed nothing visible. TimelineView.addClip requested it without
the caller's transaction id.
"""

import ast
import os
from types import SimpleNamespace

import pytest

_TIMELINE = os.path.join(os.path.dirname(__file__), "..", "src", "windows", "views", "timeline.py")


def _method(name, namespace):
    with open(_TIMELINE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), _TIMELINE)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TimelineView")
    func = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[func], type_ignores=[]), _TIMELINE, "exec"), namespace)
    return namespace[name]


class _View:
    def __init__(self):
        self.requests = []

    def Show_Waveform_Triggered(self, clip_ids, transaction_id=None):
        self.requests.append((clip_ids, transaction_id))


@pytest.fixture
def waveform_new_audio_clip():
    updates = SimpleNamespace(transaction_id=None)
    method = _method("_waveform_new_audio_clip", {"get_app": lambda: SimpleNamespace(updates=updates)})
    return method, updates


def test_the_waveform_request_carries_the_callers_transaction(waveform_new_audio_clip):
    method, updates = waveform_new_audio_clip
    updates.transaction_id = "tool-call-tid"
    view = _View()
    method(view, {"id": "C1", "reader": {"has_audio": True, "has_video": False}})
    assert view.requests == [(["C1"], "tool-call-tid")]


def test_without_a_transaction_the_request_is_unchanged(waveform_new_audio_clip):
    method, _updates = waveform_new_audio_clip
    view = _View()
    method(view, {"id": "C1", "reader": {"has_audio": True, "has_video": False}})
    assert view.requests == [(["C1"], None)]


def test_video_clips_get_no_automatic_waveform(waveform_new_audio_clip):
    method, updates = waveform_new_audio_clip
    updates.transaction_id = "tid"
    view = _View()
    method(view, {"id": "C2", "reader": {"has_audio": True, "has_video": True}})
    method(view, {"id": "C3", "reader": {"has_audio": False, "has_video": False}})
    assert view.requests == []
