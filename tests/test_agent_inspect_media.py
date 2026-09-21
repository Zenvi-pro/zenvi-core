"""inspect_media_tool schema + handler branches (mocked render)."""

from __future__ import annotations

import types

from classes.agent_tools.output import ToolOutput
from classes.agent_tools.receipt import ToolReceipt
from classes.agent_tools.schema import TOOL_SCHEMAS, validate_args


def test_media_schema_golden():
    schema = TOOL_SCHEMAS["inspect_media_tool"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["fileId"]
    assert validate_args("inspect_media_tool", {}) is not None
    assert validate_args("inspect_media_tool", {"fileId": "abc"}) is None


def test_missing_file_refused(monkeypatch):
    from classes.agent_tools import inspect as insp

    class FakeFile:
        @staticmethod
        def get(**_kw):
            return None

    monkeypatch.setattr("classes.query.File", FakeFile)
    out = insp.inspect_media(fileId="nope")
    assert isinstance(out, ToolReceipt)
    assert out.status == "refused"


def test_audio_metadata_only(monkeypatch):
    from classes.agent_tools import inspect as insp

    class FakeFileObj:
        data = {
            "has_video": False,
            "has_audio": True,
            "media_type": "audio",
            "duration": 12.5,
            "channels": 2,
            "sample_rate": 48000,
            "path": "/tmp/x.wav",
        }

        def absolute_path(self):
            return "/tmp/x.wav"

    class FakeFile:
        @staticmethod
        def get(**_kw):
            return FakeFileObj()

    monkeypatch.setattr("classes.query.File", FakeFile)
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    out = insp.inspect_media(fileId="a1")
    assert isinstance(out, ToolOutput)
    assert out.images == []
    assert out.receipt.status == "applied"
    assert out.receipt.data["hasAudio"] is True
    assert "transcription" not in (out.receipt.data or {})


def test_media_frames_mid_bin(monkeypatch, tmp_path):
    from classes.agent_tools import inspect as insp

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"fake")

    class FakeFileObj:
        data = {
            "has_video": True,
            "has_audio": True,
            "duration": 2.0,
            "path": str(media),
        }

        def absolute_path(self):
            return str(media)

    class FakeFile:
        @staticmethod
        def get(**_kw):
            return FakeFileObj()

    monkeypatch.setattr("classes.query.File", FakeFile)

    def fake_render(path, timestamps, **_kw):
        return [{
            "timestamp": t,
            "jpeg": b"\xff\xd8" + b"m" * 20,
            "width": 512,
            "height": 288,
        } for t in timestamps]

    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.render_media_frames", fake_render,
    )
    out = insp.inspect_media(fileId="f1", maxFrames=4)
    assert isinstance(out, ToolOutput)
    assert len(out.images) == 4
    assert len(out.receipt.data["frameTimestamps"]) == 4
    assert "transcription" not in out.receipt.data


def test_media_overview_one_sheet(monkeypatch, tmp_path):
    from classes.agent_tools import inspect as insp

    media = tmp_path / "long.mp4"
    media.write_bytes(b"fake")

    class FakeFileObj:
        data = {"has_video": True, "duration": 10.0, "path": str(media)}

        def absolute_path(self):
            return str(media)

    class FakeFile:
        @staticmethod
        def get(**_kw):
            return FakeFileObj()

    monkeypatch.setattr("classes.query.File", FakeFile)
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.render_media_frames",
        lambda path, timestamps, **_kw: [
            {"timestamp": t, "jpeg": b"\xff\xd8" + b"z" * 12, "width": 160, "height": 90}
            for t in timestamps
        ],
    )
    monkeypatch.setattr(
        "classes.agent_tools.inspect._overview_sheet",
        lambda rendered, time_key="timestamp": {
            "jpeg": b"\xff\xd8sheet",
            "timestamps": [0.0, 5.0, 9.9],
        },
    )
    out = insp.inspect_media(fileId="f2", overview=True)
    assert len(out.images) == 1
    assert out.receipt.data["mode"] == "overview"
    assert out.receipt.data["overview"]["tileTimestamps"] == [0.0, 5.0, 9.9]


def test_image_file_one_still(monkeypatch, tmp_path):
    from classes.agent_tools import inspect as insp

    img = tmp_path / "still.png"
    img.write_bytes(b"fake-png")

    class FakeFileObj:
        data = {"media_type": "image", "path": str(img), "duration": 0}

        def absolute_path(self):
            return str(img)

    class FakeFile:
        @staticmethod
        def get(**_kw):
            return FakeFileObj()

    monkeypatch.setattr("classes.query.File", FakeFile)
    monkeypatch.setattr(
        "classes.agent_tools.inspect_render.render_media_frames",
        lambda path, timestamps, **_kw: [{
            "timestamp": 0.0,
            "jpeg": b"\xff\xd8still",
            "width": 512,
            "height": 288,
        }],
    )
    out = insp.inspect_media(fileId="img1")
    assert isinstance(out, ToolOutput)
    assert len(out.images) == 1
    assert out.receipt.undoSteps == 0
    assert "transcription" not in out.receipt.data


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
    out = ex.execute_tool_rich("inspect_timeline_tool", {"startFrame": 0})
    assert out.receipt.undoSteps == 0
    assert history == []
