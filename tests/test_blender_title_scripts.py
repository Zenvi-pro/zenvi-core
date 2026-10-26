"""Blender title scripts on Blender 5: the fixed templates render one frame into the injected folder."""

import importlib.util
import os
import shutil

import pytest

from classes import blender_titles

SCRIPTS = os.path.join(blender_titles.blender_dir(), "scripts")


def _source(name):
    with open(os.path.join(SCRIPTS, name + ".py.in")) as f:
        return f.read()


def test_glass_slider_sets_the_output_path_before_anything_can_fail_and_finds_alpha_by_name():
    src = _source("glass_slider")
    assert "inputs[18]" not in src                      # Blender 5 moved the Principled BSDF sockets
    assert src.index('render.filepath = params["output_path"]') < src.index("keyframe_principled(")


def test_neon_curves_glare_type_is_a_menu_name_not_an_enum_id():
    assert '.replace("_", " ").title()' in _source("neon_curves")


BLENDER = shutil.which("blender")


@pytest.mark.skipif(not BLENDER, reason="Blender is not installed")
@pytest.mark.parametrize("name", ["glass_slider", "neon_curves"])
def test_template_renders_a_frame_headless(name, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "render_blender_titles", os.path.join(os.path.dirname(__file__), "..", "scripts", "render_blender_titles.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    xml_path = os.path.join(blender_titles.blender_dir(), name + ".xml")
    _, frames, problems = tool.render_one(xml_path, BLENDER, str(tmp_path), 60, False, 300, (160, 90))
    assert problems == []
    assert len(frames) == 1
