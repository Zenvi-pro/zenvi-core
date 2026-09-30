"""ComfyUI tools: template catalogue, Create / Enhance with AI through the real service and queue, jobs.

The service and queue are the production classes (without their QThreads); the queued
request is run by the production worker against a fake ComfyUI HTTP server, so these
tests see exactly the graph ComfyUI would receive.
"""

import json
import os
import re
import threading
import time

import pytest

from ai_generation_fakes import FakeComfyServer, closed_port_url, install_comfy
from classes.editor_tools.ai_generation_comfyui import NOT_CONFIGURED

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _receipt(out):
    assert not out.startswith("Error"), out
    return json.loads(out.split("\n", 1)[1])


@pytest.fixture
def server():
    s = FakeComfyServer()
    yield s
    s.close()


@pytest.fixture
def comfy(editor, monkeypatch, tmp_path, server):
    return install_comfy(editor, monkeypatch, tmp_path, url=server.url)


@pytest.fixture
def comfy_off(editor, monkeypatch, tmp_path):
    return install_comfy(editor, monkeypatch, tmp_path, url="")


def _image_file(editor, tmp_path, name="photo.png"):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n fake image")
    return editor.add_file("image", path=str(path), width=640, height=360, name=name)


def _video_file(editor, tmp_path, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"fake mp4")
    return editor.add_file("video", path=str(path), width=1280, height=720, video_length="300", name=name)


def _node(prompt_graph, class_type):
    return [n for n in prompt_graph.values() if n.get("class_type") == class_type]


# --- catalogue -----------------------------------------------------------------------------------

def test_catalogue_lists_create_and_enhance_templates_and_says_comfyui_is_off(editor, comfy_off):
    out = editor.call("list_comfyui_templates_tool")
    r = _receipt(out)
    assert out.startswith("ComfyUI is not configured (Preferences > AI > ComfyUI URL)")
    assert r["comfyui"]["status"] == "not_configured" and "Preferences > AI" in r["comfyui"]["how_to_enable"]
    creates = {row["kind"]: row for row in r["templates"] if row["category"] == "create"}
    assert set(creates) == {"image", "video", "sound", "music"}
    assert creates["image"]["menu"] == "AI Tools > Create with AI > Image..."
    assert creates["music"]["output"] == "audio" and creates["sound"]["output"] == "audio"
    assert all(row["needs_prompt"] for row in creates.values())
    actions = {(row["input"], row["action"]) for row in r["templates"] if row["category"] == "enhance"}
    assert ("video", "split_scenes") in actions and ("audio", "reduce_noise") in actions
    assert ("image", "blur_object") in actions and ("video", "captions") in actions


def test_catalogue_for_a_file_is_what_the_enhance_menu_offers(editor, comfy_off, tmp_path):
    fid = _video_file(editor, tmp_path)
    r = _receipt(editor.call("list_comfyui_templates_tool", file_id=fid))
    assert r["for_file"] == {"file_id": fid, "media_type": "video", "name": "clip.mp4"}
    assert {row["input"] for row in r["templates"]} == {"video"}
    blur = next(row for row in r["templates"] if row["action"] == "blur_object")
    assert blur["needs_object_selection"] is True
    assert blur["menu"] == "Project Files > right-click a file > Enhance with AI > Track an Object > Blur..."
    restyle = next(row for row in r["templates"] if row["action"] == "restyle")
    assert restyle["needs_reference_image"] is True and restyle["needs_prompt"] is True


def test_catalogue_by_media_type_and_unknown_file(editor, comfy_off):
    r = _receipt(editor.call("list_comfyui_templates_tool", media_type="audio"))
    assert sorted(row["action"] for row in r["templates"]) == ["reduce_noise", "remove_noise", "speech_clarity"]
    out = editor.call("list_comfyui_templates_tool", file_id="nope")
    assert out.startswith("Error: no project file with file_id='nope'")


def test_catalogue_reports_ready_and_unreachable(editor, comfy, monkeypatch):
    r = _receipt(editor.call("list_comfyui_templates_tool"))
    assert r["comfyui"]["status"] == "ready"
    editor.settings["comfy-ui-url"] = closed_port_url()
    out = editor.call("list_comfyui_templates_tool")
    assert "is not reachable" in out and _receipt(out)["comfyui"]["status"] == "unreachable"


# --- refusals when ComfyUI is off or down -----------------------------------------------------------

def test_create_and_enhance_refuse_cleanly_when_comfyui_is_not_configured(editor, comfy_off, tmp_path):
    fid = _image_file(editor, tmp_path)
    for out in (editor.call("create_media_with_comfyui_tool", kind="image", prompt="a fox"),
                editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="upscale")):
        assert out == "Error: " + NOT_CONFIGURED
    assert comfy_off.queue.jobs == {} and editor.undo_steps_since_mark() == 0


def test_an_unreachable_server_is_refused_before_queueing(editor, comfy):
    editor.settings["comfy-ui-url"] = closed_port_url()
    out = editor.call("create_media_with_comfyui_tool", kind="image", prompt="a fox")
    assert out.startswith("Error: ComfyUI is not reachable at http://127.0.0.1:") and "Nothing was queued" in out
    assert comfy.queue.jobs == {}


# --- create -----------------------------------------------------------------------------------------

def test_create_image_queues_the_dialog_payload_and_imports_the_result(editor, comfy, server):
    out = editor.call("create_media_with_comfyui_tool", kind="image", prompt="misty pine forest at dawn")
    r = _receipt(out)
    job = r["job"]
    assert job["template"] == "txt2img-basic" and job["origin"] == "agent" and job["status"] == "running"
    assert job["name"] == "generation_gen1"
    assert editor.undo_steps_since_mark() == 0, "queueing is not a project edit"
    comfy.status_bar.showMessage.assert_any_call("Queued generation job", 3000)

    finished = comfy.run_next()
    assert finished["status"] == "completed"
    posted = server.prompts[0]["prompt"]
    texts = [n["inputs"]["text"] for n in _node(posted, "CLIPTextEncode")]
    assert "misty pine forest at dawn" in texts and "low quality, blurry" in texts   # negative prompt kept
    assert server.prompts[0]["client_id"] == "openshot-qt"

    listed = _receipt(editor.call("list_comfyui_jobs_tool", job_id=job["job_id"]))["jobs"][0]
    assert listed["status"] == "completed" and len(listed["imported_file_ids"]) == 1
    new_file = editor.file(listed["imported_file_ids"][0])
    assert new_file["name"].startswith("generation")


def test_create_music_splits_lyrics_like_the_dialog(editor, comfy, server):
    _receipt(editor.call("create_media_with_comfyui_tool", kind="music",
                         prompt="lofi hip hop, mellow\nLyrics:\nla la la"))
    comfy.run_next()
    node = next(n for n in server.prompts[0]["prompt"].values() if "tags" in n.get("inputs", {}))
    assert node["inputs"]["tags"] == "lofi hip hop, mellow" and node["inputs"]["lyrics"] == "la la la"


@pytest.mark.parametrize("args, message", [
    ({"prompt": "x"}, "say what to make"),
    ({"prompt": "x", "template": "Warp Drive"}, "no Create template matches 'Warp Drive'"),
    ({"prompt": "   ", "kind": "image"}, "prompt is empty"),
    ({"prompt": "x", "kind": "portrait"}, "must be one of"),
])
def test_create_refusals(editor, comfy, args, message):
    out = editor.call("create_media_with_comfyui_tool", **args)
    assert out.startswith("Error") and message in out, out
    assert comfy.queue.jobs == {}


def test_create_by_menu_name(editor, comfy):
    r = _receipt(editor.call("create_media_with_comfyui_tool", template="Sound...", prompt="door slam"))
    assert r["job"]["template"] == "txt2audio-stable-open"


# --- enhance ----------------------------------------------------------------------------------------

def test_enhance_image_upscale_uploads_the_source_and_names_the_output(editor, comfy, server, tmp_path):
    fid = _image_file(editor, tmp_path)
    r = _receipt(editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="upscale"))
    assert r["job"]["template"] == "upscale-realesrgan-x4" and r["job"]["source_file_id"] == fid
    assert r["job"]["name"] == "photo_gen1"
    finished = comfy.run_next()
    assert server.uploads == ["photo.png"]
    load = _node(server.prompts[0]["prompt"], "LoadImage")[0]
    assert load["inputs"]["image"] == "photo.png [input]"
    assert finished["status"] == "completed" and finished["imported_file_ids"]
    assert editor.file(finished["imported_file_ids"][0])["name"] == "photo_gen1 [Increase Resolution (4x)]"


def test_enhance_video_blur_object_sends_points_boxes_and_the_tracking_selection(editor, comfy, server, tmp_path):
    fid = _video_file(editor, tmp_path)
    r = _receipt(editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="blur_object",
                             prompt="license plate", points=[{"x": 100, "y": 200}],
                             negative_points=[{"x": 5, "y": 6}], boxes=[{"x1": 90, "y1": 190, "x2": 50, "y2": 260}],
                             seed_frame=12))
    assert r["job"]["template"] == "video-blur-anything-sam2"
    comfy.run_next()
    sam = _node(server.prompts[0]["prompt"], "OpenShotSam2VideoSegmentationAddPoints")[0]["inputs"]
    assert json.loads(sam["positive_points_json"]) == [{"x": 100, "y": 200}]
    assert json.loads(sam["negative_points_json"]) == [{"x": 5, "y": 6}]
    assert json.loads(sam["positive_rects_json"]) == [{"x1": 50, "y1": 190, "x2": 90, "y2": 260}]
    assert sam["dino_prompt"] == "license plate"
    tracking = json.loads(sam["tracking_selection_json"])
    assert tracking["seed_frame"] == 12 and tracking["frames"]["12"]["positive_points"] == [{"x": 100, "y": 200}]
    assert sam["frame_index"] == 11


def test_enhance_highlight_sends_the_colors_in_the_dialogs_argb_form(editor, comfy, server, tmp_path):
    fid = _image_file(editor, tmp_path)
    _receipt(editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="highlight_object",
                         auto_detect=True, highlight_color="red", border_width=4))
    comfy.run_next()
    hl = _node(server.prompts[0]["prompt"], "OpenShotImageHighlightMasked")[0]["inputs"]
    assert hl["highlight_color"] == "#ffff0000" and hl["border_color"] == "#ffffffff"
    assert hl["border_width"] == 4 and hl["highlight_opacity"] == pytest.approx(0.28)
    assert hl["mask_brightness"] == pytest.approx(1.15) and hl["background_brightness"] == pytest.approx(0.75)


def test_enhance_restyle_video_binds_the_reference_image(editor, comfy, server, tmp_path):
    video = _video_file(editor, tmp_path)
    ref = _image_file(editor, tmp_path, "look.png")
    _receipt(editor.call("enhance_file_with_comfyui_tool", file_id=video, action="restyle",
                         prompt="watercolor painting", reference_image_file_id=ref))
    comfy.run_next()
    graph = server.prompts[0]["prompt"]
    assert _node(graph, "LoadImage")[0]["inputs"]["image"] == "look.png [input]"
    assert any(n["inputs"].get("text") == "watercolor painting" for n in _node(graph, "CLIPTextEncode"))


@pytest.mark.parametrize("args, message", [
    ({"action": "upscale", "file": "audio"}, "does not apply to audio files"),
    ({}, "say what to do"),
    ({"action": "blur_object"}, "object tracking needs a seed"),
    ({"action": "blur_object", "points": [{"x": 5000, "y": 10}]}, "outside the 1280x720 source frame"),
    ({"action": "blur_object", "auto_detect": True, "seed_frame": 999}, "beyond the file's 300 frame(s)"),
    ({"action": "upscale", "points": [{"x": 5, "y": 10}]}, "are for object tracking"),
    ({"action": "restyle", "prompt": "oil"}, "needs a reference image"),
    ({"action": "restyle", "reference": True}, "needs a prompt"),
    ({"action": "blur_object", "boxes": [{"x1": 5, "y1": 5, "x2": 5, "y2": 9}]}, "has no area"),
    ({"template": "Increase Resolution"}, "no Enhance (video) template matches"),
    ({"action": "highlight_object", "auto_detect": True, "highlight_color": "sparkly"}, "is not a color"),
    ({"action": "blur_object", "points": [[1, 2]]}, "must be an object"),
])
def test_enhance_refusals_happen_before_anything_is_queued(editor, comfy, tmp_path, args, message):
    args = dict(args)
    if args.pop("file", "") == "audio":
        fid = editor.add_file("audio")
    else:
        fid = _video_file(editor, tmp_path)
    if args.pop("reference", False):
        args["reference_image_file_id"] = _image_file(editor, tmp_path, "ref.png")
    out = editor.call("enhance_file_with_comfyui_tool", file_id=fid, **args)
    assert out.startswith("Error") and message in out, out
    assert comfy.queue.jobs == {}


def test_reference_image_must_be_an_image(editor, comfy, tmp_path):
    video = _video_file(editor, tmp_path)
    other = _video_file(editor, tmp_path, "other.mp4")
    out = editor.call("enhance_file_with_comfyui_tool", file_id=video, action="restyle", prompt="oil",
                      reference_image_file_id=other)
    assert out.startswith("Error: reference_image_file_id=") and "is a video file" in out


def test_one_active_job_per_file(editor, comfy, tmp_path):
    fid = _image_file(editor, tmp_path)
    first = _receipt(editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="upscale"))["job"]
    out = editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="depth")
    assert out.startswith("Error: Only one active generation is allowed per source file.")
    assert first["job_id"] in out


# --- jobs -------------------------------------------------------------------------------------------

def test_jobs_list_wait_and_refusals(editor, comfy):
    job = _receipt(editor.call("create_media_with_comfyui_tool", kind="image", prompt="a fox"))["job"]
    queued = _receipt(editor.call("create_media_with_comfyui_tool", kind="video", prompt="waves"))["job"]
    assert queued["status"] == "queued"
    r = _receipt(editor.call("list_comfyui_jobs_tool"))
    assert [j["job_id"] for j in r["jobs"]] == [queued["job_id"], job["job_id"]]
    assert editor.call("list_comfyui_jobs_tool", wait_seconds=5).startswith("Error: wait_seconds needs a job_id")
    assert editor.call("list_comfyui_jobs_tool", job_id="zzz").startswith("Error: no generation job 'zzz'")

    def finish_soon():
        time.sleep(0.4)
        comfy.run_next()

    t = threading.Thread(target=finish_soon)
    t.start()
    started = time.monotonic()
    out = editor.call("list_comfyui_jobs_tool", job_id=job["job_id"], wait_seconds=10)
    t.join()
    assert time.monotonic() - started < 5
    assert "is completed; imported file ids:" in out


def test_a_failed_agent_job_reports_its_error_without_a_modal(editor, comfy, server):
    server.fail_with = "Node 'KSampler' missing model"
    job = _receipt(editor.call("create_media_with_comfyui_tool", kind="image", prompt="a fox"))["job"]
    finished = comfy.run_next()
    assert finished["status"] == "failed"
    comfy.message_boxes.warning.assert_not_called()
    out = editor.call("list_comfyui_jobs_tool", job_id=job["job_id"])
    assert "is failed; error:" in out and "missing model" in out


def test_a_failed_ui_job_still_shows_the_dialog(editor, comfy, server):
    server.fail_with = "boom"
    _receipt(editor.call("create_media_with_comfyui_tool", kind="image", prompt="a fox"))
    next(iter(comfy.queue.jobs.values()))["origin"] = "ui"
    comfy.run_next()
    comfy.message_boxes.warning.assert_called_once()


def test_cancel_queued_running_by_file_and_all(editor, comfy, tmp_path):
    fid = _image_file(editor, tmp_path)
    running = _receipt(editor.call("enhance_file_with_comfyui_tool", file_id=fid, action="upscale"))["job"]
    queued = _receipt(editor.call("create_media_with_comfyui_tool", kind="image", prompt="a fox"))["job"]

    r = _receipt(editor.call("cancel_comfyui_job_tool", job_id=queued["job_id"]))
    assert r["jobs"][0]["status"] == "canceled"
    assert editor.call("cancel_comfyui_job_tool", job_id=queued["job_id"]).startswith(
        "Error: job %s is canceled" % queued["job_id"])
    r = _receipt(editor.call("cancel_comfyui_job_tool", file_id=fid))
    assert r["jobs"][0]["job_id"] == running["job_id"] and r["jobs"][0]["status"] == "canceling"
    comfy.queue._cancel_job.emit.assert_called_with(running["job_id"])
    assert editor.call("cancel_comfyui_job_tool", all_jobs=True).startswith("Error: no queued or running")
    assert editor.undo_steps_since_mark() == 0


@pytest.mark.parametrize("args", [{}, {"job_id": "a", "all_jobs": True}, {"job_id": "a", "file_id": "b"}])
def test_cancel_needs_exactly_one_target(editor, comfy, args):
    assert editor.call("cancel_comfyui_job_tool", **args).startswith("Error: pass exactly one of")


def test_cancel_unknown_and_all(editor, comfy):
    assert editor.call("cancel_comfyui_job_tool", job_id="nope").startswith("Error: no generation job 'nope'")
    _receipt(editor.call("create_media_with_comfyui_tool", kind="image", prompt="a"))
    _receipt(editor.call("create_media_with_comfyui_tool", kind="image", prompt="b"))
    r = _receipt(editor.call("cancel_comfyui_job_tool", all_jobs=True))
    assert sorted(j["status"] for j in r["jobs"]) == ["canceled", "canceling"]


# --- parity with the Generate dialog ------------------------------------------------------------------

def test_the_payload_has_exactly_the_generate_dialog_keys(editor, comfy):
    with open(os.path.join(_SRC, "windows", "generate.py"), encoding="utf-8") as fh:
        source = fh.read()
    block = source[source.index("def get_payload"):source.index("def _build_top_block")]
    dialog_keys = set(re.findall(r'^\s+"(\w+)":', block, re.M))
    from classes.editor_tools.ai_generation_comfyui import _empty_payload
    assert set(_empty_payload("n", "t", "p")) == dialog_keys


def test_a_generation_placeholder_row_is_not_renamed_as_a_file(editor):
    """Regression (found live): every progress update of a Create-with-AI placeholder row raised
    AttributeError in FilesTreeView.value_updated (File.get(placeholder id) is None)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import windows.views.files_treeview as ft

    cells = {5: "generation-job-123", 1: "generation (42%)", 2: ""}
    model = MagicMock()
    model.item.side_effect = lambda row, col: SimpleNamespace(text=lambda: cells[col])
    view = SimpleNamespace(files_model=SimpleNamespace(ignore_updates=False, model=model), win=MagicMock())
    ft.FilesTreeView.value_updated(view, SimpleNamespace(row=lambda: 0))
    view.win.FileUpdated.emit.assert_not_called()
