"""Manim editor tools: set up and render math animations on this computer, then import them."""

import json
import os

import pytest

from classes import hyperframes_local as hf
from classes import manim_local as ml
from classes import tool_handlers
from classes.editor_tools import REGISTRY
from classes.editor_tools import ai_generation_hyperframes as hfx


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def studio(editor, tmp_path, monkeypatch):
    monkeypatch.setattr(hf, "home", lambda: str(tmp_path / "hyperframes"))
    os.makedirs(hf.project_dir("proof"))
    state = {"installed": False, "latex": False, "asked": [], "imported": []}
    monkeypatch.setattr(ml, "status", lambda: {"manim": "manim" if state["installed"] else "",
                                               "latex": state["latex"], "ready": state["installed"]})

    def fake_install(latex=False, progress=None):
        state["installed"] = True
        state["latex"] = state["latex"] or latex
        return ml.status()

    def fake_render(project, scene_file, scene, transparent=True, quality="standard"):
        out = os.path.join(hf.project_dir(project), "manim-media", scene + (".webm" if transparent else ".mp4"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "wb").write(b"\x1a\x45\xdf\xa3" + b"0" * 2048)
        return out

    def fake_import(path, *, preserve_alpha=None):
        state["imported"].append((path, preserve_alpha))
        from classes.query import File
        return File.get(id=editor.add_file("video", path=path)), None

    monkeypatch.setattr(ml, "install", fake_install)
    monkeypatch.setattr(ml, "render", fake_render)
    monkeypatch.setattr(hfx, "_ask_install_consent", lambda steps, **kw: state["asked"].append(steps) or True)
    monkeypatch.setattr(tool_handlers, "_import_generated_video", fake_import)
    return state


def test_tools_are_registered():
    assert {"manim_setup_tool", "render_manim_tool"} <= set(REGISTRY)


def test_setup_reports_then_installs_with_consent_and_latex_only_when_asked(editor, studio):
    out = _receipt(editor.call("manim_setup_tool"))
    assert out["ready"] is False and studio["asked"] == []                 # reporting installs nothing
    _receipt(editor.call("manim_setup_tool", install=True))
    assert [s["id"] for s in studio["asked"][0]] == ["manim"] and studio["latex"] is False
    _receipt(editor.call("manim_setup_tool", install=True, latex=True))
    assert [s["id"] for s in studio["asked"][1]] == ["latex"] and studio["latex"] is True


def test_a_declined_install_installs_nothing(editor, studio, monkeypatch):
    monkeypatch.setattr(hfx, "_ask_install_consent", lambda steps, **kw: False)
    assert editor.call("manim_setup_tool", install=True).startswith("Error")
    assert studio["installed"] is False


def test_render_imports_the_animation_as_a_motion_graphic(editor, studio):
    studio["installed"] = True
    r = _receipt(editor.call("render_manim_tool", project="proof", scene_file="scene.py", scene="Pythagoras",
                             label="Pythagoras proof"))
    assert studio["imported"] == [(os.path.join(hf.project_dir("proof"), "manim-media", "Pythagoras.webm"), True)]
    f = editor.file(r["file_id"])
    assert f["ai_metadata"]["source"] == "hyperframes_motion_graphics"       # placed whole, like any render
    assert f["ai_metadata"]["short_summary"] == "Pythagoras proof"
    still_overlay = _receipt(editor.call("render_manim_tool", project="proof", scene_file="scene.py",
                                         scene="Pythagoras", label="Full frame", transparent=False))
    assert still_overlay["transparent"] is True and studio["imported"][-1] == (
        os.path.join(hf.project_dir("proof"), "manim-media", "Pythagoras.webm"), True)


def test_an_overlay_stays_transparent_unless_the_user_asked_for_a_full_frame(editor, studio):
    """transparent=false is what the model passes for an equation over footage. That still overlays."""
    studio["installed"] = True
    over = _receipt(editor.call("render_manim_tool", project="proof", scene_file="scene.py", scene="Pythagoras",
                                label="Equation", transparent=False))
    assert over["transparent"] is True and studio["imported"][-1][0].endswith(".webm")
    full = _receipt(editor.call("render_manim_tool", project="proof", scene_file="scene.py", scene="Card",
                                label="Full card", transparent=False, full_frame=True))
    assert full["transparent"] is False and studio["imported"][-1][0].endswith(".mp4")


def test_render_errors_come_back_as_tool_errors(editor, studio, monkeypatch):
    def broken(*a, **k):
        raise hf.HyperframesError("Manim is not set up on this computer yet; call manim_setup_tool")

    monkeypatch.setattr(ml, "render", broken)
    out = editor.call("render_manim_tool", project="proof", scene_file="scene.py", scene="Pythagoras", label="x")
    assert out.startswith("Error") and "manim_setup_tool" in out and studio["imported"] == []
