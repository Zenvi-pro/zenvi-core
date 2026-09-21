"""inspect_timeline_tool schema + handler behavior (mocked render)."""

from __future__ import annotations

import json
import types

from classes.agent_tools.output import ImageBlock, ToolOutput
from classes.agent_tools.receipt import ToolReceipt, parse_receipt
from classes.agent_tools.schema import TOOL_SCHEMAS, validate_args
from classes.agent_tools.visible_clips import mid_bin_frames


def test_timeline_schema_golden():
    schema = TOOL_SCHEMAS["inspect_timeline_tool"]
    assert schema["additionalProperties"] is False
    props = set(schema["properties"])
    assert props == {
        "startFrame", "endFrame", "maxFrames", "clipId", "start", "end", "overview",
    }
    assert validate_args("inspect_timeline_tool", {"unknown": 1}) is not None


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
    out = insp.inspect_timeline(startFrame=0, endFrame=30)
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
    out = insp.inspect_timeline(clipId="clip-a", start=1.0, end=3.0, maxFrames=6)
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
    out = insp.inspect_timeline(overview=True, startFrame=0, endFrame=60)
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
    out = insp.inspect_timeline(clipId="missing", start=0, end=1)
    assert out.status == "refused"
