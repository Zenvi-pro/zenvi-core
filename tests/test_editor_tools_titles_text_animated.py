"""Animated titles: classes.blender_titles and add_animated_title_tool, with a fake Blender."""

import json
import os
import re
import stat
import sys

import pytest

from classes import blender_titles
from titles_text_fakes import receipt, tt  # noqa: F401  (fixture)

FAKE_BLENDER = r'''#!{python}
import json, os, re, sys
args = sys.argv[1:]
if "-v" in args:
    print("Blender " + os.environ.get("FAKE_BLENDER_VERSION", "5.0.1") + " (hash 1234)")
    sys.exit(0)
body = open(args[args.index("-P") + 1]).read()
params = json.loads(re.search(r'params_json = r"""(.*?)"""', body, re.S).group(1))
for i in range(1, int(os.environ.get("FAKE_BLENDER_FRAMES", "4")) + 1):
    path = "%s%04d.png" % (params["output_path"], i)
    open(path, "wb").write(b"png")
    print("Fra:%d Mem:12M | Saved: '%s'" % (i, path))
print("Blender quit")
'''


def _script_command(path, source):
    """Write a Python *source* script that runs as a command; returns the command.

    Windows does not honour the #! line, so there a .cmd beside it runs it.
    """
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    if os.name != "nt":
        return str(path)
    wrapper = path.with_suffix(".cmd")
    wrapper.write_text('@"%s" "%s" %%*\n' % (sys.executable, path))
    return str(wrapper)


def _xml(name):
    return os.path.join(blender_titles.blender_dir(), name + ".xml")


@pytest.fixture
def blender(tt, tmp_path, monkeypatch):
    command = _script_command(tmp_path / "blender-fake", FAKE_BLENDER.replace("{python}", sys.executable))
    tt.settings["blender_command"] = command
    tt.blender_command = command
    tt.settings["blender_gpu_enabled"] = False
    monkeypatch.setenv("FAKE_BLENDER_FRAMES", "4")
    return tt


# ---------------------------------------------------------------------------
# blender_titles
# ---------------------------------------------------------------------------

def test_templates_and_their_parameters():
    assert len(blender_titles.template_paths()) == 19
    summary = blender_titles.template_summary(_xml("fly_by_1"))
    assert summary["title"] == "Fly Towards Camera" and summary["service"] == "fly_by_1.blend"
    details = blender_titles.animation_details(_xml("fly_by_1"))
    names = [p["name"] for p in details["params"]]
    assert names[:3] == ["file_name", "title", "extrude"] and "length_multiplier" in names
    fonts = next(p for p in details["params"] if p["name"] == "fontname")["values"]
    assert fonts["WenQuanYiMicroHei (Unicode)"] == "WenQuanYiMicroHei"


def test_default_params_match_what_the_dialog_holds():
    details = blender_titles.animation_details(_xml("fly_by_1"))
    params = blender_titles.default_params(details, fps_diff=2)
    assert params["title"] == "My Title" and params["end_frame"] == 80 and params["start_frame"] == 1
    assert params["extrude"] == 0.1 and params["fontname"] == "Bfont"
    assert params["length_multiplier"] == 2.0                      # 1X at a 50 fps project
    assert params["diffuse_color"] == pytest.approx([127 / 255.0] * 3 + [1.0])
    assert params["specular_color"] == [1.0, 1.0, 1.0]            # only diffuse colours carry alpha
    assert blender_titles.project_fps_diff({"num": 30, "den": 1}) == 1
    assert blender_titles.project_fps_diff({"num": 60, "den": 1}) == 2


def test_script_injection_and_sequence_details(tmp_path):
    source = os.path.join(blender_titles.blender_dir(), "scripts", "fly_by_1.py.in")
    body = blender_titles.build_script(source, {"title": "Hi", "output_path": "/x/y"}, gpu_enabled=True)
    params = json.loads(re.search(r'params_json = r"""(.*?)"""', body, re.S).group(1))
    assert params == {"title": "Hi", "output_path": "/x/y"}
    assert "def ensure_rgba" in body and "#ENABLE GPU RENDERING" in body
    assert "# INJECT_PARAMS_HERE" not in body
    seq = blender_titles.image_sequence_details(str(tmp_path), "Title", {"num": 30, "den": 1})
    assert seq["path"] == os.path.join(str(tmp_path), "Title%04d.png") and seq["fps"] == {"num": 30, "den": 1}
    assert blender_titles.version_newer_or_equal("5.0.1", "5.0")
    assert not blender_titles.version_newer_or_equal("4.5", "5.0")
    assert blender_titles.version_newer_or_equal("10.1", "5.0")   # not a string comparison


# ---------------------------------------------------------------------------
# add_animated_title_tool
# ---------------------------------------------------------------------------

def test_render_import_and_place(blender):
    video = blender.add_file("video", duration=20.0)
    blender.add_clip(video, position=0.0)
    out = blender.call("add_animated_title_tool", template="Fly Towards Camera", text="Tokyo Day 1",
                       params={"diffuse_color": "#ff0000", "text_size": 1.5, "spacemode": "left"},
                       position_seconds=1.0)
    rec = receipt(out)
    assert out.startswith("Rendered animated title 'Fly Towards Camera' (4 frames with Blender 5.0.1) on track 2")
    assert rec["frames"] == 4 and rec["params"]["title"] == "Tokyo Day 1"
    assert rec["params"]["diffuse_color"] == [1.0, 0.0, 0.0, 1.0] and rec["params"]["spacemode"] == "LEFT"
    frames = sorted(os.listdir(rec["folder"]))
    assert frames[:4] == ["Tokyo_Day_10001.png", "Tokyo_Day_10002.png", "Tokyo_Day_10003.png",
                          "Tokyo_Day_10004.png"]
    call = blender.window.files_model.calls[-1]
    assert call["image_seq_details"]["pattern"] == "Tokyo_Day_1%04d.png" and call["skip_indexing"] is True
    clip = blender.clip(rec["timeline_clip_id"])
    assert clip["layer"] == 2000000 and clip["position"] == 1.0
    assert blender.undo_steps_since_mark() == 1
    blender.undo()
    assert blender.clip(clip["id"]) is None and blender.file(rec["file_id"]) is None


def test_missing_or_old_blender_is_refused_before_anything_happens(tt, monkeypatch, blender):
    blender.settings["blender_command"] = ""
    blender.mark()
    out = blender.call("add_animated_title_tool", template="fly_by_1", text="x")
    assert out.startswith("Error") and "Blender is not available" in out and "add_title_tool" in out
    monkeypatch.setenv("FAKE_BLENDER_VERSION", "4.2.0")
    blender.settings["blender_command"] = blender.blender_command
    out = blender.call("add_animated_title_tool", template="fly_by_1", text="x")
    assert out.startswith("Error") and "too old" in out
    assert blender.undo_steps_since_mark() == 0
    assert not os.path.isdir(blender.tmp_path / "blender") or os.listdir(blender.tmp_path / "blender") == []


def test_a_render_without_frames_is_an_error_and_cleans_up(blender, monkeypatch):
    monkeypatch.setenv("FAKE_BLENDER_FRAMES", "0")
    blender.mark()
    out = blender.call("add_animated_title_tool", template="rotate_360", text="Spin")
    assert out.startswith("Error") and "rendered no frames" in out and "Blender quit" in out
    assert blender.undo_steps_since_mark() == 0
    assert os.listdir(blender.tmp_path / "blender") == []


@pytest.mark.parametrize("args, needle", [
    ({"template": "nope", "text": "x"}, "unknown animated title"),
    ({"template": "", "text": "x"}, "template is required"),
    ({"template": "fly_by_1", "params": {"wobble": 1}}, "unknown parameter 'wobble'"),
    ({"template": "fly_by_1", "params": {"extrude": 5}}, "between 0 and 1"),
    ({"template": "fly_by_1", "params": {"fontname": "Comic"}}, "must be one of"),
    ({"template": "fly_by_1", "params": {"diffuse_color": "bleu"}}, "diffuse_color"),
    ({"template": "fly_by_1", "params": {"end_frame": 500}}, "unknown parameter 'end_frame'"),
    ({"template": "snow", "text": "x"}, "has no text"),
])
def test_parameter_refusals(blender, args, needle):
    blender.mark()
    out = blender.call("add_animated_title_tool", **args)
    assert out.startswith("Error") and needle in out, out
    assert blender.undo_steps_since_mark() == 0


def test_a_silent_blender_is_stopped_at_the_timeout(tmp_path):
    """Blender that stalls without printing a line must not outlive timeout_seconds."""
    import time
    stalled = _script_command(tmp_path / "blender-stalled",
                              "#!%s\nimport time\ntime.sleep(60)\n" % sys.executable)
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        blender_titles.render(stalled, str(tmp_path / "t.blend"), str(tmp_path / "t.py"), timeout=1)
    assert time.monotonic() - began < 20
