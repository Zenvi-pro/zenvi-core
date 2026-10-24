"""inspect_timeline_tool schema + handler behavior (mocked render)."""

from __future__ import annotations

import json
import types

import pytest

from classes.agent_tools.output import ImageBlock, ToolOutput
from classes.agent_tools.receipt import ToolReceipt, parse_receipt
from classes.agent_tools.schema import TOOL_SCHEMAS, validate_args
from classes.agent_tools.visible_clips import mid_bin_frames


def test_timeline_schema_golden():
    schema = TOOL_SCHEMAS["inspect_timeline_tool"]
    assert schema["additionalProperties"] is False
    props = set(schema["properties"])
    assert props == {
        "start_frame", "end_frame", "max_frames", "timeline_clip_id", "start", "end", "overview",
    }
    assert validate_args("inspect_timeline_tool", {"unknown": 1}) is not None


def test_watch_suggested_keys_are_inspect_arguments():
    from classes.agent_tools.receipt import WatchSuggested

    watch = WatchSuggested("clip-a", 1.0, 3.0).to_dict()
    assert set(watch) == {"timeline_clip_id", "start", "end"}
    assert validate_args("inspect_timeline_tool", watch) is None


def test_mid_bin_matches_plan_example():
    assert mid_bin_frames(0, 60, 6) == [5, 15, 25, 35, 45, 55]


def test_empty_timeline_refused(monkeypatch):
    from classes.agent_tools import inspect as insp

    app = types.SimpleNamespace(project=types.SimpleNamespace(_data={
        "clips": [], "duration": 0, "fps": {"num": 30, "den": 1},
        "width": 1280, "height": 720,
    }))
    monkeypatch.setattr("classes.app.get_app", lambda: app)
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.snapshot_project",
        lambda _app: dict(app.project._data),
    )
    out = insp.inspect_timeline(start_frame=0, end_frame=30)
    assert isinstance(out, ToolReceipt)
    assert out.status == "refused"
    assert out.undoSteps == 0


def test_watch_suggested_window_and_mid_bin(monkeypatch):
    from classes.agent_tools import inspect as insp

    project = {
        "width": 1280,
        "height": 720,
        "fps": {"num": 30, "den": 1},
        "duration": 10.0,
        "clips": [{
            "id": "clip-a",
            "layer": 0,
            "position": 1.0,
            "start": 0.0,
            "end": 4.0,
        }],
        "layers": [{"number": 0, "y": 1}],
    }
    app = types.SimpleNamespace(project=types.SimpleNamespace(_data=project))
    monkeypatch.setattr("classes.app.get_app", lambda: app)
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.snapshot_project",
        lambda _app: dict(project),
    )

    captured = {}

    def fake_render(project_data, frames_0, **_kw):
        captured["frames"] = list(frames_0)
        return [{
            "frame": f,
            "seconds": f / 30.0,
            "jpeg": b"\xff\xd8" + bytes([f % 256]) + b"x" * 20,
            "clips": [],
            "width": 512,
            "height": 288,
        } for f in frames_0]

    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.render_timeline_frames", fake_render,
    )
    out = insp.inspect_timeline(timeline_clip_id="clip-a", start=1.0, end=3.0, max_frames=6)
    assert isinstance(out, ToolOutput)
    assert out.receipt.status == "applied"
    assert out.receipt.undoSteps == 0
    assert len(out.images) == 6
    assert captured["frames"] == mid_bin_frames(30, 90, 6)  # 1s–3s at 30fps


def test_overview_single_image(monkeypatch):
    from classes.agent_tools import inspect as insp

    project = {
        "width": 640,
        "height": 360,
        "fps": {"num": 30, "den": 1},
        "duration": 2.0,
        "clips": [{"id": "c", "layer": 0, "position": 0, "start": 0, "end": 2}],
        "layers": [{"number": 0}],
    }
    monkeypatch.setattr(
        "classes.app.get_app",
        lambda: types.SimpleNamespace(project=types.SimpleNamespace(_data=project)),
    )
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.snapshot_project",
        lambda _app: dict(project),
    )
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.render_timeline_frames",
        lambda *_a, **_k: [
            {
                "frame": i,
                "seconds": i / 30.0,
                "jpeg": b"\xff\xd8" + b"y" * 30,
                "clips": [],
                "width": 320,
                "height": 180,
            }
            for i in range(8)
        ],
    )
    monkeypatch.setattr(
        "classes.agent_tools.inspect._overview_sheet",
        lambda rendered, time_key="seconds": {
            "jpeg": b"\xff\xd8sheet",
            "timestamps": [r[time_key] for r in rendered[:4]],
        },
    )
    out = insp.inspect_timeline(overview=True, start_frame=0, end_frame=60)
    assert isinstance(out, ToolOutput)
    assert len(out.images) == 1
    assert out.receipt.data["mode"] == "overview"
    assert "tileTimestamps" in out.receipt.data["overview"]


def test_unknown_clip_refused(monkeypatch):
    from classes.agent_tools import inspect as insp

    project = {
        "width": 640, "height": 360, "fps": {"num": 30, "den": 1},
        "clips": [], "duration": 1,
    }
    monkeypatch.setattr(
        "classes.app.get_app",
        lambda: types.SimpleNamespace(project=types.SimpleNamespace(_data=project)),
    )
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.snapshot_project",
        lambda _app: dict(project),
    )
    out = insp.inspect_timeline(timeline_clip_id="missing", start=0, end=1)
    assert out.status == "refused"


@pytest.mark.parametrize("kwargs", [
    {"start_frame": 0, "end_frame": 900},                    # past the 300-frame end
    {"start_frame": -10, "end_frame": 20},
    {"start_frame": 0, "end_frame": 900, "overview": True},
    {"start_frame": 400, "overview": True},
])
def test_inspect_ranges_outside_the_project_are_refused(monkeypatch, kwargs):
    """Review #216: only the single-frame form checked the project's range."""
    from classes.agent_tools import inspect as insp

    project = {"width": 1280, "height": 720, "fps": {"num": 30, "den": 1}, "duration": 10.0,
               "clips": [{"id": "a", "layer": 0, "position": 0.0, "start": 0.0, "end": 10.0}],
               "layers": [{"number": 0, "y": 1}]}
    app = types.SimpleNamespace(project=types.SimpleNamespace(_data=project))
    monkeypatch.setattr("classes.app.get_app", lambda: app)
    monkeypatch.setattr("classes.agent_tools.inspect_render.snapshot_project",
                        lambda _app: dict(project))
    rendered = []
    monkeypatch.setattr("classes.agent_tools.inspect_render.render_timeline_frames",
                        lambda project_data, frames_0, **_kw: rendered.append(frames_0) or [])
    out = insp.inspect_timeline(**kwargs)
    assert isinstance(out, ToolReceipt) and out.status == "refused", out
    assert "out of range" in out.summary
    assert rendered == []


def test_inspect_tools_create_zero_undo_via_execute():
    """READ_ONLY inspect must not open an undo group / bump history."""
    from classes.agent_tools import execute as ex
    from classes.agent_tools.output import ToolOutput
    from classes.agent_tools.receipt import ToolReceipt

    history = []

    def handler(**_kw):
        return ToolOutput(
            receipt=ToolReceipt.applied(
                "inspect_timeline_tool", "Rendered 1", undo_steps=0, data={"frames": []},
            ),
            images=[],
        )

    app = types.SimpleNamespace(
        updates=types.SimpleNamespace(actionHistory=history),
        project={},
        thread=lambda: None,
    )

    def atomic(app_, fn):
        def wrapped(**kw):
            history.append("atomic")
            return fn(**kw)
        return wrapped

    ex.bind_runtime(
        handlers={"inspect_timeline_tool": handler},
        read_only=frozenset({"inspect_timeline_tool"}),
        background_safe=frozenset({"inspect_timeline_tool"}),
        ungrouped=frozenset({"inspect_timeline_tool"}),
        main_thread_timeouts={},
        get_app=lambda: app,
        run_on_main_thread=lambda fn, timeout=30: fn(),
        atomic=atomic,
        coerce_steps=lambda x: 1,
        qthread=None,
    )
    out = ex.execute_tool_rich("inspect_timeline_tool", {"start_frame": 0})
    assert out.receipt.undoSteps == 0
    assert history == []
