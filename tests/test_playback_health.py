"""Smooth playback after a big edit: preview copies of heavy footage, and a warning about tracks stacked underneath."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from classes.editor_tools import playback_health as ph  # noqa: E402


class FakeProxyService:
    def __init__(self):
        self.created = []

    def create_for_files(self, files):
        self.created.extend(f.id for f in files)

    def get_proxy_state(self, f):
        return "none"


def _service(editor, monkeypatch):
    service = FakeProxyService()
    monkeypatch.setattr(editor.app.window, "proxy_service", service, raising=False)
    monkeypatch.setattr(ph, "on_main", lambda func, *a, **kw: func(*a))
    return service


def test_heavy_videos_get_preview_copies_light_ones_do_not(editor, monkeypatch):
    service = _service(editor, monkeypatch)
    heavy = editor.add_file("video", duration=5.0, path="/m/a.mp4", height=1080, vcodec="h264")
    phone = editor.add_file("video", duration=5.0, path="/m/b.mov", height=720, vcodec="hevc")
    light = editor.add_file("video", duration=5.0, path="/m/c.mp4", height=480, vcodec="h264")
    monkeypatch.setattr("classes.editor_tools.media_files.media_missing", lambda data: False)
    note = ph.start_preview_copies([heavy, phone, light, heavy], clip_count=40)
    assert sorted(service.created) == sorted([heavy, phone]) and "2 heavy video" in note


def test_a_small_edit_starts_nothing(editor, monkeypatch):
    service = _service(editor, monkeypatch)
    heavy = editor.add_file("video", duration=5.0, path="/m/a.mp4", height=1080)
    assert ph.start_preview_copies([heavy], clip_count=3) == "" and service.created == []


def test_a_failure_to_start_copies_never_raises(editor, monkeypatch):
    service = _service(editor, monkeypatch)
    heavy = editor.add_file("video", duration=5.0, path="/m/a.mp4", height=1080)
    monkeypatch.setattr(service, "create_for_files", lambda files: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert ph.start_preview_copies([heavy], clip_count=40) == ""


def test_other_tracks_under_a_new_edit_are_named(editor):
    a = editor.add_file("video", duration=10.0, path="/m/a.mp4")
    editor.add_clip(a, position=0.0, layer=1000000, end=10.0)
    editor.add_clip(a, position=2.0, layer=2000000, end=4.0)
    editor.add_clip(a, position=50.0, layer=3000000, end=4.0)          # outside the stretch
    note = ph.stacked_tracks_note(layer=2000000, start=0.0, end=10.0)
    assert "(1 clip(s))" in note and "slows playback" in note
    assert ph.stacked_tracks_note(layer=1000000, start=60.0, end=70.0) == ""
