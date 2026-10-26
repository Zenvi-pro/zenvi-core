"""Blender title scripts on Blender 5: the fixed templates render one frame into the injected folder."""

import importlib.util
import os
import shutil

import pytest

from classes import blender_titles

SCRIPTS = os.path.join(blender_titles.blender_dir(), "scripts")


def _render_tool():
    spec = importlib.util.spec_from_file_location(
        "render_blender_titles", os.path.join(os.path.dirname(__file__), "..", "scripts", "render_blender_titles.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    return tool


def _source(name):
    with open(os.path.join(SCRIPTS, name + ".py.in")) as f:
        return f.read()


def test_glass_slider_sets_the_output_path_before_anything_can_fail_and_finds_alpha_by_name():
    src = _source("glass_slider")
    assert "inputs[18]" not in src                      # Blender 5 moved the Principled BSDF sockets
    assert src.index('render.filepath = params["output_path"]') < src.index("keyframe_principled(")


def test_neon_curves_glare_type_is_a_menu_name_not_an_enum_id():
    assert '.replace("_", " ").title()' in _source("neon_curves")


def test_render_check_reports_a_script_traceback_and_a_frame_saved_outside_the_folder():
    # Blender's real output for the old Glass Slider: it often exits 0 and renders to the .blend's path anyway
    log = ("Traceback (most recent call last):\n"
           '  File "/tmp/out/glass_slider.py", line 141, in <module>\n'
           "TypeError: bpy_struct: item.attr = val: sequence expected at dimension 1, not 'float'\n"
           "Saved: '/home/jonathan/.openshot_qt/blender/0030.png'\n")
    problems = _render_tool().log_problems(0, log, ["/home/jonathan/.openshot_qt/blender/0030.png"], "/tmp/out")
    assert problems[0].startswith("TypeError: bpy_struct: item.attr = val")
    assert problems[1].startswith("wrote outside the output folder")
    assert _render_tool().log_problems(0, "Saved: '/tmp/out/x0030.png'\n", ["/tmp/out/x0030.png"], "/tmp/out") == []


BLENDER = shutil.which("blender")


@pytest.mark.skipif(not BLENDER, reason="Blender is not installed")
@pytest.mark.parametrize("name", ["glass_slider", "neon_curves"])
def test_template_renders_a_frame_headless(name, tmp_path):
    tool = _render_tool()
    xml_path = os.path.join(blender_titles.blender_dir(), name + ".xml")
    _, frames, problems = tool.render_one(xml_path, BLENDER, str(tmp_path), 60, False, 300, (160, 90))
    assert problems == []
    assert len(frames) == 1
