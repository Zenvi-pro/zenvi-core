"""Fakes for the titles-text editor-tool tests.

The headless suite stubs ``openshot`` as an empty module and the main window
as a MagicMock. These stand-ins give the tools what the running editor gives
them: libopenshot's effect and clip factories (returning the libopenshot 1.0
JSON captured in tests/fixtures/editor_tools/), ``Timeline.addClip`` /
``update_clip_data`` built the way the real ones build clips, and
``files_model.add_files``. Paths the tools write to (title, blender) point at
the test's tmp_path.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest


class FakeEffect:
    def __init__(self, data):
        self._data = data

    def Id(self, value):
        self._data["id"] = value

    def Json(self):
        return json.dumps(self._data)


class FakeReader:
    def __init__(self, data):
        self._data = data

    def Json(self):
        return json.dumps(self._data)


def _image_reader_data(editor, path):
    data = copy.deepcopy(editor._fixtures["files"]["image"])
    data["path"] = path
    if path.lower().endswith(".svg"):
        data.update(width=1920, height=1080, type="QtImageReader")
    return data


def install_openshot_fakes(editor, monkeypatch, missing_effects=()):
    mod = sys.modules["openshot"]
    fixtures = editor._fixtures["effects"]

    class EffectInfo:
        def CreateEffect(self, name):
            if name in missing_effects or name not in fixtures:
                return None
            return FakeEffect(copy.deepcopy(fixtures[name]))

    class Clip:
        def __init__(self, path):
            self.path = path

        def Reader(self):
            if not os.path.exists(self.path):
                raise RuntimeError("file not found: %s" % self.path)
            return FakeReader(_image_reader_data(editor, self.path))

        def Json(self):
            return json.dumps(copy.deepcopy(editor._fixtures["clip"]))

    monkeypatch.setattr(mod, "EffectInfo", EffectInfo, raising=False)
    monkeypatch.setattr(mod, "Clip", Clip, raising=False)


class FakeTimeline:
    """Timeline.addClip / update_clip_data, building clips the way the real ones do."""

    def __init__(self, editor):
        self.editor = editor
        self.add_calls = []

    def addClip(self, file_id, position, track, ignore_refresh=False, call_manual_move=True,
                auto_transition=False):
        from classes.query import Clip, File, Track
        self.add_calls.append({"file_id": file_id, "position": position.x(), "track": track,
                               "call_manual_move": call_manual_move})
        f = File.get(id=file_id)
        if not f:
            return None
        if not any(int(t.get("number") or 0) == int(track) for t in self.editor.store._data["layers"]):
            t = Track()
            t.data = {"number": int(track), "y": 0, "label": "", "lock": False}
            t.save()
        data = copy.deepcopy(self.editor._fixtures["clip"])
        data.pop("id", None)
        duration = float(f.data.get("duration") or 10.0)
        if f.data.get("media_type") == "image" or f.data.get("has_single_image"):
            duration = float(self.editor.settings["default-image-length"])
        data.update({
            "file_id": file_id,
            "title": f.data.get("name", os.path.basename(f.data.get("path", "clip"))),
            "reader": copy.deepcopy(f.data),
            "layer": int(track),
            "position": float(position.x()),
            "start": 0.0,
            "end": duration,
            "duration": duration,
            "effects": [],
        })
        c = Clip()
        c.data = data
        c.save()
        return copy.deepcopy(Clip.get(id=c.id).data)

    def update_clip_data(self, clip_json, only_basic_props=True, ignore_reader=False, ignore_refresh=False,
                         transaction_id=None):
        from classes.query import Clip
        c = Clip.get(id=clip_json["id"])
        c.data = copy.deepcopy(clip_json)
        c.save()


class FakeFilesModel:
    """files_model.add_files: one File per path (image, or an image sequence when details are given)."""

    def __init__(self, editor):
        self.editor = editor
        self.calls = []

    def add_files(self, files, image_seq_details=None, quiet=False, prevent_image_seq=False,
                  prevent_recent_folder=False, skip_indexing=False):
        from classes.query import File
        if not isinstance(files, (list, tuple)):
            files = [files]
        self.calls.append({"files": list(files), "image_seq_details": image_seq_details, "quiet": quiet,
                           "prevent_image_seq": prevent_image_seq, "skip_indexing": skip_indexing})
        out = []
        for path in files:
            existing = File.get(path=path)
            if existing:
                out.append(existing)
                continue
            if image_seq_details:
                data = copy.deepcopy(self.editor._fixtures["files"]["video"])
                data.update(path=path, media_type="video", has_audio=False, duration=3.2,
                            video_length="80", fps=dict(image_seq_details.get("fps") or {"num": 25, "den": 1}))
            else:
                if not os.path.exists(path):
                    continue
                data = _image_reader_data(self.editor, path)
            f = File()
            f.data = data
            f.save()
            out.append(f)
        return out


@pytest.fixture
def tt(editor, tmp_path, monkeypatch):
    """The editor fixture plus the titles-text fakes; titles are written under tmp_path."""
    import classes
    from classes import info
    # A test elsewhere leaves a fake classes.query in sys.modules; the package attribute is the real one.
    monkeypatch.setitem(sys.modules, "classes.query", classes.query)
    monkeypatch.setattr(info, "TITLE_PATH", str(tmp_path / "title"), raising=False)
    monkeypatch.setattr(info, "BLENDER_PATH", str(tmp_path / "blender"), raising=False)
    monkeypatch.setattr(info, "USER_TITLES_PATH", str(tmp_path / "title_templates"), raising=False)
    monkeypatch.setattr(info, "EMOJIS_PATH", str(tmp_path / "emojis"), raising=False)
    install_openshot_fakes(editor, monkeypatch)
    editor.window.timeline = FakeTimeline(editor)
    editor.window.files_model = FakeFilesModel(editor)
    editor.window.selected_effects = []
    editor.tmp_path = tmp_path
    return editor


def receipt(out: str) -> dict:
    """The JSON receipt line of an ok() result."""
    assert not out.startswith("Error"), out
    lines = out.split("\n", 1)
    return json.loads(lines[1]) if len(lines) > 1 else {}
