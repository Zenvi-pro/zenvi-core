"""Inspect renderer gate / indexing / isolation (mocked openshot)."""

from __future__ import annotations

import threading
import types

import pytest

from classes.agent_tools import inspect_render as ir


def test_no_process_events_in_render_module():
    import inspect as pyinspect
    import classes.agent_tools.inspect_render as mod

    src = pyinspect.getsource(mod)
    assert "processEvents" not in src
    assert "HIGH_QUALITY_SCALING" not in src


def test_project_frame_zero_calls_getframe_one(monkeypatch):
    calls = []

    class FakeFrame:
        def GetImage(self):
            return None

        def Thumbnail(self, *a, **k):
            # write a tiny jpeg
            path = a[0]
            with open(path, "wb") as fh:
                fh.write(b"\xff\xd8\xff" + b"0" * 40)

    class FakeTimeline:
        def __init__(self, *a, **k):
            self.info = types.SimpleNamespace(sample_rate=48000, channels=2, channel_layout=4)

        def SetJson(self, *_a):
            pass

        def Open(self):
            pass

        def ApplyMapperToClips(self):
            pass

        def SetMaxSize(self, *_a):
            pass

        def GetFrame(self, n):
            calls.append(n)
            return FakeFrame()

        def Close(self):
            pass

    class FakeFraction:
        def __init__(self, n, d):
            self.num = n
            self.den = d

    fake_os = types.ModuleType("openshot")
    fake_os.Timeline = FakeTimeline
    fake_os.Fraction = FakeFraction
    monkeypatch.setitem(__import__("sys").modules, "openshot", fake_os)

    class FakeQImage:
        def __init__(self, path=None):
            self._null = False
            self._w = 512
            self._h = 288

        def isNull(self):
            return self._null

        def width(self):
            return self._w

        def height(self):
            return self._h

        def scaled(self, w, h, *a, **k):
            self._w, self._h = w, h
            return self

    monkeypatch.setattr(ir, "_qimage_from_frame", lambda frame, w, h: FakeQImage())
    monkeypatch.setattr(ir, "jpeg_bytes_from_qimage", lambda img: b"\xff\xd8xx")
    monkeypatch.setattr(
        "classes.agent_tools.inspect_overlay.apply_overlay_qimage",
        lambda img, caption=None: img,
    )

    project = {
        "width": 1280,
        "height": 720,
        "fps": {"num": 30, "den": 1},
        "clips": [{"id": "c1", "layer": 0, "position": 0, "start": 0, "end": 2}],
        "duration": 2.0,
    }
    out = ir.render_timeline_frames(project, [0])
    assert calls == [1]
    assert len(out) == 1
    assert out[0]["frame"] == 0


def test_busy_gate_refuses_second_caller(monkeypatch):
    assert ir.acquire_gate(timeout=0.05)
    try:
        with pytest.raises(ir.InspectBusy):
            # Force acquire failure path by holding the gate
            if not ir.acquire_gate(timeout=0.05):
                raise ir.InspectBusy("inspect already running")
            ir.release_gate()
    finally:
        ir.release_gate()


def test_cancel_raises_and_releases(monkeypatch):
    class FakeTimeline:
        def __init__(self, *a, **k):
            self.info = types.SimpleNamespace(sample_rate=48000, channels=2, channel_layout=4)
            self.closed = False

        def SetJson(self, *_a):
            pass

        def Open(self):
            pass

        def ApplyMapperToClips(self):
            pass

        def SetMaxSize(self, *_a):
            pass

        def GetFrame(self, n):
            ir.request_cancel()
            raise RuntimeError("should not reach")

        def Close(self):
            self.closed = True

    class FakeFraction:
        def __init__(self, n, d):
            pass

    fake_os = types.ModuleType("openshot")
    fake_os.Timeline = FakeTimeline
    fake_os.Fraction = FakeFraction
    monkeypatch.setitem(__import__("sys").modules, "openshot", fake_os)

    # Cancel before first frame check
    ir.clear_cancel()
    ir.request_cancel()
    project = {
        "width": 640,
        "height": 360,
        "fps": {"num": 30, "den": 1},
        "clips": [{"id": "c1", "layer": 0, "position": 0, "start": 0, "end": 1}],
    }
    with pytest.raises(ir.InspectCancelled):
        ir.render_timeline_frames(project, [0, 1])
    # Gate released
    assert ir.acquire_gate(timeout=0.2)
    ir.release_gate()
    ir.clear_cancel()


def test_live_timeline_arg_is_ignored(monkeypatch):
    """Passing a live timeline must not call GetFrame on it."""
    live_calls = []

    class Live:
        def GetFrame(self, n):
            live_calls.append(n)
            raise AssertionError("live GetFrame")

    private_calls = []

    class FakeFrame:
        pass

    class FakeTimeline:
        def __init__(self, *a, **k):
            self.info = types.SimpleNamespace(sample_rate=48000, channels=2, channel_layout=4)

        def SetJson(self, *_a):
            pass

        def Open(self):
            pass

        def ApplyMapperToClips(self):
            pass

        def SetMaxSize(self, *_a):
            pass

        def GetFrame(self, n):
            private_calls.append(n)
            return FakeFrame()

        def Close(self):
            pass

    fake_os = types.ModuleType("openshot")
    fake_os.Timeline = FakeTimeline
    fake_os.Fraction = lambda n, d: None
    monkeypatch.setitem(__import__("sys").modules, "openshot", fake_os)

    class FakeQImage:
        def isNull(self):
            return False

        def width(self):
            return 512

        def height(self):
            return 288

        def scaled(self, w, h, *a, **k):
            return self

    monkeypatch.setattr(ir, "_qimage_from_frame", lambda *a, **k: FakeQImage())
    monkeypatch.setattr(ir, "jpeg_bytes_from_qimage", lambda img: b"\xff\xd8yy")
    monkeypatch.setattr(
        "classes.agent_tools.inspect_overlay.apply_overlay_qimage",
        lambda img, caption=None: img,
    )

    project = {
        "width": 640, "height": 360, "fps": {"num": 30, "den": 1},
        "clips": [{"id": "c", "layer": 0, "position": 0, "start": 0, "end": 1}],
    }
    out = ir.render_timeline_frames(project, [0], live_timeline=Live())
    assert live_calls == []
    assert private_calls == [1]
    assert len(out) == 1
