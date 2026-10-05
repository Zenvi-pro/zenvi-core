"""Fakes for the handoff tests (C1 core, reused by the Remotion / HyperFrames / Adobe packages).

``linked`` is the ``tt`` editor fixture (real project store + undo, fake
timeline and libopenshot factories) plus:

* ``linked.user_path``: the unsaved project's user folder (``info.USER_PATH``),
  so renders land in ``<tmp>/user/links/<kind>/``;
* ``linked.probe``: :class:`FakeProbe` standing in for
  ``linked_media.probe_media`` (no libopenshot): media durations come from
  ``linked.probe.durations[path]`` (default 5 s at 30 fps, 1920x1080);
* ``linked.media(name, seconds=5.0)``: writes a small file to link.

:class:`FakeProvider` is a LinkProvider whose fingerprint is
``sha256:<props+version>`` and whose render writes a ``.mov`` into the
staging folder it is given (or fails / waits, as configured).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading

import pytest

from titles_text_fakes import tt  # noqa: F401  (fixture; test modules import it with `linked`)


class FakeProbe:
    def __init__(self, editor):
        self.editor = editor
        self.durations = {}
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        seconds = float(self.durations.get(path, 5.0))
        data = copy.deepcopy(self.editor._fixtures["files"]["video"])
        data.update(path=path, duration=seconds, video_length=str(int(round(seconds * 30))), width=1920,
                    height=1080, fps={"num": 30, "den": 1}, has_audio=False, media_type="video")
        data["fingerprint"] = {"size": os.path.getsize(path), "mtime": 0.0,
                               "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()}
        return data


class FakeProvider:
    """A LinkProvider for tests. ``version`` stands for the source code: bump it to make clips stale."""

    supports_studio = True

    def __init__(self, kind="remotion", label="Remotion", probe=None, seconds=5.0):
        self.kind = kind
        self.label = label
        self.version = 1
        self.probe = probe
        self.seconds = seconds
        self.fail = None             # an exception to raise from render
        self.codec = "prores4444"
        self.ext = ".mov"
        self.renders = []
        self.opened = []
        self.during_render = None    # callback(link, out_dir) run inside render
        self.gate = None             # threading.Event render waits on (cancel tests)

    def fingerprint(self, link):
        blob = json.dumps({"props": link.get("props") or {}, "v": self.version}, sort_keys=True)
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()

    def render(self, link, out_dir, *, on_progress, should_cancel):
        self.renders.append(copy.deepcopy(link))
        on_progress(0.25, "bundling")
        if self.during_render:
            self.during_render(link, out_dir)
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if should_cancel():
                    from classes.handoff.jobs import JobCancelled
                    raise JobCancelled("cancelled")
        if self.fail is not None:
            raise self.fail
        path = os.path.join(out_dir, "out" + self.ext)
        with open(path, "wb") as fh:
            fh.write(("render %d %s" % (len(self.renders), json.dumps(link.get("props"), sort_keys=True))).encode())
        if self.probe is not None:
            self.probe.durations[path] = self.seconds
        on_progress(1.0, "done")
        from classes.handoff.linked_media import RenderResult
        return RenderResult(path=path, codec=self.codec, width=1920, height=1080, fps={"num": 30, "den": 1},
                            duration_frames=int(round(self.seconds * 30)))

    def open_source(self, link):
        self.opened.append(("code", link))

    def open_studio(self, link):
        self.opened.append(("studio", link))

    def editable_props(self, link):
        return dict(link.get("props") or {})


@pytest.fixture
def linked(tt, tmp_path, monkeypatch):  # noqa: F811  (tt is a fixture)
    from classes import info
    from classes.handoff import linked_media

    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(info, "USER_PATH", str(user))
    probe = FakeProbe(tt)
    monkeypatch.setattr(linked_media, "probe_media", probe)
    tt.user_path = str(user)
    tt.probe = probe

    def media(name="render.mov", seconds=5.0, body=b"movie"):
        folder = tmp_path / "media"
        folder.mkdir(exist_ok=True)
        path = str(folder / name)
        with open(path, "wb") as fh:
            fh.write(body)
        probe.durations[path] = seconds
        return path

    tt.media = media
    providers_before = dict(linked_media._PROVIDERS)
    errors_before = dict(linked_media._RENDER_ERRORS)
    yield tt
    linked_media._PROVIDERS.clear()
    linked_media._PROVIDERS.update(providers_before)
    linked_media._RENDER_ERRORS.clear()
    linked_media._RENDER_ERRORS.update(errors_before)


def remotion_link(composition="Intro", props=None, project_dir=None):
    return {"kind": "remotion", "source": {"project_dir": project_dir, "entry": "src/index.ts",
                                           "composition": composition, "file": "src/Intro.tsx", "line": 12},
            "props": dict(props or {"title": "Hello"})}


__all__ = ["FakeProbe", "FakeProvider", "linked", "remotion_link", "tt"]
