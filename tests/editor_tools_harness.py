"""A headless editor for editor-tool unit tests.

The real ``ProjectDataStore`` (key-path updates, old values for undo), the real
``UpdateManager`` behind the real ``UpdatesRouter`` (thread-local transaction
ids, grouping, undo/redo) and the real ``classes.query`` layer -- with
``get_app`` pointed at a stand-in app in every module that reads it. Clip,
file and effect records are shaped like production data: they come from
libopenshot 1.0 JSON captured in ``tests/fixtures/editor_tools/``.

Usage::

    def test_something(editor):              # the fixture from conftest
        f = editor.add_file("video")
        c = editor.add_clip(f, position=2.0)
        out = editor.call("move_clip_tool", timeline_clip_ids=[c], position=5.0)
        assert not out.startswith("Error")
        assert editor.clip(c)["position"] == 5.0
        assert editor.undo_steps_since_mark() == 1
        editor.undo()
        assert editor.clip(c)["position"] == 2.0

``editor.window`` is a MagicMock: stub the UI methods a tool calls on it
(``editor.window.timeline.Fade_Triggered = fake``) and assert on them.
"""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
_FIXTURES = os.path.join(_HERE, "fixtures", "editor_tools")


def _load(name):
    with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


class EditorHarness:
    """A fresh default project wired to the real update machinery."""

    def __init__(self, fps=(30, 1), width=1920, height=1080):
        from classes import project_data
        from classes.update_queue import UpdateQueue, UpdatesRouter
        from classes.updates import UpdateManager

        self.manager = UpdateManager()
        self.store = project_data.ProjectDataStore.__new__(project_data.ProjectDataStore)
        with open(os.path.join(_SRC, "settings", "_default.project"), encoding="utf-8") as fh:
            self.store._data = json.load(fh)
        self.store.has_unsaved_changes = False
        self.store.current_filepath = None
        self.store._data["fps"] = {"num": fps[0], "den": fps[1]}
        self.store._data["width"] = width
        self.store._data["height"] = height
        self.manager.add_listener(self.store)

        self.app = MagicMock(name="app")
        self.app.project = self.store
        self.app.updates = UpdatesRouter(self.manager, UpdateQueue(self.manager))
        self.window = self.app.window
        self.window.selected_clips = []
        self.window.selected_transitions = []
        self.window.selected_effects = []
        self.window.preview_thread.player.Position.return_value = 1
        self.settings = {"default-image-length": 10.0, "default-transition-length": 1.0}
        self.app.get_settings.return_value.get.side_effect = lambda k, d=None: self.settings.get(k, d)
        self._patches = []
        self._mark = 0
        self._fixtures = {"files": _load("files.json"), "clip": _load("clip.json"),
                          "effects": _load("effects.json")}

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        import classes.app as app_module
        from classes import project_data, query, tool_handlers
        from classes import updates as updates_module

        for mod in (app_module, query, updates_module, project_data):
            p = patch.object(mod, "get_app", return_value=self.app)
            p.start()
            self._patches.append(p)
        p = patch.object(tool_handlers, "_get_app", return_value=self.app)
        p.start()
        self._patches.append(p)
        self.mark()
        return self

    def stop(self):
        while self._patches:
            self._patches.pop().stop()

    # -- builders (setup is not undoable: history is marked afterwards) -----
    def add_file(self, kind="video", *, path=None, duration=None, **overrides) -> str:
        """Insert a project file shaped like an import of a 'video', 'audio' or 'image'."""
        from classes.query import File

        data = copy.deepcopy(self._fixtures["files"][kind])
        if path:
            data["path"] = path
        if duration is not None:
            data["duration"] = float(duration)
            fps = data.get("fps") or {"num": 30, "den": 1}
            data["video_length"] = str(int(round(float(duration) * fps["num"] / fps["den"])))
        data.update(overrides)
        f = File()
        f.data = data
        f.save()
        self.mark()
        return f.id

    def add_clip(self, file_id, *, position=0.0, layer=1000000, start=0.0, end=None, **overrides) -> str:
        """Insert a timeline clip built the way Timeline.addClip builds one."""
        from classes.query import Clip, File

        file_data = File.get(id=file_id).data
        data = copy.deepcopy(self._fixtures["clip"])
        data.pop("id", None)
        duration = float(file_data.get("duration") or 10.0)
        if file_data.get("media_type") == "image":
            duration = float(self.settings["default-image-length"])
        data.update({
            "file_id": file_id,
            "title": os.path.basename(file_data.get("path", "clip")),
            "reader": copy.deepcopy(file_data),
            "layer": int(layer),
            "position": float(position),
            "start": float(start),
            "end": float(end if end is not None else duration),
            "duration": duration,
            "effects": [],
        })
        if file_data.get("media_type") == "audio":
            data["has_video"] = {"Points": [{"co": {"X": 1.0, "Y": 0.0}, "interpolation": 2}]}
        data.update(overrides)
        c = Clip()
        c.data = data
        c.save()
        self.mark()
        return c.id

    def add_effect(self, clip_id, class_name, **props) -> str:
        """Append a libopenshot-shaped effect to a clip; returns the effect id."""
        from classes.query import Clip

        effect = copy.deepcopy(self._fixtures["effects"][class_name])
        effect["id"] = self.store.generate_id()
        effect.update(props)
        clip = Clip.get(id=clip_id)
        effects = list(clip.data.get("effects") or []) + [effect]
        self.app.updates.update(["clips", {"id": clip_id}], {"effects": effects})
        self.mark()
        return effect["id"]

    def add_track(self, number, label="", lock=False):
        layers = list(self.store._data.get("layers") or [])
        layers.append({"id": f"L{len(layers) + 1}", "label": label, "number": int(number),
                       "y": 0, "lock": bool(lock)})
        self.store._data["layers"] = layers
        return int(number)

    def lock_track(self, number, lock=True):
        for layer in self.store._data["layers"]:
            if int(layer["number"]) == int(number):
                layer["lock"] = bool(lock)

    def effect_fixture(self, class_name) -> dict:
        return copy.deepcopy(self._fixtures["effects"][class_name])

    # -- reading ------------------------------------------------------------
    def clip(self, clip_id) -> dict | None:
        for c in self.store._data.get("clips") or []:
            if c.get("id") == clip_id:
                return c
        return None

    def clips(self) -> list:
        return list(self.store._data.get("clips") or [])

    def file(self, file_id) -> dict | None:
        for f in self.store._data.get("files") or []:
            if f.get("id") == file_id:
                return f
        return None

    def get(self, key):
        return self.store._data.get(key)

    # -- calling tools ------------------------------------------------------
    def call(self, tool_name, **args) -> str:
        """Run a tool through execute_tool, exactly like the chat does.

        Returns the tool's own text: the contract-3 receipt's summary, then its
        data as JSON (what an editor tool wrote). call_receipt() returns the
        receipt itself.
        """
        import json
        receipt = self.call_receipt(tool_name, **args)
        if receipt.get("data") is None:
            return receipt["summary"]
        return receipt["summary"] + "\n" + json.dumps(receipt["data"])

    def call_receipt(self, tool_name, **args) -> dict:
        from classes import tool_handlers
        from classes.agent_tools.receipt import parse_receipt
        out = tool_handlers.execute_tool(tool_name, args)
        receipt = parse_receipt(out)
        assert receipt is not None, f"{tool_name} did not answer a contract-3 receipt: {out[:200]}"
        return receipt

    # -- undo bookkeeping ---------------------------------------------------
    def mark(self):
        self._mark = len(self.manager.actionHistory)

    def undo_steps_since_mark(self) -> int:
        """Distinct undo transactions added since the last mark()."""
        actions = list(self.manager.actionHistory)[self._mark:]
        return len({a.transaction for a in actions})

    def undo(self):
        self.manager.undo()

    def redo(self):
        self.manager.redo()


def make_editor(**kwargs) -> EditorHarness:
    return EditorHarness(**kwargs).start()
